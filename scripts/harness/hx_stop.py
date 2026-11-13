"""hx stop: ends each named job's unit on the host. A job runs as a transient unit whose control group holds all of its
processes, the game included, so stopping the unit ends them all. Every named unit is stopped in one ssh command, then
each is reported by systemd's is-active answer: stopped when inactive or unknown, else still <state>.

Usage: python -I scripts/harness/hx.py stop <job id>...
Exit status: 0 when every named job is stopped; 1 when one is not, or the host or the configuration cannot be used; 2 for
no ids or a malformed id, which is refused before anything reaches the host.
"""
import shlex
import sys

import config
import jobs
import remote

STOPPED = ("inactive", "unknown")
USAGE = "usage: hx stop <job id>..."


def stop_script(units):
    """The script that stops each unit with its own systemctl call, then prints each unit's is-active answer as
    "<unit> <state>", with an empty state when systemctl gives none. A stop's failure is left to that answer: a job that
    has ended is a unit no longer loaded, whose stop fails while it is stopped, and one unit's failure must not keep the
    others from being stopped"""
    q = shlex.quote
    lines = ["systemctl --user stop %s || true" % q(unit) for unit in units]
    for unit in units:
        lines.append('echo %s "$(systemctl --user is-active %s || true)"' % (q(unit), q(unit)))
    return "\n".join(lines) + "\n"


def main(argv):
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        units = [jobs.unit(job_id) for job_id in argv]
    except jobs.JobError as e:
        print("hx stop: %s" % e, file=sys.stderr)
        return 2
    try:
        cfg = config.load()
        proc = remote.run(cfg.host, stop_script(units))
    except (config.ConfigError, remote.RemoteError) as e:
        print("hx stop: %s" % e, file=sys.stderr)
        return 1
    sys.stderr.write(proc.stderr)
    states = {}
    for line in proc.stdout.splitlines():
        unit, _, state = line.partition(" ")
        states[unit] = state
    every_stopped = True
    for unit in units:
        state = states.get(unit, "")
        if state in STOPPED:
            print("stopped %s" % unit)
        else:
            every_stopped = False
            print("still %s: %s" % (state or "no answer", unit))
    return 0 if every_stopped else 1
