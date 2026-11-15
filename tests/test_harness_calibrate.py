"""scripts/harness/hx_calibrate.py (hx calibrate): one build in both arms, as one bench job, run two series against the newest
recorded floor of the device, scene and method: none (A vs A, expects no change) and delay (arm B's runs planted with
RX_VK_SUBMIT_DELAY_US, expects slower). Each series is judged by hx_bench.judge and recorded as one calibration run. Fakes
only: ssh, the job, the launch, the hold, the journal and the suite's load are stand-ins; the numbers are synthetic. No game,
GPU or host is read.

- R1 (code part): none gets no change and delay gets slower, each series recorded as a match, with no skip list read or
  recorded. The plant reaches only arm B's runs, timed and correctness, of its own series. The delay's size is the smallest
  whole number of microseconds at or above 2 x floor x T, at least 1: worked numbers. A mismatch is recorded and nothing is
  rerun, and the job exits 1; a failed series is recorded and the later series still run. No recorded floor of the device,
  scene and method exits 2 before any run, nor does a newest floor measured on another build, nor a device environment that
  turns RX_VK_ASYNC on (the delay is planted only on the synchronous replay). Every record passes hx_verify's check. The client's job runs calibrate --in-job
  with one build in both arms.

python -I -B tests/test_harness_calibrate.py
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
sys.path.insert(0, str(HARNESS))
import config
import hx_calibrate
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
OTHER_SHA = "9d2e4f6a8b0c1d3e5f7a9b1c3d5e7f9a1b3c5d7e"
CLOSURE = "/nix/store/q4kx1h7v3d2m9b6n8c5p0r1s2t3u4w5y-hookshot-1.0"
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
METHOD = verdict.load_method(SUITE_WITH_METHOD, "suite.toml")
PLAN = verdict.plan(METHOD.runs_per_arm)
DELAY_KEY = "RX_VK_SUBMIT_DELAY_US"
SERIES = ("none", "delay")
EXPECTED = {"none": "no change", "delay": "slower"}
FRAME_MS = 16.5
SLOWER_FRAME_MS = 18.15


def sha(data):
    return hashlib.sha256(data).hexdigest()


def frame_csv(values):
    """A frame log: the header, then frame n (from 1) with values[n - 1] as its frametime_ms"""
    lines = ["frame,time_s,frametime_ms"] + ["%d,%.4f,%.3f" % (n, n / 60, ms) for n, ms in enumerate(values, 1)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def timed_files(frame_ms=FRAME_MS, log=b"vk: ok\n"):
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


def env_of(argv):
    """The NAME=VALUE pairs of an argv's --env options, by name"""
    pairs = [argv[i + 1] for i, word in enumerate(argv[:-1]) if word == "--env"]
    return dict(pair.split("=", 1) for pair in pairs)


def run_quietly(fn, *args, **kwargs):
    """fn's result with its stdout and stderr captured: (result, stdout, stderr)"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        result = fn(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


class PlantTest(unittest.TestCase):
    def test_the_delay_is_two_times_the_floor_times_the_median_frame_time_in_us(self):
        # 2 x 0.02 x 16.5 ms = 0.66 ms = 660 us
        self.assertEqual(hx_calibrate.plant_us(0.02, [16.5] * 5, [16.5] * 5), 660)

    def test_a_bound_that_is_not_a_whole_number_of_microseconds_is_rounded_up(self):
        # 2 x 0.0213 x 16500 us = 702.9 us, so 703
        self.assertEqual(hx_calibrate.plant_us(0.0213, [16.5] * 5, [16.5] * 5), 703)

    def test_a_bound_that_is_a_whole_number_of_microseconds_is_not_rounded_up(self):
        # 2 x 0.01 x 16500 us = 330 us exactly
        self.assertEqual(hx_calibrate.plant_us(0.01, [16.5] * 5, [16.5] * 5), 330)

    def test_the_median_is_taken_over_both_arms_run_medians(self):
        # ten run medians, five at 10 ms and five at 20 ms: their median is 15 ms = 15000 us; 2 x 0.01 x 15000 = 300
        self.assertEqual(hx_calibrate.plant_us(0.01, [10.0] * 5, [20.0] * 5), 300)

    def test_the_delay_is_at_least_one_microsecond(self):
        self.assertEqual(hx_calibrate.plant_us(0.0, [16.5] * 5, [16.5] * 5), 1)


class CalibrateCase(unittest.TestCase):
    """A temporary harness root with a recorded floor, the in-job half's reads and waits faked, and each run's launch a fake
    that writes its record run through record.py, with the frame times its planted environment makes."""

    base: Path
    root: str
    results: Path
    lines: list[str]
    build: str
    suite: scenes.Suite
    delay_takes_effect: bool
    failing: set[int]
    launches: int
    argvs: list[list[str]]
    jobs_started: int
    real_write_verdict: Callable[..., str]
    floor_run: str

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = str(self.base / "root")
        self.results = runrecord.results_dir(self.root)
        self.lines = []
        self.build = str(self.base / "build")
        make_build(self.build, SHA)
        self.suite = scenes.Suite(path="suite.toml", sha256=sha(SUITE_WITH_METHOD),
                                  scenes=scenes.parse(SCENES_ONLY, "suite.toml").scenes)
        self.delay_takes_effect = True
        self.failing = set()
        self.launches, self.argvs = 0, []
        self.jobs_started = 0
        self.patch(hx_run, "load_scene", side_effect=lambda root, name, final: (
            SUITE_WITH_METHOD, self.suite, self.suite.scenes[name]))
        self.patch(hx_floor, "validation_available", return_value=False)
        self.patch(shutil, "which", return_value=None)
        self.patch(jobs, "wait_hold", return_value=0.0)
        self.patch(hx_floor, "launch", side_effect=self.launch)
        self.real_write_verdict = hx_floor.write_verdict
        self.patch(hx_floor, "write_verdict", side_effect=lambda *args: self.real_write_verdict(
            *args, journal=self.lines.append))
        self.floor_run = self.record_floor()

    def patch(self, target, attribute, **kwargs):
        p = mock.patch.object(target, attribute, **kwargs)
        mocked = p.start()
        self.addCleanup(p.stop)
        return mocked

    def write_run(self, files):
        """A record run whose facts name the run they came from, so a calibration record's facts show which run gave them"""
        facts = {key: {"value": key, "run": len(record.run_ids(self.results)) + 1} for key in record.FACT_KEYS}
        run_id, _ = record.write_run(self.results, facts, files, {}, journal=self.lines.append)
        return run_id

    def record_file(self, run_id, name):
        return (self.results / "runs" / run_id / "files" / name).read_bytes()

    def run_manifest(self, run_id):
        manifest, _ = record.read_manifest(self.results, run_id)
        return manifest

    def hx_verify_problems(self):
        problems, _ = hx_verify.check(self.results, record.parse_journal("\n".join(self.lines)))
        return problems

    def calibration_ids(self):
        return [run_id for run_id in record.run_ids(self.results)
                if "calibration.json" in self.run_manifest(run_id)["files"]]

    def calibrations(self):
        """Each calibration record's payload, in record order"""
        return [json.loads(self.record_file(run_id, "calibration.json")) for run_id in self.calibration_ids()]

    def series_argvs(self, number):
        """The argvs of the number-th series of the job (from 0), in plan order"""
        return self.argvs[len(PLAN) * number:len(PLAN) * (number + 1)]

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
        """A run's launch as a fake. Arm B's timed run is slower when the run carries the submit delay and the delay takes
        effect (a mismatch test turns that off); the launch number is 1 for the first run of the job."""
        self.launches += 1
        self.argvs.append(argv)
        flag = "--in-job" if "--in-job" in argv else "--correctness-in-job"
        sub = argv[argv.index(flag) + 1]
        arm, kind = PLAN[int(os.path.basename(sub))]
        os.makedirs(os.path.join(sub, "game"))
        delayed = arm == "B" and kind == "timed" and DELAY_KEY in env_of(argv) and self.delay_takes_effect
        run_id = self.write_run(timed_files(frame_ms=SLOWER_FRAME_MS if delayed else FRAME_MS))
        return (1 if self.launches in self.failing else 0), run_id

    def calibrate(self, scene="light", device=DEVICE, env=("EXAMPLE_FLAG=1",)):
        """hx calibrate's in-job half in a job folder of its own: (status, stdout, stderr). One build is both arms; env is the
        device's --env pairs, NAME=VALUE each."""
        self.jobs_started += 1
        self.launches, self.argvs = 0, []
        job = str(self.base / "jobs" / ("%s-%d" % (JOB_ID, self.jobs_started)))
        os.makedirs(job)
        argv = ["--in-job", job, "--hold", str(self.base / "hold"), "--root", self.root, "--enter-closure", "/enter",
                "--a-folder", self.build, "--a-closure", CLOSURE, "--b-folder", self.build, "--b-closure", CLOSURE,
                "--device", device, "--expect", EXPECT]
        for pair in env:
            argv += ["--env", pair]
        argv += ["--scene", scene]
        return run_quietly(hx_calibrate.main, argv)


class CalibrateTest(CalibrateCase):
    def test_each_series_is_recorded_with_its_expected_call_as_a_match(self):
        status, out, _ = self.calibrate()
        self.assertEqual(status, 0)
        found = [(p["name"], p["expected"], p["actual"], p["match"]) for p in self.calibrations()]
        self.assertEqual(found, [("none", "no change", "no change", True), ("delay", "slower", "slower", True)])
        self.assertIn("none: no change (expected no change): match", out)
        self.assertIn("delay: slower (expected slower): match", out)
        self.assertEqual(self.hx_verify_problems(), [])

    def test_the_plant_reaches_only_arm_b_timed_and_correctness_runs_of_its_own_series(self):
        status, _, _ = self.calibrate()
        self.assertEqual(status, 0)
        self.assertEqual(len(self.argvs), len(SERIES) * len(PLAN))
        plants = {"none": {}, "delay": {DELAY_KEY: "660"}}
        for number, series in enumerate(SERIES):
            for (arm, kind), argv in zip(PLAN, self.series_argvs(number)):
                planted = {k: v for k, v in env_of(argv).items() if k == DELAY_KEY}
                self.assertEqual(planted, plants[series] if arm == "B" else {}, (series, arm, kind))

    def test_the_plant_is_an_env_pair_before_the_scene_and_the_run_keeps_its_own_environment(self):
        status, _, _ = self.calibrate()
        self.assertEqual(status, 0)
        delayed = self.series_argvs(1)[PLAN.index(("B", "timed"))]
        self.assertEqual(delayed[delayed.index("--scene") - 2:delayed.index("--scene") + 2],
                         ["--env", DELAY_KEY + "=660", "--scene", "light"])
        self.assertEqual(env_of(delayed)["EXAMPLE_FLAG"], "1")
        self.assertEqual(env_of(delayed)["HOS_NO_PACING"], "1")

    def test_each_series_runs_each_run_once_with_no_floor_measured(self):
        status, _, _ = self.calibrate()
        self.assertEqual(status, 0)
        self.assertEqual(self.launches, len(SERIES) * len(PLAN))
        for payload in self.calibrations():
            self.assertEqual([(e["arm"], e["kind"]) for e in payload["series"]], PLAN, payload["name"])
            self.assertEqual([e["index"] for e in payload["series"]], list(range(len(PLAN))), payload["name"])
        self.assertEqual({p["floor_run"] for p in self.calibrations()}, {self.floor_run})

    def test_the_delay_is_sized_from_the_newest_floor_of_the_device_scene_and_method(self):
        self.record_floor(a_medians=[33.0] * 5, b_medians=[33.0] * 5)
        status, _, _ = self.calibrate()
        self.assertEqual(status, 0)
        self.assertEqual(env_of(self.series_argvs(1)[PLAN.index(("B", "timed"))])[DELAY_KEY], "1320")
        self.assertEqual({p["match"] for p in self.calibrations()}, {True})

    def test_a_newer_floor_of_another_device_is_passed_over(self):
        self.record_floor(device="other-device")
        status, _, _ = self.calibrate()
        self.assertEqual(status, 0)
        self.assertEqual({p["floor_run"] for p in self.calibrations()}, {self.floor_run})

    def test_a_newer_floor_of_another_scene_is_passed_over(self):
        self.record_floor(scene="medium")
        status, _, _ = self.calibrate()
        self.assertEqual(status, 0)
        self.assertEqual({p["floor_run"] for p in self.calibrations()}, {self.floor_run})

    def test_a_newer_floor_that_failed_is_passed_over(self):
        self.record_floor(outcome="failed", reason="run 0 (arm A, timed) ended with status 1", floor=None, interval=None,
                          a_medians=None, b_medians=None)
        status, _, _ = self.calibrate()
        self.assertEqual(status, 0)
        self.assertEqual({p["floor_run"] for p in self.calibrations()}, {self.floor_run})

    def test_a_mismatch_is_recorded_and_nothing_is_rerun(self):
        self.delay_takes_effect = False
        status, out, _ = self.calibrate()
        self.assertEqual(status, 1)
        found = [(p["name"], p["actual"], p["match"]) for p in self.calibrations()]
        self.assertEqual(found, [("none", "no change", True), ("delay", "no change", False)])
        self.assertIn("delay: no change (expected slower): MISMATCH", out)
        self.assertEqual(self.launches, len(SERIES) * len(PLAN))
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_failed_series_is_recorded_failed_and_the_later_series_still_run_without_a_rerun(self):
        self.failing = {4}
        status, _, _ = self.calibrate()
        self.assertEqual(status, 1)
        found = [(p["name"], p["actual"], p["match"]) for p in self.calibrations()]
        self.assertEqual(found, [("none", "failed", False), ("delay", "slower", True)])
        self.assertIn("ended with status 1", self.calibrations()[0]["reason"])
        self.assertEqual(self.launches, 4 + len(PLAN))
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_record_that_is_not_written_stops_the_job_with_exit_2(self):
        with mock.patch.object(hx_floor, "write_verdict", side_effect=record.RecordError("disk full")):
            status, _, err = self.calibrate()
        self.assertEqual(status, 2)
        self.assertEqual(self.launches, len(PLAN))
        self.assertIn("not recorded", err)

    def test_no_recorded_floor_exits_2_before_any_run(self):
        shutil.rmtree(self.results)
        status, _, err = self.calibrate()
        self.assertEqual(status, 2)
        self.assertEqual(self.launches, 0)
        self.assertIn("no recorded floor", err)
        self.assertEqual(self.calibration_ids(), [])

    def test_a_foreign_floor_alone_exits_2_before_any_run(self):
        shutil.rmtree(self.results)
        self.record_floor(device="other-device")
        status, _, err = self.calibrate()
        self.assertEqual(status, 2)
        self.assertEqual(self.launches, 0)
        self.assertIn("no recorded floor", err)

    def test_a_floor_of_another_method_alone_exits_2_before_any_run(self):
        shutil.rmtree(self.results)
        self.record_floor(method=dict(METHOD_DICT, seed=8))
        status, _, err = self.calibrate()
        self.assertEqual(status, 2)
        self.assertEqual(self.launches, 0)
        self.assertIn("no recorded floor", err)

    def test_a_newest_floor_of_another_build_exits_2_before_any_run(self):
        other = self.record_floor(a_sha=OTHER_SHA, b_sha=OTHER_SHA)
        status, _, err = self.calibrate()
        self.assertEqual(status, 2)
        self.assertEqual(self.launches, 0)
        self.assertIn("could not run", err)
        self.assertIn(other, err)
        self.assertIn(OTHER_SHA, err)
        self.assertIn(SHA, err)
        self.assertEqual(self.calibration_ids(), [])

    def test_only_the_newest_floor_is_compared_with_this_build(self):
        self.record_floor(a_sha=OTHER_SHA, b_sha=OTHER_SHA)
        newest = self.record_floor()
        status, _, _ = self.calibrate()
        self.assertEqual(status, 0)
        self.assertEqual({p["floor_run"] for p in self.calibrations()}, {newest})

    def test_a_newer_floor_of_another_build_of_another_device_is_passed_over(self):
        self.record_floor(device="other-device", a_sha=OTHER_SHA, b_sha=OTHER_SHA)
        status, _, _ = self.calibrate()
        self.assertEqual(status, 0)
        self.assertEqual({p["floor_run"] for p in self.calibrations()}, {self.floor_run})

    def test_rx_vk_async_on_in_the_device_env_exits_2_before_any_run(self):
        status, _, err = self.calibrate(env=("EXAMPLE_FLAG=1", "RX_VK_ASYNC=1"))
        self.assertEqual(status, 2)
        self.assertEqual(self.launches, 0)
        self.assertIn("could not run", err)
        self.assertIn("RX_VK_ASYNC=1", err)
        self.assertEqual(self.calibration_ids(), [])

    def test_each_value_the_renderer_reads_as_on_exits_2_before_any_run(self):
        # on is atoi(value) != 0 (gpu/vk/vk_replay.cpp:4616-4621): a nonzero integer after any blanks, trailing text aside
        for value in ("1", "2", "-1", "+1", " 1", "1x", "3.5"):
            with self.subTest(value=value):
                status, _, _ = self.calibrate(env=("RX_VK_ASYNC=" + value,))
                self.assertEqual(status, 2)
                self.assertEqual(self.launches, 0)

    def test_each_value_the_renderer_reads_as_off_runs_as_before(self):
        for value in ("0", "", "abc", "0x1", "0.5", " 0", "-0"):
            with self.subTest(value=value):
                status, _, _ = self.calibrate(env=("RX_VK_ASYNC=" + value,))
                self.assertEqual(status, 0)
                self.assertEqual(self.launches, len(SERIES) * len(PLAN))

    def test_the_last_rx_vk_async_pair_is_the_one_the_runs_get(self):
        status, _, _ = self.calibrate(env=("RX_VK_ASYNC=1", "RX_VK_ASYNC=0"))
        self.assertEqual(status, 0)
        status, _, err = self.calibrate(env=("RX_VK_ASYNC=0", "RX_VK_ASYNC=1"))
        self.assertEqual(status, 2)
        self.assertIn("RX_VK_ASYNC=1", err)
        self.assertEqual(self.launches, 0)

    def test_the_calibration_needs_no_skip_list_and_records_no_skip(self):
        status, _, _ = self.calibrate()
        self.assertEqual(status, 0)
        self.assertEqual([p["name"] for p in self.calibrations()], ["none", "delay"])
        for payload in self.calibrations():
            self.assertNotIn("skip", payload, payload["name"])


class ClientTest(unittest.TestCase):
    cfg: config.Config
    build: str

    def setUp(self):
        self.cfg = config.Config(path="/cfg", host=HOST, hold="/hold", root="/root", dump="/dump",
                                 enter_closure="/enter",
                                 devices={DEVICE: config.Device(name=DEVICE, expect=EXPECT, env=DEVICE_ENV)})
        self.build = "/root/builds/" + SHA

    def test_the_job_runs_calibrate_in_job_with_one_build_in_both_arms(self):
        start, follow = mock.Mock(), mock.Mock(return_value=0)
        suite_text = types.SimpleNamespace(stdout=SUITE_WITH_METHOD.decode("utf-8"))
        with mock.patch.object(hx_floor.remote, "run", return_value=suite_text), \
                mock.patch.object(hx_floor.jobs, "new_id", return_value=JOB_ID):
            status, _, _ = run_quietly(hx_floor.client, "calibrate", self.cfg, DEVICE, "light",
                                       (self.build, CLOSURE), (self.build, CLOSURE), start=start, follow=follow)
        self.assertEqual(status, 0)
        job = "/root/jobs/" + JOB_ID
        argv = start.call_args.args[2]
        self.assertEqual(argv, [
            "/enter", CLOSURE, "python", "-I", job + "/tools/scripts/harness/hx.py", "calibrate", "--in-job", job,
            "--hold", "/hold", "--root", "/root", "--enter-closure", "/enter", "--a-folder", self.build,
            "--a-closure", CLOSURE, "--b-folder", self.build, "--b-closure", CLOSURE, "--device", DEVICE,
            "--expect", EXPECT, "--env", "EXAMPLE_FLAG=1", "--scene", "light"])
        parsed = hx_calibrate.parse_in_job(argv[argv.index("--in-job") + 1:])
        self.assertEqual((parsed.a_folder, parsed.b_folder, parsed.a_closure, parsed.b_closure, parsed.scene),
                         (self.build, self.build, CLOSURE, CLOSURE, "light"))

    def test_main_resolves_the_build_and_starts_the_calibrate_job(self):
        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(hx_floor, "resolve", return_value=(self.build, SHA, CLOSURE)) as resolve, \
                mock.patch.object(hx_floor, "client", return_value=1) as client:
            status, _, _ = run_quietly(hx_calibrate.main, ["--device", DEVICE, "--scene", "light", "--build", SHA])
        self.assertEqual(status, 1)
        resolve.assert_called_once_with(self.cfg, SHA)
        client.assert_called_once_with("calibrate", self.cfg, DEVICE, "light", (self.build, CLOSURE),
                                       (self.build, CLOSURE))

    def test_a_build_that_cannot_be_resolved_stops_before_any_job(self):
        refused = hx_floor.FloorError("no commit 'nope' in this checkout")
        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(hx_floor, "resolve", side_effect=refused), \
                mock.patch.object(hx_floor, "client") as client:
            status, _, err = run_quietly(hx_calibrate.main, ["--device", DEVICE, "--scene", "light", "--build", "nope"])
        self.assertEqual(status, 2)
        self.assertIn("no commit 'nope'", err)
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
