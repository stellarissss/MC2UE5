# -*- coding: utf-8 -*-
"""
fetch_cc0_textures.py -- pull the CC0 PBR set the campus materials are built on.

The rendering rework replaces Minecraft's 16x16 block textures with real
materials, so each semantic family needs an albedo, a normal and a roughness
map. Poly Haven publishes CC0 textures with an API that lists per-map,
per-resolution URLs and an md5 per file, which makes the download verifiable
rather than hopeful.

Maps fetched per family: **Diffuse** (BaseColor), **nor_dx** (Unreal wants
DirectX-convention normals, not the OpenGL `nor_gl` set) and **Rough**
(Roughness). 1k is deliberate: the terrain shader tiles these over metres, and
the 4k/8k sets are tens of megabytes each over a slow link for no visible gain
at this scale.

    python3 tools/fetch_cc0_textures.py --out Q:/MC2UE5/assets_cc0/polyhaven
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys


API_FILES = "https://api.polyhaven.com/files/%s"
MAPS = ("Diffuse", "nor_dx", "Rough")
RES = "1k"
EXT = "jpg"

#: material family -> Poly Haven asset id. Chosen for the campus: playing
#: fields, paths and roads, the concrete/brick/plaster school buildings, their
#: tiled roofs, timber, plus stone and planting.
FAMILIES = {
    "grass":        "grass_ground",
    "path":         "grass_path_3",
    "soil":         "dirt",
    "asphalt":      "asphalt_01",
    "concrete":     "brushed_concrete",
    "plaster":      "beige_wall_001",
    "brick":        "brick_4",
    "granite":      "granite_tile",
    "tiles":        "concrete_tiles_02",
    "roof":         "clay_roof_tiles",
    "wood":         "brown_planks_03",
    "bark":         "bark_brown_01",
    "leaves":       "forest_leaves_04",
    "metal":        "metal_plate",
    "gravel":       "gravel_floor",
    "rock":         "rock_face",
    "fabric":       "cotton_jersey",
    # MC quartz is a near-white smooth stone; marble is the closest real
    # material. Mapping it to the brown `rock` texture made the campus
    # courtyard (31% of the surface) read as soil.
    "quartz":       "marble_01",
    "greystone":    "stone_tiles",
}


def api(path):
    """
    Query the Poly Haven files API.

    Fetched through curl rather than urllib: the API answers urllib's default
    (absent) User-Agent with 403 Forbidden, while a normal client UA is served.
    """
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", "90", "-f",
         "-H", "User-Agent: MC2UE5-texture-fetch",
         API_FILES % path],
        capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("api %s: rc=%d %s" % (
            path, proc.returncode, proc.stderr.decode()[:120]))
    return json.loads(proc.stdout.decode("utf-8"))


def download(url, dest, md5=None, attempts=4):
    tmp = dest + ".part"
    for attempt in range(attempts):
        proc = subprocess.run(
            ["curl", "-sS", "-L", "--max-time", "300", "-o", tmp,
             "-w", "%{http_code}", url], capture_output=True)
        code = proc.stdout.decode().strip()
        if code == "200" and os.path.exists(tmp):
            if md5:
                h = hashlib.md5()
                with open(tmp, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != md5:
                    print("      md5 mismatch (attempt %d)" % (attempt + 1))
                    continue
            os.replace(tmp, dest)
            return True
        print("      http %s (attempt %d)" % (code, attempt + 1))
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--res", default=RES)
    ap.add_argument("--maps", default=",".join(MAPS))
    ap.add_argument("--only", default="", help="comma-separated families")
    args = ap.parse_args()

    maps = [m for m in args.maps.split(",") if m]
    only = {f for f in args.only.split(",") if f}
    families = {k: v for k, v in FAMILIES.items() if not only or k in only}

    total = done = failed = 0
    manifest = {}
    for fam, asset in sorted(families.items()):
        try:
            info = api(asset)
        except Exception as exc:
            print("%-10s API FAILED (%s)" % (fam, exc))
            failed += len(maps)
            total += len(maps)
            continue
        outdir = os.path.join(args.out, fam)
        os.makedirs(outdir, exist_ok=True)
        print("%-10s <- %s" % (fam, asset))
        entries = {}
        for m in maps:
            node = (info.get(m) or {}).get(args.res) or {}
            entry = node.get(EXT)
            total += 1
            if not entry:
                print("    %-8s (no %s %s)" % (m, args.res, EXT))
                failed += 1
                continue
            dest = os.path.join(outdir, "%s_%s.%s" % (fam, m.lower(), EXT))
            if os.path.exists(dest) and os.path.getsize(dest) == entry["size"]:
                print("    %-8s cached" % m)
                done += 1
                entries[m] = os.path.basename(dest)
                continue
            if download(entry["url"], dest, entry.get("md5")):
                print("    %-8s ok  %d bytes" % (m, entry["size"]))
                done += 1
                entries[m] = os.path.basename(dest)
            else:
                print("    %-8s FAILED" % m)
                failed += 1
        manifest[fam] = {"asset": asset, "maps": entries}

    with open(os.path.join(args.out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    print("=" * 60)
    print("fetched %d/%d files, %d failed" % (done, total, failed))
    print("license: CC0 (Poly Haven) -- see assets_cc0/polyhaven/manifest.json")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
