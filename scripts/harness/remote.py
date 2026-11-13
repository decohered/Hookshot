"""The build host over ssh: one bash script per call, captured (run) or streamed to the caller's output (stream), and a
store path's closure copied there with nix copy. Each script is sent as one command word, `bash -c <quoted script>`:
ssh joins the words for the host's login shell to parse, so the script runs under bash whatever that shell is. A failed
command raises (or returns its status, where that is asked); nothing is retried."""
import shlex
import subprocess
import sys


class RemoteError(Exception):
    """A command that exited nonzero (ssh's own failure is 255). output is its stderr, else its stdout."""

    def __init__(self, host, returncode, output):
        self.returncode = returncode
        self.output = output
        detail = output.strip()
        super().__init__("%s: exit status %d%s" % (host, returncode, ": " + detail if detail else ""))


def command(host, script):
    return ["ssh", "-o", "BatchMode=yes", host, "bash -c " + shlex.quote(script)]


def _decoded(proc):
    """proc with its stdout and stderr as text: UTF-8, with the bytes it cannot decode replaced"""
    proc.stdout = proc.stdout.decode("utf-8", errors="replace")
    proc.stderr = proc.stderr.decode("utf-8", errors="replace")
    return proc


def _raise_if_failed(host, proc):
    if proc.returncode:
        raise RemoteError(host, proc.returncode, proc.stderr or proc.stdout)


def run(host, script, input=None, check=True):
    """Runs script on host under bash and returns its CompletedProcess (stdout and stderr as text). input is bytes for
    its stdin, or None for /dev/null. A nonzero status raises RemoteError, unless check is false."""
    proc = _decoded(subprocess.run(command(host, script), input=input,
                                   stdin=None if input is not None else subprocess.DEVNULL,
                                   capture_output=True))
    if check:
        _raise_if_failed(host, proc)
    return proc


def stream(host, script):
    """Runs script on host under bash with its stdout and stderr on the caller's, its stdin on /dev/null; returns the
    exit status, whatever it is."""
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run(command(host, script), stdin=subprocess.DEVNULL).returncode


def copy_closure(host, path):
    """Copies the closure of the store path to host's store with nix copy; raises RemoteError if that fails."""
    _raise_if_failed(host, _decoded(subprocess.run(["nix", "copy", "--to", "ssh://" + host, path],
                                                   capture_output=True)))
