# -*- coding: utf-8 -*-
"""
calibrate_bake.py -- solve the per-channel bake gains that actually land.

Why this exists. docs/s5_material_spec.md section 2 lists a gain per family and
says it was checked as `gain * measured = target`. Those `measured` values do
not match the files that ambientCG actually serves: measured against the real
1K-JPG colour maps, the ratio of actual to documented runs from 0.88
(RoofingTiles004) to 1.68 (Bark001). The asset *choices* all hold up -- every
one of the eight reproduces the hue property the spec selected it for, and the
runner-ups are worse on the same measure -- but the gains computed from the
documented numbers would miss the targets by up to 40/255.

And `gain * measured = target` is not the equation anyway, because
`Image.point()` clamps at 255. A gain of 1.65 on a channel whose texels reach
255 saturates the highlights, so the realised mean lands *below*
`gain * measured`. Solving the arithmetic would leave the bright families
short of target by more than the +/-6 the verifier allows.

So: solve each channel independently by bisection on the *realised* mean of the
real pipeline -- open, resize to 1024, resize to 504 with LANCZOS, apply the
LUT, measure. R, G and B are independent under `point()`, so three 1-D solves
are exact and need no search over triples.

    python3 tools/calibrate_bake.py            # print the table
    python3 tools/calibrate_bake.py --emit     # print a BAKE_GAINS block
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from build_atlas import BAKE_GAINS, EXPECTED_MEAN, GENERATED_RGB  # noqa: E402

SOURCE_TILE = 1024
TILE = 504


def prepare(path):
    """The exact image the bake sees: open, resize to 1024, resize to 504."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if im.size != (SOURCE_TILE, SOURCE_TILE):
        im = im.resize((SOURCE_TILE, SOURCE_TILE), Image.LANCZOS)
    if TILE != SOURCE_TILE:
        im = im.resize((TILE, TILE), Image.LANCZOS)
    return im


def channel_mean(im, ch):
    n = TILE * TILE
    return sum(im.split()[ch].getdata()) / float(n)


def apply_gain(im, gain, ch=None):
    """Apply `gain` to one channel, or to all three when ch is None.

    Pillow insists on a 768-entry table for an RGB image whatever you intend,
    so a single-channel solve passes identity for the other two bands. That
    keeps the solve honest -- the band being measured is untouched by the
    identity entries.
    """
    ident = list(range(256))
    if ch is None:
        lut = []
        for g in gain:
            lut += [min(255, int(v * g)) for v in range(256)]
        return im.point(lut)
    lut = []
    for band in range(3):
        lut += ([min(255, int(v * gain)) for v in range(256)]
                if band == ch else ident)
    return im.point(lut)


def realised_mean(im, gains):
    """Mean after applying the three LUTs -- the same arithmetic as bake()."""
    out = apply_gain(im, gains)
    return (channel_mean(out, 0), channel_mean(out, 1), channel_mean(out, 2))


def solve_channel(im, ch, target, lo=0.05, hi=8.0, iters=40):
    """Bisect on the gain whose realised mean for `ch` is closest to `target`."""
    best, best_err = 1.0, None
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        got = channel_mean(apply_gain(im, mid, ch), ch)
        err = abs(got - target)
        if best_err is None or err < best_err:
            best, best_err = mid, err
        if got < target:
            lo = mid
        else:
            hi = mid
    return best, best_err


def clip_percent(im, gains):
    """Fraction of texels with any channel pinned at 255 after the bake.

    Reported, never silently accepted. A gain that flattens a sixth of a cell
    to pure white has not fixed the colour, it has deleted the texture -- and
    a mean-only check cannot see it, because the mean still lands on target.
    This is the one place a pure-multiply LUT is the wrong tool: `leaves`
    needed a 2.65x green gain to reach the spec's target, which is only
    reachable by clipping, so the target moved instead.
    """
    px = apply_gain(im, gains).getdata()
    n = sum(1 for _ in px)
    hit = sum(1 for p in px if p[0] >= 255 or p[1] >= 255 or p[2] >= 255)
    return 100.0 * hit / float(n)


def stddev(im, gains):
    """Per-channel standard deviation after the bake -- the detail check."""
    out = apply_gain(im, gains)
    res = []
    n = TILE * TILE
    for band in out.split():
        d = list(band.getdata())
        m = sum(d) / float(n)
        res.append((sum((x - m) ** 2 for x in d) / float(n)) ** 0.5)
    return res


def source_for(fam, ambientcg_root, assets_root, acg):
    """-> (path, note) for a family, matching build_atlas.py's own resolution."""
    if fam in acg:
        p = os.path.join(ambientcg_root, acg[fam], "%s_Color.jpg" % acg[fam])
        if os.path.isfile(p):
            return p, "ambientCG %s" % acg[fam]
    d = os.path.join(assets_root, fam)
    for name in ("%s_diffuse.jpg" % fam, "%s_diffuse.png" % fam):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p, "polyhaven %s" % fam
    return None, "MISSING"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ambientcg", default="Q:/MC2UE5/assets_cc0/ambientcg")
    ap.add_argument("--assets", default="Q:/MC2UE5/assets_cc0/polyhaven")
    ap.add_argument("--emit", action="store_true",
                    help="print a paste-ready BAKE_GAINS block")
    ap.add_argument("--out", default=os.path.join(REPO, "out", "atlas",
                                                  "bake_calibration.json"))
    args = ap.parse_args()

    from build_atlas import RESOURCE, SPORT_FAMILY

    # sports reuses the grass source with a different target, so it resolves
    # through the same map.
    acg = dict(RESOURCE)
    acg[SPORT_FAMILY] = RESOURCE["grass"]

    report = {}
    print("solving each channel by bisection on the realised mean "
          "(clamp included)")
    print()
    print("  %-9s %-24s %-24s %-22s %s"
          % ("family", "measured (504, pre-bake)", "target", "solved gain",
             "residual"))
    print("  " + "-" * 96)

    for fam in sorted(EXPECTED_MEAN):
        want = EXPECTED_MEAN[fam]
        if fam in GENERATED_RGB:
            continue
        path, note = source_for(fam, args.ambientcg, args.assets, acg)
        if path is None:
            print("  %-9s MISSING under %s / %s" % (fam, args.ambientcg,
                                                    args.assets))
            report[fam] = {"error": "missing source"}
            continue
        im = prepare(path)
        before = (channel_mean(im, 0), channel_mean(im, 1), channel_mean(im, 2))

        gains, errs = [], []
        for ch in range(3):
            g, e = solve_channel(im, ch, want[ch])
            gains.append(round(g, 4))
            errs.append(e)
        after = realised_mean(im, gains)
        clip = clip_percent(im, gains)
        sd = stddev(im, gains)
        sd_before = stddev(im, (1.0, 1.0, 1.0))

        print("  %-9s (%5.1f, %5.1f, %5.1f)   (%3d, %3d, %3d)      "
              "(%.2f, %.2f, %.2f)      max %.2f  clip %4.1f%%"
              % (fam, before[0], before[1], before[2], want[0], want[1],
                 want[2], gains[0], gains[1], gains[2], max(errs), clip))
        report[fam] = {"source": path, "origin": note,
                       "measured_prebake": [round(v, 1) for v in before],
                       "target": list(want),
                       "solved_gain": gains,
                       "realised": [round(v, 1) for v in after],
                       "residual": [round(after[i] - want[i], 2)
                                    for i in range(3)],
                       "clip_pct": round(clip, 2),
                       "stddev_before": [round(v, 1) for v in sd_before],
                       "stddev_after": [round(v, 1) for v in sd],
                       "documented_gain": list(BAKE_GAINS.get(fam, [])) or None}

    # The aggregate, computed from the solved realisations, so the <= +10
    # ceiling is checked here too rather than discovered at the end.
    fixed = [report[f]["realised"] for f in report
             if "realised" in report[f]]
    gen = [list(GENERATED_RGB[f]) for f in GENERATED_RGB]
    allm = fixed + gen
    n = len(allm)
    agg = [sum(m[i] for m in allm) / n for i in range(3)]
    print()
    print("  projected aggregate over %d cells (%5.1f, %5.1f, %5.1f)  R-B %+.1f"
          "   ceiling +10" % (n, agg[0], agg[1], agg[2], agg[0] - agg[2]))

    with open(args.out, "w") as fh:
        json.dump({"tile": TILE, "source_tile": SOURCE_TILE,
                   "method": "per-channel bisection on the realised mean of the "
                             "real pipeline, clamp at 255 included",
                   "families": report,
                   "projected_aggregate": [round(v, 1) for v in agg],
                   "projected_aggregate_rb": round(agg[0] - agg[2], 1)},
                  fh, indent=2)
    print("  report: %s" % os.path.relpath(args.out).replace("\\", "/"))

    if args.emit:
        print()
        print("BAKE_GAINS = {")
        for fam in sorted(report):
            if "solved_gain" in report[fam]:
                print('    "%-9s": (%s),' % (
                    fam, ", ".join("%.4f" % g
                                   for g in report[fam]["solved_gain"])))
        print("}")


if __name__ == "__main__":
    sys.exit(main())
