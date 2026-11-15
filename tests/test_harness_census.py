"""The renderer census (gpu/vk/vk_census.h), without a game or a GPU.

tests/census_test.cpp drives every interface function with stub calls and checks each key's value in the windows it
emits, the line grammar, and that no fed identifier reaches the output. This file builds it with the runner's compiler
and runs it with RX_VK_CENSUS unset, 1 and 0, which must give play_census::on 0, 1 and 0. It also applies the source
rule: in every gpu/vk/*.cpp, each play_census::<name>( call sits under `if (play_census::on)`, on the same line or in a
block that a line ending `if (play_census::on) {` opens. The rule is checked on the tree and on synthetic samples
that must be rejected and accepted.

It then runs the unit tests of hx census (scripts/harness/hx_census.py): the census of synthetic play logs, the key list
against gpu/vk/vk_census.h, the in-job half against a stand-in player, and the client. They need no game, GPU, network or
dump, and take their fixtures from tests/test_harness_play.py.

python -I -B tests/test_harness_census.py [--cxx clang++]
"""
import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RUN_ALL_ARGS = ["--cxx", "{cxx}"]
ROOT = Path(__file__).resolve().parents[1]
CALL = re.compile(r"\bplay_census::(?!on\b)[A-Za-z_]\w*\(")
GUARD_LINE = re.compile(r"^\s*if \(play_census::on\) ")
GUARD_OPEN = re.compile(r"if \(play_census::on\) \{$")
HX = ROOT / "scripts" / "harness" / "hx.py"

sys.path.insert(0, str(ROOT / "scripts" / "harness"))
sys.path.insert(0, str(ROOT / "tests"))   # (python -I leaves this folder off the path)
import hx_capture
import hx_census
import hx_play
import jobs
import record
import remote
import test_harness_play as play


def code(line: str) -> str:
    """The line with string literals emptied and any // comment cut, so braces inside them do not count."""
    return re.sub(r"//.*", "", re.sub(r'"(?:\\.|[^"\\])*"', '""', line)).rstrip()


def unguarded_calls(text: str) -> list[int]:
    """The line numbers of play_census::<name>( calls that are not under `if (play_census::on)`."""
    found: list[int] = []
    guards: list[int] = []
    depth = 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = code(raw)
        if CALL.search(line) and not guards and not GUARD_LINE.match(line):
            found.append(number)
        for ch in line:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                while guards and depth <= guards[-1]:
                    guards.pop()
        if GUARD_OPEN.search(line):
            guards.append(depth - 1)
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cxx", default="g++")
    a = p.parse_args()

    with tempfile.TemporaryDirectory(prefix="census-") as tmp:
        exe = Path(tmp) / "census.exe"
        subprocess.run([a.cxx, "-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                        "-I" + str(ROOT / "gpu/vk"), str(ROOT / "tests/census_test.cpp"), "-o", str(exe)], check=True)
        for setting, want in ((None, "0"), ("1", "1"), ("0", "0")):
            env = {k: v for k, v in os.environ.items() if k != "RX_VK_CENSUS"}
            if setting is not None:
                env["RX_VK_CENSUS"] = setting
            done = subprocess.run([str(exe)], capture_output=True, text=True, env=env)
            if done.returncode != 0 or "play_census::on=" + want not in done.stdout.splitlines():
                raise SystemExit("FAIL: census_test with RX_VK_CENSUS=%s (want play_census::on=%s):\n%s%s"
                                 % (setting, want, done.stdout, done.stderr))

    files = sorted((ROOT / "gpu/vk").glob("*.cpp"))
    if not files:
        raise SystemExit("FAIL: no gpu/vk/*.cpp to check the census call sites in")
    for path in files:
        bad = unguarded_calls(path.read_text(encoding="utf-8"))
        if bad:
            raise SystemExit("FAIL: census call outside if (play_census::on) in %s, line %s"
                             % (path.relative_to(ROOT), ", ".join(map(str, bad))))

    unguarded = "void f() {\n    play_census::draw(false, false);\n}\n"
    guarded = "void f() {\n    if (play_census::on) play_census::draw(false, false);\n}\n"
    block = ("void f() {\n    if (play_census::on) {\n        play_census::present(0, 0, emit);\n    }\n"
             "    play_census::flush(emit);\n}\n")
    switch = "bool x = play_census::on;\n"
    if unguarded_calls(unguarded) != [2]:
        raise SystemExit("FAIL: source rule accepts an unguarded census call")
    if unguarded_calls(guarded) != []:
        raise SystemExit("FAIL: source rule rejects a census call under if (play_census::on) on its line")
    if unguarded_calls(block) != [5]:
        raise SystemExit("FAIL: source rule does not track a block opened by if (play_census::on) { and its close")
    if unguarded_calls(switch) != []:
        raise SystemExit("FAIL: source rule counts a read of play_census::on as a call")

    print("PASS: census switch, line grammar, window values, fed identifiers and the source rule")
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    if not result.wasSuccessful():
        raise SystemExit("FAIL: hx census unit tests (above)")


def values(**counts):
    """A window's count of every key: the named ones as given, every other key 0"""
    return {key: counts.get(key, 0) for key in hx_census.KEYS}


def window_lines(number, counts, per_line=12):
    """One window's lines as gpu/vk/vk_census.h writes them: every key in KEYS order, per_line pairs to a line"""
    pairs = ["%s=%d" % (key, counts[key]) for key in hx_census.KEYS]
    return ["vk: census: window=%d %s" % (number, " ".join(pairs[i:i + per_line]))
            for i in range(0, len(pairs), per_line)]


ADDRESS_LINE = "vk: program 0x7A11C0DE0002 compiled"
THREE_WINDOWS = [
    values(presents=30, draws=100, new_variants=4, frame_pipelines_max=7, frame_sets_max=5,
           programs_variants_1=2, programs_variants_2=1),
    values(presents=30, draws=50, new_variants=0, frame_pipelines_max=9, frame_sets_max=3,
           programs_variants_1=1, programs_variants_3=2),
    values(presents=12, draws=7, new_variants=2, frame_pipelines_max=2, frame_sets_max=6,
           programs_variants_2=3, programs_variants_16_up=1),
]
# the sums; the maxima; the last window's histogram (not the sums of it: programs_variants_2 is 3, not 1 + 0 + 3)
TOTALS = values(presents=72, draws=157, new_variants=6, frame_pipelines_max=9, frame_sets_max=6,
                programs_variants_2=3, programs_variants_16_up=1)
CURVE = [[30, 4], [60, 0], [72, 2]]


def three_windows_log():
    """The play log of THREE_WINDOWS: the first window split over lines with other renderer lines between, the others whole"""
    first, second, third = (window_lines(n, counts) for n, counts in enumerate(THREE_WINDOWS, 1))
    return "\n".join(first[:3] + [ADDRESS_LINE] + first[3:] + ["vk: frame presented"] + second + third) + "\n"


CENSUS_SUMMARY = {"totals": TOTALS, "curve": CURVE, "windows": 3}

# The stand-in player: what the renderer does when a census play runs. It logs its invocation and the environment it was
# given (the census switch among it), writes each PNG of the frames folder as a present, logs its device as the renderer
# does, and, when RX_VK_CENSUS is 1, writes the play log's census lines the test names (the renderer logs them only then).
# The #! line is added by the test, with this interpreter.
CENSUS_PLAYER = r'''import json
import os
import shutil
import sys

KEYS = ("RX_CAPTURE", "RX_VK_SHOTS", "RX_VK_SHOT_EVERY", "RX_VK_PIPE_SKIP", "VK_ICD_FILENAMES", "RX_VK_CENSUS")


def main(argv):
    seen = {key: os.environ.get(key) for key in KEYS}
    with open(os.environ["STAND_IN_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"exe": argv[0], "argv": argv[1:], "env": seen}) + "\n")
    frames, shots = os.environ["STAND_IN_FRAMES"], seen["RX_VK_SHOTS"]
    for name in sorted(os.listdir(frames)):
        shutil.copyfile(os.path.join(frames, name), os.path.join(shots, "vk_" + name))
    sys.stderr.write("vk: device 0: Example GPU Listed, Vulkan 1.3, driver 0x1, type 2\n")
    sys.stderr.write("vk: device 1: %s, Vulkan 1.3, driver 0x1, type 1\n" % os.environ["STAND_IN_DEVICE"])
    sys.stderr.write("vk: %s, Vulkan 1.3, driver 0x1; mesh shaders yes, host memory import yes, pipeline libraries no\n"
                     % os.environ["STAND_IN_DEVICE"])
    if seen["RX_VK_CENSUS"] == "1":
        with open(os.environ["STAND_IN_CENSUS"], encoding="utf-8") as f:
            sys.stderr.write(f.read())
    return int(os.environ["STAND_IN_EXIT"])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''


class CensusHeaderTests(unittest.TestCase):
    def test_the_keys_are_the_header_key_names_in_order_and_each_is_quoted_in_the_header(self):
        header = (ROOT / "gpu" / "vk" / "vk_census.h").read_text(encoding="utf-8")
        names = re.search(r"kKeyNames\[kKeyCount\] = \{(.*?)\};", header, re.S)
        if names is None:
            self.fail("kKeyNames is not in gpu/vk/vk_census.h")
        self.assertEqual(re.findall(r'"([a-z0-9_]+)"', names.group(1)), list(hx_census.KEYS))
        for key in hx_census.KEYS:
            self.assertIn('"%s"' % key, header)


class ParseTests(unittest.TestCase):
    def test_one_window_with_every_key_once_parses(self):
        summary = hx_census.parse_census("\n".join(window_lines(1, values(presents=1, new_variants=2))) + "\n")
        self.assertEqual(summary["windows"], 1)
        self.assertEqual(summary["curve"], [[1, 2]])

    def test_windows_split_over_lines_with_other_lines_between_parse_to_the_exact_totals_maxima_histogram_and_curve(self):
        summary = hx_census.parse_census(three_windows_log())
        self.assertEqual(summary["totals"], TOTALS)
        self.assertEqual(summary["curve"], CURVE)
        self.assertEqual(summary["windows"], 3)

    def test_a_log_with_no_census_window_is_refused(self):
        for text in ("", "vk: frame presented\n"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(hx_census.CensusError, "no census window"):
                    hx_census.parse_census(text)

    def test_each_malformed_census_is_refused_with_its_reason(self):
        one = values(presents=1)
        good = window_lines(1, one)
        cases = [
            ("an unknown key", "\n".join(good).replace(" presents=1", " presentz=1", 1), "not in the census"),
            ("a key named twice in one window", "\n".join(good + ["vk: census: window=1 presents=1"]),
             "names presents twice"),
            ("a key missing from a window", "\n".join(good).replace(" presents=1", "", 1), "lacks presents"),
            ("a count in hex", "\n".join(good).replace(" presents=1", " presents=0x1", 1), "not a decimal count"),
            ("a count with a fraction", "\n".join(good).replace(" presents=1", " presents=1.5", 1),
             "not a decimal count"),
            ("a negative count", "\n".join(good).replace(" presents=1", " presents=-1", 1), "not a decimal count"),
            ("an empty count", "\n".join(good).replace(" presents=1", " presents=", 1), "not a decimal count"),
            ("a count that is a word", "\n".join(good).replace(" presents=1", " presents=one", 1),
             "not a decimal count"),
            ("text after the pairs", "\n".join([good[0] + " junk"] + good[1:]), "other than key=count pairs"),
            ("a pair with no =", "\n".join(good).replace(" presents=1", " presents", 1), "other than key=count pairs"),
            ("two spaces between pairs", "\n".join(good).replace(" presents=1", " presents=1 ", 1),
             "other than key=count pairs"),
            ("no space after the window number", "\n".join(good).replace("window=1 ", "window=1", 1),
             "other than key=count pairs"),
            ("a window number that is not one", "\n".join(good).replace("window=1 ", "window=x ", 1),
             "other than key=count pairs"),
            ("a window before the one it follows", "\n".join(window_lines(2, one) + window_lines(1, one)),
             "out of order"),
            ("a window after a gap", "\n".join(window_lines(1, one) + window_lines(3, one)), "out of order"),
            ("window 0", "\n".join(window_lines(0, one)), "out of order"),
        ]
        for name, text, reason in cases:
            with self.subTest(case=name):
                with self.assertRaisesRegex(hx_census.CensusError, reason):
                    hx_census.parse_census(text)

    def test_a_refusal_names_no_text_of_the_log(self):
        good = window_lines(1, values(presents=1))
        leaks = [
            "\n".join(good).replace(" presents=1", " 0x7A11C0DE0002=1", 1),
            "\n".join(good[:-1] + [good[-1] + " program 0x7A11C0DE0002"]),
        ]
        for text in leaks:
            with self.subTest(text=text[:40]):
                with self.assertRaises(hx_census.CensusError) as caught:
                    hx_census.parse_census(text)
                self.assertNotIn("7a11c0de", str(caught.exception).lower())


# The in-job half, run in a folder of its own on the stand-in build, against the stand-in compiler and player.
EXPECTED_RUN_KEYS = ["against", "build", "capture", "capture_build", "census", "device", "kind", "listed", "live",
                     "presents", "spread", "verdict", "versus_against", "versus_live", "versus_live_differing"]
W, H = play.W, play.H


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
        (self.build / "build.txt").write_text(play.build_text(), encoding="utf-8")
        self.compiles = self.base / "compiles.jsonl"
        self.invocations = self.base / "invocations.jsonl"
        player = self.base / "player_stand_in.py"
        player.write_text("#!%s -I\n%s" % (sys.executable, CENSUS_PLAYER), encoding="utf-8")
        self.compiler = self.base / "stand_in_compiler.py"
        self.compiler.write_text(play.COMPILER_STAND_IN.replace("@LOG@", repr(str(self.compiles)))
                                 .replace("@PLAYER@", repr(str(player))), encoding="utf-8")
        self.census_log = self.base / "census.log"
        self.census_log.write_text(three_windows_log(), encoding="utf-8")
        self.messages = []
        self.live_frames = [play.frame(n) for n in range(1, 5)]
        self.patch(mock.patch.object(hx_play, "CXX", (sys.executable, "-I", str(self.compiler))))
        self.patch(mock.patch.object(hx_capture, "command_outputs", return_value=play.OUTPUTS))
        self.patch(mock.patch.object(record, "write_journal", side_effect=self.messages.append))
        self.patch(mock.patch.dict(os.environ, {"STAND_IN_LOG": str(self.invocations), "STAND_IN_DEVICE": play.EXPECT,
                                                "STAND_IN_EXIT": "0", "STAND_IN_CENSUS": str(self.census_log),
                                                "STAND_IN_BUILD_FAIL": ""}))
        self.use_frames(self.live_frames)
        self.live = self.write_run([play.png(W, H, f) for f in self.live_frames], play.LIVE_DOC)
        self.assertEqual(self.live, "000001")

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def use_frames(self, frames):
        """The stand-in player's presents: a folder of the frames' PNGs, named as the renderer names them"""
        folder = Path(tempfile.mkdtemp(dir=self.base))
        for n, pixels in enumerate(frames, 1):
            (folder / ("%06d.png" % n)).write_bytes(play.png(W, H, pixels))
        os.environ["STAND_IN_FRAMES"] = str(folder)

    def write_run(self, pngs, doc):
        shots = {"present/%06d.png" % (n + 1): data for n, data in enumerate(pngs)}
        run_id, _ = record.write_run(self.record, play.facts(), {"capture.json": play.doc_bytes(doc)}, shots,
                                     journal=lambda message: None)
        return run_id

    def play(self):
        """Runs the in-job half once in a job of its own; returns its status and sets self.job and self.output"""
        job = self.base / "jobs" / jobs.new_id("check")
        (job / "game").mkdir(parents=True)
        (job / "game" / "hookshot").write_bytes(play.HOOKSHOT)
        (job / "game" / "hookshot_vk.so").write_bytes(play.HOOKSHOT_VK)
        argv = ["--in-job", str(job), "--hold", str(self.hold), "--expect", play.EXPECT, "--device", play.DEVICE,
                "--live", self.live, "--record", str(self.record), "--build-folder", str(self.build),
                "--closure", play.CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % play.ICD]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = hx_census.main(argv)
        self.job, self.output = job, out.getvalue()
        return status

    def doc(self, run_id):
        return json.loads((self.record / "runs" / run_id / "files" / "capture.json").read_text(encoding="utf-8"))

    def manifest(self, run_id):
        return record.read_manifest(self.record, run_id)[0]

    def test_the_player_is_built_from_the_build_source_and_run_once_on_the_capture_with_the_census_and_the_switches(self):
        self.assertEqual(self.play(), 0, self.output)
        compiles = play.read_jsonl(self.compiles)
        self.assertEqual(len(compiles), 2, compiles)
        source = str(self.build / "src" / "gpu" / "player" / "rx_player.cpp")
        game = self.job / "game"
        self.assertEqual(compiles[0][compiles[0].index("-c") + 1], source)
        self.assertEqual(compiles[1][compiles[1].index("-o") + 1], str(game / "rx_player"))
        invocations = play.read_jsonl(self.invocations)
        self.assertEqual(len(invocations), 1, invocations)
        self.assertEqual(invocations[0]["exe"], str(game / "rx_player"))
        self.assertEqual(invocations[0]["argv"], [play.CAPTURE, "--renderer", str(game / "hookshot_vk.so")])
        env = invocations[0]["env"]
        self.assertEqual(env["RX_VK_CENSUS"], "1")
        self.assertEqual(env["RX_VK_SHOTS"], str(self.job / "shots"))
        self.assertEqual(env["RX_VK_SHOT_EVERY"], "1")
        self.assertEqual(env["RX_VK_PIPE_SKIP"], "0")
        self.assertEqual(env["VK_ICD_FILENAMES"], play.ICD)
        self.assertIsNone(env["RX_CAPTURE"])

    def test_the_run_is_a_play_of_the_capture_with_the_census_and_no_against_judgement(self):
        self.assertEqual(self.play(), 0, self.output)
        doc = self.doc("000002")
        self.assertEqual(sorted(doc), sorted(EXPECTED_RUN_KEYS))
        self.assertEqual(doc["kind"], "play")
        self.assertEqual(doc["capture"], play.CAPTURE)
        self.assertEqual(doc["capture_build"], play.SHA)
        self.assertEqual(doc["build"], play.SHA)
        self.assertEqual(doc["device"], play.DEVICE)
        self.assertEqual(doc["live"], self.live)
        self.assertEqual(doc["presents"], 4)
        self.assertEqual(doc["verdict"], "pass")
        self.assertEqual(doc["census"], CENSUS_SUMMARY)
        self.assertEqual(doc["versus_live"], {"present/%06d.png" % n: {"pixels": 0, "max_channel": 0}
                                              for n in (1, 2, 3, 4)})
        self.assertEqual(doc["versus_live_differing"], 0)
        self.assertIsNone(doc["against"])
        self.assertIsNone(doc["versus_against"])
        self.assertIsNone(doc["spread"])
        self.assertIsNone(doc["listed"])

    def test_versus_live_differing_counts_the_presents_whose_diff_from_live_is_not_0_pixels(self):
        # present 2 differs in two pixels; present 4 is only in live, which is not 0 pixels either
        self.use_frames([self.live_frames[0], play.changed(self.live_frames[1], [0, 1], 2), self.live_frames[2]])
        self.assertEqual(self.play(), 0, self.output)
        doc = self.doc("000002")
        self.assertEqual(doc["versus_live"]["present/000002.png"], {"pixels": 2, "max_channel": 2})
        self.assertEqual(doc["versus_live"]["present/000004.png"], {"only": "live"})
        self.assertEqual(doc["versus_live_differing"], 2)

    def test_the_run_has_every_fact_key_one_screenshot_per_present_and_keeps_the_play_log_in_the_record(self):
        self.assertEqual(self.play(), 0, self.output)
        manifest = self.manifest("000002")
        self.assertEqual(sorted(manifest["facts"]), sorted(record.FACT_KEYS))
        self.assertEqual(sorted(manifest["screenshots"]), ["present/%06d.png" % n for n in (1, 2, 3, 4)])
        self.assertEqual(sorted(manifest["files"]), ["build.log", "capture.json", "play.log"])
        play_log = (self.record / "runs" / "000002" / "files" / "play.log").read_text(encoding="utf-8")
        self.assertIn("vk: census: window=1 ", play_log)
        self.assertIn(ADDRESS_LINE, play_log)

    def test_the_census_and_the_run_document_hold_fixed_keys_and_numbers_and_no_address_or_program(self):
        self.assertEqual(self.play(), 0, self.output)
        census = self.doc("000002")["census"]
        self.assertEqual(sorted(census["totals"]), sorted(hx_census.KEYS))
        for value in census["totals"].values():
            self.assertIsInstance(value, int)
        for presents, new_variants in census["curve"]:
            self.assertIsInstance(presents, int)
            self.assertIsInstance(new_variants, int)
        text = (self.record / "runs" / "000002" / "files" / "capture.json").read_text(encoding="utf-8")
        self.assertNotIn("7a11c0de", text.lower())
        self.assertNotIn("0x", text)

    def test_a_play_log_without_census_lines_is_FAIL_with_exit_1(self):
        self.census_log.write_text("vk: frame presented\n", encoding="utf-8")
        self.assertEqual(self.play(), 1, self.output)
        doc = self.doc("000002")
        self.assertEqual(doc["verdict"], "FAIL: census: the play log has no census window")
        self.assertIsNone(doc["census"])
        self.assertEqual(doc["presents"], 4)
        self.assertIn("play.log", self.manifest("000002")["files"])

    def test_a_play_log_the_parser_refuses_is_FAIL_with_exit_1(self):
        self.census_log.write_text("\n".join(window_lines(1, values(presents=1)) + ["vk: census: window=1 presents=1"])
                                   + "\n", encoding="utf-8")
        self.assertEqual(self.play(), 1, self.output)
        doc = self.doc("000002")
        self.assertEqual(doc["verdict"], "FAIL: census: window 1 names presents twice")
        self.assertIsNone(doc["census"])

    def test_a_build_that_fails_writes_its_run_with_no_census_and_exits_1(self):
        os.environ["STAND_IN_BUILD_FAIL"] = "1"
        self.assertEqual(self.play(), 1, self.output)
        doc = self.doc("000002")
        self.assertTrue(doc["verdict"].startswith("FAIL: build: "), doc["verdict"])
        self.assertIsNone(doc["presents"])
        self.assertIsNone(doc["census"])
        self.assertIsNone(doc["versus_live_differing"])
        self.assertEqual(sorted(self.manifest("000002")["files"]), ["build.log", "capture.json"])

    def test_hx_play_accepts_the_census_run_as_the_against_of_a_play_of_the_capture(self):
        self.assertEqual(self.play(), 0, self.output)
        live_doc = hx_play.load_run(self.record, self.live)[0]
        self.assertIsNone(hx_play.pair_refusal(self.live, live_doc, "000002", self.doc("000002"), play.DEVICE))


class ClientTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "harness.toml"
        self.config.write_text(play.CONFIG, encoding="utf-8")
        self.build_stdout = play.build_text()
        self.docs = {"000001": play.LIVE_DOC, "000002": play.PLAY_DOC}
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

    def refused(self, argv, reason):
        err = io.StringIO()
        with play.quiet(), contextlib.redirect_stderr(err):
            status = hx_census.main(argv)
        self.assertEqual(status, 2, err.getvalue())
        self.assertIn(reason, err.getvalue())
        self.start.assert_not_called()

    def test_an_unknown_device_exits_2_before_any_remote_call(self):
        self.refused(["000001", "--device", "nope", "--build", play.SHA], "no device 'nope'")
        self.remote.assert_not_called()

    def test_a_malformed_run_id_exits_2_before_any_job(self):
        for run_id in ("1", "0000001", "../000001", "00000a"):
            with self.subTest(run=run_id):
                self.refused([run_id, "--device", play.DEVICE, "--build", play.SHA], "not a run id")
        self.remote.assert_not_called()

    def test_a_malformed_build_id_exits_2_before_any_job(self):
        for build in ("0123", play.SHA[:39], play.SHA.upper(), "g" * 40, play.SHA + "0"):
            with self.subTest(build=build):
                self.refused(["000001", "--device", play.DEVICE, "--build", build], "not a build commit id")
        self.remote.assert_not_called()

    def test_a_run_that_is_not_a_capture_is_refused_before_any_job(self):
        self.refused(["000002", "--device", play.DEVICE, "--build", play.SHA], "is not a capture run")

    def test_a_build_folder_that_holds_another_commit_is_refused_before_the_job(self):
        self.build_stdout = play.build_text(sha="f" * 40)
        self.refused(["000001", "--device", play.DEVICE, "--build", play.SHA],
                     "says sha %s, not %s" % ("f" * 40, play.SHA))
        self.assertTrue(any("builds/%s/build.txt" % play.SHA in c.args[1] for c in self.remote.call_args_list))

    def test_a_census_starts_one_job_with_the_named_build_game_and_the_device_environment(self):
        with play.quiet():
            status = hx_census.main(["000001", "--device", play.DEVICE, "--build", play.SHA])
        self.assertEqual(status, 0)
        self.start.assert_called_once()
        cfg_arg, job_id, argv = self.start.call_args.args
        self.assertTrue(job_id.startswith("check-"), job_id)
        self.assertEqual(self.start.call_args.kwargs, {"game": "/example/harness/builds/%s/game" % play.SHA})
        job = "/example/harness/jobs/%s" % job_id
        self.assertEqual(argv, [
            play.ENTER, play.CLOSURE, "python", "-I", job + "/tools/scripts/harness/hx.py", "census", "--in-job", job,
            "--hold", "/example/hold-file", "--expect", play.EXPECT, "--device", play.DEVICE, "--live", "000001",
            "--record", "/example/harness/results", "--build-folder", "/example/harness/builds/%s" % play.SHA,
            "--closure", play.CLOSURE, "--env", "VK_ICD_FILENAMES=%s" % play.ICD,
        ])

    def test_hx_dispatches_census_and_refuses_an_unknown_device_with_exit_2(self):
        env = {**os.environ, "HX_CONFIG": str(self.config)}
        done = subprocess.run([sys.executable, "-I", "-B", str(HX), "census", "000001", "--device", "nope",
                               "--build", play.SHA], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("no device 'nope'", done.stderr)


if __name__ == "__main__":
    main()
