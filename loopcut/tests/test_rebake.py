"""rebake.py and its wiring, without Blender: the schema the model sees, the files a call sends (the high-poly by
hash, never uploaded), finding the high-poly from the source job, check_shading's advice naming the tool, and the
corner matching an unwrapped result's UVs go through. harness/rebake_apply_check.py runs the rest in Blender on SPLAP.
numpy ships with Blender, not with every Python: the matching test skips without it."""

import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import blobs, cloud, cloud_http, cloud_jobs, rebake, shading  # noqa: E402

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
HIGH = "4e" * 32
PROJECT = {"id": "unit-test", "no_generation": False}
DESCRIPTOR = {
    "name": "rebake", "namespace": "loopcut_cloud", "version": 1, "title": "Re-bake textures", "kind": "assist",
    "backend": "managed", "apply": "import_model", "latency": "minutes",
    "description": ("Bakes fresh base colour, normal, roughness and metallic maps from a model's original high-poly "
                    "source onto its current low-poly mesh. files.high is the source job's kept high-poly: the sha256 "
                    "of the retopology job's files.model. Processing usually takes under two minutes. It usually "
                    "costs 5 credits, never more than the quote. Applying the result imports the low-poly."),
    "input_schema": {"type": "object", "properties": {
        "texture_size": {"type": "integer", "enum": [1024, 2048]}, "keep_uvs": {"type": "boolean"}},
        "required": [], "additionalProperties": False},
    "file_inputs": [{"field": "high", "accept": ["model/gltf-binary"], "max_mb": 150, "required": True},
                    {"field": "model", "accept": ["model/gltf-binary", "model/vnd.fbx"], "max_mb": 50,
                     "required": True}],
    "cost": {"credits_estimate": 5, "quote_required": False}}


class Reply(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(json.dumps(data).encode() if isinstance(data, dict) else data)
        self.status, self.headers = status, headers or {}


def job_view(job_id, tool, files=None, status="succeeded"):
    value = {"id": job_id, "tool": tool, "status": status, "progress": {"pct": 100, "message": "Done"},
             "credits": {"reserved": 5, "charged": 5, "refunded": 0}, "outputs": [], "error": None,
             "provenance": {"job_id": job_id, "tool": tool, "vendor": "gcp_runner", "model": "test", "kind": "assist",
                            "created_at": "2026-10-31T00:00:00Z"}}
    if files is not None:
        value["files"] = files
    return value


class Service:
    """The job HTTP layer: blobs it keeps, uploads, job views, and the job a create returns."""

    def __init__(self, kept=(), views=None):
        self.kept, self.views, self.posts, self.uploaded, self.checked = set(kept), dict(views or {}), [], [], []

    def __call__(self, request, *, timeout):
        url = request.full_url
        if url.endswith("/blobs/check"):
            asked = json.loads(request.data)["sha256"]
            self.checked += asked
            return Reply({"missing": [h for h in asked if h not in self.kept]})
        if url.endswith("/blobs/upload-urls"):
            items = json.loads(request.data)["items"]
            self.uploaded += [(item["sha256"], item["mime"]) for item in items]
            return Reply({"items": [{"sha256": item["sha256"], "put_url": "https://storage.test/upload",
                                     "expires_at": "2099-01-01T00:00:00Z"} for item in items]})
        if request.method == "PUT":
            request.data.read()
            return Reply(b"")
        if url.endswith("/v1/jobs"):
            payload = json.loads(request.data)
            self.posts.append(payload)
            return Reply(job_view("job_rebake_1", "rebake", payload["files"], status="failed")
                         | {"error": {"code": "vendor_failed", "message": "stop here", "type": "vendor"}})
        for job_id, view in self.views.items():
            if url.endswith("/v1/jobs/" + job_id):
                return Reply(view) if view else Reply({"error": {"code": "not_found", "message": "Not found.",
                                                                 "type": "invalid_request"}}, status=404)
        raise AssertionError(url)

    def client(self):
        return cloud_http.Client(base_url="https://service.test", api_key="lc_test", transport=self,
                                 client_headers={"x-loopcut-contract": "0.11", "x-loopcut-client": "test"})


class SchemaTest(unittest.TestCase):
    def test_the_model_names_an_object_and_loopcut_fills_the_files(self):
        schema = cloud.schema(DESCRIPTOR)["function"]
        properties, required = schema["parameters"]["properties"], schema["parameters"]["required"]
        self.assertEqual(schema["name"], "loopcut_cloud__rebake")
        self.assertNotIn("high", properties)
        self.assertEqual(properties["model"], {"type": "string", "description": rebake.MODEL_TEXT})
        self.assertNotIn("model", required)
        self.assertNotIn("sha256", schema["description"])
        for words in ("in place", "check_shading", "under two minutes", "5 credits, never more than the quote"):
            self.assertIn(words, schema["description"])
        self.assertNotIn("imports the low-poly", schema["description"])
        # What it costs on every step the catalog offers it.
        print(f"\nrebake schema: {len(json.dumps(schema))} characters, about "
              f"{round(len(json.dumps(schema)) / 3.4)} tokens", file=sys.stderr)

    def test_arguments_need_no_files_and_refuse_a_hash(self):
        cloud._validate_arguments(DESCRIPTOR, {})
        cloud._validate_arguments(DESCRIPTOR, {"model": "Muse", "keep_uvs": False})
        with self.assertRaises(cloud._InputMistake):
            cloud._validate_arguments(DESCRIPTOR, {"high": HIGH})

    def test_other_tools_keep_their_file_fields(self):
        other = {**DESCRIPTOR, "name": "retopology"}
        self.assertIn("high", cloud.schema(other)["function"]["parameters"]["properties"])
        with self.assertRaises(cloud._InputMistake):
            cloud._validate_arguments(other, {})


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        cloud_jobs._registry.clear()
        redraw = patch.object(cloud_jobs, "_redraw")
        redraw.start()
        self.addCleanup(redraw.stop)
        self.model = self.root / "lowpoly.fbx"
        self.model.write_bytes(b"Kaydara FBX Binary  \x00" + b"\x01" * 64)

    def test_the_high_poly_goes_by_hash_and_only_the_low_poly_is_uploaded(self):
        service = Service(kept={HIGH})
        files = {"high": blobs.Stored(HIGH), "model": self.model}
        with self.assertRaises(cloud_http.CloudError):  # The fake service fails the job once it has the request.
            cloud_jobs.run(DESCRIPTOR, {"model": "Muse"}, files, project=PROJECT, client=service.client())
        sent = service.posts[0]
        self.assertEqual(sent["files"], {"high": HIGH, "model": hashlib.sha256(self.model.read_bytes()).hexdigest()})
        self.assertEqual(sent["input"], {})
        self.assertEqual(service.uploaded, [(sent["files"]["model"], "model/vnd.fbx")])

    def test_a_stored_file_without_its_hash_is_refused_before_anything_is_sent(self):
        service = Service()
        with self.assertRaises(cloud_http.CloudError) as caught:
            cloud_jobs.run(DESCRIPTOR, {}, {"high": blobs.Stored(job="job_retopo"), "model": self.model},
                           project=PROJECT, client=service.client())
        self.assertEqual(caught.exception.extra.get("field"), "high")
        self.assertEqual(service.posts, [])

    def test_the_high_poly_is_read_from_its_source_job_and_recorded(self):
        service = Service(kept={HIGH}, views={"job_retopo": job_view("job_retopo", "retopology",
                                                                     {"model": HIGH})})
        recorded = []
        stored = blobs.Stored(job="job_retopo", found=recorded.append)
        cloud_jobs.resolve_stored({"high": stored, "model": self.model}, service.client(),
                                  on_main=lambda fn: fn())
        self.assertEqual((stored.sha256, recorded, service.checked), (HIGH, [HIGH], [HIGH]))
        stored = blobs.Stored(job="job_rebake_0")  # A re-bake's source is its own files.high.
        service.views["job_rebake_0"] = job_view("job_rebake_0", "rebake", {"high": HIGH, "model": "ab" * 32})
        cloud_jobs.resolve_stored({"high": stored}, service.client())
        self.assertEqual(stored.sha256, HIGH)

    def test_refusals_are_plain_words(self):
        cases = [(Service(views={"job_gone": None}), "job_gone", rebake.SOURCE_JOB_GONE.format(job="job_gone")),
                 (Service(views={"job_old": job_view("job_old", "retopology")}), "job_old",
                  rebake.SOURCE_NOT_LISTED.format(job="job_old")),
                 (Service(views={"job_x": job_view("job_x", "retopology", {"model": HIGH})}), "job_x",
                  rebake.SOURCE_GONE)]
        for service, job_id, message in cases:
            with self.subTest(job=job_id), self.assertRaises(cloud_http.CloudError) as caught:
                cloud_jobs.resolve_stored({"high": blobs.Stored(job=job_id)}, service.client())
            self.assertEqual((str(caught.exception), caught.exception.code), (message, "invalid_input"))

    def test_a_kept_hash_is_checked_once_and_never_looked_up(self):
        service = Service(kept={HIGH})
        cloud_jobs.resolve_stored({"high": blobs.Stored(HIGH), "model": self.model}, service.client())
        self.assertEqual(service.checked, [HIGH])


class AdviceTest(unittest.TestCase):
    FACT = {"faces": 100, "flat": 0.97, "shape": "organic", "edges": 200}

    def test_the_fix_names_the_tool_when_offered_and_the_mesh_has_a_source(self):
        record = {"job": "job_retopo", "tool": "retopology", "maps": ["n.png"]}
        said = shading.verdict("Head", self.FACT, custom=True, maps=["retopo_x_normal"], record=record,
                               changed=["shape"], rebake="loopcut_cloud__rebake")
        self.assertTrue(all("Fix: loopcut_cloud__rebake with model='Head'" in p for p in said["problems"]), said)
        self.assertTrue(all("source job job_retopo" in p for p in said["problems"]))

    def test_no_tool_name_without_a_source_or_an_offer(self):
        for record, tool in (({"job": "job_gen", "tool": "image_to_3d", "maps": ["n.png"]}, "loopcut_cloud__rebake"),
                             ({"job": "job_retopo", "tool": "retopology", "maps": ["n.png"]}, "")):
            said = shading.verdict("Head", self.FACT, custom=True, maps=["n.png"], record=record, changed=["shape"],
                                   rebake=tool)
            self.assertFalse(any("loopcut_cloud__rebake" in p for p in said["problems"]), said)
            self.assertTrue(any("Re-bake its maps from the source model" in p for p in said["problems"]))
        self.assertTrue(shading.rebaked_from({"high_sha256": HIGH}))


@unittest.skipUnless(HAS_NUMPY, "numpy")
class MatchTest(unittest.TestCase):
    """match_corners on a grid of quads and triangles, the returned copy reordered and its corners rotated."""

    def setUp(self):
        import numpy as np

        class Tree:  # mathutils.kdtree.KDTree, brute force.
            def __init__(self, size):
                self.points = []

            def insert(self, point, index):
                self.points.append((index, np.array(point)))

            def balance(self):
                pass

            def find(self, point):
                index, best = min(self.points, key=lambda row: float(np.linalg.norm(row[1] - point)))
                return best, index, float(np.linalg.norm(best - np.array(point)))
        module = types.ModuleType("mathutils.kdtree")
        module.KDTree = Tree
        context = patch.dict(sys.modules, {"mathutils": types.ModuleType("mathutils"), "mathutils.kdtree": module})
        context.start()
        self.addCleanup(context.stop)

    def mesh(self):
        import numpy as np
        faces = []
        for i in range(3):
            for j in range(3):
                a, b, c, d = (i, j, 0), (i + 1, j, 0), (i + 1, j + 1, 0), (i, j + 1, 0)
                faces += [[a, b, c, d]] if (i + j) % 2 else [[a, b, c], [a, c, d]]
        totals = np.array([len(f) for f in faces])
        starts = np.concatenate([[0], np.cumsum(totals)[:-1]])
        points = np.array([p for f in faces for p in f], dtype=float)
        return points, starts, totals

    def test_each_corner_takes_the_uv_of_the_same_corner_returned(self):
        import numpy as np
        points, starts, totals = self.mesh()
        order = np.arange(len(totals))[::-1]  # Faces in another order, each one's corners rotated by one.
        their_points, their_totals, their_uvs, their_starts = [], [], [], []
        for face in order:
            corners = list(range(starts[face], starts[face] + totals[face]))
            corners = corners[1:] + corners[:1]
            their_starts.append(len(their_points))
            their_totals.append(totals[face])
            for corner in corners:
                their_points.append(points[corner])
                their_uvs.append(points[corner][:2] / 3 + corner * 1e-3)  # Unique per corner: seams too.
        uvs, why = rebake.match_corners(points, starts, totals, np.array(their_points), np.array(their_starts),
                                    np.array(their_totals), np.array(their_uvs), 1e-4)
        self.assertIsNone(why)
        expected = points[:, :2] / 3 + np.arange(len(points))[:, None] * 1e-3
        self.assertTrue(np.allclose(uvs, expected), (uvs, expected))

    def test_faces_that_do_not_line_up_are_refused(self):
        import numpy as np
        points, starts, totals = self.mesh()
        uvs = np.zeros((len(points), 2))
        _, why = rebake.match_corners(points, starts, totals, points + 0.1, starts, totals, uvs, 1e-4)
        self.assertIn("no longer line up", why)
        _, why = rebake.match_corners(points, starts, totals, points[:-3], starts[:-1], totals[:-1], uvs[:-3], 1e-4)
        self.assertIn("faces here", why)


if __name__ == "__main__":
    unittest.main()
