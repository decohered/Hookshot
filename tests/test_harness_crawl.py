"""scripts/harness/hx_crawl.py (hx crawl): a build's game started from the crawl file's save and, for each variant (age), each
destination with a map for it visited, settled and panned once around inside one window; the cost map and one record run
are written. Checks, with a stand-in game and fake tools (no dump, GPU or host):

- the crawl file: its defaults, and an unknown key or a bad value refused naming the key (R2); last names the destinations
  visited after the others, in the order given;
- R2: each destination with a map is visited in each variant and one without a map is skipped; the age is switched ahead
  of a variant's first arrival and of the first arrival after a restart; each visit settles, then turns the stick right
  for pan_frames inside one window; an event during that window does not fail the destination, a map change or a load
  does; each entry keeps its window's trace stats with every column (F4); a failed destination is an entry with its
  error and is not retried; the game is then quit, its outputs kept as a segment, and started again from the save in the
  same job, and the crawl goes on (exit 1); a connection lost in a visit is that visit's error;
- segment ends: a segment's start waits settle_frames presents, then the game must be in play with no event or load within
  the settle limit, else the first destination is entered with that error, nothing is sent for it, and the segment ends;
  before each later visit the game must be in play with no event or load within the settle limit, else the segment ends
  with that visit not sent and the next segment makes it first; an event that ends within the limit is waited out, one
  at a start included; a connection lost at a start is that destination's error; a connection the game closes on such a
  settle ends the segment the same way (F-1); a destination named in last ends its
  segment when visits follow it; each teleport is sent once;
- segments: each segment's log, frame trace and frame log are in the record under its number, each entry names its
  segment, and its window stats come from that segment's trace; metrics.json holds each segment's summary; a restart
  that will not start, or a device check that fails after one, ends the crawl with the destinations not reached entered
  as errors; the fallback check applies to every segment;
- IR-1: each segment's kernel log is journalctl -k over its span, from its launch to the next segment's; its hangs are that
  text's lines and its text is kept under segments/<n>/kernel.txt; no machine kernel release is recorded as the kernel log;
- F2: a fallback line in the log fails the run; a renderer that is not the expected device fails it with no entries; both
  still write the record;
- R7: the cost map is written under scenes/costmaps and as the run's file, the run is one record with no home path or
  user name in it, and hx verify finds no problem; a game that cannot start exits 2 and writes no record;
- the client: an unknown device, a bad crawl file or a missing one exit 2 before a job starts; the job runs the in-job
  half with the device's variables; one run through the client writes its record.

The in-job half runs in this process with the bridge's gap between switches (teleports and age changes) set to 0 and its
settle limit set to 0 unless a test gives one; the one client run waits out the real gap.

python -I -B tests/test_harness_crawl.py
"""
import contextlib
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

RUN_ALL_SKIP = {"windows": "the fake tools are POSIX shell and Python scripts, the stand-in game is a POSIX script started "
                           "through its shebang, and its bridge a Unix socket"}

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
HX = HARNESS / "hx.py"
sys.path.insert(0, str(HARNESS))
import bridge
import hx_crawl
import hx_verify
import jobs
import metrics
import runrecord

HOST = "example-host"
HOME = "/srv/fixture-home/tester7"
USER = "tester7"
SHA = "3f9c2a1b7d4e6f80a1b2c3d4e5f60718293a4b5c"
CLOSURE = "/nix/store/q4kx1h7v3d2m9b6n8c5p0r1s2t3u4w5y-hookshot-1.0"
DEVICE = "gpu"
EXPECT = "example GPU"
RENDERER = EXPECT + " (Vulkan, native)"
PAN = 45
SETTLE = 5
CRAWL = 'save = "start"\nslot = "01"\npan_frames = 45\nsettle_frames = 5\n'
TOOLS = ("systemd-run", "systemctl", "btrfs", "sha256sum", "vulkaninfo", "nvidia-smi", "systemd-cat", "journalctl")

# The stand-in game: a Python script named hookshot's stand-in. It answers the bridge's requests as docs/control.md gives them
# for the ones the crawl makes (a teleport starts an arrival event, as the game's does: the next status shows it playing,
# and it ends one present later), logs each request and its own start (its folder's files and the save files under its data
# folder) to FAKE_GAME_LOG, and at quit writes a frame trace beside itself and a frame log. Its destinations and their maps
# by age are fixed here. FAKE_AFTER_STICK (event, loading or map) is what its status reports from a stick until the next
# teleport: an event playing, a load going on, or the game on another map. Its n-th start prints "stand-in start n" and
# unlinks a socket left by a game that ended, as the game does. Its frame trace's frame column is each row's frame number and
# its other columns are that number plus 1000 times the start before this one, so a segment's trace is told apart. Its
# frame log's frametime is 16.7 plus the same offset. FAKE_FAIL_START (a start number) exits at that start before its
# bridge answers. FAKE_RESTART_RENDERER is the renderer from the second start on. FAKE_DROP_TO (a destination) closes the
# connection on a teleport to it, with no reply. An event that FAKE_AFTER_STICK=event starts is refused a teleport ("an
# event is playing", as the game refuses it) while it plays; it ends after FAKE_EVENT_READS status reads from the stick,
# or never when that is unset. FAKE_START_EVENT (a start number) plays an event at that start: it begins with the first request
# after the start's status reads and ends as the others do, and an age switch is refused while an event plays, as the game
# refuses it. FAKE_DROP_START (a start number) closes the connection on a wait at that start. FAKE_DROP_SETTLE (a start
# number) closes the connection on the third status read after a stick at that start: after a visit's window (two status
# reads), that is the settle before the next visit.
STAND_IN_GAME = r'''
import json
import os
import socket
import sys
import threading

env = os.environ
folder = os.getcwd()
DESTINATIONS = [
    {"index": 0, "child": "Area_child_0", "adult": "Area_adult_0"},
    {"index": 1, "child": "Area_child_1", "adult": None},
    {"index": 2, "child": None, "adult": "Area_adult_2"},
]
lock = threading.Lock()
state = {"frame": 0, "age": "child", "map": "Start_area", "after_stick": "", "event_reads": 0, "start_event": False,
         "reads_after_stick": None, "arriving": False}


def note(obj):
    with open(env["FAKE_GAME_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def write_outputs():
    header = env["FAKE_TRACE_HEADER"].split(",")
    last = state["frame"]
    offset = 1000 * (number - 1)
    with open(os.path.join(folder, "hookshot_frames.csv"), "w", encoding="utf-8") as f:
        f.write(",".join(header) + "\n")
        for n in range(1, last + 1):
            f.write(",".join([str(n)] + [str(n + offset)] * (len(header) - 1)) + "\n")
    with open(env["RX_VK_FRAME_LOG"], "w", encoding="utf-8") as f:
        f.write("frame,time_s,frametime_ms\n")
        for n in range(1, last + 1):
            f.write("%d,%.4f,%.1f\n" % (n, n / 60, 16.7 + offset))


def reply(req):
    cmd = req.get("cmd")
    frame = state["frame"]
    if cmd == "status":
        if state["after_stick"] == "event" and env.get("FAKE_EVENT_READS"):
            state["event_reads"] += 1
            if state["event_reads"] > int(env["FAKE_EVENT_READS"]):
                state["after_stick"] = ""
        after = state["after_stick"]
        arrival = state["arriving"]
        if arrival:   # the arrival event a teleport starts: read once, and it ends one present later
            state["arriving"] = False
            state["frame"] += 1
        return {"ok": True, "frame": frame, "replayed": frame, "draws": 2410.5, "state": "play",
                "map": "Other_area" if after == "map" else state["map"],
                "game": {"event": after == "event" or arrival, "loading": after == "loading", "age": state["age"],
                         "frame": frame},
                "renderer": renderer}
    if cmd == "wait":
        if req.get("event") == "present":
            state["frame"] += int(req.get("frames", 1))
            return {"ok": True, "frame": state["frame"], "state": "play", "waited": int(req.get("frames", 1))}
        if req.get("event") == "frame":
            waited = max(0, int(req["frame"]) - state["frame"])
            state["frame"] += waited
            return {"ok": True, "frame": state["frame"], "state": "play", "waited": waited}
        if req.get("event") == "map":
            return {"ok": True, "frame": frame, "state": "play", "waited": 0, "map": state["map"]}
        if req.get("event") == "loading_end":
            return {"ok": True, "frame": frame, "state": "play", "waited": 0}
    if cmd == "press":
        state["frame"] += int(req.get("frames", 8))
        return {"ok": True, "frame": state["frame"], "start": frame + 1, "end": state["frame"]}
    if cmd == "stick":
        state["after_stick"] = env.get("FAKE_AFTER_STICK", "")
        state["event_reads"] = 0
        return {"ok": True, "frame": frame, "queued": 1, "frames": int(req.get("frames", 1))}
    if cmd == "teleport":
        if "to" not in req:
            return {"ok": True, "frame": frame, "destinations": DESTINATIONS}
        if state["after_stick"] == "event":
            return {"ok": False, "frame": frame, "error": "refused", "message": "an event is playing"}
        state["after_stick"] = ""
        dest = DESTINATIONS[req["to"]]
        target = dest[state["age"]]
        if target is None or str(req["to"]) == env.get("FAKE_REFUSE_TO", ""):
            return {"ok": False, "frame": frame, "error": "refused", "message": "no map for this age here"}
        state["map"] = target
        state["frame"] += 1   # the teleport takes one present, after its reply
        state["arriving"] = True
        return {"ok": True, "frame": frame, "teleport": 1, "child": dest["child"], "adult": dest["adult"],
                "sent": True, "map": target}
    if cmd == "age":
        if req["to"] == state["age"]:
            return {"ok": True, "frame": frame, "age": req["to"], "sent": False}
        if state["after_stick"] == "event":
            return {"ok": False, "frame": frame, "error": "refused", "message": "an event is playing"}
        state["age"] = req["to"]
        return {"ok": True, "frame": frame, "age": req["to"], "sent": True, "map": state["map"]}
    if cmd == "quit":
        write_outputs()
        return {"ok": True, "frame": frame, "quitting": True}
    return {"ok": False, "frame": frame, "error": "unknown_cmd", "message": str(cmd)}


def serve(conn):
    f = conn.makefile("rwb", buffering=0)
    for raw in f:
        req = json.loads(raw)
        with lock:
            note(req)
            if state["start_event"] and req.get("cmd") != "status":
                state["after_stick"], state["start_event"] = "event", False
            if req.get("cmd") == "teleport" and str(req.get("to")) == env.get("FAKE_DROP_TO", ""):
                conn.close()
                return
            if req.get("cmd") == "wait" and str(number) == env.get("FAKE_DROP_START", ""):
                conn.close()
                return
            if req.get("cmd") == "stick":
                state["reads_after_stick"] = 0
            elif req.get("cmd") == "status" and state["reads_after_stick"] is not None:
                state["reads_after_stick"] += 1
                if state["reads_after_stick"] == 3 and str(number) == env.get("FAKE_DROP_SETTLE", ""):
                    conn.close()
                    return
            out = reply(req)
            out["id"] = req.get("id")
            f.write((json.dumps(out) + "\n").encode("utf-8"))
            if req.get("cmd") == "quit":
                os._exit(int(env.get("FAKE_EXIT", "0")))


def starts_before():
    """The starts the game log already holds"""
    try:
        with open(env["FAKE_GAME_LOG"], encoding="utf-8") as f:
            return sum(1 for line in f if '"cmd": "_start"' in line)
    except FileNotFoundError:
        return 0


number = starts_before() + 1
state["start_event"] = env.get("FAKE_START_EVENT") == str(number)
renderer = env["FAKE_RENDERER"] if number == 1 else env.get("FAKE_RESTART_RENDERER", env["FAKE_RENDERER"])
data = env["XDG_DATA_HOME"]
saves = [os.path.relpath(os.path.join(d, n), data) for d, _, names in os.walk(data) for n in names]
note({"cmd": "_start", "files": sorted(os.listdir(folder)), "saves": sorted(saves)})
print("stand-in start %d" % number, flush=True)
if env.get("FAKE_PRINT"):
    print(env["FAKE_PRINT"], flush=True)
if env.get("FAKE_FAIL_START") == str(number):
    sys.exit(3)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
if os.path.exists(env["HOS_CONTROL"]):
    os.unlink(env["HOS_CONTROL"])
srv.bind(env["HOS_CONTROL"])
srv.listen(4)
while True:
    threading.Thread(target=serve, args=(srv.accept()[0],), daemon=True).start()
'''

# The fake tools, in one Python file: each shim runs it as python -I fakes.py <tool> <arguments>. Every call is recorded as a
# JSON list in $FAKE_LOG. systemd-run runs the unit's command to its end in the foreground, with its --setenv variables, its
# --working-directory and its append: outputs (or only records the call when $FAKE_NO_RUN is set). systemctl lists no unit
# and answers is-active as inactive. btrfs cannot measure. sha256sum hashes its named files. vulkaninfo names $FAKE_DEVICE.
# nvidia-smi has no driver. systemd-cat appends its input to $FAKE_JOURNAL, and journalctl prints that file.
FAKES = r'''
import hashlib
import json
import os
import subprocess
import sys


def record(tool, args):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps([tool] + args) + "\n")


def append_target(value):
    kind, _, path = value.partition(":")
    if kind != "append":
        sys.exit("fake systemd-run: only append: is faked, not %r" % value)
    return open(path, "ab")


def systemd_run(args):
    if os.environ.get("FAKE_NO_RUN"):
        return 0
    env = dict(os.environ)
    cwd = unit = None
    props = {}
    i = 0
    while i < len(args) and args[i].startswith("--"):
        key, eq, value = args[i].partition("=")
        if key in ("--user", "--collect") and not eq:
            pass
        elif eq and key == "--unit":
            unit = value
        elif eq and key == "--working-directory":
            cwd = value
        elif eq and key == "--setenv":
            name, _, text = value.partition("=")
            env[name] = text
        elif eq and key == "--property":
            name, _, text = value.partition("=")
            props[name] = text
        else:
            sys.exit("fake systemd-run: option not faked: %s" % args[i])
        i += 1
    command = args[i:]
    if unit is None or cwd is None or not command:
        sys.exit("fake systemd-run: want --unit=, --working-directory= and a command")
    with append_target(props["StandardOutput"]) as out, append_target(props["StandardError"]) as err:
        subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    return 0


def systemctl(args):
    if args[:2] == ["--user", "list-units"]:
        return 0
    if args[:2] == ["--user", "is-active"]:
        return 3
    sys.exit("fake systemctl: not faked: %s" % " ".join(args))


def btrfs(args):
    return 1


def sha256sum(args):
    status = 0
    for name in args:
        try:
            with open(name, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        except OSError as e:
            sys.stderr.write("fake sha256sum: %s: %s\n" % (name, e.strerror))
            status = 1
            continue
        print("%s  %s" % (digest, name))
    return status


def vulkaninfo(args):
    print("Devices:")
    print("========")
    print("GPU0:")
    print("\tdeviceName         = %s" % os.environ["FAKE_DEVICE"])
    return 0


def nvidia_smi(args):
    sys.stderr.write("fake nvidia-smi: no driver\n")
    return 9


def systemd_cat(args):
    with open(os.environ["FAKE_JOURNAL"], "ab") as f:
        f.write(sys.stdin.buffer.read() + b"\n")
    return 0


def journalctl(args):
    if os.path.exists(os.environ["FAKE_JOURNAL"]):
        with open(os.environ["FAKE_JOURNAL"], "rb") as f:
            sys.stdout.buffer.write(f.read())
    return 0


tools = {"systemd-run": systemd_run, "systemctl": systemctl, "btrfs": btrfs, "sha256sum": sha256sum,
         "vulkaninfo": vulkaninfo, "nvidia-smi": nvidia_smi, "systemd-cat": systemd_cat, "journalctl": journalctl}
name = sys.argv[1]
record(name, sys.argv[2:])
sys.exit(tools[name](sys.argv[2:]))
'''

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


def game_shim(stand_in):
    """A build's hookshot: a shell script that runs the stand-in game (the launch runs it as an executable)"""
    return "#!/bin/sh\nexec %s -I %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(str(stand_in)))


def tool_shim(fakes, tool):
    return "#!/bin/sh\nexec %s -I %s %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(str(fakes)), tool)


class CrawlFileTests(unittest.TestCase):
    def parse(self, text):
        return hx_crawl.parse_crawl(text, "crawl.toml")

    def test_r2_the_crawl_file_reads_its_values_and_defaults_to_both_variants_and_no_slot(self):
        crawl = self.parse('save = "start"\npan_frames = 45\nsettle_frames = 0\n')
        self.assertEqual(crawl, hx_crawl.Crawl(save="start", slot=None, pan_frames=45, settle_frames=0,
                                               variants=("child", "adult")))

    def test_r2_a_slot_and_one_variant_are_read(self):
        crawl = self.parse('save = "start"\nslot = "03"\npan_frames = 1\nsettle_frames = 2\nvariants = ["adult"]\n')
        self.assertEqual((crawl.slot, crawl.pan_frames, crawl.settle_frames, crawl.variants), ("03", 1, 2, ("adult",)))

    def test_a_bad_or_unknown_key_is_refused_naming_the_key(self):
        base = 'save = "start"\npan_frames = 45\nsettle_frames = 5\n'
        cases = [
            ("bogus", base + "bogus = 1\n"),
            ("save", "pan_frames = 45\nsettle_frames = 5\n"),
            ("save", base.replace('"start"', '"../start"')),
            ("slot", base + 'slot = "1"\n'),
            ("pan_frames", base.replace("45", "0")),
            ("pan_frames", base.replace("45", "true")),
            ("pan_frames", base.replace("45", '"45"')),
            ("pan_frames", 'save = "start"\nsettle_frames = 5\n'),
            ("settle_frames", base.replace("= 5", "= -1")),
            ("variants", base + "variants = []\n"),
            ("variants", base + 'variants = ["child", "child"]\n'),
            ("variants", base + 'variants = ["baby"]\n'),
            ("last", base + "last = [-1]\n"),
            ("last", base + "last = [1, 1]\n"),
            ("last", base + 'last = ["1"]\n'),
            ("last", base + "last = 1\n"),
            ("last", base + "last = [true]\n"),
            ("TOML", "save = \n"),
        ]
        for key, text in cases:
            with self.subTest(key=key, text=text):
                with self.assertRaises(hx_crawl.CrawlError) as caught:
                    self.parse(text)
                self.assertIn(key, str(caught.exception))

    def test_r2_last_names_the_destinations_read_in_the_order_given(self):
        crawl = self.parse(CRAWL + "last = [2, 0]\n")
        self.assertEqual(crawl.last, (2, 0))

    def test_last_defaults_to_no_destinations(self):
        self.assertEqual(self.parse(CRAWL).last, ())


class Fixture(unittest.TestCase):
    """A harness root with a build (its game is the stand-in), a save and a crawl file, and fake tools on PATH"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "root"
        self.hold = self.tmp / "hold"
        self.game_log = self.tmp / "game.jsonl"
        self.calls_file = self.tmp / "calls.jsonl"
        self.journal = self.tmp / "journal.txt"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fakes = self.tmp / "fakes.py"
        fakes.write_text(FAKES, encoding="utf-8")
        for tool in TOOLS:
            self.write_exec(bin_dir / tool, tool_shim(fakes, tool))
        self.write_exec(bin_dir / "ssh", FAKE_SSH)
        self.write_exec(bin_dir / "python", "#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(sys.executable))
        self.stand_in = self.tmp / "stand_in.py"
        self.stand_in.write_text(STAND_IN_GAME, encoding="utf-8")
        self.enter = self.tmp / "enter-closure"
        self.write_exec(self.enter, '#!/bin/sh\nshift\nexec "$@"\n')
        conf = self.tmp / "harness.toml"
        conf.write_text(TOML % (HOST, self.hold, self.root, self.tmp / "dump", self.enter), encoding="utf-8")
        self.build_folder = self.root / "builds" / SHA
        self.write_game(self.build_folder / "game")
        (self.build_folder / "build.txt").write_text("sha: %s\nclosure: %s\n" % (SHA, CLOSURE), encoding="utf-8")
        os.symlink(SHA, self.root / "builds" / "latest")
        self.write_save()
        self.write_crawl(CRAWL)
        env = mock.patch.dict(os.environ, {
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "HOME": HOME, "USER": USER, "LOGNAME": USER,
            "HX_CONFIG": str(conf),
            "FAKE_LOG": str(self.calls_file), "FAKE_GAME_LOG": str(self.game_log), "FAKE_JOURNAL": str(self.journal),
            "FAKE_DEVICE": EXPECT, "FAKE_RENDERER": RENDERER,
            "FAKE_TRACE_HEADER": ",".join(metrics.TRACE_COLUMNS),
        })
        env.start()
        self.addCleanup(env.stop)
        for var in ("FAKE_REFUSE_TO", "FAKE_AFTER_STICK", "FAKE_EVENT_READS", "FAKE_START_EVENT", "FAKE_PRINT", "FAKE_EXIT",
                    "FAKE_NO_RUN", "FAKE_RESTART_RENDERER", "FAKE_FAIL_START", "FAKE_DROP_TO", "FAKE_DROP_START",
                    "FAKE_DROP_SETTLE"):
            os.environ.pop(var, None)

    def write_exec(self, path, text):
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def write_game(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        self.write_exec(folder / "hookshot", game_shim(self.stand_in))
        (folder / "hookshot_vk.so").write_bytes(b"library\n")

    def write_save(self):
        save = self.root / "scenes" / "saves" / "start"
        (save / "slot_01").mkdir(parents=True)
        (save / "account.sav").write_bytes(b"account\n")
        (save / "option.sav").write_bytes(b"option\n")
        (save / "slot_01" / "progress.sav").write_bytes(b"progress\n")

    def write_crawl(self, text):
        path = self.root / "scenes" / "crawl.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def calls(self, tool):
        """The argument lists of the fake tool's calls, in order"""
        if not self.calls_file.exists():
            return []
        logged = [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()]
        return [entry[1:] for entry in logged if entry[0] == tool]

    def logged(self):
        if not self.game_log.exists():
            return []
        return [json.loads(line) for line in self.game_log.read_text(encoding="utf-8").splitlines()]

    def requests(self, cmd=None):
        """The requests the stand-in game received, in order (its start excluded), or those of one command"""
        return [r for r in self.logged() if r["cmd"] != "_start" and (cmd is None or r["cmd"] == cmd)]

    def starts(self):
        return [r for r in self.logged() if r["cmd"] == "_start"]

    def segment_requests(self, number):
        """The requests the stand-in game received in its number-th start, from that start to the next one"""
        logged = self.logged()
        marks = [i for i, r in enumerate(logged) if r["cmd"] == "_start"]
        end = marks[number] if number < len(marks) else len(logged)
        return [r for r in logged[marks[number - 1]:end] if r["cmd"] != "_start"]

    def costmap_path(self, job_id):
        return self.root / "scenes" / "costmaps" / (job_id + ".json")

    def costmap(self, job_id):
        return json.loads(self.costmap_path(job_id).read_text(encoding="utf-8"))

    def run_dir(self):
        return self.root / "results" / "runs" / "000001"


class CrawlRunTests(Fixture):
    """The in-job half, run in this process as the job runs it: the gap between switches is 0 (bridge.SWITCH_GAP), and the
    settle limit is 0 unless a test gives one"""

    def crawl(self, settle=0):
        """Runs the in-job half against a new job folder; returns (its status, its output, the job id)"""
        job_id = jobs.new_id("bench")
        job = self.root / "jobs" / job_id
        (job / "data").mkdir(parents=True)
        argv = ["--in-job", str(job), "--hold", str(self.hold), "--root", str(self.root),
                "--build-folder", str(self.build_folder), "--device", DEVICE, "--expect", EXPECT,
                "--env", "HOS_EXAMPLE=1"]
        out = io.StringIO()
        with mock.patch.object(bridge, "SWITCH_GAP", 0), mock.patch.object(bridge, "SETTLE_SECONDS", settle), \
                contextlib.redirect_stdout(out):
            status = hx_crawl.main(argv)
        return status, out.getvalue(), job_id

    def test_r2_each_destination_with_a_map_is_visited_in_each_variant_and_one_without_is_skipped(self):
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["variant"], e["destination"], e["map"]) for e in entries], [
            ("child", 0, "Area_child_0"), ("child", 1, "Area_child_1"),
            ("adult", 0, "Area_adult_0"), ("adult", 2, "Area_adult_2")])
        self.assertEqual([r["to"] for r in self.requests("teleport") if "to" in r], [0, 1, 0, 2])

    def test_r2_each_visit_settles_then_turns_the_stick_right_inside_one_window(self):
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        steps = [(r["cmd"], r.get("event"), r.get("frames")) for r in self.requests()
                 if r["cmd"] == "stick" or (r["cmd"] == "wait" and r.get("event") in ("present", "frame"))]
        self.assertEqual(steps, [("wait", "present", SETTLE)]
                         + [("wait", "present", SETTLE), ("stick", None, PAN), ("wait", "frame", None)] * 4)
        self.assertTrue(all(isinstance(r["frame"], int) for r in self.requests("wait") if r.get("event") == "frame"))
        for r in self.requests("stick"):
            self.assertEqual((r["stick"], r["x"], r["y"], r["wait"]), ("R", 1, 0, False))
        for r in self.requests("wait"):
            if r.get("event") == "present" and r["frames"] == SETTLE:
                self.assertIn("timeout", r)

    def test_r2_the_age_is_switched_once_per_variant_ahead_of_its_first_destination(self):
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        moves = [(r["cmd"], r["to"]) for r in self.requests() if r["cmd"] in ("age", "teleport") and "to" in r]
        self.assertEqual(moves, [("age", "child"), ("teleport", 0), ("teleport", 1),
                                 ("age", "adult"), ("teleport", 0), ("teleport", 2)])

    def test_the_age_goes_with_a_variants_first_arrival_and_with_the_first_arrival_after_a_restart(self):
        os.environ["FAKE_REFUSE_TO"] = "0"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        moves = [(r["cmd"], r["to"]) for r in self.requests() if r["cmd"] in ("age", "teleport") and "to" in r]
        self.assertEqual(moves, [("age", "child"), ("teleport", 0),
                                 ("age", "child"), ("teleport", 1),
                                 ("age", "adult"), ("teleport", 0),
                                 ("age", "adult"), ("teleport", 2)])

    def test_r2_each_entry_keeps_its_window_trace_stats_with_every_column(self):
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual(len(entries), 4)
        for entry in entries:
            with self.subTest(variant=entry["variant"], destination=entry["destination"]):
                first, last = entry["first_replayed"], entry["last_replayed"]
                self.assertEqual(last - first, PAN)
                self.assertEqual(entry["draws"], 2410.5)
                self.assertEqual(set(entry["trace"]), set(metrics.TRACE_COLUMNS[1:]))
                self.assertEqual(entry["trace"]["ms"]["n"], PAN + 1)
                self.assertEqual(entry["trace"]["ms"]["max"], last)
                self.assertEqual(entry["trace"]["ms"]["mean"], (first + last) / 2)

    def test_a_refused_teleport_is_an_entry_with_its_error_and_the_crawl_goes_on(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        self.assertIn("destination 1 (child)", out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([("error" in e) for e in entries], [False, True, False, False])
        refused = entries[1]
        self.assertIn("refused", refused["error"])
        self.assertNotIn("map", refused)
        self.assertIn("gap", refused["trace"])
        self.assertIn("map", entries[3])
        self.assertEqual(len([r for r in self.requests("teleport") if r.get("to") == 1]), 1)

    def test_r2_last_visits_the_named_destinations_after_the_others_in_the_order_given(self):
        self.write_crawl(CRAWL + "last = [2, 0]\n")
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["variant"], e["destination"]) for e in entries],
                         [("child", 1), ("child", 0), ("adult", 2), ("adult", 0)])
        self.assertEqual([r["to"] for r in self.requests("teleport") if "to" in r], [1, 0, 2, 0])

    def test_a_destination_named_last_followed_by_visits_ends_its_segment_and_the_next_variant_starts_a_segment(self):
        self.write_crawl(CRAWL + "last = [0]\n")
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        self.assertEqual(len(self.starts()), 2)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["segment"], e["variant"], e["destination"]) for e in entries],
                         [(1, "child", 1), (1, "child", 0), (2, "adult", 2), (2, "adult", 0)])
        self.assertFalse(any("error" in e for e in entries))
        second = [i for i, r in enumerate(self.logged()) if r["cmd"] == "_start"][1]
        later = [(r["cmd"], r["to"]) for r in self.logged()[second:] if r["cmd"] in ("age", "teleport") and "to" in r]
        self.assertEqual(later, [("age", "adult"), ("teleport", 2), ("teleport", 0)])

    def test_an_event_during_a_window_does_not_fail_the_destination(self):
        os.environ["FAKE_AFTER_STICK"] = "event"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual(len(entries), 4)
        self.assertFalse(any("error" in e for e in entries))
        for entry in entries:
            with self.subTest(variant=entry["variant"], destination=entry["destination"]):
                self.assertEqual(entry["last_replayed"] - entry["first_replayed"], PAN)

    def test_an_event_that_ends_before_the_settle_limit_holds_the_next_teleport_until_it_ends(self):
        os.environ["FAKE_AFTER_STICK"] = "event"
        os.environ["FAKE_EVENT_READS"] = "3"
        status, out, job_id = self.crawl(settle=10)
        self.assertEqual(status, 0, out)
        self.assertEqual(len(self.starts()), 1)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["segment"], e["destination"], e["variant"]) for e in entries],
                         [(1, 0, "child"), (1, 1, "child"), (1, 0, "adult"), (1, 2, "adult")])
        self.assertFalse(any("error" in e for e in entries))
        self.assertEqual([r["to"] for r in self.requests("teleport") if "to" in r], [0, 1, 0, 2])

    def test_an_event_that_does_not_end_ends_the_segment_and_the_next_one_visits_the_pending_destination_first(self):
        os.environ["FAKE_AFTER_STICK"] = "event"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        self.assertEqual(len(self.starts()), 4)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["segment"], e["destination"], e["variant"]) for e in entries],
                         [(1, 0, "child"), (2, 1, "child"), (3, 0, "adult"), (4, 2, "adult")])
        self.assertFalse(any("error" in e for e in entries))
        self.assertEqual([r["to"] for r in self.requests("teleport") if "to" in r], [0, 1, 0, 2])

    def test_an_event_just_after_the_start_is_waited_out_before_the_first_age_switch(self):
        self.write_crawl(CRAWL + 'variants = ["adult"]\n')
        os.environ["FAKE_START_EVENT"] = "1"
        os.environ["FAKE_EVENT_READS"] = "3"
        status, out, job_id = self.crawl(settle=10)
        self.assertEqual(status, 0, out)
        self.assertEqual(len(self.starts()), 1)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["destination"], e["variant"]) for e in entries], [(0, "adult"), (2, "adult")])
        self.assertFalse(any("error" in e for e in entries))
        self.assertEqual([(r["cmd"], r["to"]) for r in self.requests() if r["cmd"] in ("age", "teleport") and "to" in r],
                         [("age", "adult"), ("teleport", 0), ("teleport", 2)])

    def test_an_event_at_the_start_that_does_not_end_fails_the_first_visit_and_the_next_segment_goes_on(self):
        os.environ["FAKE_START_EVENT"] = "1"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        self.assertEqual(len(self.starts()), 2)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["segment"], e["destination"], e["variant"], "error" in e) for e in entries],
                         [(1, 0, "child", True), (2, 1, "child", False), (2, 0, "adult", False), (2, 2, "adult", False)])
        self.assertIn("not in play with no event or load", entries[0]["error"])
        self.assertNotIn("map", entries[0])
        first_segment = [(r["cmd"], r["to"]) for r in self.segment_requests(1)
                         if r["cmd"] in ("age", "teleport") and "to" in r]
        self.assertEqual(first_segment, [])
        self.assertEqual([r["to"] for r in self.requests("teleport") if "to" in r], [1, 0, 2])

    def test_a_connection_lost_at_a_segments_start_is_the_first_visits_error_and_the_crawl_goes_on(self):
        os.environ["FAKE_DROP_START"] = "1"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["segment"], e["destination"], e["variant"], "error" in e) for e in entries],
                         [(1, 0, "child", True), (2, 1, "child", False), (2, 0, "adult", False), (2, 2, "adult", False)])
        self.assertIn("the game closed the connection", entries[0]["error"])
        self.assertEqual([r["to"] for r in self.requests("teleport") if "to" in r], [1, 0, 2])

    def test_f1_a_connection_closed_on_the_settle_before_a_later_visit_ends_the_segment_and_the_next_makes_that_visit(self):
        os.environ["FAKE_DROP_SETTLE"] = "1"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        self.assertIn("destinations: 4 measured, 0 failed", out.splitlines())
        self.assertEqual(len(self.starts()), 2)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["segment"], e["variant"], e["destination"]) for e in entries],
                         [(1, "child", 0), (2, "child", 1), (2, "adult", 0), (2, "adult", 2)])
        self.assertFalse(any("error" in e for e in entries))
        self.assertEqual([r["to"] for r in self.requests("teleport") if "to" in r], [0, 1, 0, 2])
        second = [(r["cmd"], r["to"]) for r in self.segment_requests(2) if r["cmd"] in ("age", "teleport") and "to" in r]
        self.assertEqual(second[:2], [("age", "child"), ("teleport", 1)])

    def test_a_segment_start_waits_settle_frames_presents_before_its_first_visit(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        for number in (1, 2):
            with self.subTest(segment=number):
                steps = self.segment_requests(number)
                first_move = next(i for i, r in enumerate(steps) if r["cmd"] in ("age", "teleport") and "to" in r)
                presents = [(r["cmd"], r.get("frames")) for r in steps[:first_move] if r.get("event") == "present"]
                self.assertIn(("wait", SETTLE), presents)

    def test_a_map_change_during_a_window_fails_the_destination(self):
        os.environ["FAKE_AFTER_STICK"] = "map"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual(len(entries), 4)
        self.assertTrue(all("error" in e for e in entries))
        self.assertIn("the window is not steady", entries[0]["error"])
        self.assertIn("Other_area", entries[0]["error"])

    def test_a_load_during_a_window_fails_the_destination(self):
        os.environ["FAKE_AFTER_STICK"] = "loading"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual(len(entries), 4)
        self.assertTrue(all("error" in e for e in entries))
        self.assertIn("the window is not steady", entries[0]["error"])
        self.assertIn("loading True", entries[0]["error"])

    def test_a_fallback_line_in_the_log_fails_the_run_and_keeps_every_entry(self):
        os.environ["FAKE_PRINT"] = "vk: preferred device 0 not available, falling back to auto selection"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        self.assertIn("fell back to another GPU", out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual(len(entries), 4)
        self.assertFalse(any("error" in e for e in entries))
        self.assertTrue((self.run_dir() / "manifest.json").is_file())

    def test_a_renderer_that_is_not_the_expected_device_fails_with_no_entries_and_still_writes_the_record(self):
        os.environ["FAKE_RENDERER"] = "other GPU (Vulkan, native)"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        self.assertIn("other GPU (Vulkan, native)", out)
        self.assertIn(RENDERER, out)
        self.assertEqual(self.costmap(job_id)["entries"], [])
        self.assertEqual(self.requests("teleport"), [])
        self.assertTrue((self.run_dir() / "manifest.json").is_file())

    def test_r7_the_cost_map_and_one_run_record_are_written_and_hx_verify_finds_no_problem(self):
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        cost = self.costmap(job_id)
        self.assertEqual(set(cost), {"build_sha", "device", "cache", "pan_frames", "settle_frames", "entries"})
        self.assertEqual((cost["build_sha"], cost["device"], cost["cache"], cost["pan_frames"], cost["settle_frames"]),
                         (SHA, DEVICE, "cold", PAN, SETTLE))
        self.assertEqual(sorted(p.name for p in (self.root / "results" / "runs").iterdir()), ["000001"])
        lines = out.splitlines()
        self.assertIn("run: 000001", lines)
        manifest = self.run_dir() / "manifest.json"
        self.assertIn("manifest: " + hashlib.sha256(manifest.read_bytes()).hexdigest(), lines)
        self.assertEqual((self.run_dir() / "files" / "costmap.json").read_bytes(),
                         self.costmap_path(job_id).read_bytes())
        names = set(json.loads(manifest.read_text(encoding="utf-8"))["files"])
        self.assertLessEqual({"costmap.json", "metrics.json", "crawl.toml", "segments/1/log.txt",
                              "segments/1/hookshot_frames.csv", "segments/1/frametimes.csv"}, names)
        self.assertEqual(sorted(p.name for p in (self.run_dir() / "files" / "segments").iterdir()), ["1"])
        verify = io.StringIO()
        with contextlib.redirect_stdout(verify):
            code = hx_verify.main(["--root", str(self.root / "results")])
        self.assertEqual((code, verify.getvalue()), (0, "1 run checked, no problems\n"))

    def test_r7_no_fact_or_file_name_in_the_record_has_the_home_path_or_user_name(self):
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        text = (self.run_dir() / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn(HOME, text)
        self.assertNotIn(USER, text)

    def test_a_warm_game_folder_for_the_device_is_the_one_copied_and_recorded_as_warm(self):
        warm = self.build_folder / "warm" / DEVICE / "game"
        self.write_game(warm)
        (warm / "warm_marker").write_text("warm\n", encoding="utf-8")
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        self.assertEqual(self.costmap(job_id)["cache"], "warm")
        self.assertIn("warm_marker", self.starts()[0]["files"])

    def test_without_a_warm_folder_the_build_game_is_copied_and_recorded_as_cold(self):
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        self.assertEqual(self.costmap(job_id)["cache"], "cold")
        self.assertEqual(self.starts()[0]["files"], ["hookshot", "hookshot_vk.so"])

    def test_the_save_folder_is_copied_into_the_jobs_data_folder_for_the_game(self):
        status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        names = {p.rsplit("/", 1)[-1] for p in self.starts()[0]["saves"]}
        self.assertEqual(names, {"account.sav", "option.sav", "progress.sav"})

    def test_a_game_that_cannot_start_exits_2_and_writes_no_record(self):
        (self.build_folder / "game" / "hookshot").unlink()
        status, out, job_id = self.crawl()
        self.assertEqual(status, 2, out)
        self.assertIn("crawl: could not run:", out)
        self.assertIn("hookshot", out)
        self.assertFalse((self.root / "results").exists())
        self.assertFalse(self.costmap_path(job_id).exists())

    def test_a_bad_last_exits_2_naming_the_key_and_starts_no_game(self):
        self.write_crawl(CRAWL + "last = [1, 1]\n")
        status, out, job_id = self.crawl()
        self.assertEqual(status, 2, out)
        self.assertIn("last must be", out)
        self.assertEqual(self.logged(), [])

    def test_a_save_folder_that_is_missing_exits_2_naming_it(self):
        self.write_crawl(CRAWL.replace('"start"', '"missing"'))
        status, out, job_id = self.crawl()
        self.assertEqual(status, 2, out)
        self.assertIn("missing", out)
        self.assertFalse((self.root / "results").exists())

    def test_a_refused_teleport_ends_its_segment_and_the_game_starts_again_from_the_save(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        starts = self.starts()
        self.assertEqual(len(starts), 2)
        self.assertEqual({p.rsplit("/", 1)[-1] for p in starts[1]["saves"]},
                         {"account.sav", "option.sav", "progress.sav"})
        logged = self.logged()
        second = [i for i, r in enumerate(logged) if r["cmd"] == "_start"][1]
        later = [(r["cmd"], r["to"]) for r in logged[second:] if r["cmd"] in ("age", "teleport") and "to" in r]
        self.assertEqual(later, [("age", "adult"), ("teleport", 0), ("teleport", 2)])
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["segment"], e["destination"], e["variant"], "error" in e) for e in entries],
                         [(1, 0, "child", False), (1, 1, "child", True), (2, 0, "adult", False), (2, 2, "adult", False)])
        self.assertEqual(len([r for r in self.requests("teleport") if r.get("to") == 1]), 1)

    def test_each_segment_keeps_its_log_trace_and_frame_log_in_the_record(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        names = set(json.loads((self.run_dir() / "manifest.json").read_text(encoding="utf-8"))["files"])
        self.assertLessEqual({"segments/%d/%s" % (n, name) for n in (1, 2)
                              for name in ("log.txt", "hookshot_frames.csv", "frametimes.csv")}, names)
        seg = self.run_dir() / "files" / "segments"
        first_log = (seg / "1" / "log.txt").read_bytes()
        self.assertIn(b"stand-in start 1\n", first_log)
        self.assertNotIn(b"stand-in start 2", first_log)
        self.assertIn(b"stand-in start 2\n", (seg / "2" / "log.txt").read_bytes())

        def first_ms(path):
            return path.read_text(encoding="utf-8").splitlines()[1].split(",")[1]

        self.assertEqual((first_ms(seg / "1" / "hookshot_frames.csv"), first_ms(seg / "2" / "hookshot_frames.csv")),
                         ("1", "1001"))
        self.assertTrue((seg / "1" / "frametimes.csv").read_text(encoding="utf-8").splitlines()[1].endswith(",16.7"))
        self.assertTrue((seg / "2" / "frametimes.csv").read_text(encoding="utf-8").splitlines()[1].endswith(",1016.7"))
        verify = io.StringIO()
        with contextlib.redirect_stdout(verify):
            code = hx_verify.main(["--root", str(self.root / "results")])
        self.assertEqual((code, verify.getvalue()), (0, "1 run checked, no problems\n"))

    def test_each_entry_names_its_segment_and_its_window_stats_come_from_that_segments_trace(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([e["segment"] for e in entries], [1, 1, 2, 2])
        for entry in entries:
            if "error" in entry:
                continue
            offset = 1000 * (entry["segment"] - 1)
            with self.subTest(segment=entry["segment"], destination=entry["destination"]):
                self.assertEqual(entry["trace"]["ms"]["max"], entry["last_replayed"] + offset)
                self.assertEqual(entry["trace"]["ms"]["mean"],
                                 (entry["first_replayed"] + entry["last_replayed"]) / 2 + offset)

    def test_metrics_json_holds_each_segments_summary_with_its_exit_status(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        doc = json.loads((self.run_dir() / "files" / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual([s["segment"] for s in doc["segments"]], [1, 2])
        for segment in doc["segments"]:
            with self.subTest(segment=segment["segment"]):
                self.assertEqual(segment["summary"]["hangs"]["exit_status"], 0)

    def test_ir1_each_segments_hangs_are_the_kernel_log_lines_and_its_text_is_kept_in_its_segment(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        xid = "[  913.0] NVRM: Xid (PCI:0000:01:00): 31, pid=4321, Ch 00000008"
        self.journal.write_text(xid + "\n", encoding="utf-8")
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        doc = json.loads((self.run_dir() / "files" / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual([s["summary"]["hangs"]["kernel"] for s in doc["segments"]], [[xid], [xid]])
        seg = self.run_dir() / "files" / "segments"
        for number in (1, 2):
            with self.subTest(segment=number):
                self.assertEqual((seg / str(number) / "kernel.txt").read_text(encoding="utf-8"), xid + "\n")

    def test_ir1_no_record_file_holds_the_machines_kernel_release_as_the_kernel_log(self):
        release = "Linux version 9.9.9-fixture\n"
        real = runrecord.machine_outputs

        def with_release(run=subprocess.run):
            return {**real(run), "kernel": release}

        with mock.patch.object(runrecord, "machine_outputs", with_release):
            status, out, job_id = self.crawl()
        self.assertEqual(status, 0, out)
        manifest = json.loads((self.run_dir() / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("kernel.txt", manifest["files"])
        for path in (self.run_dir() / "files").rglob("*"):
            if path.is_file():
                with self.subTest(file=str(path.relative_to(self.run_dir()))):
                    self.assertNotIn(release, path.read_text(encoding="utf-8"))

    def test_ir1_each_segments_kernel_span_runs_from_its_launch_to_the_next_launch(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        before = int(time.time())
        status, out, job_id = self.crawl()
        after = int(time.time())
        self.assertEqual(status, 1, out)
        calls = self.calls("journalctl")
        self.assertEqual(len(calls), 2, calls)
        for args in calls:
            self.assertIn("-k", args)
        since = [args[args.index("--since") + 1] for args in calls]
        until = [args[args.index("--until") + 1] if "--until" in args else None for args in calls]
        launches = [int(value[1:]) for value in since]
        self.assertTrue(all(value.startswith("@") for value in since), since)
        self.assertTrue(before <= launches[0] <= launches[1] <= after, (before, launches, after))
        self.assertEqual(until, [since[1], None])

    def test_a_restart_that_will_not_start_ends_the_crawl_and_the_destinations_not_reached_get_entries_saying_so(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        os.environ["FAKE_FAIL_START"] = "2"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        self.assertIn("would not start again", out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["segment"], e["destination"], e["variant"]) for e in entries],
                         [(1, 0, "child"), (1, 1, "child"), (2, 0, "adult"), (2, 2, "adult")])
        for entry in entries[2:]:
            with self.subTest(destination=entry["destination"]):
                self.assertIn("not reached", entry["error"])
                self.assertNotIn("map", entry)
        self.assertEqual([r["to"] for r in self.requests("teleport") if "to" in r], [0, 1])
        self.assertEqual(len(self.starts()), 2)
        segments = self.run_dir() / "files" / "segments"
        self.assertEqual(sorted(p.name for p in segments.iterdir()), ["1"])

    def test_a_device_that_is_not_the_expected_one_after_a_restart_ends_the_crawl(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        os.environ["FAKE_RESTART_RENDERER"] = "other GPU (Vulkan, native)"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        self.assertIn("other GPU (Vulkan, native)", out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["segment"], e["destination"], "error" in e) for e in entries],
                         [(1, 0, False), (1, 1, True), (2, 0, True), (2, 2, True)])
        self.assertIn("not reached", entries[2]["error"])
        self.assertEqual([r["to"] for r in self.requests("teleport") if "to" in r], [0, 1])

    def test_the_fallback_check_applies_to_every_segment(self):
        os.environ["FAKE_REFUSE_TO"] = "1"
        os.environ["FAKE_PRINT"] = "vk: preferred device 0 not available, falling back to auto selection"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        self.assertIn("segment 1: the log shows the renderer fell back to another GPU", out)
        self.assertIn("segment 2: the log shows the renderer fell back to another GPU", out)

    def test_a_connection_lost_during_a_visit_is_that_visits_error_and_the_crawl_goes_on(self):
        os.environ["FAKE_DROP_TO"] = "1"
        status, out, job_id = self.crawl()
        self.assertEqual(status, 1, out)
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["segment"], e["destination"], "error" in e) for e in entries],
                         [(1, 0, False), (1, 1, True), (2, 0, False), (2, 2, False)])
        self.assertIn("the game closed the connection", entries[1]["error"])
        self.assertEqual([r["to"] for r in self.requests("teleport") if "to" in r], [0, 1, 0, 2])


class ClientTests(Fixture):
    """hx crawl through hx.py: the refusals before a job starts, the job's argv, and one run through the job"""

    def hx(self, *args):
        return subprocess.run([sys.executable, "-I", "-B", str(HX), "crawl", *args], capture_output=True, text=True,
                              timeout=300)

    def test_an_unknown_device_exits_2_naming_the_devices_and_starts_no_job(self):
        done = self.hx("--device", "nope")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("nope", done.stderr)
        self.assertIn("gpu", done.stderr)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_the_job_runs_the_in_job_half_with_the_device_variables(self):
        os.environ["FAKE_NO_RUN"] = "1"
        done = self.hx("--device", DEVICE)
        self.assertIn("build: " + SHA, done.stdout.splitlines())
        job_id = [line.split(": ", 1)[1] for line in done.stdout.splitlines() if line.startswith("job: ")][0]
        (run,) = self.calls("systemd-run")
        job = str(self.root / "jobs" / job_id)
        self.assertIn("--working-directory=" + job, run)
        argv = [str(self.enter), CLOSURE, "python", "-I", str(Path(job) / "tools" / "scripts" / "harness" / "hx.py"),
                "crawl", "--in-job", job, "--hold", str(self.hold), "--root", str(self.root),
                "--build-folder", str(self.root / "builds" / "latest"), "--device", DEVICE, "--expect", EXPECT,
                "--env", "HOS_EXAMPLE=1"]
        self.assertEqual(run[-len(argv):], argv)
        self.assertIn("job ended without a status", done.stdout)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

    def test_r2_a_crawl_through_the_client_writes_its_cost_map_and_its_record(self):
        # (the client run waits out the bridge's real gap between the two teleports: about 16 s)
        self.write_crawl(CRAWL + 'variants = ["child"]\n')
        done = self.hx("--device", DEVICE)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = done.stdout.splitlines()
        self.assertIn("run: 000001", lines)
        job_id = [line.split(": ", 1)[1] for line in lines if line.startswith("job: ")][0]
        entries = self.costmap(job_id)["entries"]
        self.assertEqual([(e["variant"], e["destination"]) for e in entries], [("child", 0), ("child", 1)])


if __name__ == "__main__":
    unittest.main()
