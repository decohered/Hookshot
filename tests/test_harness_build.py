"""scripts/harness/hx_build.py through hx.py build. A small git repository in a temporary folder holds copies of the harness
scripts, scripts/hookshot_ctl.py and a stand-in nx2/make_game.py that writes the two binaries and the log. hx.py build runs
in it with HX_CONFIG naming a test config. Fakes on PATH stand in for ssh (it runs the script locally), nix and nix-store
(they record their arguments), devenv (it makes the .devenv/gc/shell link), systemd-run and systemctl (systemd-run runs the
unit's command to its end, as the jobs test does), and the enter-closure script drops its first argument.
Checks: the closure is resolved, refused outside the store, copied and pinned; the SHA is the revision's full id and its tree
is the one built; make_game runs through the closure entry as one job with its environment; the record (stdout and
build.txt) holds the closure, the SHA, the dirty diff and both hashes, and the hashes are the files'; a clean checkout says
none; a dirty one gives the diff's sha256 and the diff; a successful build prints only its record, with make_game's console
output in console.log on the host and devenv's output shown only when devenv shell fails; a failed build prints its status,
the console.log path and its last 20 lines, and returns the status; an unknown rev returns 2 before
anything reaches the host; a refused start returns 1; a lost ssh returns 255; a failed copy returns 1; the dirty diff is the
one from before the build, even when the checkout changes during it; a build folder keeps the record of the build that last
succeeded there, or none. Nothing reaches a real host.

python -I -B tests/test_harness_build.py
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RUN_ALL_SKIP = {"windows": "the fake ssh, nix, nix-store, devenv, systemd-run and systemctl are POSIX shims, and the "
                           "enter-closure script and the job run under sh and bash"}

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"

HOST = "example-host"
CLOSURE = "/nix/store/7mkzq3d0ghs1c5lgh1f9m0nvq8bx0c6a-devenv-shell"
JOB = re.compile(r"build-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
STAND_IN_HOOKSHOT = b"stand-in executable\n"
STAND_IN_RENDERER = b"stand-in renderer\n"
FAKE_NAMES = ("ssh", "nix", "nix-store", "devenv", "systemd-run", "systemctl")

# The fakes, in one Python file that the shims run as: python -I fakes.py <tool> <arguments>. Every call is recorded as a
# JSON list in $FAKE_LOG. ssh takes what remote.command builds and runs its command word with sh, as a login shell would.
# devenv prints a line to each stream, and with $FAKE_DEVENV_FAIL set it returns 1 before it makes .devenv/gc/shell, a link
# to $FAKE_CLOSURE; with $FAKE_DEVENV_EDIT set it first rewrites that file. nix fails when $FAKE_NIX_FAIL is set.
# systemd-run takes the options the start script passes and runs the unit's command to its end, in the foreground, with
# its --setenv variables, its --working-directory and its append: outputs; then it returns 0. systemctl lists no units and
# answers is-active inactive. ssh loses the connection on the follow script when $FAKE_SSH_LOSE is set.
FAKE_TOOLS = r'''
import json
import os
import subprocess
import sys


def record(tool, args):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps([tool] + args) + "\n")


def fail(message):
    sys.exit("fake %s: %s" % (sys.argv[1], message))


def ssh(args):
    record("ssh", args)
    if os.environ.get("FAKE_SSH_LOSE") and "is-active" in args[-1]:
        print("ssh: connection lost", file=sys.stderr)
        return 255
    if args[:3] != ["-o", "BatchMode=yes", "example-host"] or len(args) != 4:
        fail("want -o BatchMode=yes example-host and one command word, got %r" % (args,))
    return subprocess.run(["sh", "-c", args[3]]).returncode


def nix(args):
    record("nix", args)
    if os.environ.get("FAKE_NIX_FAIL"):
        fail("copy refused")
    return 0


def nix_store(args):
    record("nix-store", args)
    return 0


def devenv(args):
    record("devenv", args)
    print("devenv: stand-in output")
    print("devenv: stand-in warning", file=sys.stderr)
    if os.environ.get("FAKE_DEVENV_FAIL"):
        return 1
    if os.environ.get("FAKE_DEVENV_EDIT"):
        with open(os.environ["FAKE_DEVENV_EDIT"], "w", encoding="utf-8") as f:
            f.write("edited during the closure evaluation\n")
    gc = os.path.join(".devenv", "gc")
    os.makedirs(gc, exist_ok=True)
    link = os.path.join(gc, "shell")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(os.environ["FAKE_CLOSURE"], link)
    return 0


def append_path(value):
    kind, _, path = value.partition(":")
    if kind != "append":
        fail("only append: is faked, not %r" % value)
    return path


def systemd_run(args):
    record("systemd-run", args)
    env = dict(os.environ)
    cwd = None
    props = {}
    i = 0
    while i < len(args) and args[i].startswith("--"):
        key, eq, value = args[i].partition("=")
        if key in ("--user", "--collect") and not eq:
            pass
        elif eq and key == "--unit":
            pass
        elif eq and key == "--working-directory":
            cwd = value
        elif eq and key == "--setenv":
            name, _, text = value.partition("=")
            env[name] = text
        elif eq and key == "--property":
            name, _, text = value.partition("=")
            props[name] = text
        else:
            fail("option not faked: %s" % args[i])
        i += 1
    command = args[i:]
    if cwd is None or not command:
        fail("want --working-directory= and a command")
    log = append_path(props["StandardOutput"])
    if append_path(props["StandardError"]) != log:
        fail("stdout and stderr must share one file")
    with open(log, "ab") as out:
        subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    return 0


def systemctl(args):
    record("systemctl", args)
    if args[:1] != ["--user"] or len(args) < 2:
        fail("want --user and a verb")
    if args[1] == "list-units":
        return 0
    if args[1] == "is-active":
        return 3
    fail("verb not faked: %s" % args[1])


tools = {"ssh": ssh, "nix": nix, "nix-store": nix_store, "devenv": devenv, "systemd-run": systemd_run,
         "systemctl": systemctl}
sys.exit(tools[sys.argv[1]](sys.argv[2:]))
'''

# The stand-in for nx2/make_game.py: it writes the two binaries and the log, and exits with $HX_STANDIN_STATUS. With
# $HX_STANDIN_EDIT set, it also rewrites that file, as an edit to the checkout while the build runs. As make_game's console
# output it prints a marker on each stream, then 25 numbered lines on stdout.
MAKE_GAME = r'''"""A stand-in for nx2/make_game.py: it writes the two binaries and the log, and exits with HX_STANDIN_STATUS."""
import argparse
import os
import pathlib
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--nup")
parser.add_argument("--out", required=True)
parser.add_argument("--log", required=True)
args = parser.parse_args()
print("stand-in stdout marker", flush=True)
print("stand-in stderr marker", file=sys.stderr, flush=True)
for n in range(1, 26):
    print("stand-in line %02d" % n, flush=True)
out = pathlib.Path(args.out)
out.mkdir(parents=True, exist_ok=True)
(out / "hookshot").write_bytes(b"stand-in executable\n")
(out / "hookshot_vk.so").write_bytes(b"stand-in renderer\n")
pathlib.Path(args.log).write_text("stand-in build\n", encoding="utf-8")
if os.environ.get("HX_STANDIN_EDIT"):
    pathlib.Path(os.environ["HX_STANDIN_EDIT"]).write_text("edited during the build\n", encoding="utf-8")
sys.exit(int(os.environ.get("HX_STANDIN_STATUS", "0")))
'''

TOML = '''host = "%s"
hold = "%s"
root = "%s"
dump = "%s"
enter_closure = "%s"

[devices.gpu]
expect = "example GPU"
'''


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        fakes = tmp / "fakes.py"
        fakes.write_text(FAKE_TOOLS, encoding="utf-8")
        for name in FAKE_NAMES:
            self.shim(bin_dir / name, "exec %s -I %s %s \"$@\"" % (shlex.quote(sys.executable), shlex.quote(str(fakes)), name))
        self.shim(bin_dir / "python", "exec %s \"$@\"" % shlex.quote(sys.executable))
        self.log = tmp / "calls.jsonl"
        self.enter = tmp / "enter-closure"
        self.shim(self.enter, "shift\nexec \"$@\"")
        self.dump = tmp / "dump.nup"
        self.hold = tmp / "hold"
        self.root = tmp / "harness root's"
        self.checkout = tmp / "checkout"
        conf = tmp / "harness.toml"
        conf.write_text(TOML % (HOST, self.hold, self.root, self.dump, self.enter), encoding="utf-8")
        env = mock.patch.dict(os.environ, {
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_LOG": str(self.log),
            "FAKE_CLOSURE": CLOSURE,
            "HX_CONFIG": str(conf),
        })
        env.start()
        self.addCleanup(env.stop)
        for var in ("FAKE_NIX_FAIL", "FAKE_SSH_LOSE", "FAKE_DEVENV_FAIL", "FAKE_DEVENV_EDIT", "HX_STANDIN_STATUS",
                    "HX_STANDIN_EDIT"):
            os.environ.pop(var, None)
        self.make_checkout()

    def shim(self, path, body):
        path.write_text("#!/bin/sh\n%s\n" % body, encoding="utf-8")
        path.chmod(0o755)

    def make_checkout(self):
        self.checkout.mkdir()
        for path in sorted(HARNESS.glob("*.py")) + [ROOT / "scripts" / "hookshot_ctl.py"]:
            dest = self.checkout / path.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        make_game = self.checkout / "nx2" / "make_game.py"
        make_game.parent.mkdir()
        make_game.write_text(MAKE_GAME, encoding="utf-8")
        self.write_notes("first\n")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")

    def write_notes(self, text):
        (self.checkout / "notes.txt").write_text(text, encoding="utf-8")

    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=harness test", "-c", "user.email=test@example.invalid",
                               "-c", "commit.gpgsign=false", *args],
                              cwd=str(self.checkout), check=True, capture_output=True, encoding="utf-8").stdout

    def run_build(self, *args):
        hx = self.checkout / "scripts" / "harness" / "hx.py"
        return subprocess.run([sys.executable, "-I", "-B", str(hx), "build", *args], cwd=str(self.checkout),
                              capture_output=True)

    def assertExit(self, proc, status):
        self.assertEqual(proc.returncode, status, proc.stderr.decode("utf-8", "replace"))

    def calls(self, tool):
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [entry[1:] for entry in entries if entry[0] == tool]

    def only_job(self):
        runs = self.calls("systemd-run")
        self.assertEqual(len(runs), 1)
        (unit,) = [a for a in runs[0] if a.startswith("--unit=")]
        job = unit[len("--unit=hx-"):-len(".service")]
        self.assertRegex(job, JOB)
        return job

    def build_dir(self, sha):
        return self.root / "builds" / sha

    def record_text(self, sha, job, dirty):
        fields = [("closure", CLOSURE), ("sha", sha), ("dirty", dirty),
                  ("hookshot", sha256(STAND_IN_HOOKSHOT)), ("hookshot_vk.so", sha256(STAND_IN_RENDERER)),
                  ("build", str(self.build_dir(sha))), ("job", job)]
        return "".join("%s: %s\n" % field for field in fields)

    def assert_record(self, proc, sha, diff=b""):
        job = self.only_job()
        dirty = sha256(diff) if diff else "none"
        head = self.record_text(sha, job, dirty).encode("utf-8")
        self.assertEqual(proc.stdout, head + diff)
        self.assertEqual(proc.stderr, b"")
        self.assertEqual((self.build_dir(sha) / "build.txt").read_bytes(), head)
        self.assertEqual((self.build_dir(sha) / "dirty.diff").exists(), bool(diff))
        if diff:
            self.assertEqual((self.build_dir(sha) / "dirty.diff").read_bytes(), diff)

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def working_diff(self):
        return subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=str(self.checkout), capture_output=True,
                              check=True).stdout

    # R1: hx build <rev> copies the closure, builds in it, and prints the record

    def test_r1_the_closure_is_copied_to_the_host_and_pinned_there(self):
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        self.assertEqual(self.calls("devenv"), [["shell", "--", "true"]])
        self.assertEqual(self.calls("nix"), [["copy", "--to", "ssh://" + HOST, CLOSURE]])
        gc_root = str(self.root / "gc" / os.path.basename(CLOSURE))
        self.assertEqual(self.calls("nix-store"), [["--add-root", gc_root, "--realise", CLOSURE]])

    def test_r1_the_build_runs_make_game_through_the_closure_entry_as_one_job(self):
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        sha = self.head()
        job = self.only_job()
        (run,) = self.calls("systemd-run")
        build = self.build_dir(sha)
        self.assertIn("--working-directory=%s" % (self.root / "jobs" / job), run)
        self.assertIn("--setenv=XDG_CACHE_HOME=%s" % (self.root / "cache"), run)
        self.assertIn("--setenv=HOOKSHOT_NO_UPDATE_CHECK=1", run)
        argv = [str(self.enter), CLOSURE, "python", "-I", str(build / "src" / "nx2" / "make_game.py"),
                "--nup", str(self.dump), "--out", str(build / "game"), "--log", str(build / "build.log")]
        self.assertEqual(run[-len(argv):], argv)
        self.assertEqual((build / "game" / "hookshot").read_bytes(), STAND_IN_HOOKSHOT)
        self.assertEqual((build / "game" / "hookshot_vk.so").read_bytes(), STAND_IN_RENDERER)
        self.assertTrue((build / "build.log").is_file())

    def test_r1_the_revision_resolves_to_its_full_sha_and_its_own_tree_is_built(self):
        self.write_notes("second\n")
        self.git("commit", "-q", "-am", "second")
        first = self.git("rev-parse", "HEAD~1").strip()
        proc = self.run_build("HEAD~1")
        self.assertExit(proc, 0)
        self.assertEqual((self.build_dir(first) / "src" / "notes.txt").read_text(encoding="utf-8"), "first\n")
        self.assert_record(proc, first)

    def test_r1_a_clean_checkout_prints_the_record_with_dirty_none_and_keeps_it_beside_the_build(self):
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        self.assert_record(proc, self.head())

    def test_r1_a_dirty_checkout_prints_the_diffs_sha256_and_the_diff_and_keeps_the_diff(self):
        self.write_notes("changed\n")
        diff = self.working_diff()
        self.assertIn(b"+changed", diff)
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        self.assert_record(proc, self.head(), diff=diff)

    # B1: the dirty diff is the one the closure was evaluated from, read before the build starts

    def test_b1_an_edit_during_the_build_does_not_change_the_recorded_diff(self):
        self.write_notes("changed\n")
        diff = self.working_diff()
        os.environ["HX_STANDIN_EDIT"] = str(self.checkout / "notes.txt")
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        self.assertEqual((self.checkout / "notes.txt").read_text(encoding="utf-8"), "edited during the build\n")
        self.assert_record(proc, self.head(), diff=diff)

    # B2: a folder holds the record of the build that last succeeded there, or none

    def test_b1_a_checkout_edited_during_the_closure_evaluation_is_refused_before_anything_reaches_the_host(self):
        for label, before in (("dirty", "changed\n"), ("clean", "first\n")):
            with self.subTest(checkout=label):
                self.write_notes(before)
                self.log.unlink(missing_ok=True)
                os.environ["FAKE_DEVENV_EDIT"] = str(self.checkout / "notes.txt")
                proc = self.run_build("HEAD")
                self.assertExit(proc, 1)
                self.assertIn(b"changed while", proc.stderr)
                for tool in ("nix", "nix-store", "ssh", "systemd-run"):
                    self.assertEqual(self.calls(tool), [], tool)
                self.assertFalse(self.build_dir(self.head()).exists())
                os.environ.pop("FAKE_DEVENV_EDIT")

    def test_b2_a_clean_rebuild_of_a_dirty_sha_leaves_no_earlier_diff(self):
        self.write_notes("changed\n")
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        self.assertTrue((self.build_dir(self.head()) / "dirty.diff").is_file())
        self.write_notes("first\n")
        self.log.unlink(missing_ok=True)
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        self.assert_record(proc, self.head())

    def test_b2_a_failing_rebuild_after_a_success_leaves_no_earlier_record(self):
        self.write_notes("changed\n")
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        build = self.build_dir(self.head())
        self.assertTrue((build / "build.txt").is_file())
        self.assertTrue((build / "dirty.diff").is_file())
        self.log.unlink(missing_ok=True)
        os.environ["HX_STANDIN_STATUS"] = "3"
        proc = self.run_build("HEAD")
        self.assertExit(proc, 3)
        self.assertFalse((build / "build.txt").exists())
        self.assertFalse((build / "dirty.diff").exists())

    def test_b2_a_refused_rebuild_leaves_the_earlier_record_in_place(self):
        self.write_notes("changed\n")
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        build = self.build_dir(self.head())
        kept = {name: (build / name).read_bytes() for name in ("build.txt", "dirty.diff")}
        self.hold.touch()
        self.log.unlink(missing_ok=True)
        proc = self.run_build("HEAD")
        self.assertExit(proc, 1)
        self.assertIn(b"held", proc.stderr)
        self.assertEqual(self.calls("systemd-run"), [])
        for name, data in kept.items():
            self.assertEqual((build / name).read_bytes(), data, name)

    def test_r1_the_latest_link_points_at_the_build(self):
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        self.assertEqual(os.readlink(self.root / "builds" / "latest"), str(self.build_dir(self.head())))

    def test_a_failed_build_prints_its_status_and_its_log_and_returns_it(self):
        os.environ["HX_STANDIN_STATUS"] = "3"
        proc = self.run_build("HEAD")
        self.assertExit(proc, 3)
        build = self.build_dir(self.head())
        self.assertIn(("build failed: exit status 3; log: %s" % (build / "console.log")).encode("utf-8"), proc.stdout)
        self.assertFalse((build / "build.txt").exists())

    # DL58: make_game's console output stays in console.log on the host; hx build prints only the record

    def test_dl58_make_games_console_output_is_in_console_log_on_the_host_and_not_in_the_output(self):
        proc = self.run_build("HEAD")
        self.assertExit(proc, 0)
        self.assert_record(proc, self.head())
        console = (self.build_dir(self.head()) / "console.log").read_text(encoding="utf-8")
        self.assertIn("stand-in stdout marker\n", console)
        self.assertIn("stand-in stderr marker\n", console)
        self.assertIn("stand-in line 25\n", console)

    def test_dl58_devenv_output_is_printed_only_when_devenv_shell_fails(self):
        os.environ["FAKE_DEVENV_FAIL"] = "1"
        proc = self.run_build("HEAD")
        self.assertExit(proc, 1)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(b"devenv: stand-in output\n", proc.stderr)
        self.assertIn(b"devenv: stand-in warning\n", proc.stderr)
        self.assertIn(b"devenv shell -- true failed with status 1", proc.stderr)
        self.assertEqual(self.calls("nix"), [])

    def test_dl58_a_failed_build_prints_the_console_log_path_and_its_last_20_lines(self):
        os.environ["HX_STANDIN_STATUS"] = "3"
        proc = self.run_build("HEAD")
        self.assertExit(proc, 3)
        console = self.build_dir(self.head()) / "console.log"
        lines = console.read_text(encoding="utf-8").splitlines()
        self.assertGreater(len(lines), 20)
        self.assertEqual(proc.stdout.decode("utf-8").splitlines(),
                         ["build failed: exit status 3; log: %s" % console] + lines[-20:])

    def test_an_unknown_rev_returns_2_before_anything_reaches_the_host(self):
        proc = self.run_build("no-such-revision")
        self.assertExit(proc, 2)
        self.assertIn(b"no-such-revision", proc.stderr)
        for tool in ("devenv", "ssh", "nix", "nix-store", "systemd-run"):
            self.assertEqual(self.calls(tool), [], tool)

    def test_no_rev_is_a_usage_error_returning_2(self):
        proc = self.run_build()
        self.assertExit(proc, 2)
        self.assertIn(b"usage", proc.stderr)

    def test_a_refused_start_prints_the_reason_and_returns_1(self):
        self.hold.touch()
        proc = self.run_build("HEAD")
        self.assertExit(proc, 1)
        self.assertIn(b"held", proc.stderr)
        self.assertIn(str(self.hold).encode("utf-8"), proc.stderr)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_a_lost_ssh_returns_255_and_says_the_build_goes_on(self):
        os.environ["FAKE_SSH_LOSE"] = "1"
        proc = self.run_build("HEAD")
        self.assertExit(proc, 255)
        job = self.only_job()
        self.assertIn(("ssh lost: the build goes on as hx-%s.service" % job).encode("utf-8"), proc.stdout)
        self.assertFalse((self.build_dir(self.head()) / "build.txt").exists())

    def test_a_closure_outside_the_store_is_refused_before_anything_reaches_the_host(self):
        for bad in ("/tmp/elsewhere-devenv-shell", "/nix/store-elsewhere/x-devenv-shell", "/nix/store/../etc/x"):
            with self.subTest(closure=bad):
                self.log.unlink(missing_ok=True)
                os.environ["FAKE_CLOSURE"] = bad
                proc = self.run_build("HEAD")
                self.assertExit(proc, 1)
                self.assertIn(b"/nix/store/", proc.stderr)
                for tool in ("ssh", "nix", "nix-store", "systemd-run"):
                    self.assertEqual(self.calls(tool), [], tool)

    def test_a_failed_closure_copy_returns_1_and_starts_nothing(self):
        os.environ["FAKE_NIX_FAIL"] = "1"
        proc = self.run_build("HEAD")
        self.assertExit(proc, 1)
        self.assertIn(b"copy refused", proc.stderr)
        self.assertEqual(self.calls("nix-store"), [])
        self.assertEqual(self.calls("systemd-run"), [])


if __name__ == "__main__":
    unittest.main()
