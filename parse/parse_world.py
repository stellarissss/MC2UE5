#!/usr/bin/env python3
"""
Minecraft 1.16.5 Anvil world parser  (Route C / Stage 1 / Layer 1)

Reads a 1.16.5 save, walks every dimension (overworld / DIM-1 / DIM1), decodes
each chunk's `Palette` + `BlockStates` and aggregates statistics.

Design notes / correctness traps handled here:

1. Chunk data compression is type 2 (zlib) in this save. Types 1/3 are supported
   too; anything else is reported rather than silently mis-decoded.

2. 1.16.5 stores, per Section:
       Y           : TAG_Byte
       Palette     : TAG_List of {Name, Properties}   <-- section level!
       BlockStates : TAG_Long_Array                   <-- a RAW long array,
                                                         NOT a compound
   (The `block_states` compound with `palette` + `indices` is the 1.18+ layout.)
   A Section with a single-entry Palette and no BlockStates is a *uniform*
   section: all 4096 blocks are that entry.

3. THE BIG ONE -- bit packing is NON-STRETCH for DataVersion < 2529 (20w17a).
   Each 64-bit long holds floor(64/bits) whole values and a value NEVER straddles
   two longs. The "textbook" formula `long = i*bits//64, shift = (i*bits)%64`
   silently produces WRONG indices whenever bits does not divide 64 (5,6,7,9..15).
   Verified against anvil-parser: non-stretch = 0 mismatches, stretch = 155.

4. Palette index 0 is NOT guaranteed to be air; air must be detected by name
   (minecraft:air / cave_air / void_air).

5. Stats only ever need counts, so sections are decoded with vectorised numpy
   (bincount over palette indices) instead of per-block Python objects. Peak
   memory stays at one chunk regardless of world size.
"""

import argparse
import gzip
import io
import json
import os
import struct
import sys
import time
import zlib
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from nbt.nbt import NBTFile

AIR_NAMES = frozenset(("minecraft:air", "minecraft:cave_air", "minecraft:void_air"))

# (logical name, subdirectory relative to the save root)
DIMENSIONS = (
    ("overworld", ""),
    ("nether", "DIM-1"),
    ("end", "DIM1"),
)

SECTION_Y_OFFSETS = 16  # blocks per section
U64 = np.uint64

# v2 voxel word field limits (see write_voxel_file):
#   bits 0-3 dx | 4-7 dz | 8-16 worldY (9 bits) | 17-31 localPaletteIdx (15 bits)
MAX_VOXEL_Y = 0x1FF            # 511
MAX_LOCAL_PALETTE_IDX = 0x7FFF  # 32767


# --------------------------------------------------------------------------- #
# low level helpers
# --------------------------------------------------------------------------- #
def v(tag):
    """Unwrap an nbt tag into a plain python value."""
    return tag.value if hasattr(tag, "value") else tag


def props_key(props):
    """Canonical, order-independent string for a Properties compound."""
    if not props:
        return ""
    return ";".join("%s=%s" % (k, props[k]) for k in sorted(props))


def state_key(name, props):
    return name, props_key(props)


def bits_for(palette_size):
    """Bits per block entry. 1.16 always uses at least 4."""
    if palette_size <= 1:
        return 0
    return max(4, (palette_size - 1).bit_length())


def as_u64(longs):
    """
    View a TAG_Long_Array as unsigned 64-bit words.

    NBT longs are *signed*, so indices whose top bit is set come back as
    negative Python ints. Reinterpreting via int64 -> uint64 preserves the bit
    pattern (this is the on-disk representation, not a numeric conversion).
    """
    return np.asarray(longs, dtype=np.int64).view(np.uint64)


def unpack_non_stretch(longs, bits, count=4096):
    """
    Decode `count` palette indices from a TAG_Long_Array.

    NON-STRETCH (1.16.5, DataVersion < 2529): every long holds floor(64/bits)
    whole values, so value i lives in long i//per at bit offset (i%per)*bits.
    """
    per = 64 // bits
    arr = as_u64(longs)
    need = -(-count // per)
    if arr.size < need:  # defensive: malformed / truncated
        arr = np.concatenate([arr, np.zeros(need - arr.size, dtype=np.uint64)])
    i = np.arange(count, dtype=np.int64)
    shifts = (i % per).astype(np.uint64) * U64(bits)
    return ((arr[i // per] >> shifts) & U64((1 << bits) - 1)).astype(np.int32)


def unpack_stretch(longs, bits, count=4096):
    """The WRONG-for-1.16.5 formula, kept only so --selftest can prove it wrong."""
    arr = as_u64(longs)
    need = -(-count * bits // 64)
    if arr.size < need:
        arr = np.concatenate([arr, np.zeros(need - arr.size, dtype=np.uint64)])
    sh = np.arange(count, dtype=np.uint64) * U64(bits)
    return ((arr[sh // U64(64)] >> (sh % U64(64))) & U64((1 << bits) - 1)).astype(np.int32)


def decompress(payload, ctype):
    if ctype == 1:
        return gzip.decompress(payload)
    if ctype == 2:
        return zlib.decompress(payload)
    if ctype == 3:
        return payload
    raise ValueError("unsupported compression type %d" % ctype)


def find_regions(save_root, subdir):
    rdir = os.path.join(save_root, subdir, "region") if subdir else os.path.join(save_root, "region")
    if not os.path.isdir(rdir):
        return [], []
    mcas, mccs = [], []
    for fn in sorted(os.listdir(rdir)):
        if not fn.endswith(".mca"):
            continue
        stem = fn[:-4]
        try:
            rx, rz = (int(x) for x in stem.split(".")[1:3])
        except (ValueError, IndexError):
            continue
        mcas.append((rx, rz, os.path.join(rdir, fn)))
    for fn in sorted(os.listdir(rdir)):
        if fn.endswith(".mcc"):
            mccs.append(os.path.join(rdir, fn))
    mcas.sort(key=lambda t: (t[0], t[1]))
    return mcas, mccs


# --------------------------------------------------------------------------- #
# palette helpers
# --------------------------------------------------------------------------- #
def read_palette(section):
    """Return [(name, canonical_props), ...] for a Section's Palette."""
    out = []
    for entry in section["Palette"]:
        name = v(entry["Name"])
        ptag = entry.get("Properties")
        props = {}
        if ptag is not None:
            for k in ptag.keys():
                props[k] = v(ptag[k])
        out.append((name, props_key(props)))
    return out


# --------------------------------------------------------------------------- #
# core chunk decode
# --------------------------------------------------------------------------- #
class ChunkResult(object):
    __slots__ = ("counts", "nonair", "ymin", "ymax", "biomes",
                 "chunk_local_palette", "packed_voxels", "xmin", "xmax",
                 "zmin", "zmax")


def decode_chunk(level, want_voxels, origin_x=0, origin_z=0):
    """
    Decode one chunk's Level compound.

    `origin_x`/`origin_z` are the absolute block coordinates of the chunk's
    (0,0) corner (= regionX*512 + localX*16). They are used only to report the
    true voxel extent in `xmin/xmax/zmin/zmax`; voxel *storage* stays
    chunk-local. Defaults of 0 keep chunk-local-only callers working.

    Returns a ChunkResult. Counts are aggregated per unique block *state*
    (name + properties); `nonair` counts blocks that are not any flavour of air.
    """
    res = ChunkResult()
    res.counts = Counter()
    res.nonair = 0
    res.ymin = None
    res.ymax = None
    res.biomes = None
    res.chunk_local_palette = []
    res.packed_voxels = []
    res.xmin = res.xmax = res.zmin = res.zmax = None

    sections = level.get("Sections")
    if sections is None:
        return res

    # chunk-local palette so the voxel stream can carry small indices
    local_index = {}
    local_list = []
    voxel_chunks = []

    for sec in sections:
        if "Palette" not in sec:
            continue  # empty / light-only section
        pal = read_palette(sec)
        if not pal:
            continue
        sec_y = int(v(sec["Y"]))

        # local palette entries for this chunk
        loc_of = []
        for st in pal:
            j = local_index.get(st)
            if j is None:
                j = len(local_list)
                local_index[st] = j
                local_list.append(st)
            loc_of.append(j)
        loc_of = np.asarray(loc_of, dtype=np.int32)

        air_mask = np.asarray([st[0] in AIR_NAMES for st in pal], dtype=bool)

        if "BlockStates" not in sec:
            # uniform section: single palette entry fills all 4096 blocks
            if len(pal) == 1 and not air_mask[0]:
                res.counts[pal[0]] += 4096
                res.nonair += 4096
                lo = hi = sec_y * SECTION_Y_OFFSETS
                res.ymin = lo if res.ymin is None else min(res.ymin, lo)
                res.ymax = hi if res.ymax is None else max(res.ymax, hi + 15)
                # uniform section fills all 16x16 columns
                res.xmin = origin_x
                res.xmax = origin_x + 15
                res.zmin = origin_z
                res.zmax = origin_z + 15
                if want_voxels:
                    pos = np.arange(4096, dtype=np.int64)
                    voxel_chunks.append(
                        (pos % 16)
                        | (((pos // 16) % 16) << 4)
                        | ((sec_y * SECTION_Y_OFFSETS + (pos // 256)) << 8)
                        | (np.int64(loc_of[0]) << 17)
                    )
            continue

        bits = bits_for(len(pal))
        if bits == 0:
            continue
        idx = unpack_non_stretch(sec["BlockStates"].value, bits)

        # per-palette-index histogram, one bincount per section
        bc = np.bincount(idx, minlength=len(pal))
        for pi in np.nonzero(bc)[0]:
            st = pal[pi]
            if st[0] not in AIR_NAMES:
                res.counts[st] += int(bc[pi])

        keep = ~air_mask[idx]
        n_keep = int(keep.sum())
        res.nonair += n_keep
        if n_keep:
            pos = np.nonzero(keep)[0]
            dy = (pos // 256).astype(np.int64)
            lo = sec_y * SECTION_Y_OFFSETS + int(dy.min())
            hi = sec_y * SECTION_Y_OFFSETS + int(dy.max())
            res.ymin = lo if res.ymin is None else min(res.ymin, lo)
            res.ymax = hi if res.ymax is None else max(res.ymax, hi)
            # True voxel extent. `pos` is a chunk-local voxel index laid out as
            # y*256 + z*16 + x, so the column range comes straight from it --
            # far tighter than assuming the whole 16x16 chunk is occupied.
            cx_lo = origin_x + int((pos % 16).min())
            cx_hi = origin_x + int((pos % 16).max())
            cz_lo = origin_z + int(((pos // 16) % 16).min())
            cz_hi = origin_z + int(((pos // 16) % 16).max())
            res.xmin = cx_lo if res.xmin is None else min(res.xmin, cx_lo)
            res.xmax = cx_hi if res.xmax is None else max(res.xmax, cx_hi)
            res.zmin = cz_lo if res.zmin is None else min(res.zmin, cz_lo)
            res.zmax = cz_hi if res.zmax is None else max(res.zmax, cz_hi)
            if want_voxels:
                local = loc_of[idx[pos]].astype(np.int64)
                wy = sec_y * SECTION_Y_OFFSETS + dy
                if wy.max() > MAX_VOXEL_Y or local.max() > MAX_LOCAL_PALETTE_IDX:
                    raise ValueError(
                        "v2 voxel word overflow: y=%d localIdx=%d (max %d / %d)"
                        % (wy.max(), local.max(), MAX_VOXEL_Y, MAX_LOCAL_PALETTE_IDX))
                voxel_chunks.append(
                    (pos % 16)
                    | (((pos // 16) % 16) << 4)
                    | (wy << 8)
                    | (local << 17)
                )

    bio = level.get("Biomes")
    # 1.16.5 stores Biomes as a 1024-entry IntArray (256 pre-1.15 layouts used a
    # 256-byte ByteArray). Accept both shapes.
    if bio is not None and len(bio) in (256, 1024):
        # Depending on the nbt version, items surface either as TAG objects or
        # as plain ints -- accept both.
        res.biomes = np.fromiter(
            (int(v(b)) & 0xFF for b in bio), dtype=np.uint8, count=len(bio)
        )

    res.chunk_local_palette = local_list
    if want_voxels and voxel_chunks:
        res.packed_voxels = np.concatenate(voxel_chunks).astype(np.uint32)
    elif want_voxels:
        # Always hand back an ndarray in voxel mode. Leaving the `[]` sentinel
        # in place made the caller's `res.packed_voxels.size` raise
        # AttributeError for every all-air chunk, which under --full turned into
        # 3452 spurious "errors" in stats.json for the overworld and 465 for the
        # end -- while still writing correct output, so the bug hid behind a
        # plausible-looking error count.
        res.packed_voxels = np.empty(0, dtype=np.uint32)
    return res


# --------------------------------------------------------------------------- #
# region worker
# --------------------------------------------------------------------------- #
PART_MAGIC = 0x4D433257  # "MC2W"
PART_HEADER = struct.Struct("<IIQ")  # magic, chunkCount, tableBytes
# rx, rz (region coords), cx, cz (region-local), palCount, pad, voxelCount, voxelOffset
PART_CHUNK = struct.Struct("<iiiiHHIQ")


def worker(task):
    (dim, rx, rz, path, sample_dir) = task
    t0 = time.time()
    out = {
        "dim": dim, "rx": rx, "rz": rz,
        "chunks": 0, "nonair": 0,
        "counts": Counter(),
        "ymin": None, "ymax": None,
        "xmin": None, "xmax": None, "zmin": None, "zmax": None,
        "biomes": Counter(),
        "sections": 0,
        "errors": [],
        "error_count": 0,
        "compression_types": Counter(),
    }
    part_meta = None

    with open(path, "rb") as f:
        header = f.read(4096)
        if len(header) < 4096:
            out["errors"].append("short header")
            return out, None
        try:
            timestamps = f.read(4096)
            if len(timestamps) < 4096:
                out["errors"].append("missing timestamp table")
        except Exception as exc:  # pragma: no cover
            out["errors"].append("timestamps: %s" % exc)

        table = bytearray()
        table_rel = 0
        pending = []  # (cx, cz, local_palette, packed_bytes)

        for idx in range(1024):
            entry = struct.unpack_from(">I", header, idx * 4)[0]
            offset = entry >> 8
            if offset == 0:
                continue
            cx = idx & 31
            cz = idx >> 5
            try:
                f.seek(offset * 4096)
                head = f.read(5)
                if len(head) < 5:
                    raise ValueError("truncated chunk header")
                length = int.from_bytes(head[:4], "big")
                ctype = head[4]
                out["compression_types"][ctype] += 1
                raw = decompress(f.read(length - 1), ctype)
                root = NBTFile(buffer=io.BytesIO(raw))
                level = root["Level"] if "Level" in root else root

                want_voxels = sample_dir is not None
                res = decode_chunk(level, want_voxels,
                                   origin_x=rx * 512 + cx * 16,
                                   origin_z=rz * 512 + cz * 16)

                out["chunks"] += 1
                if res.ymin is not None:
                    out["ymin"] = res.ymin if out["ymin"] is None else min(out["ymin"], res.ymin)
                    out["ymax"] = res.ymax if out["ymax"] is None else max(out["ymax"], res.ymax)
                out["sections"] += len(level.get("Sections") or [])
                out["nonair"] += res.nonair
                out["counts"].update(res.counts)
                if res.nonair:
                    # Track the *actual* voxel extent, not the chunk grid.
                    # Using the chunk bounds (cx*16 .. cx*16+15) inflated the
                    # reported world range: a chunk whose only non-air blocks
                    # sit in one corner would still claim all 16 columns, and a
                    # chunk with a single non-air block claimed a full 16x16
                    # footprint. That made world_x/z_range disagree with the
                    # exported .bin (which stores true voxel coordinates) and
                    # would have mis-sized the phase-2 terrain tiles.
                    wx = res.xmin, res.xmax
                    wz = res.zmin, res.zmax
                    out["xmin"] = wx[0] if out["xmin"] is None else min(out["xmin"], wx[0])
                    out["xmax"] = wx[1] if out["xmax"] is None else max(out["xmax"], wx[1])
                    out["zmin"] = wz[0] if out["zmin"] is None else min(out["zmin"], wz[0])
                    out["zmax"] = wz[1] if out["zmax"] is None else max(out["zmax"], wz[1])
                if res.biomes is not None:
                    out["biomes"].update(Counter(res.biomes.tolist()))
                if want_voxels:
                    pv = res.packed_voxels
                    if pv is not None and len(pv):
                        pending.append((cx, cz, res.chunk_local_palette,
                                        pv.tobytes()))
            except Exception as exc:
                out["error_count"] += 1
                if len(out["errors"]) < 5:
                    out["errors"].append("chunk(%d,%d): %r" % (cx, cz, exc))

        # ---- write this region's voxel part file (voxel mode only) ----------
        if sample_dir is not None:
            os.makedirs(sample_dir, exist_ok=True)
            part_path = os.path.join(sample_dir, "%s_%d_%d.part" % (dim, rx, rz))
            table = bytearray()
            voxel_rel = 0
            for cx, cz, local_pal, vox in pending:
                table.extend(PART_CHUNK.pack(rx, rz, cx, cz, len(local_pal), 0,
                                             len(vox) // 4, voxel_rel))
                for nm, pk in local_pal:
                    nb = nm.encode("utf-8")
                    pb = pk.encode("utf-8")
                    table.extend(struct.pack("<HH", len(nb), len(pb)))
                    table.extend(nb)
                    table.extend(pb)
                voxel_rel += len(vox)
            with open(part_path, "wb") as pf:
                pf.write(PART_HEADER.pack(PART_MAGIC, len(pending), len(table)))
                pf.write(bytes(table))
                for _cx, _cz, _pal, vox in pending:
                    pf.write(vox)
            part_meta = {"path": part_path}

    out["seconds"] = time.time() - t0
    return out, part_meta


# --------------------------------------------------------------------------- #
# level.dat
# --------------------------------------------------------------------------- #
GAMETYPES = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}


def parse_level_dat(path):
    if not os.path.isfile(path):
        return {"error": "level.dat not found"}
    d = NBTFile(path)["Data"]
    ver = d.get("Version") or {}
    wgs = d.get("WorldGenSettings") or {}
    out = {
        "level_name": v(d.get("LevelName")),
        "game_type": v(d.get("GameType")),
        "game_type_name": GAMETYPES.get(v(d.get("GameType")), "unknown"),
        "difficulty": v(d.get("Difficulty")),
        "hardcore": bool(v(d.get("hardcore"))) if "hardcore" in d else None,
        "data_version": v(d.get("DataVersion")),
        "version_id": v(ver.get("Id")) if ver else None,
        "version_name": v(ver.get("Name")) if ver else None,
        "spawn": {
            "x": v(d.get("SpawnX")), "y": v(d.get("SpawnY")), "z": v(d.get("SpawnZ")),
            "angle": v(d.get("SpawnAngle")),
        },
        "seed": v(wgs.get("seed")) if wgs else None,
        "generate_features": v(wgs.get("generate_features")) if wgs else None,
        "bonus_chest": v(wgs.get("bonus_chest")) if wgs else None,
        "was_modded": bool(v(d.get("WasModded"))) if "WasModded" in d else None,
        "last_played_ms": v(d.get("LastPlayed")),
        "time": v(d.get("Time")),
        "day_time": v(d.get("DayTime")),
        "initialized": bool(v(d.get("initialized"))) if "initialized" in d else None,
    }
    return out


# --------------------------------------------------------------------------- #
# sample writer
# --------------------------------------------------------------------------- #
FILE_MAGIC = b"MC2WV2\0\0"
FILE_HEADER = struct.Struct("<8sIB3sQQQQQQ")  # 64 bytes
# absChunkX, absChunkZ, palCount, pad, voxelCount, voxelOffset
CHUNK_TAB = struct.Struct("<iiHHIQ")


def write_voxel_file(out_path, dim_id, parts, meta_by_dim):
    """
    Assemble the intermediate voxel file for one dimension from per-region .part
    files (MC2WV2 layout).

    Layout:
        [0]      header (64 B)
        [64]     global palette  (unique block states, shared by all chunks)
        [..]     chunk table     (one row per chunk + local->global palette map)
        [..]     voxel stream    (4 B per non-air block)

    Voxel word v2: bits 0-3 dx, 4-7 dz, 8-16 worldY (absolute, 9 bits),
                   17-31 chunk-local palette index (15 bits).

    v2 differs from v1 in two ways that matter to layer 3:
      * chunkX/chunkZ are ABSOLUTE chunk coords (regionX*32 + local), so one
        file can aggregate many regions without needing the region filename;
      * the voxel word carries absolute world Y (v1 stored only the 4-bit
        section-local Y and silently lost everything above y=15).
    """
    palette = []
    gidx = {}

    def gid(st):
        j = gidx.get(st)
        if j is None:
            j = len(palette)
            gidx[st] = j
            palette.append(st)
        return j

    rows = []
    for part in parts:
        for m in meta_by_dim[part]:
            local = m["local"]
            gmap = [gid(st) for st in local]
            abs_cx = m["rx"] * 32 + m["cx"]
            abs_cz = m["rz"] * 32 + m["cz"]
            rows.append((abs_cx, abs_cz, gmap, m["voxels"], part))

    # palette blob
    pbuf = bytearray()
    pbuf.extend(struct.pack("<I", len(palette)))
    for name, pk in palette:
        nb, pb = name.encode("utf-8"), pk.encode("utf-8")
        pbuf.extend(struct.pack("<HH", len(nb), len(pk)))
        pbuf.extend(nb)
        pbuf.extend(pb)

    total_voxels = sum(r[3] for r in rows)

    palette_off = FILE_HEADER.size
    ctab_off = palette_off + len(pbuf)

    # The chunk table has a variable-size palette map per row, so the voxel
    # section offset depends on the table size: compute the table size first,
    # then lay it out at the correct base offset.
    table_size = sum(CHUNK_TAB.size + 4 * len(r[2]) for r in rows)
    vdata_off = ctab_off + table_size

    ctbuf = bytearray()
    vox_off = vdata_off
    for cx, cz, gmap, vcount, part in rows:
        ctbuf.extend(CHUNK_TAB.pack(cx, cz, len(gmap), 0, vcount, vox_off))
        ctbuf.extend(struct.pack("<%dI" % len(gmap), *gmap))
        vox_off += vcount * 4
    assert len(ctbuf) == table_size, (len(ctbuf), table_size)

    header = FILE_HEADER.pack(
        FILE_MAGIC, 2, dim_id, b"\0\0\0", len(rows), total_voxels,
        len(palette), palette_off, ctab_off, vdata_off,
    )
    assert len(header) == FILE_HEADER.size, len(header)

    with open(out_path, "wb") as f:
        f.write(header)
        f.write(bytes(pbuf))
        f.write(bytes(ctbuf))
        # stream voxel payloads region by region, in the same order as `rows`
        cur = None
        fh = None
        try:
            for cx, cz, gmap, vcount, part in rows:
                if cur != part:
                    if fh:
                        fh.close()
                    fh = open(part, "rb")
                    magic, nch, tbytes = PART_HEADER.unpack(fh.read(PART_HEADER.size))
                    if magic != PART_MAGIC:
                        raise IOError("bad part magic in %s" % part)
                    fh.seek(PART_HEADER.size + tbytes)
                    cur = part
                remaining = vcount * 4
                while remaining:
                    buf = fh.read(min(remaining, 1 << 20))
                    if not buf:
                        raise IOError("unexpected EOF in %s" % part)
                    f.write(buf)
                    remaining -= len(buf)
        finally:
            if fh:
                fh.close()

    return {
        "path": out_path,
        "bytes": os.path.getsize(out_path),
        "chunks": len(rows),
        "voxels": total_voxels,
        "palette_entries": len(palette),
        "regions": len(parts),
    }


def read_part_metadata(part_paths):
    """Walk each .part table to recover per-chunk metadata for assembly."""
    meta = {}
    for p in part_paths:
        with open(p, "rb") as f:
            magic, nch, tbytes = PART_HEADER.unpack(f.read(PART_HEADER.size))
            if magic != PART_MAGIC:
                raise IOError("bad part magic in %s" % p)
            data = f.read(tbytes)
        off = 0
        entries = []
        for _ in range(nch):
            rx, rz, cx, cz, palcount, _pad, vcount, _rel = PART_CHUNK.unpack_from(data, off)
            off += PART_CHUNK.size
            local = []
            for _ in range(palcount):
                nlen, plen = struct.unpack_from("<HH", data, off)
                off += 4
                nm = data[off:off + nlen].decode("utf-8")
                off += nlen
                pk = data[off:off + plen].decode("utf-8")
                off += plen
                local.append((nm, pk))
            entries.append({"rx": rx, "rz": rz, "cx": cx, "cz": cz,
                            "voxels": vcount, "local": local})
        meta[p] = entries
    return meta


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #
def selftest(save_root):
    """
    Prove the non-stretch unpacking is right and the naive formula is wrong,
    by comparing both against the anvil-parser reference implementation.
    """
    try:
        import anvil
    except ImportError:
        print("selftest: anvil-parser not installed, skipping cross-check")
        return
    import random

    mcas, _ = find_regions(save_root, "")
    rng = random.Random(11)
    checked = 0
    ns_bad = st_bad = tested = 0
    bits_seen = set()
    for rx, rz, path in mcas:
        if checked >= 12:
            break
        ref = anvil.Region.from_file(path)
        with open(path, "rb") as f:
            header = f.read(4096)
            order = [ci for ci in range(1024)
                     if struct.unpack_from(">I", header, ci * 4)[0] >> 8]
            if not order:
                continue  # empty region contributes nothing to the check
            rng.shuffle(order)
            for ci in order:
                entry = struct.unpack_from(">I", header, ci * 4)[0]
                if entry >> 8 == 0:
                    continue
                f.seek((entry >> 8) * 4096)
                head = f.read(5)
                length = int.from_bytes(head[:4], "big")
                raw = decompress(f.read(length - 1), head[4])
                root = NBTFile(buffer=io.BytesIO(raw))
                level = root["Level"] if "Level" in root else root
                chunk = ref.get_chunk(int(v(level["xPos"])), int(v(level["zPos"])))
                for sec in (level.get("Sections") or []):
                    if "BlockStates" not in sec or "Palette" not in sec:
                        continue
                    pal = read_palette(sec)
                    bits = bits_for(len(pal))
                    if bits < 5:
                        continue
                    longs = sec["BlockStates"].value
                    a = unpack_non_stretch(longs, bits)
                    b = unpack_stretch(longs, bits)
                    sy = int(v(sec["Y"]))
                    # expected long count is the decisive structural check
                    struct_ok = len(longs) == -(-4096 // (64 // bits))
                    ns_bad += 0 if struct_ok else 1
                    bits_seen.add(bits)
                    for i in rng.sample(range(4096), 150):
                        x, z, y = i % 16, (i // 16) % 16, i // 256
                        blk = chunk.get_block(x, sy * 16 + y, z)
                        cand = [j for j, st in enumerate(pal)
                                if st[0] == blk.namespace + ":" + blk.id
                                and st[1] == props_key(blk.properties)]
                        if not cand:
                            continue
                        tested += 1
                        ns_bad += a[i] not in cand
                        st_bad += b[i] not in cand
                    checked += 1
                if checked >= 12:
                    break

    print("selftest: %d sections (bits seen: %s), %d sampled blocks"
          % (checked, sorted(bits_seen), tested))
    print("  non-stretch formula : %d mismatches  <-- must be 0" % ns_bad)
    print("  naive i*bits//64    : %d mismatches  (expected > 0)" % st_bad)
    if ns_bad or not bits_seen:
        print("  FAIL")
        sys.exit(1)
    print("  OK")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Parse a Minecraft 1.16.5 Anvil save")
    ap.add_argument("--save", required=True, help="save root (contains level.dat)")
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)),
                    help="output dir for stats.json (default: this script's dir)")
    ap.add_argument("--voxel-dir", default=None,
                    help="dir for the prototype voxel sample (default: <out>/../voxel_data)")
    ap.add_argument("--sample-regions", type=int, default=1,
                    help="how many overworld regions to export as the sample")
    ap.add_argument("--full", action="store_true",
                    help="export EVERY region of EVERY dimension to "
                         "<voxel-dir>/full/{overworld,nether,end}.bin (MC2WV2)")
    ap.add_argument("--keep-parts", action="store_true",
                    help="with --full, keep the intermediate _parts/*.part files")
    ap.add_argument("--jobs", type=int, default=min(16, os.cpu_count() or 4))
    ap.add_argument("--limit-regions", type=int, default=0, help="debug: cap regions per dim")
    ap.add_argument("--selftest", action="store_true",
                    help="validate bit-unpacking against anvil-parser and exit")
    args = ap.parse_args()

    save_root = os.path.abspath(args.save)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    voxel_dir = args.voxel_dir or os.path.abspath(
        os.path.join(out_dir, os.pardir, "voxel_data"))
    os.makedirs(voxel_dir, exist_ok=True)
    full_dir = os.path.join(voxel_dir, "full")
    if args.full:
        os.makedirs(full_dir, exist_ok=True)

    if args.selftest:
        selftest(save_root)
        return

    if not os.path.isdir(save_root):
        sys.exit("save root not found: %s" % save_root)

    t_start = time.time()
    level_info = parse_level_dat(os.path.join(save_root, "level.dat"))

    stats = {
        "save_root": save_root,
        "level": level_info,
        "dimensions": {},
        "global": {},
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    all_states = Counter()
    dim_ids = {"overworld": 0, "nether": 1, "end": 2}

    for dim, subdir in DIMENSIONS:
        mcas, mccs = find_regions(save_root, subdir)
        if args.limit_regions:
            mcas = mcas[: args.limit_regions]
        if not mcas:
            stats["dimensions"][dim] = {"region_count": 0, "chunk_count": 0,
                                        "non_air_blocks": 0}
            continue

        # --full exports EVERY region of EVERY dimension; the default sample mode
        # only exports the first --sample-regions regions of the overworld.
        if args.full:
            part_dir = os.path.join(full_dir, "_parts", dim)
        else:
            n_sample = args.sample_regions if dim == "overworld" else 0
            part_dir = os.path.join(voxel_dir, "_parts") if n_sample else None
        if part_dir and os.path.isdir(part_dir):
            for fn in os.listdir(part_dir):  # drop stale parts from earlier runs
                if fn.endswith(".part"):
                    os.remove(os.path.join(part_dir, fn))

        tasks = []
        for i, (rx, rz, path) in enumerate(mcas):
            keep = part_dir if (args.full or i < n_sample) else None
            tasks.append((dim, rx, rz, path, keep))

        counts = Counter()
        biomes = Counter()
        ctypes = Counter()
        chunks = nonair = sections = 0
        bounds = {}  # key -> [min, max]
        errors = []
        error_count = 0
        part_paths = []

        t0 = time.time()
        if args.jobs > 1:
            with ProcessPoolExecutor(max_workers=args.jobs) as ex:
                results = list(ex.map(worker, tasks))
        else:
            results = [worker(t) for t in tasks]

        for res, pmeta in results:
            chunks += res["chunks"]
            nonair += res["nonair"]
            sections += res["sections"]
            counts.update(res["counts"])
            biomes.update(res["biomes"])
            ctypes.update(res["compression_types"])
            # (slot, is_min) pairs: the slot key is the axis ("x"/"y"/"z"), so
            # the branch test has to match the *slot*, not the field name.
            # The previous check tested ("xmin","ymax","xmin","zmin") against
            # slot "x"/"y"/"z", so it was never True -- min-side bounds stayed
            # frozen at whichever region happened to be merged first, silently
            # under-reporting the world extent.
            for key, is_min, val in (("y", True, res["ymin"]),
                                      ("y", False, res["ymax"]),
                                      ("x", True, res["xmin"]),
                                      ("x", False, res["xmax"]),
                                      ("z", True, res["zmin"]),
                                      ("z", False, res["zmax"])):
                if val is None:
                    continue
                slot = bounds.get(key)
                if slot is None:
                    bounds[key] = [val, val]
                elif is_min:
                    slot[0] = min(slot[0], val)
                else:
                    slot[1] = max(slot[1], val)
            errors.extend("r.%d.%d %s" % (res["rx"], res["rz"], e) for e in res["errors"][:5])
            error_count += res["error_count"]
            if pmeta:
                part_paths.append(pmeta["path"])

        ymin, ymax = bounds.get("y", [None, None])
        xmin, xmax = bounds.get("x", [None, None])
        zmin, zmax = bounds.get("z", [None, None])
        elapsed = time.time() - t0
        all_states.update(counts)

        name_counts = Counter()
        for (name, _pk), c in counts.items():
            name_counts[name] += c

        top = name_counts.most_common(30)
        top_states = counts.most_common(30)

        def span(a, b):
            return None if a is None else [a, b]

        stats["dimensions"][dim] = {
            "region_count": len(mcas),
            "external_mcc_files": len(mccs),
            "chunk_count": chunks,
            "section_count": sections,
            "non_air_blocks": nonair,
            "distinct_block_types": len(name_counts),
            "distinct_block_states": len(counts),
            "y_range": span(ymin, ymax),
            "world_x_range": span(xmin, xmax),
            "world_z_range": span(zmin, zmax),
            "top30_block_types": [{"name": n, "count": c} for n, c in top],
            "top30_block_states": [
                {"name": n, "properties": p, "count": c} for (n, p), c in top_states],
            "biome_id_histogram": [
                {"id": int(b), "count": int(c)} for b, c in biomes.most_common(20)],
            "compression_types": {str(k): v_ for k, v_ in ctypes.items()},
            "parse_seconds": round(elapsed, 2),
            "errors": errors[:20],
            "error_count": error_count,
        }
        print("[%s] regions=%d chunks=%d nonair=%d types=%d states=%d %.1fs"
              % (dim, len(mcas), chunks, nonair, len(name_counts), len(counts), elapsed))

        if part_paths:
            meta = read_part_metadata(part_paths)
            if args.full:
                out_path = os.path.join(full_dir, "%s.bin" % dim)
            else:
                out_path = os.path.join(voxel_dir, "sample_%s.bin" % dim)
            info = write_voxel_file(out_path, dim_ids[dim], part_paths, meta)
            stats["dimensions"][dim]["voxel_file" if args.full else "sample_file"] = info
            print("    %s -> %s (%.2f MiB, %d chunks, %d voxels, %d regions)"
                  % ("full   " if args.full else "sample ", info["path"],
                     info["bytes"] / 1048576.0, info["chunks"], info["voxels"],
                     info["regions"]))
            if args.full and not args.keep_parts:
                for p in part_paths:  # parts are ~another full copy on disk
                    os.remove(p)

    g_chunks = sum(d.get("chunk_count", 0) for d in stats["dimensions"].values())
    g_nonair = sum(d.get("non_air_blocks", 0) for d in stats["dimensions"].values())
    g_types = set()
    g_states = set()
    gname = Counter()
    for d in stats["dimensions"].values():
        gname.update({e["name"]: e["count"] for e in d.get("top30_block_types", [])})
    for (name, pk), _c in all_states.items():
        g_states.add((name, pk))
        g_types.add(name)

    stats["global"] = {
        "total_regions": sum(d.get("region_count", 0) for d in stats["dimensions"].values()),
        "total_chunks": g_chunks,
        "total_non_air_blocks": g_nonair,
        "distinct_block_types": len(g_types),
        "distinct_block_states": len(g_states),
        "top30_block_types_overworld": stats["dimensions"]["overworld"]["top30_block_types"][:30],
        "estimated_full_export_bytes": g_nonair * 4,
        "estimated_full_export_mib": round(g_nonair * 4 / 1048576.0, 1),
    }
    stats["parse_seconds_total"] = round(time.time() - t_start, 2)

    # global state table is the backbone of the intermediate format
    with open(os.path.join(out_dir, "block_states.json"), "w") as f:
        json.dump(
            {"count": len(all_states),
             "states": [{"name": n, "properties": p, "count": c}
                        for (n, p), c in all_states.most_common()]},
            f, indent=1)

    with open(os.path.join(out_dir, "stats.json"), "w") as f:
        json.dump(stats, f, indent=1, ensure_ascii=False)

    print("\nGLOBAL chunks=%d nonair=%d types=%d states=%d total=%.1fs"
          % (g_chunks, g_nonair, len(g_types), len(g_states),
             stats["parse_seconds_total"]))
    print("wrote %s" % os.path.join(out_dir, "stats.json"))


if __name__ == "__main__":
    main()
