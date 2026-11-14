"""scripts/harness/hx_floor.py (hx floor): one device's noise floor for one scene, measured as one bench job. Fakes only: ssh,
the job, the subprocesses, the game, the bridge and the journal are stand-ins, and the numbers and PNGs are synthetic; no
game, GPU or host is read. The in-job test takes the scene from the real parser but supplies the suite's bytes and its
load itself, because scenes.parse refuses a top-level [method] table (scenes.py, _known); the method is read from the
same bytes by verdict.load_method, which ignores the other tables.

- R1: run_argv is hx run --in-job's argv in the build's closure with the device's environment pairs in order; the client
  starts one bench job whose argv runs the in-job half, and refuses a bad method before any job starts; resolve refuses a
  build whose build.txt lacks a line or names another commit; launch returns a run's status and its id; correctness_argv
  is the argv that the in-job correctness parser reads back; the validation layer is used only when vulkaninfo lists it;
  correctness_run takes three screenshots, sixty presents apart, and records them with the run's files; the series plans
  A-B-B-A, launches each run once through its arm's closure, removes each game folder, and stops at the first run that
  does not pass; run_frames, run_log and run_shots read the record back and refuse a changed or missing file; a complete
  floor and a failed floor each write one record run that hx verify accepts, a complete one with its floor, interval,
  medians, image tolerance and determinism, and a void baseline fails it.
- R4: a hold pauses the series, and the seconds it waited show on the entry of the run it came before.

python -I -B tests/test_harness_floor.py
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
sys.path.insert(0, str(HARNESS))
import bridge
import config
import game
import hx_floor
import hx_run
import hx_verify
import jobs
import record
import runrecord
import scenes
import verdict

HOST = "example-host"
SHA = "3f9c2a1b7d4e6f80a1b2c3d4e5f60718293a4b5c"
SHA_OTHER = "9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a39281706"
CLOSURE = "/nix/store/q4kx1h7v3d2m9b6n8c5p0r1s2t3u4w5y-hookshot-1.0"
CLOSURE_OTHER = "/nix/store/z1x2c3v4b5n6m7q8w9e0r1t2y3u4i5o6-hookshot-1.0"
DEVICE = "gpu"
EXPECT = "Example GPU"
DEVICE_ENV = {"EXAMPLE_FLAG": "1"}
JOB_ID = "bench-20261113T000000Z-abcdef"
FRAMES = 60
HASHES = "%s  hookshot\n%s  hookshot_vk.so\n" % ("a" * 64, "b" * 64)

METHOD_TABLE = """[method]
confidence = 0.95
runs_per_arm = 5
warmup_seconds = 0
resamples = 200
seed = 7
"""
SCENES = """
[scenes.light]
kind = "light"
cache = "cold"
motion = "still"
save = "start"
route = [{ teleport = 0 }]
seconds = 1

[scenes.medium]
kind = "medium"
cache = "warm"
motion = "moving"
save = "start"
route = []
inputs = "tap"
seconds = 1

[scenes.dense]
kind = "dense"
cache = "cold"
motion = "still"
save = "start"
route = []
seconds = 1
held_out = true

[scenes.load]
kind = "load"
cache = "cold"
motion = "still"
save = "start"
route = []
load_to = 1
seconds = 1

[scenes.cutscene]
kind = "cutscene"
cache = "cold"
motion = "still"
save = "start"
route = [{ wait = 10 }]
seconds = 1

[scenes.soak]
kind = "soak"
cache = "cold"
motion = "still"
save = "start"
route = []
seconds = 1800
"""
SCENES_ONLY = SCENES.encode("utf-8")
SUITE_WITH_METHOD = (METHOD_TABLE + SCENES).encode("utf-8")
METHOD = verdict.load_method(SUITE_WITH_METHOD, "suite.toml")
PLAN = [("A", "timed"), ("B", "timed"), ("B", "timed"), ("A", "timed"), ("A", "timed"), ("B", "timed"),
        ("B", "timed"), ("A", "timed"), ("A", "timed"), ("B", "timed"), ("A", "correctness"), ("B", "correctness")]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(rows):
    """An 8-bit RGB PNG of rows, each a list of (r, g, b) tuples of one length"""
    height, width = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def solid(value):
    return png([[(value, value, value)] * 2 for _ in range(2)])


SHOT_A = solid(10)
SHOT_B = solid(200)


def frame_csv(values):
    """A frame log: the header, then frame n (from 1) with values[n - 1] as its frametime_ms"""
    lines = ["frame,time_s,frametime_ms"] + ["%d,%.4f,%.3f" % (n, n / 60, ms) for n, ms in enumerate(values, 1)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def timed_files(frame_ms=16.5, log=b"vk: ok\n"):
    """A run's files as hx run writes them, for a window of FRAMES frames each frame_ms long"""
    return {
        "log.txt": log,
        "frametimes.csv": frame_csv([frame_ms] * FRAMES),
        "scene.json": hx_run.dump({"window": {"map": "Area", "first": 1, "last": FRAMES}}),
        "metrics.json": hx_run.dump({}),
    }


def make_build(folder, sha_value=SHA):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "build.txt"), "w", encoding="utf-8") as f:
        f.write("closure: %s\nsha: %s\n" % (CLOSURE, sha_value))


def fake_copy(source, dest):
    """hx_run.copy_game's stand-in: the destination made, with the game's binary in it"""
    os.makedirs(dest)
    with open(os.path.join(dest, "hookshot"), "wb") as f:
        f.write(b"binary")


def run_quietly(fn, *args, **kwargs):
    """fn's result with its stdout and stderr captured: (result, stdout, stderr)"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


def env_pairs(argv):
    """The NAME=VALUE words of an argv's --env options, in order"""
    return [argv[i + 1] for i, word in enumerate(argv) if word == "--env"]


class RecordCase(unittest.TestCase):
    """A temporary harness root whose record root is root/results, and the lines the test's journal collects"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = str(self.base / "root")
        self.results = runrecord.results_dir(self.root)
        self.lines = []

    def patch(self, target, attribute, **kwargs):
        p = mock.patch.object(target, attribute, **kwargs)
        mocked = p.start()
        self.addCleanup(p.stop)
        return mocked

    def write_run(self, files, shots=None):
        facts = {key: {"value": key} for key in record.FACT_KEYS}
        run_id, _ = record.write_run(self.results, facts, files, shots or {}, journal=self.lines.append)
        return run_id

    def run_manifest(self, run_id):
        manifest, _ = record.read_manifest(self.results, run_id)
        return manifest

    def record_file(self, run_id, name):
        return (self.results / "runs" / run_id / "files" / name).read_bytes()

    def hx_verify_problems(self):
        problems, _ = hx_verify.check(self.results, record.parse_journal("\n".join(self.lines)))
        return problems


class ArgvTest(unittest.TestCase):
    def test_run_argv_is_hx_run_in_job_in_the_build_closure_with_the_env_pairs_in_their_order(self):
        argv = hx_floor.run_argv("/enter", "/nix/store/c", "/job/tools/scripts/harness/hx.py", "/job/runs/00", "/hold",
                                 "/root", "/builds/x", "gpu", "Example GPU", {"B": "2", "A": "1"}, "light")
        self.assertEqual(argv, ["/enter", "/nix/store/c", "python", "-I", "/job/tools/scripts/harness/hx.py", "run",
                                "--in-job", "/job/runs/00", "--hold", "/hold", "--root", "/root", "--build-folder",
                                "/builds/x", "--device", "gpu", "--expect", "Example GPU", "--env", "B=2", "--env",
                                "A=1", "--scene", "light"])

    def test_correctness_argv_is_the_in_job_floor_argv_in_the_build_closure_with_the_env_pairs_in_their_order(self):
        argv = hx_floor.correctness_argv("/enter", "/nix/store/c", "/job/tools/scripts/harness/hx.py", "/job/runs/00",
                                         "/root", "/builds/x", "gpu", "Example GPU", {"B": "2", "A": "1"}, "light")
        self.assertEqual(argv, ["/enter", "/nix/store/c", "python", "-I", "/job/tools/scripts/harness/hx.py", "floor",
                                "--correctness-in-job", "/job/runs/00", "--root", "/root", "--build-folder", "/builds/x",
                                "--device", "gpu", "--expect", "Example GPU", "--env", "B=2", "--env", "A=1",
                                "--scene", "light"])

    def test_correctness_argv_is_read_back_by_the_correctness_in_job_parser(self):
        argv = hx_floor.correctness_argv("/enter", "/nix/store/c", "/job/tools/scripts/harness/hx.py", "/job/runs/00",
                                         "/root", "/builds/x", "gpu", "Example GPU", {"B": "2", "A": "1"}, "light")
        a = hx_floor.parse_correctness_in_job(argv[argv.index("--correctness-in-job") + 1:])
        self.assertEqual((a.job, a.root, a.build_folder, a.device, a.expect, a.env, a.scene),
                         ("/job/runs/00", "/root", "/builds/x", "gpu", "Example GPU", [("B", "2"), ("A", "1")], "light"))

    def test_launch_returns_the_status_and_the_run_id_and_echoes_the_output(self):
        out = io.StringIO()
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"hx run: pass\nrun: 000007\nmanifest: x\n")
        self.assertEqual(hx_floor.launch(["cmd"], run=lambda argv, **kw: done, out=out), (0, "000007"))
        self.assertIn("run: 000007", out.getvalue())

    def test_launch_keeps_the_id_of_a_run_that_failed(self):
        done = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"hx run: FAIL wrong device\nrun: 000008\n")
        self.assertEqual(hx_floor.launch(["cmd"], run=lambda argv, **kw: done, out=io.StringIO()), (1, "000008"))

    def test_launch_without_a_run_line_has_no_id(self):
        done = subprocess.CompletedProcess(args=[], returncode=2, stdout=b"hx run: the run could not run\n")
        self.assertEqual(hx_floor.launch(["cmd"], run=lambda argv, **kw: done, out=io.StringIO()), (2, None))

    def test_launch_that_cannot_start_is_status_2_and_says_so(self):
        out = io.StringIO()

        def run(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory")

        self.assertEqual(hx_floor.launch(["/enter"], run=run, out=out), (2, None))
        self.assertIn("cannot run /enter", out.getvalue())


class ValidationTest(unittest.TestCase):
    def listing(self, stdout, returncode=0):
        done = subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)
        return hx_floor.validation_available(run=lambda argv, **kwargs: done)

    def test_a_listed_validation_layer_is_available(self):
        self.assertTrue(self.listing(b"Layers:\n\tVK_LAYER_KHRONOS_validation  1.3\n"))

    def test_a_vulkan_that_does_not_list_the_layer_is_not_available(self):
        self.assertFalse(self.listing(b"Layers:\n\tVK_LAYER_MESA_device_select\n"))

    def test_a_nonzero_exit_is_not_available_even_when_the_layer_is_named(self):
        self.assertFalse(self.listing(b"VK_LAYER_KHRONOS_validation\n", returncode=1))

    def test_a_missing_vulkaninfo_is_not_available(self):
        def run(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory")

        self.assertFalse(hx_floor.validation_available(run=run))


class ResolveTest(unittest.TestCase):
    cfg = config.Config(path="/cfg", host=HOST, hold="/hold", root="/root", dump="/dump", enter_closure="/enter",
                        devices={})

    def test_a_build_resolves_to_its_folder_sha_and_closure(self):
        with mock.patch.object(hx_floor.hx_smoke, "build_folder", return_value=("/root/builds/x", SHA)), \
                mock.patch.object(hx_floor.hx_smoke, "read_build", return_value={"sha": SHA, "closure": CLOSURE}):
            self.assertEqual(hx_floor.resolve(self.cfg, SHA), ("/root/builds/x", SHA, CLOSURE))

    def test_a_build_without_a_closure_line_is_refused(self):
        with mock.patch.object(hx_floor.hx_smoke, "build_folder", return_value=("/root/builds/x", None)), \
                mock.patch.object(hx_floor.hx_smoke, "read_build", return_value={"sha": SHA}):
            with self.assertRaisesRegex(hx_floor.FloorError, "closure"):
                hx_floor.resolve(self.cfg, None)

    def test_a_build_of_another_commit_than_the_one_asked_for_is_refused(self):
        with mock.patch.object(hx_floor.hx_smoke, "build_folder", return_value=("/root/builds/x", SHA)), \
                mock.patch.object(hx_floor.hx_smoke, "read_build", return_value={"sha": SHA_OTHER, "closure": CLOSURE}):
            with self.assertRaisesRegex(hx_floor.FloorError, "says sha"):
                hx_floor.resolve(self.cfg, SHA)

    def test_a_missing_build_is_refused_as_a_floor_error(self):
        with mock.patch.object(hx_floor.hx_smoke, "build_folder", side_effect=hx_floor.hx_smoke.SmokeError("no commit")):
            with self.assertRaisesRegex(hx_floor.FloorError, "no commit"):
                hx_floor.resolve(self.cfg, "nope")


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config(path="/cfg", host=HOST, hold="/hold", root="/root", dump="/dump",
                                 enter_closure="/enter",
                                 devices={DEVICE: config.Device(name=DEVICE, expect=EXPECT, env=DEVICE_ENV)})
        self.build_a = "/root/builds/" + SHA
        self.build_b = "/root/builds/" + SHA_OTHER

    def client(self, suite=SUITE_WITH_METHOD, start=None, follow=None, device=DEVICE, scene="light"):
        start = mock.Mock() if start is None else start
        follow = mock.Mock(return_value=0) if follow is None else follow
        suite_text = types.SimpleNamespace(stdout=suite.decode("utf-8"))
        with mock.patch.object(hx_floor.remote, "run", return_value=suite_text), \
                mock.patch.object(hx_floor.jobs, "new_id", return_value=JOB_ID):
            status, out, _ = run_quietly(hx_floor.client, "floor", self.cfg, device, scene,
                                         (self.build_a, CLOSURE), (self.build_b, CLOSURE_OTHER),
                                         start=start, follow=follow)
        return status, out, start, follow

    def test_the_job_runs_the_in_job_half_with_both_arms_the_device_and_the_scene(self):
        status, _, start, follow = self.client()
        job = "/root/jobs/" + JOB_ID
        self.assertEqual(start.call_args.args[:2], (self.cfg, JOB_ID))
        self.assertEqual(start.call_args.args[2], [
            "/enter", CLOSURE, "python", "-I", job + "/tools/scripts/harness/hx.py", "floor", "--in-job", job,
            "--hold", "/hold", "--root", "/root", "--enter-closure", "/enter", "--a-folder", self.build_a,
            "--a-closure", CLOSURE, "--b-folder", self.build_b, "--b-closure", CLOSURE_OTHER, "--device", DEVICE,
            "--expect", EXPECT, "--env", "EXAMPLE_FLAG=1", "--scene", "light"])
        follow.assert_called_once_with(self.cfg, JOB_ID)
        self.assertEqual(status, 0)

    def test_the_client_says_the_builds_the_method_and_the_plan_length(self):
        _, out, _, _ = self.client()
        self.assertIn("build A: " + self.build_a, out)
        self.assertIn("build B: " + self.build_b, out)
        self.assertIn("runs per arm 5", out)
        self.assertIn("plan: 12 runs", out)

    def test_a_method_that_is_refused_stops_before_any_job(self):
        bad = SUITE_WITH_METHOD.replace(b"runs_per_arm = 5", b"runs_per_arm = 3")
        start = mock.Mock()
        with self.assertRaises(verdict.MethodError):
            self.client(suite=bad, start=start)
        start.assert_not_called()

    def test_a_suite_with_no_method_table_stops_before_any_job(self):
        start = mock.Mock()
        with self.assertRaises(verdict.MethodError):
            self.client(suite=SCENES_ONLY, start=start)
        start.assert_not_called()

    def test_an_unknown_device_stops_before_any_job(self):
        start = mock.Mock()
        with self.assertRaisesRegex(hx_floor.FloorError, "no device"):
            self.client(device="nope", start=start)
        start.assert_not_called()

    def test_a_lost_ssh_says_the_job_goes_on(self):
        status, out, _, _ = self.client(follow=mock.Mock(return_value=255))
        self.assertEqual(status, 255)
        self.assertIn("ssh was lost; the job goes on: hx stop " + JOB_ID, out)


class CorrectnessTest(RecordCase):
    def setUp(self):
        super().setUp()
        self.build = str(self.base / "build")
        make_build(self.build)
        self.sub = str(self.base / "job" / "runs" / "00")
        os.makedirs(os.path.join(self.sub, "run"))
        os.makedirs(os.path.join(self.root, "scenes"))
        with open(os.path.join(self.root, "scenes", "suite.toml"), "wb") as f:
            f.write(SCENES_ONLY)
        self.requests = []
        self.started = []
        self.played = []
        self.patch(hx_run, "copy_game", side_effect=fake_copy)
        self.patch(hx_run, "binary_hashes", return_value=HASHES)
        self.patch(hx_run, "kernel_text", return_value=None)

    def fake_game(self, outputs=None):
        requests = self.requests

        class FakeConn:
            def request(self, cmd, **args):
                requests.append((cmd, args))
                if cmd == "shot":
                    with open(args["path"], "wb") as f:
                        f.write(SHOT_A)
                    return {"ok": True, "path": args["path"]}
                return {"ok": True}

            def close(self):
                pass

        class FakeGame:
            def connect(self):
                return FakeConn()

            def quit(self, timeout=60):
                return 0

            def outputs(self):
                return outputs if outputs is not None else {"log": b"vk: ok\n", "frame_log": frame_csv([16.0] * FRAMES)}

        return FakeGame()

    def start(self, job, game_dir, device_env, save=None, slot=None):
        self.started.append((job, game_dir, dict(device_env), save, slot))
        return self.game

    def play(self, running, scene, expect):
        self.played.append((scene.name, expect))
        return {"replayed": 1}, {"replayed": FRAMES}, "Area"

    def correct(self, start=None, play=None):
        self.game = self.fake_game()
        return hx_floor.correctness_run(self.sub, self.root, self.build, DEVICE, EXPECT, DEVICE_ENV, "light",
                                        start=start or self.start, play=play or self.play, machine=lambda: {},
                                        journal=self.lines.append)

    def test_a_pass_takes_three_shots_sixty_presents_apart_and_records_them_with_the_run(self):
        (status, run_id), _, _ = run_quietly(self.correct)
        self.assertEqual((status, run_id), (0, "000001"))
        expected = []
        for number in (1, 2, 3):
            expected.append(("wait", {"event": "present", "frames": 60, "timeout": hx_run.WAIT_SECONDS}))
            expected.append(("shot", {"path": os.path.join(self.sub, "run", "shot_%d.png" % number),
                                      "timeout": hx_run.WAIT_SECONDS}))
        self.assertEqual(self.requests, expected)
        manifest = self.run_manifest(run_id)
        self.assertEqual(sorted(manifest["screenshots"]), ["shot_1.png", "shot_2.png", "shot_3.png"])
        self.assertEqual(manifest["screenshots"]["shot_1.png"]["sha256"], sha(SHOT_A))
        self.assertEqual(sorted(manifest["files"]),
                         ["frametimes.csv", "log.txt", "metrics.json", "scene.json", "suite.toml"])
        self.assertEqual(manifest["facts"]["suite_sha256"], sha(SCENES_ONLY))
        self.assertEqual(json.loads(self.record_file(run_id, "scene.json"))["verdict"], "pass")
        self.assertEqual(self.hx_verify_problems(), [])

    def test_the_game_starts_from_the_scene_save_with_the_device_environment_and_no_warm_copy(self):
        run_quietly(self.correct)
        job, game_dir, device_env, save, slot = self.started[0]
        self.assertEqual(game_dir, os.path.join(self.sub, "game"))
        self.assertEqual(device_env, DEVICE_ENV)
        self.assertEqual(save, scenes.save_dir(self.root, "start"))
        self.assertIsNone(slot)
        self.assertEqual(self.played, [("light", EXPECT)])

    def test_a_failed_window_is_recorded_as_a_fail_and_takes_no_shots(self):
        def play(running, scene, expect):
            raise bridge.BridgeError("the window is not steady at frame 9: map 'Other', not 'Area'")

        (status, run_id), _, _ = run_quietly(self.correct, play=play)
        self.assertEqual((status, run_id), (1, "000001"))
        self.assertEqual([cmd for cmd, _ in self.requests], [])
        self.assertEqual(sorted(self.run_manifest(run_id)["screenshots"]), [])
        scene_report = json.loads(self.record_file(run_id, "scene.json"))
        self.assertEqual(scene_report["verdict"], "FAIL")
        self.assertIn("not steady", scene_report["reason"])
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_game_that_does_not_start_is_status_2_and_records_nothing(self):
        def start(job, game_dir, device_env, save=None, slot=None):
            raise game.GameError("the game would not start")

        (status, run_id), _, err = run_quietly(self.correct, start=start)
        self.assertEqual((status, run_id), (2, None))
        self.assertIn("the game did not start", err)
        self.assertEqual(record.run_ids(self.results), [])


class CorrectnessInJobTest(unittest.TestCase):
    def test_the_in_job_correctness_argv_runs_correctness_run_with_its_arguments_and_exits_with_its_status(self):
        argv = hx_floor.correctness_argv("/enter", CLOSURE, "/job/hx.py", "/job/runs/00", "/root", "/builds/x", DEVICE,
                                         EXPECT, DEVICE_ENV, "light")
        with mock.patch.object(hx_floor, "correctness_run", return_value=(1, "000004")) as run:
            status, _, _ = run_quietly(hx_floor.main, argv[argv.index("--correctness-in-job"):])
        self.assertEqual(status, 1)
        run.assert_called_once_with("/job/runs/00", "/root", "/builds/x", DEVICE, EXPECT, DEVICE_ENV, "light")


class SeriesTest(RecordCase):
    def setUp(self):
        super().setUp()
        self.job = str(self.base / "job")
        os.makedirs(self.job)
        self.a = argparse.Namespace(job=self.job, hold="/hold", root=self.root, enter_closure="/enter",
                                    a_folder="/builds/a", a_closure="/nix/store/a", b_folder="/builds/b",
                                    b_closure="/nix/store/b", device=DEVICE, expect=EXPECT,
                                    env=[("EXAMPLE_FLAG", "1")], scene="light")
        self.events, self.holds, self.statuses = [], [], {}
        self.argvs = {}
        self.validation = False
        self.patch(hx_floor, "validation_available", side_effect=lambda: self.validation)

    def wait(self):
        self.events.append(("wait",))
        return self.holds.pop(0) if self.holds else 0.0

    def launch(self, argv):
        flag = "--in-job" if "--in-job" in argv else "--correctness-in-job"
        sub = argv[argv.index(flag) + 1]
        index = int(os.path.basename(sub))
        self.events.append(("run", index))
        self.argvs[index] = argv
        os.makedirs(os.path.join(sub, "game"))
        return self.statuses.get(index, 0), "%06d" % (index + 1)

    def series(self):
        return hx_floor.run_series(self.a, METHOD, self.wait, launch=self.launch)

    def test_the_plan_runs_abba_timed_runs_then_one_correctness_run_per_arm(self):
        result = self.series()
        self.assertEqual([(e["arm"], e["kind"]) for e in result["entries"]], PLAN)
        self.assertEqual([e["index"] for e in result["entries"]], list(range(12)))
        self.assertIsNone(result["failure"])
        self.assertTrue(all(e["status"] == 0 for e in result["entries"]))

    def test_each_run_waits_before_it_runs_and_a_hold_shows_on_the_entry_it_precedes(self):
        self.holds = [0.0, 0.0, 42.5]
        result = self.series()
        self.assertEqual(len(self.events), 24)
        self.assertTrue(all(self.events[i] == ("wait",) for i in range(0, 24, 2)))
        self.assertEqual([("run", n) for n in range(12)], [e for e in self.events if e[0] == "run"])
        self.assertEqual([e["held_s"] for e in result["entries"][:4]], [0.0, 0.0, 42.5, 0.0])

    def test_each_run_is_launched_through_its_arms_closure_and_the_closure_entry(self):
        result = self.series()
        closures = {"A": "/nix/store/a", "B": "/nix/store/b"}
        hx_py = os.path.join(self.job, "tools", "scripts", "harness", "hx.py")
        for entry in result["entries"]:
            argv = self.argvs[entry["index"]]
            self.assertEqual(argv[:5], ["/enter", closures[entry["arm"]], "python", "-I", hx_py], entry["index"])

    def test_timed_runs_are_hx_run_in_job_and_correctness_runs_are_the_floor_correctness_in_job(self):
        self.series()
        for index in range(10):
            self.assertEqual(self.argvs[index][5:7], ["run", "--in-job"], index)
            self.assertEqual(self.argvs[index][self.argvs[index].index("--in-job") + 1],
                             os.path.join(self.job, "runs", "%02d" % index))
        for index in (10, 11):
            self.assertEqual(self.argvs[index][5:7], ["floor", "--correctness-in-job"], index)
            self.assertEqual(self.argvs[index][self.argvs[index].index("--correctness-in-job") + 1],
                             os.path.join(self.job, "runs", "%02d" % index))

    def test_each_run_gets_its_arms_build_folder(self):
        self.series()
        self.assertEqual(self.argvs[0][self.argvs[0].index("--build-folder") + 1], "/builds/a")
        self.assertEqual(self.argvs[1][self.argvs[1].index("--build-folder") + 1], "/builds/b")
        self.assertEqual(self.argvs[10][self.argvs[10].index("--build-folder") + 1], "/builds/a")
        self.assertEqual(self.argvs[11][self.argvs[11].index("--build-folder") + 1], "/builds/b")

    def test_timed_runs_get_the_device_environment_and_the_timing_environment(self):
        self.series()
        for index in range(10):
            self.assertEqual(env_pairs(self.argvs[index]), ["EXAMPLE_FLAG=1", "HOS_NO_PACING=1"], index)

    def test_correctness_runs_get_the_timing_environment_too_and_no_validation_when_the_layer_is_not_listed(self):
        result = self.series()
        self.assertFalse(result["validation"])
        for index in (10, 11):
            self.assertEqual(env_pairs(self.argvs[index]), ["EXAMPLE_FLAG=1", "HOS_NO_PACING=1"], index)

    def test_correctness_runs_get_the_validation_environment_when_the_layer_is_listed(self):
        self.validation = True
        result = self.series()
        self.assertTrue(result["validation"])
        for index in (10, 11):
            self.assertEqual(env_pairs(self.argvs[index]),
                             ["EXAMPLE_FLAG=1", "HOS_NO_PACING=1", "RX_VK_VALIDATION=1"], index)
        for index in range(10):
            self.assertEqual(env_pairs(self.argvs[index]), ["EXAMPLE_FLAG=1", "HOS_NO_PACING=1"], index)

    def test_each_sub_job_has_data_and_run_folders_and_loses_its_game_folder_after_its_run(self):
        self.series()
        for index in range(12):
            sub = os.path.join(self.job, "runs", "%02d" % index)
            self.assertTrue(os.path.isdir(os.path.join(sub, "data")), index)
            self.assertTrue(os.path.isdir(os.path.join(sub, "run")), index)
            self.assertFalse(os.path.lexists(os.path.join(sub, "game")), index)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(sub, "run")).st_mode), 0o700, index)

    def test_the_first_run_that_does_not_pass_ends_the_series_and_nothing_is_rerun(self):
        self.statuses = {3: 1}
        result = self.series()
        self.assertEqual([e["index"] for e in result["entries"]], [0, 1, 2, 3])
        self.assertEqual(result["entries"][-1]["status"], 1)
        self.assertEqual(result["entries"][-1]["run"], "000004")
        self.assertIn("run 3", result["failure"])
        self.assertEqual(sum(1 for e in self.events if e[0] == "run"), 4)
        self.assertEqual(sum(1 for e in self.events if e == ("wait",)), 4)

    def test_a_folder_error_after_a_run_ends_the_series_and_keeps_the_runs_that_ran(self):
        real_rmtree = shutil.rmtree
        calls = []

        def rmtree(path):
            calls.append(path)
            if len(calls) == 2:
                raise PermissionError(13, "Permission denied", path)
            real_rmtree(path)

        self.patch(shutil, "rmtree", side_effect=rmtree)
        result = self.series()
        self.assertEqual([e["index"] for e in result["entries"]], [0, 1])
        self.assertEqual([e["run"] for e in result["entries"]], ["000001", "000002"])
        self.assertEqual(result["entries"][-1]["status"], 0)
        self.assertIn("run 1", result["failure"])
        self.assertIn("Permission denied", result["failure"])
        self.assertEqual(sum(1 for e in self.events if e[0] == "run"), 2)

    def test_a_folder_error_before_a_run_starts_it_never_and_ends_the_series(self):
        os.makedirs(os.path.join(self.job, "runs", "02", "data"))
        result = self.series()
        self.assertEqual([e["index"] for e in result["entries"]], [0, 1, 2])
        self.assertIsNone(result["entries"][-1]["run"])
        self.assertEqual(result["entries"][-1]["status"], 2)
        self.assertIn("run 2", result["failure"])
        self.assertEqual([e for e in self.events if e[0] == "run"], [("run", 0), ("run", 1)])

    def test_a_correctness_run_that_could_not_run_is_the_series_failure(self):
        self.statuses = {11: 2}
        result = self.series()
        self.assertEqual(result["entries"][-1]["index"], 11)
        self.assertEqual(result["entries"][-1]["status"], 2)
        self.assertIn("run 11", result["failure"])


class ReadBackTest(RecordCase):
    def setUp(self):
        super().setUp()
        self.values = [float(n) for n in range(1, FRAMES + 1)]
        files = timed_files()
        files["frametimes.csv"] = frame_csv(self.values)
        self.run_id = self.write_run(files, {"shot_1.png": SHOT_A, "shot_2.png": SHOT_B})

    def set_bytes(self, path, data):
        (self.results / path).write_bytes(data)

    def test_run_frames_are_the_frames_of_the_window_in_order(self):
        scene = hx_run.dump({"window": {"map": "Area", "first": 10, "last": 19}})
        run_id = self.write_run({**timed_files(), "frametimes.csv": frame_csv(self.values), "scene.json": scene})
        self.assertEqual(hx_floor.run_frames(self.results, run_id), [float(n) for n in range(10, 20)])

    def test_run_frames_of_a_window_without_frames_are_refused(self):
        scene = hx_run.dump({"window": {"map": "Area", "first": 500, "last": 600}})
        run_id = self.write_run({**timed_files(), "scene.json": scene})
        with self.assertRaisesRegex(hx_floor.FloorError, "no frames|window"):
            hx_floor.run_frames(self.results, run_id)

    def test_run_frames_of_a_scene_with_no_window_are_refused(self):
        run_id = self.write_run({**timed_files(), "scene.json": hx_run.dump({"window": {"first": None, "last": None}})})
        with self.assertRaisesRegex(hx_floor.FloorError, "window"):
            hx_floor.run_frames(self.results, run_id)

    def test_run_log_is_the_run_log_as_text(self):
        run_id = self.write_run(timed_files(log=b"vk: flash at present 4\n"))
        self.assertEqual(hx_floor.run_log(self.results, run_id), "vk: flash at present 4\n")

    def test_run_shots_are_the_screenshots_by_name_in_the_record(self):
        shots = hx_floor.run_shots(self.results, self.run_id)
        self.assertEqual(sorted(shots), ["shot_1.png", "shot_2.png"])
        self.assertEqual(shots["shot_1.png"].read_bytes(), SHOT_A)
        self.assertEqual(shots["shot_2.png"].read_bytes(), SHOT_B)

    def test_a_changed_frame_log_is_refused(self):
        self.set_bytes("runs/%s/files/frametimes.csv" % self.run_id, frame_csv([99.0] * FRAMES))
        with self.assertRaisesRegex(hx_floor.FloorError, "does not match"):
            hx_floor.run_frames(self.results, self.run_id)

    def test_a_missing_log_is_refused(self):
        (self.results / "runs" / self.run_id / "files" / "log.txt").unlink()
        with self.assertRaisesRegex(hx_floor.FloorError, "cannot be read"):
            hx_floor.run_log(self.results, self.run_id)

    def test_a_changed_screenshot_is_refused(self):
        self.set_bytes("screenshots/" + sha(SHOT_B), SHOT_A)
        with self.assertRaisesRegex(hx_floor.FloorError, "does not match"):
            hx_floor.run_shots(self.results, self.run_id)

    def test_a_missing_screenshot_is_refused(self):
        (self.results / "screenshots" / sha(SHOT_A)).unlink()
        with self.assertRaisesRegex(hx_floor.FloorError, "cannot be read"):
            hx_floor.run_shots(self.results, self.run_id)


class WriteVerdictTest(RecordCase):
    def test_the_run_takes_the_first_runs_facts_and_holds_the_payload_and_the_suite(self):
        facts = {key: {"first": key} for key in record.FACT_KEYS}
        first, _ = record.write_run(self.results, facts, timed_files(), {}, journal=self.lines.append)
        payload = {"kind": "floor", "outcome": "recorded"}
        run_id = hx_floor.write_verdict(self.root, "floor.json", payload, SUITE_WITH_METHOD, first,
                                        journal=self.lines.append)
        manifest = self.run_manifest(run_id)
        self.assertEqual(manifest["facts"], facts)
        self.assertEqual(self.record_file(run_id, "floor.json"), hx_run.dump(payload))
        self.assertEqual(self.record_file(run_id, "suite.toml"), SUITE_WITH_METHOD)
        self.assertEqual(self.hx_verify_problems(), [])

    def test_with_no_first_run_every_fact_is_a_gap(self):
        run_id = hx_floor.write_verdict(self.root, "floor.json", {"kind": "floor"}, SUITE_WITH_METHOD, None,
                                        journal=self.lines.append)
        gap = {"gap": "no run of the series was recorded"}
        self.assertEqual(self.run_manifest(run_id)["facts"], {key: gap for key in record.FACT_KEYS})
        self.assertEqual(self.hx_verify_problems(), [])


class InJobTest(RecordCase):
    """The in-job half end to end: the real series and the real measurement, with the runs and the shots faked and their
    records written for real. The scene is the real parse of the scenes, its load supplied here (see the docstring)."""

    def setUp(self):
        super().setUp()
        self.job = str(self.base / "jobs" / JOB_ID)
        os.makedirs(self.job)
        self.build = str(self.base / "build")
        make_build(self.build)
        self.suite_bytes = SUITE_WITH_METHOD
        parsed = scenes.parse(SCENES_ONLY, "suite.toml")
        self.suite = scenes.Suite(path="suite.toml", sha256=sha(SUITE_WITH_METHOD), scenes=parsed.scenes)
        self.scene = parsed.scenes["light"]
        self.statuses, self.logs, self.correct_shots = {}, {}, {}
        self.timed_argvs = []
        self.patch(hx_run, "load_scene",
                   side_effect=lambda root, name, final: (self.suite_bytes, self.suite, self.scene))
        self.patch(hx_floor, "validation_available", return_value=False)
        self.patch(hx_floor.shutil, "which", return_value=None)
        self.patch(jobs, "wait_hold", side_effect=[0.0, 0.0, 42.5] + [0.0] * 9)
        real_series = hx_floor.run_series
        self.patch(hx_floor, "run_series", side_effect=lambda a, method, wait: real_series(
            a, method, wait, launch=self.launch))
        real_write = hx_floor.write_verdict
        self.patch(hx_floor, "write_verdict",
                   side_effect=lambda *args: real_write(*args, journal=self.lines.append))

    def launch(self, argv):
        """A run's launch as a fake: a timed run writes its record run, a correctness run writes its record run with shots"""
        if "--in-job" in argv:
            sub = argv[argv.index("--in-job") + 1]
            index = int(os.path.basename(sub))
            self.timed_argvs.append(argv)
            os.makedirs(os.path.join(sub, "game"))
            run_id = self.write_run(timed_files(frame_ms=16.5 + 0.1 * index))
            return self.statuses.get(index, 0), run_id
        sub = argv[argv.index("--correctness-in-job") + 1]
        index = int(os.path.basename(sub))
        os.makedirs(os.path.join(sub, "game"))
        shots = self.correct_shots.get(index, {"shot_1.png": SHOT_A, "shot_2.png": SHOT_A, "shot_3.png": SHOT_A})
        run_id = self.write_run(timed_files(log=self.logs.get(index, b"vk: ok\n")), shots)
        return self.statuses.get(index, 0), run_id

    def argv(self):
        return ["--in-job", self.job, "--hold", str(self.base / "hold"), "--root", self.root,
                "--enter-closure", "/enter", "--a-folder", self.build, "--a-closure", CLOSURE,
                "--b-folder", self.build, "--b-closure", CLOSURE, "--device", DEVICE, "--expect", EXPECT,
                "--env", "EXAMPLE_FLAG=1", "--scene", "light"]

    def floor(self, run_id):
        return json.loads(self.record_file(run_id, "floor.json"))

    def latest(self):
        return record.run_ids(self.results)[-1]

    def test_a_complete_floor_is_recorded_with_its_floor_tolerance_and_determinism(self):
        status, out, _ = run_quietly(hx_floor.main, self.argv())
        self.assertEqual(status, 0)
        run_id = self.latest()
        self.assertIn("floor: " + run_id, out)
        payload = self.floor(run_id)
        self.assertEqual(payload["kind"], "floor")
        self.assertEqual(payload["outcome"], "recorded")
        self.assertIsNone(payload["reason"])
        self.assertEqual((payload["device"], payload["expect"], payload["scene"]), (DEVICE, EXPECT, "light"))
        self.assertEqual((payload["a_sha"], payload["b_sha"]), (SHA, SHA))
        self.assertEqual(payload["suite_sha256"], sha(SUITE_WITH_METHOD))
        self.assertEqual(payload["method"], {"confidence": 0.95, "runs_per_arm": 5, "warmup_seconds": 0.0,
                                             "resamples": 200, "seed": 7})
        self.assertFalse(payload["validation"])
        self.assertEqual(len(payload["series"]), 12)
        self.assertEqual([(e["arm"], e["kind"]) for e in payload["series"]], PLAN)
        self.assertEqual(payload["series"][2]["held_s"], 42.5)
        self.assertIsInstance(payload["floor"], float)
        self.assertGreaterEqual(payload["floor"], 0)
        low, high = payload["interval"]
        self.assertLessEqual(low, high)
        self.assertEqual(len(payload["a_medians"]), 5)
        self.assertEqual(len(payload["b_medians"]), 5)
        self.assertEqual(payload["image"]["metric"], "pixel")
        self.assertEqual(payload["image"]["tolerance"], 0.0)
        self.assertEqual(payload["image"]["determinism"], "deterministic")
        self.assertEqual(self.run_manifest(run_id)["facts"], self.run_manifest("000001")["facts"])
        self.assertEqual(self.hx_verify_problems(), [])

    def test_nondeterministic_shots_are_recorded_with_their_tolerance_and_do_not_void_the_floor(self):
        self.correct_shots[11] = {"shot_1.png": SHOT_A, "shot_2.png": SHOT_B, "shot_3.png": SHOT_A}
        status, _, _ = run_quietly(hx_floor.main, self.argv())
        self.assertEqual(status, 0)
        image = self.floor(self.latest())["image"]
        self.assertEqual(image["determinism"], "nondeterministic")
        self.assertGreater(image["tolerance"], 0)
        self.assertEqual(image["shots"]["shot_2.png"], image["tolerance"])
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_flash_in_a_baseline_log_voids_the_floor_and_the_record_says_so(self):
        self.logs[10] = b"vk: flash at present 12\n"
        status, out, _ = run_quietly(hx_floor.main, self.argv())
        self.assertEqual(status, 1)
        payload = self.floor(self.latest())
        self.assertEqual(payload["outcome"], "failed")
        self.assertIn("flash", payload["reason"])
        for key in ("floor", "interval", "a_medians", "b_medians", "image"):
            self.assertIsNone(payload[key], key)
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_failed_timed_run_stops_the_series_and_records_a_failed_floor(self):
        self.statuses = {3: 1}
        status, out, _ = run_quietly(hx_floor.main, self.argv())
        self.assertEqual(status, 1)
        payload = self.floor(self.latest())
        self.assertEqual(payload["outcome"], "failed")
        self.assertIn("run 3", payload["reason"])
        self.assertEqual([e["index"] for e in payload["series"]], [0, 1, 2, 3])
        self.assertIsNone(payload["floor"])
        self.assertIsNone(payload["image"])
        self.assertEqual(len(self.timed_argvs), 4)
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_method_refused_by_the_suite_is_a_could_not_run_that_records_nothing(self):
        self.suite_bytes = SUITE_WITH_METHOD.replace(b"runs_per_arm = 5", b"runs_per_arm = 2")
        status, _, err = run_quietly(hx_floor.main, self.argv())
        self.assertEqual(status, 2)
        self.assertIn("runs_per_arm", err)
        self.assertEqual(record.run_ids(self.results), [])
        self.assertEqual(self.timed_argvs, [])


if __name__ == "__main__":
    unittest.main()
