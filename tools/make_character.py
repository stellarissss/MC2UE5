# -*- coding: utf-8 -*-
"""
make_character.py -- build the player figure: meshes and skin texture.

The figure is Minecraft-proportioned and dressed as a Chinese school student,
generated rather than imported because the project has no character assets and
the source data is voxel. A blocky figure is also exactly what a rig of boxes
reproduces faithfully, so nothing is approximated.

Dimensions, in Minecraft blocks where one block is 100 cm:

    total height   2.00   head 0.50, torso 0.75, legs 0.75
    width          0.50   torso and head
    limb section   0.25   arms and legs
    shoulder span  1.00   arm centres at +/-0.375

Each limb is written as its own OBJ with the geometry modelled *below its
pivot*, because the character rotates each limb about its joint; if the mesh
were authored around its centre the limb would orbit instead of swinging.

The skin is a single 64x64 atlas in the Minecraft layout, painted with a
school uniform. One texture keeps the draw call count at one for the whole
figure and makes the UV mapping match what Minecraft players already expect.

    python3 make_character.py --out <dir>
"""

import argparse
import math
import os
import struct
import sys
import zlib

BLOCK_CM = 100.0

# ---------------------------------------------------------------------------
# Minecraft skin atlas geometry
# ---------------------------------------------------------------------------
# Regions of a 64x64 skin, in pixels. Taken from the vanilla layout so the
# figure reads as Minecraft rather than as a generic blocky human.
SKIN = 64
HEAD_FRONT = (8, 8, 8, 8)
HEAD_BACK = (0, 8, 8, 8)
HEAD_TOP = (8, 0, 8, 8)
HEAD_BOTTOM = (16, 0, 8, 8)
HEAD_RIGHT = (0, 0, 8, 8)
HEAD_LEFT = (16, 0, 8, 8)
BODY_FRONT = (20, 20, 8, 12)
BODY_BACK = (32, 20, 8, 12)
BODY_RIGHT = (16, 20, 4, 12)
BODY_LEFT = (28, 20, 4, 12)
BODY_TOP = (20, 16, 8, 4)
BODY_BOTTOM = (28, 16, 8, 4)

# Arms and legs occupy *different* rectangles of the atlas -- the right arm
# lives at (40,16) and the right leg at (0,16). Sharing one set of regions
# between them paints the trousers onto the sleeve and vice versa, which reads
# as a figure with its uniform in the wrong place.
ARM_FRONT = (44, 20, 4, 12)
ARM_BACK = (48, 20, 4, 12)
ARM_RIGHT = (40, 20, 4, 12)
ARM_LEFT = (52, 20, 4, 12)
ARM_TOP = (44, 16, 4, 4)
ARM_BOTTOM = (48, 16, 4, 4)

LEG_FRONT = (4, 20, 4, 12)
LEG_BACK = (8, 20, 4, 12)
LEG_RIGHT = (0, 20, 4, 12)
LEG_LEFT = (12, 20, 4, 12)
LEG_TOP = (4, 16, 4, 4)
LEG_BOTTOM = (8, 16, 4, 4)

# ---------------------------------------------------------------------------
# School uniform palette
# ---------------------------------------------------------------------------
# A Chinese secondary-school uniform: white shirt, navy collar and a dark
# pleated skirt or trousers, with the red scarf of the Young Pioneers. The
# scarf is the detail that makes the silhouette read as a school uniform
# rather than as a generic white-shirt figure.
PALETTE = {
    "skin":       (0xE8, 0xB9, 0x8E),
    "skin_shade": (0xD0, 0xA0, 0x78),
    "hair":       (0x2B, 0x22, 0x1E),
    "shirt":      (0xF2, 0xF3, 0xF5),
    "shirt_shade": (0xD8, 0xDA, 0xDF),
    "collar":     (0x1E, 0x2A, 0x4A),
    "navy":       (0x23, 0x2E, 0x52),
    "navy_shade": (0x18, 0x20, 0x3C),
    "scarf":      (0xC8, 0x22, 0x28),
    "scarf_shade": (0x9A, 0x18, 0x1E),
    "shoe":       (0x1C, 0x1C, 0x20),
    "tie":        (0x8A, 0x1F, 0x24),
    "eye":        (0x2A, 0x2E, 0x3A),
    "mouth":      (0x6B, 0x3A, 0x34),
    "hair_over":  (0x3A, 0x2E, 0x28),
}


def fill_rect(px, x, y, w, h, colour):
    for yy in range(y, y + h):
        if not 0 <= yy < SKIN:
            continue
        row = yy * SKIN * 4
        for xx in range(x, x + w):
            if not 0 <= xx < SKIN:
                continue
            i = row + xx * 4
            px[i] = colour[0]
            px[i + 1] = colour[1]
            px[i + 2] = colour[2]
            px[i + 3] = 255


def blit(px, region, colour):
    fill_rect(px, region[0], region[1], region[2], region[3], colour)


def build_skin():
    """-> bytes of a 64x64 RGBA skin in the vanilla Minecraft layout."""
    px = bytearray(SKIN * SKIN * 4)
    for i in range(0, len(px), 4):
        px[i] = px[i + 1] = px[i + 2] = 0
        px[i + 3] = 255

    # ---- head ------------------------------------------------------------
    # Hair wraps the back, top and sides; the face is left clear.
    blit(px, HEAD_TOP, PALETTE["hair"])
    blit(px, HEAD_BACK, PALETTE["hair"])
    blit(px, HEAD_RIGHT, PALETTE["skin_shade"])
    blit(px, HEAD_LEFT, PALETTE["skin_shade"])
    blit(px, HEAD_BOTTOM, PALETTE["skin_shade"])
    blit(px, HEAD_FRONT, PALETTE["skin"])

    # Fringe across the top of the face, and sideburns.
    fill_rect(px, HEAD_FRONT[0], HEAD_FRONT[1], 8, 2, PALETTE["hair"])
    fill_rect(px, HEAD_FRONT[0], HEAD_FRONT[1], 1, 8, PALETTE["hair"])
    fill_rect(px, HEAD_FRONT[0] + 7, HEAD_FRONT[1], 1, 8, PALETTE["hair"])

    # Eyes: 2x1 pixels each, with a white highlight beside them.
    for ex in (1, 5):
        fill_rect(px, HEAD_FRONT[0] + ex, HEAD_FRONT[1] + 4, 2, 1,
                  PALETTE["eye"])
    # Mouth.
    fill_rect(px, HEAD_FRONT[0] + 2, HEAD_FRONT[1] + 6, 4, 1,
              PALETTE["mouth"])

    # ---- torso -----------------------------------------------------------
    # White shirt body with a navy collar band at the top.
    blit(px, BODY_FRONT, PALETTE["shirt"])
    blit(px, BODY_BACK, PALETTE["shirt"])
    blit(px, BODY_RIGHT, PALETTE["shirt_shade"])
    blit(px, BODY_LEFT, PALETTE["shirt_shade"])
    blit(px, BODY_TOP, PALETTE["collar"])
    blit(px, BODY_BOTTOM, PALETTE["shirt_shade"])

    # Collar: a navy V on the front, and the tie hanging from it.
    fx, fy, fw, fh = BODY_FRONT
    fill_rect(px, fx, fy, fw, 2, PALETTE["collar"])
    fill_rect(px, fx + 3, fy, 2, 1, PALETTE["shirt"])
    fill_rect(px, fx + 3, fy + 1, 2, 3, PALETTE["tie"])
    # Red scarf crossing the collar, worn over the left shoulder.
    fill_rect(px, fx, fy + 2, 8, 1, PALETTE["scarf"])
    fill_rect(px, fx + 6, fy + 3, 2, 3, PALETTE["scarf_shade"])

    # A breast pocket, so the shirt is not a blank rectangle.
    fill_rect(px, fx + 1, fy + 6, 2, 2, PALETTE["shirt_shade"])

    # ---- arms ------------------------------------------------------------
    # Short-sleeve uniform shirt: white for the upper half, skin below.
    for right in (ARM_FRONT, ARM_BACK, ARM_RIGHT, ARM_LEFT):
        blit(px, right, PALETTE["skin"])
        x, y, w, h = right
        fill_rect(px, x, y, w, 5, PALETTE["shirt"])
        fill_rect(px, x, y, w, 1, PALETTE["shirt_shade"])
    blit(px, ARM_TOP, PALETTE["shirt"])
    blit(px, ARM_BOTTOM, PALETTE["skin_shade"])

    # ---- legs ------------------------------------------------------------
    # Navy trousers to the ankle, then a black shoe.
    for part in (LEG_FRONT, LEG_BACK, LEG_RIGHT, LEG_LEFT):
        blit(px, part, PALETTE["navy"])
        x, y, w, h = part
        fill_rect(px, x, y, w, 3, PALETTE["shoe"])
        fill_rect(px, x, y + 9, w, 3, PALETTE["navy_shade"])
    blit(px, LEG_TOP, PALETTE["navy"])
    blit(px, LEG_BOTTOM, PALETTE["shoe"])

    return bytes(px)


def write_png(path, width, height, rgba):
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


# ---------------------------------------------------------------------------
# Box mesh with per-face UV regions
# ---------------------------------------------------------------------------

#: Face order used throughout: +X -X +Y -Y +Z -Z
FACE_DIRS = (
    ((1, 0, 0), (-1, 0, 0)),    # +X (right),  -X (left)
    ((0, 1, 0), (0, -1, 0)),    # +Y (back),   -Y (front)  -- MC: +Z is south
    ((0, 0, 1), (0, 0, -1)),    # +Z (up),     -Z (down)
)


def box_faces(sx, sy, sz, origin):
    """
    -> [(normal, [4 corners])] for an axis-aligned box.

    ``origin`` is the box centre. Faces are wound counter-clockwise seen from
    outside, which matters: a reversed winding makes the figure render inside
    out under any culling mode.
    """
    hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
    ox, oy, oz = origin
    c = [
        (ox - hx, oy - hy, oz - hz),   # 0
        (ox + hx, oy - hy, oz - hz),   # 1
        (ox + hx, oy + hy, oz - hz),   # 2
        (ox - hx, oy + hy, oz - hz),   # 3
        (ox - hx, oy - hy, oz + hz),   # 4
        (ox + hx, oy - hy, oz + hz),   # 5
        (ox + hx, oy + hy, oz + hz),   # 6
        (ox - hx, oy + hy, oz + hz),   # 7
    ]
    quads = (
        ((1, 0, 0), (1, 2, 6, 5)),    # +X
        ((-1, 0, 0), (4, 7, 3, 0)),   # -X
        ((0, 1, 0), (0, 4, 5, 1)),    # -Y
        ((0, -1, 0), (3, 2, 6, 7)),   # +Y
        ((0, 0, 1), (4, 0, 1, 5)),    # -Z
        ((0, 0, -1), (7, 6, 2, 3)),   # +Z
    )
    return [(n, [c[i] for i in q]) for n, q in quads]


def write_obj(path, sx, sy, sz, origin, regions):
    """
    Write one box as an OBJ.

    ``regions`` maps a face index to its skin rectangle, so each face samples
    the part of the atlas it is meant to. Faces default to the front region,
    which is the right answer for a limb seen from any side closely enough.
    """
    faces = box_faces(sx, sy, sz, origin)
    verts = []
    uvs = []
    tris = []

    for idx, (_n, corners) in enumerate(faces):
        rx, ry, rw, rh = regions.get(idx, regions[0])
        base = len(verts)
        for k, corner in enumerate(corners):
            verts.append(corner)
            # Face-local UV: (0,0) (1,0) (1,1) (0,1), matching the corner
            # order used above.
            u = (0.0, 1.0, 1.0, 0.0)[k]
            v = (0.0, 0.0, 1.0, 1.0)[k]
            uvs.append(((rx + u * rw) / SKIN, (ry + v * rh) / SKIN))
        tris.append((base, base + 1, base + 2, base + 3))

    with open(path, "w", newline="\n") as fh:
        fh.write("# MC2UE5 character part -- Minecraft proportions, "
                 "pivot at the origin\n")
        for v in verts:
            fh.write("v %.3f %.3f %.3f\n" % v)
        for t in uvs:
            fh.write("vt %.6f %.6f\n" % t)
        # Every face normal is axis-aligned here, so one per face is enough.
        seen = set()
        for idx, (n, _c) in enumerate(faces):
            if n in seen:
                continue
            seen.add(n)
            fh.write("vn %d %d %d\n" % n)
        for quad in tris:
            fh.write("f %d/%d/%d %d/%d/%d %d/%d/%d %d/%d/%d\n" % (
                quad[0] + 1, quad[0] + 1, quad[0] + 1,
                quad[1] + 1, quad[1] + 1, quad[1] + 1,
                quad[2] + 1, quad[2] + 1, quad[2] + 1,
                quad[3] + 1, quad[3] + 1, quad[3] + 1))
    return len(verts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    skin = build_skin()
    skin_path = os.path.join(args.out, "char_skin.png")
    write_png(skin_path, SKIN, SKIN, skin)
    print("skin  : %s (%d bytes)" % (skin_path, os.path.getsize(skin_path)))

    T = 25.0

    # Head: centred on its own origin, which sits at the middle of the head.
    write_obj(os.path.join(args.out, "char_head.obj"), 50.0, 50.0, 50.0,
              (0.0, 0.0, 0.0),
              {0: HEAD_RIGHT, 1: HEAD_LEFT, 2: HEAD_BOTTOM, 3: HEAD_TOP,
               4: HEAD_BACK, 5: HEAD_FRONT})
    print("head  : 50 x 50 x 50, centre at the origin")

    # Torso: the component sits at mid-torso, so the box is centred there too.
    write_obj(os.path.join(args.out, "char_torso.obj"), 50.0, 25.0, 75.0,
              (0.0, 0.0, 0.0),
              {0: BODY_RIGHT, 1: BODY_LEFT, 2: BODY_TOP, 3: BODY_BOTTOM,
               4: BODY_FRONT, 5: BODY_BACK})
    print("torso : 50 x 25 x 75")

    # Arms and legs hang *below* the pivot: the component origin is the joint.
    write_obj(os.path.join(args.out, "char_arm.obj"), T, T, 75.0,
              (0.0, 0.0, -37.5),
              {0: ARM_RIGHT, 1: ARM_LEFT, 2: ARM_TOP, 3: ARM_BOTTOM,
               4: ARM_FRONT, 5: ARM_BACK})
    write_obj(os.path.join(args.out, "char_leg.obj"), T, T, 75.0,
              (0.0, 0.0, -37.5),
              {0: LEG_RIGHT, 1: LEG_LEFT, 2: LEG_TOP, 3: LEG_BOTTOM,
               4: LEG_FRONT, 5: LEG_BACK})
    print("arm   : 25 x 25 x 75, hanging from the shoulder pivot")
    print("leg   : 25 x 25 x 75, hanging from the hip pivot")

    # Proportions, for the record and for the character placement code.
    print("total height: 200 cm (2 blocks); shoulder span 100 cm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
