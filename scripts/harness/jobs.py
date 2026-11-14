"""The harness's jobs on the build host. Each job is a transient systemd user unit, hx-<job id>.service, that outlives the
ssh session that started it, in a folder of its own under the harness root. start() runs one guarded script over ssh:
under the start lock it refuses while the hold file exists, while a job the kind forbids is running (a bench runs
alone), and while the harness root has under twice the kind's last size free; then it makes the job folder, unpacks the
tools, copies the build's game folder in and starts the unit. When a job's command ends, its wrapper records the job
folder's exclusive size as its kind's last size, removes the job's game folder, and writes the exit file. follow()
prints a job's output to its end and returns its status. wait_hold() is what a job runs before each launch.

Every path and value that reaches the host is shell-quoted, and a job id is checked against the new_id shape before it
makes a path or a unit name. Paths on the host are POSIX whatever the PC is.
"""
import datetime
import io
import os
import posixpath
import re
import secrets
import shlex
import sys
import tarfile
import time
from pathlib import Path

import remote

KINDS = ("build", "check", "bench")
HELD = 75
BUSY = 76
LOW_SPACE = 77
CHECKOUT = Path(__file__).resolve().parents[2]
JOB_ID = re.compile(r"(build|check|bench)-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}")
BUILD_SHA = re.compile(r"[0-9a-f]{7,64}")
# The job's wrapper, run as bash -c WRAPPER hx <exit file> <job folder> <kind's size file> <argv...>. It runs the argv,
# records the job folder's exclusive size as the kind's last size (when btrfs cannot measure it, the job's output says
# so and the size is left as it was), removes the job's game folder, and only then writes the exit file.
WRAPPER = r"""f=$1; job=$2; size_file=$3; shift 3
"$@"; s=$?
excl=
if du=$(btrfs filesystem du -s --raw "$job"); then
  read -r _ excl _ <<< "$(printf '%s\n' "$du" | tail -n 1)"
fi
case $excl in
  ''|*[!0-9]*) echo "job size: btrfs cannot measure $job; the last size of its kind is unchanged" ;;
  *) printf '%s\n' "$excl" > "$size_file.tmp" && mv -f "$size_file.tmp" "$size_file" ;;
esac
rm -rf -- "$job/game"
echo "$s" > "$f"
exit "$s"
"""


class JobError(Exception):
    pass


def _checked(job_id):
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise JobError("not a job id: %r" % (job_id,))
    return job_id


def new_id(kind):
    if kind not in KINDS:
        raise JobError("kind must be one of %s, not %r" % (", ".join(KINDS), kind))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "%s-%s-%s" % (kind, stamp, secrets.token_hex(3))


def unit(job_id):
    return "hx-%s.service" % _checked(job_id)


def job_dir(cfg, job_id):
    return posixpath.join(cfg.root, "jobs", _checked(job_id))


def build_dir(cfg, sha):
    if not isinstance(sha, str) or not BUILD_SHA.fullmatch(sha):
        raise JobError("not a build's commit id: %r" % (sha,))
    return posixpath.join(cfg.root, "builds", sha)


def tools_tar(checkout=CHECKOUT):
    """A tar of the harness's scripts and hookshot_ctl.py, at their paths in the checkout, for a job's tools folder"""
    root = Path(checkout)
    found = sorted((root / "scripts" / "harness").glob("*.py")) + [root / "scripts" / "hookshot_ctl.py"]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in found:
            tar.add(str(path), arcname=path.relative_to(root).as_posix())
    return buf.getvalue()


def start_script(cfg, job_id, argv, env=None, game=None):
    """The bash script that starts job_id. It runs under the start lock and checks the hold before the folder is made
    and again just before the unit starts, so a hold that appears while the folder is copied still stops the unit.
    A kind with a last size is refused while the harness root's filesystem has under twice that free."""
    if not argv:
        raise JobError("a job needs a command to run")
    job = job_dir(cfg, job_id)
    kind = job_id.split("-")[0]
    size_file = posixpath.join(cfg.root, "jobs", kind + ".size")
    log = posixpath.join(job, "output.log")
    q = shlex.quote
    head = ["systemd-run", "--user", "--unit=" + unit(job_id), "--collect", "--working-directory=" + job]
    head += ["--setenv=%s=%s" % kv for kv in (env or {}).items()]
    head += ["--property=StandardOutput=append:" + log, "--property=StandardError=append:" + log]
    tail = [WRAPPER, "hx", posixpath.join(job, "exit"), job, size_file] + list(argv)
    lines = [
        "set -eu",
        "root=%s" % q(cfg.root),
        "hold=%s" % q(cfg.hold),
        "job=%s" % q(job),
        "kind=%s" % q(kind),
        "held_check() {",
        '  if [ -e "$hold" ]; then',
        '    echo "held: $hold is present, so no job starts while it is" >&2',
        "    exit %d" % HELD,
        "  fi",
        "}",
        'mkdir -p "$root"',
        'exec 9>"$root/start.lock"',
        "flock 9",
        "held_check",
        "running=$(systemctl --user list-units --plain --no-legend --state=active,activating %s)" % q("hx-*.service"),
        "busy=",
        "while read -r u _; do",
        '  case "$kind:$u" in',
        '    bench:hx-*|*:hx-bench-*) busy="$busy $u" ;;',
        "  esac",
        'done <<< "$running"',
        'if [ -n "$busy" ]; then',
        '  echo "busy: refused while running:$busy" >&2',
        "  exit %d" % BUSY,
        "fi",
        "size_file=%s" % q(size_file),
        'if [ -f "$size_file" ]; then',
        '  last=$(cat "$size_file")',
        '  free_out=$(df --output=avail -B1 "$root")',
        '  free=$(echo "$free_out" | tail -n 1)',
        '  [ "$free" -ge $((2 * last)) ] || {',
        '    echo "disk: refused: $free bytes free in $root, under $((2 * last))"'
        ' "(twice the last $kind size, $last)" >&2',
        "    exit %d" % LOW_SPACE,
        "  }",
        "fi",
        'mkdir -p "$root/jobs"',
        'mkdir "$job"',
        'mkdir "$job/data" "$job/tools"',
        'mkdir -m 700 "$job/run"',
        'tar -xf - -C "$job/tools"',
    ]
    if game is not None:
        lines.append('cp -a --reflink=auto %s "$job/game"' % q(game))
    lines += [
        "held_check",
        " ".join(map(q, head)) + ' "$BASH" -c ' + " ".join(map(q, tail)),
        "flock -u 9",
    ]
    return "\n".join(lines) + "\n"


def start(cfg, job_id, argv, env=None, game=None, checkout=CHECKOUT):
    """Starts job_id as a unit running argv in its folder. Raises JobError with the script's message when it is refused
    (HELD, BUSY, LOW_SPACE) or fails."""
    script = start_script(cfg, job_id, argv, env, game)
    try:
        remote.run(cfg.host, script, input=tools_tar(checkout))
    except remote.RemoteError as e:
        raise JobError(e.output.strip() or str(e)) from e


def launch_env(job_dir, device_env=None):
    """The environment every game launch in a job adds: its saves folder, no update check, then the device's variables"""
    device_env = device_env or {}
    env = {"XDG_DATA_HOME": posixpath.join(job_dir, "data"), "HOOKSHOT_NO_UPDATE_CHECK": "1"}
    env.update(device_env)
    return env


def follow(cfg, job_id):
    """Prints the job's output as it grows until its unit is no longer active, then returns the job's status (its exit
    file's). 255 is ssh lost: the job goes on."""
    q = shlex.quote
    script = "\n".join([
        "job=%s" % q(job_dir(cfg, job_id)),
        "unit=%s" % q(unit(job_id)),
        "pos=0",
        "while :; do",
        '  if systemctl --user is-active --quiet "$unit"; then active=1; else active=0; fi',
        '  if [ -f "$job/output.log" ]; then',
        '    size=$(wc -c < "$job/output.log")',
        '    if [ "$size" -gt "$pos" ]; then',
        '      tail -c +"$((pos + 1))" "$job/output.log" | head -c "$((size - pos))"',
        "      pos=$size",
        "    fi",
        "  fi",
        '  [ "$active" -eq 1 ] || break',
        "  sleep 1",
        "done",
        'if [ -f "$job/exit" ]; then',
        '  read -r status < "$job/exit"',
        '  exit "$status"',
        "fi",
        "echo \"job ended without a status\"",
        "exit 1",
    ]) + "\n"
    return remote.stream(cfg.host, script)


def wait_hold(hold, interval=5, out=sys.stdout, sleep=time.sleep):
    """Waits while the hold file exists, saying so once and then how long it waited; returns the seconds waited"""
    start = time.monotonic()
    said = False
    while os.path.exists(hold):
        if not said:
            print("hold present (%s): waiting for it to go" % hold, file=out, flush=True)
            said = True
        sleep(interval)
    waited = time.monotonic() - start
    if said:
        print("hold gone: waited %.0f s" % waited, file=out, flush=True)
    return waited
