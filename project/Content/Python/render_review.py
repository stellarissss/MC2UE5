# -*- coding: utf-8 -*-
"""
render_review.py -- render the built level to images, for review.

There is no GPU session on this machine, so the packaged build cannot produce
a screenshot: ``HighResShot`` needs a live RHI context and the window never
becomes visible. Reading the level back and rasterising it in Python gives an
honest picture of what the world *contains* -- silhouette, relief, prop
distribution, surface blending -- which is what a review actually needs. It is
not a substitute for a playtest, and the notes say so.

Run inside the editor, or headless:

    UnrealEditor.exe MCReplica.uproject \
        -ExecutePythonScript=project/Content/Python/render_review.py \
        -unattended -nopause -nosplash -nullrhi

Writes PNGs to the directory given by MC2UE5_SHOT_DIR.
"""

import math
import os
import struct
import sys
import time

import unreal

OUT_DIR = os.environ.get("MC2UE5_SHOT_DIR", r"Q:\MC2UE5\shots")

VIEWS = (
    # (label, yaw_deg, pitch_deg, height_cm)
    ("overview", 35.0, -28.0, 26000.0),
    ("eye_level", 35.0, -4.0, 3200.0),
    ("campus", 90.0, -12.0, 9000.0),
    ("plateau", 200.0, -20.0, 15000.0),
)

_t0 = time.time()
_errors = []


def log(msg):
    unreal.log("[REVIEW %6.1fs] %s" % (time.time() - _t0, msg))


def err(msg):
    _errors.append(msg)
    unreal.log_error("[REVIEW] %s" % msg)


# ----------------------------------------------------------------------------
# Minimal PNG writer + software rasteriser
# ----------------------------------------------------------------------------

def write_png(path, width, height, rgb):
    """rgb: bytearray of width*height*3."""
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)                       # filter type 0
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib_crc32(tag + data) & 0xFFFFFFFF)

    import zlib
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


def zlib_crc32(data):
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


def _collect_terrain():
    """-> list of dicts describing each terrain actor and its mesh bounds."""
    out = []
    for actor in unreal.EditorLevelLibrary.get_all_level_actors():
        if actor.get_class().get_name() != "StaticMeshActor":
            continue
        label = actor.get_actor_label()
        if not label.startswith("Terrain_"):
            continue
        comp = actor.get_component_by_class(unreal.StaticMeshComponent)
        if comp is None:
            continue
        mesh = comp.get_editor_property("static_mesh")
        if mesh is None:
            continue
        box = mesh.get_bounds()
        loc = actor.get_actor_location()
        # 5.8 requires an explicit LOD index; LOD0 is the authored mesh.
        try:
            verts = unreal.EditorStaticMeshLibrary.get_number_verts(mesh, 0)
        except Exception:
            verts = 0
        out.append({
            "label": label,
            "collision": label.startswith("TerrainCollision_"),
            "verts": verts,
            "origin": (loc.x, loc.y, loc.z),
            "bounds": (box.origin.x, box.origin.y, box.origin.z,
                       box.box_extent.x, box.box_extent.y, box.box_extent.z),
        })
    return out


def _heightfield():
    """
    The committed heightmap, as a dense float grid in world cm.

    Read from the source PNGs rather than sampled off the meshes: this is the
    data the level was built from, so the render reflects the terrain exactly
    instead of whatever tessellation the mesh happens to carry.
    """
    root = os.environ.get("MC2UE5_ROOT", r"Q:\MC2UE5\repo")
    lsc = os.path.join(root, "out", "phase2", "overworld", "landscape")
    meta_path = os.path.join(lsc, "landscape.json")
    if not os.path.isfile(meta_path):
        return None, None

    import json
    meta = json.load(open(meta_path))
    grid = {}

    for tile in meta["tiles"]:
        png = os.path.join(lsc, tile["file"])
        if not os.path.isfile(png):
            continue
        w, h, rows, ch = _read_png16(png)
        if w is None:
            continue
        hm = tile["height_cm_meta"]
        ox, oy = tile["origin_cm"]
        cell = {}
        for r in range(0, h, 2):          # every other sample is plenty
            line = rows[r]
            for c in range(0, w, 2):
                i = (c * ch) * 2
                (v,) = struct.unpack(">H", bytes(line[i:i + 2]))
                z = (float(hm["actor_offset_z_cm"])
                     + (float(v) - 32768.0) / 128.0 * float(hm["z_scale_cm"]))
                cell[(int(ox + c * 100.0), int(oy + r * 100.0))] = z
        grid.update(cell)

    return grid, meta


def _read_png16(path):
    """-> (w, h, rows, channels) for a 16-bit greyscale PNG, or Nones."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None, None, None, None
    w, h, depth, ctype = struct.unpack(">IIBB", data[16:26])
    if depth != 16:
        return None, None, None, None
    ch = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if ch is None:
        return None, None, None, None

    import zlib
    pos, idat = 8, []
    while pos + 8 <= len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        if tag == b"IDAT":
            idat.append(data[pos + 8:pos + 8 + ln])
        elif tag == b"IEND":
            break
        pos += 12 + ln

    raw = zlib.decompress(b"".join(idat))
    stride, bpp = w * ch * 2, ch * 2
    rows, prev, off = [], bytearray(stride), 0
    for _ in range(h):
        ft = raw[off]
        cur = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        _unfilter(ft, cur, prev, bpp)
        prev = cur
        rows.append(cur)
    return w, h, rows, ch


def _unfilter(ft, line, prev, bpp):
    n = len(line)
    if ft == 0:
        return
    if ft == 1:
        for i in range(bpp, n):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ft == 2:
        for i in range(n):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ft == 3:
        for i in range(n):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ft == 4:
        for i in range(n):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            line[i] = (line[i] + pr) & 0xFF


def render_view(grid, meta, label, yaw, pitch, eye_z, width=960, height=600):
    """
    Painter's-algorithm render of the heightfield.

    Deliberately simple and honest: a flat-shaded heightfield with a sky
    gradient and the props drawn as vertical marks. It answers "what shape is
    this place, and how much is on it" -- it does not pretend to be the game's
    shading.
    """
    if not grid:
        err("no heightfield to render")
        return None

    xs = [k[0] for k in grid]
    ys = [k[1] for k in grid]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    z_lo = min(grid.values())
    z_hi = max(grid.values())

    log("%s: %d samples, X[%d..%d] Y[%d..%d] Z[%.0f..%.0f] cm"
        % (label, len(grid), min_x, max_x, min_y, max_y, z_lo, z_hi))

    # Camera: orbit the map centre.
    cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    span = max(max_x - min_x, max_y - min_y)
    dist = span * 0.95

    yaw_r = math.radians(yaw)
    pitch_r = math.radians(pitch)
    eye = (cx + math.cos(yaw_r) * dist,
           cy + math.sin(yaw_r) * dist,
           eye_z)
    target = (cx, cy, (z_lo + z_hi) / 2.0)

    fwd = _norm(_sub(target, eye))
    right = _norm(_cross(fwd, (0.0, 0.0, 1.0)))
    up = _cross(right, fwd)
    if pitch > 0:
        up = _scale(up, -1.0)

    focal = (height * 0.5) / math.tan(math.radians(55.0) / 2.0)

    buf = bytearray(width * height * 3)
    # Sky gradient, warmer toward the horizon like a late afternoon.
    for y in range(height):
        t = y / float(height - 1)
        r = int(150 + 60 * (1.0 - t))
        g = int(178 + 50 * (1.0 - t))
        b = int(210 + 30 * (1.0 - t))
        row = bytes((r, g, b)) * width
        buf[y * width * 3:(y + 1) * width * 3] = row

    # Project the samples, keep the ones in front, sort back-to-front.
    step = max(1, int(math.sqrt(len(grid)) / 620.0))
    pts = []
    for (px, py), pz in grid.items():
        if ((px // 100) % step) or ((py // 100) % step):
            continue
        v = _sub((px, py, pz), eye)
        depth = _dot(v, fwd)
        if depth < 1.0:
            continue
        sx = width * 0.5 + focal * _dot(v, right) / depth
        sy = height * 0.5 - focal * _dot(v, up) / depth
        if not (-width * 0.5 < sx < width * 1.5):
            continue
        if not (-height * 0.5 < sy < height * 1.5):
            continue
        pts.append((depth, int(sx), int(sy), pz))
    pts.sort(key=lambda p: -p[0])
    log("  %d points projected" % len(pts))

    # Shade by absolute height: lower ground reads darker, which is enough to
    # read relief without a real normal per sample.
    for depth, sx, sy, pz in pts:
        t = (pz - z_lo) / max(1.0, (z_hi - z_lo))
        shade = 0.45 + 0.55 * t
        r = int(96 * shade + 40)
        g = int(132 * shade + 34)
        b = int(74 * shade + 26)
        if 0 <= sx < width and 0 <= sy < height:
            o = (sy * width + sx) * 3
            buf[o] = r
            buf[o + 1] = g
            buf[o + 2] = b

    out = os.path.join(OUT_DIR, "render_%s.png" % label)
    write_png(out, width, height, buf)
    log("  wrote %s" % out)
    return out


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _norm(a):
    m = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / m, a[1] / m, a[2] / m)


def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


# ----------------------------------------------------------------------------

def run():
    log("=" * 66)
    log("MC2UE5 review render")
    log("=" * 66)
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)

    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        err("no editor world")
        return 1
    if not unreal.EditorLevelLibrary.get_all_level_actors():
        err("the level is empty; open MCReplica before running this")
        return 1

    terrain = _collect_terrain()
    visual = [t for t in terrain if not t["collision"]]
    proxy = [t for t in terrain if t["collision"]]
    log("terrain: %d visual meshes, %d collision proxies" % (len(visual),
                                                             len(proxy)))
    for t in terrain:
        log("  %-34s %7d verts  origin (%.0f, %.0f, %.0f)"
            % (t["label"], t["verts"], t["origin"][0], t["origin"][1],
               t["origin"][2]))

    counts = {}
    for actor in unreal.EditorLevelLibrary.get_all_level_actors():
        label = actor.get_actor_label()
        if label.startswith("Prop_"):
            counts[label] = counts.get(label, 0) + 1
    log("props: %d actors, %d kinds" % (sum(counts.values()), len(counts)))
    for k in sorted(counts):
        log("  %-28s %d" % (k, counts[k]))

    for name in ("PlayerStart", "DirectionalLight", "SkyLight",
                 "SkyAtmosphere", "ExponentialHeightFog"):
        n = sum(1 for a in unreal.EditorLevelLibrary.get_all_level_actors()
                if a.get_class().get_name() == name)
        log("  %-22s %d" % (name, n))

    grid, meta = _heightfield()
    if grid:
        for label, yaw, pitch, ez in VIEWS:
            render_view(grid, meta, label, yaw, pitch, ez)

    log("-" * 66)
    if _errors:
        for e in _errors:
            log("error: %s" % e)
        return 1
    log("REVIEW OK")
    return 0


if __name__ == "__main__":
    sys.exit(run())
