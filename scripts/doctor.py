"""
MC2UE5 environment doctor.

The sandbox can lose pip packages across hibernation cycles -- this bit us once:
`nbt` silently disappeared, `parse_world.py` then failed on 3452 of 17029
overworld chunks (20%) with a confusing
`AttributeError: 'list' object has no attribute 'size'`, because a *different*
build of the `nbt` package was importable and its `TAG_List` iterated
differently.

The failure was silent-ish (it only showed up as an error count in stats.json),
so this doctor exists to turn that class of problem into an immediate, loud one.

Usage:
    python3 scripts/doctor.py
Exit code 0 = healthy, 1 = something is wrong.
"""

import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# (import name, why we need it, minimum version or None)
REQUIRED = [
    ("nbt", "layer 1 chunk/level.dat NBT decoding", "1.5.0"),
    ("numpy", "vectorised palette unpacking", "1.24"),
]

# Needed by phase 2 only; absence is reported as a warning, not a failure.
PHASE2 = [
    ("scipy", "trilinear upsample + anisotropic gaussian"),
    ("skimage", "edge-preserving staircase removal"),
    ("cv2", "morphology + connected components"),
    ("trimesh", "mesh validation / export"),
    ("PIL", "heightmap PNG encoding"),
]


def _version(mod):
    """
    Distribution version, not module attribute.

    `nbt` (the NBT package) does not expose `__version__`, so fall back to the
    installed distribution metadata. Preferring `__version__` first keeps this
    working for packages that only set the attribute.
    """
    v = getattr(mod, "__version__", None)
    if v:
        return v
    try:
        from importlib import metadata
        name = "NBT" if mod.__name__ == "nbt" else mod.__name__
        return metadata.version(name)
    except Exception:
        return "unknown"


def _parse_ver(v):
    out = []
    for part in str(v).split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return out


def check_required():
    problems = []
    for name, why, minver in REQUIRED:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:
            problems.append("MISSING %-10s (%s) -- %s" % (name, why, exc))
            continue
        got = _version(mod)
        if minver and _parse_ver(got) < _parse_ver(minver):
            problems.append("OLD     %-10s have %s, need >= %s" % (name, got, minver))
        else:
            print("  ok  %-10s %-10s %s" % (name, got, why))
    return problems


def check_phase2():
    warnings = []
    for name, why in PHASE2:
        try:
            mod = importlib.import_module(name)
            print("  ok  %-10s %-10s %s" % (name, _version(mod), why))
        except Exception as exc:
            warnings.append("MISSING %-10s (%s) -- %s" % (name, why, exc))
    return warnings


def check_artifacts():
    """Layer 1 outputs that phase 2 consumes. Read-only consumption."""
    problems = []
    stats = os.path.join(ROOT, "parse", "stats.json")
    if not os.path.isfile(stats):
        problems.append("MISSING parse/stats.json -- run parse_world.py first")
    else:
        with open(stats) as f:
            data = json.load(f)
        for dim, info in data["dimensions"].items():
            errs = info.get("error_count", 0)
            flag = "ok " if errs == 0 else "BAD"
            print("  %s %-9s chunks=%-6d nonair=%-10d errors=%d"
                  % (flag, dim, info["chunk_count"],
                     info["non_air_blocks"], errs))
            if errs:
                problems.append(
                    "PARSE ERRORS in %s: %d chunks failed -- inspect "
                    "parse/stats.json .errors; usually a broken `nbt` install"
                    % (dim, errs))

    for dim in ("overworld", "nether", "end"):
        p = os.path.join(ROOT, "voxel_data", "full", dim + ".bin")
        if not os.path.isfile(p):
            problems.append("MISSING %s -- run: parse_world.py --save <save> --full"
                            % p)
        else:
            with open(p, "rb") as f:
                magic = f.read(8)
            if magic != b"MC2WV2\0\0":
                problems.append("BAD MAGIC in %s: %r (v1 files are truncated in Y "
                                "and must not be mixed with v2)" % (p, magic))
            else:
                print("  ok  voxel_data/full/%s.bin (%d bytes)"
                      % (dim, os.path.getsize(p)))
    return problems


def check_consistency():
    """Cross-check that the exported voxel files agree with stats.json."""
    sys.path.insert(0, HERE)
    try:
        import read_sample as rs
    except Exception as exc:
        return ["could not import read_sample.py: %r" % exc]

    problems = []
    with open(os.path.join(ROOT, "parse", "stats.json")) as f:
        data = json.load(f)
    for dim, info in data["dimensions"].items():
        p = os.path.join(ROOT, "voxel_data", "full", dim + ".bin")
        if not os.path.isfile(p):
            continue
        with open(p, "rb") as f:
            hdr = rs.read_header(f)
        want = info["non_air_blocks"]
        got = hdr["voxel_count"]
        if got != want:
            problems.append("MISMATCH %s: voxel file has %d voxels, stats says %d"
                            % (dim, got, want))
        else:
            print("  ok  %-9s %d voxels match stats.json" % (dim, got))
    return problems


def main():
    print("MC2UE5 doctor  (python %s)" % sys.version.split()[0])
    print("\n[1/4] runtime deps")
    problems = check_required()
    print("\n[2/4] phase 2 deps (optional)")
    warnings = check_phase2()
    print("\n[3/4] layer 1 artifacts")
    problems += check_artifacts()
    print("\n[4/4] cross-consistency")
    problems += check_consistency()

    if warnings:
        print("\nWARNINGS (%d):" % len(warnings))
        for w in warnings:
            print("  - " + w)
        print("  install with: pip3 install -r scripts/requirements.txt")

    if problems:
        print("\nUNHEALTHY (%d problem(s)):" % len(problems))
        for p in problems:
            print("  ! " + p)
        return 1
    print("\nHEALTHY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
