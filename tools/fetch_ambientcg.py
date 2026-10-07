# -*- coding: utf-8 -*-
"""
fetch_ambientcg.py -- fetch the CC0 colour maps named in docs/s5_material_spec.md.

Serial, with retry, and it records what it resolved into a manifest so a rebuild
never re-hits the network (spec section 7 risk 9: the API is unauthenticated but
rate-limited, and a parallel fetch across 11 families gets 429).

    python3 tools/fetch_ambientcg.py                  # fetch what is missing
    python3 tools/fetch_ambientcg.py --measure-only    # measure, do not fetch

Fetch 1K-JPG, not PNG: build_atlas.py resizes anything that is not
SOURCE_TILE=1024 down to SOURCE_TILE, so a 1K source drops in with no change,
and a 1K JPG is a fraction of the bytes of the 2K PNG.
"""

import argparse
import io
import json
import os
import sys
import time
import zipfile

try:
    from urllib.request import Request, urlopen
except ImportError:                                        # py2
    from urllib2 import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

#: Where the fetched maps land. Outside the repo, alongside the existing
#: polyhaven tree, for the same reason build_atlas.py's default --assets is:
#: the in-repo assets_cc0 is a stub and binaries do not belong in git.
DEST = "Q:/MC2UE5/assets_cc0/ambientcg"

#: family -> ambientCG asset id. The eight re-sourced families come straight
#: from spec section 3; `path` is PavingStones103, which the spec calls the
#: cheapest fix in the document (this cell already measures R-B +8.4).
ASSETS = {
    "grass":  "Grass004",
    "leaves": "Leaf003",
    "bark":   "Bark001",
    "roof":   "RoofingTiles004",
    "brick":  "Bricks074",
    "gravel": "Gravel030",
    "metal":  "MetalPlates006",
    "path":   "PavingStones103",
}

#: Extra candidates measured only, never shipped. Present so the choice in
#: ASSETS is auditable rather than asserted -- if Grass004 stops being the
#: greenest grass, the runner-up is on disk already.
EXTRA = {
    "grass_alt":  "Grass008",
    "leaves_alt": "Leaf002",
    "metal_alt":  "Metal032",
    "brick_alt":  "Bricks079",
    "gravel_alt": "Gravel022",
}


def _get(url, timeout=90):
    req = Request(url, headers={"User-Agent": "MC2UE5-build_atlas/1.0"})
    return urlopen(req, timeout=timeout)


def fetch_one(asset_id, dest_root, retries=3):
    """-> (path_to_jpg, note). Downloads <ID>_1K-JPG.zip and extracts *_Color.jpg."""
    outdir = os.path.join(dest_root, asset_id)
    marker = os.path.join(outdir, asset_id + "_Color.jpg")
    if os.path.isfile(marker):
        return marker, "cached"

    url = "https://ambientcg.com/get?file=%s_1K-JPG.zip" % asset_id
    last = None
    for attempt in range(retries):
        try:
            raw = _get(url).read()
            break
        except Exception as exc:                            # noqa: BLE001
            last = exc
            # Rate limited or transient: back off, do not hammer.
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError("%s: fetch failed after %d tries: %s"
                           % (asset_id, retries, last))

    zf = zipfile.ZipFile(io.BytesIO(raw))
    # 1K-JPG zips contain <ID>_Color.jpg / _NormalGL.jpg / _Roughness.jpg /
    # _AO.jpg / _Displacement.jpg. Take the colour map only: the atlas packs
    # BaseColor and the material graph has exactly one TextureSampleParameter2D.
    names = [n for n in zf.namelist()
             if n.lower().endswith((".jpg", ".jpeg"))
             and "color" in os.path.basename(n).lower()
             and "normal" not in os.path.basename(n).lower()]
    if not names:
        raise RuntimeError("%s: no *_Color.jpg inside the zip (%s)"
                           % (asset_id, zf.namelist()[:8]))
    name = sorted(names, key=len)[0]

    os.makedirs(outdir, exist_ok=True)
    dst = os.path.join(outdir, asset_id + "_Color.jpg")
    with open(dst, "wb") as fh:
        fh.write(zf.read(name))
    # Drop roughness/normal/ao so the tree does not imply they were fetched.
    for other in zf.namelist():
        if other == name:
            continue
        base = os.path.basename(other).lower()
        if any(k in base for k in ("normal", "roughness", "_ao", "disp")):
            try:
                os.remove(os.path.join(outdir, other))
            except OSError:
                pass
    return dst, "downloaded %s" % name


def measure(path):
    """-> (mean_rgb tuple, saturation pct). Reads the real file."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    r, g, b = im.split()
    n = im.size[0] * im.size[1]
    mean = (sum(r.getdata()) / float(n),
            sum(g.getdata()) / float(n),
            sum(b.getdata()) / float(n))
    mx, mn = max(mean), min(mean)
    sat = 0.0 if mx <= 0 else (mx - mn) / mx * 100.0
    return mean, sat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=DEST)
    ap.add_argument("--measure-only", action="store_true")
    ap.add_argument("--out", default=os.path.join(REPO, "out", "atlas",
                                                  "ambientcg_sources.json"))
    args = ap.parse_args()

    report = {}
    failures = []
    wanted = dict(ASSETS)
    wanted.update(EXTRA)

    for key, asset_id in sorted(wanted.items()):
        shipped = key in ASSETS
        try:
            if args.measure_only:
                path = os.path.join(args.dest, asset_id,
                                    asset_id + "_Color.jpg")
                if not os.path.isfile(path):
                    raise RuntimeError("not on disk: %s" % path)
                note = "measured only"
            else:
                path, note = fetch_one(asset_id, args.dest)
            mean, sat = measure(path)
            report[key] = {"asset": asset_id, "path": path,
                           "shipped": shipped, "note": note,
                           "size": list(im_size(path)),
                           "mean": [round(v, 1) for v in mean],
                           "rb": round(mean[0] - mean[2], 1),
                           "sat_pct": round(sat, 1)}
            flag = "SHIP" if shipped else "alt "
            print("  %s %-16s %-20s mean (%5.1f, %5.1f, %5.1f)  R-B %+6.1f  sat %4.1f%%  %s"
                  % (flag, key, asset_id, mean[0], mean[1], mean[2],
                     mean[0] - mean[2], sat, note))
        except Exception as exc:                            # noqa: BLE001
            failures.append((key, str(exc)))
            print("  FAIL %-16s %-20s %s" % (key, asset_id, exc))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump({
            "licence": "CC0 1.0 Universal (public domain dedication)",
            "source": "ambientCG (https://ambientcg.com)",
            "api": "https://ambientcg.com/get?file=<ASSET>_1K-JPG.zip",
            "map": "Color only. Roughness/normal stay separate streaming "
                   "textures; the master material has one TextureSampleParameter2D "
                   "and an atlas cannot hold three channels per cell without "
                   "either tripling its size or packing channels.",
            "dest": args.dest,
            "assets": report,
            "failures": [{"key": k, "error": e} for k, e in failures],
        }, fh, indent=2)
    print()
    print("%d measured, %d failed -> %s"
          % (len(report), len(failures),
             os.path.relpath(args.out).replace("\\", "/")))
    return 1 if failures else 0


def im_size(path):
    from PIL import Image
    return Image.open(path).size


if __name__ == "__main__":
    sys.exit(main())
