"""scripts/harness/scenes.py, the suite file, against a temporary folder with neutral placeholder names: suite.toml loads
from a folder outside the repository and its sha256 is the file's bytes' sha256 (R3); a complete suite loads, and a suite
lacking a kind, a cache, a motion or a held-out scene, a soak outside 30 to 60 minutes, a load without load_to, a moving
scene without inputs, a bad route step or an unknown key is refused (R4); select refuses a held-out scene unless final,
returns a scene that is not held out either way, and refuses an unknown name (R6). Fakes only: no dump, GPU, game or host
is read.

`hx run` (scripts/harness/hx_run.py): the in-job half runs one suite scene against a temporary harness root and a stand-in
game that answers the control bridge over its socket (docs/control.md): the suite file is in the record with its sha256
(R3); a held-out scene runs only with --final, an unknown or warm-without-copy scene exits 2 before any launch, and nothing
is written then (R6); the route runs in order and the window holds the scene's frames, the raw outputs and metrics are kept
(R4, R5); each run is one record that hx verify passes, and a wrong device or a fallback is a recorded FAIL (R7). The client
half runs through hx.py with fake ssh, systemd-run and systemctl, so the scene runs through its bench job.

python -I -B tests/test_harness_scenario.py
"""
import contextlib
import hashlib
import io
import json
import os
import posixpath
import shlex
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
import bridge
import hx_run
import hx_verify
import metrics
import record
import scenes

KINDS = ("light", "medium", "dense", "load", "cutscene", "soak")


def toml_value(value):
    """value as TOML: the fixtures are Python dicts, and each test edits its own copy"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {toml_value(item)}" for key, item in value.items()) + " }"
    return "[" + ", ".join(toml_value(item) for item in value) + "]"


def render(suite):
    """suite.toml bytes for a dict of {scene name: {key: value}}"""
    text = ""
    for name, keys in suite.items():
        text += f"[scenes.{json.dumps(name)}]\n"
        text += "".join(f"{key} = {toml_value(value)}\n" for key, value in keys.items())
        text += "\n"
    return text.encode("utf-8")


def complete():
    """A suite with every kind, both caches, both motions, and held-out and other scenes"""
    return {
        "scene_a": {"kind": "light", "cache": "warm", "motion": "still", "save": "save_a", "seconds": 120,
                    "route": [{"teleport": 0}, {"wait": 60}]},
        "scene_b": {"kind": "medium", "cache": "cold", "motion": "moving", "save": "save_a", "seconds": 120,
                    "inputs": "walk_a", "route": [{"teleport": 1}, {"age": "child"}, {"inputs": "walk_a"}]},
        "scene_c": {"kind": "dense", "cache": "warm", "motion": "moving", "save": "save_a", "seconds": 120,
                    "slot": "01", "hour": 23, "inputs": "walk_b", "held_out": True,
                    "route": [{"teleport": 2}, {"age": "adult"}, {"inputs": "walk_b"}]},
        "scene_d": {"kind": "load", "cache": "cold", "motion": "still", "save": "save_a", "seconds": 60,
                    "load_to": 3, "held_out": True, "route": [{"teleport": 2}, {"wait": 1}]},
        "scene_e": {"kind": "cutscene", "cache": "warm", "motion": "still", "save": "save_a", "seconds": 60,
                    "hour": 0, "route": [{"wait": 1}]},
        "scene_f": {"kind": "soak", "cache": "cold", "motion": "still", "save": "save_a", "seconds": 1800,
                    "route": [{"teleport": "dest_a"}]},
    }


class SuiteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def write(self, data):
        path = self.folder / "suite.toml"
        path.write_bytes(data)
        return path

    def assertRefused(self, data, *needles):
        """loading data raises SceneError, and the message names the file and each needle"""
        path = self.write(data)
        with self.assertRaises(scenes.SceneError) as caught:
            scenes.load(path)
        message = str(caught.exception)
        self.assertIn(str(path), message)
        for needle in needles:
            self.assertIn(needle, message)


class FileTest(SuiteTest):
    def test_the_suite_loads_from_a_folder_outside_the_repository(self):
        path = self.write(render(complete()))
        self.assertFalse(path.resolve().is_relative_to(ROOT.resolve()))
        suite = scenes.load(path)
        self.assertEqual(suite.path, str(path))
        self.assertEqual(sorted(suite.scenes), sorted(complete()))

    def test_the_hash_is_the_sha256_of_the_file_bytes(self):
        data = render(complete())
        suite = scenes.load(self.write(data))
        self.assertEqual(suite.sha256, hashlib.sha256(data).hexdigest())

    def test_one_changed_byte_changes_the_hash(self):
        data = render(complete()) + b"# note: a\n"
        before = scenes.load(self.write(data))
        after = scenes.load(self.write(data.replace(b"note: a", b"note: b")))
        self.assertNotEqual(before.sha256, after.sha256)
        self.assertEqual(before.scenes, after.scenes)

    def test_a_missing_file_is_refused_naming_it(self):
        path = self.folder / "absent.toml"
        with self.assertRaises(scenes.SceneError) as caught:
            scenes.load(path)
        self.assertIn(str(path), str(caught.exception))

    def test_text_that_is_not_toml_or_utf8_is_refused_naming_the_file(self):
        for label, data in (("not TOML", b"[scenes.scene_a\n"), ("not UTF-8", b"\xff\xfe")):
            with self.subTest(label):
                self.assertRefused(data)


class SuiteRulesTest(SuiteTest):
    def test_a_complete_suite_loads_with_its_scenes(self):
        suite = scenes.load(self.write(render(complete())))
        self.assertEqual(sorted(suite.scenes), sorted(complete()))
        a = suite.scenes["scene_a"]
        self.assertEqual((a.name, a.kind, a.cache, a.motion, a.held_out), ("scene_a", "light", "warm", "still", False))
        self.assertEqual(a.route, ({"teleport": 0}, {"wait": 60}))
        self.assertEqual((a.slot, a.hour, a.inputs, a.load_to), (None, None, None, None))
        c = suite.scenes["scene_c"]
        self.assertEqual((c.slot, c.hour, c.inputs, c.held_out), ("01", 23, "walk_b", True))
        self.assertEqual(suite.scenes["scene_d"].load_to, 3)
        self.assertEqual(suite.scenes["scene_f"].seconds, 1800)

    def test_each_kind_is_required(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                suite = {name: keys for name, keys in complete().items() if keys["kind"] != kind}
                self.assertRefused(render(suite), f'kind = "{kind}"')

    def test_both_caches_are_required(self):
        for missing, other in (("cold", "warm"), ("warm", "cold")):
            with self.subTest(missing=missing):
                suite = complete()
                for keys in suite.values():
                    if keys["cache"] == missing:
                        keys["cache"] = other
                self.assertRefused(render(suite), f'cache = "{missing}"')

    def test_both_motions_are_required(self):
        for missing, other in (("still", "moving"), ("moving", "still")):
            with self.subTest(missing=missing):
                suite = complete()
                for keys in suite.values():
                    if keys["motion"] == missing:
                        keys["motion"] = other
                        keys.setdefault("inputs", "walk_a")
                self.assertRefused(render(suite), f'motion = "{missing}"')

    def test_both_held_out_and_not_held_out_scenes_are_required(self):
        for held in (True, False):
            with self.subTest(held_out=held):
                suite = complete()
                for keys in suite.values():
                    keys["held_out"] = held
                missing = "false" if held else "true"
                self.assertRefused(render(suite), f"held_out = {missing}")

    def test_a_soak_runs_30_to_60_minutes(self):
        for seconds in (1799, 3601):
            with self.subTest(seconds=seconds):
                suite = complete()
                suite["scene_f"]["seconds"] = seconds
                self.assertRefused(render(suite), "scene_f", "seconds")
        for seconds in (1800, 3600):
            with self.subTest(seconds=seconds):
                suite = complete()
                suite["scene_f"]["seconds"] = seconds
                self.assertEqual(scenes.load(self.write(render(suite))).scenes["scene_f"].seconds, seconds)

    def test_a_load_scene_needs_load_to_and_other_scenes_do_not_have_it(self):
        suite = complete()
        del suite["scene_d"]["load_to"]
        self.assertRefused(render(suite), "scene_d", "load_to")
        suite = complete()
        suite["scene_a"]["load_to"] = 1
        self.assertRefused(render(suite), "scene_a", "load_to")

    def test_a_moving_scene_needs_inputs(self):
        suite = complete()
        del suite["scene_b"]["inputs"]
        self.assertRefused(render(suite), "scene_b", "inputs")

    def test_a_bad_route_is_refused_naming_the_route(self):
        bad_routes = {
            "not a list": "wait",
            "a step that is not a table": [3],
            "a step with two keys": [{"wait": 1, "age": "child"}],
            "a step with no key": [{}],
            "an unknown key": [{"jump": 1}],
            "a wait below 1": [{"wait": 0}],
            "a wait that is not a whole number": [{"wait": "60"}],
            "a wait that is a bool": [{"wait": True}],
            "an age that is neither child nor adult": [{"age": "teen"}],
            "a teleport below 0": [{"teleport": -1}],
            "a teleport that is a float": [{"teleport": 1.5}],
            "a teleport that is a bool": [{"teleport": True}],
            "an empty inputs script": [{"inputs": ""}],
            "an inputs script that is not a string": [{"inputs": 3}],
        }
        for label, route in bad_routes.items():
            with self.subTest(label):
                suite = complete()
                suite["scene_a"]["route"] = route
                self.assertRefused(render(suite), "scene_a", "route")

    def test_a_misdescribed_scene_is_refused_naming_the_key(self):
        cases = {
            "a kind that is not one": ("scene_a", "kind", "heavy"),
            "a cache that is not warm or cold": ("scene_a", "cache", "tepid"),
            "a motion that is not still or moving": ("scene_a", "motion", "drifting"),
            "a save that is not a folder name": ("scene_a", "save", "../save_a"),
            "a slot that is one digit": ("scene_a", "slot", "1"),
            "a slot that is three digits": ("scene_a", "slot", "100"),
            "a slot that is a number": ("scene_a", "slot", 1),
            "an hour past 23": ("scene_a", "hour", 24),
            "an hour that is a bool": ("scene_a", "hour", True),
            "seconds that are a string": ("scene_a", "seconds", "120"),
            "seconds below 1": ("scene_a", "seconds", 0),
            "held_out that is not a bool": ("scene_a", "held_out", "yes"),
            "an inputs script that is not a string": ("scene_b", "inputs", 3),
        }
        for label, (name, key, value) in cases.items():
            with self.subTest(label):
                suite = complete()
                suite[name][key] = value
                self.assertRefused(render(suite), name, key)

    def test_a_missing_required_key_is_refused_naming_it(self):
        for key in ("kind", "cache", "motion", "save", "route", "seconds"):
            with self.subTest(key):
                suite = complete()
                del suite["scene_a"][key]
                self.assertRefused(render(suite), "scene_a", key)

    def test_an_unknown_key_is_refused_naming_it(self):
        suite = complete()
        suite["scene_a"]["speed"] = 2
        self.assertRefused(render(suite), "scene_a", "speed")
        self.assertRefused(b"title = 1\n" + render(complete()), "title")

    def test_a_method_table_is_accepted_uninterpreted_and_other_top_level_keys_are_refused(self):
        method = (b"\n[method]\nconfidence = 0.95\nruns_per_arm = 3\nwarmup_seconds = 60\nresamples = 1000\nseed = 7\n"
                  b'note = "anything"\nkind = "heavy"\nlist = [1, 2]\n')
        data = render(complete()) + method
        suite = scenes.load(self.write(data))
        self.assertEqual(suite.scenes, scenes.load(self.write(render(complete()))).scenes)
        self.assertEqual(suite.sha256, hashlib.sha256(data).hexdigest())
        refused = {"a number": (b"method = 3\n", "method"), "a string": (b'method = "x"\n', "method"),
                   "a list": (b"method = [1]\n", "method"), "another unknown key": (b"title = 1\n", "title")}
        for label, (top, needle) in refused.items():
            with self.subTest(label):
                self.assertRefused(top + render(complete()), needle)

    def test_a_scene_name_is_letters_digits_underscore_or_dash(self):
        for name in ("bad name", "a.b"):
            with self.subTest(name):
                suite = complete()
                suite[name] = suite.pop("scene_a")
                self.assertRefused(render(suite), name)

    def test_a_suite_without_a_scenes_table_is_refused(self):
        self.assertRefused(b"", "scenes")
        self.assertRefused(b"scenes = 3\n", "scenes")


class SelectTest(SuiteTest):
    def setUp(self):
        super().setUp()
        self.suite = scenes.load(self.write(render(complete())))

    def test_a_held_out_scene_needs_final(self):
        with self.assertRaises(scenes.SceneError) as caught:
            scenes.select(self.suite, "scene_c", False)
        self.assertIn("scene_c", str(caught.exception))
        self.assertIn("final", str(caught.exception))

    def test_a_held_out_scene_is_returned_with_final(self):
        scene = scenes.select(self.suite, "scene_c", True)
        self.assertEqual(scene.name, "scene_c")
        self.assertTrue(scene.held_out)

    def test_a_scene_that_is_not_held_out_is_returned_either_way(self):
        for final in (False, True):
            with self.subTest(final=final):
                self.assertEqual(scenes.select(self.suite, "scene_a", final).name, "scene_a")

    def test_an_unknown_name_is_refused_either_way(self):
        for final in (False, True):
            with self.subTest(final=final):
                with self.assertRaises(scenes.SceneError) as caught:
                    scenes.select(self.suite, "scene_z", final)
                self.assertIn("scene_z", str(caught.exception))


class FoldersTest(unittest.TestCase):
    def test_the_scene_and_save_folders_are_under_the_root(self):
        self.assertEqual(scenes.scene_dir("/harness"), "/harness/scenes")
        self.assertEqual(scenes.save_dir("/harness", "save_a"), "/harness/scenes/saves/save_a")


# hx run: one suite scene run once as a bench job. The in-job half runs in process against a temporary harness root and
# a stand-in game (below) that answers the control bridge over its socket, writes the frame trace beside itself and the
# frame log when it quits, and logs each request it gets. The client half runs through hx.py with fake ssh, systemd-run
# and systemctl, in the style of tests/test_harness_jobs.py.

HOST = "example-host"
DEVICE = "gpu"
EXPECT = "example GPU"
RENDERER = EXPECT + " (Vulkan, native)"
SHA = "3f9c2a1b7d4e6f80a1b2c3d4e5f60718293a4b5c"
CLOSURE = "/nix/store/0123456789abcdefghijklmnopqrstuv-example-hookshot-1.0"
EXE_SHA = record.sha256_bytes(b"exe")
LIB_SHA = record.sha256_bytes(b"lib")
RUN = "000001"
SAVE = "save_a"
SUITE_FILE = "suite.toml"
RENDERER_OTHER = "other GPU (Vulkan, native)"

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

# The stand-in game. It logs each request and its launch (the save folder it has, its build's marker, its device variable),
# prints the renderer's metric lines and FAKE_FALLBACK's fallback line, answers the bridge's requests over HOS_CONTROL, and
# on quit writes the frame trace for every frame presented. FAKE_RENDERER is the renderer status reports, FAKE_REFUSE a
# request it refuses. @HARNESS@ and @SCRIPTS@ are the folders of the harness and of hookshot_ctl.
STAND_IN = r'''
import json
import os
import socket
import sys
import threading

sys.path.insert(0, @HARNESS@)
sys.path.insert(0, @SCRIPTS@)
import hookshot_ctl
import metrics

env = os.environ
log = open(env["FAKE_GAME_LOG"], "a", buffering=1)
ticks = [0]
here = ["start_map"]
saves = os.path.join(env["XDG_DATA_HOME"], "hookshot", "save", hookshot_ctl.SAVE_USER)
marker = open("marker").read() if os.path.exists("marker") else None
log.write(json.dumps({"cmd": "_start", "marker": marker,
                      "saves": sorted(os.listdir(saves)) if os.path.isdir(saves) else None,
                      "env": {name: env.get(name) for name in ("HOS_EXAMPLE", "HOS_HEADLESS", "XDG_DATA_HOME")}}) + "\n")
print("stand-in: started", flush=True)
print("vk: a frame's replay time (ms): submit 1.0, present 2.0, draw 3.0, descriptors 4.0, texture lookups 5.0, "
      "uploads 6.0, pipelines 7.0, programs 8.0, commands 9.0, dispatch 10.0, memory sync 11.0, queue submit 12.0, "
      "slot wait 13.0", flush=True)
print("vk: GPU time 12.5 ms a frame (the longest submission 3.25 ms)", flush=True)
print("vk: memory: working set 512 MB (peak 600), private 300 MB", flush=True)
print("vk: VRAM: 1024 MB used of 4096 MB allowed", flush=True)
if env.get("FAKE_FALLBACK"):
    print("vk: preferred device 0 not available or lacks Vulkan 1.3, falling back to auto selection", flush=True)


def write_outputs():
    count = ticks[0]
    with open("hookshot_frames.csv", "w") as f:
        f.write(",".join(metrics.TRACE_COLUMNS) + "\n")
        for frame in range(1, count + 1):
            f.write(",".join([str(frame)] + ["0.5"] * (len(metrics.TRACE_COLUMNS) - 1)) + "\n")
    with open(env["RX_VK_FRAME_LOG"], "w") as f:
        f.write("frame,time_s,frametime_ms\n")
        for frame in range(1, count + 1):
            f.write("%d,%.4f,16.7\n" % (frame, frame / 60))


def answer(req):
    cmd = req.get("cmd")
    if cmd == env.get("FAKE_REFUSE"):
        return {"ok": False, "error": "refused", "message": "%s refused by the stand-in" % cmd}
    if cmd == "status":
        return {"ok": True, "frame": ticks[0], "replayed": ticks[0], "draws": 2410.3,
                "renderer": env["FAKE_RENDERER"], "state": "play", "map": here[0],
                "game": {"event": False, "loading": False, "age": "child"}}
    if cmd == "teleport":
        here[0] = "dest_%s" % req["to"]
        return {"ok": True, "frame": ticks[0], "teleport": 1, "sent": True, "map": here[0]}
    if cmd == "age":
        return {"ok": True, "frame": ticks[0], "age": req["to"], "sent": True, "map": here[0]}
    if cmd == "sequence":
        return {"ok": True, "frame": ticks[0], "start": 0, "end": 8}
    if cmd == "wait":
        if req.get("event") == "present":
            ticks[0] += req.get("frames", 1)
        return {"ok": True, "frame": ticks[0], "state": "play", "waited": 0, "map": here[0]}
    return {"ok": False, "error": "unknown_cmd", "message": str(cmd)}


def serve(conn):
    f = conn.makefile("rwb", buffering=0)
    for raw in f:
        req = json.loads(raw)
        log.write(json.dumps(req) + "\n")
        if req.get("cmd") == "quit":
            write_outputs()
            f.write(json.dumps({"id": req.get("id"), "ok": True, "quitting": True}).encode() + b"\n")
            os._exit(0)
        reply = answer(req)
        reply["id"] = req.get("id")
        f.write(json.dumps(reply).encode() + b"\n")


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

CONFIG_TEXT = '''host = "%s"
hold = "%s"
root = "%s"
dump = "%s"
enter_closure = "%s"

[devices.gpu]
expect = "example GPU"
env = { HOS_EXAMPLE = "1" }
'''


def write_file(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class RunFixture(unittest.TestCase):
    """A harness root with the complete suite and a save, a build whose game is the stand-in, and the fake tools on PATH.
    The bridge's switch gap is off, so a test's teleports and ages do not wait."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="run-")
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
            make_executable(self.bin / name, text)
        for tool in ("systemd-run", "systemctl"):
            make_executable(self.bin / tool, "#!/bin/sh\nexec %s -I %s %s \"$@\"\n" % (
                shlex.quote(sys.executable), shlex.quote(str(fakes)), tool))
        make_executable(self.bin / "ssh", FAKE_SSH)
        make_executable(self.bin / "python", "#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(sys.executable))
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
        self.make_game(self.build_folder / "game", "cold")
        write_file(self.build_folder / "build.txt",
                   "closure: %s\nsha: %s\ndirty: none\nhookshot: %s\n" % (CLOSURE, SHA, EXE_SHA))
        self.suite_bytes = render(complete())
        (self.root / "scenes").mkdir()
        (self.root / "scenes" / SUITE_FILE).write_bytes(self.suite_bytes)
        self.make_save()
        self.enter_path = self.base / "enter-closure"
        make_executable(self.enter_path, '#!/bin/sh\nshift\nexec "$@"\n')
        self.conf = self.base / "harness.toml"
        self.conf.write_text(CONFIG_TEXT % (HOST, self.hold, self.root, self.base / "dump", self.enter_path),
                             encoding="utf-8")
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
            "HX_CONFIG": str(self.conf),
        })
        switch = mock.patch.object(bridge, "SWITCH_GAP", 0)
        switch.start()
        self.addCleanup(switch.stop)

    def set_env(self, values):
        patch = mock.patch.dict(os.environ, values)
        patch.start()
        self.addCleanup(patch.stop)

    def make_game(self, folder, marker):
        """A game folder: its hookshot is the stand-in, and its marker names the build or the warm copy it is"""
        folder.mkdir(parents=True)
        (folder / "marker").write_text(marker, encoding="utf-8")
        make_executable(folder / "hookshot", "#!/bin/sh\nexec %s -I %s \"$@\"\n" % (shlex.quote(sys.executable),
                                                                                shlex.quote(str(self.stand_in))))

    def make_save(self):
        save = Path(scenes.save_dir(self.root, SAVE))
        for name, text in (("account.sav", "account"), ("option.sav", "option"), ("slot_01/progress.sav", "one"),
                           ("slot_02/progress.sav", "two")):
            write_file(save / name, text)

    def job_path(self, name="job"):
        return self.base / name

    def make_job(self, name):
        """A job folder as the job's start makes it: its data folder, and no game yet (the in-job half copies it in)"""
        job = self.job_path(name)
        (job / "data").mkdir(parents=True)
        return job

    def run_in_job(self, scene, final=False, job_name="job"):
        """hx run's in-job half, in process, with its output: (exit status, stdout, stderr)"""
        self.make_job(job_name)
        argv = ["--in-job", str(self.job_path(job_name)), "--hold", str(self.hold), "--root", str(self.root),
                "--build-folder", str(self.build_folder), "--device", DEVICE, "--expect", EXPECT,
                "--env", "HOS_EXAMPLE=1", "--scene", scene]
        if final:
            argv.append("--final")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hx_run.main(argv)
        return code, out.getvalue(), err.getvalue()

    def game_requests(self):
        if not self.game_log.exists():
            return []
        return [json.loads(line) for line in self.game_log.read_text(encoding="utf-8").splitlines()]

    def started(self):
        starts = [r for r in self.game_requests() if r["cmd"] == "_start"]
        self.assertEqual(len(starts), 1)
        return starts[0]

    def record_manifest(self, run_id=RUN):
        manifest, _ = record.read_manifest(self.results, run_id)
        return manifest

    def record_file(self, name, run_id=RUN):
        return (self.results / "runs" / run_id / "files" / name).read_bytes()

    def assert_record_clean(self, count=1):
        journal = record.parse_journal(self.journal.read_text(encoding="utf-8"))
        self.assertEqual(hx_verify.check(self.results, journal), ([], count))


class RunInJobTest(RunFixture):
    # R3: the record holds the suite file and its sha256

    def test_r3_the_record_holds_the_suite_file_and_its_sha256(self):
        code, out, err = self.run_in_job("scene_b")
        self.assertEqual(code, 0, out + err)
        digest = record.sha256_bytes(self.suite_bytes)
        self.assertEqual(self.record_file(SUITE_FILE), self.suite_bytes)
        self.assertEqual(self.record_manifest()["facts"]["suite_sha256"], digest)
        self.assertIn("suite: " + digest, out.splitlines())
        self.assert_record_clean()

    # R4: the route runs in order, the window holds the scene's frames, and a warm scene starts from its warm copy

    def test_r4_the_route_runs_in_order_and_the_window_holds_the_scenes_frames(self):
        code, out, err = self.run_in_job("scene_b")
        self.assertEqual(code, 0, out + err)
        moves = [r for r in self.game_requests() if r["cmd"] in ("teleport", "age", "sequence", "wait")]
        shape = [(r["cmd"], r.get("event"), r.get("to"), r.get("script"), r.get("wait", True)) for r in moves]
        self.assertEqual(shape[:6], [
            ("teleport", None, 1, None, True),
            ("wait", "map", None, None, True),
            ("age", None, "child", None, True),
            ("wait", "loading_end", None, None, True),
            ("sequence", None, None, "walk_a", True),
            ("sequence", None, None, "walk_a", False),
        ])
        self.assertEqual([(r["event"], r["frames"]) for r in moves[6:]], [("present", 60)] * 120)

    def test_r4_a_warm_scene_starts_from_the_warm_copy_with_its_save_slot_and_hour(self):
        self.make_game(self.build_folder / "warm" / DEVICE / "game", "warm")
        code, out, err = self.run_in_job("scene_c", final=True)
        self.assertEqual(code, 0, out + err)
        started = self.started()
        self.assertEqual(started["marker"], "warm")
        self.assertEqual(started["saves"], ["account.sav", "option.sav", "slot_01"])
        self.assertEqual(started["env"]["HOS_EXAMPLE"], "1")
        self.assertEqual((self.job_path() / "run" / "live.txt").read_text(encoding="utf-8"), "!cheat 123\n")

    # R5: the run keeps its raw outputs and its metrics: the frame times, every trace column, replay, GPU time, RSS,
    # VRAM and the exit status

    def test_r5_the_run_keeps_its_raw_outputs_and_its_metrics(self):
        code, out, err = self.run_in_job("scene_b")
        self.assertEqual(code, 0, out + err)
        names = set(self.record_manifest()["files"])
        self.assertEqual(names, {"log.txt", "hookshot_frames.csv", "frametimes.csv", "kernel.txt", "metrics.json",
                                 "scene.json", SUITE_FILE})
        self.assertIn(b"stand-in: started", self.record_file("log.txt"))
        trace = self.record_file("hookshot_frames.csv").decode("utf-8").splitlines()
        self.assertEqual(trace[0], ",".join(metrics.TRACE_COLUMNS))
        self.assertEqual(len(trace), 1 + 7200)
        report = json.loads(self.record_file("metrics.json"))
        self.assertEqual(report["trace"]["rows"], 7200)
        self.assertEqual(sorted(report["trace"]["columns"]), sorted(metrics.TRACE_COLUMNS[1:]))
        self.assertEqual(report["frame_time_ms"]["n"], 7200)
        self.assertEqual(report["replay_ms"]["submit"]["mean"], 1.0)
        self.assertEqual(report["gpu_ms"]["max"], 12.5)
        self.assertEqual(report["longest_submission_ms"]["max"], 3.25)
        self.assertEqual(report["rss_mb"]["peak_mb"]["max"], 600)
        self.assertEqual(report["vram_mb"]["used_mb"]["max"], 1024)
        self.assertEqual(report["hangs"]["exit_status"], 0)
        self.assertEqual((report["window"]["first"], report["window"]["last"]), (0, 7200))
        self.assertEqual(report["window"]["trace"]["rows"], 7200)
        self.assertEqual(report["window"]["frame_time_ms"]["n"], 7200)

    # R6: a held-out scene runs only with --final; anything refused exits 2 before the game is launched or recorded

    def test_r6_a_held_out_scene_without_final_exits_2_before_any_launch(self):
        code, out, err = self.run_in_job("scene_d")
        self.assertEqual(code, 2, out + err)
        self.assertIn("held out", err)
        self.assertFalse(self.game_log.exists(), "the game was launched")
        self.assertFalse((self.job_path() / "game").exists(), "the game was copied into the job")
        self.assertFalse(self.results.exists(), "a record was written")

    def test_r6_with_final_the_held_out_scene_runs_and_is_recorded(self):
        code, out, err = self.run_in_job("scene_d", final=True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.started()["marker"], "cold")
        self.assertTrue(json.loads(self.record_file("scene.json"))["final"])
        self.assert_record_clean()

    def test_r6_an_unknown_scene_exits_2_before_any_launch(self):
        code, out, err = self.run_in_job("scene_z", final=True)
        self.assertEqual(code, 2, out + err)
        self.assertIn("scene_z", err)
        self.assertFalse(self.game_log.exists(), "the game was launched")
        self.assertFalse(self.results.exists(), "a record was written")

    def test_a_warm_scene_without_a_warm_copy_exits_2_before_any_launch(self):
        code, out, err = self.run_in_job("scene_c", final=True)
        self.assertEqual(code, 2, out + err)
        self.assertIn("warm", err)
        self.assertFalse(self.game_log.exists(), "the game was launched")
        self.assertFalse(self.results.exists(), "a record was written")

    # R7: every run writes one record that hx verify passes; a wrong device or a fallback is a recorded FAIL

    def test_r7_each_run_writes_one_record_and_hx_verify_passes(self):
        code, out, err = self.run_in_job("scene_b")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.run_in_job("scene_d", final=True, job_name="job-2")
        self.assertEqual(code, 0, out + err)
        self.assertIn("run: 000002", out.splitlines())
        self.assert_record_clean(count=2)

    def test_r7_a_wrong_device_is_a_recorded_fail_with_the_game_quit(self):
        self.set_env({"FAKE_RENDERER": RENDERER_OTHER})
        code, out, err = self.run_in_job("scene_b")
        self.assertEqual(code, 1, out + err)
        last = out.splitlines()[-1]
        self.assertTrue(last.startswith("hx run: FAIL "), out)
        self.assertIn(RENDERER_OTHER, last)
        self.assertIn("run: " + RUN, out.splitlines())
        scene = json.loads(self.record_file("scene.json"))
        self.assertEqual(scene["verdict"], "FAIL")
        self.assertIn(RENDERER_OTHER, scene["reason"])
        self.assertIn("quit", [r["cmd"] for r in self.game_requests()])
        self.assert_record_clean()

    def test_r7_a_fallback_line_in_the_log_is_a_recorded_fail(self):
        self.set_env({"FAKE_FALLBACK": "1"})
        code, out, err = self.run_in_job("scene_b")
        self.assertEqual(code, 1, out + err)
        self.assertIn("fell back to another GPU", out.splitlines()[-1])
        self.assert_record_clean()

    def test_a_refused_teleport_is_a_recorded_fail_and_the_game_is_quit(self):
        self.set_env({"FAKE_REFUSE": "teleport"})
        code, out, err = self.run_in_job("scene_b")
        self.assertEqual(code, 1, out + err)
        self.assertIn("teleport failed: refused", out.splitlines()[-1])
        self.assertIn("quit", [r["cmd"] for r in self.game_requests()])
        self.assertIn("run: " + RUN, out.splitlines())
        self.assert_record_clean()

    # the build's warm copy for this device: made by a passing cold run when absent, never replaced

    def test_a_passing_cold_run_makes_the_build_warm_copy_from_its_game(self):
        code, out, err = self.run_in_job("scene_d", final=True)
        self.assertEqual(code, 0, out + err)
        warm = self.build_folder / "warm" / DEVICE / "game"
        self.assertEqual((warm / "marker").read_text(encoding="utf-8"), "cold")
        self.assertFalse((warm.parent / "game.part").exists())

    def test_a_failing_run_makes_no_warm_copy(self):
        self.set_env({"FAKE_RENDERER": RENDERER_OTHER})
        code, out, err = self.run_in_job("scene_d", final=True)
        self.assertEqual(code, 1, out + err)
        self.assertFalse((self.build_folder / "warm").exists())

    def test_an_existing_warm_copy_is_never_replaced(self):
        self.make_game(self.build_folder / "warm" / DEVICE / "game", "kept")
        code, out, err = self.run_in_job("scene_d", final=True)
        self.assertEqual(code, 0, out + err)
        self.assertEqual((self.build_folder / "warm" / DEVICE / "game" / "marker").read_text(encoding="utf-8"), "kept")

    def test_a_passing_run_whose_warm_copy_cannot_be_made_still_exits_0_with_a_warning(self):
        write_file(self.build_folder / "warm", "a file, not a folder\n")
        code, out, err = self.run_in_job("scene_d", final=True)
        self.assertEqual(code, 0, out + err)
        self.assertIn("run: " + RUN, out.splitlines())
        self.assertIn("hx run: pass", out.splitlines())
        self.assertIn("warm copy is not made", err)
        self.assert_record_clean()

    def test_the_run_prints_its_id_its_manifest_hash_and_the_suite_hash(self):
        code, out, err = self.run_in_job("scene_b")
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        manifest = (self.results / "runs" / RUN / "manifest.json").read_bytes()
        self.assertIn("run: " + RUN, lines)
        self.assertIn("manifest: " + record.sha256_bytes(manifest), lines)
        self.assertIn("suite: " + record.sha256_bytes(self.suite_bytes), lines)


class RunClientTest(RunFixture):
    def setUp(self):
        super().setUp()
        os.symlink(SHA, self.root / "builds" / "latest")

    def hx_run(self, *args):
        return subprocess.run([sys.executable, "-I", "-B", str(HX), "run", *args], capture_output=True, text=True,
                              timeout=300)

    def started_jobs(self):
        if not self.run_log.exists():
            return []
        return [json.loads(line) for line in self.run_log.read_text(encoding="utf-8").splitlines()]

    def test_the_client_runs_a_final_scene_as_a_bench_job_with_its_device_env_and_records_it(self):
        done = self.hx_run("scene_d", "--device", DEVICE, "--final")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = done.stdout.splitlines()
        self.assertIn("build: " + SHA, lines)
        self.assertIn("run: " + RUN, lines)
        self.assertIn("suite: " + record.sha256_bytes(self.suite_bytes), lines)
        self.assertEqual(lines[-1], "hx run: pass", done.stdout)
        (run,) = self.started_jobs()
        (working,) = [a for a in run if a.startswith("--working-directory=")]
        job = working.split("=", 1)[1]
        name = posixpath.basename(job)
        self.assertTrue(name.startswith("bench-"), job)
        self.assertIn("job: " + name, lines)
        argv = [str(self.enter_path), CLOSURE, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
                "run", "--in-job", job, "--hold", str(self.hold), "--root", str(self.root),
                "--build-folder", posixpath.join(str(self.root), "builds", "latest"), "--device", DEVICE,
                "--expect", EXPECT, "--env", "HOS_EXAMPLE=1", "--scene", "scene_d", "--final"]
        self.assertEqual(run[-len(argv):], argv)
        self.assertEqual((self.build_folder / "warm" / DEVICE / "game" / "marker").read_text(encoding="utf-8"), "cold")
        self.assert_record_clean()

    def test_the_client_returns_the_jobs_fail_status_1_with_the_recorded_verdict(self):
        self.set_env({"FAKE_RENDERER": RENDERER_OTHER})
        done = self.hx_run("scene_d", "--device", DEVICE, "--final")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertTrue(done.stdout.splitlines()[-1].startswith("hx run: FAIL "), done.stdout)

    def test_an_unknown_device_exits_2_on_one_line_naming_the_devices(self):
        done = self.hx_run("scene_b", "--device", "nope")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("nope", done.stderr)
        self.assertIn(DEVICE, done.stderr)
        self.assertEqual(self.started_jobs(), [])

    def test_a_config_that_cannot_be_read_exits_2_on_one_line(self):
        self.set_env({"HX_CONFIG": str(self.base / "no-such-config.toml")})
        done = self.hx_run("scene_b", "--device", DEVICE)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(len(done.stderr.strip().splitlines()), 1, done.stderr)
        self.assertIn("cannot read it", done.stderr)

    def test_a_build_rev_that_is_no_commit_here_exits_2(self):
        done = self.hx_run("scene_b", "--device", DEVICE, "--build", "no-such-rev-zz")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("no commit", done.stderr)
        self.assertEqual(self.started_jobs(), [])


if __name__ == "__main__":
    unittest.main()
