"""scripts/harness/remote.py against a fake ssh and a fake nix first on PATH: the command shape (one bash -c word, after
-o BatchMode=yes and the host), a bash-only script with quotes, $ and newlines running intact, input bytes on the
script's stdin, the captured output and its decoding, RemoteError on a nonzero status (and on ssh's own 255), stream's
exit status and its stdin on /dev/null, and copy_closure's nix copy arguments. Nothing reaches a real host.

python -I -B tests/test_harness_remote.py
"""
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RUN_ALL_SKIP = {"windows": "the fake ssh and nix are POSIX shell scripts, and the remote script runs under bash"}

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "harness"
sys.path.insert(0, str(HARNESS))
import remote

HOST = "example-host"
STORE_PATH = "/nix/store/00000000000000000000000000000000-example-closure"

# A stand-in for ssh: it takes exactly what remote.command builds (-o BatchMode=yes, the host, one command word), then
# runs that word the way a remote login shell does (sh -c here), so its exit status is the script's.
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

# A stand-in for nix: it records its arguments, one per line, and fails when FAKE_NIX_FAIL is set.
FAKE_NIX = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_NIX_LOG"
if [ -n "$FAKE_NIX_FAIL" ]; then
  echo "error: copying the closure failed" >&2
  exit 1
fi
"""

# The script every bash-only check runs: arrays, [[ ]], quotes, a literal $HOME, and a newline inside a word.
INTACT = r'''set -eu
words=("it's" 'say "hi"' '$HOME' "two
lines")
[[ ${#words[@]} -eq 4 ]] || { echo "count ${#words[@]}" >&2; exit 4; }
printf '%s\n' "${words[@]}"
'''
INTACT_OUT = "it's\nsay \"hi\"\n$HOME\ntwo\nlines\n"


def in_child(code, stdin=b""):
    """Runs code in a fresh Python, with remote imported, and stdin set to the given bytes: stream and run send their
    own stdout and stderr to the caller's, so they are checked from outside this process"""
    prelude = "import sys\nsys.path.insert(0, %r)\nimport remote\n" % str(HARNESS)
    return subprocess.run([sys.executable, "-I", "-B", "-c", prelude + code], input=stdin, capture_output=True)


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("nix", FAKE_NIX)):
            fake = bin_dir / name
            fake.write_text(body, encoding="utf-8")
            fake.chmod(0o755)
        self.nix_log = Path(self.tmp.name) / "nix-args.txt"
        env = mock.patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                                           "FAKE_NIX_LOG": str(self.nix_log)})
        env.start()
        self.addCleanup(env.stop)
        for var in ("FAKE_SSH_REFUSE", "FAKE_NIX_FAIL"):
            os.environ.pop(var, None)

    def test_command_is_one_bash_word_after_batchmode_and_host(self):
        self.assertEqual(remote.command(HOST, "echo 'a b'"),
                         ["ssh", "-o", "BatchMode=yes", HOST, "bash -c " + shlex.quote("echo 'a b'")])

    def test_script_with_bash_syntax_quotes_dollar_and_newlines_runs_intact(self):
        self.assertEqual(remote.run(HOST, INTACT).stdout, INTACT_OUT)

    def test_input_bytes_reach_the_script_stdin(self):
        self.assertEqual(remote.run(HOST, "cat", input=b"line one\nline two\n").stdout, "line one\nline two\n")

    def test_output_is_decoded_as_utf8_with_bad_bytes_replaced(self):
        self.assertEqual(remote.run(HOST, "cat", input=b"ok\xff").stdout, "ok�")

    def test_nonzero_status_raises_with_status_and_stderr(self):
        with self.assertRaises(remote.RemoteError) as caught:
            remote.run(HOST, "echo boom >&2; exit 3")
        self.assertEqual(caught.exception.returncode, 3)
        self.assertEqual(caught.exception.output, "boom\n")
        self.assertIn(HOST, str(caught.exception))
        self.assertIn("exit status 3", str(caught.exception))
        self.assertIn("boom", str(caught.exception))

    def test_error_output_falls_back_to_stdout_when_stderr_is_empty(self):
        with self.assertRaises(remote.RemoteError) as caught:
            remote.run(HOST, "echo only-out; exit 2")
        self.assertEqual(caught.exception.output, "only-out\n")

    def test_nonzero_status_is_returned_with_check_false(self):
        done = remote.run(HOST, "echo boom >&2; exit 3", check=False)
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stderr, "boom\n")

    def test_ssh_own_failure_raises_with_status_255(self):
        with mock.patch.dict(os.environ, {"FAKE_SSH_REFUSE": "1"}):
            with self.assertRaises(remote.RemoteError) as caught:
                remote.run(HOST, "true")
        self.assertEqual(caught.exception.returncode, 255)
        self.assertIn("Connection refused", caught.exception.output)

    def test_run_without_input_gives_the_script_an_empty_stdin(self):
        child = in_child("r = remote.run('example-host', 'cat')\nsys.stdout.write(r.stdout)\n", stdin=b"secret\n")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout, b"")

    def test_stream_returns_the_status_and_sends_output_to_the_callers(self):
        child = in_child("sys.exit(remote.stream('example-host', 'echo out; echo err >&2; exit 5'))\n")
        self.assertEqual(child.returncode, 5, child.stderr)
        self.assertEqual(child.stdout, b"out\n")
        self.assertEqual(child.stderr, b"err\n")

    def test_stream_gives_the_script_an_empty_stdin(self):
        child = in_child("sys.exit(remote.stream('example-host', 'cat'))\n", stdin=b"secret\n")
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout, b"")

    def test_copy_closure_passes_to_and_the_path(self):
        remote.copy_closure(HOST, STORE_PATH)
        self.assertEqual(self.nix_log.read_text(encoding="utf-8").splitlines(),
                         ["copy", "--to", "ssh://example-host", STORE_PATH])

    def test_copy_closure_raises_with_the_nix_error(self):
        with mock.patch.dict(os.environ, {"FAKE_NIX_FAIL": "1"}):
            with self.assertRaises(remote.RemoteError) as caught:
                remote.copy_closure(HOST, STORE_PATH)
        self.assertEqual(caught.exception.returncode, 1)
        self.assertEqual(caught.exception.output, "error: copying the closure failed\n")
        self.assertIn(HOST, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
