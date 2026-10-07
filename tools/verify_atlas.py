# -*- coding: utf-8 -*-
"""
verify_atlas.py -- read the atlas PNG back and assert what is actually in it.

Why this exists. The `Tint` vector parameter in S5 verified as set on the
instance, verified as connected to BaseColor, verified as compiled, and
changed **zero pixels**. Every check that failed to catch it trusted an
*intent* -- a parameter dump, a compile log, a "the script ran" claim. This
tool reads the artefact instead: the per-cell pixel means of the shipped PNG,
at the rectangles `plan_layout()` says the cells occupy.

It is deliberately standalone. It re-derives nothing about the layout: it
imports `plan_layout()` from build_atlas.py, the same function the mesher
imports, so a disagreement between packer and verifier is impossible by
construction.

    python3 tools/verify_atlas.py                 # verify out/atlas + the UE copy
    python3 tools/verify_atlas.py --png <path>
    python3 tools/verify_atlas.py --report-only   # print, never fail

Exit code 0 = every assertion held. Non-zero = at least one failed, and each
failure is printed with the numbers that produced it.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from block_families import FAMILIES                        # noqa: E402
from build_atlas import (ATLAS_FAMILIES, EXPECTED_MEAN,      # noqa: E402
                         GENERATED_RGB, plan_layout)

#: Expected mean RGB per family, imported from build_atlas.py rather than
#: copied. A second literal here would be a second thing to forget to update,
#: and a stale copy asserts nothing useful -- it would fail on a correct
#: build and pass on a wrong one, depending on which way it drifted.
TARGETS = dict(EXPECTED_MEAN)

#: Families that must read mineral-neutral: abs(R-B) within this. `sports` is
#: deliberately NOT here -- it is a grass pitch, so it is judged by the
#: G >= R rule below like `grass` and `leaves`, and the spec's own sports
#: target of (96, 122, 74) is R-B +22 by construction.
MINERAL = ("concrete", "granite", "tiles", "gravel", "quartz", "plaster",
           "asphalt", "metal", "rock", "greystone", "path")

#: Per-channel tolerance against TARGETS, per spec section 6 step 1.
TOL = 6

#: Atlas aggregate R-B ceiling, per spec section 6 step 1.
AGGREGATE_RB_MAX = 10.0


def cell_mean(im, box):
    """Mean RGB of an Image crop, as floats. No numpy needed for 504x504."""
    x0, y0, x1, y1 = box
    crop = im.crop((x0, y0, x1, y1))
    r, g, b = crop.split()
    n = crop.size[0] * crop.size[1]
    return (sum(r.getdata()) / float(n),
            sum(g.getdata()) / float(n),
            sum(b.getdata()) / float(n))


def saturation(mean):
    """HSV saturation of a mean colour, 0..1. Matches the spec's definition:
    (max-min)/max, so a flat cell is 0 and pure black is 0 too."""
    mx, mn = max(mean), min(mean)
    return 0.0 if mx <= 0 else (mx - mn) / mx


def fmt(mean):
    return "(%5.1f, %5.1f, %5.1f)" % mean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--png", default="out/atlas/atlas_diffuse.png")
    ap.add_argument("--ue-png", default="project/Content/MC/Atlas/atlas_diffuse.png")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--json", default=None, help="write the measurements here")
    args = ap.parse_args()

    from PIL import Image

    layout = plan_layout(ATLAS_FAMILIES)
    meta = layout["_atlas"]
    W, H, T = meta["width"], meta["height"], meta["tile"]

    if not os.path.isfile(args.png):
        print("FATAL: %s not found" % args.png)
        return 2
    im = Image.open(args.png)
    if im.size != (W, H):
        print("FATAL: %s is %dx%d but plan_layout() says %dx%d"
              % (args.png, im.size[0], im.size[1], W, H))
        print("       the PNG and the layout disagree -- every UV rect is suspect")
        return 2
    im = im.convert("RGB")

    failures = []
    rows = []
    print("atlas %s  %dx%d  tile %d pad %d  %d cols x %d rows"
          % (args.png, W, H, T, meta["pad"], meta["cols"], meta["rows"]))
    print("cell layout is imported from plan_layout(); not re-derived here.")
    print()
    print("  cell     family      measured            R-B   sat%   "
          "target              verdict")
    print("  " + "-" * 78)

    occupied = []
    for fam in ATLAS_FAMILIES:
        cell = layout[fam]
        box = cell["px"]
        mean = cell_mean(im, box)
        occupied.append(mean)
        rb = mean[0] - mean[2]
        sat = saturation(mean) * 100.0
        tgt = TARGETS.get(fam)

        col, row = cell["col"], cell["row"]
        problems = []

        if tgt is not None:
            if fam in GENERATED_RGB:
                # Generated cells are pasted as a flat colour; assert exactly.
                want = GENERATED_RGB[fam]
                for ch in range(3):
                    if abs(mean[ch] - want[ch]) > 2:
                        problems.append("ch%d %.1f != generated %d"
                                        % (ch, mean[ch], want[ch]))
            else:
                for ch in range(3):
                    if abs(mean[ch] - tgt[ch]) > TOL:
                        problems.append("ch%d %.1f vs target %d (d=%+.1f, tol %d)"
                                        % (ch, mean[ch], tgt[ch],
                                           mean[ch] - tgt[ch], TOL))

        if fam in MINERAL and abs(rb) > 12:
            problems.append("mineral R-B %+.1f outside +/-12" % rb)
        if fam in ("grass", "leaves", "sports") and mean[1] < mean[0]:
            problems.append("G %.1f < R %.1f, hue inversion" % (mean[1], mean[0]))
        if fam == "brick" and not mean[0] > mean[1]:
            problems.append("R %.1f not > G %.1f, brick stopped being brick"
                            % (mean[0], mean[1]))

        verdict = "ok" if not problems else "FAIL"
        print("  r%dc%d   %-9s  %s  %+6.1f %5.1f   %-18s %s"
              % (row, col, fam, fmt(mean), rb, sat,
                 fmt(tgt) if tgt else "-", verdict))
        for p in problems:
            print("             !! %s" % p)
            failures.append((fam, p))
        rows.append({"family": fam, "col": col, "row": row, "px": box,
                     "mean": [round(v, 2) for v in mean],
                     "rb": round(rb, 2), "sat_pct": round(sat, 1),
                     "target": list(tgt) if tgt else None,
                     "problems": problems})

    # Free cells: r2c6 / r2c7 must still be empty. A family that lands in the
    # wrong cell is the UV v-flip failure mode with no error message, so this
    # is asserted rather than assumed.
    ncols, nrows = meta["cols"], meta["rows"]
    used = set((layout[f]["col"], layout[f]["row"]) for f in ATLAS_FAMILIES)
    empties = [(c, r) for r in range(nrows) for c in range(ncols)
               if (c, r) not in used]
    print()
    print("free cells (allocated, must stay flat 0,0,0):")
    for col, row in empties:
        x0, y0 = col * meta["cell"] + meta["pad"], \
                 (nrows - 1 - row) * meta["cell"] + meta["pad"]
        m = cell_mean(im, (x0, y0, x0 + T, y0 + T))
        flat = max(m) < 1.0
        print("  r%dc%d  %s  %s" % (row, col, fmt(m), "empty" if flat else "OCCUPIED"))
        if not flat:
            failures.append(("r%dc%d" % (row, col),
                             "free cell is not empty: %s" % fmt(m)))

    # Aggregate over the occupied cells only. Including the empty cells would
    # drag the mean toward black and flatter the result.
    n = len(occupied)
    agg = tuple(sum(m[i] for m in occupied) / n for i in range(3))
    agg_rb = agg[0] - agg[2]
    print()
    print("aggregate over %d occupied cells: %s  R-B = %+.1f  sat %.1f%%"
          % (n, fmt(agg), agg_rb, saturation(agg) * 100))
    print("  pre-bake baseline, measured the same way over the same 21 cells, "
          "was (115.7, 102.3, 83.9) R-B = +31.8; ceiling is %+.0f"
          % AGGREGATE_RB_MAX)
    if agg_rb > AGGREGATE_RB_MAX:
        failures.append(("<aggregate>", "R-B %+.1f > %+.0f" % (agg_rb,
                                                             AGGREGATE_RB_MAX)))

    # The UE copy is the one the editor samples. A correct out/ PNG with a
    # stale Content copy is the documented silent failure, so compare bytes.
    if args.ue_png and os.path.isfile(args.ue_png):
        import hashlib
        def sha(p):
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        a, b = sha(args.png), sha(args.ue_png)
        same = a == b
        print()
        print("UE copy %s" % args.ue_png)
        print("  sha256 out/atlas   %s" % a[:16])
        print("  sha256 UE Content  %s  %s" % (b[:16],
                                               "IDENTICAL" if same else "DIFFERENT"))
        if not same:
            failures.append(("<ue-copy>", "UE Content copy is not the file that "
                                         "was just verified"))
    elif args.ue_png:
        print()
        print("UE copy %s MISSING" % args.ue_png)
        failures.append(("<ue-copy>", "missing %s" % args.ue_png))

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"png": args.png, "atlas": [W, H],
                       "aggregate": [round(v, 2) for v in agg],
                       "aggregate_rb": round(agg_rb, 2),
                       "families": rows,
                       "failures": [{"family": f, "problem": p}
                                    for f, p in failures]}, fh, indent=2)
        print("report: %s" % args.json)

    print()
    if failures:
        print("FAILED: %d assertion(s)" % len(failures))
        for f, p in failures:
            print("  %s: %s" % (f, p))
        return 0 if args.report_only else 1
    print("PASS: every cell asserted against pixels, not against intent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
