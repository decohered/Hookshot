"""scripts/harness/hx_bench.py (hx bench): the A-B-B-A series of two builds as one bench job, judged against a recorded
floor of the same device, scene and method, and recorded as one bench record run. Fakes only: ssh, the job, the
subprocesses, the game, the bridge and the journal are stand-ins, and the numbers are synthetic; no game, GPU or host is
read. The in-job test takes the scene from the real parser but supplies the suite's bytes and its load itself, because
scenes.parse refuses a top-level [method] table (scenes.py, _known).

- R2: hx bench runs the floor's series, A-B-B-A with each run once, and calls it slower, faster or no change against the
  floor's noise floor: a planted slower, faster and identical series each get their call, and a change inside the floor
  is no change. The client job's argv carries both closures, the device, the scene and --floor, and the in-job parser
  reads it back. A floor recorded with no image is accepted.
- R3 (DL185): a flash, a new validation error in B or a hang void the bench with no verdict, each cause alone; a
  validation error both builds share does not void it. The correctness runs' pictures decide nothing: pictures that
  differ, or differ in size, leave the bench judged by its timing. A run that fails, or prints no run id, is recorded
  failed, and a run is not rerun.
- R4: a hold during the series shows on the entry of the run it precedes, and the verdict is the verdict without the hold.
- A floor of another device, scene or method, or one that is not a recorded floor, is refused before any run. Every bench
  record passes hx_verify's check.

python -I -B tests/test_harness_bench.py
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import struct
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
import config
import hx_bench
import hx_floor
import hx_run
import hx_verify
import jobs
import record
import runrecord
import scenes

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
FLOOR = 0.02
METHOD_DICT = {"confidence": 0.95, "runs_per_arm": 5, "warmup_seconds": 0.0, "resamples": 200, "seed": 7}

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


def solid(value, size=2):
    return png([[(value, value, value)] * size for _ in range(size)])


SHOT_A = solid(10)
SHOT_B = solid(200)
SHOT_WIDER = solid(200, size=3)


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


def make_build(folder, sha_value):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "build.txt"), "w", encoding="utf-8") as f:
        f.write("closure: %s\nsha: %s\n" % (CLOSURE, sha_value))


def run_quietly(fn, *args, **kwargs):
    """fn's result with its stdout and stderr captured: (result, stdout, stderr)"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


class BenchCase(unittest.TestCase):
    """A temporary harness root with a recorded floor, the in-job half's reads and waits faked, and each run a fake that
    writes its record run through record.py, so hx_verify can check the whole record."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = str(self.base / "root")
        self.results = runrecord.results_dir(self.root)
        self.lines = []
        self.build_a = str(self.base / "build-a")
        self.build_b = str(self.base / "build-b")
        make_build(self.build_a, SHA)
        make_build(self.build_b, SHA_OTHER)
        self.suite = scenes.Suite(path="suite.toml", sha256=sha(SUITE_WITH_METHOD),
                                  scenes=scenes.parse(SCENES_ONLY, "suite.toml").scenes)
        self.frame_ms = {"A": 16.5, "B": 16.5}
        self.holds = []
        self.statuses, self.logs, self.timed_logs, self.correct_shots = {}, {}, {}, {}
        self.unnamed, self.bad_window = set(), set()
        self.launched, self.argvs = [], {}
        self.jobs_started = 0
        self.patch(hx_run, "load_scene", side_effect=lambda root, name, final: (
            SUITE_WITH_METHOD, self.suite, self.suite.scenes[name]))
        self.patch(hx_floor, "validation_available", return_value=False)
        self.patch(shutil, "which", return_value=None)
        self.patch(jobs, "wait_hold", side_effect=lambda hold, **kwargs: self.holds.pop(0) if self.holds else 0.0)
        real_series = hx_floor.run_series
        self.patch(hx_floor, "run_series", side_effect=lambda a, method, wait: real_series(
            a, method, wait, launch=self.launch))
        self.real_write_verdict = hx_floor.write_verdict
        self.patch(hx_floor, "write_verdict", side_effect=lambda *args: self.real_write_verdict(
            *args, journal=self.lines.append))
        self.floor_run = self.record_floor()

    def patch(self, target, attribute, **kwargs):
        p = mock.patch.object(target, attribute, **kwargs)
        mocked = p.start()
        self.addCleanup(p.stop)
        return mocked

    def write_run(self, files, shots=None):
        """A record run whose facts name the run they came from, so a bench record's facts show which run gave them"""
        facts = {key: {"value": key, "run": len(record.run_ids(self.results)) + 1} for key in record.FACT_KEYS}
        run_id, _ = record.write_run(self.results, facts, files, shots or {}, journal=self.lines.append)
        return run_id

    def record_file(self, run_id, name):
        return (self.results / "runs" / run_id / "files" / name).read_bytes()

    def run_manifest(self, run_id):
        manifest, _ = record.read_manifest(self.results, run_id)
        return manifest

    def hx_verify_problems(self):
        problems, _ = hx_verify.check(self.results, record.parse_journal("\n".join(self.lines)))
        return problems

    def payload(self, run_id):
        return json.loads(self.record_file(run_id, "bench.json"))

    def latest(self):
        return record.run_ids(self.results)[-1]

    def floor_payload(self, **changes):
        """A floor's payload as hx floor writes it for a recorded floor, with the changes"""
        payload = {
            "kind": "floor", "outcome": "recorded", "reason": None, "device": DEVICE, "expect": EXPECT,
            "scene": "light", "a_sha": SHA, "b_sha": SHA, "suite_sha256": sha(SUITE_WITH_METHOD),
            "method": dict(METHOD_DICT), "validation": False, "series": [], "floor": FLOOR,
            "interval": [-0.01, 0.01], "a_medians": [16.5] * 5, "b_medians": [16.5] * 5,
        }
        payload.update(changes)
        return payload

    def record_floor(self, **changes):
        """A floor record as hx floor writes it: its first run's facts and floor.json; returns its id"""
        facts = {key: {"value": key} for key in record.FACT_KEYS}
        first, _ = record.write_run(self.results, facts, timed_files(), {}, journal=self.lines.append)
        return self.real_write_verdict(self.root, "floor.json", self.floor_payload(**changes), SUITE_WITH_METHOD, first,
                                       journal=self.lines.append)

    def launch(self, argv):
        """A run's launch as a fake: a timed run writes its record run, a correctness run writes its record run with shots"""
        if "--in-job" in argv:
            sub = argv[argv.index("--in-job") + 1]
            index = int(os.path.basename(sub))
            self.launched.append(index)
            self.argvs[index] = argv
            os.makedirs(os.path.join(sub, "game"))
            arm = "A" if argv[argv.index("--build-folder") + 1] == self.build_a else "B"
            files = timed_files(frame_ms=self.frame_ms[arm], log=self.timed_logs.get(index, b"vk: ok\n"))
            if index in self.bad_window:
                files["scene.json"] = hx_run.dump({"window": {"map": "Area", "first": 500, "last": 600}})
            run_id = None if index in self.unnamed else self.write_run(files)
            return self.statuses.get(index, 0), run_id
        sub = argv[argv.index("--correctness-in-job") + 1]
        index = int(os.path.basename(sub))
        self.launched.append(index)
        self.argvs[index] = argv
        os.makedirs(os.path.join(sub, "game"))
        run_id = self.write_run(timed_files(log=self.logs.get(index, b"vk: ok\n")), self.correct_shots.get(index))
        return self.statuses.get(index, 0), run_id

    def bench(self, scene="light", device=DEVICE):
        """hx bench's in-job half in a job folder of its own: (status, stdout, stderr)"""
        self.jobs_started += 1
        self.launched, self.argvs = [], {}
        job = str(self.base / "jobs" / ("%s-%d" % (JOB_ID, self.jobs_started)))
        os.makedirs(job)
        argv = ["--in-job", job, "--hold", str(self.base / "hold"), "--root", self.root, "--enter-closure", "/enter",
                "--a-folder", self.build_a, "--a-closure", CLOSURE, "--b-folder", self.build_b,
                "--b-closure", CLOSURE_OTHER, "--device", device, "--expect", EXPECT, "--env", "EXAMPLE_FLAG=1",
                "--scene", scene, "--floor", self.floor_run]
        return run_quietly(hx_bench.main, argv)

    def assertVoid(self, status, out, reasons):
        """The bench is void with exactly these reasons: exit 1, no verdict, and every measured value null"""
        self.assertEqual(status, 1)
        self.assertIn("verdict: void", out)
        run_id = self.latest()
        payload = self.payload(run_id)
        self.assertEqual(payload["outcome"], "void")
        self.assertEqual(payload["void_reasons"], reasons)
        for key in ("interval", "a_medians", "b_medians"):
            self.assertIsNone(payload[key], key)
        self.assertEqual(self.hx_verify_problems(), [])
        return payload

    def assertFailed(self, status, out, reason):
        self.assertEqual(status, 1)
        self.assertIn("verdict: failed", out)
        payload = self.payload(self.latest())
        self.assertEqual(payload["outcome"], "failed")
        self.assertIn(reason, payload["reason"])
        self.assertEqual(payload["void_reasons"], [])
        for key in ("interval", "a_medians", "b_medians"):
            self.assertIsNone(payload[key], key)
        self.assertEqual(self.hx_verify_problems(), [])
        return payload


class VerdictTest(BenchCase):
    def test_a_planted_slower_series_is_recorded_slower_against_the_floor(self):
        self.frame_ms = {"A": 16.5, "B": 18.15}
        status, out, _ = self.bench()
        run_id = self.latest()
        self.assertEqual(status, 0)
        self.assertIn("bench: " + run_id, out)
        self.assertIn("verdict: slower", out)
        payload = self.payload(run_id)
        self.assertEqual(payload["kind"], "bench")
        self.assertEqual(payload["outcome"], "slower")
        self.assertIsNone(payload["reason"])
        self.assertEqual(payload["void_reasons"], [])
        low, high = payload["interval"]
        self.assertAlmostEqual(low, 0.1, places=6)
        self.assertAlmostEqual(high, 0.1, places=6)
        self.assertEqual(len(payload["a_medians"]), 5)
        self.assertEqual(len(payload["b_medians"]), 5)
        self.assertEqual([(e["arm"], e["kind"]) for e in payload["series"]], PLAN)
        self.assertEqual((payload["device"], payload["expect"], payload["scene"]), (DEVICE, EXPECT, "light"))
        self.assertEqual((payload["a_sha"], payload["b_sha"]), (SHA, SHA_OTHER))
        self.assertEqual(payload["suite_sha256"], sha(SUITE_WITH_METHOD))
        self.assertEqual(payload["method"], METHOD_DICT)
        self.assertEqual(payload["floor_run"], self.floor_run)
        self.assertEqual(payload["floor"], FLOOR)
        self.assertNotIn("image", payload)
        self.assertFalse(payload["validation"])
        self.assertEqual(self.record_file(run_id, "suite.toml"), SUITE_WITH_METHOD)
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_planted_faster_series_is_recorded_faster_against_the_floor(self):
        self.frame_ms = {"A": 16.5, "B": 14.85}
        status, out, _ = self.bench()
        self.assertEqual(status, 0)
        self.assertIn("verdict: faster", out)
        payload = self.payload(self.latest())
        self.assertEqual(payload["outcome"], "faster")
        low, high = payload["interval"]
        self.assertAlmostEqual(low, -0.1, places=6)
        self.assertAlmostEqual(high, -0.1, places=6)
        self.assertEqual(self.hx_verify_problems(), [])

    def test_an_identical_series_is_recorded_no_change(self):
        status, out, _ = self.bench()
        self.assertEqual(status, 0)
        self.assertIn("verdict: no change", out)
        self.assertEqual(self.payload(self.latest())["outcome"], "no change")
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_change_inside_the_floor_is_no_change(self):
        self.frame_ms = {"A": 16.5, "B": 16.665}
        status, out, _ = self.bench()
        self.assertEqual(status, 0)
        self.assertIn("verdict: no change", out)
        payload = self.payload(self.latest())
        self.assertEqual(payload["outcome"], "no change")
        self.assertAlmostEqual(payload["interval"][0], 0.01, places=6)
        self.assertEqual(self.hx_verify_problems(), [])

    def test_the_series_runs_abba_each_run_once_through_its_arms_build_and_closure(self):
        status, _, _ = self.bench()
        self.assertEqual(status, 0)
        self.assertEqual(self.launched, list(range(12)))
        payload = self.payload(self.latest())
        self.assertEqual([(e["arm"], e["kind"]) for e in payload["series"]], PLAN)
        closures = {"A": CLOSURE, "B": CLOSURE_OTHER}
        folders = {"A": self.build_a, "B": self.build_b}
        for entry in payload["series"]:
            argv = self.argvs[entry["index"]]
            self.assertEqual(argv[1], closures[entry["arm"]], entry["index"])
            self.assertEqual(argv[argv.index("--build-folder") + 1], folders[entry["arm"]], entry["index"])

    def test_the_bench_record_takes_the_facts_of_its_first_timed_run(self):
        status, _, _ = self.bench()
        self.assertEqual(status, 0)
        run_id = self.latest()
        first = self.payload(run_id)["series"][0]["run"]
        self.assertEqual(self.run_manifest(run_id)["facts"], self.run_manifest(first)["facts"])
        self.assertNotEqual(first, self.floor_run)

    def test_correctness_pictures_that_differ_do_not_void_the_bench_and_the_timing_decides(self):
        self.frame_ms = {"A": 16.5, "B": 18.15}
        for label, other in (("another colour", SHOT_B), ("another size", SHOT_WIDER)):
            with self.subTest(label):
                self.correct_shots = {10: {"shot_1.png": SHOT_A}, 11: {"shot_1.png": other}}
                status, out, _ = self.bench()
                self.assertEqual(status, 0)
                self.assertIn("verdict: slower", out)
                self.assertEqual(self.payload(self.latest())["void_reasons"], [])
                self.assertEqual(self.hx_verify_problems(), [])

    def test_a_flash_in_the_correctness_log_of_b_voids_the_bench(self):
        self.logs[11] = b"vk: flash at present 12\n"
        status, out, _ = self.bench()
        self.assertVoid(status, out,
                        ["flash in correctness log b: 1 line(s), first: vk: flash at present 12"])

    def test_a_validation_error_new_in_b_voids_the_bench(self):
        self.logs[11] = b"Validation Error: [ VUID-vkCmdDraw-None-0001 ] draw\n"
        status, out, _ = self.bench()
        self.assertVoid(status, out, ["validation error VUID-vkCmdDraw-None-0001 in correctness log b, "
                                      "not in correctness log a"])

    def test_a_validation_error_both_builds_share_does_not_void_the_bench(self):
        self.logs[10] = b"Validation Error: [ VUID-vkCmdDraw-None-0001 ] draw\n"
        self.logs[11] = b"Validation Error: [ VUID-vkCmdDraw-None-0001 ] draw\n"
        status, out, _ = self.bench()
        self.assertEqual(status, 0)
        self.assertIn("verdict: no change", out)
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_hang_in_a_timed_log_voids_the_bench(self):
        self.timed_logs[2] = b"vk: GPU hang: queue 0\n"
        status, out, _ = self.bench()
        self.assertEqual(status, 1)
        self.assertIn("verdict: void", out)
        payload = self.payload(self.latest())
        self.assertEqual(payload["outcome"], "void")
        self.assertEqual(len(payload["void_reasons"]), 1)
        self.assertTrue(payload["void_reasons"][0].startswith("hang in timed log "), payload["void_reasons"])
        self.assertTrue(payload["void_reasons"][0].endswith("1 line(s), first: vk: GPU hang: queue 0"),
                        payload["void_reasons"])
        for key in ("interval", "a_medians", "b_medians"):
            self.assertIsNone(payload[key], key)
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_failed_run_is_recorded_failed_and_the_series_is_not_rerun(self):
        self.statuses = {3: 1}
        status, out, _ = self.bench()
        payload = self.assertFailed(status, out, "run 3 (arm A, timed) ended with status 1")
        self.assertEqual([e["index"] for e in payload["series"]], [0, 1, 2, 3])
        self.assertEqual(self.launched, [0, 1, 2, 3])
        self.assertEqual(payload["floor"], FLOOR)

    def test_a_timed_run_that_prints_no_run_id_fails_the_bench(self):
        self.unnamed = {2}
        status, out, _ = self.bench()
        self.assertFailed(status, out, "run 2 (arm B, timed) printed no run id")

    def test_a_run_whose_frames_are_not_in_its_window_fails_the_bench(self):
        self.bad_window = {2}
        status, out, _ = self.bench()
        self.assertFailed(status, out, "has no frames from 500 to 600")


class HoldTest(BenchCase):
    def test_a_hold_mid_series_shows_on_the_entry_it_precedes_and_the_verdict_is_the_verdict_without_it(self):
        self.frame_ms = {"A": 16.5, "B": 18.15}
        status, _, _ = self.bench()
        self.assertEqual(status, 0)
        without = self.payload(self.latest())
        self.holds = [0.0, 0.0, 42.5]
        status, _, _ = self.bench()
        self.assertEqual(status, 0)
        held_id = self.latest()
        held = self.payload(held_id)
        self.assertEqual(held["series"][2]["held_s"], 42.5)
        self.assertEqual([e["held_s"] for e in held["series"] if e["index"] != 2], [0.0] * 11)
        for key in ("outcome", "interval", "a_medians", "b_medians", "floor"):
            self.assertEqual(held[key], without[key], key)
        self.assertEqual(self.hx_verify_problems(), [])


class RefusalTest(BenchCase):
    """A floor that is not this device, scene and method's recorded floor is refused before any run: exit 2, no run
    started, nothing recorded."""

    def refused(self, fragment):
        before = record.run_ids(self.results)
        status, _, err = self.bench()
        self.assertEqual(status, 2)
        self.assertIn(fragment, err)
        self.assertEqual(self.launched, [])
        self.assertEqual(record.run_ids(self.results), before)

    def test_a_floor_of_another_device_is_refused(self):
        self.floor_run = self.record_floor(device="other-gpu")
        self.refused("device")

    def test_a_floor_of_another_scene_is_refused(self):
        self.floor_run = self.record_floor(scene="medium")
        self.refused("scene")

    def test_a_floor_of_another_method_is_refused(self):
        self.floor_run = self.record_floor(method={**METHOD_DICT, "seed": 8})
        self.refused("method")

    def test_a_floor_that_is_not_recorded_is_refused(self):
        self.floor_run = self.record_floor(outcome="failed")
        self.refused("not 'recorded'")

    def test_a_record_whose_floor_file_is_not_a_floor_is_refused(self):
        self.floor_run = self.record_floor(kind="bench")
        self.refused("not a floor")

    def test_a_record_without_a_floor_file_is_refused(self):
        self.floor_run = self.write_run({"bench.json": hx_run.dump({"kind": "bench"})})
        self.refused("floor.json")

    def test_a_floor_file_that_is_not_json_is_refused(self):
        self.floor_run = self.write_run({"floor.json": b"not json"})
        self.refused("not JSON")

    def test_a_floor_with_no_noise_floor_is_refused(self):
        self.floor_run = self.record_floor(floor=None)
        self.refused("no noise floor")


class ArgvTest(unittest.TestCase):
    def test_the_client_takes_two_builds_a_device_a_scene_and_a_floor(self):
        args = hx_bench.parse_client([SHA, SHA_OTHER, "--device", DEVICE, "--scene", "light", "--floor", "000002"])
        self.assertEqual((args.a, args.b, args.device, args.scene, args.floor),
                         (SHA, SHA_OTHER, DEVICE, "light", "000002"))

    def test_a_floor_id_that_is_not_six_digits_is_refused(self):
        with self.assertRaises(SystemExit):
            run_quietly(hx_bench.parse_client, [SHA, SHA_OTHER, "--device", DEVICE, "--scene", "light", "--floor", "12"])


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config(path="/cfg", host=HOST, hold="/hold", root="/root", dump="/dump",
                                 enter_closure="/enter",
                                 devices={DEVICE: config.Device(name=DEVICE, expect=EXPECT, env=DEVICE_ENV)})
        self.build_a = "/root/builds/" + SHA
        self.build_b = "/root/builds/" + SHA_OTHER

    def test_the_job_runs_the_bench_in_job_half_with_both_closures_the_device_the_scene_and_the_floor(self):
        start, follow = mock.Mock(), mock.Mock(return_value=0)
        suite_text = types.SimpleNamespace(stdout=SUITE_WITH_METHOD.decode("utf-8"))
        with mock.patch.object(hx_floor.remote, "run", return_value=suite_text), \
                mock.patch.object(hx_floor.jobs, "new_id", return_value=JOB_ID):
            status, _, _ = run_quietly(hx_floor.client, "bench", self.cfg, DEVICE, "light",
                                       (self.build_a, CLOSURE), (self.build_b, CLOSURE_OTHER),
                                       extra=("--floor", "000002"), start=start, follow=follow)
        self.assertEqual(status, 0)
        job = "/root/jobs/" + JOB_ID
        argv = start.call_args.args[2]
        self.assertEqual(argv, [
            "/enter", CLOSURE, "python", "-I", job + "/tools/scripts/harness/hx.py", "bench", "--in-job", job,
            "--hold", "/hold", "--root", "/root", "--enter-closure", "/enter", "--a-folder", self.build_a,
            "--a-closure", CLOSURE, "--b-folder", self.build_b, "--b-closure", CLOSURE_OTHER, "--device", DEVICE,
            "--expect", EXPECT, "--env", "EXAMPLE_FLAG=1", "--scene", "light", "--floor", "000002"])
        parsed = hx_bench.parse_in_job(argv[argv.index("--in-job") + 1:])
        self.assertEqual((parsed.floor, parsed.a_closure, parsed.b_closure, parsed.scene),
                         ("000002", CLOSURE, CLOSURE_OTHER, "light"))

    def test_main_resolves_both_builds_and_starts_the_bench_with_its_floor(self):
        cfg = self.cfg
        with mock.patch.object(config, "load", return_value=cfg), \
                mock.patch.object(hx_floor, "resolve", side_effect=[(self.build_a, SHA, CLOSURE),
                                                                    (self.build_b, SHA_OTHER, CLOSURE_OTHER)]), \
                mock.patch.object(hx_floor, "client", return_value=1) as client:
            status, _, _ = run_quietly(hx_bench.main, [SHA, SHA_OTHER, "--device", DEVICE, "--scene", "light",
                                                       "--floor", "000002"])
        self.assertEqual(status, 1)
        client.assert_called_once_with("bench", cfg, DEVICE, "light", (self.build_a, CLOSURE),
                                       (self.build_b, CLOSURE_OTHER), extra=("--floor", "000002"))

    def test_a_build_that_cannot_be_resolved_stops_before_any_job(self):
        refused = hx_floor.FloorError("no commit 'nope' in this checkout")
        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(hx_floor, "resolve", side_effect=refused), \
                mock.patch.object(hx_floor, "client") as client:
            status, _, err = run_quietly(hx_bench.main, ["nope", SHA_OTHER, "--device", DEVICE, "--scene", "light",
                                                         "--floor", "000002"])
        self.assertEqual(status, 2)
        self.assertIn("no commit 'nope'", err)
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
