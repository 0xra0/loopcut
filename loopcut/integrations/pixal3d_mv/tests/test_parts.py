"""Item 4 part labelling: cameras, 2D mask rules and the 2D -> 3D label transfer (no models, no GPU).
Run with: python -m unittest tests.test_parts -v"""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parts as p
import view_qa as q


def plate_with_boss():
    """A wide thin plate (the shell) and a small box mounted 1 cm in front of it (+Y faces the front camera)."""
    import trimesh
    plate = trimesh.creation.box(extents=(1.0, 0.1, 1.4))
    boss = trimesh.creation.box(extents=(0.2, 0.1, 0.2))
    boss.apply_translation((0.2, 0.11, -0.3))
    mesh = trimesh.util.concatenate([plate, boss])
    return mesh, len(plate.faces)


def label_maps(mesh, n_plate, size=256):
    """What a perfect detector would return: the boss painted as label 2 wherever the camera sees it."""
    v, f = np.asarray(mesh.vertices), np.asarray(mesh.faces)
    maps = {}
    for view in q.VIEWS:
        whole = q.project(v, f, *q.CAMERAS[view], size=size)
        labels = whole.astype(np.uint8)
        if view != "back":                                    # from behind the plate hides the boss
            # project() frames each call to its own input, so paint the boss through the whole mesh's
            # framing: project the mesh with the plate collapsed onto the boss.
            boss_only = v.copy()
            keep = np.zeros(len(v), bool)
            keep[np.unique(f[n_plate:])] = True
            boss_only[~keep] = v[keep].mean(0)
            frame = q.project(np.vstack([boss_only, v.min(0), v.max(0)]), f, *q.CAMERAS[view], size=size)
            labels[frame & whole] = 2
        maps[view] = labels
    return maps


class Cameras(unittest.TestCase):
    def test_forward_points_from_camera_into_scene(self):
        # File coords: front camera sits at +Y, left at +X, back at -Y, right at -X (see view_qa.CAMERAS).
        expected = {"front": (0, -1, 0), "left": (-1, 0, 0), "back": (0, 1, 0), "right": (1, 0, 0)}
        for view, forward in expected.items():
            self.assertEqual(tuple(p.camera_axes(q.CAMERAS[view])[2]), forward, view)

    def test_rays_cover_the_mesh_box(self):
        mesh, _ = plate_with_boss()
        v = np.asarray(mesh.vertices)
        alpha = np.zeros((100, 100), bool)
        alpha[10:90, 30:70] = True
        origins, forward, rows, cols = p.pixel_rays(alpha, v, q.CAMERAS["front"])
        self.assertEqual(len(origins), alpha.sum())
        self.assertAlmostEqual(origins[:, 2].min(), v[:, 2].min(), delta=0.02)   # top row reaches the top
        self.assertAlmostEqual(origins[:, 2].max(), v[:, 2].max(), delta=0.02)
        self.assertLess((origins @ forward).max(), (v @ forward).min())        # every ray starts before the mesh


class MaskRules(unittest.TestCase):
    def test_whole_object_and_off_object_boxes_are_dropped(self):
        alpha = np.zeros((100, 100), bool)
        alpha[10:90, 10:90] = True
        boxes = [[10, 10, 90, 90], [0, 0, 5, 5], [20, 20, 40, 40], [50, 50, 60, 60]]
        self.assertEqual(p.plausible(boxes, alpha, limit=4), [[20, 20, 40, 40], [50, 50, 60, 60]])
        self.assertEqual(p.plausible(boxes, alpha, limit=1), [[20, 20, 40, 40]])

    def test_smaller_part_wins_overlap(self):
        alpha = np.ones((10, 10), bool)
        big = np.zeros((10, 10), bool); big[2:8, 2:8] = True
        small = np.zeros((10, 10), bool); small[4:6, 4:6] = True
        labels = p.resolve(alpha, [(int(small.sum()), 3, small), (int(big.sum()), 2, big)])
        self.assertEqual(labels[5, 5], 3)
        self.assertEqual(labels[2, 2], 2)
        self.assertEqual(labels[0, 0], 1)


class Transfer(unittest.TestCase):
    def setUp(self):
        import trimesh
        self.mesh, self.n_plate = plate_with_boss()
        welded = self.mesh.copy()
        welded.merge_vertices()
        self.adjacency = welded.face_adjacency
        self.v, self.f = np.asarray(self.mesh.vertices), np.asarray(self.mesh.faces)
        self.trimesh = trimesh

    def test_boss_faces_get_the_part_and_plate_stays_shell(self):
        label, _ = p.face_labels(label_maps(self.mesh, self.n_plate), self.v, self.f, self.adjacency)
        self.assertTrue((label[self.n_plate:] == 2).all(), label[self.n_plate:])
        self.assertTrue((label[:self.n_plate] == 1).all(), label[:self.n_plate])

    def test_without_part_pixels_everything_is_shell(self):
        maps = {v: (m > 0).astype(np.uint8) for v, m in label_maps(self.mesh, self.n_plate).items()}
        label, _ = p.face_labels(maps, self.v, self.f, self.adjacency)
        self.assertTrue((label == 1).all())

    def test_unseen_faces_take_their_neighbours_label(self):
        adjacency = np.array([[0, 1], [1, 2], [2, 3]])
        self.assertEqual(p.propagate(np.array([2, -1, -1, 1]), adjacency).tolist()[:2], [2, 2])
        self.assertEqual(p.propagate(np.array([-1, -1]), np.zeros((0, 2), int)).tolist(), [1, 1])

    def test_small_part_patch_inside_shell_is_absorbed(self):
        # a strip of 10 faces: one stray part face in the middle of shell
        adjacency = np.array([[i, i + 1] for i in range(9)])
        label = np.ones(10, int)
        label[5] = 3
        self.assertTrue((p.absorb_islands(label, adjacency, min_faces=3) == 1).all())
        label[3:8] = 3                                          # a real part (5 faces) survives
        self.assertEqual(int((p.absorb_islands(label, adjacency, min_faces=3) == 3).sum()), 5)


if __name__ == "__main__":
    unittest.main()
