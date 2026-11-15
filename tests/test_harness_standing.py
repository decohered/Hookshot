"""scripts/harness/hx_standing.py (hx standing): two builds' A-B-B-A timed runs as one bench job, each run under a bridgeless
driver through HOS_INPUT_LIVE, and the interval between them, recorded as unconfirmed. Fakes only: the game is a process
stand-in whose log, trace and signals the test writes, its clock is the driver's clock, and ssh, the job and the journal
are stand-ins; the numbers and names are synthetic, and no game, GPU or host is read.

- R2: the driver presses A (at most 30 presses, each followed by two new status lines) until three status lines in a row
  show at least 150 draws a frame, waits 600 presents to settle, holds the scene's seconds x 60 presents as the window, waits
  for a status line 120 presents past the window, and ends the game's process group (SIGTERM, then SIGKILL). The window's
  frame times are the trace's ms column between its bounds; the device line and the fallback are checked. A run that does
  not pass ends the series, recorded, with exit 1. Each run is a record run that hx verify accepts. The comparison is one
  record run, standing.json: unconfirmed, with the interval and each run's warm median, and no faster, slower, pass or fail
  call.

python -I -B tests/test_harness_standing.py
"""
import argparse
import contextlib
import io
import json
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
sys.path.insert(0, str(HARNESS))
import config
import hx_floor
import hx_run
import hx_standing
import hx_verify
import jobs
import record
import remote
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
JOB_ID = "bench-20261114T000000Z-abcdef"
HASHES = "%s  hookshot\n%s  hookshot_vk.so\n" % ("a" * 64, "b" * 64)
KERNEL = "kernel: quiet\n"
INTRO = 5           # draws a frame before play
PLAY = 2000         # draws a frame in play
PRESENTS = 120      # a status line every this many presents
WINDOW = (960, 1019)  # the play-first scenario: play at 360 presents, settle to 960, 60 frames from 960 to 1019
FLUSHED = 1200      # the first status line at or past 1140 (the window's last frame 1019, plus 1, plus 120 presents)

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
SUITE = (METHOD_TABLE + SCENES).encode("utf-8")
METHOD = verdict.load_method(SUITE, "suite.toml")
TRACE_HEADER = ",".join(["frame", "ms"] + ["c%d" % n for n in range(2, 20)])   # v2.0.14's trace: 20 columns
ABBA = "ABBAABBAAB"


def make_build(folder, sha_value):
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


def expected_ms(first, last, offset=0.0):
    """The ms the fake game gives frames first to last: 16.0, 16.5, 17.0 or 17.5 by frame, plus offset"""
    return [16.0 + (frame % 4) * 0.5 + offset for frame in range(first, last + 1)]


class FakeProc:
    """subprocess.Popen's stand-in for the game: its pid, and poll and wait as the game's return code says"""

    def __init__(self, game):
        self.game = game
        self.pid = game.pid

    def poll(self):
        return self.game.returncode

    def wait(self, timeout=None):
        if self.game.returncode is None:
            if timeout is not None:
                raise subprocess.TimeoutExpired("hookshot", timeout)
            raise AssertionError("waited on a game that is still running, with no timeout")
        return self.game.returncode


class FakeGame:
    """The game as a process stand-in. Each sleep the driver makes is one tick of the game: it reads the presses in its live
    file, writes one status line every PRESENTS presents (150 draws or more once it has play_after presses, INTRO before) and
    the trace's rows up to its presents, and ends when end_at ticks have passed. Its group's signals end it: SIGTERM ends it
    unless exits_on_term is false, SIGKILL always does. The frame times are 16.0, 16.5, 17.0 or 17.5 ms by frame, plus offset;
    with sentinel (first, last), the frames outside it take 500 ms, so that a window which is not the right one shows.
    Its clock is its own: each sleep advances it by the seconds slept."""

    def __init__(self, play_after=0, exits_on_term=True, lines=True, end_at=None, device=EXPECT, extra_log="",
                 offset=0.0, sentinel=None):
        self.pid = 4242
        self.play_after = play_after
        self.exits_on_term = exits_on_term
        self.lines = lines
        self.end_at = end_at
        self.device = device
        self.extra_log = extra_log
        self.offset = offset
        self.sentinel = sentinel
        self.now = 0.0
        self.ticks = 0
        self.presents = 0
        self.rows = 0
        self.returncode = None
        self.signals = []
        self.started = False
        self.argv = self.kwargs = self.env = None
        self.live_path = self.log_path = self.trace_path = None

    def popen(self, argv, **kwargs):
        assert not self.started, "the game is started once"
        self.started = True
        self.argv, self.kwargs, self.env = argv, kwargs, kwargs["env"]
        self.live_path = self.env["HOS_INPUT_LIVE"]
        self.log_path = os.path.join(os.path.dirname(self.live_path), "log.txt")
        self.trace_path = os.path.join(kwargs["cwd"], "hookshot_frames.csv")
        return FakeProc(self)

    def killpg(self, pgid, sig):
        assert pgid == self.pid, "the group is the game's own"
        if self.returncode is not None:
            raise ProcessLookupError(3, "No such process")
        self.signals.append((sig, self.presents))
        if sig == signal.SIGKILL or (sig == signal.SIGTERM and self.exits_on_term):
            self.returncode = -int(sig)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.tick()

    def tick(self):
        if self.returncode is not None:
            return
        self.ticks += 1
        if self.ticks == 1:
            self._append(self.log_path, "[hos host] vk: %s, Vulkan 1.3, driver 0x1\n%s" % (self.device, self.extra_log))
        if self.end_at is not None and self.ticks >= self.end_at:
            self.returncode = 1
            return
        if not self.lines:
            return
        presses = self.presses()
        self.presents += PRESENTS
        playing = self.play_after is not None and presses >= self.play_after
        draws = PLAY if playing else INTRO
        self._append(self.log_path, "[hos Presentation Thread] vk: %d presents, 60.0 fps; a frame: %d draws (0 skipped), "
                                    "3 dispatches\n" % (self.presents, draws))
        self._write_trace()

    def presses(self):
        """The presses in the live file: lines of 0:A"""
        if not os.path.exists(self.live_path):
            return 0
        with open(self.live_path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip() == "0:A")

    def frame_ms(self, frame):
        if self.sentinel is not None and not self.sentinel[0] <= frame <= self.sentinel[1]:
            return 500.0 + self.offset
        return 16.0 + (frame % 4) * 0.5 + self.offset

    def _write_trace(self):
        if self.rows == 0:
            self._append(self.trace_path, TRACE_HEADER + "\n")
        text = "".join("%d,%.2f%s\n" % (frame, self.frame_ms(frame), ",0" * 18)
                       for frame in range(self.rows, self.presents))
        self._append(self.trace_path, text)
        self.rows = self.presents

    @staticmethod
    def _append(path, text):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)

    def log_bytes(self):
        with open(self.log_path, "rb") as f:
            return f.read()

    def trace_bytes(self):
        with open(self.trace_path, "rb") as f:
            return f.read()


class StandingCase(unittest.TestCase):
    """A temporary harness root with the suite, a save and two builds; the game's copy and hashes are faked, and the
    record runs' journal is collected"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = str(self.base / "root")
        self.results = runrecord.results_dir(self.root)
        self.lines = []
        self.build = {"A": str(self.base / "build-a"), "B": str(self.base / "build-b")}
        make_build(self.build["A"], SHA)
        make_build(self.build["B"], SHA_OTHER)
        os.makedirs(scenes.scene_dir(self.root))
        with open(os.path.join(scenes.scene_dir(self.root), "suite.toml"), "wb") as f:
            f.write(SUITE)
        save = scenes.save_dir(self.root, "start")
        os.makedirs(os.path.join(save, "slot_01"))
        for name in ("account.sav", "option.sav"):
            with open(os.path.join(save, name), "wb") as f:
                f.write(b"")
        self.patch(hx_run, "copy_game", side_effect=fake_copy)
        self.patch(hx_run, "binary_hashes", return_value=HASHES)

    def patch(self, target, attribute, **kwargs):
        p = mock.patch.object(target, attribute, **kwargs)
        mocked = p.start()
        self.addCleanup(p.stop)
        return mocked

    def sub(self, index):
        """A sub-job folder as the series makes it: data/ and run/ (mode 0700), no game yet"""
        sub = os.path.join(self.base, "jobs", JOB_ID, "runs", "%02d" % index)
        os.makedirs(os.path.join(sub, "data"))
        os.makedirs(os.path.join(sub, "run"), mode=0o700)
        return sub

    def timed_run(self, game, index=0, arm="A", scene="light", popen=None):
        """One timed run in its own sub-job, through the game's fakes: (status, run id), stdout, stderr"""
        a = argparse.Namespace(job=self.sub(index), root=self.root, build_folder=self.build[arm], device=DEVICE,
                               expect=EXPECT, env=list(DEVICE_ENV.items()), scene=scene)
        return run_quietly(hx_standing.run_in_job, a, popen=popen or game.popen, killpg=game.killpg, clock=game.clock,
                           sleep=game.sleep, kernel=lambda launched: KERNEL, machine=lambda: {},
                           journal=self.lines.append)

    def verify(self):
        """hx verify's check of the record: (problems, runs checked)"""
        return hx_verify.check(self.results, record.parse_journal("\n".join(self.lines)))

    def record_file(self, run_id, name):
        return (self.results / "runs" / run_id / "files" / name).read_bytes()

    def standing_run(self, run_id):
        return json.loads(self.record_file(run_id, "standing_run.json"))


class PlanAndArgvTest(unittest.TestCase):
    def test_the_timed_plan_is_the_abba_cycle_of_timed_runs_only(self):
        self.assertEqual(hx_standing.timed_plan(5), [("A", "timed"), ("B", "timed"), ("B", "timed"), ("A", "timed")] * 2
                         + [("A", "timed"), ("B", "timed")])

    def test_run_argv_is_standing_run_in_job_through_the_arms_closure_and_reads_back(self):
        argv = hx_standing.run_argv("/enter", CLOSURE, "/job/tools/scripts/harness/hx.py", "/job/runs/03", "/root",
                                    "/builds/a", DEVICE, EXPECT, DEVICE_ENV, "light")
        self.assertEqual(argv[:5], ["/enter", CLOSURE, "python", "-I", "/job/tools/scripts/harness/hx.py"])
        self.assertEqual(argv[5:7], ["standing", "--run-in-job"])
        self.assertNotIn("--hold", argv)
        a = hx_standing.parse_run_in_job(argv[argv.index("--run-in-job") + 1:])
        self.assertEqual((a.job, a.root, a.build_folder, a.device, a.expect, a.scene),
                         ("/job/runs/03", "/root", "/builds/a", DEVICE, EXPECT, "light"))
        self.assertEqual(a.env, [("EXAMPLE_FLAG", "1")])

    def test_standing_env_is_the_run_conditions_without_a_bridge_and_with_no_pacing(self):
        env = hx_standing.standing_env("/job", {"EXAMPLE_FLAG": "1"})
        self.assertNotIn("HOS_CONTROL", env)
        self.assertEqual(env["HOS_NO_PACING"], "1")
        self.assertEqual((env["HOS_HEADLESS"], env["HOS_REALTIME_STEP"], env["RX_VK_FRAME_TRACE"]), ("1", "0", "1"))
        self.assertEqual(env["HOS_INPUT_LIVE"], os.path.join("/job", "run", "live.txt"))
        self.assertEqual(env["XDG_DATA_HOME"], os.path.join("/job", "data"))
        self.assertEqual(env["EXAMPLE_FLAG"], "1")


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config(path="harness.toml", host=HOST, hold="/hold", root="/root", dump="/dump",
                                 enter_closure="/enter",
                                 devices={DEVICE: config.Device(name=DEVICE, expect=EXPECT, env=dict(DEVICE_ENV))})
        self.text = SUITE.decode("utf-8")
        self.started = []
        self.patch_remote = mock.patch.object(remote, "run", side_effect=lambda host, script: types.SimpleNamespace(
            stdout=self.text))
        self.patch_remote.start()
        self.addCleanup(self.patch_remote.stop)

    def start(self, cfg, job_id, argv):
        self.started.append((job_id, argv))

    def follow(self, cfg, job_id):
        return 0

    def call(self, device=DEVICE):
        return run_quietly(hx_standing.client, self.cfg, device, "light", ("/builds/a", CLOSURE),
                           ("/builds/b", CLOSURE_OTHER), start=self.start, follow=self.follow)

    def test_the_client_starts_one_bench_job_that_runs_standing_in_job_with_both_arms(self):
        status, out, _ = self.call()
        self.assertEqual(status, 0)
        self.assertEqual(len(self.started), 1)
        _, argv = self.started[0]
        self.assertEqual(argv[:2], ["/enter", CLOSURE])
        self.assertTrue(argv[4].endswith(os.path.join("tools", "scripts", "harness", "hx.py")))
        self.assertEqual(argv[5:7], ["standing", "--in-job"])
        a = hx_standing.parse_in_job(argv[argv.index("--in-job") + 1:])
        self.assertEqual((a.a_folder, a.a_closure, a.b_folder, a.b_closure),
                         ("/builds/a", CLOSURE, "/builds/b", CLOSURE_OTHER))
        self.assertEqual((a.device, a.expect, a.scene, a.hold, a.root, a.enter_closure),
                         (DEVICE, EXPECT, "light", "/hold", "/root", "/enter"))
        self.assertEqual(a.env, [("EXAMPLE_FLAG", "1")])
        self.assertIn("plan: 10 timed runs", out)
        self.assertNotIn("correctness", out)

    def test_a_device_that_is_not_configured_is_refused_before_any_job(self):
        with self.assertRaises(hx_floor.FloorError):
            self.call(device="nope")
        self.assertEqual(self.started, [])

    def test_a_method_that_is_refused_stops_before_any_job(self):
        self.text = METHOD_TABLE.replace("runs_per_arm = 5", "runs_per_arm = 4") + SCENES
        with self.assertRaises(verdict.MethodError):
            self.call()
        self.assertEqual(self.started, [])


class DriverTest(StandingCase):
    def test_presses_stop_once_three_status_lines_show_at_least_150_draws(self):
        game = FakeGame(play_after=0, sentinel=WINDOW)
        (status, run_id), out, err = self.timed_run(game)
        self.assertEqual(status, 0, err)
        self.assertEqual(game.presses(), 1)
        record_ = self.standing_run(run_id)
        self.assertEqual((record_["presses"], record_["presents_at_play"]), (1, 360))
        self.assertEqual(record_["window"], {"first": 960, "last": 1019})
        self.assertEqual((record_["verdict"], record_["reason"]), ("pass", None))

    def test_the_press_whose_two_line_wait_completes_the_streak_is_the_last_one_sent(self):
        # Lines 1 to 3 are intro, so press 1 is followed by lines 2 and 3 and the streak is not yet whole. Press 2 is
        # followed by lines 4 and 5, which leave line 3 in the last three: press 3 is sent, and lines 6 and 7 complete
        # the streak. So the play starts at 840 presents, and the settle and window follow from it.
        game = FakeGame(play_after=2)
        (status, run_id), _, err = self.timed_run(game)
        self.assertEqual(status, 0, err)
        record_ = self.standing_run(run_id)
        self.assertEqual(record_["presses"], 3)
        self.assertEqual(game.presses(), 3)
        self.assertEqual(record_["presents_at_play"], 840)
        self.assertEqual(record_["window"], {"first": 1440, "last": 1499})

    def test_no_play_after_30_presses_is_a_recorded_fail(self):
        game = FakeGame(play_after=None)
        (status, run_id), out, _ = self.timed_run(game)
        self.assertEqual(status, 1, out)
        self.assertEqual(game.presses(), 30)
        record_ = self.standing_run(run_id)
        self.assertEqual((record_["verdict"], record_["reason"]), ("FAIL", "no play after 30 presses"))
        self.assertEqual(self.verify(), ([], 1))

    def test_the_game_is_ended_by_its_process_group_once_a_status_line_is_120_presents_past_the_window(self):
        game = FakeGame(play_after=0, sentinel=WINDOW)
        self.timed_run(game)
        self.assertEqual(game.signals, [(signal.SIGTERM, FLUSHED)])

    def test_a_game_that_ignores_sigterm_is_ended_with_sigkill(self):
        game = FakeGame(play_after=0, exits_on_term=False, sentinel=WINDOW)
        (status, run_id), _, err = self.timed_run(game)
        self.assertEqual(status, 0, err)
        self.assertEqual(game.signals, [(signal.SIGTERM, FLUSHED), (signal.SIGKILL, FLUSHED)])
        self.assertEqual(self.standing_run(run_id)["exit_status"], -signal.SIGKILL)

    def test_a_game_that_ends_early_is_a_recorded_fail(self):
        game = FakeGame(play_after=0, end_at=3)
        (status, run_id), out, _ = self.timed_run(game)
        self.assertEqual(status, 1, out)
        record_ = self.standing_run(run_id)
        self.assertEqual(record_["verdict"], "FAIL")
        self.assertIn("the game ended (1)", record_["reason"])
        self.assertEqual(self.verify(), ([], 1))

    def test_a_wait_that_passes_its_deadline_is_a_recorded_fail(self):
        game = FakeGame(lines=False)
        (status, run_id), out, _ = self.timed_run(game)
        self.assertEqual(status, 1, out)
        self.assertEqual(self.standing_run(run_id)["reason"], "the first status line did not come within 1800 s")
        self.assertEqual(game.now, 1800.0)

    def test_the_device_line_is_checked(self):
        game = FakeGame(play_after=0, device="Other GPU")
        (status, run_id), _, _ = self.timed_run(game)
        self.assertEqual(status, 1)
        self.assertEqual(self.standing_run(run_id)["reason"],
                         "the log has no line 'vk: Example GPU, Vulkan'")

    def test_a_fallback_to_another_gpu_is_a_recorded_fail(self):
        game = FakeGame(play_after=0, extra_log="vk: falling back to auto selection\n")
        (status, run_id), _, _ = self.timed_run(game)
        self.assertEqual(status, 1)
        self.assertTrue(self.standing_run(run_id)["reason"].startswith("the renderer fell back to another GPU"))

    def test_a_game_that_does_not_start_is_status_2_and_records_nothing(self):
        def refuse(argv, **kwargs):
            raise OSError(2, "No such file or directory")

        (status, run_id), _, err = self.timed_run(FakeGame(), popen=refuse)
        self.assertEqual((status, run_id), (2, None))
        self.assertIn("the game did not start", err)
        self.assertEqual(self.lines, [])

    def test_the_game_runs_under_the_run_conditions_without_a_bridge_and_with_no_pacing(self):
        game = FakeGame(play_after=0, sentinel=WINDOW)
        self.timed_run(game)
        sub = os.path.join(self.base, "jobs", JOB_ID, "runs", "00")
        self.assertNotIn("HOS_CONTROL", game.env)
        self.assertEqual(game.env["HOS_NO_PACING"], "1")
        self.assertEqual(game.env["HOS_INPUT_LIVE"], os.path.join(sub, "run", "live.txt"))
        self.assertEqual(game.env["XDG_DATA_HOME"], os.path.join(sub, "data"))
        self.assertEqual(game.env["EXAMPLE_FLAG"], "1")
        self.assertEqual(game.argv, [os.path.join(sub, "game", "hookshot")])
        self.assertIs(game.kwargs["start_new_session"], True)
        self.assertIs(game.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(os.path.isdir(os.path.join(sub, "data", "hookshot")))

    def test_each_run_is_a_record_run_of_its_log_trace_kernel_and_standing_run_that_hx_verify_accepts(self):
        game = FakeGame(play_after=0, sentinel=WINDOW)
        (status, run_id), _, err = self.timed_run(game)
        self.assertEqual(status, 0, err)
        manifest, _ = record.read_manifest(self.results, run_id)
        self.assertEqual(set(manifest["files"]), {"log.txt", "hookshot_frames.csv", "kernel.txt", "standing_run.json"})
        self.assertEqual(self.record_file(run_id, "log.txt"), game.log_bytes())
        self.assertEqual(self.record_file(run_id, "hookshot_frames.csv"), game.trace_bytes())
        self.assertEqual(self.record_file(run_id, "kernel.txt"), KERNEL.encode("utf-8"))
        self.assertEqual(self.verify(), ([], 1))


class WindowTest(unittest.TestCase):
    @staticmethod
    def trace(rows, header=TRACE_HEADER):
        """A trace text: the header, then one row per (frame, ms) with 18 more columns, as v2.0.14 writes them"""
        lines = [header] + ["%d,%s%s" % (frame, ms, ",0" * 18) for frame, ms in rows]
        return "\n".join(lines) + "\n"

    def test_the_window_frame_times_are_the_ms_column_of_the_rows_between_its_bounds(self):
        text = self.trace([(frame, "%.1f" % (frame + 0.5)) for frame in range(10)])
        self.assertEqual(hx_standing.window_ms(text, 3, 5), [3.5, 4.5, 5.5])

    def test_a_trace_without_an_ms_column_is_refused(self):
        text = self.trace([(0, "1.0")], header=TRACE_HEADER.replace("ms", "time"))
        with self.assertRaises(hx_standing.StandingError):
            hx_standing.window_ms(text, 0, 0)

    def test_a_row_whose_frame_or_ms_is_not_a_number_is_refused(self):
        for bad in ("x,1.0", "1,nan", "1,fast"):
            with self.subTest(row=bad):
                text = self.trace([]) + bad + ",0" * 18 + "\n"
                with self.assertRaises(hx_standing.StandingError):
                    hx_standing.window_ms(text, 0, 5)

    def test_a_row_with_too_few_fields_is_refused(self):
        text = TRACE_HEADER + "\n0,1.0\n"
        with self.assertRaises(hx_standing.StandingError):
            hx_standing.window_ms(text, 0, 0)

    def test_an_unterminated_last_line_is_not_a_row(self):
        text = self.trace([(frame, "2.0") for frame in range(7)]) + "7,9.00"
        self.assertEqual(hx_standing.window_ms(text, 6, 7), [2.0])

    def test_a_window_with_no_rows_is_refused(self):
        with self.assertRaises(hx_standing.StandingError):
            hx_standing.window_ms(self.trace([(0, "1.0")]), 50, 60)


class InJobTest(StandingCase):
    """The in-job half end to end: the real series and the real comparison, with each run's game faked and its record
    written for real"""

    def setUp(self):
        super().setUp()
        self.job = os.path.join(self.base, "jobs", JOB_ID)
        os.makedirs(self.job)
        self.holds, self.waits = [], 0
        self.argvs, self.games = [], {}
        self.game_kwargs, self.refuse_runs = {}, set()
        self.patch(jobs, "wait_hold", side_effect=self.wait_hold)

    def wait_hold(self, hold, **kwargs):
        self.waits += 1
        return self.holds.pop(0) if self.holds else 0.0

    def args(self, scene="light"):
        return argparse.Namespace(job=self.job, hold=str(self.base / "hold"), root=self.root, enter_closure="/enter",
                                  a_folder=self.build["A"], a_closure=CLOSURE, b_folder=self.build["B"],
                                  b_closure=CLOSURE_OTHER, device=DEVICE, expect=EXPECT,
                                  env=list(DEVICE_ENV.items()), scene=scene)

    @staticmethod
    def refused(argv, **kwargs):
        raise OSError(2, "No such file or directory")

    def launch(self, argv):
        """A run's launch as a fake: the run's own in-process half, with its game faked. Arm B's build runs 1 ms slower."""
        a = hx_standing.parse_run_in_job(argv[argv.index("--run-in-job") + 1:])
        index = int(os.path.basename(a.job))
        self.argvs.append(argv)
        game = FakeGame(offset=1.0 if a.build_folder == self.build["B"] else 0.0, sentinel=WINDOW,
                        **self.game_kwargs.get(index, {}))
        self.games[index] = game
        popen = self.refused if index in self.refuse_runs else game.popen
        return hx_standing.run_in_job(a, popen=popen, killpg=game.killpg, clock=game.clock, sleep=game.sleep,
                                      kernel=lambda launched: KERNEL, machine=lambda: {}, journal=self.lines.append)

    def in_job(self, scene="light"):
        return run_quietly(hx_standing.in_job, self.args(scene), launch=self.launch, journal=self.lines.append)

    def standing_json(self):
        return json.loads(self.record_file(record.run_ids(self.results)[-1], "standing.json"))

    def test_a_complete_standing_is_recorded_unconfirmed_with_its_interval_and_medians_and_no_call(self):
        status, out, err = self.in_job()
        self.assertEqual(status, 0, err)
        run_ids = record.run_ids(self.results)
        payload = self.standing_json()
        frames_a, frames_b = expected_ms(960, 1019), expected_ms(960, 1019, offset=1.0)
        low, high = verdict.bootstrap([frames_a] * 5, [frames_b] * 5, METHOD)
        self.assertEqual(payload["kind"], "standing")
        self.assertIs(payload["unconfirmed"], True)
        self.assertIsNone(payload["reason"])
        self.assertAlmostEqual(payload["interval"][0], low)
        self.assertAlmostEqual(payload["interval"][1], high)
        self.assertGreater(payload["interval"][0], 0)
        self.assertEqual(payload["a_medians"], [statistics.median(frames_a)] * 5)
        self.assertEqual(payload["b_medians"], [statistics.median(frames_b)] * 5)
        for key in ("verdict", "outcome", "faster", "slower", "pass", "fail"):
            self.assertNotIn(key, payload)
        for word in ("faster", "slower"):
            self.assertNotIn(word, out)
        self.assertIn("standing: " + run_ids[-1], out)
        self.assertEqual(hx_standing.run_frames(self.results, run_ids[0]), frames_a)
        self.assertEqual(self.verify(), ([], 11))

    def test_the_series_is_the_timed_plan_and_each_run_is_its_own_process_through_its_arms_closure(self):
        status, _, err = self.in_job()
        self.assertEqual(status, 0, err)
        self.assertEqual(len(self.argvs), 10)
        for index, argv in enumerate(self.argvs):
            closure = CLOSURE if ABBA[index] == "A" else CLOSURE_OTHER
            self.assertEqual(argv[:4], ["/enter", closure, "python", "-I"], index)
            self.assertTrue(argv[4].endswith(os.path.join("tools", "scripts", "harness", "hx.py")), index)
            self.assertEqual(argv[5:7], ["standing", "--run-in-job"], index)
            self.assertEqual(os.path.basename(argv[7]), "%02d" % index)
            self.assertNotIn("--hold", argv)

    def test_each_run_waits_for_the_hold_before_it_runs_and_a_hold_shows_on_the_entry_it_precedes(self):
        self.holds = [0.0, 0.0, 42.5]
        status, _, err = self.in_job()
        self.assertEqual(status, 0, err)
        self.assertEqual(self.waits, 10)
        self.assertEqual([e["held_s"] for e in self.standing_json()["series"][:4]], [0.0, 0.0, 42.5, 0.0])

    def test_a_run_that_does_not_pass_ends_the_series_and_the_standing_is_recorded_with_exit_1(self):
        self.game_kwargs = {2: {"play_after": None}}
        status, _, err = self.in_job()
        self.assertEqual(status, 1, err)
        self.assertEqual(len(self.argvs), 3)
        self.assertEqual(self.waits, 3)
        payload = self.standing_json()
        self.assertEqual(payload["reason"], "run 2 (arm B) ended with status 1")
        self.assertEqual([(e["index"], e["status"]) for e in payload["series"]], [(0, 0), (1, 0), (2, 1)])
        self.assertIsNone(payload["interval"])
        self.assertIsNone(payload["a_medians"])
        self.assertIsNone(payload["b_medians"])
        self.assertEqual(self.verify(), ([], 4))

    def test_a_run_whose_game_does_not_start_ends_the_series_with_status_2_and_records_no_run(self):
        self.refuse_runs = {1}
        status, _, err = self.in_job()
        self.assertEqual(status, 1, err)
        self.assertEqual(len(self.argvs), 2)
        self.assertEqual(self.standing_json()["reason"], "run 1 (arm B) ended with status 2")
        self.assertEqual(self.verify(), ([], 2))

    def test_a_standing_that_cannot_run_is_refused_before_any_run(self):
        status, _, err = self.in_job(scene="nope")
        self.assertEqual(status, 2)
        self.assertEqual(self.argvs, [])
        self.assertIn("could not run", err)


if __name__ == "__main__":
    unittest.main()
