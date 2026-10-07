# -*- coding: utf-8 -*-
"""
probe_obj_import_compare.py -- decide why the terrain tiles lost 90%+ of their
triangles, by importing two variants into FRESH asset paths in ONE editor run.

The three candidates this separates, which a single-variant test cannot:

  * original OBJ imports with ~all its triangles too
      -> the existing /Game/MC/Terrain/terrain_* assets are simply STALE and were
         never re-imported from the current OBJ. Nothing is wrong with the OBJ.
  * only the positive-zero variant imports fully
      -> negative-zero normals ("vn -0.0000 -0.0000 1.0000") are the cause.
  * both variants lose the same fraction
      -> neither; the importer itself is dropping faces for another reason.

Importing into a fresh path is the whole point: re-importing in place would be
indistinguishable from reading a stale asset, which is exactly the ambiguity
that has been confusing this measurement.

Read-only with respect to the pipeline; the only writes are the two scratch
assets under /Game/MC/_Test, which are deleted at the end.

Run inside the editor (argv is not forwarded, so stages come from the env):

    set MC_PROBE=junk_terrain
    UnrealEditor-Cmd.exe MCReplica.uproject -ExecutePythonScript=<this> ^
        -nullrhi -unattended -nopause -nosplash -stdout
"""

import os
import traceback

import unreal

OUT = "Q:/MC2UE5/logs/probe_obj_import_compare.txt"
REPO = "Q:/MC2UE5/repo"
TEST_DIR = "/Game/MC/_Test"
L = []

#: (label, obj path, expected f-line count)
#:
#: Round 3. Two hypotheses are already dead, each by a direct test rather than
#: by argument: negative-zero normals (a variant with every "-0.0000" rewritten
#: imported identically) and CRLF line endings (an LF-only rewrite imported
#: identically). Both variants ALSO matched the shipped asset exactly, so the
#: shipped asset is neither stale nor damaged -- the importer really does drop
#: the faces. What is left is either a face-count limit or something about the
#: file's content, and these three cases separate them:
#:
#:   synth128    a minimal, independently generated 128x128 grid with the SAME
#:               32,258 triangles. If this imports whole, our file's content is
#:               at fault; if it also collapses, the grid itself is.
#:   trunc1000   the real tile with the head intact and only the first 1,000
#:   trunc16000  faces kept. If these keep ~all their faces, the loss is a
#:               threshold near the full count; if they keep ~0.8% again, the
#:               loss is proportional and no limit is involved.
CASES = (
    ("junk_control_struct", os.path.join(REPO, "out/structures/bld_028_structure.obj"), 10924),
    ("junk_flat8", os.path.join(REPO, "out/_t_flat8.obj"), 98),
    ("junk_flat64", os.path.join(REPO, "out/_t_flat64.obj"), 7938),
    ("junk_slope128", os.path.join(REPO, "out/_t_slope128.obj"), 32258),
)

#: Cases whose expected behaviour we report on, and the reference points the
#: verdict compares against.
_REFERENCE = (
    ("control: bld_028_structure (known 100%)", 10924, 10924),
)


def say(s):
    L.append(str(s))
    unreal.log("[MCPROBE] " + str(s))
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def obj_faces(path):
    """Face count straight from the file -- the number the importer must match."""
    n = 0
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("f "):
                    n += 1
    except OSError:
        return -1
    return n


def negative_zeros(path):
    """Count of `vn` lines carrying a negative zero -- round 1's hypothesis."""
    n = 0
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith("vn ") and "-0.0000" in line:
                    n += 1
    except OSError:
        return -1
    return n


def cr_count(path):
    """Carriage returns in the file -- the line-ending hypothesis.

    Read as bytes: opening in text mode with universal newlines would silently
    hide exactly the thing being measured.
    """
    try:
        with open(path, "rb") as fh:
            return fh.read().count(b"\r")
    except OSError:
        return -1


def mesh_stats(mesh):
    """(triangles, vertices, sections, lods) on the mesh, lods 0."""
    out = {}
    for key, fn in (("triangles", "get_num_triangles"),
                    ("vertices", "get_num_vertices"),
                    ("sections", "get_num_sections"),
                    ("lods", "get_num_lods")):
        f = getattr(mesh, fn, None)
        if f is None:
            out[key] = "NO_API(%s)" % fn
            continue
        try:
            out[key] = f(0)
        except Exception as exc:
            out[key] = "ERR:%s" % str(exc)[:40]
    return out


def main():
    ok = True
    try:
        say("=== probe_obj_import_compare ===")
        if not unreal.EditorAssetLibrary.does_directory_exist(TEST_DIR):
            unreal.EditorAssetLibrary.make_directory(TEST_DIR)

        results = []
        for label, src, expected_f in CASES:
            say("")
            say("--- %s ---" % label)
            src = src.replace("\\", "/")
            if not os.path.isfile(src):
                say("  *** MISSING SOURCE: %s" % src)
                ok = False
                continue
            f_lines = obj_faces(src)
            negz = negative_zeros(src)
            crs = cr_count(src)
            say("  source: %s" % src)
            say("  f lines: %d (expected %d)   CR bytes: %d   vn with -0.0000: %d"
                % (f_lines, expected_f, crs, negz))

            task = unreal.AssetImportTask()
            task.set_editor_property("filename", src)
            task.set_editor_property("destination_path", TEST_DIR)
            task.set_editor_property("destination_name", label)
            task.set_editor_property("automated", True)
            task.set_editor_property("replace_existing", True)
            task.set_editor_property("save", False)
            unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

            mesh = unreal.load_asset("%s/%s" % (TEST_DIR, label))
            if mesh is None:
                say("  *** IMPORT PRODUCED NOTHING ***")
                ok = False
                continue
            st = mesh_stats(mesh)
            tri = st["triangles"]
            pct = (100.0 * tri / f_lines) if isinstance(tri, int) and f_lines else 0.0
            say("  imported: %s" % st)
            say("  -> kept %s / %d faces = %.1f%%" % (tri, f_lines, pct))
            results.append((label, f_lines, negz, tri, pct,
                            st["vertices"], st["sections"]))

        # Also re-read the EXISTING shipped asset, for a same-run comparison.
        say("")
        say("--- existing shipped asset (same build, same run) ---")
        for name in ("terrain_-016_-032", "terrain_-144_-544"):
            m = unreal.load_asset("/Game/MC/Terrain/%s" % name)
            if m is None:
                say("  %-22s *** MISSING ***" % name)
                continue
            st = mesh_stats(m)
            say("  %-22s %s" % (name, st))

        # ---- verdict ----------------------------------------------------
        say("")
        say("=== verdict ===")
        for label, exp, kept in _REFERENCE:
            say("  reference: %-40s %d / %d" % (label, kept, exp))
        for r in results:
            say("  %-20s kept %6s / %6d  %6.1f%%   (verts %s, sections %s)"
                % (r[0], r[3], r[1], r[4], r[5], r[6]))
        say("")
        ctl = d.get("junk_control_struct")
        if ctl and isinstance(ctl[3], int):
            say("  control  %s: %s / %d = %.1f%%"
                % (ctl[0], ctl[3], ctl[1], ctl[4]))
            if ctl[4] > 99.0:
                say("  => harness is sound: a known-good OBJ imports whole here.")
            else:
                say("  => WARNING: the control did not import whole; the "
                    "harness itself is suspect and nothing below can be trusted.")
        f8 = d.get("junk_flat8")
        f64 = d.get("junk_flat64")
        sl = d.get("junk_slope128")
        if f8 and isinstance(f8[3], int):
            say("  flat 8x8   (98 faces)   -> %s  (%.1f%%)" % (f8[3], f8[4]))
        if f64 and isinstance(f64[3], int):
            say("  flat 64x64 (7938 faces) -> %s  (%.1f%%)" % (f64[3], f64[4]))
        if sl and isinstance(sl[3], int):
            say("  slope 128x128 (32258)   -> %s  (%.1f%%)" % (sl[3], sl[4]))
        say("")
        if not (f8 and f64 and sl):
            say("  (incomplete batch)")
        elif all(isinstance(x[3], int) for x in (f8, f64, sl)):
            if f8[4] > 99.0 and f64[4] > 99.0:
                say("  => flatness is NOT the trigger: both flat grids import "
                    "whole, so the collapse needs a larger grid.")
            elif f8[4] < 99.0:
                say("  => a tiny 8x8 FLAT grid already collapses, so the trigger "
                    "is the flat/coplanar layout itself, not the size.")
            else:
                say("  => size-dependent: 8x8 flat is fine, 64x64 flat is not.")
            if sl[4] > 99.0:
                say("  => a NON-flat 128x128 grid of the same size imports whole "
                    "=> COPLANARITY is the trigger.")
            else:
                say("  => the non-flat 128x128 grid also collapses, so "
                    "coplanarity alone is not enough to explain it.")
    except Exception:
        ok = False
        say("FAILED:\n" + traceback.format_exc())

    # Clean up the scratch assets so they cannot ship or be committed.
    say("")
    try:
        for label, _src, _e in CASES:
            p = "%s/%s" % (TEST_DIR, label)
            if unreal.EditorAssetLibrary.does_asset_exist(p):
                unreal.EditorAssetLibrary.delete_asset(p)
                say("deleted %s" % p)
        if unreal.EditorAssetLibrary.does_directory_exist(TEST_DIR):
            unreal.EditorAssetLibrary.delete_directory(TEST_DIR)
            say("deleted %s" % TEST_DIR)
    except Exception as exc:
        say("cleanup problem: %s" % str(exc)[:120])

    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")


if __name__ == "__main__":
    main()
