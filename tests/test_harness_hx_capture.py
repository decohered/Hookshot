"""hx capture (scripts/harness/hx_capture.py) with no GPU, game, journal or network. The client refuses an unknown device
before any remote call, and starts one check job whose in-job command carries the frames, the device's environment and a
capture folder under the record root. In-job, against a stand-in game (a fake hookshot_ctl.py that writes the recorder's
folder and the renderer's PNGs the way the renderer does): the launch sets RX_CAPTURE, the shots folder and the renderer
switches; the run is written with every fact key, the capture's hashes, counts, sizes and free space, one screenshot per
present, and no capture byte; a failing verdict and a launch that fails each write their run, marked FAIL, and exit 1.
The machine's GPU listing is a fixture. The journal is injected.

With --scene, the in-job half plays a suite scene through hx_run.play, against a stand-in for game.start: the game starts
with the scene's save, slot, recorder and switches, and the run records the scene, the suite's sha256, the window,
and the seconds and frames played. --seconds S replaces the suite's seconds. A held-out or unknown scene exits 2 with
no game started.

python -I -B tests/test_harness_hx_capture.py
"""
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HX = ROOT / "scripts" / "harness" / "hx.py"
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
import bridge
import game
import hx_capture
import hx_run
import hx_smoke
import jobs
import record
import remote
import scenes

SHA = "0123456789abcdef0123456789abcdef01234567"
CLOSURE = "/nix/store/%s-hookshot-closure" % ("a" * 32)
DEVICE = "first"
EXPECT = "Example GPU One"
RENDERER = "%s (Vulkan, native)" % EXPECT
ICD = "/example/icd.json"
ENTER = "/example/enter-closure.sh"
CALLS = b"RXCAPT01 stand-in calls"
BLOBS = (b"stand-in blob one", b"stand-in blob two")
HOOKSHOT = b"stand-in hookshot"
HOOKSHOT_VK = b"stand-in hookshot_vk.so"

VULKAN = """GPU0:
\tapiVersion         = 1.3.280
\tdriverID           = DRIVER_ID_MESA_RADV
\tdriverName         = radv
\tdriverInfo         = Mesa 24.2.1 (LLVM 18.1.8)
\tvendorID           = 0x1002
\tdeviceName         = Example GPU Zero
GPU1:
\tapiVersion         = 1.3.280
\tdriverID           = DRIVER_ID_MESA_RADV
\tdriverName         = radv
\tdriverInfo         = Mesa 24.2.1 (LLVM 18.1.8)
\tvendorID           = 0x1002
\tdeviceName         = Example GPU One
"""

OUTPUTS = {
    "vulkaninfo": VULKAN,
    "nvidia_smi": None,
    "kernel": "Linux version 6.8.0-test (builder@example) #1 SMP\n",
    "load": "0.10 0.20 0.30 1/200 1234\n",
    "cpu_clocks": "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq:2400000\n",
}

CONFIG = """host = "example-host"
hold = "/example/hold-file"
root = "/example/harness"
dump = "/example/dump"
enter_closure = "/example/enter-closure.sh"

[devices.first]
expect = "Example GPU One"
env = { VK_ICD_FILENAMES = "/example/icd.json" }
"""

# A suite with a scene of each kind, cache and motion, and a held-out scene and one that is not (scenes.parse requires it).
SUITE_TOML = """[method]
note = "stand-in"

[scenes.town-night]
kind = "dense"
cache = "cold"
motion = "still"
save = "town"
slot = "01"
hour = 22
route = [{ teleport = 0 }, { wait = 60 }]
seconds = 2

[scenes.market-walk]
kind = "medium"
cache = "warm"
motion = "moving"
save = "market"
route = [{ age = "adult" }]
inputs = "stick up 30"
seconds = 1

[scenes.plaza-load]
kind = "load"
cache = "cold"
motion = "still"
save = "town"
route = [{ teleport = 0 }]
load_to = "plaza"
seconds = 1

[scenes.plaza-cutscene]
kind = "cutscene"
cache = "cold"
motion = "still"
save = "town"
route = []
seconds = 1

[scenes.long-soak]
kind = "soak"
cache = "warm"
motion = "still"
save = "market"
route = []
seconds = 1800

[scenes.sealed-final]
kind = "light"
cache = "warm"
motion = "still"
save = "market"
route = []
seconds = 1
held_out = true
"""
# What the stand-in play returns: the window's first status, its last, and the map it was held on.
FIRST = {"renderer": RENDERER, "frame": 5, "replayed": 4}
LAST = {"renderer": RENDERER, "frame": 125, "replayed": 124}
MAP = "plaza"

# The stand-in game's control bridge. It answers the four commands hx capture sends, and does what the renderer does:
# with RX_CAPTURE set, the game's own process writes the recorder's calls.rxc and blobs/<sha256> when it exits (after a
# quit, and a short shutdown, so reading them before the process has ended finds nothing); on each present it writes a
# PNG as vk_NNNNNN.png into RX_VK_SHOTS. The constants are filled in by the test from the values it checks against.
STAND_IN = '''import hashlib
import json
import os
import subprocess
import sys
import time

CALLS = @CALLS@
BLOBS = @BLOBS@
RENDERER = @RENDERER@


def option(args, name):
    return args[args.index(name) + 1] if name in args else None


def state_of(sock):
    with open(os.path.join(os.path.dirname(sock), "state.json"), encoding="utf-8") as f:
        return json.load(f)


def save(sock, state):
    with open(os.path.join(os.path.dirname(sock), "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f)


def launch(args):
    if os.environ.get("FAKE_LAUNCH_FAIL"):
        sys.stderr.write("fake: the game would not start\\n")
        return 1
    run = option(args, "--run")
    env = {}
    for i, word in enumerate(args):
        if word == "--env":
            name, _, value = args[i + 1].partition("=")
            env[name] = value
    log = os.path.join(run, "game.log")
    with open(log, "w", encoding="utf-8") as f:
        f.write("fake game started\\n")
    sock = os.path.join(run, "bridge.sock")
    save(sock, {"env": env, "frame": 0})
    game = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--game", os.path.join(run, "quit"),
                             env.get("RX_CAPTURE", "")], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    print(json.dumps({"socket": sock, "pid": game.pid, "run": run, "log": log}))
    return 0


def game(quit_flag, capture):
    """The game's process: it runs until the quit flag is set, then writes the recorder's files and exits"""
    while not os.path.exists(quit_flag):
        time.sleep(0.02)
    time.sleep(0.5)
    if capture:
        os.makedirs(os.path.join(capture, "blobs"), exist_ok=True)
        with open(os.path.join(capture, "calls.rxc"), "wb") as f:
            f.write(CALLS)
        for data in BLOBS:
            with open(os.path.join(capture, "blobs", hashlib.sha256(data).hexdigest()), "wb") as f:
                f.write(data)
    return 0


def quit_game(args):
    open(os.path.join(os.path.dirname(option(args, "--sock")), "quit"), "w", encoding="utf-8").close()
    return 0


def wait(args):
    sock = option(args, "--sock")
    frames = int(option(args, "--frame"))
    state = state_of(sock)
    shots = state["env"]["RX_VK_SHOTS"]
    for n in range(1, frames + 1):
        with open(os.path.join(shots, "vk_%06d.png" % n), "wb") as f:
            f.write(b"PNG stand-in present %d" % n)
    with open(os.path.join(shots, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("not a present\\n")
    state["frame"] = frames
    save(sock, state)
    return 0


def status(args):
    state = state_of(option(args, "--sock"))
    renderer = os.environ.get("FAKE_RENDERER", RENDERER)
    print(json.dumps({"renderer": renderer, "frame": state["frame"], "replayed": 3}))
    return 0


def main(argv):
    if argv[:1] == ["--game"]:
        return game(argv[1], argv[2])
    command = next(word for word in argv if word in ("launch", "wait", "status", "quit"))
    if command == "launch":
        return launch(argv)
    if command == "wait":
        return wait(argv)
    if command == "status":
        return status(argv)
    return quit_game(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def build_text(sha=SHA, dirty="none"):
    return ("closure: %s\nsha: %s\ndirty: %s\nhookshot: 0\nhookshot_vk.so: 0\nbuild: /example/builds/%s\n"
            "job: build-example\n" % (CLOSURE, sha, dirty, sha))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def fake_stand_in_source():
    text = STAND_IN
    for marker, value in (("@CALLS@", CALLS), ("@BLOBS@", BLOBS), ("@RENDERER@", RENDERER)):
        text = text.replace(marker, repr(value))
    return text


class InJobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.record = base / "results"
        self.job = base / "jobs" / jobs.new_id("check")
        self.job_run = self.job / "run"
        self.capture = self.record / "captures" / self.job.name
        self.hold = base / "hold-file"
        self.build = base / "build"
        game = self.job / "game"
        for folder in (self.job_run, game, self.build):
            folder.mkdir(parents=True)
        (game / "hookshot").write_bytes(HOOKSHOT)
        (game / "hookshot_vk.so").write_bytes(HOOKSHOT_VK)
        (self.build / "build.txt").write_text(build_text(), encoding="utf-8")
        self.stand_in = base / "fake_hookshot_ctl.py"
        self.stand_in.write_text(fake_stand_in_source(), encoding="utf-8")
        self.messages = []
        self.output = ""
        self.patch(mock.patch.object(hx_smoke, "CTL", self.stand_in))
        self.patch(mock.patch.object(hx_capture, "command_outputs", return_value=OUTPUTS))
        self.patch(mock.patch.object(record, "write_journal", side_effect=self.messages.append))
        self.patch(mock.patch.dict(os.environ, {"FAKE_LAUNCH_FAIL": "", "FAKE_RENDERER": RENDERER}))

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def run_job(self, frames=3, expect=EXPECT):
        argv = ["--in-job", str(self.job), "--hold", str(self.hold), "--expect", expect, "--frames", str(frames),
                "--device", DEVICE, "--capture", str(self.capture), "--record", str(self.record),
                "--build-folder", str(self.build), "--closure", CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % ICD]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = hx_capture.main(argv)
        self.output = out.getvalue()
        return status

    def run_dir(self):
        return self.record / "runs" / "000001"

    def manifest(self):
        return record.read_manifest(self.record, "000001")[0]

    def capture_json(self):
        return json.loads((self.run_dir() / "files" / "capture.json").read_text(encoding="utf-8"))

    def game_state(self):
        with open(self.job_run / "state.json", encoding="utf-8") as f:
            return json.load(f)

    def test_the_launch_sets_the_recorder_the_shots_folder_and_the_renderer_switches(self):
        self.assertEqual(self.run_job(), 0, self.output)
        game_env = self.game_state()["env"]
        self.assertEqual(game_env["RX_CAPTURE"], str(self.capture))
        self.assertEqual(game_env["RX_VK_SHOTS"], str(self.capture / "shots"))
        self.assertEqual(game_env["RX_VK_SHOT_EVERY"], "1")
        self.assertEqual(game_env["RX_VK_PIPE_SKIP"], "0")
        self.assertEqual(game_env["VK_ICD_FILENAMES"], ICD)
        self.assertEqual(game_env, {**hx_capture.SWITCHES, "RX_CAPTURE": str(self.capture),
                                    "RX_VK_SHOTS": str(self.capture / "shots"), "VK_ICD_FILENAMES": ICD,
                                    "XDG_DATA_HOME": str(self.job / "data"), "HOOKSHOT_NO_UPDATE_CHECK": "1"})

    def test_a_run_is_written_with_every_fact_key_from_the_build_the_game_folder_and_the_machine(self):
        self.assertEqual(self.run_job(), 0, self.output)
        facts = self.manifest()["facts"]
        self.assertEqual(sorted(facts), sorted(record.FACT_KEYS))
        self.assertEqual(facts["shas"], {"hookshot": SHA})
        self.assertEqual(facts["diff"], "")
        self.assertEqual(facts["closure"], CLOSURE)
        self.assertEqual(facts["binaries"], {"hookshot": sha(HOOKSHOT), "hookshot_vk.so": sha(HOOKSHOT_VK)})
        self.assertEqual(facts["env"], {"VK_ICD_FILENAMES": ICD, "RX_VK_SHOT_EVERY": "1", "RX_VK_PIPE_SKIP": "0"})
        self.assertEqual(facts["gpu"]["deviceName"], EXPECT)
        self.assertEqual(facts["mesa"], "24.2.1")
        self.assertEqual(facts["load"], {"1min": 0.1, "5min": 0.2, "15min": 0.3})

    def test_the_run_holds_the_game_log_and_capture_json_with_the_capture_named_by_hashes_counts_and_sizes(self):
        self.assertEqual(self.run_job(), 0, self.output)
        self.assertEqual(sorted(self.manifest()["files"]), ["capture.json", "game.log"])
        self.assertEqual((self.run_dir() / "files" / "game.log").read_text(encoding="utf-8"), "fake game started\n")
        doc = self.capture_json()
        self.assertEqual(doc["capture"], str(self.capture))
        self.assertEqual(doc["build"], SHA)
        self.assertEqual(doc["device"], DEVICE)
        self.assertEqual(doc["frames"], 3)
        self.assertEqual(doc["presents"], 3)
        self.assertEqual(doc["calls"], {"sha256": sha(CALLS), "size": len(CALLS)})
        self.assertEqual(doc["blobs"], {"count": 2, "bytes": sum(len(b) for b in BLOBS)})
        self.assertIsInstance(doc["free_bytes_before"], int)
        self.assertGreater(doc["free_bytes_before"], 0)
        self.assertEqual(doc["verdict"], "pass")
        self.assertNotIn("seconds", doc)

    def test_no_capture_byte_is_copied_into_the_run(self):
        self.assertEqual(self.run_job(), 0, self.output)
        capture_digests = {sha(CALLS), *(sha(data) for data in BLOBS)}
        for path in self.run_dir().rglob("*"):
            if path.is_file():
                self.assertNotIn(sha(path.read_bytes()), capture_digests, path)
        self.assertFalse((self.run_dir() / "files" / "calls.rxc").exists())
        self.assertFalse(any("blobs" in p.parts for p in self.run_dir().rglob("*")))

    def test_each_present_is_one_screenshot_named_by_its_present_with_the_bytes_the_renderer_wrote(self):
        self.assertEqual(self.run_job(frames=3), 0, self.output)
        shots = self.manifest()["screenshots"]
        self.assertEqual(set(shots), {"present/%06d.png" % n for n in (1, 2, 3)})
        for n in (1, 2, 3):
            data = b"PNG stand-in present %d" % n
            self.assertEqual(shots["present/%06d.png" % n], {"sha256": sha(data), "size": len(data)})
            self.assertEqual((self.record / "screenshots" / sha(data)).read_bytes(), data)

    def test_the_run_is_journalled_under_its_id_and_manifest_hash(self):
        self.assertEqual(self.run_job(), 0, self.output)
        manifest_sha = sha((self.run_dir() / "manifest.json").read_bytes())
        self.assertEqual(self.messages, [record.journal_line("000001", manifest_sha)])

    def test_a_failing_verdict_still_writes_its_run_marked_fail_and_exits_1(self):
        with mock.patch.dict(os.environ, {"FAKE_RENDERER": "Example GPU Zero (Vulkan, native)"}):
            status = self.run_job()
        self.assertEqual(status, 1, self.output)
        self.assertIn("capture: FAIL", self.output)
        doc = self.capture_json()
        self.assertTrue(doc["verdict"].startswith("FAIL: the renderer is"), doc["verdict"])
        self.assertEqual(doc["presents"], 3)
        self.assertEqual(len(self.manifest()["screenshots"]), 3)
        self.assertEqual(len(self.messages), 1)

    def test_a_launch_that_fails_writes_a_FAIL_run_whose_recorder_fields_are_gaps(self):
        with mock.patch.dict(os.environ, {"FAKE_LAUNCH_FAIL": "1"}):
            status = self.run_job()
        self.assertEqual(status, 1, self.output)
        self.assertEqual(sorted(self.manifest()["files"]), ["capture.json"])
        self.assertEqual(self.manifest()["screenshots"], {})
        doc = self.capture_json()
        self.assertTrue(doc["verdict"].startswith("FAIL: launch:"), doc["verdict"])
        self.assertIsNone(doc["presents"])
        self.assertIn("gap", doc["calls"])
        self.assertIn("gap", doc["blobs"])

    def test_a_device_the_machine_does_not_list_refuses_the_check_before_the_game_starts(self):
        status = self.run_job(expect="Example GPU Nine")
        self.assertEqual(status, 2, self.output)
        self.assertIn("Example GPU Nine", self.output)
        self.assertFalse((self.job_run / "state.json").exists())
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.record / "runs").exists())


class RunFactsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.build = base / "build"
        self.game = base / "game"
        self.build.mkdir()
        self.game.mkdir()
        (self.game / "hookshot").write_bytes(HOOKSHOT)
        (self.game / "hookshot_vk.so").write_bytes(HOOKSHOT_VK)
        (self.build / "build.txt").write_text(build_text(), encoding="utf-8")
        self.patch(mock.patch.object(hx_capture, "command_outputs", return_value=OUTPUTS))

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def facts(self, env=None):
        return hx_capture.run_facts(str(self.build), str(self.game), CLOSURE, env or {"VK_ICD_FILENAMES": ICD}, EXPECT)

    def test_the_build_and_the_game_folder_give_the_build_facts(self):
        facts = self.facts()
        self.assertEqual(facts["shas"], {"hookshot": SHA})
        self.assertEqual(facts["diff"], "")
        self.assertEqual(facts["binaries"], {"hookshot": sha(HOOKSHOT), "hookshot_vk.so": sha(HOOKSHOT_VK)})
        self.assertEqual(facts["closure"], CLOSURE)
        self.assertEqual(facts["env"], {"VK_ICD_FILENAMES": ICD})

    def test_a_dirty_build_records_its_diff_verbatim(self):
        diff = b"diff --git a/x b/x\n+changed\n"
        (self.build / "dirty.diff").write_bytes(diff)
        (self.build / "build.txt").write_text(build_text(dirty=sha(diff)), encoding="utf-8")
        self.assertEqual(self.facts()["diff"], diff.decode("utf-8"))

    def test_the_gpu_is_the_one_whose_device_name_is_expected(self):
        facts = self.facts()
        self.assertEqual(facts["gpu"]["deviceName"], EXPECT)
        self.assertEqual(facts["driver"]["vulkan"]["driverName"], "radv")


class CommandOutputsTests(unittest.TestCase):
    def test_every_machine_command_runs_with_the_device_environment_and_its_output_is_kept(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout="output\n", stderr="")

        with mock.patch.object(hx_capture.subprocess, "run", side_effect=fake_run):
            outputs = hx_capture.command_outputs({"VK_ICD_FILENAMES": ICD})
        self.assertEqual(sorted(argv for argv, _ in seen), sorted(record.MACHINE_COMMANDS.values()))
        self.assertEqual(outputs, {name: "output\n" for name in record.MACHINE_COMMANDS})
        for _, kwargs in seen:
            self.assertEqual(kwargs["env"]["VK_ICD_FILENAMES"], ICD)
            self.assertIn("PATH", kwargs["env"])

    def test_a_command_that_fails_or_is_absent_gives_none(self):
        def fake_run(argv, **kwargs):
            if argv[0] == "nvidia-smi":
                raise FileNotFoundError(argv[0])
            return subprocess.CompletedProcess(argv, 1, stdout="partial\n", stderr="")

        with mock.patch.object(hx_capture.subprocess, "run", side_effect=fake_run):
            outputs = hx_capture.command_outputs({})
        self.assertEqual(outputs, {name: None for name in record.MACHINE_COMMANDS})


class GpuIndexTests(unittest.TestCase):
    def test_the_index_of_the_one_device_with_that_name(self):
        self.assertEqual(hx_capture.gpu_index(VULKAN, EXPECT), 1)

    def test_no_device_with_that_name_is_refused(self):
        with self.assertRaises(hx_capture.CaptureError):
            hx_capture.gpu_index(VULKAN, "Example GPU Nine")

    def test_two_devices_with_that_name_are_refused(self):
        with self.assertRaises(hx_capture.CaptureError):
            hx_capture.gpu_index(VULKAN.replace("Example GPU Zero", EXPECT), EXPECT)

    def test_no_vulkaninfo_output_is_refused(self):
        with self.assertRaises(hx_capture.CaptureError):
            hx_capture.gpu_index(None, EXPECT)


class PresentShotsTests(unittest.TestCase):
    def test_only_six_digit_present_pngs_are_taken_each_under_its_present(self):
        names = ("vk_000001.png", "vk_001000.png", "vk_1000000.png", "vk_12.png", "shot_1.png", "notes.txt")
        with tempfile.TemporaryDirectory() as tmp:
            for name in names:
                Path(tmp, name).write_bytes(name.encode("utf-8"))
            self.assertEqual(hx_capture.present_shots(tmp), {
                "present/000001.png": b"vk_000001.png",
                "present/001000.png": b"vk_001000.png",
            })


class ClientTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "harness.toml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.build_stdout = build_text()
        self.patch(mock.patch.dict(os.environ, {"HX_CONFIG": str(self.config)}))
        self.remote = self.patch(mock.patch.object(remote, "run", side_effect=self.remote_run))
        self.start = self.patch(mock.patch.object(jobs, "start"))
        self.patch(mock.patch.object(jobs, "follow", return_value=0))

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def remote_run(self, host, script, input=None, check=True):
        return subprocess.CompletedProcess([host, script], 0, stdout=self.build_stdout, stderr="")

    def test_an_unknown_device_exits_2_before_any_remote_call(self):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_capture.main(["--device", "nope"])
        self.assertEqual(status, 2)
        self.assertIn("no device 'nope'", err.getvalue())
        self.remote.assert_not_called()
        self.start.assert_not_called()

    def test_hx_dispatches_capture_and_refuses_an_unknown_device_with_exit_2(self):
        env = {**os.environ, "HX_CONFIG": str(self.config)}
        done = subprocess.run([sys.executable, "-I", "-B", str(HX), "capture", "--device", "nope"], env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("no device 'nope'", done.stderr)

    def test_the_job_runs_the_build_game_with_the_frames_device_env_and_a_capture_folder_under_the_record(self):
        with quiet():
            status = hx_capture.main(["--device", DEVICE, "--frames", "7"])
        self.assertEqual(status, 0)
        self.start.assert_called_once()
        cfg_arg, job_id, argv = self.start.call_args.args
        self.assertTrue(job_id.startswith("check-"), job_id)
        self.assertEqual(self.start.call_args.kwargs, {"game": "/example/harness/builds/latest/game"})
        job = "/example/harness/jobs/%s" % job_id
        capture = "/example/harness/results/captures/%s" % job_id
        self.assertEqual(argv, [
            ENTER, CLOSURE, "python", "-I", job + "/tools/scripts/harness/hx.py", "capture", "--in-job", job,
            "--hold", "/example/hold-file", "--expect", EXPECT, "--frames", "7", "--device", DEVICE,
            "--capture", capture, "--record", "/example/harness/results", "--build-folder",
            "/example/harness/builds/latest", "--closure", CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % ICD,
        ])

    def test_a_build_whose_sha_is_not_a_commit_is_refused_before_the_job(self):
        self.build_stdout = build_text(sha="not-a-sha")
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_capture.main(["--device", DEVICE])
        self.assertEqual(status, 2)
        self.assertIn("40-hex commit", err.getvalue())
        self.start.assert_not_called()

    def test_a_scene_job_carries_the_scene_and_the_root_and_no_frames(self):
        with quiet():
            status = hx_capture.main(["--device", DEVICE, "--scene", "town-night"])
        self.assertEqual(status, 0)
        _, job_id, argv = self.start.call_args.args
        job = "/example/harness/jobs/%s" % job_id
        capture = "/example/harness/results/captures/%s" % job_id
        self.assertEqual(argv, [
            ENTER, CLOSURE, "python", "-I", job + "/tools/scripts/harness/hx.py", "capture", "--in-job", job,
            "--hold", "/example/hold-file", "--expect", EXPECT, "--device", DEVICE,
            "--capture", capture, "--record", "/example/harness/results", "--build-folder",
            "/example/harness/builds/latest", "--closure", CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % ICD,
            "--scene", "town-night", "--root", "/example/harness", "--seconds", "20",
        ])

    def test_a_scene_job_carries_the_given_seconds(self):
        with quiet():
            status = hx_capture.main(["--device", DEVICE, "--scene", "town-night", "--seconds", "45"])
        self.assertEqual(status, 0)
        _, _, argv = self.start.call_args.args
        self.assertEqual(argv[-6:], ["--scene", "town-night", "--root", "/example/harness", "--seconds", "45"])

    def test_a_scene_with_frames_exits_2_before_any_remote_call(self):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_capture.main(["--device", DEVICE, "--scene", "town-night", "--frames", "7"])
        self.assertEqual(status, 2)
        self.assertIn("--frames does not apply with --scene", err.getvalue())
        self.remote.assert_not_called()
        self.start.assert_not_called()

    def test_seconds_without_a_scene_exits_2_before_any_remote_call(self):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_capture.main(["--device", DEVICE, "--seconds", "45"])
        self.assertEqual(status, 2)
        self.assertIn("--seconds does not apply without --scene", err.getvalue())
        self.remote.assert_not_called()
        self.start.assert_not_called()

    def test_seconds_must_be_a_positive_whole_number(self):
        for value in ("0", "1.5", "x"):
            with self.subTest(value=value):
                err = io.StringIO()
                with quiet(), contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                    hx_capture.main(["--device", DEVICE, "--scene", "town-night", "--seconds", value])
                self.assertIn("not a number of seconds", err.getvalue())
                self.remote.assert_not_called()
                self.start.assert_not_called()


class SceneInJobTests(unittest.TestCase):
    """The in-job half with --scene: game.start and hx_run.play are stand-ins, and the suite is a file under a harness root,
    read as hx run reads it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.record = base / "results"
        self.job = base / "jobs" / jobs.new_id("check")
        self.capture = self.record / "captures" / self.job.name
        self.hold = base / "hold-file"
        self.build = base / "build"
        self.root = base / "harness"
        game_dir = self.job / "game"
        for folder in (self.job / "run", game_dir, self.build, self.root / "scenes"):
            folder.mkdir(parents=True)
        (game_dir / "hookshot").write_bytes(HOOKSHOT)
        (game_dir / "hookshot_vk.so").write_bytes(HOOKSHOT_VK)
        (self.build / "build.txt").write_text(build_text(), encoding="utf-8")
        self.suite_bytes = SUITE_TOML.encode("utf-8")
        (self.root / "scenes" / "suite.toml").write_bytes(self.suite_bytes)
        self.log_text = "fake game started\n"
        self.launch_error = None
        self.running = mock.Mock()
        self.running.quit.return_value = 0
        self.messages = []
        self.output = ""
        self.patch(mock.patch.object(hx_capture, "command_outputs", return_value=OUTPUTS))
        self.patch(mock.patch.object(record, "write_journal", side_effect=self.messages.append))
        self.start = self.patch(mock.patch.object(game, "start", side_effect=self.start_game))
        self.play = self.patch(mock.patch.object(hx_run, "play", return_value=(FIRST, LAST, MAP)))

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def start_game(self, job, game_dir, device_env, **kwargs):
        if self.launch_error is not None:
            raise game.GameError(self.launch_error)
        log = os.path.join(job, "run", "log.txt")
        with open(log, "w", encoding="utf-8") as f:
            f.write(self.log_text)
        self.running.log = log
        return self.running

    def run_scene(self, scene="town-night", expect=EXPECT, seconds=None):
        argv = ["--in-job", str(self.job), "--hold", str(self.hold), "--expect", expect, "--device", DEVICE,
                "--capture", str(self.capture), "--record", str(self.record), "--build-folder", str(self.build),
                "--closure", CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % ICD, "--scene", scene, "--root", str(self.root)]
        if seconds is not None:
            argv += ["--seconds", str(seconds)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = hx_capture.main(argv)
        self.output = out.getvalue()
        return status

    def run_dir(self):
        return self.record / "runs" / "000001"

    def manifest(self):
        return record.read_manifest(self.record, "000001")[0]

    def capture_json(self):
        return json.loads((self.run_dir() / "files" / "capture.json").read_text(encoding="utf-8"))

    def test_the_game_starts_with_the_scene_save_slot_recorder_and_switches_and_play_gets_the_scene(self):
        self.assertEqual(self.run_scene(), 0, self.output)
        args, kwargs = self.start.call_args
        self.assertEqual(args, (str(self.job), str(self.job / "game"), {"VK_ICD_FILENAMES": ICD}))
        self.assertEqual(kwargs["save"], str(self.root / "scenes" / "saves" / "town"))
        self.assertEqual(kwargs["slot"], "01")
        self.assertEqual(kwargs["extra"], {**hx_capture.SWITCHES, "RX_CAPTURE": str(self.capture),
                                           "RX_VK_SHOTS": str(self.capture / "shots")})
        self.play.assert_called_once()
        running, scene, expect = self.play.call_args.args
        self.assertIs(running, self.running)
        self.assertEqual(scene.name, "town-night")
        self.assertEqual(expect, EXPECT)
        self.running.quit.assert_called_once_with(hx_run.QUIT_SECONDS)
        self.assertEqual((self.run_dir() / "files" / "game.log").read_text(encoding="utf-8"), self.log_text)

    def test_capture_json_holds_the_scene_suite_hash_window_frames_and_last_present(self):
        self.assertEqual(self.run_scene(), 0, self.output)
        doc = self.capture_json()
        self.assertEqual(doc["scene"], "town-night")
        self.assertEqual(doc["suite_sha256"], sha(self.suite_bytes))
        self.assertEqual(doc["window"], {"first": FIRST["replayed"] + 1, "last": LAST["replayed"]})
        self.assertEqual(doc["frames"], 120)
        self.assertEqual(doc["seconds"], 2)
        self.assertEqual(doc["presents"], LAST["frame"])
        self.assertEqual(doc["build"], SHA)
        self.assertEqual(doc["device"], DEVICE)
        self.assertEqual(doc["verdict"], "pass")

    def test_seconds_replace_the_scenes_seconds_and_play_gets_every_other_field_unchanged(self):
        self.assertEqual(self.run_scene(seconds=45), 0, self.output)
        _, scene, _ = self.play.call_args.args
        suite = scenes.parse(self.suite_bytes, str(self.root / "scenes" / "suite.toml"))
        self.assertEqual(scene, dataclasses.replace(scenes.select(suite, "town-night", False), seconds=45))

    def test_capture_json_holds_the_given_seconds_and_that_many_seconds_of_frames(self):
        self.assertEqual(self.run_scene(seconds=45), 0, self.output)
        doc = self.capture_json()
        self.assertEqual(doc["seconds"], 45)
        self.assertEqual(doc["frames"], 45 * hx_run.FPS)

    def test_a_play_error_writes_a_FAIL_run_with_no_window_and_exits_1(self):
        self.play.side_effect = bridge.BridgeError("the window is not steady at frame 9: loading True")
        self.assertEqual(self.run_scene(), 1, self.output)
        self.assertIn("capture: FAIL", self.output)
        doc = self.capture_json()
        self.assertEqual(doc["verdict"], "FAIL: the window is not steady at frame 9: loading True")
        self.assertIsNone(doc["window"])
        self.assertIsNone(doc["presents"])
        self.assertEqual(doc["scene"], "town-night")
        self.running.quit.assert_called_once_with(hx_run.QUIT_SECONDS)
        self.assertEqual(len(self.messages), 1)

    def test_a_quit_error_after_the_window_writes_a_FAIL_run_that_keeps_the_window_and_exits_1(self):
        self.running.quit.side_effect = game.GameError("the game did not quit in 60 s: killed")
        self.assertEqual(self.run_scene(), 1, self.output)
        doc = self.capture_json()
        self.assertEqual(doc["verdict"], "FAIL: the game did not quit in 60 s: killed")
        self.assertEqual(doc["window"], {"first": FIRST["replayed"] + 1, "last": LAST["replayed"]})
        self.assertEqual(doc["presents"], LAST["frame"])

    def test_a_game_log_that_shows_the_device_fallback_fails_the_run(self):
        self.log_text = "fake game started\nrenderer %s\n" % hx_smoke.FALLBACK
        self.assertEqual(self.run_scene(), 1, self.output)
        self.assertEqual(self.capture_json()["verdict"],
                         "FAIL: the log shows the renderer fell back to another GPU (%s)" % hx_smoke.FALLBACK)

    def test_a_game_that_will_not_start_writes_a_FAIL_run_with_no_window_and_no_game_log(self):
        self.launch_error = "the game would not start (no executable)"
        self.assertEqual(self.run_scene(), 1, self.output)
        self.assertEqual(sorted(self.manifest()["files"]), ["capture.json"])
        doc = self.capture_json()
        self.assertEqual(doc["verdict"], "FAIL: launch: the game would not start (no executable)")
        self.assertIsNone(doc["window"])
        self.assertIsNone(doc["presents"])
        self.assertEqual(doc["frames"], 120)
        self.play.assert_not_called()

    def test_a_held_out_scene_exits_2_with_no_game_started_and_nothing_written(self):
        self.assertEqual(self.run_scene("sealed-final"), 2, self.output)
        self.assertIn("held out", self.output)
        self.start.assert_not_called()
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.record / "runs").exists())

    def test_an_unknown_scene_exits_2_with_no_game_started_and_nothing_written(self):
        self.assertEqual(self.run_scene("nope"), 2, self.output)
        self.assertIn("no scene named 'nope'", self.output)
        self.start.assert_not_called()
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.record / "runs").exists())


class InJobArgumentsTests(unittest.TestCase):
    BASE = ["/example/job", "--hold", "/example/hold-file", "--expect", EXPECT, "--device", DEVICE,
            "--capture", "/example/capture", "--record", "/example/record", "--build-folder", "/example/build",
            "--closure", CLOSURE]

    def test_a_run_takes_frames_or_a_scene_with_its_root_and_not_both_or_neither(self):
        cases = (
            (["--frames", "3"], True),
            (["--scene", "town-night", "--root", "/example/harness"], True),
            (["--frames", "3", "--scene", "town-night", "--root", "/example/harness"], False),
            (["--scene", "town-night"], False),
            (["--frames", "3", "--root", "/example/harness"], False),
            (["--scene", "town-night", "--root", "/example/harness", "--seconds", "45"], True),
            (["--scene", "town-night", "--root", "/example/harness", "--seconds", "0"], False),
            (["--frames", "3", "--seconds", "45"], False),
            ([], False),
        )
        for extra, accepted in cases:
            with self.subTest(extra=extra):
                if accepted:
                    hx_capture.parse_in_job(self.BASE + extra)
                else:
                    with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                        hx_capture.parse_in_job(self.BASE + extra)


if __name__ == "__main__":
    unittest.main()
