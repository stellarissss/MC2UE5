"""
MC2WV2 reader -- the *only* thing phase 2 is allowed to know about layer 1.

Design rules, in priority order:

1. **Zero coupling to layer 1 internals.** This module re-implements the
   MC2WV2 decoder from the written spec (parse/INTERMEDIATE_FORMAT.md) rather
   than importing parse_world.py. If layer 1 is refactored, phase 2 keeps
   working as long as the on-disk contract holds.

2. **Read-only.** We open files 'rb' and never write to voxel_data/. Layer 1
   stays the single writer.

3. **Sparse in, dense out -- on demand only.** The voxel stream is sparse
   (non-air blocks only, ~143 MiB total). Materialising a dense
   (X x 256 x Z) uint8 array for the whole overworld would need
   4320*256*2960 = 3.3 GB. So we offer:
     - `iter_chunks()`  -- streaming, one chunk at a time (constant memory)
     - `ChunkCursor`    -- random access by (chunkX, chunkZ)
     - `column_scan()`  -- XZ columns, the shape terrain/semantic work needs
   A dense array is only ever built for a *bounded tile*, never the world.

Coordinate conventions (from the spec):
    worldX = chunkX * 16 + dx
    worldZ = chunkZ * 16 + dz
    worldY = absolute, 9 bits (0..511), stored in the voxel word
    voxel word = dx | dz<<4 | worldY<<8 | localPaletteIndex<<17
"""

import json
import mmap
import os
import struct

import numpy as np

MAGIC = b"MC2WV2\0\0"
HEADER = struct.Struct("<8sIB3sQQQQQQ")
CHUNK_TAB = struct.Struct("<iiHHIQ")

DIM_OVERWORLD, DIM_NETHER, DIM_END = 0, 1, 2
DIM_NAMES = {DIM_OVERWORLD: "overworld",
             DIM_NETHER: "nether",
             DIM_END: "end"}

# Voxel word field layout (v2).
_DX_MASK, _DX_SHIFT = 0xF, 0
_DZ_MASK, _DZ_SHIFT = 0xF, 4
_Y_MASK, _Y_SHIFT = 0x1FF, 8
_LPI_MASK, _LPI_SHIFT = 0x7FFF, 17

AIR_NAMES = frozenset(("minecraft:air", "minecraft:cave_air", "minecraft:void_air"))


class MC2WError(Exception):
    pass


# --------------------------------------------------------------------------- #
# header / palette / chunk table
# --------------------------------------------------------------------------- #

class VoxelFile(object):
    """
    Random-access reader for one dimension's MC2WV2 file.

    Typical use::

        with VoxelFile(path) as vf:
            print(vf.header["voxel_count"])
            for chunk in vf.iter_chunks():
                ...  # chunk.xyz (int32[N]), chunk.y (int32[N]), chunk.state (int32[N])
    """

    def __init__(self, path):
        self.path = path
        self._f = open(path, "rb")
        try:
            self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:
            # empty file: mmap refuses 0-length mappings
            self._mm = None

        raw = self._read(0, HEADER.size)
        (magic, version, dim_id, _res, n_chunks, n_voxels, n_pal,
         pal_off, tab_off, vox_off) = HEADER.unpack(raw)
        if magic != MAGIC:
            raise MC2WError("%s: bad magic %r, expected %r "
                            "(v1 files truncate Y above 15 and are not "
                            "interchangeable with v2)" % (path, magic, MAGIC))
        if version != 2:
            raise MC2WError("%s: unsupported version %d" % (path, version))

        self.version = version
        self.dimension_id = dim_id
        self.dimension = DIM_NAMES.get(dim_id, "dim%d" % dim_id)
        self.palette_offset = pal_off
        self.chunk_table_offset = tab_off
        self.voxel_offset = vox_off

        self.palette = self._read_palette(pal_off, n_pal)
        # block name -> palette index, for reverse lookups
        self._by_name = {}
        for i, (nm, _pk) in enumerate(self.palette):
            self._by_name.setdefault(nm, i)

        self._rows, self._row_index = self._read_chunk_table(tab_off, n_chunks)

        self.header = {
            "version": version,
            "dimension": self.dimension,
            "chunk_count": n_chunks,
            "voxel_count": n_voxels,
            "palette_count": n_pal,
            "palette_offset": pal_off,
            "chunk_table_offset": tab_off,
            "voxel_offset": vox_off,
        }

    # -- low level ---------------------------------------------------------- #

    def _read(self, off, n):
        if self._mm is not None:
            return self._mm[off:off + n]
        self._f.seek(off)
        return self._f.read(n)

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- sections ----------------------------------------------------------- #

    def _read_palette(self, off, count):
        """
        Walk palette entries sequentially. Entry layout is
        `u16 nameLen; u16 propsLen; name; props`, so entry sizes are not
        indexable -- a single sequential pass is both simplest and fastest.

        The section is bounded by chunk_table_offset (which the header already
        told us), so the memoryview covers exactly the right bytes.
        """
        mv = memoryview(self._read(off, self.chunk_table_offset - off))
        (n,) = struct.unpack_from("<I", mv, 0)
        if n != count:
            raise MC2WError("palette count mismatch: header %d payload %d"
                            % (count, n))
        out = []
        pos = 4
        for _ in range(n):
            nlen, plen = struct.unpack_from("<HH", mv, pos)
            pos += 4
            nm = bytes(mv[pos:pos + nlen]).decode("utf-8")
            pos += nlen
            pk = bytes(mv[pos:pos + plen]).decode("utf-8")
            pos += plen
            out.append((nm, pk))
        return out

    def _read_chunk_table(self, off, count):
        # Each row: fixed 24 B + 4 B per local palette entry.
        # Read the whole table in one go; it is < 2 MiB for this save.
        mv = memoryview(self._read(off, self.voxel_offset - off))
        rows = []
        pos = 0
        index = {}
        for _ in range(count):
            cx, cz, pcount, _pad, vcount, voff = CHUNK_TAB.unpack_from(mv, pos)
            pos += CHUNK_TAB.size
            gmap = np.frombuffer(mv, dtype="<u4", count=pcount, offset=pos)
            pos += 4 * pcount
            gmap = np.array(gmap, dtype=np.int32)
            rows.append((cx, cz, gmap, vcount, voff))
            index[(cx, cz)] = len(rows) - 1
        return rows, index

    # -- public API --------------------------------------------------------- #

    def chunk_rows(self):
        """[(chunkX, chunkZ, localToGlobal, voxelCount, voxelOffset), ...]"""
        return self._rows

    def state_name(self, palette_index):
        return self.palette[palette_index]

    def palette_index_of(self, block_name):
        """First palette index with this block name, or None."""
        return self._by_name.get(block_name)

    def iter_chunks(self, want_air=False):
        """
        Yield per-chunk decoded arrays. Constant memory.

        Each item is a dict with:
            chunkX, chunkZ : int
            xyz            : int32[N, 3] world coords (x, y, z)
            state          : int32[N] global palette index
        """
        for (cx, cz, gmap, vcount, voff) in self._rows:
            if vcount == 0:
                continue
            words = np.frombuffer(self._read(voff, vcount * 4), dtype="<u4")
            dx = (words & _DX_MASK).astype(np.int32)
            dz = ((words >> _DZ_SHIFT) & _DZ_MASK).astype(np.int32)
            y = ((words >> _Y_SHIFT) & _Y_MASK).astype(np.int32)
            lpi = ((words >> _LPI_SHIFT) & _LPI_MASK).astype(np.int32)
            state = gmap[lpi]
            if not want_air:
                keep = ~self.air_flags[state]
                if not keep.all():
                    if not keep.any():
                        continue
                    dx, dz, y, state = dx[keep], dz[keep], y[keep], state[keep]
            yield {
                "chunkX": cx, "chunkZ": cz,
                "xyz": np.stack([cx * 16 + dx, y, cz * 16 + dz], axis=1),
                "state": state,
            }

    @property
    def air_flags(self):
        """bool array over palette indices: True where the block is air."""
        if not hasattr(self, "_air_flags"):
            self._air_flags = np.array(
                [nm in AIR_NAMES for (nm, _pk) in self.palette], dtype=bool)
        return self._air_flags


class ChunkCursor(object):
    """
    Random access to chunks by (chunkX, chunkZ), with an insertion-ordered cache.

    Built on VoxelFile but keeps decoded chunks around so that scanning
    neighbouring tiles does not re-decode the same chunk repeatedly.
    """

    def __init__(self, path, cache_chunks=256):
        self.vf = VoxelFile(path)
        self._cache = {}
        self._order = []
        self._cap = cache_chunks

    def close(self):
        self.vf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get(self, chunkX, chunkZ, want_air=False):
        """-> (x, y, z, state) int32 arrays, or None when the chunk is empty."""
        key = (chunkX, chunkZ)
        if key in self._cache:
            return self._cache[key]

        row_i = self.vf._row_index.get(key)
        out = None
        if row_i is not None:
            cx, cz, gmap, vcount, voff = self.vf._rows[row_i]
            if vcount:
                words = np.frombuffer(self.vf._read(voff, vcount * 4),
                                      dtype="<u4")
                dx = (words & _DX_MASK).astype(np.int32)
                dz = ((words >> _DZ_SHIFT) & _DZ_MASK).astype(np.int32)
                y = ((words >> _Y_SHIFT) & _Y_MASK).astype(np.int32)
                lpi = ((words >> _LPI_SHIFT) & _LPI_MASK).astype(np.int32)
                state = gmap[lpi]
                if not want_air:
                    keep = ~self.vf.air_flags[state]
                    if not keep.all():
                        if not keep.any():
                            dx = dz = y = state = np.empty(0, np.int32)
                        else:
                            dx, dz, y, state = (dx[keep], dz[keep],
                                                y[keep], state[keep])
                out = (cx * 16 + dx, y, cz * 16 + dz, state)

        if len(self._order) >= self._cap:
            self._cache.pop(self._order.pop(0), None)
        self._cache[key] = out
        self._order.append(key)
        return out


# --------------------------------------------------------------------------- #
# dense tile builder (bounded!)
# --------------------------------------------------------------------------- #

def build_dense_tile(cursor, cx0, cz0, nchunks, ymax, palette_list,
                     fill=0, dtype=np.uint8, remap=None):
    """
    Materialise a bounded (nchunks*16) x (ymax+1) x (nchunks*16) dense array.

    `remap` maps global palette index -> dense-array value (default identity).
    Used to squash 1146 block states into a handful of semantic classes.

    Memory: (nchunks*16)^2 * (ymax+1) bytes. With nchunks=16, ymax=127 that is
    256*256*128 = 8.4 MB -- safe. Never call this with the whole world.
    """
    W = nchunks * 16
    out = np.full((W, ymax + 1, W), fill, dtype=dtype)
    for cdx in range(nchunks):
        for cdz in range(nchunks):
            got = cursor.get(cx0 + cdx, cz0 + cdz)
            if got is None:
                continue
            xs, ys, zs, st = got
            if st.size == 0:
                continue
            lx = xs - cx0 * 16
            lz = zs - cz0 * 16
            vals = st if remap is None else remap[st]
            out[lx, ys, lz] = vals
    return out


# --------------------------------------------------------------------------- #
# metadata helpers
# --------------------------------------------------------------------------- #

def load_block_states(path):
    with open(path) as f:
        rows = json.load(f)
    out = []
    for r in rows:
        out.append((r["name"], r.get("properties", "")))
    return out


def load_material_manifest(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict):
        # tolerate a wrapper object
        for key in ("materials", "entries", "blocks"):
            if key in data:
                data = data[key]
                break
    return data


def world_bounds(path):
    """
    Tight (xmin, xmax, zmin, zmax, ymin, ymax) over non-air voxels.

    Computed from the voxel stream itself, not from the chunk table's grid
    extent. Deriving X/Z from chunk bounds would overstate the footprint (a
    chunk holding a single block still claims 16x16 columns) and would disagree
    with parse/stats.json, which tracks true voxel coordinates.
    """
    with VoxelFile(path) as vf:
        rows = vf._rows
        if not rows:
            return None
        xmin = ymin = zmin = None
        xmax = ymax = zmax = None
        for (_cx, _cz, _g, vcount, voff) in rows:
            if vcount == 0:
                continue
            words = np.frombuffer(vf._read(voff, vcount * 4), dtype="<u4")
            cx, cz = _cx, _cz
            lo_x = cx * 16 + int((words & _DX_MASK).min())
            hi_x = cx * 16 + int((words & _DX_MASK).max())
            lo_z = cz * 16 + int(((words >> _DZ_SHIFT) & _DZ_MASK).min())
            hi_z = cz * 16 + int(((words >> _DZ_SHIFT) & _DZ_MASK).max())
            ys = (words >> _Y_SHIFT) & _Y_MASK
            lo_y, hi_y = int(ys.min()), int(ys.max())
            xmin = lo_x if xmin is None else min(xmin, lo_x)
            xmax = hi_x if xmax is None else max(xmax, hi_x)
            zmin = lo_z if zmin is None else min(zmin, lo_z)
            zmax = hi_z if zmax is None else max(zmax, hi_z)
            ymin = lo_y if ymin is None else min(ymin, lo_y)
            ymax = hi_y if ymax is None else max(ymax, hi_y)
        return (xmin, xmax, zmin, zmax, ymin, ymax)
