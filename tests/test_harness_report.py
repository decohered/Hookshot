"""scripts/harness/hx.py report, as a subprocess, against fixture runs that scripts/harness/record.py wrote into
temporary roots: a first run and a chained second run print the text their manifests say; the output comes from the
record alone (another working directory, an empty PATH and discarded sources change nothing); a run file or screenshot
that changed or went missing refuses the report; an unknown id or a missing root refuses it; and hx.py is the frozen
file. No GPU, game, journal or network is used.

python -I -B tests/test_harness_report.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
HX = HARNESS / "hx.py"
HX_SHA256 = "d1cd2df6e2199d9a767b3e81aaec13c1b097b57e3d8151395c3f6aafa9cf1681"
sys.path.insert(0, str(HARNESS))
import record

# What Python needs to start with no PATH entries: on Windows, the system folder its libraries load from
BARE_ENV = {"PATH": "", **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if os.name == "nt" else {})}


def facts(**changes):
    """A facts dict with every FACT_KEYS key; values are stored verbatim, so nested values are used too"""
    f = {key: {"value": key} for key in record.FACT_KEYS}
    f.update(changes)
    return f


def sha(data):
    return hashlib.sha256(data).hexdigest()


def expected_report(root, run_id):
    """The report of a run, built line by line from its manifest file: the format in the report's own docstring, with
    the manifest's hash taken from the bytes on disk"""
    raw = (Path(root) / "runs" / run_id / "manifest.json").read_bytes()
    m = json.loads(raw)
    lines = [f"run {m['id']}", f"written {m['written']}", f"manifest {sha(raw)}"]
    if m["prior"] is None:
        lines.append("prior none")
    else:
        lines.append(f"prior {m['prior']['id']} {m['prior']['sha256']}")
    lines.append("facts")
    for key in record.FACT_KEYS:
        lines.append(f"  {key}: {json.dumps(m['facts'][key], sort_keys=True)}")
    lines.append("files")
    for path in sorted(m["files"]):
        entry = m["files"][path]
        lines.append(f"  {entry['sha256']} {entry['size']} {path}")
    lines.append("screenshots")
    for name in sorted(m["screenshots"]):
        entry = m["screenshots"][name]
        lines.append(f"  {entry['sha256']} {entry['size']} {name}")
    return "\n".join(lines) + "\n"


class ReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "record"
        self.journal = []

    def write(self, files=None, shots=None, fact=None):
        """write_run into the test's root, with the test's journal"""
        return record.write_run(self.root, facts() if fact is None else fact, files or {}, shots or {},
                                journal=self.journal.append)

    def run_hx(self, *args, cwd=None, env=None):
        command = [sys.executable, "-I", "-B", str(HX), "report", *args]
        return subprocess.run(command, capture_output=True, cwd=cwd, env=env)

    def report_text(self, *args, **kwargs):
        """The report's stdout, after checking that it exited 0 with nothing on stderr"""
        done = self.run_hx(*args, **kwargs)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stderr, b"")
        return done.stdout.decode("utf-8")

    def assert_refused(self, *args, names, **kwargs):
        """The report exited nonzero, printed nothing, and its stderr names each of the given strings"""
        done = self.run_hx(*args, **kwargs)
        stderr = done.stderr.decode("utf-8", "replace")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout, b"")
        for name in names:
            self.assertIn(name, stderr)

    def fixture(self):
        """A run with one run file and one screenshot: returns (run id, run file path, screenshot path)"""
        run_id, _ = self.write(files={"logs/out.txt": b"kept\n"}, shots={"shot.png": b"\x89PNG kept"})
        run_file = self.root / "runs" / run_id / "files" / "logs" / "out.txt"
        shot = self.root / "screenshots" / sha(b"\x89PNG kept")
        return run_id, run_file, shot

    def test_first_and_chained_runs_print_what_their_manifests_say(self):
        first_id, first_sha = self.write(files={"logs/out.txt": b"one\n", "data.bin": bytes(range(256))},
                                         shots={"title.png": b"\x89PNG first"},
                                         fact=facts(shas={"b": 2, "a": [1, None]}, gpu=None, load=1.5))
        second_id, _ = self.write(files={"logs/out.txt": b"two\n"}, fact=facts(driver="fixture"))

        first = self.report_text(first_id, "--root", str(self.root))
        second = self.report_text(second_id, "--root", str(self.root))

        self.assertEqual(first, expected_report(self.root, first_id))
        self.assertEqual(second, expected_report(self.root, second_id))
        self.assertIn("\nprior none\n", first)
        self.assertIn(f"\nprior {first_id} {first_sha}\n", second)

    def test_output_comes_from_the_record_alone(self):
        source = self.base / "source"
        source.mkdir()
        (source / "out.txt").write_bytes(b"from a source file\n")
        (source / "shot.png").write_bytes(b"\x89PNG bytes")
        run_id, _ = self.write(files={"out.txt": (source / "out.txt").read_bytes()},
                               shots={"shot.png": (source / "shot.png").read_bytes()})
        before = self.report_text(run_id, "--root", str(self.root))

        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.report_text(run_id, "--root", str(self.root), cwd=elsewhere), before)
        self.assertEqual(self.report_text(run_id, "--root", str(self.root), env=BARE_ENV), before)

        shutil.rmtree(source)
        self.assertEqual(self.report_text(run_id, "--root", str(self.root)), before)
        self.assertEqual(before, expected_report(self.root, run_id))

    def test_the_default_root_is_under_the_home_folder(self):
        home = self.base / "home"
        results = home / "hookshot-harness" / "results"
        run_id, _ = record.write_run(results, facts(), {"a.txt": b"home"}, {}, journal=self.journal.append)
        env = {**BARE_ENV, "HOME": str(home), "USERPROFILE": str(home)}
        self.assertEqual(self.report_text(run_id, env=env), expected_report(results, run_id))

    def test_a_changed_run_file_refuses_the_report(self):
        run_id, run_file, _ = self.fixture()
        run_file.write_bytes(b"KEPT\n")
        self.assert_refused(run_id, "--root", str(self.root), names=[str(run_file)])

    def test_a_missing_run_file_refuses_the_report(self):
        run_id, run_file, _ = self.fixture()
        run_file.unlink()
        self.assert_refused(run_id, "--root", str(self.root), names=[str(run_file)])

    def test_a_changed_screenshot_refuses_the_report(self):
        run_id, _, shot = self.fixture()
        shot.write_bytes(b"\x89PNG KEPT")
        self.assert_refused(run_id, "--root", str(self.root), names=[str(shot)])

    def test_a_missing_screenshot_refuses_the_report(self):
        run_id, _, shot = self.fixture()
        shot.unlink()
        self.assert_refused(run_id, "--root", str(self.root), names=[str(shot)])

    def test_an_unknown_id_refuses_the_report(self):
        self.write(files={"a.txt": b"a"})
        self.assert_refused("000009", "--root", str(self.root), names=["000009"])

    def test_a_missing_root_refuses_the_report(self):
        missing = self.base / "no-such-root"
        self.assert_refused("000001", "--root", str(missing), names=[str(missing)])

    def shaped_run(self):
        """A run with one run file; returns its id, and the folder of its manifest"""
        run_id, _ = self.write(files={"a.txt": b"a"}, shots={"shot.png": b"\x89PNG shape"})
        return run_id, self.root / "runs" / run_id

    def manifest_of(self, folder):
        return json.loads((folder / "manifest.json").read_bytes())

    def write_manifest(self, folder, manifest):
        (folder / "manifest.json").write_bytes(json.dumps(manifest).encode("utf-8"))

    def assert_manifest_refused(self, run_id, reason):
        """The report exited 1, printed nothing, and its stderr is one line naming the run and the reason"""
        done = self.run_hx(run_id, "--root", str(self.root))
        stderr = done.stderr.decode("utf-8", "replace")
        self.assertEqual(done.returncode, 1, stderr)
        self.assertEqual(done.stdout, b"")
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(stderr.splitlines(), [f"hx report: run {run_id}: {reason}"])

    def test_a_manifest_that_is_not_an_object_refuses_the_report(self):
        run_id, folder = self.shaped_run()
        self.write_manifest(folder, [1])
        self.assert_manifest_refused(run_id, "its manifest is not a JSON object")

    def test_a_manifest_missing_a_fact_key_refuses_the_report(self):
        run_id, folder = self.shaped_run()
        manifest = self.manifest_of(folder)
        del manifest["facts"]["gpu"]
        self.write_manifest(folder, manifest)
        self.assert_manifest_refused(run_id, "its manifest's facts lack gpu")

    def test_a_manifest_whose_facts_are_not_an_object_refuses_the_report(self):
        run_id, folder = self.shaped_run()
        manifest = self.manifest_of(folder)
        manifest["facts"] = [manifest["facts"]]
        self.write_manifest(folder, manifest)
        self.assert_manifest_refused(run_id, "its manifest's facts are not an object")

    def test_a_manifest_whose_file_entry_lacks_a_hash_or_size_refuses_the_report(self):
        for field, entry in (("sha256", {"size": 1}), ("size", {"sha256": "0" * 64})):
            with self.subTest(missing=field):
                run_id, folder = self.shaped_run()
                manifest = self.manifest_of(folder)
                manifest["files"]["a.txt"] = entry
                self.write_manifest(folder, manifest)
                self.assert_manifest_refused(run_id, "its manifest's files are not sha256 and size entries")

    def test_a_manifest_whose_screenshot_entry_lacks_a_hash_or_size_refuses_the_report(self):
        for field, entry in (("sha256", {"size": 1}), ("size", {"sha256": "0" * 64})):
            with self.subTest(missing=field):
                run_id, folder = self.shaped_run()
                manifest = self.manifest_of(folder)
                manifest["screenshots"]["shot.png"] = entry
                self.write_manifest(folder, manifest)
                self.assert_manifest_refused(run_id, "its manifest's screenshots are not sha256 and size entries")

    def test_a_manifest_whose_prior_is_neither_null_nor_a_run_id_and_hash_refuses_the_report(self):
        for prior in ("000001", {"id": "000001"}, {"sha256": "0" * 64}, ["000001", "0" * 64]):
            with self.subTest(prior=prior):
                run_id, folder = self.shaped_run()
                manifest = self.manifest_of(folder)
                manifest["prior"] = prior
                self.write_manifest(folder, manifest)
                self.assert_manifest_refused(run_id, "its manifest's prior is neither null nor a run id with a sha256")

    def test_a_manifest_missing_its_id_or_written_refuses_the_report(self):
        for field in ("id", "written"):
            with self.subTest(missing=field):
                run_id, folder = self.shaped_run()
                manifest = self.manifest_of(folder)
                del manifest[field]
                self.write_manifest(folder, manifest)
                self.assert_manifest_refused(run_id, f"its manifest has no {field}")

    def test_a_manifest_naming_a_path_the_system_cannot_open_refuses_the_report(self):
        for label, change in (
            ("file name", lambda m: m["files"].__setitem__("a\x00b.txt", m["files"].pop("a.txt"))),
            ("screenshot hash", lambda m: m["screenshots"].__setitem__("shot.png", {"sha256": "a\x00", "size": 1})),
        ):
            with self.subTest(names=label):
                run_id, folder = self.shaped_run()
                manifest = self.manifest_of(folder)
                change(manifest)
                self.write_manifest(folder, manifest)
                done = self.run_hx(run_id, "--root", str(self.root))
                stderr = done.stderr.decode("utf-8", "replace")
                self.assertEqual(done.returncode, 1, stderr)
                self.assertEqual(done.stdout, b"")
                self.assertNotIn("Traceback", stderr)
                lines = stderr.splitlines()
                self.assertEqual(len(lines), 1, stderr)
                self.assertTrue(lines[0].startswith("hx report: "), lines[0])
                self.assertIn("cannot be read", lines[0])

    def test_hx_is_the_frozen_entry_point(self):
        self.assertEqual(sha(HX.read_bytes()), HX_SHA256)


if __name__ == "__main__":
    unittest.main()
