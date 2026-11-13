"""hx stop through hx.py, against a fake ssh and a fake systemctl first on PATH: one ssh command stops every named unit, each
unit is reported stopped (inactive or unknown) or still <state>, the exit status is 0 only when every unit is stopped, no
ids or a malformed id exits 2 before any ssh call, and a refused ssh, a failed stop or a missing config exits 1 with its
message. Nothing reaches a real host.

python -I -B tests/test_harness_stop.py
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUN_ALL_SKIP = {"windows": "the fake ssh and systemctl are POSIX shell scripts, and the remote script runs under bash"}

ROOT = Path(__file__).resolve().parents[1]
HX = ROOT / "scripts" / "harness" / "hx.py"

ONE = "build-20260101T000000Z-0a1b2c"
TWO = "bench-20260102T030405Z-ffeedd"
UNIT_ONE = "hx-%s.service" % ONE
UNIT_TWO = "hx-%s.service" % TWO

CONFIG = """host = "example-host"
hold = "/example/hold-file"
root = "/example/harness"
dump = "/example/dump"
enter_closure = "/example/enter-closure.sh"

[devices.first]
expect = "Example GPU One"
"""

# A stand-in for ssh: it logs one line per call, then takes exactly what remote.command builds (-o BatchMode=yes, the host,
# one command word) and runs that word the way a remote login shell does (sh -c here), so the script runs under bash.
FAKE_SSH = """#!/bin/sh
echo ssh >> "$FAKE_SSH_LOG"
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

# A stand-in for systemctl --user: it logs its arguments, one line per call. stop succeeds, fails when FAKE_STOP_FAIL is
# set, and fails as systemd does (exit 5, "not loaded") for a unit whose state the test wrote as not-loaded: a transient
# unit that has ended. is-active answers the state the test wrote for the unit in $FAKE_STATES, else inactive (a
# not-loaded unit is inactive), with systemctl's exit status: 0 for an active state, 3 otherwise.
FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$FAKE_SYSTEMCTL_LOG"
case "$*" in
  "--user stop "*)
    if [ -n "$FAKE_STOP_FAIL" ]; then
      echo "Failed to stop unit: bus unreachable" >&2
      exit 1
    fi
    if [ -f "$FAKE_STATES/$3" ] && [ "$(cat "$FAKE_STATES/$3")" = not-loaded ]; then
      echo "Failed to stop $3: Unit $3 not loaded." >&2
      exit 5
    fi
    exit 0 ;;
  "--user is-active "*)
    state=inactive
    if [ -f "$FAKE_STATES/$3" ]; then state=$(cat "$FAKE_STATES/$3"); fi
    if [ "$state" = not-loaded ]; then state=inactive; fi
    echo "$state"
    case "$state" in
      active|activating|deactivating) exit 0 ;;
      *) exit 3 ;;
    esac ;;
esac
echo "fake systemctl: not faked: $*" >&2
exit 2
"""


class StopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        bin_dir = base / "bin"
        bin_dir.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("systemctl", FAKE_SYSTEMCTL)):
            fake = bin_dir / name
            fake.write_text(body, encoding="utf-8")
            fake.chmod(0o755)
        self.config = base / "harness.toml"
        self.config.write_text(CONFIG, encoding="utf-8")
        self.states = base / "states"
        self.states.mkdir()
        self.ssh_log = base / "ssh.log"
        self.systemctl_log = base / "systemctl.log"
        # (no FAKE_ or HX_CONFIG variable from the caller's environment reaches a run)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_") and k != "HX_CONFIG"}
        self.env.update({
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "HX_CONFIG": str(self.config),
            "FAKE_SSH_LOG": str(self.ssh_log),
            "FAKE_SYSTEMCTL_LOG": str(self.systemctl_log),
            "FAKE_STATES": str(self.states),
        })

    def stop(self, *args, **extra):
        env = {**self.env, **extra}
        return subprocess.run([sys.executable, "-I", "-B", str(HX), "stop", *args], env=env, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL)

    def set_state(self, unit, state):
        (self.states / unit).write_text(state + "\n", encoding="utf-8")

    def ssh_calls(self):
        return self.ssh_log.read_text(encoding="utf-8").splitlines() if self.ssh_log.exists() else []

    def systemctl_calls(self):
        return self.systemctl_log.read_text(encoding="utf-8").splitlines() if self.systemctl_log.exists() else []

    def test_one_id_stops_its_unit_and_reports_it_stopped(self):
        done = self.stop(ONE)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["stopped %s" % UNIT_ONE])
        self.assertIn("--user stop %s" % UNIT_ONE, self.systemctl_calls())
        self.assertEqual(len(self.ssh_calls()), 1)

    def test_two_ids_stop_both_units_in_one_ssh_command(self):
        done = self.stop(ONE, TWO)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["stopped %s" % UNIT_ONE, "stopped %s" % UNIT_TWO])
        self.assertEqual(len(self.ssh_calls()), 1)
        self.assertIn("--user stop %s" % UNIT_ONE, self.systemctl_calls())
        self.assertIn("--user stop %s" % UNIT_TWO, self.systemctl_calls())

    def test_a_job_that_already_ended_counts_as_stopped_and_the_others_are_still_stopped(self):
        self.set_state(UNIT_ONE, "not-loaded")
        done = self.stop(ONE, TWO)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["stopped %s" % UNIT_ONE, "stopped %s" % UNIT_TWO])
        self.assertIn("--user stop %s" % UNIT_TWO, self.systemctl_calls())
        self.assertEqual(len(self.ssh_calls()), 1)

    def test_a_unit_still_active_after_the_stop_exits_1(self):
        self.set_state(UNIT_ONE, "active")
        done = self.stop(ONE)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["still active: %s" % UNIT_ONE])

    def test_one_still_stopping_of_two_exits_1_and_reports_each(self):
        self.set_state(UNIT_TWO, "deactivating")
        done = self.stop(ONE, TWO)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(done.stdout.splitlines(),
                         ["stopped %s" % UNIT_ONE, "still deactivating: %s" % UNIT_TWO])

    def test_inactive_and_unknown_units_count_as_stopped(self):
        self.set_state(UNIT_ONE, "unknown")
        done = self.stop(ONE)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["stopped %s" % UNIT_ONE])

    def test_no_ids_exits_2_with_usage_and_no_ssh_call(self):
        done = self.stop()
        self.assertEqual(done.returncode, 2)
        self.assertIn("usage: hx stop", done.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_a_malformed_id_exits_2_refused_before_any_ssh_call(self):
        for bad in ("build-20260101T000000Z-0a1b2c/../../x", "%s;reboot" % ONE, "%s x" % ONE, "../x", "hx-%s" % ONE):
            with self.subTest(bad=bad):
                done = self.stop(bad)
                self.assertEqual(done.returncode, 2)
                self.assertIn("not a job id", done.stderr)
                self.assertEqual(done.stdout, "")
                self.assertEqual(self.ssh_calls(), [])

    def test_one_malformed_id_among_good_ones_sends_nothing(self):
        done = self.stop(ONE, "%s;reboot" % TWO)
        self.assertEqual(done.returncode, 2)
        self.assertIn("not a job id", done.stderr)
        self.assertEqual(self.ssh_calls(), [])
        self.assertEqual(self.systemctl_calls(), [])

    def test_refused_ssh_exits_1_with_its_message(self):
        done = self.stop(ONE, FAKE_SSH_REFUSE="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("Connection refused", done.stderr)
        self.assertEqual(done.stdout, "")

    def test_a_failed_stop_exits_1_with_systemctls_message_and_the_units_state(self):
        self.set_state(UNIT_ONE, "active")
        done = self.stop(ONE, FAKE_STOP_FAIL="1")
        self.assertEqual(done.returncode, 1)
        self.assertIn("bus unreachable", done.stderr)
        self.assertEqual(done.stdout.splitlines(), ["still active: %s" % UNIT_ONE])

    def test_a_missing_config_exits_1_naming_the_file_before_any_ssh_call(self):
        absent = str(self.config.parent / "absent.toml")
        done = self.stop(ONE, HX_CONFIG=absent)
        self.assertEqual(done.returncode, 1)
        self.assertIn(absent, done.stderr)
        self.assertEqual(self.ssh_calls(), [])


if __name__ == "__main__":
    unittest.main()
