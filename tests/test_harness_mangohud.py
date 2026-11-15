"""scripts/harness/hx_mangohud.py (hx mangohud CSV RUN), as a subprocess, against fixture runs that scripts/harness/record.py
wrote into temporary roots. A MangoHud CSV placed beside a run becomes a new record run: its mangohud.csv is the CSV's
bytes, its mangohud.json names the target run and the CSV's sha256 and size, and its facts are the target's facts. hx
report prints the new run, hx verify finds no problem after it, and the target's folder and the screenshots do not change.
A run id that is not six digits, an absent run, a manifest that cannot be read, a missing or unreadable CSV and an empty
CSV each exit 2 with the cause on stderr, and write nothing. The CSV's source path is not recorded. The journal is a fake
systemd-cat on PATH, so the user's journal is not written; no GPU, game or host is used.

python -I -B tests/test_harness_mangohud.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
HX = HARNESS / "hx.py"
sys.path.insert(0, str(HARNESS))
import hx_report
import hx_verify
import record

# The fake journal: each message on stdin is appended to $FAKE_JOURNAL as one line, as systemd-cat would journal it
SYSTEMD_CAT = '#!/bin/sh\nprintf \'%s\\n\' "$(cat)" >> "$FAKE_JOURNAL"\n'
RUN_ALL_SKIP = {"windows": "the fake systemd-cat is a POSIX shell script on PATH: the journal is systemd's"}

# A synthetic MangoHud log: made-up numbers, no game and no host
CSV = b"fps,frametime,cpu_load\n60,16.6,12\n61,16.4,11\n"


def facts(**changes):
    """A facts dict with every FACT_KEYS key; values are stored verbatim, so nested values are used too"""
    f = {key: {"value": key} for key in record.FACT_KEYS}
    f.update(changes)
    return f


def entry(data):
    return {"sha256": record.sha256_bytes(data), "size": len(data)}


def tree(folder):
    """Every file under a folder, by its path relative to the folder, with its bytes"""
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def facts_block(text):
    """The facts lines of a report: from its 'facts' line up to its 'files' line"""
    lines = text.splitlines()
    return lines[lines.index("facts") + 1:lines.index("files")]


class MangohudTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "record"
        self.journal = self.base / "journal.txt"
        self.journal.write_text("", encoding="utf-8")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        shim = self.bin / "systemd-cat"
        shim.write_text(SYSTEMD_CAT, encoding="utf-8")
        shim.chmod(0o755)
        self.source = self.base / "captures"
        self.source.mkdir()
        self.csv = self.source / "fixture-run.csv"
        self.csv.write_bytes(CSV)

    def append_journal(self, message):
        """The journal the fixture runs are anchored in: the same file the fake systemd-cat appends to"""
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(message + "\n")

    def write(self, files=None, shots=None, fact=None):
        """write_run into the test's root, anchored in the test's journal"""
        return record.write_run(self.root, facts() if fact is None else fact, files or {}, shots or {},
                                journal=self.append_journal)

    def record_runs(self):
        """Two runs, the second chained to the first: returns the first run's id and its facts"""
        target_facts = facts(gpu={"name": "Example GPU", "index": 0}, load={"1min": 0.25, "5min": 0.5, "15min": 1.0},
                             shas={"hookshot": "3f9c2a1b7d4e6f80a1b2c3d4e5f60718293a4b5c"})
        target, _ = self.write(files={"logs/out.txt": b"kept\n"}, shots={"shot.png": b"\x89PNG kept"},
                               fact=target_facts)
        self.write(files={"logs/other.txt": b"second\n"})
        return target, target_facts

    def manifest_of(self, run_id, root=None):
        manifest, _ = record.read_manifest(self.root if root is None else root, run_id)
        return manifest

    def run_hx(self, *args, home=None):
        """hx mangohud with the fake journal first on PATH; HOME is given only when the default root is under test"""
        env = {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""), "FAKE_JOURNAL": str(self.journal)}
        if home is not None:
            env["HOME"] = str(home)
        return subprocess.run([sys.executable, "-I", "-B", str(HX), "mangohud", *args], capture_output=True, env=env)

    def journal_pairs(self):
        return record.parse_journal(self.journal.read_text(encoding="utf-8"))

    def placed(self, target, *extra):
        """The command placing the CSV beside the target, which must exit 0: returns the new run's id"""
        done = self.run_hx(str(self.csv), target, "--root", str(self.root), *extra)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stderr, b"")
        return done.stdout.decode("utf-8").removeprefix("run: ").removesuffix("\n")

    def assert_refused(self, cause, *args, home=None):
        """The command exits 2, prints nothing on stdout, and names the cause on stderr; the record and the journal are as
        they were, so nothing was written"""
        before_tree = tree(self.root)
        before_ids = record.run_ids(self.root)
        before_journal = self.journal.read_bytes()
        done = self.run_hx(*args, home=home)
        stderr = done.stderr.decode("utf-8", "replace")
        self.assertEqual(done.returncode, 2, stderr)
        self.assertEqual(done.stdout, b"")
        self.assertNotIn("Traceback", stderr)
        self.assertIn(cause, stderr)
        self.assertEqual(record.run_ids(self.root), before_ids)
        self.assertEqual(tree(self.root), before_tree)
        self.assertEqual(self.journal.read_bytes(), before_journal)

    def test_the_csv_lands_in_a_new_run_that_names_the_target(self):
        target, _ = self.record_runs()
        before = record.run_ids(self.root)
        done = self.run_hx(str(self.csv), target, "--root", str(self.root))
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.stderr, b"")
        new = f"{int(before[-1]) + 1:06d}"
        self.assertEqual(done.stdout, f"run: {new}\n".encode("utf-8"))
        self.assertEqual(record.run_ids(self.root), before + [new])

        folder = self.root / "runs" / new / "files"
        self.assertEqual((folder / "mangohud.csv").read_bytes(), CSV)
        summary = (folder / "mangohud.json").read_bytes()
        self.assertEqual(json.loads(summary),
                         {"kind": "mangohud", "target": target, "csv": entry(CSV)})

        manifest = self.manifest_of(new)
        self.assertEqual(manifest["files"], {"mangohud.csv": entry(CSV), "mangohud.json": entry(summary)})
        self.assertEqual(manifest["facts"], self.manifest_of(target)["facts"])
        _, prior_sha = record.read_manifest(self.root, before[-1])
        self.assertEqual(manifest["prior"], {"id": before[-1], "sha256": prior_sha})

    def test_the_new_run_reads_back_through_report_and_verify(self):
        target, _ = self.record_runs()
        new = self.placed(target)

        text = hx_report.render(self.root, new)
        self.assertTrue(text.startswith(f"run {new}\n"))
        self.assertIn(f"  {entry(CSV)['sha256']} {len(CSV)} mangohud.csv\n", text)
        self.assertEqual(facts_block(text), facts_block(hx_report.render(self.root, target)))

        problems, count = hx_verify.check(self.root, self.journal_pairs())
        self.assertEqual(problems, [])
        self.assertEqual(count, 3)

    def test_the_target_folder_and_the_screenshots_are_unchanged(self):
        target, _ = self.record_runs()
        target_folder = self.root / "runs" / target
        target_before = tree(target_folder)
        shots_before = tree(self.root / "screenshots")

        self.placed(target)

        self.assertEqual(tree(target_folder), target_before)
        self.assertEqual(tree(self.root / "screenshots"), shots_before)

    def test_the_default_root_is_the_home_record(self):
        home = self.base / "home"
        results = home / "hookshot-harness" / "results"
        target, _ = record.write_run(results, facts(), {"a.txt": b"home"}, {}, journal=self.append_journal)

        done = self.run_hx(str(self.csv), target, home=home)

        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        new = f"{int(target) + 1:06d}"
        self.assertEqual(done.stdout, f"run: {new}\n".encode("utf-8"))
        self.assertEqual((results / "runs" / new / "files" / "mangohud.csv").read_bytes(), CSV)
        self.assertFalse(self.root.exists())

    def test_the_csv_source_path_is_not_recorded(self):
        target, _ = self.record_runs()
        new = self.placed(target)

        written = b"".join(tree(self.root / "runs" / new).values())
        self.assertNotIn(str(self.source).encode("utf-8"), written)
        self.assertNotIn(b"fixture-run", written)

    def test_a_run_id_that_is_not_six_digits_is_refused(self):
        target, _ = self.record_runs()
        for bad in ("12345", "1234567", "00000a", f"../{target}"):
            with self.subTest(run=bad):
                self.assert_refused("not a run id", str(self.csv), bad, "--root", str(self.root))

    def test_an_absent_run_is_refused(self):
        self.record_runs()
        self.assert_refused("run 000009: cannot read its manifest", str(self.csv), "000009", "--root", str(self.root))

    def test_a_manifest_that_cannot_be_read_is_refused(self):
        target, _ = self.record_runs()
        manifest = self.root / "runs" / target / "manifest.json"
        original = manifest.read_bytes()
        good = json.loads(original)
        without_facts = {key: value for key, value in good.items() if key != "facts"}
        without_gpu = {**good, "facts": {key: value for key, value in good["facts"].items() if key != "gpu"}}
        cases = (
            ("missing", None, f"run {target}: cannot read its manifest"),
            ("not JSON", b"{not json", f"run {target}: its manifest is not JSON"),
            ("not an object", b"[1]", f"run {target}: its manifest has no facts object"),
            ("no facts", json.dumps(without_facts).encode("utf-8"), f"run {target}: its manifest has no facts object"),
            ("facts not an object", json.dumps({**good, "facts": [good["facts"]]}).encode("utf-8"),
             f"run {target}: its manifest has no facts object"),
            ("a fact missing", json.dumps(without_gpu).encode("utf-8"), "facts lack gpu"),
        )
        try:
            for label, data, cause in cases:
                with self.subTest(manifest=label):
                    if data is None:
                        manifest.unlink()
                    else:
                        manifest.write_bytes(data)
                    self.assert_refused(cause, str(self.csv), target, "--root", str(self.root))
        finally:
            manifest.write_bytes(original)

    def test_a_missing_csv_is_refused(self):
        target, _ = self.record_runs()
        missing = self.source / "absent.csv"
        self.assert_refused(f"{missing} cannot be read", str(missing), target, "--root", str(self.root))

    def test_a_csv_that_cannot_be_read_is_refused(self):
        target, _ = self.record_runs()
        self.assert_refused(f"{self.source} cannot be read", str(self.source), target, "--root", str(self.root))

    def test_an_empty_csv_is_refused(self):
        target, _ = self.record_runs()
        empty = self.source / "empty.csv"
        empty.write_bytes(b"")
        self.assert_refused(f"{empty} is empty", str(empty), target, "--root", str(self.root))


if __name__ == "__main__":
    unittest.main()
