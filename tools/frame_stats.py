# -*- coding: utf-8 -*-
"""
frame_stats.py -- objective texture-tiling evidence from a captured frame.

Written for S6A. The question this answers is narrow and the answer has to be
a number, because "it looks textured to me" is exactly the claim that was
already made once and turned out to be false.

Three measurements per region:

* **Local standard deviation** -- the image is tiled into 8x8 blocks, the
  standard deviation of luma is taken inside each block, and the mean over
  blocks is reported. A flat colour field (the failure mode: one atlas cell
  stretched across a whole greedy quad, so every 1 m block shows the cell's
  *average* colour) has a local std at the noise floor. Real per-block tiling
  has structure at the block scale and the number rises by an order of
  magnitude.

* **Gradient energy** -- mean(|dI/dx| + |dI/dy|) on luma, in 0..255 units per
  pixel. Same idea, first-difference instead of second-order so it is not
  dominated by single-pixel noise.

* **Colour diversity** -- luma-weighted share of the top-5 quantised colours,
  plus the count of distinguishable hue clusters. A stretched average-colour
  field collapses to one or two colours.

Usage:

    python frame_stats.py SHOT.png --region ground:400,600,1200,900
    python frame_stats.py SHOT.png --auto
    python frame_stats.py --diff A.png B.png

`--auto` uses the whole image plus a top-strip sky baseline; the image is not
otherwise segmented, because guessing where the facade is from statistics
would be circular. Pass explicit regions once the frame has been looked at.
"""

import argparse
import json
import sys

import numpy as np
from PIL import Image


# --------------------------------------------------------------------------- #
# measurements
# --------------------------------------------------------------------------- #

def luma(rgb):
    """Rec.601 luma, 0..255, float."""
    a = rgb.astype(np.float64)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def local_std(gray, block=8):
    """Mean of the per-block standard deviations, over `block`x`block` tiles."""
    h, w = gray.shape
    bh, bw = h // block, w // block
    if bh == 0 or bw == 0:
        return float(gray.std())
    crop = gray[:bh * block, :bw * block]
    tiles = crop.reshape(bh, block, bw, block).transpose(0, 2, 1, 3)
    return float(tiles.reshape(bh, bw, block * block).std(axis=2).mean())


def grad_energy(gray):
    """mean(|dI/dx| + |dI/dy|), in luma units per pixel."""
    gx = np.abs(np.diff(gray, axis=1)).mean()
    gy = np.abs(np.diff(gray, axis=0)).mean()
    return float(gx + gy)


def laplacian_energy(gray):
    """mean(|Laplacian|) with the 4-neighbour stencil."""
    lap = (4.0 * gray[1:-1, 1:-1]
           - gray[:-2, 1:-1] - gray[2:, 1:-1]
           - gray[1:-1, :-2] - gray[1:-1, 2:])
    return float(np.abs(lap).mean())


def colour_clusters(rgb, bits=5, topn=5):
    """Top-N quantised colours by share, and the number of clusters >1% share.

    `bits` per channel after a right shift, so bits=5 is 32 levels: coarse
    enough that two pixels of the same material land in the same bin, fine
    enough that grass / concrete / brick do not.
    """
    q = (rgb >> (8 - bits)).astype(np.int32)
    keys = (q[..., 0] << (2 * bits)) | (q[..., 1] << bits) | q[..., 2]
    vals, counts = np.unique(keys.ravel(), return_counts=True)
    order = np.argsort(counts)[::-1]
    total = counts.sum()
    out = []
    for i in order[:topn]:
        k = int(vals[i])
        r = ((k >> (2 * bits)) & ((1 << bits) - 1)) << (8 - bits)
        g = ((k >> bits) & ((1 << bits) - 1)) << (8 - bits)
        b = (k & ((1 << bits) - 1)) << (8 - bits)
        # centre the bin: +half a step, so 0 does not read as pure black
        half = 1 << (8 - bits - 1)
        out.append({
            "rgb": [int(r + half), int(g + half), int(b + half)],
            "share": round(float(counts[i]) / total, 4),
        })
    n_over_1pct = int((counts > total * 0.01).sum())
    return out, n_over_1pct


def region_stats(rgb):
    gray = luma(rgb)
    clusters, n_clusters = colour_clusters(rgb)
    return {
        "size": [int(rgb.shape[1]), int(rgb.shape[0])],
        "mean_rgb": [round(float(v), 1) for v in rgb.reshape(-1, 3).mean(axis=0)],
        "mean_luma": round(float(gray.mean()), 2),
        "std_luma": round(float(gray.std()), 2),
        "local_std_8x8": round(local_std(gray, 8), 2),
        "grad_energy": round(grad_energy(gray), 2),
        "laplacian_energy": round(laplacian_energy(gray), 3),
        "colour_clusters_gt1pct": n_clusters,
        "top_colours": clusters,
    }


def crop(img, x0, y0, x1, y1):
    w, h = img.size
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise SystemExit("region too small after clamping: %d,%d,%d,%d"
                         % (x0, y0, x1, y1))
    return np.asarray(img.convert("RGB").crop((x0, y0, x1, y1)))


# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*")
    ap.add_argument("--region", action="append", default=[],
                    metavar="NAME:x0,y0,x1,y1")
    ap.add_argument("--auto", action="store_true",
                    help="whole image + top-strip sky baseline")
    ap.add_argument("--diff", action="store_true",
                    help="with exactly 2 images: per-pixel difference")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    if not args.images:
        ap.error("need at least one image")

    report = {}

    if args.diff:
        if len(args.images) != 2:
            ap.error("--diff needs exactly 2 images")
        a = Image.open(args.images[0]).convert("RGB")
        b = Image.open(args.images[1]).convert("RGB")
        if a.size != b.size:
            report["diff"] = {"error": "size mismatch",
                              "a": a.size, "b": b.size}
        else:
            na = np.asarray(a).astype(np.int16)
            nb = np.asarray(b).astype(np.int16)
            d = np.abs(na - nb).max(axis=2)
            report["diff"] = {
                "a": args.images[0], "b": args.images[1],
                "max": int(d.max()),
                "mean": round(float(d.mean()), 4),
                "px_changed": int((d > 0).sum()),
                "px_gt8": int((d > 8).sum()),
                "px_total": int(d.size),
                "pct_gt8": round(100.0 * (d > 8).sum() / d.size, 4),
            }
        print(json.dumps(report, indent=1))
        if args.json:
            with open(args.json, "w") as fh:
                json.dump(report, fh, indent=1)
        return 0

    regions = []
    for spec in args.region:
        name, _, coords = spec.partition(":")
        try:
            x0, y0, x1, y1 = (int(v) for v in coords.split(","))
        except ValueError:
            ap.error("bad --region %r (want NAME:x0,y0,x1,y1)" % spec)
        regions.append((name, x0, y0, x1, y1))

    for path in args.images:
        img = Image.open(path).convert("RGB")
        w, h = img.size
        per = {"file": path, "size": [w, h], "regions": {}}

        if args.auto or not regions:
            per["regions"]["WHOLE"] = region_stats(
                np.asarray(img))
            # Sky baseline: the top 10% of rows, centre 60% of columns. Sky is
            # a smooth gradient, so it is the "flat" reference the ground and
            # facade numbers have to beat.
            per["regions"]["SKY_top10pct"] = region_stats(
                crop(img, int(w * 0.20), 0, int(w * 0.80), int(h * 0.10)))

        for name, x0, y0, x1, y1 in regions:
            per["regions"][name] = region_stats(crop(img, x0, y0, x1, y1))

        report[path] = per

    print(json.dumps(report, indent=1))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
