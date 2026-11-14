"""hx analyze (scripts/harness/hx_analyze.py) with no GPU, game, journal or network. The client refuses, each before any job
starts and with exit 2: a malformed run id, a run whose capture.json cannot be read, a run that is not a capture, an
--against run that is the run itself, is not a capture or is of another scene (or of none, when the run has one), a
capture device the configuration does not list, and a build whose build.txt names another commit or no /nix/store
closure. A valid analysis starts one check job with the capture's build folder, closure and device expect, --against
only when given, and no game folder. In-job, analysis.py (another task's, absent from this tree) is a stand-in that
records what it is given and returns what the test sets: it receives the capture folder, the window from capture.json
(None when absent or malformed) and the --against pair. The run holds analysis.json with the fields the command records
and the stand-in's dict, and no capture folder path; an AnalysisError writes a run marked FAIL with null statistics and
exits 1; the in-job half repeats the refusals and writes no run for them. The run is journalled, and hx verify passes on
the record. The journal is injected.

python -I -B tests/test_harness_hx_analyze.py
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HX = ROOT / "scripts" / "harness" / "hx.py"
sys.path.insert(0, str(ROOT / "scripts" / "harness"))

# analysis.py is another task's and not in this tree: hx_analyze imports this stand-in, which records each call and returns
# the statistics the test sets, or raises AnalysisError with the message the test sets
class AnalysisError(Exception):
    pass


def analyze(capture, window=None, against=None):
    analysis.calls.append((capture, window, against))
    if analysis.error is not None:
        raise AnalysisError(analysis.error)
    return analysis.statistics


analysis = types.SimpleNamespace(AnalysisError=AnalysisError, analyze=analyze, calls=[], error=None, statistics={})
sys.modules["analysis"] = analysis

import hx_analyze
import hx_capture
import hx_verify
import jobs
import record
import remote

SHA = "0123456789abcdef0123456789abcdef01234567"
CLOSURE = "/nix/store/%s-hookshot-closure" % ("a" * 32)
DEVICE = "first"
EXPECT = "Example GPU One"
ENTER = "/example/enter-closure.sh"
CAPTURE = "/example/harness/results/captures/check-example"
SCENE = "town-at-night"
STATISTICS = {"stand_in": {"command_buffers": 3}}
HOOKSHOT = b"stand-in hookshot"
HOOKSHOT_VK = b"stand-in hookshot_vk.so"

CONFIG = """host = "example-host"
hold = "/example/hold-file"
root = "/example/harness"
dump = "/example/dump"
enter_closure = "/example/enter-closure.sh"

[devices.first]
expect = "Example GPU One"
"""

VULKAN = """GPU0:
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

# The capture runs (what hx capture writes) and the play run the client refuses.
CAPTURE_DOC = {"capture": CAPTURE, "build": SHA, "device": DEVICE, "frames": 4, "presents": 4, "verdict": "pass"}
SCENE_DOC = {**CAPTURE_DOC, "scene": SCENE, "window": {"first": 3, "last": 9}}
PLAY_DOC = {"kind": "play", "capture": CAPTURE, "device": DEVICE, "build": SHA, "live": "000001", "against": None,
            "presents": 4, "verdict": "pass"}


def build_text(sha=SHA, closure=CLOSURE):
    return "closure: %s\nsha: %s\ndirty: none\n" % (closure, sha)


def facts():
    return {key: {"value": key} for key in record.FACT_KEYS}


def doc_bytes(doc):
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class ClientTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "harness.toml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.build_stdout = build_text()
        self.docs = {
            "000001": SCENE_DOC,
            "000002": PLAY_DOC,
            "000003": {**SCENE_DOC, "capture": CAPTURE + "-second"},
            "000004": {**SCENE_DOC, "scene": "medium"},
            "000005": CAPTURE_DOC,
            "000006": {"build": SHA, "device": DEVICE},
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

    def refuse(self, argv):
        """Runs the client on argv: its status and what it printed to stderr"""
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx_analyze.main(argv)
        return status, err.getvalue()

    def test_a_malformed_run_id_exits_2_before_any_remote_call(self):
        for run_id in ("1", "0000001", "../000001", "00000a"):
            with self.subTest(run=run_id):
                status, err = self.refuse([run_id])
                self.assertEqual(status, 2)
                self.assertIn("not a run id", err)
                status, err = self.refuse(["000001", "--against", run_id])
                self.assertEqual(status, 2)
                self.assertIn("not a run id", err)
        self.remote.assert_not_called()
        self.start.assert_not_called()

    def test_a_run_whose_capture_json_cannot_be_read_exits_2_before_any_job(self):
        status, err = self.refuse(["000009"])
        self.assertEqual(status, 2)
        self.assertIn("000009", err)
        self.start.assert_not_called()

    def test_a_run_that_is_not_a_capture_exits_2_before_any_job(self):
        for run_id in ("000002", "000006"):
            with self.subTest(run=run_id):
                status, err = self.refuse([run_id])
                self.assertEqual(status, 2)
                self.assertIn("is not a capture run", err)
                status, err = self.refuse(["000001", "--against", run_id])
                self.assertEqual(status, 2)
                self.assertIn("is not a capture run", err)
        self.start.assert_not_called()

    def test_an_against_run_that_is_the_run_itself_exits_2_before_any_job(self):
        status, err = self.refuse(["000001", "--against", "000001"])
        self.assertEqual(status, 2)
        self.assertIn("names the run itself", err)
        self.start.assert_not_called()

    def test_an_against_run_of_another_scene_or_without_one_when_the_run_has_one_exits_2(self):
        for against in ("000004", "000005"):
            with self.subTest(against=against):
                status, err = self.refuse(["000001", "--against", against])
                self.assertEqual(status, 2)
                self.assertIn("has scene", err)
        self.start.assert_not_called()

    def test_a_capture_device_the_configuration_does_not_list_exits_2_before_any_build_is_read(self):
        for device in ("nope", ["first"]):
            with self.subTest(device=device):
                self.docs["000001"] = {**SCENE_DOC, "device": device}
                status, err = self.refuse(["000001"])
                self.assertEqual(status, 2)
                self.assertIn("names no device of the configuration", err)
        self.assertFalse(any("build.txt" in c.args[1] for c in self.remote.call_args_list))
        self.start.assert_not_called()

    def test_a_build_whose_build_txt_names_another_commit_exits_2_before_any_job(self):
        self.build_stdout = build_text(sha="f" * 40)
        status, err = self.refuse(["000001"])
        self.assertEqual(status, 2)
        self.assertIn("not the capture's %s" % SHA, err)
        self.start.assert_not_called()

    def test_a_build_whose_closure_is_not_a_store_path_exits_2_before_any_job(self):
        self.build_stdout = build_text(closure="/tmp/closure")
        status, err = self.refuse(["000001"])
        self.assertEqual(status, 2)
        self.assertIn("closure must be a /nix/store/ path", err)
        self.start.assert_not_called()

    def test_a_valid_analysis_starts_one_check_job_with_the_build_closure_and_expect_and_no_game_folder(self):
        with quiet():
            status = hx_analyze.main(["000001"])
        self.assertEqual(status, 0)
        self.start.assert_called_once()
        _, job_id, argv = self.start.call_args.args
        self.assertTrue(job_id.startswith("check-"), job_id)
        self.assertEqual(self.start.call_args.kwargs, {})
        job = "/example/harness/jobs/%s" % job_id
        self.assertEqual(argv, [
            ENTER, CLOSURE, "python", "-I", job + "/tools/scripts/harness/hx.py", "analyze", "--in-job", job,
            "--hold", "/example/hold-file", "--record", "/example/harness/results", "--run", "000001",
            "--build-folder", "/example/harness/builds/%s" % SHA, "--closure", CLOSURE, "--expect", EXPECT,
        ])

    def test_an_against_run_is_passed_to_the_job_after_the_run(self):
        with quiet():
            status = hx_analyze.main(["000001", "--against", "000003"])
        self.assertEqual(status, 0)
        argv = self.start.call_args.args[2]
        self.assertEqual(argv[argv.index("--run") + 1], "000001")
        self.assertEqual(argv[argv.index("--against") + 1], "000003")

    def test_hx_dispatches_analyze_to_hx_analyze_and_refuses_a_malformed_run_id_with_exit_2(self):
        # hx.py is loaded in this process, not run as a subprocess: a subprocess cannot see the stand-in analysis module
        spec = importlib.util.spec_from_file_location("hx", HX)
        hx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hx)
        err = io.StringIO()
        with quiet(), contextlib.redirect_stderr(err):
            status = hx.main(["analyze", "1"])
        self.assertEqual(status, 2, err.getvalue())
        self.assertIn("analyze: not a run id", err.getvalue())
        self.start.assert_not_called()


class InJobTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.record = self.base / "results"
        self.hold = self.base / "hold-file"
        self.build = self.base / "build"
        (self.build / "game").mkdir(parents=True)
        (self.build / "game" / "hookshot").write_bytes(HOOKSHOT)
        (self.build / "game" / "hookshot_vk.so").write_bytes(HOOKSHOT_VK)
        (self.build / "build.txt").write_text(build_text(), encoding="utf-8")
        self.messages = []
        self.patch(mock.patch.object(hx_capture, "command_outputs", return_value=OUTPUTS))
        self.patch(mock.patch.object(record, "write_journal", side_effect=self.messages.append))
        analysis.calls, analysis.error, analysis.statistics = [], None, STATISTICS
        self.live = self.write_run(SCENE_DOC)
        self.assertEqual(self.live, "000001")

    def patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def write_run(self, doc):
        """Writes a run whose capture.json is doc into the record root, journalled into self.messages; returns its id"""
        run_id, _ = record.write_run(self.record, facts(), {"capture.json": doc_bytes(doc)}, {},
                                     journal=self.messages.append)
        return run_id

    def analyze(self, run="000001", against=None):
        """Runs the in-job half once in a job of its own; returns its status and sets self.output"""
        job = self.base / "jobs" / jobs.new_id("check")
        job.mkdir(parents=True)
        argv = ["--in-job", str(job), "--hold", str(self.hold), "--record", str(self.record), "--run", run]
        if against is not None:
            argv += ["--against", against]
        argv += ["--build-folder", str(self.build), "--closure", CLOSURE, "--expect", EXPECT]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = hx_analyze.main(argv)
        self.output = out.getvalue()
        return status

    def doc(self, run_id):
        return json.loads((self.record / "runs" / run_id / "files" / "analysis.json").read_text(encoding="utf-8"))

    def manifest(self, run_id):
        return record.read_manifest(self.record, run_id)[0]

    def test_the_in_job_half_waits_for_the_hold_file_before_it_analyses(self):
        wait = self.patch(mock.patch.object(jobs, "wait_hold"))
        self.assertEqual(self.analyze(), 0, self.output)
        wait.assert_called_once_with(str(self.hold))
        self.assertEqual(len(analysis.calls), 1)

    def test_the_stand_in_receives_the_capture_folder_and_the_window_of_capture_json(self):
        self.assertEqual(self.analyze(), 0, self.output)
        self.assertEqual(analysis.calls, [(CAPTURE, (3, 9), None)])

    def test_a_capture_without_a_window_or_with_a_malformed_one_gives_the_stand_in_none(self):
        cases = {
            "no window": CAPTURE_DOC,
            "one end only": {**CAPTURE_DOC, "window": {"first": 3}},
            "not ints": {**CAPTURE_DOC, "window": {"first": "3", "last": 9}},
        }
        for name, doc in cases.items():
            with self.subTest(case=name):
                analysis.calls = []
                run = self.write_run(doc)
                self.assertEqual(self.analyze(run=run), 0, self.output)
                self.assertEqual(analysis.calls, [(CAPTURE, None, None)])

    def test_with_against_the_stand_in_receives_the_against_capture_folder_and_its_window(self):
        other = self.write_run({**SCENE_DOC, "capture": CAPTURE + "-second", "window": {"first": 4, "last": 8}})
        self.assertEqual(self.analyze(against=other), 0, self.output)
        self.assertEqual(analysis.calls, [(CAPTURE, (3, 9), (CAPTURE + "-second", (4, 8)))])
        self.assertEqual(self.doc("000003")["against_run"], other)

    def test_the_run_holds_analysis_json_with_the_fields_the_stand_in_dict_and_no_capture_folder(self):
        self.assertEqual(self.analyze(), 0, self.output)
        self.assertEqual(sorted(self.manifest("000002")["files"]), ["analysis.json"])
        self.assertEqual(self.manifest("000002")["screenshots"], {})
        expected = {
            "kind": "analysis",
            "capture_run": "000001",
            "against_run": None,
            "capture_build": SHA,
            "device": DEVICE,
            "scene": SCENE,
            "window": [3, 9],
            "verdict": "pass",
            "statistics": STATISTICS,
        }
        text = (self.record / "runs" / "000002" / "files" / "analysis.json").read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(expected, indent=2, sort_keys=True) + "\n")
        self.assertNotIn(CAPTURE, text)

    def test_the_run_has_every_fact_key_and_the_build_commit(self):
        self.assertEqual(self.analyze(), 0, self.output)
        facts_of_run = self.manifest("000002")["facts"]
        self.assertEqual(sorted(facts_of_run), sorted(record.FACT_KEYS))
        self.assertEqual(facts_of_run["shas"], {"hookshot": SHA})

    def test_a_pass_exits_0_and_prints_the_run_id(self):
        self.assertEqual(self.analyze(), 0, self.output)
        self.assertIn("analyze: pass", self.output)
        self.assertIn("run: 000002", self.output)

    def test_the_run_is_journalled_and_hx_verify_passes_on_the_record(self):
        self.assertEqual(self.analyze(), 0, self.output)
        self.assertEqual(len(self.messages), 2, self.messages)
        problems, count = hx_verify.check(self.record, record.parse_journal("\n".join(self.messages)))
        self.assertEqual((problems, count), ([], 2))

    def test_an_analysis_error_writes_a_fail_run_with_null_statistics_and_exits_1(self):
        analysis.error = "no presents in the capture"
        self.assertEqual(self.analyze(), 1, self.output)
        self.assertIn("analyze: FAIL: no presents in the capture", self.output)
        self.assertIn("run: 000002", self.output)
        doc = self.doc("000002")
        self.assertEqual(doc["verdict"], "FAIL: no presents in the capture")
        self.assertIsNone(doc["statistics"])
        self.assertEqual(doc["window"], [3, 9])

    def test_the_in_job_half_repeats_the_refusals_and_writes_no_run_for_them(self):
        self.write_run(PLAY_DOC)
        self.write_run({**SCENE_DOC, "scene": "medium"})
        cases = (
            ("000001", "000001", "names the run itself"),
            ("000001", "000002", "is not a capture run"),
            ("000001", "000003", "has scene"),
            ("000009", None, "000009"),
        )
        for run, against, phrase in cases:
            with self.subTest(run=run, against=against):
                analysis.calls = []
                self.assertEqual(self.analyze(run=run, against=against), 2, self.output)
                self.assertIn(phrase, self.output)
                self.assertEqual(analysis.calls, [])
                self.assertEqual(record.run_ids(self.record), ["000001", "000002", "000003"])

    def test_the_in_job_half_refuses_a_build_whose_build_txt_names_another_commit(self):
        (self.build / "build.txt").write_text(build_text(sha="f" * 40), encoding="utf-8")
        self.assertEqual(self.analyze(), 2, self.output)
        self.assertIn("not the capture's %s" % SHA, self.output)
        self.assertEqual(analysis.calls, [])
        self.assertEqual(record.run_ids(self.record), ["000001"])


if __name__ == "__main__":
    unittest.main()
