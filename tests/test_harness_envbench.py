"""scripts/harness/hx_envbench.py (hx envbench): one build benched against itself as one bench job, with NAME=VALUE pairs added
to arm B's runs only (timed and correctness), judged against a recorded floor as hx bench judges it, and recorded as one
envbench run. Fakes only: ssh, the job, the launch, the hold, the journal and the suite's load are stand-ins; the numbers
are synthetic, and the pair names are fake. No game, GPU or host is read.

- R3 (code part): each --b-env pair reaches arm B's timed and correctness runs, as --env before --scene, and no arm A run.
  The series is judged as hx bench judges it, and the call is recorded with the pairs as b_env. A void or failed series is
  recorded and exits 1, and nothing is rerun. The correctness runs' pictures decide nothing (DL185), so pictures that
  differ leave the call to the timing. A floor of another device, scene or method, or one that is not a recorded
  floor, exits 2 before any run. Every envbench record passes hx_verify's check. The client's one job runs envbench --in-job
  with one build in both arms, the floor and the pairs. A malformed, missing or repeated --b-env is a usage error.

python -I -B tests/test_harness_envbench.py
"""
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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
sys.path.insert(0, str(HARNESS))
import config
import hx_envbench
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
CLOSURE = "/nix/store/q4kx1h7v3d2m9b6n8c5p0r1s2t3u4w5y-hookshot-1.0"
DEVICE = "gpu"
EXPECT = "Example GPU"
DEVICE_ENV = {"EXAMPLE_FLAG": "1"}
JOB_ID = "bench-20261114T000000Z-abcdef"
FLOOR_ID = "000147"
PAIR, OTHER_PAIR = "EXAMPLE_WAIT", "EXAMPLE_MODE"
FRAME_MS = 16.5
SLOWER_FRAME_MS = 18.15
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
CORRECTNESS_A = PLAN.index(("A", "correctness"))
CORRECTNESS_B = PLAN.index(("B", "correctness"))


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


def frame_csv(values):
    """A frame log: the header, then frame n (from 1) with values[n - 1] as its frametime_ms"""
    lines = ["frame,time_s,frametime_ms"] + ["%d,%.4f,%.3f" % (n, n / 60, ms) for n, ms in enumerate(values, 1)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def timed_files(frame_ms=FRAME_MS):
    """A run's files as hx run writes them, for a window of 60 frames each frame_ms long"""
    return {
        "log.txt": b"vk: ok\n",
        "frametimes.csv": frame_csv([frame_ms] * 60),
        "scene.json": hx_run.dump({"window": {"map": "Area", "first": 1, "last": 60}}),
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


class EnvbenchCase(unittest.TestCase):
    """A temporary harness root with a recorded floor, the in-job half's reads and waits faked, and each run a fake that
    writes its record run through record.py, so hx_verify can check the whole record. Both arms are one build; the fake
    slows arm B's timed runs when a pair the fake responds to is planted there."""

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
        self.responds_to = {PAIR}
        self.statuses, self.correct_shots = {}, {}
        self.launched, self.argvs = [], {}
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

    def write_run(self, files, shots=None):
        """A record run whose facts name the run they came from, so an envbench record's facts show which run gave them"""
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
        return json.loads(self.record_file(run_id, "envbench.json"))

    def latest(self):
        return record.run_ids(self.results)[-1]

    def floor_payload(self, **changes):
        """A floor's payload as hx floor writes it for a recorded floor, with the changes"""
        payload = {
            "kind": "floor", "outcome": "recorded", "reason": None, "device": DEVICE, "expect": EXPECT,
            "scene": "light", "a_sha": SHA, "b_sha": SHA, "suite_sha256": sha(SUITE_WITH_METHOD),
            "method": dict(METHOD_DICT), "validation": False, "series": [], "floor": FLOOR,
            "interval": [-0.01, 0.01], "a_medians": [FRAME_MS] * 5, "b_medians": [FRAME_MS] * 5,
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
        """A run's launch as a fake: the run's arm and kind are its position's in the plan, since both arms share one build
        folder. Arm B's timed run is slower when it carries a pair the fake responds to. A correctness run writes its log
        and the pictures correct_shots gives its index, if any: none by default, as hx floor's runs take none."""
        flag = "--in-job" if "--in-job" in argv else "--correctness-in-job"
        sub = argv[argv.index(flag) + 1]
        index = int(os.path.basename(sub))
        arm, kind = PLAN[index]
        self.launched.append(index)
        self.argvs[index] = argv
        os.makedirs(os.path.join(sub, "game"))
        if kind == "timed":
            slowed = arm == "B" and bool(self.responds_to & set(env_of(argv)))
            run_id = self.write_run(timed_files(frame_ms=SLOWER_FRAME_MS if slowed else FRAME_MS))
        else:
            run_id = self.write_run(timed_files(), self.correct_shots.get(index))
        return self.statuses.get(index, 0), run_id

    def envbench(self, b_env=(PAIR + "=1",), scene="light", device=DEVICE, floor=None):
        """hx envbench's in-job half in a job folder of its own: (status, stdout, stderr). One build is both arms, and the
        floor is the recorded floor of the fixture unless one is named."""
        self.jobs_started += 1
        self.launched, self.argvs = [], {}
        job = str(self.base / "jobs" / ("%s-%d" % (JOB_ID, self.jobs_started)))
        os.makedirs(job)
        argv = ["--in-job", job, "--hold", str(self.base / "hold"), "--root", self.root, "--enter-closure", "/enter",
                "--a-folder", self.build, "--a-closure", CLOSURE, "--b-folder", self.build, "--b-closure", CLOSURE,
                "--device", device, "--expect", EXPECT, "--env", "EXAMPLE_FLAG=1", "--scene", scene,
                "--floor", floor or self.floor_run]
        for pair in b_env:
            argv += ["--b-env", pair]
        return run_quietly(hx_envbench.main, argv)


class PairTest(EnvbenchCase):
    def test_each_pair_reaches_every_run_of_arm_b_and_no_run_of_arm_a(self):
        status, _, _ = self.envbench(b_env=(PAIR + "=1", OTHER_PAIR + "=fast"))
        self.assertEqual(status, 0)
        self.assertEqual(self.launched, list(range(len(PLAN))))
        for index, (arm, kind) in enumerate(PLAN):
            env = env_of(self.argvs[index])
            if arm == "B":
                self.assertEqual((env[PAIR], env[OTHER_PAIR]), ("1", "fast"), (index, kind))
            else:
                self.assertNotIn(PAIR, env, (index, kind))
                self.assertNotIn(OTHER_PAIR, env, (index, kind))

    def test_each_pair_is_an_env_option_just_before_the_scene(self):
        status, _, _ = self.envbench(b_env=(PAIR + "=1", OTHER_PAIR + "=fast"))
        self.assertEqual(status, 0)
        for index, (arm, kind) in enumerate(PLAN):
            argv = self.argvs[index]
            at = argv.index("--scene")
            if arm == "B":
                self.assertEqual(argv[at - 4:at + 2], ["--env", PAIR + "=1", "--env", OTHER_PAIR + "=fast", "--scene",
                                                       "light"], (index, kind))
            else:
                self.assertEqual(argv[at:at + 2], ["--scene", "light"], (index, kind))
                self.assertNotIn(PAIR + "=1", argv, (index, kind))

    def test_the_device_environment_stays_in_every_run(self):
        status, _, _ = self.envbench()
        self.assertEqual(status, 0)
        for index in range(len(PLAN)):
            self.assertEqual(env_of(self.argvs[index])["EXAMPLE_FLAG"], "1", index)


class VerdictTest(EnvbenchCase):
    def test_a_pair_that_slows_arm_b_is_recorded_slower_against_the_floor(self):
        status, out, _ = self.envbench()
        run_id = self.latest()
        self.assertEqual(status, 0)
        self.assertIn("envbench: " + run_id, out)
        self.assertIn("verdict: slower (interval +10.00% to +10.00%, floor 2.00%)", out)
        payload = self.payload(run_id)
        self.assertEqual(payload["kind"], "envbench")
        self.assertEqual(payload["outcome"], "slower")
        self.assertEqual(payload["b_env"], {PAIR: "1"})
        self.assertIsNone(payload["reason"])
        self.assertEqual(payload["void_reasons"], [])
        low, high = payload["interval"]
        self.assertAlmostEqual(low, 0.1, places=6)
        self.assertAlmostEqual(high, 0.1, places=6)
        self.assertEqual(len(payload["a_medians"]), 5)
        self.assertEqual(len(payload["b_medians"]), 5)
        self.assertEqual([(e["arm"], e["kind"]) for e in payload["series"]], PLAN)
        self.assertEqual((payload["device"], payload["expect"], payload["scene"]), (DEVICE, EXPECT, "light"))
        self.assertEqual((payload["a_sha"], payload["b_sha"]), (SHA, SHA))
        self.assertEqual(payload["floor_run"], self.floor_run)
        self.assertEqual(payload["floor"], FLOOR)
        self.assertEqual(payload["method"], METHOD_DICT)
        self.assertEqual(self.record_file(run_id, "suite.toml"), SUITE_WITH_METHOD)
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_pair_that_changes_nothing_is_recorded_no_change(self):
        self.responds_to = set()
        status, out, _ = self.envbench()
        self.assertEqual(status, 0)
        self.assertIn("verdict: no change", out)
        payload = self.payload(self.latest())
        self.assertEqual((payload["kind"], payload["outcome"], payload["b_env"]), ("envbench", "no change", {PAIR: "1"}))
        self.assertEqual(self.hx_verify_problems(), [])

    def test_the_series_runs_each_run_once_and_is_judged_against_the_recorded_floor(self):
        status, _, _ = self.envbench()
        self.assertEqual(status, 0)
        self.assertEqual(self.launched, list(range(len(PLAN))))
        payload = self.payload(self.latest())
        self.assertEqual((payload["floor"], payload["floor_run"]), (FLOOR, self.floor_run))


class VoidAndFailedTest(EnvbenchCase):
    def test_pictures_that_differ_do_not_void_the_envbench_and_the_timing_decides(self):
        self.correct_shots = {CORRECTNESS_A: {"shot_1.png": SHOT_A}, CORRECTNESS_B: {"shot_1.png": SHOT_B}}
        status, out, _ = self.envbench()
        self.assertEqual(status, 0)
        self.assertIn("verdict: slower", out)
        payload = self.payload(self.latest())
        self.assertEqual((payload["kind"], payload["outcome"], payload["void_reasons"]), ("envbench", "slower", []))
        self.assertNotIn("image", payload)
        self.assertEqual(self.launched, list(range(len(PLAN))))
        self.assertEqual(self.hx_verify_problems(), [])

    def test_a_failed_run_is_recorded_failed_exits_1_and_the_series_is_not_rerun(self):
        self.statuses = {3: 1}
        status, out, _ = self.envbench()
        self.assertEqual(status, 1)
        self.assertIn("verdict: failed", out)
        payload = self.payload(self.latest())
        self.assertEqual((payload["kind"], payload["outcome"]), ("envbench", "failed"))
        self.assertIn("run 3 (arm A, timed) ended with status 1", payload["reason"])
        self.assertEqual([e["index"] for e in payload["series"]], [0, 1, 2, 3])
        self.assertEqual(self.launched, [0, 1, 2, 3])
        for key in ("interval", "a_medians", "b_medians"):
            self.assertIsNone(payload[key], key)
        self.assertEqual(self.hx_verify_problems(), [])


class RefusalTest(EnvbenchCase):
    """A floor that is not this device, scene and method's recorded floor is refused before any run: exit 2, no run
    started, nothing recorded."""

    def refused(self, floor, fragment):
        before = record.run_ids(self.results)
        status, _, err = self.envbench(floor=floor)
        self.assertEqual(status, 2)
        self.assertIn(fragment, err)
        self.assertEqual(self.launched, [])
        self.assertEqual(record.run_ids(self.results), before)

    def test_a_floor_of_another_device_is_refused(self):
        self.refused(self.record_floor(device="other-gpu"), "device")

    def test_a_floor_of_another_scene_is_refused(self):
        self.refused(self.record_floor(scene="medium"), "scene")

    def test_a_floor_of_another_method_is_refused(self):
        self.refused(self.record_floor(method={**METHOD_DICT, "seed": 8}), "method")

    def test_a_floor_that_is_not_recorded_is_refused(self):
        self.refused(self.record_floor(outcome="failed"), "not 'recorded'")

    def test_a_record_that_is_not_a_floor_is_refused(self):
        self.refused(self.record_floor(kind="bench"), "not a floor")

    def test_a_floor_id_that_is_not_in_the_record_is_refused(self):
        self.refused("000999", "cannot read its manifest")


class ArgvTest(unittest.TestCase):
    CLIENT = ["--device", DEVICE, "--scene", "light", "--floor", FLOOR_ID]

    def test_the_client_takes_a_device_a_scene_a_floor_pairs_and_an_optional_build(self):
        a = hx_envbench.parse_client(self.CLIENT + ["--b-env", PAIR + "=1", "--b-env", OTHER_PAIR + "=fast",
                                                    "--build", SHA])
        self.assertEqual((a.device, a.scene, a.floor, a.build), (DEVICE, "light", FLOOR_ID, SHA))
        self.assertEqual(a.b_env, [(PAIR, "1"), (OTHER_PAIR, "fast")])

    def test_without_a_build_the_build_is_none_for_the_newest(self):
        self.assertIsNone(hx_envbench.parse_client(self.CLIENT + ["--b-env", PAIR + "=1"]).build)

    def test_a_value_may_contain_equals_signs(self):
        a = hx_envbench.parse_client(self.CLIENT + ["--b-env", PAIR + "=a=b"])
        self.assertEqual(a.b_env, [(PAIR, "a=b")])

    def test_b_env_is_required(self):
        with self.assertRaises(SystemExit) as cm:
            run_quietly(hx_envbench.parse_client, self.CLIENT)
        self.assertEqual(cm.exception.code, 2)

    def test_a_malformed_b_env_is_a_usage_error(self):
        for bad in (PAIR, "=1"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as cm:
                run_quietly(hx_envbench.parse_client, self.CLIENT + ["--b-env", bad])
            self.assertEqual(cm.exception.code, 2)

    def test_a_b_env_name_given_twice_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm:
            run_quietly(hx_envbench.parse_client, self.CLIENT + ["--b-env", PAIR + "=1", "--b-env", PAIR + "=2"])
        self.assertEqual(cm.exception.code, 2)

    def test_a_floor_id_that_is_not_six_digits_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as cm:
            run_quietly(hx_envbench.parse_client, ["--device", DEVICE, "--scene", "light", "--floor", "12",
                                                   "--b-env", PAIR + "=1"])
        self.assertEqual(cm.exception.code, 2)

    def test_the_in_job_half_refuses_a_repeated_pair(self):
        argv = ["/root/jobs/" + JOB_ID, "--hold", "/hold", "--root", "/root", "--enter-closure", "/enter",
                "--a-folder", "/a", "--a-closure", CLOSURE, "--b-folder", "/a", "--b-closure", CLOSURE,
                "--device", DEVICE, "--expect", EXPECT, "--scene", "light", "--floor", FLOOR_ID,
                "--b-env", PAIR + "=1", "--b-env", PAIR + "=2"]
        with self.assertRaises(SystemExit) as cm:
            run_quietly(hx_envbench.parse_in_job, argv)
        self.assertEqual(cm.exception.code, 2)


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config(path="/cfg", host=HOST, hold="/hold", root="/root", dump="/dump",
                                 enter_closure="/enter",
                                 devices={DEVICE: config.Device(name=DEVICE, expect=EXPECT, env=DEVICE_ENV)})
        self.build = "/root/builds/" + SHA

    def test_the_job_runs_envbench_in_job_with_one_build_in_both_arms_the_floor_and_the_pairs(self):
        start, follow = mock.Mock(), mock.Mock(return_value=0)
        suite_text = types.SimpleNamespace(stdout=SUITE_WITH_METHOD.decode("utf-8"))
        real_client = hx_floor.client

        def client_with_fake_job(*args, **kwargs):
            return real_client(*args, start=start, follow=follow, **kwargs)

        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(hx_floor, "resolve", return_value=(self.build, SHA, CLOSURE)), \
                mock.patch.object(hx_floor.remote, "run", return_value=suite_text), \
                mock.patch.object(hx_floor.jobs, "new_id", return_value=JOB_ID), \
                mock.patch.object(hx_floor, "client", side_effect=client_with_fake_job):
            status, _, _ = run_quietly(hx_envbench.main, ["--device", DEVICE, "--scene", "light", "--floor", FLOOR_ID,
                                                          "--b-env", PAIR + "=1", "--b-env", OTHER_PAIR + "=fast"])
        self.assertEqual(status, 0)
        self.assertEqual(start.call_count, 1)
        job = "/root/jobs/" + JOB_ID
        argv = start.call_args.args[2]
        self.assertEqual(argv, [
            "/enter", CLOSURE, "python", "-I", job + "/tools/scripts/harness/hx.py", "envbench", "--in-job", job,
            "--hold", "/hold", "--root", "/root", "--enter-closure", "/enter", "--a-folder", self.build,
            "--a-closure", CLOSURE, "--b-folder", self.build, "--b-closure", CLOSURE, "--device", DEVICE,
            "--expect", EXPECT, "--env", "EXAMPLE_FLAG=1", "--scene", "light", "--floor", FLOOR_ID,
            "--b-env", PAIR + "=1", "--b-env", OTHER_PAIR + "=fast"])
        parsed = hx_envbench.parse_in_job(argv[argv.index("--in-job") + 1:])
        self.assertEqual((parsed.a_folder, parsed.b_folder, parsed.a_closure, parsed.b_closure, parsed.scene, parsed.floor),
                         (self.build, self.build, CLOSURE, CLOSURE, "light", FLOOR_ID))
        self.assertEqual(parsed.b_env, [(PAIR, "1"), (OTHER_PAIR, "fast")])

    def test_a_build_that_cannot_be_resolved_stops_before_any_job(self):
        refused = hx_floor.FloorError("no commit 'nope' in this checkout")
        with mock.patch.object(config, "load", return_value=self.cfg), \
                mock.patch.object(hx_floor, "resolve", side_effect=refused), \
                mock.patch.object(hx_floor, "client") as client:
            status, _, err = run_quietly(hx_envbench.main, ["--device", DEVICE, "--scene", "light", "--floor", FLOOR_ID,
                                                            "--b-env", PAIR + "=1", "--build", "nope"])
        self.assertEqual(status, 2)
        self.assertIn("no commit 'nope'", err)
        client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
