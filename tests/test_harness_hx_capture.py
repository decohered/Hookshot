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

With --scene --at-scene, the game starts with RX_CAPTURE_START naming the job's capture-start file (absent at launch),
the scene is played as hx_run.play plays it, and the file is made once the route has arrived, just before the window (a
load scene: before its arrive). The run records at_scene and start_present, the present the game's log says the capture
started at; its screenshots are the live presents from that one on, renumbered from 1. Without that start line the run
FAILs. --at-scene without --scene exits 2 with no remote call.

With --presents N (with --at-scene), the game gets RX_CAPTURE_PRESENTS=N, the window is N presents after the start, and
the run records presents_recorded, after and end_present, the present the game's log says the capture ended at, which
must be the start's present + N - 1 (no such line, or another present, FAILs the run). --after K waits K presents before
the start file is made. Its screenshots are the N presents the capture recorded, renumbered from 1. --presents without
--at-scene, --after without --presents, and --seconds with --presents each exit 2 with no remote call.

Every capture run removes its shots folder once its run is written.

With --trim RUN (with --device, and --keep-registered if given), the in-job half writes RUN's working set as a new capture
run: a new capture folder whose calls.rxc is RUN's, except that each POOL page and PAGES entry outside the read set names
the zero page; whose blobs folder holds the zero page and hard links to the blobs the new calls.rxc names; and whose
capture.json is RUN's with the new fields. The read set is the pages each submit reads (analysis.py's scan), the storage of
each texture a present or a texel call names, and with --keep-registered the storage of each registered texture. The run
keeps RUN's screenshots. --trim with --scene, --frames, --build or a scene flag, --keep-registered without --trim, a run
that is not a capture, a --device other than the run's, and a capture analysis.py refuses each exit 2 with nothing written.

python -I -B tests/test_harness_hx_capture.py
"""
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HX = ROOT / "scripts" / "harness" / "hx.py"
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
import analysis
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


START_FILE = "capture-start"
START_LINE = "rx capture: started at present %d\n"
END_LINE = "rx capture: ended at present %d\n"
TOWN_NIGHT_START = 123     # the present town-night's at-scene run starts at, with no --after (its teleport and its
                           # arrival event take a present each)
WINDOW_PRESENTS = 30       # a --presents run's window and the recorder's presents
AFTER_PRESENTS = 10        # a --after run's presents waited before the start file


class FakeBridge:
    """A game's control bridge for a scene's play, settled in play on the renderer. A present is one frame: a teleport
    presents one and starts its arrival event, which the next status shows playing and which ends one present later (as
    the game's does, so bridge.arrive sees it begin and end); a wait presents its frames, or up to its frame. With shots set, each present writes its PNG (vk_NNNNNN.png) into it, as the
    renderer does. When armed, the first present that finds the start file is the capture's first, as the recorder's is.
    With records set, the recorder keeps that many presents from its start and ends at the last of them, which is the
    present the game's log names; records None is a recorder that never ends. Each request is recorded with whether the
    start file exists when it is sent."""

    def __init__(self, start=None, shots=None, armed=True, records=None):
        self.start, self.shots, self.armed, self.records = start, shots, armed, records
        self.frame = 0
        self.started = None
        self.ends_at = None
        self.ended = None
        self.requests = []
        self.timeouts = []
        self.arriving = False

    def close(self):
        pass

    def request(self, cmd, timeout=None, **args):
        self.requests.append((cmd, args, self.start_exists()))
        self.timeouts.append(timeout)
        if cmd == "teleport":
            reply = {"ok": True, "map": MAP, "frame": self.frame}
            self.arriving = True
            self.present(1)
            return reply
        if cmd == "wait" and args.get("event") == "present":
            self.present(args["frames"])
            return {"ok": True, "frame": self.frame}
        if cmd == "wait" and args.get("event") == "frame":
            self.present(max(0, args["frame"] - self.frame))
            return {"ok": True, "frame": self.frame}
        if cmd == "age":
            return {"ok": True, "sent": True}
        if cmd == "status":
            reply = {"ok": True, "renderer": RENDERER, "state": "play", "map": MAP, "frame": self.frame,
                     "replayed": self.frame, "game": {"frame": self.frame, "event": self.arriving, "loading": False}}
            if self.arriving:   # (the arrival event, shown once, ends one present later)
                self.arriving = False
                self.present(1)
            return reply
        return {"ok": True}

    def start_exists(self):
        return self.start is not None and os.path.exists(self.start)

    def present(self, count):
        for _ in range(count):
            self.frame += 1
            if self.shots is not None:
                with open(os.path.join(self.shots, "vk_%06d.png" % self.frame), "wb") as f:
                    f.write(b"PNG present %d" % self.frame)
            if self.armed and self.started is None and self.start_exists():
                self.started = self.frame
                if self.records is not None:
                    self.ends_at = self.frame + self.records - 1
            if self.frame == self.ends_at:
                self.ended = self.frame


class FakeGame:
    """A started game for a scene's play: its bridge is conn, and its log (set as start_game sets it) gets the recorder's
    start line when the capture started, and its end line when the capture ended, written when the game quits"""

    def __init__(self, conn):
        self.conn = conn
        self.log = None
        self.hours = []

    def connect(self):
        return self.conn

    def set_hour(self, hour):
        self.hours.append(hour)

    def quit(self, timeout=60):
        if self.conn.started is not None:
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(START_LINE % self.conn.started)
        if self.conn.ended is not None:
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(END_LINE % self.conn.ended)
        return 0


def shifted(request, frames):
    """A request with its frame wait's target moved by frames (any other request as it is)"""
    cmd, args = request
    if cmd == "wait" and args.get("event") == "frame":
        return cmd, {**args, "frame": args["frame"] + frames}
    return request


def requests_of(conn):
    return [(cmd, args) for cmd, args, _ in conn.requests]


def start_flags(conn):
    return [exists for _, _, exists in conn.requests]


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

    def test_the_shots_folder_is_removed_once_the_run_is_written(self):
        self.assertEqual(self.run_job(frames=3), 0, self.output)
        self.assertEqual(len(self.manifest()["screenshots"]), 3)
        self.assertFalse((self.capture / "shots").exists())

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
        self.assertFalse((self.capture / "shots").exists())
        self.assertEqual(len(self.messages), 1)

    def test_a_launch_that_fails_writes_a_FAIL_run_whose_recorder_fields_are_gaps(self):
        with mock.patch.dict(os.environ, {"FAKE_LAUNCH_FAIL": "1"}):
            status = self.run_job()
        self.assertEqual(status, 1, self.output)
        self.assertEqual(sorted(self.manifest()["files"]), ["capture.json"])
        self.assertEqual(self.manifest()["screenshots"], {})
        self.assertFalse((self.capture / "shots").exists())
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

    def test_a_scene_job_with_at_scene_carries_the_flag_last(self):
        with quiet():
            status = hx_capture.main(["--device", DEVICE, "--scene", "town-night", "--at-scene"])
        self.assertEqual(status, 0)
        _, _, argv = self.start.call_args.args
        self.assertEqual(argv[-7:], ["--scene", "town-night", "--root", "/example/harness", "--seconds", "20",
                                     "--at-scene"])

    def test_a_scene_job_with_at_scene_and_presents_carries_them_last_and_no_seconds(self):
        with quiet():
            status = hx_capture.main(["--device", DEVICE, "--scene", "town-night", "--at-scene", "--presents", "30",
                                      "--after", "10"])
        self.assertEqual(status, 0)
        _, _, argv = self.start.call_args.args
        self.assertEqual(argv[-9:], ["--scene", "town-night", "--root", "/example/harness", "--at-scene", "--presents",
                                     "30", "--after", "10"])

    def test_presents_and_after_are_refused_before_any_remote_call_unless_they_apply(self):
        cases = (
            (["--scene", "town-night", "--presents", "30"], "--presents does not apply without --at-scene"),
            (["--presents", "30"], "--presents does not apply without --at-scene"),
            (["--scene", "town-night", "--at-scene", "--after", "10"], "--after does not apply without --presents"),
            (["--scene", "town-night", "--at-scene", "--presents", "30", "--seconds", "45"],
             "--seconds does not apply with --presents"),
        )
        for extra, message in cases:
            with self.subTest(extra=extra):
                err = io.StringIO()
                with quiet(), contextlib.redirect_stderr(err):
                    status = hx_capture.main(["--device", DEVICE, *extra])
                self.assertEqual(status, 2)
                self.assertIn(message, err.getvalue())
                self.remote.assert_not_called()
                self.start.assert_not_called()

    def test_presents_must_be_a_positive_whole_number_and_after_a_whole_number(self):
        for flag, value in (("--presents", "0"), ("--presents", "1.5"), ("--after", "-1"), ("--after", "x")):
            with self.subTest(flag=flag, value=value):
                err = io.StringIO()
                with quiet(), contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                    hx_capture.main(["--device", DEVICE, "--scene", "town-night", "--at-scene", flag, value])
                self.assertIn("not a number of", err.getvalue())
                self.remote.assert_not_called()
                self.start.assert_not_called()

    def test_at_scene_without_a_scene_exits_2_before_any_remote_call(self):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_capture.main(["--device", DEVICE, "--at-scene"])
        self.assertEqual(status, 2)
        self.assertIn("--at-scene does not apply without --scene", err.getvalue())
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
        # (the stand-in refuses no switch, so the gap between two switches is not waited for)
        self.patch(mock.patch.object(bridge, "SWITCH_GAP", 0))

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

    def use_game(self, armed=True, records=None):
        """Makes the game the at-scene run plays: a bridge that presents (FakeBridge), with the start file the run names,
        and a recorder that keeps records presents from its start (None: it never ends)"""
        self.bridge = FakeBridge(start=str(self.job / START_FILE), shots=str(self.capture / "shots"), armed=armed,
                                 records=records)
        self.running = FakeGame(self.bridge)

    def run_scene(self, scene="town-night", expect=EXPECT, seconds=None, at_scene=False, presents=None, after=None):
        argv = ["--in-job", str(self.job), "--hold", str(self.hold), "--expect", expect, "--device", DEVICE,
                "--capture", str(self.capture), "--record", str(self.record), "--build-folder", str(self.build),
                "--closure", CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % ICD, "--scene", scene, "--root", str(self.root)]
        if seconds is not None:
            argv += ["--seconds", str(seconds)]
        if at_scene:
            argv.append("--at-scene")
        if presents is not None:
            argv += ["--presents", str(presents)]
        if after is not None:
            argv += ["--after", str(after)]
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
        self.assertFalse((self.capture / "shots").exists())
        self.assertEqual(len(self.messages), 1)

    def test_the_shots_folder_is_removed_once_the_scene_run_is_written(self):
        self.assertEqual(self.run_scene(), 0, self.output)
        self.assertTrue((self.run_dir() / "files" / "capture.json").exists())
        self.assertFalse((self.capture / "shots").exists())

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
        self.assertFalse((self.capture / "shots").exists())
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

    def test_an_at_scene_run_launches_with_the_start_file_named_and_the_file_absent(self):
        self.use_game()
        self.assertEqual(self.run_scene(at_scene=True), 0, self.output)
        _, kwargs = self.start.call_args
        self.assertEqual(kwargs["extra"], {**hx_capture.SWITCHES, "RX_CAPTURE": str(self.capture),
                                           "RX_VK_SHOTS": str(self.capture / "shots"),
                                           "RX_CAPTURE_START": str(self.job / START_FILE)})
        # the first request is after the launch, and nothing makes the file before the route has arrived
        self.assertFalse(self.bridge.requests[0][2], "the start file exists at the game's first request")
        self.play.assert_not_called()

    def test_capture_json_holds_at_scene_and_the_present_the_capture_started_at(self):
        self.use_game()
        self.assertEqual(self.run_scene(at_scene=True), 0, self.output)
        doc = self.capture_json()
        self.assertIs(doc["at_scene"], True)
        self.assertEqual(doc["start_present"], TOWN_NIGHT_START)
        # the window's first present follows the start's: the start's present is waited for before the window
        self.assertEqual(doc["window"], {"first": TOWN_NIGHT_START + 1, "last": TOWN_NIGHT_START + 120})
        self.assertEqual(doc["presents"], TOWN_NIGHT_START + 120)
        self.assertEqual(doc["verdict"], "pass")

    def test_the_screenshots_are_the_live_presents_from_the_start_renumbered_from_one(self):
        self.use_game()
        self.assertEqual(self.run_scene(at_scene=True), 0, self.output)
        shots = self.manifest()["screenshots"]
        self.assertEqual(set(shots), {"present/%06d.png" % n for n in range(1, 122)})
        for n in range(1, 122):
            data = b"PNG present %d" % (TOWN_NIGHT_START - 1 + n)
            self.assertEqual(shots["present/%06d.png" % n], {"sha256": sha(data), "size": len(data)}, n)
        self.assertFalse((self.capture / "shots").exists())

    def test_a_load_scene_at_scene_starts_the_capture_at_its_arrive(self):
        self.use_game()
        self.assertEqual(self.run_scene("plaza-load", at_scene=True), 0, self.output)
        doc = self.capture_json()
        # the route's teleport and its arrival event take presents 1 and 2; the arrive's teleport is the start's
        # present, and its arrival event one more before the window
        self.assertEqual(doc["start_present"], 3)
        shots = self.manifest()["screenshots"]
        self.assertEqual(set(shots), {"present/%06d.png" % n for n in range(1, 64)})
        data = b"PNG present 3"
        self.assertEqual(shots["present/000001.png"], {"sha256": sha(data), "size": len(data)})

    def test_a_recording_with_no_start_line_writes_a_FAIL_run_with_no_start_present_and_exits_1(self):
        self.use_game(armed=False)
        self.assertEqual(self.run_scene(at_scene=True), 1, self.output)
        self.assertIn("capture: FAIL", self.output)
        doc = self.capture_json()
        self.assertEqual(doc["verdict"], "FAIL: the log has no 'rx capture: started at present' line: the capture did not "
                                         "start")
        self.assertIs(doc["at_scene"], True)
        self.assertIsNone(doc["start_present"])
        self.assertEqual(self.manifest()["screenshots"], {})
        self.assertFalse((self.capture / "shots").exists())
        self.assertEqual(len(self.messages), 1)

    def test_an_at_scene_run_with_presents_gives_the_game_the_recorders_presents_with_its_start(self):
        self.use_game(records=WINDOW_PRESENTS)
        self.assertEqual(self.run_scene(at_scene=True, presents=WINDOW_PRESENTS), 0, self.output)
        _, kwargs = self.start.call_args
        self.assertEqual(kwargs["extra"], {**hx_capture.SWITCHES, "RX_CAPTURE": str(self.capture),
                                           "RX_VK_SHOTS": str(self.capture / "shots"),
                                           "RX_CAPTURE_START": str(self.job / START_FILE),
                                           "RX_CAPTURE_PRESENTS": str(WINDOW_PRESENTS)})

    def test_an_at_scene_run_with_after_waits_that_many_presents_before_it_makes_the_start_file(self):
        self.use_game(records=WINDOW_PRESENTS)
        self.assertEqual(self.run_scene(at_scene=True, presents=WINDOW_PRESENTS, after=AFTER_PRESENTS), 0, self.output)
        made = start_flags(self.bridge).index(True)
        self.assertEqual(requests_of(self.bridge)[made], ("wait", {"event": "present", "frames": 1}))
        self.assertEqual(requests_of(self.bridge)[made - 1], ("wait", {"event": "present", "frames": AFTER_PRESENTS}))
        self.assertEqual(self.bridge.timeouts[made - 1], hx_run.WAIT_SECONDS)
        self.assertFalse(self.bridge.requests[made - 1][2], "the start file exists before the after presents")

    def test_an_at_scene_run_with_presents_holds_a_window_of_that_many_presents_after_its_start(self):
        self.use_game(records=WINDOW_PRESENTS)
        self.assertEqual(self.run_scene(at_scene=True, presents=WINDOW_PRESENTS, after=AFTER_PRESENTS), 0, self.output)
        start = TOWN_NIGHT_START + AFTER_PRESENTS
        doc = self.capture_json()
        self.assertEqual(doc["start_present"], start)
        self.assertEqual(doc["window"], {"first": start + 1, "last": start + WINDOW_PRESENTS})
        self.assertEqual(doc["presents"], start + WINDOW_PRESENTS)
        self.assertEqual(doc["frames"], WINDOW_PRESENTS)

    def test_capture_json_holds_presents_recorded_after_and_the_present_the_capture_ended_at(self):
        self.use_game(records=WINDOW_PRESENTS)
        self.assertEqual(self.run_scene(at_scene=True, presents=WINDOW_PRESENTS, after=AFTER_PRESENTS), 0, self.output)
        start = TOWN_NIGHT_START + AFTER_PRESENTS
        doc = self.capture_json()
        self.assertEqual(doc["presents_recorded"], WINDOW_PRESENTS)
        self.assertEqual(doc["after"], AFTER_PRESENTS)
        self.assertEqual(doc["end_present"], start + WINDOW_PRESENTS - 1)
        self.assertEqual(doc["verdict"], "pass")

    def test_the_screenshots_are_the_presents_recorded_from_the_start_renumbered_from_one(self):
        self.use_game(records=WINDOW_PRESENTS)
        self.assertEqual(self.run_scene(at_scene=True, presents=WINDOW_PRESENTS, after=AFTER_PRESENTS), 0, self.output)
        start = TOWN_NIGHT_START + AFTER_PRESENTS
        shots = self.manifest()["screenshots"]
        self.assertEqual(set(shots), {"present/%06d.png" % n for n in range(1, WINDOW_PRESENTS + 1)})
        for n in range(1, WINDOW_PRESENTS + 1):
            data = b"PNG present %d" % (start + n - 1)
            self.assertEqual(shots["present/%06d.png" % n], {"sha256": sha(data), "size": len(data)}, n)

    def test_a_capture_with_no_end_line_writes_a_FAIL_run_with_no_end_present_and_exits_1(self):
        self.use_game(records=None)
        self.assertEqual(self.run_scene(at_scene=True, presents=WINDOW_PRESENTS), 1, self.output)
        self.assertIn("capture: FAIL", self.output)
        doc = self.capture_json()
        self.assertEqual(doc["verdict"], "FAIL: the log has no 'rx capture: ended at present' line: the capture did not "
                                         "end")
        self.assertIsNone(doc["end_present"])
        self.assertEqual(doc["start_present"], TOWN_NIGHT_START)
        self.assertEqual(doc["presents_recorded"], WINDOW_PRESENTS)
        self.assertEqual(len(self.messages), 1)

    def test_a_capture_that_ends_at_another_present_writes_a_FAIL_run_and_exits_1(self):
        self.use_game(records=WINDOW_PRESENTS + 1)
        self.assertEqual(self.run_scene(at_scene=True, presents=WINDOW_PRESENTS), 1, self.output)
        start = TOWN_NIGHT_START
        end = start + WINDOW_PRESENTS
        doc = self.capture_json()
        self.assertEqual(doc["verdict"], "FAIL: the capture ended at present %d, not %d: %d presents from its start at "
                                         "present %d" % (end, end - 1, WINDOW_PRESENTS, start))
        self.assertEqual(doc["end_present"], end)
        self.assertEqual(len(self.messages), 1)


class AtScenePlayTests(unittest.TestCase):
    """hx_capture.play_at_scene against hx_run.play: the same requests in the same order, and the capture's start file made
    once the route has arrived, immediately before the window (a load scene: before its arrive), followed by one wait, of
    hx_run.WAIT_SECONDS, for the present that starts the capture (its snapshot holds the game while it is written)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.suite = scenes.parse(SUITE_TOML.encode("utf-8"), "suite.toml")
        patcher = mock.patch.object(bridge, "SWITCH_GAP", 0)   # (the fake refuses no switch: no gap to wait for)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scene(self, name):
        return scenes.select(self.suite, name, False)

    def play_at_scene(self, name):
        """Plays the scene at its start file; returns the bridge it played on and the game"""
        start = os.path.join(self.tmp, name + "-" + START_FILE)
        conn = FakeBridge(start=start)
        game_ = FakeGame(conn)
        hx_capture.play_at_scene(game_, self.scene(name), EXPECT, start)
        return conn, game_

    def test_the_requests_and_the_holds_are_hx_run_plays_in_their_order_for_each_kind_of_scene(self):
        for name in ("town-night", "market-walk", "plaza-load", "plaza-cutscene"):
            with self.subTest(scene=name):
                live_conn = FakeBridge()
                live = FakeGame(live_conn)
                hx_run.play(live, self.scene(name), EXPECT)
                at_conn, at_game = self.play_at_scene(name)
                at = requests_of(at_conn)
                made = start_flags(at_conn).index(True)
                self.assertEqual(at[made], ("wait", {"event": "present", "frames": 1}))
                self.assertEqual(at_conn.timeouts[made], hx_run.WAIT_SECONDS)
                # the window waits for game frames counted from its first status, which the start's present makes one
                # frame later than the live run's
                live_requests = requests_of(live_conn)
                self.assertEqual(at[:made] + at[made + 1:], [shifted(r, 1) for r in live_requests])
                self.assertEqual(at_game.hours, live.hours)

    def test_a_scene_makes_its_start_file_after_the_route_and_before_the_window(self):
        for name in ("town-night", "market-walk", "plaza-cutscene"):
            with self.subTest(scene=name):
                conn, _ = self.play_at_scene(name)
                # the start's present wait, then the window's first status and a wait and a status for each step
                made = 1 + 1 + 2 * (self.scene(name).seconds * hx_run.FPS // bridge.WINDOW_STEP)
                flags = start_flags(conn)
                first = len(flags) - made
                self.assertEqual(flags, [False] * first + [True] * made)
                self.assertEqual(conn.requests[first][0], "wait")

    def test_a_load_scene_makes_its_start_file_before_its_arrive(self):
        conn, _ = self.play_at_scene("plaza-load")
        names = [cmd for cmd, _, _ in conn.requests]
        arrive = len(names) - 1 - names[::-1].index("teleport")   # the arrive's teleport: the route's came before
        flags = start_flags(conn)
        # the file is made after the route's teleport and the status that precedes the arrive; the start's present wait
        # follows, then the arrive's own settle
        self.assertIn("teleport", names[:arrive - 2])
        self.assertEqual(names[arrive - 2], "wait")
        self.assertEqual(flags, [False] * (arrive - 2) + [True] * (len(flags) - arrive + 2))


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
            (["--scene", "town-night", "--root", "/example/harness", "--at-scene"], True),
            (["--frames", "3", "--at-scene"], False),
            (["--at-scene"], False),
            (["--scene", "town-night", "--root", "/example/harness", "--at-scene", "--presents", "30"], True),
            (["--scene", "town-night", "--root", "/example/harness", "--at-scene", "--presents", "30", "--after", "0"],
             True),
            (["--scene", "town-night", "--root", "/example/harness", "--at-scene", "--presents", "30", "--after", "10"],
             True),
            (["--scene", "town-night", "--root", "/example/harness", "--presents", "30"], False),
            (["--frames", "3", "--presents", "30"], False),
            (["--scene", "town-night", "--root", "/example/harness", "--at-scene", "--presents", "0"], False),
            (["--scene", "town-night", "--root", "/example/harness", "--at-scene", "--presents", "30", "--after", "-1"],
             False),
            (["--scene", "town-night", "--root", "/example/harness", "--at-scene", "--after", "10"], False),
            (["--scene", "town-night", "--root", "/example/harness", "--at-scene", "--presents", "30", "--seconds", "45"],
             False),
            ([], False),
        )
        for extra, accepted in cases:
            with self.subTest(extra=extra):
                if accepted:
                    hx_capture.parse_in_job(self.BASE + extra)
                else:
                    with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                        hx_capture.parse_in_job(self.BASE + extra)

    def test_trim_takes_no_scene_frames_or_scene_flags_and_keep_registered_only_with_trim(self):
        accepted = (["--trim", "000001"], ["--trim", "000001", "--keep-registered"])
        refused = (["--trim", "000001", "--frames", "3"], ["--trim", "000001", "--scene", "town-night", "--root", "/r"],
                   ["--trim", "000001", "--at-scene"], ["--trim", "000001", "--seconds", "45"],
                   ["--keep-registered"], ["--frames", "3", "--keep-registered"])
        for extra in accepted:
            with self.subTest(extra=extra):
                self.assertEqual(hx_capture.parse_in_job(self.BASE + extra).trim, "000001")
        for extra in refused:
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    hx_capture.parse_in_job(self.BASE + extra)


# hx capture --trim. The captures are synthetic, in the format of host/rx_capture.h (version 1): each has a POOL over pages
# 16 to 20 and a PAGES listing of 21 to 23, a submit that reads pages 17 and 22, a present and texel calls that name
# textures on pages 19, 18 and 21, and a registered texture on page 20 that nothing else uses. Pages 16 and 23 are read
# by nothing. Each page's bytes are its number, repeated, so every page has its own digest.
TRIM_RUN, TRIMMED_RUN, PLAY_RUN = "000001", "000002", "000002"   # the run after TRIM_RUN, trimmed or a play, in its test
PAGE = 4096
ZERO_PAGE = bytes(PAGE)
ZERO_SHA = hashlib.sha256(ZERO_PAGE).digest()
RECORD_OBJECT, RECORD_POOL, RECORD_PAGES, RECORD_SUBMIT = 1, 2, 3, 16
RECORD_REGISTER, RECORD_PRESENT, RECORD_READ_TEXELS, RECORD_WRITE_TEXELS = 26, 25, 33, 34
OP_END, OP_BIND_UBO = 0, 19
HANDLES, RECORDING = 0x6A00_0000_0100, 0x6A00_0001_0000
TEX_PRESENT, TEX_WRITE = 0x6A00_0002_0000, 0x6A00_0002_1000
TEX_READ, TEX_REGISTERED = 0x6A00_0002_2000, 0x6A00_0002_3000
REGISTERED = 7
# virtual_capture: a texture at a GPU address, the buffers the submit binds at GPU addresses, and gpu_guest's CALL kind
TEX_VIRTUAL = 0x6A00_0003_0000
VA_BUF, VA_TEX, VA_NONE, VA_UNMAPPED = 0x6B00_0000_0000, 0x6B00_0001_0000, 0x6B00_0002_0000, 0x6B00_0003_0000
VA_RECORDED = 18 * PAGE                  # a GPU page with a POOL record (pool page 18)
GPU_GUEST = 68


def page_bytes(number):
    return bytes([number]) * PAGE


def digest(data):
    return hashlib.sha256(data).digest()


def op(code, payload=b""):
    """One command record of a recording: its op and its size (header included), then its payload"""
    return struct.pack("<II", code, 8 + len(payload)) + payload


def ubo(addr, size):
    """A BIND_UBO of size bytes at addr: a buffer range the submit reads"""
    return op(OP_BIND_UBO, struct.pack("<3Q", 0, addr, size))


def texture(gpu, size):
    """An nvn_tex of format 1 whose storage is size bytes at gpu: format at +8, storage size at +56, gpu at +96"""
    data = bytearray(128)
    struct.pack_into("<i", data, 8, 1)
    struct.pack_into("<Q", data, 56, size)
    struct.pack_into("<Q", data, 96, gpu)
    return bytes(data)


def records_of(data):
    """Each record of a calls.rxc as (kind, its 32-byte header, its payload), in order"""
    out, off = [], 16
    while off < len(data):
        kind, total = struct.unpack_from("<II", data, off)
        out.append((kind, data[off:off + 32], data[off + 32:off + total]))
        off += total
    return out


def payload_of(records, kind):
    return next(payload for found, _, payload in records if found == kind)


def pool_digests(payload):
    """The digest of each page of a POOL record, in page order"""
    return [payload[16 + 32 * k:48 + 32 * k] for k in range((len(payload) - 16) // 32)]


def page_entries(payload):
    """(page, digest) of each entry of a PAGES record, in order"""
    (count,) = struct.unpack_from("<Q", payload)
    return [(struct.unpack_from("<Q", payload, 8 + 40 * k)[0], payload[16 + 40 * k:48 + 40 * k]) for k in range(count)]


class TrimCapture:
    """A capture folder written record by record: calls.rxc, and the blobs its records name. Each call is followed by its
    reply, as the recorder writes it."""

    def __init__(self, folder):
        self.folder = folder
        self.records = []
        self.blobs = {}
        self.objects = []

    def blob(self, data):
        found = digest(data)
        self.blobs[found] = data
        return found

    def add(self, kind, payload=b""):
        self.records.append((kind, payload))

    def object(self, addr, data):
        found = self.blob(data)
        self.objects.append(found)
        self.add(RECORD_OBJECT, struct.pack("<QQ", addr, len(data)) + found)

    def pool(self, start, pages):
        self.add(RECORD_POOL, struct.pack("<QQ", start, PAGE * len(pages)) + b"".join(self.blob(p) for p in pages))

    def pages(self, entries):
        payload = struct.pack("<Q", len(entries))
        for number, data in entries:
            payload += struct.pack("<Q", number) + self.blob(data)
        self.add(RECORD_PAGES, payload)

    def call(self, kind, *args, result=0):
        self.add(kind, struct.pack("<%dQ" % len(args), *args))
        self.add(kind | 0x10000, struct.pack("<QQ", len(self.records), result))

    def write(self):
        os.makedirs(os.path.join(self.folder, "blobs"))
        for found, data in self.blobs.items():
            with open(os.path.join(self.folder, "blobs", found.hex()), "wb") as f:
                f.write(data)
        with open(os.path.join(self.folder, "calls.rxc"), "wb") as f:
            f.write(b"RXCAPT01" + struct.pack("<II", 1, 0))
            for number, (kind, payload) in enumerate(self.records, 1):
                f.write(struct.pack("<IIQQQ", kind, 32 + len(payload), number, 0, 0) + payload)


def working_set_capture(folder):
    """The synthetic capture of the trim tests, written into folder; returns it as a TrimCapture"""
    c = TrimCapture(folder)
    c.pool(16 * PAGE, [page_bytes(n) for n in range(16, 21)])
    c.pages([(n, page_bytes(n)) for n in range(21, 24)])
    c.object(TEX_PRESENT, texture(19 * PAGE, PAGE))
    c.object(TEX_WRITE, texture(18 * PAGE, PAGE))
    c.object(TEX_READ, texture(21 * PAGE, PAGE))
    c.object(TEX_REGISTERED, texture(20 * PAGE, PAGE))
    c.call(RECORD_REGISTER, REGISTERED, TEX_REGISTERED, 0)
    c.object(RECORDING, ubo(17 * PAGE + 16, 32) + ubo(22 * PAGE, 16) + op(OP_END))
    c.object(HANDLES, struct.pack("<Q", RECORDING))
    c.call(RECORD_SUBMIT, HANDLES, 1)
    c.call(RECORD_WRITE_TEXELS, TEX_WRITE, 0, 0, 0, 0)
    c.call(RECORD_READ_TEXELS, TEX_READ)
    c.call(RECORD_PRESENT, TEX_PRESENT, 0)
    c.write()
    return c


def virtual_capture(folder):
    """The synthetic capture of the virtual-address tests, written into folder: working_set_capture's POOL, and PAGES that
    also list page 0 (recorded, so that a landing on page 0 would show). A submit binds buffers at VA_BUF + 16, at
    VA_RECORDED, at VA_NONE and at VA_UNMAPPED, and a present names a texture stored at VA_TEX. The gpu_guest calls come
    after the present: VA_BUF + 16 answers guest address 17 * PAGE + 16 (pool page 17 at its offset), VA_RECORDED
    answers pool page 19 (its own page has a record, so the answer adds nothing), VA_TEX answers guest page 21, VA_NONE
    has no answer, and VA_UNMAPPED answers 0, unmapped."""
    c = TrimCapture(folder)
    c.pool(16 * PAGE, [page_bytes(n) for n in range(16, 21)])
    c.pages([(0, bytes([1]) * PAGE)] + [(n, page_bytes(n)) for n in range(21, 24)])
    c.object(TEX_VIRTUAL, texture(VA_TEX, PAGE))
    c.object(RECORDING, ubo(VA_BUF + 16, 32) + ubo(VA_RECORDED, 16) + ubo(VA_NONE, 16) + ubo(VA_UNMAPPED, 16) +
             op(OP_END))
    c.object(HANDLES, struct.pack("<Q", RECORDING))
    c.call(RECORD_SUBMIT, HANDLES, 1)
    c.call(RECORD_PRESENT, TEX_VIRTUAL, 0)
    c.call(GPU_GUEST, VA_BUF + 16, result=17 * PAGE + 16)
    c.call(GPU_GUEST, VA_RECORDED, result=19 * PAGE)
    c.call(GPU_GUEST, VA_TEX, result=21 * PAGE)
    c.call(GPU_GUEST, VA_UNMAPPED, result=0)
    c.write()
    return c


class TrimInJobTests(unittest.TestCase):
    """The in-job half of --trim, on a synthetic capture run in a record root: the trimmed calls.rxc, its blobs, and the run"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.record = base / "results"
        self.job = base / "jobs" / jobs.new_id("check")
        self.capture = self.record / "captures" / jobs.new_id("check")
        self.hold = base / "hold-file"
        self.build = base / "build"
        (self.build / "game").mkdir(parents=True)
        (self.build / "game" / "hookshot").write_bytes(HOOKSHOT)
        (self.build / "game" / "hookshot_vk.so").write_bytes(HOOKSHOT_VK)
        (self.build / "build.txt").write_text(build_text(), encoding="utf-8")
        self.messages = []
        self.output = ""
        self.patch(mock.patch.object(hx_capture, "command_outputs", return_value=OUTPUTS))
        self.patch(mock.patch.object(record, "write_journal", side_effect=self.messages.append))
        self.source_folder = self.record / "captures" / "check-20261115T000000Z-aaaaaa"
        self.source = working_set_capture(self.source_folder)
        self.screenshots = {"present/000001.png": b"PNG stand-in present 1"}
        self.run_doc = {"capture": str(self.source_folder), "build": SHA, "device": DEVICE, "frames": 3,
                        "presents": 1, "verdict": "pass"}
        self.assertEqual(self.write_run(self.run_doc), TRIM_RUN)

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def write_run(self, doc):
        """Writes a run whose capture.json is doc, with the screenshots of a capture run; returns its id"""
        run_id, _ = record.write_run(self.record, {key: {} for key in record.FACT_KEYS},
                                     {"capture.json": json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")},
                                     self.screenshots, journal=self.messages.append)
        return run_id

    def trim(self, run=TRIM_RUN, keep=False):
        argv = ["--in-job", str(self.job), "--hold", str(self.hold), "--expect", EXPECT, "--trim", run, "--device", DEVICE,
                "--capture", str(self.capture), "--record", str(self.record), "--build-folder", str(self.build),
                "--closure", CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % ICD]
        if keep:
            argv.append("--keep-registered")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = hx_capture.main(argv)
        self.output = out.getvalue()
        return status

    def trimmed_doc(self):
        return json.loads((self.record / "runs" / TRIMMED_RUN / "files" / "capture.json").read_text(encoding="utf-8"))

    def trimmed_records(self):
        return records_of((self.capture / "calls.rxc").read_bytes())

    def source_records(self):
        return records_of((self.source_folder / "calls.rxc").read_bytes())

    def test_the_pages_a_submit_reads_keep_their_blobs(self):
        self.assertEqual(self.trim(), 0, self.output)
        records = self.trimmed_records()
        self.assertEqual(pool_digests(payload_of(records, RECORD_POOL))[1], digest(page_bytes(17)))
        self.assertEqual(page_entries(payload_of(records, RECORD_PAGES))[1], (22, digest(page_bytes(22))))

    def test_an_unread_pool_page_and_an_unread_pages_entry_name_the_zero_page(self):
        self.assertEqual(self.trim(), 0, self.output)
        records = self.trimmed_records()
        self.assertEqual(pool_digests(payload_of(records, RECORD_POOL))[0], ZERO_SHA)
        self.assertEqual(page_entries(payload_of(records, RECORD_PAGES))[2], (23, ZERO_SHA))

    def test_the_pages_of_the_textures_a_present_and_the_texel_calls_name_keep_their_blobs(self):
        self.assertEqual(self.trim(), 0, self.output)
        records = self.trimmed_records()
        pool = pool_digests(payload_of(records, RECORD_POOL))
        self.assertEqual(pool[2], digest(page_bytes(18)))
        self.assertEqual(pool[3], digest(page_bytes(19)))
        self.assertEqual(page_entries(payload_of(records, RECORD_PAGES))[0], (21, digest(page_bytes(21))))

    def test_a_registered_only_texture_is_zeroed_without_keep_registered(self):
        self.assertEqual(self.trim(), 0, self.output)
        self.assertEqual(pool_digests(payload_of(self.trimmed_records(), RECORD_POOL))[4], ZERO_SHA)

    def test_with_keep_registered_a_registered_texture_keeps_its_pages(self):
        self.assertEqual(self.trim(keep=True), 0, self.output)
        self.assertEqual(pool_digests(payload_of(self.trimmed_records(), RECORD_POOL))[4], digest(page_bytes(20)))
        self.assertIs(self.trimmed_doc()["keep_registered"], True)

    def test_other_records_are_byte_identical_and_the_pool_and_pages_keep_their_headers_and_entry_pages(self):
        self.assertEqual(self.trim(), 0, self.output)
        source, trimmed = self.source_records(), self.trimmed_records()
        self.assertEqual([kind for kind, _, _ in trimmed], [kind for kind, _, _ in source])
        for (kind, head, payload), (_, trimmed_head, trimmed_payload) in zip(source, trimmed):
            with self.subTest(kind=kind):
                self.assertEqual(trimmed_head, head)
                if kind == RECORD_POOL:
                    self.assertEqual(len(trimmed_payload), len(payload))
                    self.assertEqual(trimmed_payload[:16], payload[:16])
                elif kind == RECORD_PAGES:
                    self.assertEqual([number for number, _ in page_entries(trimmed_payload)],
                                     [number for number, _ in page_entries(payload)])
                else:
                    self.assertEqual(trimmed_payload, payload)

    def assert_blobs(self, kept):
        """The new blobs folder holds the named blobs (the objects, the kept pages' and the zero page) and nothing else; each
        named blob but the zero page is a hard link to the capture's own"""
        named = set(self.source.objects) | {digest(page_bytes(n)) for n in kept} | {ZERO_SHA}
        blobs = self.capture / "blobs"
        self.assertEqual(set(os.listdir(blobs)), {found.hex() for found in named})
        for found in named - {ZERO_SHA}:
            self.assertTrue(os.path.samefile(self.source_folder / "blobs" / found.hex(), blobs / found.hex()))
        self.assertEqual((blobs / ZERO_SHA.hex()).read_bytes(), ZERO_PAGE)

    def test_only_the_named_blobs_are_linked_and_the_zero_page_is_written(self):
        self.assertEqual(self.trim(), 0, self.output)
        self.assert_blobs(kept=(17, 18, 19, 21, 22))

    def test_with_keep_registered_the_registered_texture_s_page_is_linked_too(self):
        self.assertEqual(self.trim(keep=True), 0, self.output)
        self.assert_blobs(kept=(17, 18, 19, 20, 21, 22))

    def use_virtual_capture(self):
        """Replaces the run's capture with virtual_capture, in the same folder"""
        shutil.rmtree(self.source_folder)
        self.source = virtual_capture(self.source_folder)

    def test_a_submit_read_at_a_virtual_address_keeps_the_pool_page_its_answer_lands_on(self):
        self.use_virtual_capture()
        self.assertEqual(self.trim(), 0, self.output)
        self.assertEqual(pool_digests(payload_of(self.trimmed_records(), RECORD_POOL))[1], digest(page_bytes(17)))

    def test_a_texture_stored_at_a_virtual_address_keeps_the_guest_page_its_answer_lands_on(self):
        self.use_virtual_capture()
        self.assertEqual(self.trim(), 0, self.output)
        self.assertEqual(page_entries(payload_of(self.trimmed_records(), RECORD_PAGES))[1],
                         (21, digest(page_bytes(21))))

    def test_only_unrecorded_pages_with_an_answer_add_guest_pages(self):
        self.use_virtual_capture()
        self.assertEqual(self.trim(), 0, self.output)
        records = self.trimmed_records()
        pool = pool_digests(payload_of(records, RECORD_POOL))
        self.assertEqual([pool[0], pool[3], pool[4]], [ZERO_SHA] * 3)
        entries = page_entries(payload_of(records, RECORD_PAGES))
        self.assertEqual([entries[0], entries[2], entries[3]], [(0, ZERO_SHA), (22, ZERO_SHA), (23, ZERO_SHA)])
        self.assert_blobs(kept=(17, 18, 21))

    def test_analysis_reads_the_trimmed_capture_with_its_submit_and_its_present(self):
        self.assertEqual(self.trim(), 0, self.output)
        result = analysis.analyze(str(self.capture))
        self.assertEqual((result["presents"], result["submits"]), (1, 1))

    def test_the_run_names_its_capture_trim_size_and_keeps_the_runs_screenshots(self):
        self.assertEqual(self.trim(), 0, self.output)
        doc = self.trimmed_doc()
        calls = self.capture / "calls.rxc"
        blobs = list((self.capture / "blobs").iterdir())
        self.assertEqual(doc["capture"], str(self.capture))
        self.assertEqual(doc["trimmed_from"], TRIM_RUN)
        self.assertIs(doc["keep_registered"], False)
        self.assertEqual(doc["calls"], {"sha256": sha(calls.read_bytes()), "size": calls.stat().st_size})
        self.assertEqual(doc["blobs"], {"count": len(blobs), "bytes": sum(b.stat().st_size for b in blobs)})
        for key in ("build", "device", "frames", "presents", "verdict"):
            self.assertEqual(doc[key], self.run_doc[key])
        manifest, _ = record.read_manifest(self.record, TRIMMED_RUN)
        self.assertEqual(manifest["screenshots"], record.read_manifest(self.record, TRIM_RUN)[0]["screenshots"])

    def test_a_capture_analysis_refuses_exits_2_and_writes_nothing(self):
        (self.source_folder / "calls.rxc").write_bytes(b"NOT A CAPTURE FILE")
        self.assertEqual(self.trim(), 2, self.output)
        self.assertIn("bad magic", self.output)
        self.assertFalse(self.capture.exists())
        self.assertEqual(record.run_ids(self.record), [TRIM_RUN])

    def test_a_missing_screenshot_exits_2_before_the_capture_folder_is_made(self):
        entry = record.read_manifest(self.record, TRIM_RUN)[0]["screenshots"]["present/000001.png"]
        (self.record / "screenshots" / entry["sha256"]).unlink()
        self.assertEqual(self.trim(), 2, self.output)
        self.assertFalse(self.capture.exists())
        self.assertEqual(record.run_ids(self.record), [TRIM_RUN])

    def test_a_run_that_cannot_be_written_removes_the_capture_folder_and_exits_2(self):
        self.patch(mock.patch.object(record, "write_run", side_effect=record.RecordError("run not written")))
        self.assertEqual(self.trim(), 2, self.output)
        self.assertIn("run not written", self.output)
        self.assertFalse(self.capture.exists())

    def test_a_run_written_but_not_journalled_keeps_the_capture_folder_it_names(self):
        self.patch(mock.patch.object(record, "write_journal", side_effect=OSError("journal refused")))
        self.assertEqual(self.trim(), 2, self.output)
        self.assertIn("written but not journalled", self.output)
        self.assertTrue((self.capture / "calls.rxc").is_file())

    def test_a_run_that_is_not_a_capture_run_exits_2_and_writes_nothing(self):
        self.assertEqual(self.write_run({"kind": "play", "capture": str(self.source_folder), "build": SHA,
                                         "device": DEVICE}), PLAY_RUN)
        self.assertEqual(self.trim(run=PLAY_RUN), 2, self.output)
        self.assertIn("is not a capture run", self.output)
        self.assertFalse(self.capture.exists())
        self.assertEqual(record.run_ids(self.record), [TRIM_RUN, PLAY_RUN])


class TrimClientTests(unittest.TestCase):
    """The client half of --trim: the run is read on the host, refusals come before any job, and the job's argv carries the
    run, the device and --keep-registered when given, with no game folder"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "harness.toml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.run_doc = {"capture": "/example/harness/results/captures/check-example", "build": SHA, "device": DEVICE,
                        "frames": 3, "verdict": "pass"}
        self.patch(mock.patch.dict(os.environ, {"HX_CONFIG": str(self.config)}))
        self.remote = self.patch(mock.patch.object(remote, "run", side_effect=self.remote_run))
        self.start = self.patch(mock.patch.object(jobs, "start"))
        self.patch(mock.patch.object(jobs, "follow", return_value=0))

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def remote_run(self, host, script, input=None, check=True):
        text = json.dumps(self.run_doc) if "capture.json" in script else build_text()
        return subprocess.CompletedProcess([host, script], 0, stdout=text, stderr="")

    def test_the_job_carries_the_run_the_device_and_no_frames_scene_or_game(self):
        with quiet():
            status = hx_capture.main(["--trim", TRIM_RUN, "--device", DEVICE])
        self.assertEqual(status, 0)
        self.start.assert_called_once()
        _, job_id, argv = self.start.call_args.args
        self.assertEqual(self.start.call_args.kwargs, {})
        job = "/example/harness/jobs/%s" % job_id
        capture = "/example/harness/results/captures/%s" % job_id
        self.assertEqual(argv, [
            ENTER, CLOSURE, "python", "-I", job + "/tools/scripts/harness/hx.py", "capture", "--in-job", job,
            "--hold", "/example/hold-file", "--expect", EXPECT, "--trim", TRIM_RUN, "--device", DEVICE,
            "--capture", capture, "--record", "/example/harness/results", "--build-folder",
            "/example/harness/builds/%s" % SHA, "--closure", CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % ICD,
        ])

    def test_keep_registered_is_carried_after_the_run_when_given(self):
        with quiet():
            status = hx_capture.main(["--trim", TRIM_RUN, "--device", DEVICE, "--keep-registered"])
        self.assertEqual(status, 0)
        _, _, argv = self.start.call_args.args
        self.assertEqual(argv[argv.index("--trim"):argv.index("--trim") + 3], ["--trim", TRIM_RUN, "--keep-registered"])

    def refused(self, argv, message):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_capture.main(argv)
        self.assertEqual(status, 2, err.getvalue())
        self.assertIn(message, err.getvalue())
        self.start.assert_not_called()

    def test_trim_takes_no_scene_frames_build_or_scene_flags_before_any_remote_call(self):
        for extra in (["--scene", "town-night"], ["--frames", "3"], ["--build", SHA], ["--seconds", "45"],
                      ["--at-scene"], ["--presents", "30"], ["--after", "10"]):
            with self.subTest(extra=extra):
                self.refused(["--trim", TRIM_RUN, "--device", DEVICE, *extra], "--trim takes only")
                self.remote.assert_not_called()

    def test_keep_registered_without_trim_exits_2_before_any_remote_call(self):
        self.refused(["--device", DEVICE, "--keep-registered"], "--keep-registered does not apply without --trim")
        self.remote.assert_not_called()

    def test_a_run_id_that_is_not_one_exits_2_before_any_remote_call(self):
        self.refused(["--trim", "../000001", "--device", DEVICE], "not a run id")
        self.remote.assert_not_called()

    def test_a_run_that_is_not_a_capture_run_exits_2_before_any_job(self):
        self.run_doc = {"kind": "play", "capture": "/example/harness/results/captures/check-example", "build": SHA,
                        "device": DEVICE}
        self.refused(["--trim", TRIM_RUN, "--device", DEVICE], "is not a capture run")

    def test_a_device_other_than_the_runs_exits_2_before_any_job(self):
        self.run_doc = {**self.run_doc, "device": "other"}
        self.refused(["--trim", TRIM_RUN, "--device", DEVICE], "was captured on 'other', not %r" % DEVICE)

    def test_a_run_whose_build_is_not_a_commit_exits_2_before_any_job(self):
        self.run_doc = {**self.run_doc, "build": "not-a-sha"}
        self.refused(["--trim", TRIM_RUN, "--device", DEVICE], "names no build commit")


if __name__ == "__main__":
    unittest.main()
