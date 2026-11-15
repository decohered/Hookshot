"""scripts/harness/jobs.py through the real start script. A fake ssh runs the script under bash, and a fake systemd-run and
systemctl stand in for the user manager: systemd-run runs the unit's command to its end, in the foreground, with its
environment and its output in output.log, and systemctl answers from files the test writes. Checks: the unit's arguments
and that the argv runs with its environment, its output and its status (R2); the hold refused before the folder is made
and when it appears while the folder is made, and wait_hold (R3); a build refused beside a build or a bench, a bench
refused beside any other job, and a check beside a check or a build (R4); separate folders, saves and game copies for
two jobs of one build (R5); a start waiting for the
start lock; follow's output, status and a lost ssh; and the job id, the quoting and an empty command. The job's end
(DL60, DL181): its game and shots folders removed at any depth, its data kept, its exit file written after those, its
exclusive size kept as its kind's last size (its game size when it copied a game; every kind, a size btrfs cannot
measure left as it was), a failed removal reported in its output with its status kept, a link out of the job folder
not followed, and a start under twice that size refused with its own status, under the start lock and before its
folder is made. A job stopped by SIGTERM removes the same folders, records no size and exits 143. hx smoke
(SmokeTests): its refusals, the job it starts (argv, game folder, device env) and the verdict of the in-job half against
a stand-in game (R6), and its hold (R3). Nothing reaches a real host or a systemd user manager.

python -I -B tests/test_harness_jobs.py
"""
import fcntl
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

RUN_ALL_SKIP = {"windows": "the fake ssh, systemd-run and systemctl are POSIX shell and Python scripts, and the start "
                           "script runs under bash"}

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
sys.path.insert(0, str(HARNESS))
import config
import jobs
import remote

HOST = "example-host"

# A stand-in for ssh: it takes exactly what remote.command builds (-o BatchMode=yes, the host, one command word), then
# runs that word the way a remote login shell does (sh -c here), so the start script runs under bash.
FAKE_SSH = """#!/bin/sh
if [ -n "$FAKE_SSH_REFUSE" ]; then
  echo "ssh: connect to host example-host port 22: Connection refused" >&2
  exit 255
fi
if [ $# -ne 4 ] || [ "$1" != "-o" ] || [ "$2" != "BatchMode=yes" ] || [ "$3" != "example-host" ]; then
  echo "fake ssh: want -o BatchMode=yes example-host <one command word>, got: $*" >&2
  exit 2
fi
exec sh -c "$4"
"""

# systemd-run and systemctl, in one Python file that the shims run as: python -I fakes.py <tool> <arguments>. Every call
# is recorded as a JSON list in $FAKE_LOG. systemd-run takes the options the start script passes (--user, --collect, and
# --name=value) and refuses any other. It runs the unit's command to its end, in the foreground, with its --setenv
# variables, its --working-directory and its --property outputs (append: files), then returns 0, as the real one returns
# once the unit has started. systemctl list-units prints $FAKE_UNITS. is-active answers one step of $FAKE_STEPS per call:
# "inactive" (exit 3), or "active:TEXT", which appends TEXT and a newline to $FAKE_GROW and answers active (exit 0).
# btrfs filesystem du -s --raw prints the Exclusive bytes $FAKE_BTRFS_EXCL, with a Total 4096 more (or fails when
# $FAKE_BTRFS_FAIL is set); df --output=avail -B1 prints $FAKE_AVAIL; rm -rf -- PATH records, with the call, whether the
# job's exit file exists yet (the one beside PATH, or the one in $FAKE_JOB when that is set: a folder nested in the job
# has none beside it) and the text of PATH's marker file (when it has one), then removes PATH (or fails when
# $FAKE_RM_FAIL is set, removing nothing).
FAKE_TOOLS = r'''
import json
import os
import shutil
import subprocess
import sys


def record(tool, args):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps([tool] + args) + "\n")


def fail(message):
    sys.exit("fake %s: %s" % (sys.argv[1], message))


def append_target(value):
    kind, _, path = value.partition(":")
    if kind != "append":
        fail("only append: is faked, not %r" % value)
    return open(path, "ab")


def systemd_run(args):
    record("systemd-run", args)
    env = dict(os.environ)
    cwd = None
    unit = None
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
            fail("option not faked: %s" % args[i])
        i += 1
    command = args[i:]
    if unit is None or cwd is None or not command:
        fail("want --unit=, --working-directory= and a command")
    out = append_target(props["StandardOutput"])
    err = append_target(props["StandardError"])
    with out, err:
        subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
    return 0


def next_step():
    path = os.environ["FAKE_STEPS"]
    with open(path, encoding="utf-8") as f:
        steps = f.read().splitlines()
    if not steps:
        return "inactive"
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(step + "\n" for step in steps[1:]))
    return steps[0]


def systemctl(args):
    record("systemctl", args)
    if len(args) < 2 or args[0] != "--user":
        fail("want --user and a verb")
    verb, rest = args[1], args[2:]
    if verb == "list-units":
        if os.environ.get("FAKE_HOLD_ON_LIST"):
            open(os.environ["FAKE_HOLD_ON_LIST"], "w").close()
        with open(os.environ["FAKE_UNITS"], encoding="utf-8") as f:
            sys.stdout.write(f.read())
        return 0
    if verb == "is-active":
        quiet = "--quiet" in rest
        step = next_step()
        if step.startswith("active:"):
            with open(os.environ["FAKE_GROW"], "a", encoding="utf-8") as f:
                f.write(step[len("active:"):] + "\n")
            if not quiet:
                print("active")
            return 0
        if not quiet:
            print("inactive")
        return 3
    fail("verb not faked: %s" % verb)


def btrfs(args):
    record("btrfs", args)
    if os.environ.get("FAKE_BTRFS_FAIL"):
        fail("ERROR: not a btrfs filesystem")
    if len(args) != 5 or args[:4] != ["filesystem", "du", "-s", "--raw"]:
        fail("want filesystem du -s --raw <path>, got: %s" % " ".join(args))
    exclusive = int(os.environ["FAKE_BTRFS_EXCL"])
    print("     Total   Exclusive  Set shared  Filename")
    print("%12d %12d %12d  %s" % (exclusive + 4096, exclusive, 4096, args[4]))
    return 0


def df(args):
    record("df", args)
    if len(args) != 3 or args[:2] != ["--output=avail", "-B1"]:
        fail("want --output=avail -B1 <path>, got: %s" % " ".join(args))
    print("     Avail")
    print(os.environ["FAKE_AVAIL"])
    return 0


def rm(args):
    if len(args) != 3 or args[:2] != ["-rf", "--"]:
        fail("want rm -rf -- <path>, got: %s" % " ".join(args))
    path = args[2]
    exit_file = os.path.join(os.environ.get("FAKE_JOB", os.path.dirname(path)), "exit")
    marker = os.path.join(path, "marker")
    extra = []
    if os.path.isfile(marker):
        with open(marker, encoding="utf-8") as f:
            extra.append("marker=%s" % f.read().strip())
    record("rm", args + ["exit-present=%s" % os.path.exists(exit_file)] + extra)
    if os.environ.get("FAKE_RM_FAIL"):
        fail("cannot remove %s: Permission denied" % path)
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)
    return 0


tools = {"systemd-run": systemd_run, "systemctl": systemctl, "btrfs": btrfs, "df": df, "rm": rm}
sys.exit(tools[sys.argv[1]](sys.argv[2:]))
'''

TOML = '''host = "%s"
hold = "%s"
root = "%s"
dump = "%s"
enter_closure = "%s"

[devices.gpu]
expect = "example GPU"
env = { HOS_EXAMPLE = "1" }
'''


def shim(python, fakes, tool):
    return "#!/bin/sh\nexec %s -I %s %s \"$@\"\n" % (shlex.quote(python), shlex.quote(str(fakes)), tool)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        fakes = tmp / "fakes.py"
        fakes.write_text(FAKE_TOOLS, encoding="utf-8")
        scripts = {"ssh": FAKE_SSH,
                   "systemd-run": shim(sys.executable, fakes, "systemd-run"),
                   "systemctl": shim(sys.executable, fakes, "systemctl"),
                   "btrfs": shim(sys.executable, fakes, "btrfs"),
                   "df": shim(sys.executable, fakes, "df"),
                   "rm": shim(sys.executable, fakes, "rm")}
        for name, body in scripts.items():
            fake = bin_dir / name
            fake.write_text(body, encoding="utf-8")
            fake.chmod(0o755)
        self.log = tmp / "calls.jsonl"
        self.units_file = tmp / "units.txt"
        self.units_file.write_text("", encoding="utf-8")
        self.steps = tmp / "steps.txt"
        self.steps.write_text("", encoding="utf-8")
        self.grow = tmp / "grow.txt"
        self.grow.write_text("", encoding="utf-8")
        self.hold = tmp / "hold"
        self.root = tmp / "harness root's"
        conf = tmp / "harness.toml"
        conf.write_text(TOML % (HOST, self.hold, self.root, tmp / "dump", tmp / "enter-closure"), encoding="utf-8")
        self.conf = str(conf)
        env = mock.patch.dict(os.environ, {
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_LOG": str(self.log),
            "FAKE_UNITS": str(self.units_file),
            "FAKE_STEPS": str(self.steps),
            "FAKE_GROW": str(self.grow),
            "FAKE_BTRFS_EXCL": "4096",
            "FAKE_AVAIL": "1000000000000",
        })
        env.start()
        self.addCleanup(env.stop)
        for var in ("FAKE_SSH_REFUSE", "FAKE_HOLD_ON_LIST", "FAKE_BTRFS_FAIL", "FAKE_RM_FAIL"):
            os.environ.pop(var, None)
        self.cfg = config.load(self.conf)

    def set_units(self, *names):
        self.units_file.write_text("".join("%s loaded active running Hookshot job\n" % n for n in names), encoding="utf-8")

    def calls(self, tool):
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [entry[1:] for entry in entries if entry[0] == tool]

    def events(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def size_file(self, kind, game=False):
        return Path(self.root, "jobs", kind + (".game.size" if game else ".size"))

    def set_size(self, kind, text, game=False):
        self.size_file(kind, game).parent.mkdir(parents=True, exist_ok=True)
        self.size_file(kind, game).write_text(text, encoding="utf-8")

    def run_start(self, job_id, argv, env=None, game=None):
        script = jobs.start_script(self.cfg, job_id, argv, env, game)
        return remote.run(HOST, script, input=jobs.tools_tar(), check=False)

    def build(self, sha, files):
        game = Path(jobs.build_dir(self.cfg, sha)) / "game"
        game.mkdir(parents=True)
        for name, text in files.items():
            (game / name).write_text(text, encoding="utf-8")
        return str(game)

    def follow(self, job_id):
        code = ("import sys\nsys.path.insert(0, %r)\nimport config\nimport jobs\n"
                "sys.exit(jobs.follow(config.load(%r), %r))\n" % (str(HARNESS), self.conf, job_id))
        return subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True)

    # new_id, unit, job_dir, tools_tar

    def test_new_id_is_kind_utc_time_and_six_hex(self):
        for kind in jobs.KINDS:
            job_id = jobs.new_id(kind)
            self.assertRegex(job_id, r"^%s-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$" % kind)
            self.assertEqual(jobs.unit(job_id), "hx-%s.service" % job_id)
        with self.assertRaises(jobs.JobError):
            jobs.new_id("deploy")

    def test_a_job_id_outside_the_new_id_shape_is_refused_before_it_makes_a_path_or_a_unit(self):
        for bad in ("check-../../x", "bench-20261112T000000Z-abcdef; rm -rf /", "build-2026-abc", "deploy-20261112T000000Z-abcdef"):
            with self.subTest(bad=bad):
                with self.assertRaises(jobs.JobError):
                    jobs.unit(bad)
                with self.assertRaises(jobs.JobError):
                    jobs.job_dir(self.cfg, bad)
                with self.assertRaises(jobs.JobError):
                    jobs.start_script(self.cfg, bad, ["true"])

    def test_tools_tar_holds_the_harness_scripts_and_hookshot_ctl_at_their_paths(self):
        with tarfile.open(fileobj=io.BytesIO(jobs.tools_tar()), mode="r") as tar:
            names = set(tar.getnames())
        self.assertIn("scripts/harness/jobs.py", names)
        self.assertIn("scripts/harness/remote.py", names)
        self.assertIn("scripts/hookshot_ctl.py", names)
        self.assertEqual([n for n in names if not n.startswith("scripts/harness/") and n != "scripts/hookshot_ctl.py"], [])

    # R2: the transient unit, its argv, its environment, its output and its status

    def test_r2_the_unit_runs_the_argv_with_its_env_and_keeps_output_and_status(self):
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["sh", "-c", 'echo "out $HX_TEST"; echo err >&2; exit 3'],
                   env={"HX_TEST": "from-env"})
        (run,) = self.calls("systemd-run")
        self.assertIn("--user", run)
        self.assertIn("--collect", run)
        self.assertIn("--unit=hx-%s.service" % job_id, run)
        self.assertIn("--setenv=HX_TEST=from-env", run)
        for refused in ("--scope", "--wait", "--pty"):
            self.assertFalse([a for a in run if a == refused or a.startswith(refused + "=")], refused)
        job = jobs.job_dir(self.cfg, job_id)
        self.assertIn("--working-directory=%s" % job, run)
        self.assertEqual(Path(job, "output.log").read_text(encoding="utf-8"), "out from-env\nerr\n")
        self.assertEqual(Path(job, "exit").read_text(encoding="utf-8"), "3\n")
        self.assertEqual(stat.S_IMODE(Path(job, "run").stat().st_mode), 0o700)
        self.assertTrue(Path(job, "data").is_dir())
        self.assertTrue(Path(job, "tools", "scripts", "harness", "jobs.py").is_file())
        self.assertTrue(Path(job, "tools", "scripts", "hookshot_ctl.py").is_file())

    def test_r2_argv_words_and_env_values_reach_the_job_intact(self):
        job_id = jobs.new_id("check")
        argv = ["sh", "-c", 'printf "%s|" "$HX_ODD" "$@"', "sh", "it's", "$HOME", "two\nlines"]
        jobs.start(self.cfg, job_id, argv, env={"HX_ODD": "a'b \"c\" $(echo no)"})
        output = Path(jobs.job_dir(self.cfg, job_id), "output.log").read_text(encoding="utf-8")
        self.assertEqual(output, "a'b \"c\" $(echo no)|it's|$HOME|two\nlines|")

    def test_a_job_folder_that_exists_is_an_error(self):
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["true"])
        with self.assertRaises(jobs.JobError):
            jobs.start(self.cfg, job_id, ["true"])
        self.assertEqual(len(self.calls("systemd-run")), 1)

    def test_an_empty_command_is_refused_before_anything_runs(self):
        with self.assertRaises(jobs.JobError):
            jobs.start(self.cfg, jobs.new_id("check"), [])
        self.assertEqual(self.calls("systemd-run"), [])

    def test_a_start_waits_for_the_start_lock_before_it_checks_the_hold_or_the_running_jobs(self):
        self.root.mkdir(parents=True, exist_ok=True)
        tar = Path(self.tmp.name) / "tools.tar"
        tar.write_bytes(jobs.tools_tar())
        script = jobs.start_script(self.cfg, jobs.new_id("check"), ["true"])
        with open(self.root / "start.lock", "w", encoding="utf-8") as lock, open(tar, "rb") as stdin, \
                open(Path(self.tmp.name) / "out.txt", "w", encoding="utf-8") as out:
            fcntl.flock(lock, fcntl.LOCK_EX)
            proc = subprocess.Popen(remote.command(HOST, script), stdin=stdin, stdout=out, stderr=subprocess.STDOUT)
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.wait(timeout=1)
            self.assertEqual(self.calls("systemd-run"), [])
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertEqual(len(self.calls("systemd-run")), 1)

    # R3: the hold

    def test_r3_with_the_hold_present_nothing_starts_and_the_status_is_held(self):
        self.hold.touch()
        job_id = jobs.new_id("check")
        refused = self.run_start(job_id, ["true"])
        self.assertEqual(refused.returncode, jobs.HELD)
        self.assertIn(str(self.hold), refused.stderr)
        self.assertEqual(self.calls("systemd-run"), [])
        self.assertFalse(Path(jobs.job_dir(self.cfg, job_id)).exists())

    def test_r3_start_raises_with_the_hold_named_when_the_hold_is_present(self):
        self.hold.touch()
        with self.assertRaises(jobs.JobError) as caught:
            jobs.start(self.cfg, jobs.new_id("check"), ["true"])
        self.assertIn("held", str(caught.exception))
        self.assertIn(str(self.hold), str(caught.exception))
        self.assertEqual(self.calls("systemd-run"), [])

    def test_r3_a_hold_that_appears_while_the_folder_is_made_still_stops_the_unit(self):
        os.environ["FAKE_HOLD_ON_LIST"] = str(self.hold)
        refused = self.run_start(jobs.new_id("check"), ["true"])
        self.assertEqual(refused.returncode, jobs.HELD)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_r3_wait_hold_returns_only_once_the_hold_is_gone(self):
        self.hold.touch()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                self.hold.unlink()
        out = io.StringIO()
        with mock.patch("time.monotonic", side_effect=[100.0, 112.0]):
            waited = jobs.wait_hold(str(self.hold), interval=5, out=out, sleep=sleep)
        self.assertEqual(sleeps, [5, 5, 5])
        self.assertFalse(self.hold.exists())
        self.assertEqual(waited, 12.0)
        self.assertEqual(out.getvalue().count("waiting"), 1)
        self.assertIn("waited 12 s", out.getvalue())

    def test_r3_wait_hold_without_a_hold_returns_at_once_and_says_nothing(self):
        out = io.StringIO()
        sleeps = []
        with mock.patch("time.monotonic", side_effect=[1.0, 1.0]):
            waited = jobs.wait_hold(str(self.hold), out=out, sleep=sleeps.append)
        self.assertEqual((waited, sleeps, out.getvalue()), (0.0, [], ""))

    # R4: a bench runs alone; a build excludes builds and benches; a check runs beside checks and builds

    def test_r4_a_bench_is_refused_while_a_check_runs(self):
        check = jobs.new_id("check")
        self.set_units(jobs.unit(check))
        refused = self.run_start(jobs.new_id("bench"), ["true"])
        self.assertEqual(refused.returncode, jobs.BUSY)
        self.assertIn(jobs.unit(check), refused.stderr)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_r4_a_bench_is_refused_while_a_build_runs(self):
        build = jobs.new_id("build")
        self.set_units(jobs.unit(build))
        refused = self.run_start(jobs.new_id("bench"), ["true"])
        self.assertEqual(refused.returncode, jobs.BUSY)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_r4_a_build_is_refused_while_a_build_runs_and_makes_no_folder(self):
        running = jobs.new_id("build")
        self.set_units(jobs.unit(running))
        second = jobs.new_id("build")
        refused = self.run_start(second, ["true"])
        self.assertEqual(refused.returncode, jobs.BUSY)
        self.assertIn(jobs.unit(running), refused.stderr)
        self.assertEqual(self.calls("systemd-run"), [])
        self.assertFalse(Path(jobs.job_dir(self.cfg, second)).exists())

    def test_r4_a_build_is_refused_while_a_bench_runs(self):
        bench = jobs.new_id("bench")
        self.set_units(jobs.unit(bench))
        refused = self.run_start(jobs.new_id("build"), ["true"])
        self.assertEqual(refused.returncode, jobs.BUSY)
        self.assertIn(jobs.unit(bench), refused.stderr)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_r4_a_check_starts_beside_a_running_build(self):
        build = jobs.new_id("build")
        self.set_units(jobs.unit(build))
        check = jobs.new_id("check")
        jobs.start(self.cfg, check, ["true"])
        (run,) = self.calls("systemd-run")
        self.assertIn("--unit=%s" % jobs.unit(check), run)

    def test_r4_a_check_is_refused_while_a_bench_runs(self):
        bench = jobs.new_id("bench")
        self.set_units(jobs.unit(bench))
        refused = self.run_start(jobs.new_id("check"), ["true"])
        self.assertEqual(refused.returncode, jobs.BUSY)
        self.assertIn(jobs.unit(bench), refused.stderr)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_r4_two_checks_both_start(self):
        running = jobs.new_id("check")
        self.set_units(jobs.unit(running))
        second = jobs.new_id("check")
        jobs.start(self.cfg, second, ["true"])
        (run,) = self.calls("systemd-run")
        self.assertIn("--unit=%s" % jobs.unit(second), run)

    def test_r4_the_running_jobs_are_read_with_the_harness_unit_pattern(self):
        jobs.start(self.cfg, jobs.new_id("check"), ["true"])
        (listing,) = self.calls("systemctl")
        self.assertEqual(listing, ["--user", "list-units", "--plain", "--no-legend", "--state=active,activating",
                                   "hx-*.service"])

    # R5: two jobs from one build keep their own folders, saves and game copies

    def test_r5_two_jobs_of_one_build_keep_their_own_saves_and_game_copies(self):
        game = self.build("abc1234", {"hookshot": "exe\n", "hookshot_options.ini": "build\n"})
        first, second = jobs.new_id("check"), jobs.new_id("check")
        # the job's game copy is removed when it ends (DL60), so the command keeps what it wrote in its saves folder
        script = ('echo "$0" > "$XDG_DATA_HOME/save"; echo "$0" > game/hookshot_options.ini; '
                  'echo "$0" > "game/only-$0"; cp game/hookshot_options.ini "game/only-$0" "$XDG_DATA_HOME/"')
        for job_id, letter in ((first, "A"), (second, "B")):
            env = jobs.launch_env(jobs.job_dir(self.cfg, job_id), {"HOS_EXAMPLE": "1"})
            jobs.start(self.cfg, job_id, ["sh", "-c", script, letter], env=env, game=game)
        a, b = jobs.job_dir(self.cfg, first), jobs.job_dir(self.cfg, second)
        self.assertNotEqual(a, b)
        self.assertEqual(Path(a, "data", "save").read_text(encoding="utf-8"), "A\n")
        self.assertEqual(Path(b, "data", "save").read_text(encoding="utf-8"), "B\n")
        self.assertEqual(Path(a, "data", "hookshot_options.ini").read_text(encoding="utf-8"), "A\n")
        self.assertEqual(Path(b, "data", "hookshot_options.ini").read_text(encoding="utf-8"), "B\n")
        self.assertTrue(Path(a, "data", "only-A").is_file())
        self.assertFalse(Path(a, "data", "only-B").exists())
        self.assertTrue(Path(b, "data", "only-B").is_file())
        self.assertFalse(Path(b, "data", "only-A").exists())
        self.assertFalse(Path(a, "game").exists())
        self.assertFalse(Path(b, "game").exists())
        self.assertEqual(sorted(p.name for p in Path(game).iterdir()), ["hookshot", "hookshot_options.ini"])
        self.assertEqual(Path(game, "hookshot_options.ini").read_text(encoding="utf-8"), "build\n")

    # DL60, DL181: the job's end removes its game and shots folders, keeps the rest, and records its exclusive size as
    # its kind's last size, the game size when it copied a game; a start under twice that size is refused under the
    # start lock, before its folder is made

    def test_a_finished_job_drops_its_game_folder_keeps_the_rest_and_records_the_exclusive_size(self):
        game = self.build("abc1234", {"payload": "exe\n"})
        job_id = jobs.new_id("check")
        os.environ["FAKE_BTRFS_EXCL"] = "123456"
        jobs.start(self.cfg, job_id, ["sh", "-c", "echo out; echo save > data/save"], game=game)
        job = Path(jobs.job_dir(self.cfg, job_id))
        self.assertFalse(Path(job, "game").exists())
        for kept in ("output.log", "run", "data", "tools", "exit"):
            self.assertTrue(Path(job, kept).exists(), kept)
        self.assertEqual(Path(job, "data", "save").read_text(encoding="utf-8"), "save\n")
        self.assertEqual(Path(job, "output.log").read_text(encoding="utf-8"), "out\n")
        self.assertEqual(Path(job, "exit").read_text(encoding="utf-8"), "0\n")
        self.assertEqual(self.size_file("check", game=True).read_text(encoding="utf-8"), "123456\n")
        self.assertEqual(self.calls("btrfs"), [["filesystem", "du", "-s", "--raw", str(job)]])
        self.assertTrue(Path(game, "payload").is_file())

    def test_the_job_is_measured_before_its_game_folder_goes_and_its_exit_file_is_written_after(self):
        game = self.build("abc1234", {"payload": "exe\n"})
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["true"], game=game)
        job = Path(jobs.job_dir(self.cfg, job_id))
        order = [entry[0] for entry in self.events()]
        self.assertLess(order.index("btrfs"), order.index("rm"))
        self.assertEqual(self.calls("rm"), [["-rf", "--", str(Path(job, "game")), "exit-present=False"]])
        self.assertTrue(Path(job, "exit").is_file())

    def test_every_kind_records_its_size_builds_included_and_a_job_without_a_game_folder_too(self):
        os.environ["FAKE_BTRFS_EXCL"] = "2048"
        for kind in jobs.KINDS:
            with self.subTest(kind=kind):
                jobs.start(self.cfg, jobs.new_id(kind), ["true"])
                self.assertEqual(self.size_file(kind).read_text(encoding="utf-8"), "2048\n")

    def test_a_job_whose_game_folder_cannot_be_removed_says_so_and_keeps_its_status(self):
        game = self.build("abc1234", {"payload": "exe\n"})
        os.environ["FAKE_RM_FAIL"] = "1"
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["sh", "-c", "exit 5"], game=game)
        job = Path(jobs.job_dir(self.cfg, job_id))
        self.assertIn("cannot remove", Path(job, "output.log").read_text(encoding="utf-8"))
        self.assertTrue(Path(job, "game", "payload").is_file())
        self.assertEqual(Path(job, "exit").read_text(encoding="utf-8"), "5\n")

    def test_a_job_btrfs_cannot_measure_says_so_and_keeps_the_last_size(self):
        game = self.build("abc1234", {"payload": "exe\n"})
        self.set_size("check", "777\n", game=True)
        os.environ["FAKE_BTRFS_FAIL"] = "1"
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["sh", "-c", "exit 5"], game=game)
        job = Path(jobs.job_dir(self.cfg, job_id))
        output = Path(job, "output.log").read_text(encoding="utf-8")
        self.assertIn("not a btrfs filesystem", output)
        self.assertIn("cannot measure", output)
        self.assertEqual(self.size_file("check", game=True).read_text(encoding="utf-8"), "777\n")
        self.assertFalse(Path(job, "game").exists())
        self.assertEqual(Path(job, "exit").read_text(encoding="utf-8"), "5\n")

    def test_a_finished_job_removes_every_game_and_shots_folder_at_any_depth_and_keeps_its_data(self):
        game = self.build("abc1234", {"payload": "exe\n"})
        job_id = jobs.new_id("check")
        script = ("mkdir -p shots runs/01/game runs/01/shots runs/01/data; echo save > data/save; "
                  "echo save > runs/01/data/save; echo shot > runs/01/shots/shot.png")
        jobs.start(self.cfg, job_id, ["sh", "-c", script], game=game)
        job = Path(jobs.job_dir(self.cfg, job_id))
        for gone in ("game", "shots", "runs/01/game", "runs/01/shots"):
            self.assertFalse(Path(job, gone).exists(), gone)
        for kept in ("data/save", "runs/01/data/save", "output.log", "exit"):
            self.assertTrue(Path(job, kept).is_file(), kept)
        self.assertEqual(sorted(call[2] for call in self.calls("rm")),
                         sorted(str(Path(job, p)) for p in ("game", "shots", "runs/01/game", "runs/01/shots")))
        self.assertTrue(Path(game, "payload").is_file())

    def test_the_nested_folders_are_removed_before_the_exit_file_is_written(self):
        game = self.build("abc1234", {"payload": "exe\n"})
        job_id = jobs.new_id("check")
        job = Path(jobs.job_dir(self.cfg, job_id))
        os.environ["FAKE_JOB"] = str(job)
        jobs.start(self.cfg, job_id, ["sh", "-c", "mkdir -p shots runs/01/game runs/01/shots"], game=game)
        expected = sorted(str(Path(job, p)) for p in ("game", "shots", "runs/01/game", "runs/01/shots"))
        removed = self.calls("rm")
        self.assertEqual(sorted(call[2] for call in removed), expected)
        self.assertEqual([call[3] for call in removed], ["exit-present=False"] * len(expected))
        self.assertTrue(Path(job, "exit").is_file())

    def test_a_job_stopped_by_sigterm_removes_its_game_and_shots_folders_keeps_its_data_and_exits_143(self):
        game = self.build("abc1234", {"payload": "exe\n"})
        self.set_size("check", "777\n", game=True)
        job_id = jobs.new_id("check")
        job = Path(jobs.job_dir(self.cfg, job_id))
        os.environ["FAKE_JOB"] = str(job)
        # A stop: systemd signals the whole control group, so the wrapper ($PPID) and the command ($$) get SIGTERM.
        script = ("mkdir -p shots runs/01/game runs/01/shots runs/01/data; echo save > data/save; "
                  "echo save > runs/01/data/save; kill -TERM $PPID $$")
        jobs.start(self.cfg, job_id, ["sh", "-c", script], game=game)
        for gone in ("game", "shots", "runs/01/game", "runs/01/shots"):
            self.assertFalse(Path(job, gone).exists(), gone)
        for kept in ("data/save", "runs/01/data/save"):
            self.assertTrue(Path(job, kept).is_file(), kept)
        expected = sorted(str(Path(job, p)) for p in ("game", "shots", "runs/01/game", "runs/01/shots"))
        removed = self.calls("rm")
        self.assertEqual(sorted(call[2] for call in removed), expected)
        self.assertEqual([call[3] for call in removed], ["exit-present=False"] * len(expected))
        self.assertEqual(Path(job, "exit").read_text(encoding="utf-8"), "143\n")
        self.assertEqual(self.size_file("check", game=True).read_text(encoding="utf-8"), "777\n")
        self.assertEqual(self.calls("btrfs"), [])
        done = self.follow(job_id)
        self.assertEqual(done.returncode, 143, done.stderr)

    def test_removal_never_follows_a_link_out_of_the_job_folder(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("kept\n", encoding="utf-8")
        job_id = jobs.new_id("check")
        script = 'mkdir -p runs/01/shots && ln -s "$HX_OUTSIDE" runs/shots'
        jobs.start(self.cfg, job_id, ["sh", "-c", script], env={"HX_OUTSIDE": str(outside)})
        job = Path(jobs.job_dir(self.cfg, job_id))
        self.assertFalse(Path(job, "runs", "01", "shots").exists())
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "kept\n")

    def test_a_start_under_twice_the_kinds_last_size_is_refused_naming_both_numbers_before_its_folder(self):
        self.set_size("check", "1000\n")
        os.environ["FAKE_AVAIL"] = "1999"
        job_id = jobs.new_id("check")
        with self.assertRaises(jobs.JobError) as caught:
            jobs.start(self.cfg, job_id, ["true"])
        self.assertIn("1999", str(caught.exception))
        self.assertIn("2000", str(caught.exception))
        self.assertEqual(self.calls("df"), [["--output=avail", "-B1", str(self.root)]])
        self.assertFalse(Path(jobs.job_dir(self.cfg, job_id)).exists())
        self.assertEqual(self.calls("systemd-run"), [])

    def test_the_refusal_exits_with_its_own_status(self):
        self.set_size("check", "1000\n")
        os.environ["FAKE_AVAIL"] = "1999"
        refused = self.run_start(jobs.new_id("check"), ["true"])
        self.assertEqual(refused.returncode, jobs.LOW_SPACE)
        self.assertIn("1999", refused.stderr)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_a_start_with_twice_the_kinds_last_size_free_starts(self):
        self.set_size("check", "1000\n")
        os.environ["FAKE_AVAIL"] = "2000"
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["true"])
        (run,) = self.calls("systemd-run")
        self.assertIn("--unit=%s" % jobs.unit(job_id), run)

    def test_a_kind_with_no_size_starts_unchecked_and_another_kinds_size_does_not_hold_it(self):
        self.set_size("bench", "%d\n" % 10 ** 12)
        os.environ["FAKE_AVAIL"] = "1"
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["true"])
        self.assertEqual(self.calls("df"), [])
        (run,) = self.calls("systemd-run")
        self.assertIn("--unit=%s" % jobs.unit(job_id), run)

    def test_the_space_check_runs_under_the_start_lock_before_the_folder_is_made(self):
        self.set_size("check", "1000\n")
        os.environ["FAKE_AVAIL"] = "1"
        job_id = jobs.new_id("check")
        script = jobs.start_script(self.cfg, job_id, ["true"])
        tar = Path(self.tmp.name) / "tools.tar"
        tar.write_bytes(jobs.tools_tar())
        out = Path(self.tmp.name) / "out.txt"
        with open(self.root / "start.lock", "w", encoding="utf-8") as lock, open(tar, "rb") as stdin, \
                open(out, "w", encoding="utf-8") as stdout:
            fcntl.flock(lock, fcntl.LOCK_EX)
            proc = subprocess.Popen(remote.command(HOST, script), stdin=stdin, stdout=stdout, stderr=subprocess.STDOUT)
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.wait(timeout=1)
            self.assertEqual(self.calls("df"), [])
            self.assertFalse(Path(jobs.job_dir(self.cfg, job_id)).exists())
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(proc.wait(timeout=30), jobs.LOW_SPACE)
        self.assertEqual(len(self.calls("df")), 1)
        self.assertFalse(Path(jobs.job_dir(self.cfg, job_id)).exists())
        self.assertIn("disk: refused", out.read_text(encoding="utf-8"))

    # A job's last size is kept per kind and per whether it copies a game: a job that copies no game never changes the
    # guard of a job that does, and a job that copies one never changes the guard of a job that does not

    def test_a_job_without_a_game_leaves_the_game_size_and_a_start_with_a_game_is_refused_under_twice_that(self):
        game = self.build("abc1234", {"payload": "exe\n"})
        self.set_size("check", "1000\n", game=True)
        os.environ["FAKE_BTRFS_EXCL"] = "700"
        jobs.start(self.cfg, jobs.new_id("check"), ["true"])
        self.assertEqual(self.size_file("check").read_text(encoding="utf-8"), "700\n")
        self.assertEqual(self.size_file("check", game=True).read_text(encoding="utf-8"), "1000\n")
        os.environ["FAKE_AVAIL"] = "1999"
        refused_id = jobs.new_id("check")
        with self.assertRaises(jobs.JobError) as caught:
            jobs.start(self.cfg, refused_id, ["true"], game=game)
        self.assertIn("1999", str(caught.exception))
        self.assertIn("2000", str(caught.exception))
        self.assertFalse(Path(jobs.job_dir(self.cfg, refused_id)).exists())

    def test_a_start_without_a_game_is_guarded_by_the_gameless_size_and_not_by_the_game_size(self):
        game = self.build("abc1234", {"payload": "exe\n"})
        self.set_size("check", "1000\n")
        os.environ["FAKE_BTRFS_EXCL"] = str(10 ** 12)
        jobs.start(self.cfg, jobs.new_id("check"), ["true"], game=game)
        self.assertEqual(self.size_file("check").read_text(encoding="utf-8"), "1000\n")
        os.environ["FAKE_AVAIL"] = "2000"
        started = self.run_start(jobs.new_id("check"), ["true"])
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(self.calls("df"), [["--output=avail", "-B1", str(self.root)]])

    def test_launch_env_sets_the_saves_folder_the_update_check_and_the_device_variables(self):
        self.assertEqual(jobs.launch_env("/x/jobs/j", {"HOS_EXAMPLE": "1"}),
                         {"XDG_DATA_HOME": "/x/jobs/j/data", "HOOKSHOT_NO_UPDATE_CHECK": "1", "HOS_EXAMPLE": "1"})
        self.assertEqual(jobs.launch_env("/x/jobs/j"),
                         {"XDG_DATA_HOME": "/x/jobs/j/data", "HOOKSHOT_NO_UPDATE_CHECK": "1"})

    # follow: the output as it grows, the job's status, a job with no status, a lost ssh

    def test_follow_prints_the_output_and_returns_the_jobs_status(self):
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["sh", "-c", "echo a; echo b; exit 4"])
        done = self.follow(job_id)
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertEqual(done.stdout, b"a\nb\n")

    def test_follow_keeps_printing_while_the_job_runs(self):
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["sh", "-c", "echo zero"])
        self.steps.write_text("active:one\nactive:two\ninactive\n", encoding="utf-8")
        os.environ["FAKE_GROW"] = str(Path(jobs.job_dir(self.cfg, job_id), "output.log"))
        done = self.follow(job_id)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, b"zero\none\ntwo\n")

    def test_follow_prints_each_byte_once_when_the_log_grows_between_its_size_and_its_print(self):
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["sh", "-c", "echo one"])
        log = Path(jobs.job_dir(self.cfg, job_id), "output.log")
        # A wc that counts the log as it is, then appends "two" to it once: the job writes after follow measured the size.
        slow = Path(self.tmp.name) / "slow"
        slow.mkdir()
        marker = Path(self.tmp.name) / "grew"
        wc = slow / "wc"
        wc.write_text("#!/bin/sh\n%s \"$@\"\n[ -e %s ] || { : > %s; echo two >> %s; }\n" % (
            shlex.quote(shutil.which("wc")), shlex.quote(str(marker)), shlex.quote(str(marker)), shlex.quote(str(log))),
            encoding="utf-8")
        wc.chmod(0o755)
        self.steps.write_text("active:\ninactive\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"PATH": str(slow) + os.pathsep + os.environ["PATH"]}):
            done = self.follow(job_id)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, b"one\ntwo\n")

    def test_follow_says_so_and_returns_1_when_the_job_has_no_status(self):
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["true"])
        Path(jobs.job_dir(self.cfg, job_id), "exit").unlink()
        done = self.follow(job_id)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(b"ended without a status", done.stdout)

    def test_follow_returns_255_when_ssh_is_lost_and_the_job_goes_on(self):
        job_id = jobs.new_id("check")
        jobs.start(self.cfg, job_id, ["true"])
        with mock.patch.dict(os.environ, {"FAKE_SSH_REFUSE": "1"}):
            done = self.follow(job_id)
        self.assertEqual(done.returncode, 255)


HX = HARNESS / "hx.py"

# A stand-in game for the control bridge (docs/control.md, as far as hx smoke reads it): it serves status, wait and quit
# on $HOS_CONTROL, logs each request and its launch's environment to $FAKE_GAME_LOG, and answers from knobs in the
# environment: FAKE_RENDERER (the renderer status names), FAKE_REPLAYED (status' replayed; default the frame), FAKE_FALLBACK
# (prints the fallback line to stdout, which is the run's log.txt), FAKE_DIE_ON_WAIT (exits at the first wait, before any
# answer), FAKE_STALL (answers a wait with the bridge's timeout error, as a game that never reaches the frame does).
STAND_IN_GAME = r'''
import json
import os
import socket
import threading
import time

log = open(os.environ["FAKE_GAME_LOG"], "a", buffering=1)
log.write(json.dumps({"cmd": "_start", "env": {name: os.environ.get(name) for name in (
    "HOS_EXAMPLE", "XDG_DATA_HOME", "HOOKSHOT_NO_UPDATE_CHECK", "HOS_HEADLESS")}}) + "\n")
if os.environ.get("FAKE_FALLBACK"):
    print("vk: preferred device 0 not available or lacks Vulkan 1.3, falling back to auto selection", flush=True)
frame = [0]


def ticker():
    while True:
        time.sleep(0.001)
        frame[0] += 1


threading.Thread(target=ticker, daemon=True).start()
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(os.environ["HOS_CONTROL"])
srv.listen(4)


def serve(conn):
    f = conn.makefile("rwb", buffering=0)
    for raw in f:
        req = json.loads(raw)
        log.write(json.dumps(req) + "\n")
        rid, cmd = req.get("id"), req.get("cmd")
        if cmd == "status":
            replayed = os.environ.get("FAKE_REPLAYED")
            r = {"id": rid, "ok": True, "frame": frame[0], "renderer": os.environ["FAKE_RENDERER"],
                 "replayed": frame[0] if replayed is None else int(replayed)}
        elif cmd == "wait":
            if os.environ.get("FAKE_DIE_ON_WAIT"):
                os._exit(0)
            if os.environ.get("FAKE_STALL"):
                r = {"id": rid, "ok": False, "frame": frame[0], "error": "timeout", "message": "not met in time"}
            else:
                while frame[0] < req["frame"]:
                    time.sleep(0.001)
                r = {"id": rid, "ok": True, "frame": frame[0]}
        elif cmd == "quit":
            f.write(json.dumps({"id": rid, "ok": True, "quitting": True}).encode() + b"\n")
            os._exit(0)
        else:
            r = {"id": rid, "ok": False, "frame": frame[0], "error": "unknown_cmd", "message": cmd}
        f.write((json.dumps(r) + "\n").encode())


while True:
    threading.Thread(target=serve, args=(srv.accept()[0],), daemon=True).start()
'''


def game_shim(stand_in):
    """A build's hookshot: a shell script that runs the stand-in game (the launch runs it as an executable)"""
    return "#!/bin/sh\nexec %s -I %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(str(stand_in)))


def head_sha():
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


class SmokeTests(unittest.TestCase):
    """hx smoke through hx.py: the client's refusals, the job it starts (its argv, game folder and device env) and the
    verdict the in-job half reaches with the stand-in game (R6); the in-job half's hold (R3)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        fakes = tmp / "fakes.py"
        fakes.write_text(FAKE_TOOLS, encoding="utf-8")
        self.stand_in = tmp / "stand_in.py"
        self.stand_in.write_text(STAND_IN_GAME, encoding="utf-8")
        enter = tmp / "enter-closure"
        enter.write_text('#!/bin/sh\nshift\nexec "$@"\n', encoding="utf-8")
        enter.chmod(0o755)
        # (the job runs "python -I ..." on the host: here python is this interpreter)
        scripts = {"ssh": FAKE_SSH,
                   "systemd-run": shim(sys.executable, fakes, "systemd-run"),
                   "systemctl": shim(sys.executable, fakes, "systemctl"),
                   "btrfs": shim(sys.executable, fakes, "btrfs"),
                   "df": shim(sys.executable, fakes, "df"),
                   "rm": shim(sys.executable, fakes, "rm"),
                   "python": "#!/bin/sh\nexec %s \"$@\"\n" % shlex.quote(sys.executable)}
        for name, body in scripts.items():
            fake = bin_dir / name
            fake.write_text(body, encoding="utf-8")
            fake.chmod(0o755)
        self.log = tmp / "calls.jsonl"
        self.game_log = tmp / "game.jsonl"
        self.units_file = tmp / "units.txt"
        self.units_file.write_text("", encoding="utf-8")
        self.steps = tmp / "steps.txt"
        self.steps.write_text("", encoding="utf-8")
        self.grow = tmp / "grow.txt"
        self.grow.write_text("", encoding="utf-8")
        self.hold = tmp / "hold"
        self.root = tmp / "harness root's"
        conf = tmp / "harness.toml"
        conf.write_text(TOML % (HOST, self.hold, self.root, tmp / "dump", enter), encoding="utf-8")
        self.conf = str(conf)
        env = mock.patch.dict(os.environ, {
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_LOG": str(self.log),
            "FAKE_GAME_LOG": str(self.game_log),
            "FAKE_UNITS": str(self.units_file),
            "FAKE_STEPS": str(self.steps),
            "FAKE_GROW": str(self.grow),
            "FAKE_RENDERER": "example GPU (Vulkan, native)",
            "FAKE_BTRFS_EXCL": "4096",
            "FAKE_AVAIL": "1000000000000",
            "HX_CONFIG": self.conf,
        })
        env.start()
        self.addCleanup(env.stop)
        for var in ("FAKE_SSH_REFUSE", "FAKE_FALLBACK", "FAKE_DIE_ON_WAIT", "FAKE_STALL", "FAKE_REPLAYED",
                    "FAKE_BTRFS_FAIL", "FAKE_RM_FAIL"):
            os.environ.pop(var, None)
        self.cfg = config.load(self.conf)

    def calls(self, tool):
        if not self.log.exists():
            return []
        entries = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [entry[1:] for entry in entries if entry[0] == tool]

    def game_requests(self):
        if not self.game_log.exists():
            return []
        return [json.loads(line) for line in self.game_log.read_text(encoding="utf-8").splitlines()]

    def build(self, sha, hookshot=True, closure="/nix/store/fake-closure"):
        """A build in the harness root: its build.txt, and a game folder with a marker and (unless hookshot is false) the
        stand-in as hookshot"""
        folder = Path(jobs.build_dir(self.cfg, sha))
        game = folder / "game"
        game.mkdir(parents=True)
        (game / "marker").write_text(sha + "\n", encoding="utf-8")
        if hookshot:
            exe = game / "hookshot"
            exe.write_text(game_shim(self.stand_in), encoding="utf-8")
            exe.chmod(0o755)
        (folder / "build.txt").write_text("sha: %s\nclosure: %s\n" % (sha, closure), encoding="utf-8")
        return folder

    def latest(self, sha):
        os.symlink(sha, self.root / "builds" / "latest")

    def smoke(self, *args):
        return subprocess.run([sys.executable, "-I", "-B", str(HX), "smoke", *args], capture_output=True, text=True,
                              timeout=120)

    def job_of(self, run):
        (working,) = [a for a in run if a.startswith("--working-directory=")]
        return working.split("=", 1)[1]

    # the client: its refusals, and the job it starts

    def test_an_unknown_device_exits_2_and_names_the_devices(self):
        done = self.smoke("--device", "nope")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("nope", done.stderr)
        self.assertIn("gpu", done.stderr)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_frames_must_be_at_least_one(self):
        self.build("abc1234")
        self.latest("abc1234")
        done = self.smoke("--device", "gpu", "--frames", "0")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("--frames", done.stderr)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_the_job_argv_carries_the_device_env_expected_name_frames_and_the_build_game_folder(self):
        self.build("abc1234")
        self.latest("abc1234")
        done = self.smoke("--device", "gpu", "--frames", "7")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        (run,) = self.calls("systemd-run")
        job = self.job_of(run)
        argv = [self.cfg.enter_closure, "/nix/store/fake-closure", "python", "-I",
                str(Path(job, "tools", "scripts", "harness", "hx.py")), "smoke", "--in-job", job, "--hold", str(self.hold),
                "--expect", "example GPU", "--frames", "7", "--env", "HOS_EXAMPLE=1"]
        self.assertEqual(run[-len(argv):], argv)
        self.assertEqual(self.calls("rm"),
                         [["-rf", "--", str(Path(job, "game")), "exit-present=False", "marker=abc1234"]])
        self.assertFalse(Path(job, "game").exists())
        self.assertIn("build: abc1234", done.stdout.splitlines())
        self.assertIn("job: " + Path(job).name, done.stdout.splitlines())

    def test_build_rev_runs_the_build_of_that_commit_in_this_checkout(self):
        head = head_sha()
        self.build("abc1234")
        self.latest("abc1234")
        self.build(head)
        done = self.smoke("--device", "gpu", "--frames", "7", "--build", "HEAD")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("build: " + head, done.stdout.splitlines())
        (run,) = self.calls("systemd-run")
        job = self.job_of(run)
        self.assertEqual(self.calls("rm"),
                         [["-rf", "--", str(Path(job, "game")), "exit-present=False", "marker=" + head]])
        self.assertFalse(Path(job, "game").exists())

    def test_a_build_rev_that_is_no_commit_here_exits_2(self):
        self.build("abc1234")
        self.latest("abc1234")
        done = self.smoke("--device", "gpu", "--build", "no-such-rev-zz")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("no commit", done.stderr)
        self.assertEqual(self.calls("systemd-run"), [])

    def test_a_build_whose_build_txt_names_another_sha_is_refused(self):
        folder = self.build(head_sha())
        (folder / "build.txt").write_text("sha: abc1234\nclosure: /nix/store/fake-closure\n", encoding="utf-8")
        done = self.smoke("--device", "gpu", "--build", "HEAD")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("abc1234", done.stderr)
        self.assertEqual(self.calls("systemd-run"), [])

    # R6: the in-job half's verdict, with the stand-in game

    def test_r6_a_stand_in_answering_as_the_device_passes_after_n_presents_with_the_device_env(self):
        self.build("abc1234")
        self.latest("abc1234")
        done = self.smoke("--device", "gpu")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lines = done.stdout.splitlines()
        self.assertIn("device: example GPU (Vulkan, native)", lines)
        presents = [int(line.split(": ")[1]) for line in lines if line.startswith("presents: ")]
        self.assertEqual(len(presents), 1)
        self.assertGreaterEqual(presents[0], 300)
        self.assertEqual(lines[-1], "smoke: pass")
        requests = self.game_requests()
        self.assertEqual([r["cmd"] for r in requests][-3:], ["wait", "status", "quit"])
        (wait,) = [r for r in requests if r["cmd"] == "wait"]
        self.assertEqual((wait["event"], wait["frame"]), ("frame", 300))
        (start,) = [r for r in requests if r["cmd"] == "_start"]
        self.assertEqual(start["env"]["HOS_EXAMPLE"], "1")
        self.assertEqual(start["env"]["HOS_HEADLESS"], "1")
        self.assertEqual(start["env"]["HOOKSHOT_NO_UPDATE_CHECK"], "1")
        self.assertTrue(start["env"]["XDG_DATA_HOME"].endswith("data"))

    def test_r6_a_stand_in_answering_as_another_device_fails_and_names_it(self):
        self.build("abc1234")
        self.latest("abc1234")
        os.environ["FAKE_RENDERER"] = "other GPU (Vulkan, native)"
        done = self.smoke("--device", "gpu", "--frames", "7")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("device: other GPU (Vulkan, native)", done.stdout.splitlines())
        self.assertIn("other GPU (Vulkan, native)", done.stdout.splitlines()[-1])
        self.assertTrue(done.stdout.splitlines()[-1].startswith("smoke: FAIL "))

    def test_r6_a_fallback_line_in_the_log_fails(self):
        self.build("abc1234")
        self.latest("abc1234")
        os.environ["FAKE_FALLBACK"] = "1"
        done = self.smoke("--device", "gpu", "--frames", "7")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("device: example GPU (Vulkan, native)", done.stdout.splitlines())
        self.assertTrue(done.stdout.splitlines()[-1].startswith("smoke: FAIL "))
        self.assertIn("fell back to another GPU", done.stdout.splitlines()[-1])

    def test_r6_a_game_that_ends_before_the_frames_fails(self):
        self.build("abc1234")
        self.latest("abc1234")
        os.environ["FAKE_DIE_ON_WAIT"] = "1"
        done = self.smoke("--device", "gpu", "--frames", "7")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("smoke: FAIL wait: hookshot_ctl: the game closed the connection", done.stdout.splitlines())

    def test_r6_a_game_that_never_reaches_the_frames_fails_with_the_timeout(self):
        self.build("abc1234")
        self.latest("abc1234")
        os.environ["FAKE_STALL"] = "1"
        done = self.smoke("--device", "gpu", "--frames", "7")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("smoke: FAIL wait: hookshot_ctl: timeout: not met in time", done.stdout.splitlines())

    def test_r6_a_game_with_no_frames_replayed_fails(self):
        self.build("abc1234")
        self.latest("abc1234")
        os.environ["FAKE_REPLAYED"] = "0"
        done = self.smoke("--device", "gpu", "--frames", "7")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("device: example GPU (Vulkan, native)", done.stdout.splitlines())
        self.assertTrue(done.stdout.splitlines()[-1].startswith("smoke: FAIL "))
        self.assertIn("replayed", done.stdout.splitlines()[-1])

    def test_a_launch_that_fails_is_a_fail_with_the_controls_message(self):
        self.build("abc1234", hookshot=False)
        self.latest("abc1234")
        done = self.smoke("--device", "gpu", "--frames", "7")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("smoke: FAIL launch: hookshot_ctl: no game at", done.stdout)

    # R3: the in-job half launches nothing while the hold is there

    def test_r3_with_the_hold_present_the_in_job_half_launches_nothing_until_it_is_gone(self):
        job = Path(self.tmp.name) / "job"
        (job / "game").mkdir(parents=True)
        exe = job / "game" / "hookshot"
        exe.write_text(game_shim(self.stand_in), encoding="utf-8")
        exe.chmod(0o755)
        (job / "run").mkdir(mode=0o700)
        (job / "data").mkdir()
        self.hold.touch()
        cmd = [sys.executable, "-I", "-B", str(HX), "smoke", "--in-job", str(job), "--hold", str(self.hold),
               "--expect", "example GPU", "--frames", "7", "--env", "HOS_EXAMPLE=1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        watchdog = threading.Timer(60, proc.kill)
        watchdog.start()
        try:
            first = proc.stdout.readline()
            self.assertIn("hold present", first)
            time.sleep(2)   # (the in-job half rechecks the hold after 5 s: a launch made meanwhile would show by now)
            self.assertFalse(self.game_log.exists(), "the game was launched while the hold was present")
            self.hold.unlink()
            rest, err = proc.communicate(timeout=60)
        finally:
            watchdog.cancel()
            proc.kill()
        self.assertEqual(proc.returncode, 0, first + rest + err)
        self.assertIn("hold gone", rest)
        self.assertIn("smoke: pass", rest.splitlines())
        self.assertIn("_start", [r["cmd"] for r in self.game_requests()])


if __name__ == "__main__":
    unittest.main()
