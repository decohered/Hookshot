"""scripts/harness/hx_opening.py, `hx opening`: a new game taken to play by its opening's inputs alone and its save folder kept in
the scene folder's saves (R1), and the run written as one record that hx verify passes (R7). The in-job half runs in
process against a temporary harness root and a stand-in game that answers the control bridge over its socket
(docs/control.md) and writes its save slot when the opening's inputs arrive. The client half runs through hx.py with fake
ssh, systemd-run and systemctl, so the whole check runs through its job. Checks: the opening reaches play, the slot the
run wrote is copied to the scene folder's saves, and the record passes hx verify; the game starts with the opening skipped
and no save, and the steps go in order with 1800 s timeouts; no slot written in the run, a slot older than the launch, a
game not in play, a refused step, another device, a fallback line in the log and a save folder that cannot be copied are
recorded FAILs with the game quit; a save folder that exists, a bad opening file and a game that does not start exit 2
before a record is written; the record's files, facts and metrics are written, scrubbed of the home path and user name,
and a kernel log that cannot be read is a gap; the client exits 2 for an unknown device, a missing config or an unknown
build revision, and passes the job's status through. Fakes only: no game, GPU, journal or host is used.

python -I -B tests/test_harness_opening.py
"""
import contextlib
import io
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RUN_ALL_SKIP = {"windows": "the stand-in game is a POSIX script started through its shebang, its bridge a Unix socket, "
                           "and the fake tools are POSIX shell scripts"}

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
HX = HARNESS / "hx.py"
sys.path.insert(0, str(HARNESS))
import hx_opening
import hx_verify
import record

HOST = "example-host"
SAVE = "save_a"
DEVICE = "gpu"
EXPECT = "example GPU"
RENDERER = EXPECT + " (Vulkan, native)"
SHA = "3f9c2a1b7d4e6f80a1b2c3d4e5f60718293a4b5c"
CLOSURE = "/nix/store/0123456789abcdefghijklmnopqrstuv-example-hookshot-1.0"
EXE_SHA = record.sha256_bytes(b"exe")
LIB_SHA = record.sha256_bytes(b"lib")
FIXTURE_HOME = "/srv/fixture-home/tester"
FIXTURE_USER = "tester"
RUN = "000001"
WAIT = 1800

OPENING = """save = "save_a"

[[steps]]
inputs = "0:A/8"

[[steps]]
wait = 60

[[steps]]
state = "play"
"""

VULKANINFO = "\n".join([
    "Devices:",
    "========",
    "GPU0:",
    "\tapiVersion         = 1.3.277",
    "\tdriverVersion      = 24.2.8",
    "\tvendorID           = 0x1002",
    "\tdeviceID           = 0x73bf",
    "\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU",
    "\tdeviceName         = " + EXPECT,
    "\tdriverID           = DRIVER_ID_MESA_RADV",
    "\tdriverName         = radv",
    "\tdriverInfo         = Mesa 24.2.8",
    "\tconformanceVersion = 1.3.8.2",
    "\tdeviceUUID         = 00000000-0000-0000-0000-000000000001",
    "",
])

# The stand-in game. It logs each request and its launch's environment, answers the bridge's requests over HOS_CONTROL, and
# writes the save slot the game writes on reaching play when the opening's inputs arrive (FAKE_SLOT: write, old or none).
# FAKE_STATE is the state status reports, FAKE_RENDERER the renderer, FAKE_REFUSE a request it refuses, and FAKE_FALLBACK
# prints the renderer's fallback line into its log. @HARNESS@ and @SCRIPTS@ are the folders of the harness and of hookshot_ctl.
STAND_IN = r'''
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, @HARNESS@)
sys.path.insert(0, @SCRIPTS@)
import hookshot_ctl
import metrics

env = os.environ
ticks = [0]
log = open(env["FAKE_GAME_LOG"], "a", buffering=1)
data = env["XDG_DATA_HOME"]
saves = os.path.join(data, "hookshot", "save", hookshot_ctl.SAVE_USER)
log.write(json.dumps({"cmd": "_start", "env": {name: env.get(name) for name in (
    "HOS_SKIP_OPENING", "XDG_DATA_HOME", "HOOKSHOT_NO_UPDATE_CHECK", "HOS_HEADLESS", "HOS_EXAMPLE")},
    "data": sorted(os.listdir(data))}) + "\n")
os.makedirs(saves, exist_ok=True)
with open("hookshot_frames.csv", "w") as f:
    f.write(",".join(metrics.TRACE_COLUMNS) + "\n")
    for frame in (1, 2, 3):
        f.write(",".join([str(frame)] + ["0.5"] * (len(metrics.TRACE_COLUMNS) - 1)) + "\n")
with open(env["RX_VK_FRAME_LOG"], "w") as f:
    f.write("frame,time_s,frametime_ms\n1,0.0,16.7\n2,0.0167,16.6\n3,0.0333,16.8\n")
print("stand-in: started", flush=True)
if env.get("FAKE_FALLBACK"):
    print("vk: preferred device 0 not available or lacks Vulkan 1.3, falling back to auto selection", flush=True)


def ticker():
    while True:
        time.sleep(0.001)
        ticks[0] += 1


def write_slot(when):
    slot = os.path.join(saves, "slot_03")
    os.makedirs(slot, exist_ok=True)
    for path, text in ((os.path.join(saves, "account.sav"), b"account"),
                       (os.path.join(saves, "option.sav"), b"option"),
                       (os.path.join(slot, "progress.sav"), b"progress")):
        with open(path, "wb") as f:
            f.write(text)
        os.utime(path, (when, when))
    os.utime(slot, (when, when))


def answer(req):
    cmd = req.get("cmd")
    if cmd == env.get("FAKE_REFUSE"):
        return {"ok": False, "error": "refused", "message": "%s refused by the stand-in" % cmd}
    if cmd == "status":
        frame = ticks[0]
        return {"ok": True, "frame": frame, "replayed": frame, "renderer": env["FAKE_RENDERER"],
                "state": env["FAKE_STATE"], "map": "map_a",
                "game": {"event": False, "loading": False, "age": "child", "frame": frame}}
    if cmd == "sequence":
        mode = env.get("FAKE_SLOT", "write")
        if mode == "write":
            write_slot(time.time())
        elif mode == "old":
            write_slot(1000)
        return {"ok": True, "frame": ticks[0], "start": 0, "end": 8}
    if cmd == "wait":
        target = ticks[0] + req.get("frames", 1) if req.get("event") == "present" else ticks[0]
        while ticks[0] < target:
            time.sleep(0.001)
        return {"ok": True, "frame": ticks[0], "state": env["FAKE_STATE"], "waited": 0}
    if cmd == "saves":
        slots = []
        for name in os.listdir(saves):
            path = os.path.join(saves, name)
            if name.startswith("slot_") and os.path.isdir(path):
                slots.append({"slot": name[len("slot_"):], "mtime": int(os.stat(path).st_mtime)})
        return {"ok": True, "frame": ticks[0], "path": saves, "account": os.path.isfile(os.path.join(saves, "account.sav")),
                "option": os.path.isfile(os.path.join(saves, "option.sav")), "slots": slots}
    return {"ok": False, "error": "unknown_cmd", "message": cmd}


def serve(conn):
    f = conn.makefile("rwb", buffering=0)
    for raw in f:
        req = json.loads(raw)
        log.write(json.dumps(req) + "\n")
        if req.get("cmd") == "quit":
            f.write(json.dumps({"id": req.get("id"), "ok": True, "quitting": True}).encode() + b"\n")
            os._exit(0)
        reply = answer(req)
        reply["id"] = req.get("id")
        f.write(json.dumps(reply).encode() + b"\n")


threading.Thread(target=ticker, daemon=True).start()
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(env["HOS_CONTROL"])
srv.listen(4)
while True:
    threading.Thread(target=serve, args=(srv.accept()[0],), daemon=True).start()
'''

# The fake tools the in-job half runs (systemd-cat and journalctl write and read a journal file; the kernel log is read
# from FAKE_KERNEL, which journalctl -k fails without) and the client's systemd-run and systemctl, in one Python file.
SHIMS = {
    "systemd-cat": '#!/bin/sh\nprintf \'%s\\n\' "$(cat)" >> "$FAKE_JOURNAL"\n',
    "journalctl": '#!/bin/sh\nif [ "$1" = "-k" ]; then\n  [ -f "$FAKE_KERNEL" ] || exit 1\n  cat "$FAKE_KERNEL"\n'
                  'else\n  cat "$FAKE_JOURNAL"\nfi\n',
    "vulkaninfo": '#!/bin/sh\ncat "$FAKE_VULKANINFO"\n',
    "nvidia-smi": '#!/bin/sh\ncat "$FAKE_NVIDIA_SMI"\n',
    "sha256sum": '#!/bin/sh\ncat "$FAKE_SHA256SUM"\n',
}

FAKE_JOB_TOOLS = r'''
import json
import os
import subprocess
import sys


def systemd_run(args):
    with open(os.environ["FAKE_RUN_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\n")
    env = dict(os.environ)
    cwd = None
    outputs = {}
    i = 0
    while i < len(args) and args[i].startswith("--"):
        key, _, value = args[i].partition("=")
        if key == "--setenv":
            name, _, text = value.partition("=")
            env[name] = text
        elif key == "--working-directory":
            cwd = value
        elif key == "--property":
            name, _, text = value.partition("=")
            outputs[name] = text
        elif key not in ("--user", "--collect", "--unit"):
            sys.exit("fake systemd-run: option not faked: %s" % args[i])
        i += 1
    command = args[i:]
    if cwd is None or not command:
        sys.exit("fake systemd-run: want --working-directory= and a command")
    streams = []
    for name in ("StandardOutput", "StandardError"):
        kind, _, path = outputs[name].partition(":")
        if kind != "append":
            sys.exit("fake systemd-run: only append: is faked, not %r" % outputs[name])
        streams.append(open(path, "ab"))
    with streams[0], streams[1]:
        subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=streams[0], stderr=streams[1])
    return 0


def systemctl(args):
    # no unit is listed, and is-active answers inactive: each job has ended by the time it is asked
    return 3 if "is-active" in args else 0


tools = {"systemd-run": systemd_run, "systemctl": systemctl}
sys.exit(tools[sys.argv[1]](sys.argv[2:]))
'''

# A stand-in for ssh: it takes exactly what remote.command builds, then runs that word the way a remote login shell does.
FAKE_SSH = """#!/bin/sh
if [ $# -ne 4 ] || [ "$1" != "-o" ] || [ "$2" != "BatchMode=yes" ] || [ "$3" != "example-host" ]; then
  echo "fake ssh: want -o BatchMode=yes example-host <one command word>, got: $*" >&2
  exit 2
fi
exec sh -c "$4"
"""

TOML = '''host = "%s"
hold = "%s"
root = "%s"
dump = "%s"
enter_closure = "%s"

[devices.gpu]
expect = "example GPU"
env = { HOS_EXAMPLE = "1" }
'''


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class OpeningTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="opening-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.stand_in = self.base / "stand_in.py"
        self.stand_in.write_text(STAND_IN.replace("@HARNESS@", repr(str(HARNESS)))
                                 .replace("@SCRIPTS@", repr(str(ROOT / "scripts"))), encoding="utf-8")
        fakes = self.base / "fakes.py"
        fakes.write_text(FAKE_JOB_TOOLS, encoding="utf-8")
        for name, text in SHIMS.items():
            executable(self.bin / name, text)
        for tool in ("systemd-run", "systemctl"):
            executable(self.bin / tool, "#!/bin/sh\nexec %s -I %s %s \"$@\"\n" % (shlex.quote(sys.executable),
                                                                                shlex.quote(str(fakes)), tool))
        executable(self.bin / "ssh", FAKE_SSH)
        executable(self.bin / "python", "#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(sys.executable))
        self.journal = self.base / "journal.txt"
        self.journal.write_text("", encoding="utf-8")
        self.kernel = self.base / "kernel.txt"
        self.kernel.write_text("kernel: one line\n", encoding="utf-8")
        self.vulkaninfo = self.base / "vulkaninfo.txt"
        self.vulkaninfo.write_text(VULKANINFO, encoding="utf-8")
        self.nvidia = self.base / "nvidia-smi.txt"
        self.nvidia.write_text("", encoding="utf-8")
        self.sha256sum = self.base / "sha256sum.txt"
        self.sha256sum.write_text("%s  hookshot\n%s  hookshot_vk.so\n" % (EXE_SHA, LIB_SHA), encoding="utf-8")
        self.game_log = self.base / "game.jsonl"
        self.run_log = self.base / "systemd-run.jsonl"
        self.hold = self.base / "hold"
        self.root = self.base / "harness root"
        self.root.mkdir()
        self.results = self.root / "results"
        self.build_folder = self.root / "builds" / SHA
        self.make_build()
        write(self.root / "scenes" / "opening.toml", OPENING)
        self.enter_path = self.base / "enter-closure"
        executable(self.enter_path, '#!/bin/sh\nshift\nexec "$@"\n')
        self.conf = self.base / "harness.toml"
        self.conf.write_text(TOML % (HOST, self.hold, self.root, self.base / "dump", self.enter_path), encoding="utf-8")
        self.set_env({
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_JOURNAL": str(self.journal),
            "FAKE_KERNEL": str(self.kernel),
            "FAKE_VULKANINFO": str(self.vulkaninfo),
            "FAKE_NVIDIA_SMI": str(self.nvidia),
            "FAKE_SHA256SUM": str(self.sha256sum),
            "FAKE_GAME_LOG": str(self.game_log),
            "FAKE_RUN_LOG": str(self.run_log),
            "FAKE_RENDERER": RENDERER,
            "FAKE_STATE": "play",
            "FAKE_SLOT": "write",
            "HX_CONFIG": str(self.conf),
        })
        for var in ("FAKE_REFUSE", "FAKE_FALLBACK"):
            os.environ.pop(var, None)

    def set_env(self, values):
        patch = mock.patch.dict(os.environ, values)
        patch.start()
        self.addCleanup(patch.stop)

    def make_build(self):
        """The build in the harness root: its build.txt and a game folder whose hookshot is the stand-in"""
        game = self.build_folder / "game"
        game.mkdir(parents=True)
        (game / "marker").write_text(SHA + "\n", encoding="utf-8")
        executable(game / "hookshot", "#!/bin/sh\nexec %s -I %s \"$@\"\n" % (shlex.quote(sys.executable),
                                                                            shlex.quote(str(self.stand_in))))
        write(self.build_folder / "build.txt", "closure: %s\nsha: %s\ndirty: none\nhookshot: %s\n"
              % (CLOSURE, SHA, EXE_SHA))

    def make_job(self, name="job"):
        """A job folder as the job's start makes it: its data folder and a copy of the build's game folder"""
        job = self.base / name
        (job / "data").mkdir(parents=True)
        shutil.copytree(self.build_folder / "game", job / "game")
        return job

    def knob(self, **values):
        self.set_env(values)

    def run_in_job(self, job=None):
        """hx opening's in-job half, in process, with its output: (exit status, stdout, stderr)"""
        job = job or self.make_job()
        argv = ["--in-job", str(job), "--hold", str(self.hold), "--root", str(self.root),
                "--build-folder", str(self.build_folder), "--device", DEVICE, "--expect", EXPECT, "--env", "HOS_EXAMPLE=1"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hx_opening.main(argv)
        return code, out.getvalue(), err.getvalue()

    def game_requests(self):
        if not self.game_log.exists():
            return []
        return [json.loads(line) for line in self.game_log.read_text(encoding="utf-8").splitlines()]

    def started(self):
        starts = [r for r in self.game_requests() if r["cmd"] == "_start"]
        self.assertEqual(len(starts), 1)
        return starts[0]

    def saved(self):
        return self.root / "scenes" / "saves" / SAVE

    def record_manifest(self, run_id=RUN):
        manifest, _ = record.read_manifest(self.results, run_id)
        return manifest

    def record_file(self, name, run_id=RUN):
        return (self.results / "runs" / run_id / "files" / name).read_bytes()

    def assert_record_clean(self, count=1):
        journal = record.parse_journal(self.journal.read_text(encoding="utf-8"))
        self.assertEqual(hx_verify.check(self.results, journal), ([], count))

    def last_line(self, out):
        return out.splitlines()[-1]

    def assert_recorded_fail(self, code, out, reason):
        self.assertEqual(code, 1, out)
        self.assertTrue(self.last_line(out).startswith("opening: FAIL "), out)
        self.assertIn(reason, self.last_line(out))
        self.assertIn("run: " + RUN, out.splitlines())
        report = json.loads(self.record_file("opening.json"))
        self.assertEqual(report["verdict"], "FAIL")
        self.assertIsNotNone(report["reason"])
        self.assertFalse(self.saved().exists(), "a FAIL must not copy the save folder")
        self.assert_record_clean()

    def make_latest(self):
        os.symlink(SHA, self.root / "builds" / "latest")

    def hx_opening(self, *args):
        return subprocess.run([sys.executable, "-I", "-B", str(HX), "opening", *args], capture_output=True, text=True,
                              timeout=300)

    # R1: the opening reaches play, its slot is copied to the scene folder's saves, and the run is recorded

    def test_r1_the_opening_reaches_play_and_the_slot_the_run_wrote_is_copied_to_the_scene_saves(self):
        code, out, err = self.run_in_job()
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.last_line(out).startswith("opening: pass"), out)
        self.assertEqual(sorted(p.name for p in self.saved().iterdir()), ["account.sav", "option.sav", "slot_03"])
        self.assertEqual((self.saved() / "slot_03" / "progress.sav").read_bytes(), b"progress")
        self.assertFalse((self.root / "scenes" / "saves" / (SAVE + ".part")).exists())
        self.assertIn("run: " + RUN, out.splitlines())
        self.assert_record_clean()

    def test_r1_the_steps_go_in_order_and_each_wait_and_input_carries_the_1800_second_timeout(self):
        code, out, err = self.run_in_job()
        self.assertEqual(code, 0, out + err)
        requests = [r for r in self.game_requests() if r["cmd"] in ("sequence", "wait", "saves", "quit")]
        self.assertEqual([r["cmd"] for r in requests], ["sequence", "wait", "wait", "saves", "quit"])
        self.assertEqual(requests[0], {"id": requests[0]["id"], "cmd": "sequence", "script": "0:A/8", "timeout": WAIT})
        self.assertEqual(requests[1]["event"], "present")
        self.assertEqual(requests[1]["frames"], 60)
        self.assertEqual(requests[1]["timeout"], WAIT)
        self.assertEqual(requests[2]["event"], "state")
        self.assertEqual(requests[2]["state"], "play")
        self.assertEqual(requests[2]["timeout"], WAIT)

    def test_r1_the_game_starts_with_the_opening_skipped_and_no_save(self):
        code, out, err = self.run_in_job()
        self.assertEqual(code, 0, out + err)
        started = self.started()
        self.assertEqual(started["env"]["HOS_SKIP_OPENING"], "1")
        self.assertEqual(started["env"]["HOS_EXAMPLE"], "1")
        self.assertEqual(started["env"]["XDG_DATA_HOME"], str(self.base / "job" / "data"))
        self.assertEqual(started["data"], [], "the game must start with an empty data folder: no save")

    # R7: the run is one record with its files, its facts and its metrics

    def test_r7_the_record_holds_the_run_files_the_facts_and_the_report_and_passes_hx_verify(self):
        code, out, err = self.run_in_job()
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertIn("run: " + RUN, lines)
        self.assertIn("manifest: " + record.sha256_bytes(
            (self.results / "runs" / RUN / "manifest.json").read_bytes()), lines)
        manifest = self.record_manifest()
        self.assertEqual(set(manifest["files"]), {"log.txt", "hookshot_frames.csv", "frametimes.csv", "kernel.txt",
                                                  "metrics.json", "opening.toml", "opening.json"})
        self.assertEqual(set(manifest["facts"]), set(record.FACT_KEYS))
        self.assertEqual(self.record_file("opening.toml"), OPENING.encode("utf-8"))
        self.assertEqual(self.record_file("kernel.txt"), b"kernel: one line\n")
        self.assertEqual(json.loads(self.record_file("opening.json")),
                         {"save": SAVE, "slot": "03", "verdict": "pass", "reason": None, "exit_status": 0})
        metrics = json.loads(self.record_file("metrics.json"))
        self.assertEqual(metrics["trace"]["rows"], 3)
        self.assertEqual(metrics["frame_time_ms"]["n"], 3)
        self.assertIn("window", metrics)
        self.assertIn("first", metrics["window"])
        self.assertIn("last", metrics["window"])
        self.assert_record_clean()

    def test_r7_the_record_is_scrubbed_of_the_home_path_and_the_user_name(self):
        write(self.build_folder / "dirty.diff", "+# scratch at %s/scratch by %s\n" % (FIXTURE_HOME, FIXTURE_USER))
        self.knob(HOME=FIXTURE_HOME)
        with mock.patch("getpass.getuser", return_value=FIXTURE_USER):
            code, out, err = self.run_in_job()
        self.assertEqual(code, 0, out + err)
        text = (self.results / "runs" / RUN / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn(FIXTURE_HOME, text)
        self.assertNotIn(FIXTURE_USER, text)
        self.assertIn("~/scratch", text)
        self.assertEqual(self.record_manifest()["facts"]["diff"], "+# scratch at ~/scratch by user\n")

    def test_r7_a_kernel_log_that_cannot_be_read_is_a_gap_and_no_kernel_file(self):
        self.knob(FAKE_KERNEL=str(self.base / "no-such-kernel-log"))
        code, out, err = self.run_in_job()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("kernel.txt", self.record_manifest()["files"])
        metrics = json.loads(self.record_file("metrics.json"))
        self.assertIn("gap", metrics["hangs"]["kernel"])

    # FAIL, recorded: the game is quit and the record is written

    def test_no_slot_written_in_the_run_is_a_recorded_fail(self):
        self.knob(FAKE_SLOT="none")
        code, out, err = self.run_in_job()
        self.assert_recorded_fail(code, out, "no save slot was written in this run")
        self.assertIn("quit", [r["cmd"] for r in self.game_requests()])

    def test_a_slot_older_than_the_launch_is_not_one_the_run_wrote(self):
        self.knob(FAKE_SLOT="old")
        code, out, err = self.run_in_job()
        self.assert_recorded_fail(code, out, "no save slot was written in this run")

    def test_a_game_not_in_play_is_a_recorded_fail_naming_the_state(self):
        self.knob(FAKE_STATE="menu")
        with mock.patch.object(hx_opening, "SETTLE_SECONDS", 1):
            code, out, err = self.run_in_job()
        self.assert_recorded_fail(code, out, "not in play with no event or load")
        self.assertIn("state menu", self.last_line(out))
        self.assertIn("quit", [r["cmd"] for r in self.game_requests()])

    def test_a_refused_step_is_a_recorded_fail_and_the_game_is_still_quit(self):
        self.knob(FAKE_REFUSE="sequence")
        code, out, err = self.run_in_job()
        self.assert_recorded_fail(code, out, "sequence failed: refused")
        self.assertIn("quit", [r["cmd"] for r in self.game_requests()])

    def test_a_renderer_other_than_the_expected_device_is_a_recorded_fail(self):
        self.knob(FAKE_RENDERER="other GPU (Vulkan, native)")
        code, out, err = self.run_in_job()
        self.assert_recorded_fail(code, out, "other GPU (Vulkan, native)")

    def test_a_fallback_line_in_the_log_is_a_recorded_fail(self):
        self.knob(FAKE_FALLBACK="1")
        code, out, err = self.run_in_job()
        self.assert_recorded_fail(code, out, "fell back to another GPU")

    def test_a_save_folder_that_cannot_be_copied_is_a_recorded_fail(self):
        write(self.root / "scenes" / "saves", "not a folder\n")
        code, out, err = self.run_in_job()
        self.assertEqual(code, 1, out + err)
        self.assertIn("could not be copied", self.last_line(out))
        self.assertTrue((self.results / "runs" / RUN).is_dir())
        self.assertEqual(json.loads(self.record_file("opening.json"))["verdict"], "FAIL")
        self.assert_record_clean()

    # exit 2 before anything is launched or recorded

    def test_an_existing_save_folder_exits_2_before_the_launch(self):
        self.saved().mkdir(parents=True)
        code, out, err = self.run_in_job()
        self.assertEqual(code, 2, out + err)
        self.assertIn("exists", err)
        self.assertIn("never overwritten", err)
        self.assertFalse(self.game_log.exists(), "the game was launched")
        self.assertFalse(self.results.exists())

    def test_a_bad_opening_file_exits_2_before_the_launch_naming_the_key(self):
        cases = {
            "an unknown key": ('extra = 1\nsave = "save_a"\nsteps = []\n', "extra"),
            "no save": ("steps = []\n", "save"),
            "no steps": ('save = "save_a"\n', "steps"),
            "a save with a slash": ('save = "a/b"\nsteps = []\n', "save"),
            "a wait of 0": ('save = "save_a"\nsteps = [{ wait = 0 }]\n', "wait"),
            "a wait that is a bool": ('save = "save_a"\nsteps = [{ wait = true }]\n', "wait"),
            "a state that is no state": ('save = "save_a"\nsteps = [{ state = "titles" }]\n', "state"),
            "a step with two keys": ('save = "save_a"\nsteps = [{ wait = 1, state = "play" }]\n', "wait"),
            "a step with an unknown key": ('save = "save_a"\nsteps = [{ press = "A" }]\n', "press"),
            "empty inputs": ('save = "save_a"\nsteps = [{ inputs = "" }]\n', "inputs"),
            "invalid TOML": ('save = "save_a\n', "not valid TOML"),
        }
        for number, (label, (text, key)) in enumerate(cases.items()):
            with self.subTest(case=label):
                write(self.root / "scenes" / "opening.toml", text)
                code, out, err = self.run_in_job(self.make_job("job-%d" % number))
                self.assertEqual(code, 2, out + err)
                self.assertIn(key, err)
                self.assertFalse(self.game_log.exists(), "the game was launched")
                self.assertFalse(self.results.exists())
                self.game_log.unlink(missing_ok=True)

    def test_a_game_that_does_not_start_exits_2_and_writes_no_record(self):
        job = self.make_job()
        (job / "game" / "hookshot").unlink()
        code, out, err = self.run_in_job(job)
        self.assertEqual(code, 2, out + err)
        self.assertIn("did not start", err)
        self.assertFalse(self.results.exists())

    # the client half, through hx.py and the job

    def test_the_client_runs_the_check_as_a_job_and_passes_with_the_slot_saved(self):
        self.make_latest()
        done = self.hx_opening("--device", DEVICE)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = done.stdout.splitlines()
        self.assertIn("build: " + SHA, lines)
        self.assertIn("run: " + RUN, lines)
        self.assertTrue(lines[-1].startswith("opening: pass"), done.stdout)
        (run,) = [json.loads(line) for line in self.run_log.read_text(encoding="utf-8").splitlines()]
        (working,) = [a for a in run if a.startswith("--working-directory=")]
        job = working.split("=", 1)[1]
        self.assertTrue(any(line == "job: " + posixpath.basename(job) for line in lines), done.stdout)
        argv = [str(self.enter_path), CLOSURE, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
                "opening", "--in-job", job, "--hold", str(self.hold), "--root", str(self.root),
                "--build-folder", posixpath.join(str(self.root), "builds", "latest"), "--device", DEVICE,
                "--expect", EXPECT, "--env", "HOS_EXAMPLE=1"]
        self.assertEqual(run[-len(argv):], argv)
        self.assertEqual((self.saved() / "slot_03" / "progress.sav").read_bytes(), b"progress")
        self.assert_record_clean()

    def test_the_client_returns_the_jobs_fail_status_1_and_the_recorded_reason(self):
        self.make_latest()
        self.knob(FAKE_SLOT="none")
        done = self.hx_opening("--device", DEVICE)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(done.stdout.splitlines()[-1].startswith("opening: FAIL "), done.stdout)
        self.assertIn("no save slot was written in this run", done.stdout.splitlines()[-1])

    def test_an_unknown_device_exits_2_on_one_line_naming_the_devices_and_starts_no_job(self):
        done = self.hx_opening("--device", "nope")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("nope", done.stderr)
        self.assertIn(DEVICE, done.stderr)
        self.assertFalse((self.root / "jobs").exists())

    def test_a_config_that_cannot_be_read_exits_2_on_one_line(self):
        self.knob(HX_CONFIG=str(self.base / "no-such-config.toml"))
        done = self.hx_opening("--device", DEVICE)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("cannot read it", done.stderr)

    def test_a_build_rev_that_is_no_commit_here_exits_2(self):
        self.make_latest()
        done = self.hx_opening("--device", DEVICE, "--build", "no-such-rev-zz")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("no commit", done.stderr)
        self.assertFalse((self.root / "jobs").exists())


if __name__ == "__main__":
    unittest.main()
