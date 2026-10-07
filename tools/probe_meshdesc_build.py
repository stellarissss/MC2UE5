# -*- coding: utf-8 -*-
"""
probe_meshdesc_build.py -- build the terrain the supported way instead of via OBJ.

Why this exists. The Interchange OBJ translator drops almost every polygon from a
large height-field: 8x8 (98 faces) imports whole, 64x64 keeps 12.3%, 128x128 keeps
0.8%, while a 10,924-face voxel shell imports 100%. Rewriting the normals and the
line endings changed nothing, the geometry is provably valid, and the importer logs
no warning -- the loss is inside UInterchangeOBJTranslator::MakeMeshDescriptionForGroup,
which is engine code. Writing quads instead of triangles is worse, not better: it
builds for minutes and ends with "Input has 0 triangles".

So stop using the importer. `unreal.StaticMesh` this build exposes

    create_static_mesh_description() -> StaticMeshDescription
    build_from_static_mesh_descriptions(...)
    get_static_mesh_description()
    add_material() / set_material()

and `unreal.StaticMeshDescription` exposes create_vertex / create_vertex_instance /
create_triangle / create_polygon_group / set_vertex_position /
set_vertex_instance_uv / set_polygon_group_material_slot_name. That is everything a
height field needs, it is deterministic, it assigns material slots exactly, and it
never touches an OBJ file.

This probe answers the only two questions that decide it: what are the real call
signatures, and does every triangle survive at the real 128x128 tile size.

Run:

    UnrealEditor-Cmd.exe MCReplica.uproject -ExecutePythonScript=<this> ^
        -nullrhi -unattended -nopause -nosplash -stdout
"""

import time
import traceback

import unreal

OUT = "Q:/MC2UE5/logs/probe_meshdesc_build.txt"
TEST_DIR = "/Game/MC/_Test"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCMD] " + str(s))
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def signatures():
    say("=== call signatures ===")
    for cls_name, meths in (
            ("StaticMesh", ("create_static_mesh_description",
                            "build_from_static_mesh_descriptions",
                            "get_static_mesh_description",
                            "add_material", "set_material", "get_num_triangles")),
            ("StaticMeshDescription", ("create_polygon_group", "create_vertex",
                                       "create_vertex_instance", "create_triangle",
                                       "set_vertex_position",
                                       "set_vertex_instance_uv",
                                       "set_polygon_group_material_slot_name",
                                       "reserve_new_vertices",
                                       "reserve_new_vertex_instances",
                                       "reserve_new_triangles",
                                       "reserve_new_polygons"))):
        say("  --- %s ---" % cls_name)
        cls = getattr(unreal, cls_name, None)
        for m in meths:
            f = getattr(cls, m, None) if cls else None
            if f is None:
                say("    %-38s -- absent" % m)
                continue
            doc = (getattr(f, "__doc__", "") or "").strip().splitlines()
            head = doc[0].strip() if doc else "(no doc)"
            say("    %-38s %s" % (m, head[:150]))


def make_grid(smd, pg, n, step=100.0, z_fn=None):
    """Fill a StaticMeshDescription with an n x n grid. -> triangles created.

    create_triangle takes the polygon group plus a *list* of vertex instance ids,
    not three separate arguments -- discovered by calling it, not by recalling it.
    UVs go on the vertex instances, one per instance, via
    set_vertex_instance_uv(instance, uv, uv_index).
    """
    vids = []
    for i in range(n):
        row = []
        for j in range(n):
            v = smd.create_vertex()
            z = 0.0 if z_fn is None else z_fn(i, j)
            smd.set_vertex_position(v, unreal.Vector(i * step, j * step, z))
            row.append(v)
        vids.append(row)

    tris = 0
    for i in range(n - 1):
        for j in range(n - 1):
            # Vertex instances carry their own UV, so each grid corner gets its
            # own instance rather than sharing one per vertex.
            def inst(v, u, w):
                e = smd.create_vertex_instance(v)
                smd.set_vertex_instance_uv(e, unreal.Vector2D(u, w), 0)
                return e

            a, b, c, d = (vids[i][j], vids[i + 1][j],
                          vids[i + 1][j + 1], vids[i][j + 1])
            ia = inst(a, float(i), float(j))
            ib = inst(b, float(i + 1), float(j))
            ic = inst(c, float(i + 1), float(j + 1))
            id_ = inst(d, float(i), float(j + 1))
            smd.create_triangle(pg, [ia, ib, ic])
            smd.create_triangle(pg, [ia, ic, id_])
            tris += 2
    return tris


def build_case(label, n, z_fn=None):
    """Create an asset, fill it from a MeshDescription, report what landed."""
    t0 = time.time()
    tools = unreal.AssetToolsHelpers.get_asset_tools()
    if not unreal.EditorAssetLibrary.does_directory_exist(TEST_DIR):
        unreal.EditorAssetLibrary.make_directory(TEST_DIR)

    asset = tools.create_asset(label, TEST_DIR, unreal.StaticMesh, None)
    if asset is None:
        return None, "create_asset(..., None) returned None"
    try:
        smd = asset.create_static_mesh_description()
    except Exception as exc:
        return None, "create_static_mesh_description raised %s" % str(exc)[:120]
    if smd is None:
        return None, "create_static_mesh_description returned None"

    try:
        pg = smd.create_polygon_group()
        smd.set_polygon_group_material_slot_name(pg, "grout")
        want = make_grid(smd, pg, n, z_fn=z_fn)
    except Exception:
        return None, "filling raised:\n%s" % traceback.format_exc()[-400:]

    try:
        asset.build_from_static_mesh_descriptions([smd])
    except Exception:
        return None, "build raised:\n%s" % traceback.format_exc()[-400:]

    try:
        got = asset.get_num_triangles(0)
        verts = asset.get_num_vertices(0)
        secs = asset.get_num_sections(0)
    except Exception as exc:
        return want, "stats raised %s" % str(exc)[:120]
    dt = time.time() - t0
    pct = 100.0 * got / want if want else 0.0
    say("  %-16s n=%-4d want=%-7d got=%-7s verts=%-7s sections=%-3s %6.1f%%  %.1fs"
        % (label, n, want, got, verts, secs, pct, dt))
    return want, {"got": got, "verts": verts, "sections": secs,
                  "pct": pct, "dt": dt}


def main():
    ok = True
    try:
        say("=== probe_meshdesc_build ===")
        signatures()
        say("")
        say("=== builds ===")
        flat8, d8 = build_case("MD_flat8", 8)
        if isinstance(d8, str):
            say("    flat8 detail: %s" % d8)
        flat128, d128 = build_case("MD_flat128", 128)
        if isinstance(d128, str):
            say("    flat128 detail: %s" % d128)

        say("")
        say("=== verdict ===")
        say("  OBJ importer, same 128x128 flat grid: 252 / 32258 = 0.8%")
        if isinstance(d128, dict):
            say("  MeshDescription route, 128x128:      %s / %d = %.1f%%"
                % (d128["got"], flat128, d128["pct"]))
            if d128["pct"] > 99.0:
                say("  => WORKS. Rebuild the terrain through StaticMeshDescription;")
                say("     no OBJ file, no importer, exact material slots.")
            else:
                say("  => the MeshDescription route loses faces too, so the loss is")
                say("     downstream of the file format entirely.")
        else:
            say("  MeshDescription route did not complete: %s" % d128)
            ok = False
    except Exception:
        ok = False
        say("FAILED:\n" + traceback.format_exc())

    try:
        if unreal.EditorAssetLibrary.does_directory_exist(TEST_DIR):
            for a in unreal.EditorAssetLibrary.list_assets(TEST_DIR):
                unreal.EditorAssetLibrary.delete_asset(a)
            unreal.EditorAssetLibrary.delete_directory(TEST_DIR)
            say("")
            say("cleaned %s" % TEST_DIR)
    except Exception as exc:
        say("cleanup: %s" % str(exc)[:100])

    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")


if __name__ == "__main__":
    main()
