# -*- coding: utf-8 -*-
"""
test_extract_structures.py -- correctness tests for the S3/S4 mesher.

The mesher's failure modes are all silent. A flipped winding, an off-by-one
face plane, or a UV that straddles two atlas cells produces a file that imports
cleanly, reports a plausible triangle count, and renders inside-out or
mis-textured. Nothing errors. So the properties are asserted here rather than
eyeballed in the viewport.

Run:  python3 tools/test_extract_structures.py
"""

import json
import os
import sys
import unittest
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import extract_structures as E                          # noqa: E402
from build_atlas import (ATLAS_DEFAULTS, ATLAS_FAMILIES,  # noqa: E402
                         plan_layout)

LAYOUT = plan_layout(ATLAS_FAMILIES, **ATLAS_DEFAULTS)


def mesh(solid, mat=None, cap=4, offset=(0, 0)):
    if mat is None:
        mat = solid.astype(np.uint8)
    return E.mesh_volume(solid, solid, mat, cap, offset)


def read_obj(path):
    verts, norms, uvs, faces, mats = [], [], [], [], []
    cur = None
    for line in open(path):
        if line.startswith("v "):
            verts.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("vt "):
            uvs.append([float(x) for x in line.split()[1:4][:2]])
        elif line.startswith("vn "):
            norms.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("usemtl "):
            cur = line.split()[1]
        elif line.startswith("f "):
            faces.append([int(t.split("/")[0]) - 1 for t in line.split()[1:]])
            mats.append(cur)
    return (np.array(verts), np.array(norms), np.array(uvs),
            np.array(faces), mats)


class TestFaceCulling(unittest.TestCase):
    def test_single_voxel_is_six_quads(self):
        s = np.zeros((3, 3, 3), bool)
        s[1, 1, 1] = True
        _c, _uv, _n, quads = mesh(s)
        self.assertEqual(len(quads), 6)

    def test_no_interior_faces(self):
        """A solid 4x4x4 cube: only the 6 outer faces, never the interior."""
        s = np.zeros((6, 6, 6), bool)
        s[1:5, 1:5, 1:5] = True
        _c, _uv, _n, quads = mesh(s, cap=8)
        # 6 faces, each merged into a single 4x4 quad.
        self.assertEqual(len(quads), 6)
        self.assertEqual(sum(q[2] * q[3] for q in quads), 6 * 16)

    def test_two_touching_voxels_share_no_face(self):
        """Two voxels in a row: 2 end caps + 4 merged sides = 6 quads.

        Naively it is 12 (6 each) minus the 2 shared faces, and greedy merging
        then collapses each 2x1 side into one quad. Getting 6 rather than 8 is
        the proof that the shared faces really were cancelled *and* that the
        merge ran.
        """
        s = np.zeros((4, 3, 3), bool)
        s[1, 1, 1] = True
        s[2, 1, 1] = True
        _c, _uv, _n, quads = mesh(s, cap=8)
        self.assertEqual(len(quads), 6)
        self.assertEqual(sum(q[2] * q[3] for q in quads), 4 * 2 + 2 * 1)

    def test_hollow_shell_emits_inner_faces(self):
        """A 3x3x3 shell has a cavity, so it has an inside face too."""
        s = np.zeros((5, 5, 5), bool)
        s[1:4, 1:4, 1:4] = True
        s[2, 2, 2] = False
        _c, _uv, _n, quads = mesh(s)
        # Outer 6 + the cavity's 6.
        self.assertEqual(len(quads), 12)


class TestWinding(unittest.TestCase):
    def _assert_outward(self, solid, cap=8):
        corners, _uv, normals, quads = mesh(solid, cap=cap)
        for base, _slot, _w, _h in quads:
            p0, p1, p2 = corners[base:base + 3]
            cr = np.cross(p1 - p0, p2 - p0)
            self.assertGreater(np.dot(cr, normals[base]), 0,
                               "winding disagrees with the stored normal")

    def _assert_volume(self, solid, expected_m3, cap=8):
        """Signed volume must equal the voxel count, in both triangles.

        This is the strongest orientation invariant available here, and unlike
        edge pairing it survives greedy meshing. Greedy merging emits one quad
        per rectangle, so where a long edge on one face meets two shorter
        collinear edges on the next there is a **T-junction** with no matching
        reverse edge. Those are not inversions -- the geometry is still closed
        and still correctly wound -- but they mean "every edge has an exact
        reverse" is simply the wrong assertion for a greedy mesher, and a test
        built on it reports a healthy mesh as broken.

        A flipped face anywhere makes the volume come out wrong, and summing one
        triangle per quad instead of both makes it come out exactly half, which
        is a plausible-looking wrong answer rather than an obvious one.
        """
        corners, _uv, _n, quads = mesh(solid, cap=cap)
        vol = 0.0
        for base, _s, _w, _h in quads:
            a, b, c = corners[base], corners[base + 1], corners[base + 2]
            vol += np.dot(a, np.cross(b, c)) / 6.0
            a, c, d = corners[base], corners[base + 2], corners[base + 3]
            vol += np.dot(a, np.cross(c, d)) / 6.0
        self.assertAlmostEqual(vol / 1e6, float(expected_m3), places=3,
                               msg="signed volume %.3f m3, expected %s m3"
                                   % (vol / 1e6, expected_m3))

    def test_cube(self):
        s = np.zeros((5, 5, 5), bool)
        s[1:4, 1:4, 1:4] = True
        self._assert_outward(s)
        self._assert_volume(s, 27)

    def test_shell(self):
        s = np.zeros((5, 5, 5), bool)
        s[1:4, 1:4, 1:4] = True
        s[2, 2, 2] = False
        self._assert_outward(s)
        # 27 voxels minus the 1-voxel cavity.
        self._assert_volume(s, 26)

    def test_l_shape(self):
        """An asymmetric shape: a symmetric test set cannot catch a flipped
        in-plane axis, because the error is symmetric about the diagonal."""
        s = np.zeros((6, 6, 6), bool)
        s[1:5, 1:3, 1:5] = True
        s[1:3, 3:5, 1:5] = True
        self._assert_outward(s)
        # 4x2x4 slab plus a 2x2x4 arm.
        self._assert_volume(s, 32 + 16)

    def test_parity_matches_axes(self):
        """The winding decision must follow the axis parity, not a constant."""
        for d in range(3):
            other = [a for a in range(3) if a != d]
            parity = E.hand_parity([other[0], other[1], d])
            self.assertIn(parity, (-1, 1))
        self.assertEqual(E.hand_parity([0, 1, 2]), 1)
        self.assertEqual(E.hand_parity([1, 2, 0]), 1)
        self.assertEqual(E.hand_parity([0, 2, 1]), -1)


class TestGeometry(unittest.TestCase):
    def test_closed_cube_volume(self):
        """Signed volume of a closed 4x4x4 cube = 64 m^3.

        This is the only test that catches a *systematically* wrong face plane:
        a mesh can be consistently wound and still enclose the wrong volume.
        """
        s = np.zeros((6, 6, 6), bool)
        s[1:5, 1:5, 1:5] = True
        corners, _uv, _n, quads = mesh(s, cap=8)
        vol = 0.0
        for base, _s, _w, _h in quads:
            # Both triangles of the quad. Summing one gives exactly half, which
            # is a real trap: it looks like a plausible wrong answer.
            a, b, c = corners[base], corners[base + 1], corners[base + 2]
            vol += np.dot(a, np.cross(b, c)) / 6.0
            a, c, d = corners[base], corners[base + 2], corners[base + 3]
            vol += np.dot(a, np.cross(c, d)) / 6.0
        self.assertAlmostEqual(vol / 1e6, 64.0, places=3)

    def test_face_plane_position(self):
        """A single voxel's faces sit exactly on its cube's boundary planes."""
        s = np.zeros((3, 3, 3), bool)
        s[1, 1, 1] = True
        corners, _uv, normals, quads = mesh(s)
        seen = {}
        for base, _slot, _w, _h in quads:
            n = normals[base]
            axis = int(np.argmax(np.abs(n)))
            sign = int(np.sign(n[axis]))
            seen[(axis, sign)] = corners[base][axis]
        # Voxel (1,1,1) spans block 1..2, i.e. 100..200 cm on each axis.
        self.assertEqual(seen[(0, -1)], 100.0)
        self.assertEqual(seen[(0, +1)], 200.0)
        self.assertEqual(seen[(1, -1)], 100.0)
        self.assertEqual(seen[(1, +1)], 200.0)
        self.assertEqual(seen[(2, -1)], 100.0)
        self.assertEqual(seen[(2, +1)], 200.0)

    def test_axis_mapping(self):
        """Minecraft +z must be Unreal +Y, and block height must be Unreal Z."""
        self.assertEqual(E.MC_TO_UE, (0, 2, 1))
        s = np.zeros((3, 3, 3), bool)
        s[1, 1, 1] = True
        corners, _uv, normals, _q = mesh(s, offset=(10, 20))
        # x -> UE X (carrying the +10 block offset), y height -> UE Z (no
        # offset), z -> UE Y (carrying the +20 block offset). Voxel 1 spans one
        # block either side of its origin, so x = (1..2 + 10) * 100 cm.
        self.assertEqual(sorted(set(corners[:, 0])), [1100.0, 1200.0])
        self.assertEqual(sorted(set(corners[:, 1])), [2100.0, 2200.0])
        self.assertEqual(sorted(set(corners[:, 2])), [100.0, 200.0])

    def test_world_offset(self):
        s = np.zeros((3, 3, 3), bool)
        s[1, 1, 1] = True
        a, _uv, _n, _q = mesh(s, offset=(0, 0))
        b, _uv, _n, _q = mesh(s, offset=(100, -50))
        # The offset is a block coordinate, so +100 blocks on x is +10,000 cm
        # on UE X, and -50 blocks on z is -5,000 cm on UE Y.
        np.testing.assert_allclose(b - a,
                                   np.tile([10000.0, -5000.0, 0.0],
                                           (len(a), 1)))


class TestGreedyMerging(unittest.TestCase):
    def test_cap_controls_merge(self):
        s = np.zeros((9, 3, 3), bool)
        s[1:8, 1, 1] = True
        counts = {cap: len(mesh(s, cap=cap)[3]) for cap in (1, 2, 4, 8)}
        self.assertGreater(counts[1], counts[2])
        self.assertGreaterEqual(counts[2], counts[4])
        # A 7-long bar caps out at 2 merged faces plus the end caps.
        self.assertLessEqual(counts[8], 8)

    def test_merge_respects_material(self):
        """Two materials side by side must not merge into one quad.

        This is the atlas-correctness test: a merged quad spanning two cells
        would stretch one texture over both, and the seam is invisible in a
        triangle count.
        """
        s = np.zeros((5, 4, 4), bool)
        s[1:4, 1, 1] = True
        mat = np.zeros((5, 4, 4), np.uint8)
        mat[1:4, 1, 1] = 1
        mat[2, 1, 1] = 2
        _c, _uv, _n, quads = mesh(s, mat=mat, cap=8)
        # The odd material out cannot merge with its neighbours on either side.
        self.assertGreaterEqual(len(quads), 4)

    def test_merge_is_greedy_not_maximal_rectangles(self):
        """An L must not be covered by two overlapping rectangles."""
        s = np.zeros((5, 5, 5), bool)
        s[1:4, 1, 1:4] = True
        s[3, 2:4, 1:4] = True
        _c, _uv, _n, quads = mesh(s, cap=8)
        # 2 faces of 3x1 + 2 faces of 1x3 + the elbow's 2 single faces.
        self.assertGreater(len(quads), 4)


class TestAtlasUVs(unittest.TestCase):
    def test_uvs_stay_inside_their_cell(self):
        """Every UV must land in exactly one atlas cell.

        The failure this catches is subtle and expensive: a UV a texel outside
        its cell samples the neighbour family, so a wall of 'quartz' quietly
        gains a stripe of 'brick' along one edge, at every mip level.
        """
        s = np.zeros((9, 6, 6), bool)
        s[1:8, 1:5, 1:5] = True
        mat = np.zeros((9, 6, 6), np.uint8)
        mat[1:8, 1:5, 1:5] = 1
        corners, uvs, normals, quads = mesh(s, mat=mat, cap=4)
        scaled = E.scale_uvs(uvs, quads, LAYOUT)
        for base, slot, _w, _h in quads:
            fam = ATLAS_FAMILIES[slot - 1]
            u0, v0, u1, v1 = LAYOUT[fam]["uv"]
            for k in range(4):
                u, v = scaled[base + k]
                self.assertGreaterEqual(u, u0 - 1e-9)
                self.assertLessEqual(u, u1 + 1e-9)
                self.assertGreaterEqual(v, v0 - 1e-9)
                self.assertLessEqual(v, v1 + 1e-9)

    def test_every_family_has_a_cell(self):
        for fam in ATLAS_FAMILIES:
            self.assertIn(fam, LAYOUT)
            u0, v0, u1, v1 = LAYOUT[fam]["uv"]
            self.assertLess(u0, u1)
            self.assertLess(v0, v1)

    def test_cells_do_not_overlap(self):
        seen = {}
        for fam in ATLAS_FAMILIES:
            px = LAYOUT[fam]["px"]
            for x in range(px[0], px[2]):
                for y in range(px[1], px[3]):
                    key = (x, y)
                    self.assertNotIn(key, seen,
                                     "cell overlap at %s: %s and %s"
                                     % (key, seen.get(key), fam))
                    seen[key] = fam

    def test_atlas_is_power_of_two(self):
        """Non-POT atlases cross-contaminate families under mip reduction."""
        a = LAYOUT["_atlas"]
        for dim in (a["width"], a["height"]):
            self.assertEqual(dim & (dim - 1), 0,
                             "atlas dimension %d is not a power of two" % dim)


class TestMaterialVolume(unittest.TestCase):
    """The voxel -> family map, checked for internal consistency.

    These exist because of a specific bug: the block name was resolved only on
    the first chunk that used a given palette index, so on every later chunk the
    name leaked from the previous iteration. A voxel's `nameid` and its `family`
    then came from two different blocks and nothing detected it -- the water
    layer rendered with brick, rock and wood while still being *called* water.
    Asserting that name and family agree catches that class of bug without
    needing to know the right answer for every block.
    """

    CACHE = "out/materials/voxelmat.npz"

    def setUp(self):
        if not os.path.isfile(self.CACHE):
            self.skipTest("run tools/extract_structures.py first")
        self.z = np.load(self.CACHE, allow_pickle=False)
        self.names = [str(s) for s in self.z["names"]]

    def test_family_matches_name_for_every_voxel(self):
        nameid, fam = self.z["nameid"], self.z["family"]
        solid = fam != 0
        # Map every distinct (nameid, family) pair actually present.
        pairs = np.unique(np.stack([nameid[solid], fam[solid]], axis=1),
                          axis=0)
        self.assertGreater(len(pairs), 50, "suspiciously few name/family pairs")
        bad = []
        for nid, f in pairs:
            expected = E.family_of(self.names[int(nid)])
            # Slots are numbered over SLOT_NAMES (atlas families + colour
            # variants), not over ATLAS_FAMILIES: a dyed block is tagged with
            # its variant name, so reading the slot through the atlas list
            # would both mislabel it and hide the variant entirely.
            if E.SLOT_NAMES[int(f) - 1] != expected:
                bad.append((self.names[int(nid)], E.SLOT_NAMES[int(f) - 1],
                            expected))
        self.assertEqual(bad, [],
                         "%d block names carry the wrong material family: %s"
                         % (len(bad), bad[:8]))

    def test_generated_families_are_reachable(self):
        """The two generated cells must be reachable, or they are dead weight.

        `path` and `asphalt` legitimately go unsampled -- this save has no
        dirt_path or asphalt blocks -- so requiring every family to appear would
        be asserting something untrue about the data. The generated cells are
        different: they exist only because the mesh needs them, so if nothing
        maps to one it is either a bug or a cell that should be dropped.
        """
        used = {ATLAS_FAMILIES[int(f) - 1]
                for f in np.unique(self.z["family"]) if f}
        for fam in ("other", "water"):
            self.assertIn(fam, used,
                          "generated family %r is never sampled" % fam)

    def test_water_voxels_are_the_water_family(self):
        role_path = "out/classify/rolevolume.npy"
        if not os.path.isfile(role_path):
            self.skipTest("no role volume")
        role = np.load(role_path)
        w = role == E.WATER
        self.assertGreater(int(w.sum()), 0)
        fams = {ATLAS_FAMILIES[int(f) - 1] for f in np.unique(self.z["family"][w])}
        self.assertEqual(fams, {"water"})


class TestOutputFiles(unittest.TestCase):
    """Checks against the real emitted files, skipped if not yet built."""

    MANIFEST = "out/structures/manifest.json"

    def setUp(self):
        if not os.path.isfile(self.MANIFEST):
            self.skipTest("run tools/extract_structures.py first")
        import json
        self.man = json.load(open(self.MANIFEST))

    def test_bbox_matches_structures_json(self):
        bad = [b["id"] for b in self.man["buildings"]
               if not b["bbox_matches_structures_json"]]
        self.assertEqual(bad, [],
                         "buildings whose mesh bbox moved: %s" % bad)

    def test_every_obj_has_vertex_normals(self):
        """The v/vt/vn triple is mandatory.

        reimport_terrain.py records the cost of omitting it: the importer
        silently discarded ~90% of the geometry.
        """
        for b in self.man["buildings"]:
            for part in b["meshes"].values():
                path = os.path.join("out/structures", part["obj"])
                if not os.path.isfile(path):
                    continue
                # vt/vn/usemtl come after every vertex, so a mesh with more
                # than a few thousand vertices pushes them well past any prefix
                # worth reading. Count the records instead of sampling -- and
                # report counts, never file contents, so a failure does not
                # dump a megabyte of vertices into the log.
                counts = {"v": 0, "vt": 0, "vn": 0, "f": 0, "usemtl": 0}
                with open(path) as fh:
                    for line in fh:
                        tag = line.split(" ", 1)[0]
                        if tag in counts:
                            counts[tag] += 1
                self.assertGreater(counts["v"], 0, path)
                self.assertEqual(counts["vt"], counts["v"], path)
                self.assertEqual(counts["vn"], counts["v"], path)
                self.assertEqual(counts["f"], counts["v"] // 4 * 2, path)
                self.assertGreater(counts["usemtl"], 0,
                                   "%s has faces but no material" % path)

    def test_structure_and_detail_partition_the_buildings(self):
        """The 88 buildings partition exactly; the rest is reported, not lost."""
        blk = self.man["blocks"]
        self.assertEqual(blk["structure"] + blk["detail"],
                         blk["in_reported_buildings"])
        self.assertEqual(blk["in_reported_buildings"]
                         + blk["unclaimed_fragments"], blk["labelled_total"])

    def test_triangle_reduction_is_real(self):
        t = self.man["triangles"]
        self.assertLess(t["all_outputs"], t["naive_12_per_block_same_voxels"])
        self.assertLess(t["all_outputs"], t["old_cube_layer"])

    def test_water_is_emitted(self):
        self.assertIsNotNone(self.man["water"]["obj"])
        self.assertGreater(self.man["water"]["blocks"], 0)

    def test_water_layer_uses_only_the_water_family(self):
        """A water face textured with brick is the stale-cache bug, visible.

        `build_material_volume` caches the voxel->family map. When a mapping
        rule changes and the cache is not invalidated, the old map keeps being
        served and every run still reports success -- the water layer came out
        carrying brick, rock and wood because of exactly that. One assertion on
        the emitted material list turns a silent wrong answer into a failure.
        """
        self.assertEqual(self.man["water"]["material_slots"], ["water"])

    def test_material_cache_is_not_stale(self):
        """The cache must record the inputs that produced it."""
        cache = "out/materials/voxelmat.npz"
        if not os.path.isfile(cache):
            self.skipTest("no material cache")
        import numpy as np
        z = np.load(cache, allow_pickle=False)
        self.assertIn("stamp", z.files,
                      "cache has no stamp; it cannot know it is stale")
        stamp = json.loads(str(z["stamp"][0]))
        self.assertEqual(stamp["atlas_families"], ATLAS_FAMILIES)
        self.assertEqual(stamp["campus"], list(E.CAMPUS))


if __name__ == "__main__":
    unittest.main(verbosity=2)