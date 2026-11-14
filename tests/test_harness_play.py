"""hx play (scripts/harness/hx_play.py) with no GPU, game, journal or network. The client refuses an unknown device, a malformed
run id, a run that is not a capture, and an --against run that is not a play of the same capture on the same device, each
before any job starts; a valid play starts one check job with the build's game and the device's environment. In-job,
against a stand-in compiler and a stand-in player (a script the compiler copies to the linked executable: it writes the
PNGs it is given as the renderer writes them, and logs its device as the renderer does): the player is built from the
build's source and run once on the capture with the switches, the shots folder and the device environment; the run has
every fact key, the verdict, one screenshot per present and the per-present diff against live; with --against, the
spread and the listed presents; a failing build, player, device, fallback or a player with no presents writes its run,
marked FAIL, and exits 1. The PNG decoder and the diff run on synthetic images that use each of the five row filters.
The machine's GPU listing is a fixture. The journal is injected.

python -I -B tests/test_harness_play.py
"""
import contextlib
import hashlib
import io
import json
import os
import random
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

RUN_ALL_SKIP = {"windows": "the in-job half runs on the Linux host, and its stand-in player is a script run through its "
                           "#! line"}

ROOT = Path(__file__).resolve().parents[1]
HX = ROOT / "scripts" / "harness" / "hx.py"
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
import hx_capture
import hx_play
import jobs
import record
import remote

SHA = "0123456789abcdef0123456789abcdef01234567"
CLOSURE = "/nix/store/%s-hookshot-closure" % ("a" * 32)
DEVICE = "first"
EXPECT = "Example GPU One"
ICD = "/example/icd.json"
ENTER = "/example/enter-closure.sh"
CAPTURE = "/example/harness/results/captures/check-example"
HOOKSHOT = b"stand-in hookshot"
HOOKSHOT_VK = b"stand-in hookshot_vk.so"
W, H = 8, 8

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

# The live capture run (what hx capture writes) and the plays of it that the client refuses or accepts.
LIVE_DOC = {"capture": CAPTURE, "build": SHA, "device": DEVICE, "frames": 4, "presents": 4, "verdict": "pass"}
PLAY_DOC = {"kind": "play", "capture": CAPTURE, "device": DEVICE, "build": SHA, "live": "000001", "against": None,
            "presents": 4, "verdict": "pass"}

# The stand-in player: what the renderer does when a play runs. It logs its invocation and the environment it was given,
# writes each PNG of the frames folder as the renderer writes a present, logs its device as the renderer does, and exits
# with the status the test names. The #! line is added by the test, with this interpreter.
PLAYER_STAND_IN = r'''import json
import os
import shutil
import sys

KEYS = ("RX_CAPTURE", "RX_VK_SHOTS", "RX_VK_SHOT_EVERY", "RX_VK_PIPE_SKIP", "VK_ICD_FILENAMES")


def main(argv):
    seen = {key: os.environ.get(key) for key in KEYS}
    with open(os.environ["STAND_IN_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"exe": argv[0], "argv": argv[1:], "env": seen}) + "\n")
    frames, shots = os.environ["STAND_IN_FRAMES"], seen["RX_VK_SHOTS"]
    for name in sorted(os.listdir(frames)):
        shutil.copyfile(os.path.join(frames, name), os.path.join(shots, "vk_" + name))
    # as the renderer logs: each device it lists (gpu/vk/vk_main.cpp:194), then the one it made (gpu/vk/vk_main.cpp:1589)
    sys.stderr.write("vk: device 0: Example GPU Listed, Vulkan 1.3, driver 0x1, type 2\n")
    sys.stderr.write("vk: device 1: %s, Vulkan 1.3, driver 0x1, type 1\n" % os.environ["STAND_IN_DEVICE"])
    sys.stderr.write("vk: %s, Vulkan 1.3, driver 0x1; mesh shaders yes, host memory import yes, pipeline libraries no\n"
                     % os.environ["STAND_IN_DEVICE"])
    if os.environ["STAND_IN_FALLBACK"]:
        sys.stderr.write("vk: preferred device 1 not available or lacks Vulkan 1.3, falling back to auto selection\n")
    return int(os.environ["STAND_IN_EXIT"])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''

# The stand-in compiler (the test substitutes its log and the player's path): it logs each command, fails when the test
# says so, writes an object for -c and copies the stand-in player to the -o path of the link.
COMPILER_STAND_IN = r'''import json
import os
import shutil
import sys

LOG, PLAYER = @LOG@, @PLAYER@

argv = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as f:
    f.write(json.dumps(argv) + "\n")
if os.environ["STAND_IN_BUILD_FAIL"]:
    sys.stderr.write("stand-in compiler: refused\n")
    sys.exit(1)
out = argv[argv.index("-o") + 1]
if "-c" in argv:
    with open(out, "wb") as f:
        f.write(b"stand-in object\n")
else:
    shutil.copyfile(PLAYER, out)
    os.chmod(out, 0o755)
'''


def build_text(sha=SHA):
    return ("closure: %s\nsha: %s\ndirty: none\nhookshot: 0\nhookshot_vk.so: 0\nbuild: /example/builds/%s\n"
            "job: build-example\n" % (CLOSURE, sha, sha))


def facts():
    return {key: {"value": key} for key in record.FACT_KEYS}


def doc_bytes(doc):
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def frame(seed, width=W, height=H):
    """Synthetic 8-bit RGB pixels, deterministic in seed"""
    return random.Random(seed).randbytes(width * height * 3)


def changed(rgb, pixels, delta, channel=1):
    """rgb with the channel of each listed pixel moved by delta, up or down whichever stays in 0..255: the difference
    is exactly delta for any pixel values when delta is at most 127"""
    out = bytearray(rgb)
    for p in pixels:
        i = p * 3 + channel
        out[i] = out[i] + delta if out[i] + delta <= 255 else out[i] - delta
    return bytes(out)


def paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def filtered_row(kind, row, prev, bpp=3):
    """A raw scanline (row, after the previous row prev) with PNG filter type kind, the filter byte first"""
    out = bytearray([kind])
    for i, x in enumerate(row):
        a = row[i - bpp] if i >= bpp else 0
        b = prev[i]
        c = prev[i - bpp] if i >= bpp else 0
        predictor = (0, a, b, (a + b) // 2, paeth(a, b, c))[kind]
        out.append((x - predictor) & 255)
    return bytes(out)


def png(width, height, rgb, kind=None, colour=2):
    """A PNG of rgb, 8-bit: row y filtered by kind, or by y % 5 (all five filters) when kind is None"""
    stride = width * 3

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    raw, prev = bytearray(), bytes(stride)
    for y in range(height):
        row = rgb[y * stride:(y + 1) * stride]
        raw += filtered_row(y % 5 if kind is None else kind, row, prev)
        prev = row
    header = struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


class PngTests(unittest.TestCase):
    def test_each_of_the_five_row_filters_decodes_to_the_pixels_it_encodes(self):
        pixels = frame(1)
        for kind in range(5):
            with self.subTest(filter=kind):
                self.assertEqual(hx_play.decode_png(png(W, H, pixels, kind=kind)), (W, H, pixels))

    def test_rows_with_all_five_filters_in_one_image_decode_to_its_pixels(self):
        pixels = frame(2, width=5, height=7)
        self.assertEqual(hx_play.decode_png(png(5, 7, pixels)), (5, 7, pixels))

    def test_a_png_that_is_not_8_bit_rgb_is_refused(self):
        with self.assertRaises(hx_play.PlayError):
            hx_play.decode_png(png(W, H, frame(3), colour=6))

    def test_a_chunk_that_fails_its_crc_is_refused(self):
        data = bytearray(png(W, H, frame(4)))
        data[data.index(b"IDAT") + 6] ^= 0xFF
        with self.assertRaises(hx_play.PlayError):
            hx_play.decode_png(bytes(data))

    def test_a_file_that_is_not_a_png_is_refused(self):
        with self.assertRaises(hx_play.PlayError):
            hx_play.decode_png(b"not a picture")


class DiffTests(unittest.TestCase):
    def test_identical_presents_differ_in_no_pixel(self):
        data = png(W, H, frame(5))
        self.assertEqual(hx_play.diff_png(data, data), {"pixels": 0, "max_channel": 0})

    def test_one_changed_pixel_is_counted_with_its_largest_channel_difference(self):
        a, b = frame(6), changed(frame(6), [10], 5)
        self.assertEqual(hx_play.diff_png(png(W, H, a), png(W, H, b)), {"pixels": 1, "max_channel": 5})

    def test_a_pixel_changed_in_two_channels_counts_once_at_the_larger_difference(self):
        a = frame(7)
        b = changed(changed(a, [3], 3, channel=0), [3], 9, channel=2)
        self.assertEqual(hx_play.diff_png(png(W, H, a), png(W, H, b)), {"pixels": 1, "max_channel": 9})

    def test_two_changed_pixels_are_two_and_the_largest_of_their_differences_is_the_maximum(self):
        a = frame(8)
        b = changed(changed(a, [0], 100), [63], 20)
        self.assertEqual(hx_play.diff_png(png(W, H, a), png(W, H, b)), {"pixels": 2, "max_channel": 100})

    def test_presents_of_different_sizes_count_every_pixel_of_the_larger_as_differing(self):
        big, small = frame(9, width=W, height=H), frame(9, width=W, height=4)
        self.assertEqual(hx_play.diff_png(png(W, H, big), png(W, 4, small)),
                         {"pixels": W * H, "max_channel": None, "sizes": [[W, H], [W, 4]]})


class JudgeTests(unittest.TestCase):
    def test_a_present_only_one_side_has_is_marked_with_that_side(self):
        frames = {"present/000001.png": b"a"}
        self.assertEqual(hx_play.compare_presents(frames, {}, "play", "live"), {"present/000001.png": {"only": "play"}})
        self.assertEqual(hx_play.compare_presents({}, frames, "play", "live"), {"present/000001.png": {"only": "live"}})

    def test_without_a_play_to_compare_there_is_no_spread_and_nothing_is_listed(self):
        live = {"present/000001.png": png(W, H, frame(10))}
        play = {"present/000001.png": png(W, H, changed(frame(10), [1], 4))}
        judged = hx_play.judge(play, live, None)
        self.assertEqual(judged["versus_live"], {"present/000001.png": {"pixels": 1, "max_channel": 4}})
        self.assertIsNone(judged["versus_against"])
        self.assertIsNone(judged["spread"])
        self.assertIsNone(judged["listed"])


class InJobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.record = self.base / "results"
        self.hold = self.base / "hold-file"
        self.build = self.base / "build"
        source = self.build / "src" / "gpu" / "player"
        source.mkdir(parents=True)
        (source / "rx_player.cpp").write_text("// stand-in source\n", encoding="utf-8")
        (self.build / "build.txt").write_text(build_text(), encoding="utf-8")
        self.compiles = self.base / "compiles.jsonl"
        self.invocations = self.base / "invocations.jsonl"
        player = self.base / "player_stand_in.py"
        player.write_text("#!%s -I\n%s" % (sys.executable, PLAYER_STAND_IN), encoding="utf-8")
        self.compiler = self.base / "stand_in_compiler.py"
        self.compiler.write_text(COMPILER_STAND_IN.replace("@LOG@", repr(str(self.compiles)))
                                 .replace("@PLAYER@", repr(str(player))), encoding="utf-8")
        self.messages = []
        self.live_frames = [frame(n) for n in range(1, 5)]
        self.patch(mock.patch.object(hx_play, "CXX", (sys.executable, "-I", str(self.compiler))))
        self.patch(mock.patch.object(hx_capture, "command_outputs", return_value=OUTPUTS))
        self.patch(mock.patch.object(record, "write_journal", side_effect=self.messages.append))
        self.patch(mock.patch.dict(os.environ, {"STAND_IN_LOG": str(self.invocations), "STAND_IN_DEVICE": EXPECT,
                                                "STAND_IN_EXIT": "0", "STAND_IN_FALLBACK": "",
                                                "STAND_IN_BUILD_FAIL": ""}))
        self.use_frames(self.live_frames)
        self.live = self.write_run([png(W, H, f) for f in self.live_frames], LIVE_DOC)
        self.assertEqual(self.live, "000001")

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def use_frames(self, frames):
        """The stand-in player's presents: a folder of the frames' PNGs (of the RGB pixels given), named as the renderer
        names them"""
        folder = Path(tempfile.mkdtemp(dir=self.base))
        for n, pixels in enumerate(frames, 1):
            (folder / ("%06d.png" % n)).write_bytes(png(W, H, pixels))
        os.environ["STAND_IN_FRAMES"] = str(folder)

    def write_run(self, pngs, doc):
        shots = {"present/%06d.png" % (n + 1): data for n, data in enumerate(pngs)}
        run_id, _ = record.write_run(self.record, facts(), {"capture.json": doc_bytes(doc)}, shots,
                                     journal=lambda message: None)
        return run_id

    def play(self, against=None, expect=EXPECT, device=DEVICE):
        """Runs the in-job half once in a job of its own; returns its status and sets self.job and self.output"""
        job = self.base / "jobs" / jobs.new_id("check")
        (job / "game").mkdir(parents=True)
        (job / "game" / "hookshot").write_bytes(HOOKSHOT)
        (job / "game" / "hookshot_vk.so").write_bytes(HOOKSHOT_VK)
        argv = ["--in-job", str(job), "--hold", str(self.hold), "--expect", expect, "--device", device,
                "--live", self.live, "--record", str(self.record), "--build-folder", str(self.build),
                "--closure", CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % ICD]
        if against is not None:
            argv += ["--against", against]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = hx_play.main(argv)
        self.job, self.output = job, out.getvalue()
        return status

    def doc(self, run_id):
        return json.loads((self.record / "runs" / run_id / "files" / "capture.json").read_text(encoding="utf-8"))

    def manifest(self, run_id):
        return record.read_manifest(self.record, run_id)[0]

    def test_the_player_is_built_from_the_build_source_and_run_once_on_the_capture_with_the_switches_and_the_shots(self):
        self.assertEqual(self.play(), 0, self.output)
        compiles = read_jsonl(self.compiles)
        self.assertEqual(len(compiles), 2, compiles)
        source = str(self.build / "src" / "gpu" / "player" / "rx_player.cpp")
        game = self.job / "game"
        self.assertIn("-std=c++20", compiles[0])
        self.assertEqual(compiles[0][compiles[0].index("-c") + 1], source)
        self.assertEqual(compiles[0][compiles[0].index("-o") + 1], str(game / "rx_player.o"))
        self.assertIn("-ldl", compiles[1])
        self.assertEqual(compiles[1][compiles[1].index("-o") + 1], str(game / "rx_player"))
        invocations = read_jsonl(self.invocations)
        self.assertEqual(len(invocations), 1, invocations)
        self.assertEqual(invocations[0]["exe"], str(game / "rx_player"))
        self.assertEqual(invocations[0]["argv"], [CAPTURE, "--renderer", str(game / "hookshot_vk.so")])
        env = invocations[0]["env"]
        self.assertEqual(env["RX_VK_SHOTS"], str(self.job / "shots"))
        self.assertEqual(env["RX_VK_SHOT_EVERY"], "1")
        self.assertEqual(env["RX_VK_PIPE_SKIP"], "0")
        self.assertEqual(env["VK_ICD_FILENAMES"], ICD)
        self.assertIsNone(env["RX_CAPTURE"])

    def test_the_player_is_linked_exporting_the_hos_posix_symbols_as_the_game_is(self):
        # the renderer looks hos_posix_guest_memfd and hos_posix_guest_discard up with dlsym(RTLD_DEFAULT): they reach the
        # player only if its executable exports them (nx2/build.py links the game with the same flag)
        self.assertEqual(self.play(), 0, self.output)
        link = read_jsonl(self.compiles)[1]
        self.assertIn("-Wl,--export-dynamic-symbol=hos_posix_*", link)

    def test_the_run_has_every_fact_key_the_verdict_one_screenshot_per_present_and_is_journalled(self):
        self.assertEqual(self.play(), 0, self.output)
        run_id = "000002"
        manifest = self.manifest(run_id)
        self.assertEqual(sorted(manifest["facts"]), sorted(record.FACT_KEYS))
        self.assertEqual(manifest["facts"]["shas"], {"hookshot": SHA})
        self.assertEqual(self.doc(run_id)["verdict"], "pass")
        self.assertEqual(sorted(manifest["screenshots"]), ["present/%06d.png" % n for n in (1, 2, 3, 4)])
        for n, pixels in enumerate(self.live_frames, 1):
            data = png(W, H, pixels)
            entry = manifest["screenshots"]["present/%06d.png" % n]
            self.assertEqual(entry, {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
            self.assertEqual((self.record / "screenshots" / entry["sha256"]).read_bytes(), data)
        self.assertEqual(sorted(manifest["files"]), ["build.log", "capture.json", "play.log"])
        manifest_sha = hashlib.sha256((self.record / "runs" / run_id / "manifest.json").read_bytes()).hexdigest()
        self.assertEqual(self.messages, [record.journal_line(run_id, manifest_sha)])

    def test_each_present_is_diffed_against_the_live_run_present_by_present(self):
        self.assertEqual(self.play(), 0, self.output)
        doc = self.doc("000002")
        self.assertEqual(doc["live"], self.live)
        self.assertIsNone(doc["against"])
        self.assertEqual(doc["build"], SHA)
        self.assertEqual(doc["capture_build"], SHA)
        self.assertEqual(doc["versus_live"], {"present/%06d.png" % n: {"pixels": 0, "max_channel": 0}
                                              for n in (1, 2, 3, 4)})
        self.assertIsNone(doc["versus_against"])
        self.assertIsNone(doc["spread"])
        self.assertIsNone(doc["listed"])

    def test_a_play_against_another_play_of_the_capture_records_their_spread_and_the_presents_over_it(self):
        # The first play differs from live at present 2 by one pixel and at present 3 by five; the second differs from the
        # first at present 1 by two pixels (the spread) and has the same presents 2 and 3 as the first. It has three
        # presents, so present 4 is only in live. Over the spread: present 3 (five pixels) and present 4.
        first = [self.live_frames[0], changed(self.live_frames[1], [0], 1, channel=0),
                 changed(self.live_frames[2], range(5), 9), self.live_frames[3]]
        self.use_frames(first)
        self.assertEqual(self.play(), 0, self.output)
        first_run = "000002"
        second = [changed(self.live_frames[0], [0, 1], 3, channel=0), first[1], first[2]]
        self.use_frames(second)
        self.assertEqual(self.play(against=first_run), 0, self.output)
        doc = self.doc("000003")
        self.assertEqual(doc["against"], first_run)
        self.assertEqual(doc["spread"], 2)
        self.assertEqual(doc["listed"], ["present/000003.png", "present/000004.png"])
        self.assertEqual(doc["versus_against"]["present/000001.png"], {"pixels": 2, "max_channel": 3})
        self.assertEqual(doc["versus_against"]["present/000002.png"], {"pixels": 0, "max_channel": 0})
        self.assertEqual(doc["versus_live"]["present/000001.png"], {"pixels": 2, "max_channel": 3})
        self.assertEqual(doc["versus_live"]["present/000002.png"], {"pixels": 1, "max_channel": 1})
        self.assertEqual(doc["versus_live"]["present/000003.png"], {"pixels": 5, "max_channel": 9})
        self.assertEqual(doc["versus_live"]["present/000004.png"], {"only": "live"})

    def test_a_player_that_exits_nonzero_writes_its_run_marked_fail_and_exits_1(self):
        os.environ["STAND_IN_EXIT"] = "3"
        self.assertEqual(self.play(), 1, self.output)
        self.assertIn("play: FAIL", self.output)
        doc = self.doc("000002")
        self.assertEqual(doc["verdict"], "FAIL: player: exit status 3")
        self.assertEqual(doc["presents"], 4)
        self.assertEqual(len(self.manifest("000002")["screenshots"]), 4)
        self.assertIn("play.log", self.manifest("000002")["files"])

    def test_a_player_on_another_device_writes_its_run_marked_fail_and_exits_1(self):
        os.environ["STAND_IN_DEVICE"] = "Example GPU Zero"
        self.assertEqual(self.play(), 1, self.output)
        self.assertEqual(self.doc("000002")["verdict"],
                         "FAIL: the renderer is on 'Example GPU Zero', not 'Example GPU One'")

    def test_a_fallback_to_another_gpu_writes_its_run_marked_fail_and_exits_1(self):
        os.environ["STAND_IN_FALLBACK"] = "1"
        self.assertEqual(self.play(), 1, self.output)
        self.assertTrue(self.doc("000002")["verdict"].startswith("FAIL: the log shows the renderer fell back"),
                        self.doc("000002")["verdict"])

    def test_a_player_that_writes_no_presents_writes_its_run_marked_fail_and_exits_1(self):
        self.use_frames([])
        self.assertEqual(self.play(), 1, self.output)
        doc = self.doc("000002")
        self.assertEqual(doc["verdict"], "FAIL: the player wrote no presents")
        self.assertEqual(doc["presents"], 0)
        self.assertEqual(self.manifest("000002")["screenshots"], {})

    def test_a_build_that_fails_writes_its_run_marked_fail_with_no_player_run_and_exits_1(self):
        os.environ["STAND_IN_BUILD_FAIL"] = "1"
        self.assertEqual(self.play(), 1, self.output)
        doc = self.doc("000002")
        self.assertTrue(doc["verdict"].startswith("FAIL: build: "), doc["verdict"])
        self.assertIsNone(doc["presents"])
        self.assertEqual(sorted(self.manifest("000002")["files"]), ["build.log", "capture.json"])
        self.assertEqual(self.manifest("000002")["screenshots"], {})
        self.assertEqual(read_jsonl(self.invocations), [])

    def test_a_device_the_machine_does_not_list_refuses_before_the_build_and_writes_nothing(self):
        self.assertEqual(self.play(expect="Example GPU Nine"), 2, self.output)
        self.assertIn("Example GPU Nine", self.output)
        self.assertEqual(read_jsonl(self.compiles), [])
        self.assertEqual(record.run_ids(self.record), ["000001"])

    def test_an_against_run_that_is_not_a_play_is_refused_before_the_build_and_writes_nothing(self):
        self.assertEqual(self.play(against=self.live), 2, self.output)
        self.assertIn("is not a play", self.output)
        self.assertEqual(read_jsonl(self.compiles), [])
        self.assertEqual(record.run_ids(self.record), ["000001"])


class ClientTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "harness.toml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.build_stdout = build_text()
        self.docs = {
            "000001": LIVE_DOC,
            "000002": PLAY_DOC,
            "000003": {**PLAY_DOC, "capture": CAPTURE + "-other"},
            "000004": {**PLAY_DOC, "device": "second"},
        }
        self.patch(mock.patch.dict(os.environ, {"HX_CONFIG": str(self.config)}))
        self.remote = self.patch(mock.patch.object(remote, "run", side_effect=self.remote_run))
        self.start = self.patch(mock.patch.object(jobs, "start"))
        self.patch(mock.patch.object(jobs, "follow", return_value=0))

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def remote_run(self, host, script, input=None, check=True):
        if "build.txt" in script:
            return subprocess.CompletedProcess([host, script], 0, stdout=self.build_stdout, stderr="")
        found = re.search(r"runs/([0-9]{6})/files/capture\.json", script)
        if found and found.group(1) in self.docs:
            return subprocess.CompletedProcess([host, script], 0, stdout=json.dumps(self.docs[found.group(1)]),
                                               stderr="")
        raise remote.RemoteError(host, 1, "cat: no such file")

    def test_an_unknown_device_exits_2_before_any_remote_call(self):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_play.main(["000001", "--device", "nope"])
        self.assertEqual(status, 2)
        self.assertIn("no device 'nope'", err.getvalue())
        self.remote.assert_not_called()
        self.start.assert_not_called()

    def test_a_malformed_run_id_exits_2_before_any_job(self):
        for run_id in ("1", "0000001", "../000001", "00000a"):
            with self.subTest(run=run_id):
                err = io.StringIO()
                with quiet(), contextlib.redirect_stderr(err):
                    status = hx_play.main([run_id, "--device", DEVICE])
                self.assertEqual(status, 2)
                self.assertIn("not a run id", err.getvalue())
        self.remote.assert_not_called()
        self.start.assert_not_called()

    def test_hx_dispatches_play_and_refuses_an_unknown_device_with_exit_2(self):
        env = {**os.environ, "HX_CONFIG": str(self.config)}
        done = subprocess.run([sys.executable, "-I", "-B", str(HX), "play", "000001", "--device", "nope"], env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("no device 'nope'", done.stderr)

    def test_a_play_starts_one_check_job_with_the_build_game_and_the_device_environment(self):
        with quiet():
            status = hx_play.main(["000001", "--device", DEVICE])
        self.assertEqual(status, 0)
        self.start.assert_called_once()
        cfg_arg, job_id, argv = self.start.call_args.args
        self.assertTrue(job_id.startswith("check-"), job_id)
        self.assertEqual(self.start.call_args.kwargs, {"game": "/example/harness/builds/%s/game" % SHA})
        job = "/example/harness/jobs/%s" % job_id
        self.assertEqual(argv, [
            ENTER, CLOSURE, "python", "-I", job + "/tools/scripts/harness/hx.py", "play", "--in-job", job,
            "--hold", "/example/hold-file", "--expect", EXPECT, "--device", DEVICE, "--live", "000001",
            "--record", "/example/harness/results", "--build-folder", "/example/harness/builds/%s" % SHA,
            "--closure", CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % ICD,
        ])

    def test_an_against_play_of_the_same_capture_on_the_device_is_passed_to_the_job(self):
        with quiet():
            status = hx_play.main(["000001", "--device", DEVICE, "--against", "000002"])
        self.assertEqual(status, 0)
        argv = self.start.call_args.args[2]
        self.assertEqual(argv[argv.index("--live") + 1], "000001")
        self.assertEqual(argv[argv.index("--against") + 1], "000002")

    def test_the_live_run_must_be_a_capture_run(self):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_play.main(["000002", "--device", DEVICE])
        self.assertEqual(status, 2)
        self.assertIn("is not a capture run", err.getvalue())
        self.start.assert_not_called()

    def test_the_against_run_must_be_a_play(self):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_play.main(["000001", "--device", DEVICE, "--against", "000001"])
        self.assertEqual(status, 2)
        self.assertIn("is not a play", err.getvalue())
        self.start.assert_not_called()

    def test_the_against_run_must_play_the_same_capture(self):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_play.main(["000001", "--device", DEVICE, "--against", "000003"])
        self.assertEqual(status, 2)
        self.assertIn("plays another capture", err.getvalue())
        self.start.assert_not_called()

    def test_the_against_run_must_play_on_the_same_device(self):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_play.main(["000001", "--device", DEVICE, "--against", "000004"])
        self.assertEqual(status, 2)
        self.assertIn("plays on 'second'", err.getvalue())
        self.start.assert_not_called()

    def test_a_run_that_does_not_exist_exits_2_before_any_job(self):
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_play.main(["000009", "--device", DEVICE])
        self.assertEqual(status, 2)
        self.assertIn("000009", err.getvalue())
        self.start.assert_not_called()

    def test_a_build_folder_that_holds_another_commit_than_the_capture_is_refused_before_the_job(self):
        self.build_stdout = build_text(sha="f" * 40)
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_play.main(["000001", "--device", DEVICE])
        self.assertEqual(status, 2)
        self.assertIn("not the capture's %s" % SHA, err.getvalue())
        self.start.assert_not_called()

    def test_a_capture_that_names_no_build_commit_is_refused_before_any_build_is_read(self):
        self.docs["000001"] = {**LIVE_DOC, "build": "not-a-sha"}
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_play.main(["000001", "--device", DEVICE])
        self.assertEqual(status, 2)
        self.assertIn("names no build commit", err.getvalue())
        self.assertFalse(any("build.txt" in c.args[1] for c in self.remote.call_args_list))
        self.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
