"""hx smoke: a build's game, run headless on one GPU until it has presented N frames. It passes only when the renderer
presented them on that GPU. The client half (on this PC) checks the device, reads the build's build.txt on the host and
starts a check job (jobs.py) that runs the in-job half on the host: it waits for the hold, launches the game through the
control bridge with the job's own hookshot_ctl.py, reads the status and the run's log, quits the game and prints the
verdict.

    hx smoke --device D [--build REV] [--frames N]

Exit status: 0 pass, 1 FAIL (the verdict), 2 the check could not run (an unknown device, a build that is missing or
refused, a job that will not start). The client returns the job's status; 255 is ssh lost, and the job goes on.
"""
import argparse
import json
import os
import posixpath
import shlex
import subprocess
import sys
import time
from pathlib import Path

import config
import jobs
import remote

DEFAULT_FRAMES = 300
WAIT_SECONDS = 1800
CTL = Path(__file__).resolve().parent.parent / "hookshot_ctl.py"
FALLBACK = "falling back to auto selection"


class SmokeError(Exception):
    pass


def positive(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError("not a number of frames (at least 1): %r" % text)
    return value


def pair(text):
    name, eq, value = text.partition("=")
    if not eq or not name:
        raise argparse.ArgumentTypeError("not NAME=VALUE: %r" % text)
    return name, value


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx smoke", description="Run a build's game headless on one device until it has "
                                                             "presented N frames.")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--build", metavar="REV", help="the build of this checkout's commit REV (default: the newest build)")
    p.add_argument("--frames", type=positive, default=DEFAULT_FRAMES,
                   help="presents to wait for (default %d)" % DEFAULT_FRAMES)
    return p.parse_args(argv)


def parse_in_job(argv):
    p = argparse.ArgumentParser(prog="hx smoke --in-job", description="The in-job half, run inside a check job on the host.")
    p.add_argument("job", metavar="JOB", help="the job's folder")
    p.add_argument("--hold", required=True, metavar="PATH", help="the hold file: no launch while it exists")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--frames", type=positive, required=True, help="presents to wait for")
    p.add_argument("--env", type=pair, action="append", metavar="NAME=VALUE", help="the device's environment for the game")
    return p.parse_args(argv)


def main(argv):
    if argv[:1] == ["--in-job"]:
        return in_job(parse_in_job(argv[1:]))
    try:
        return client(parse_client(argv))
    except (SmokeError, config.ConfigError, jobs.JobError) as e:
        print("smoke: %s" % e, file=sys.stderr)
        return 2


def client(a):
    cfg = config.load()
    device = cfg.devices.get(a.device)
    if device is None:
        raise SmokeError("no device %r; the devices are: %s" % (a.device, ", ".join(sorted(cfg.devices))))
    folder, rev_sha = build_folder(cfg, a.build)
    info = read_build(cfg, folder)
    sha, closure = info.get("sha"), info.get("closure")
    if not sha or not closure:
        raise SmokeError("%s: build.txt needs sha and closure lines" % folder)
    if rev_sha is not None and sha != rev_sha:
        raise SmokeError("the build in %s says sha %s, not %s" % (folder, sha, rev_sha))
    print("build: %s" % sha)
    job_id = jobs.new_id("check")
    job = jobs.job_dir(cfg, job_id)
    argv = [cfg.enter_closure, closure, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "smoke", "--in-job", job, "--hold", cfg.hold, "--expect", device.expect, "--frames", str(a.frames)]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    jobs.start(cfg, job_id, argv, game=posixpath.join(folder, "game"))
    print("job: %s" % job_id)
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("smoke: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def build_folder(cfg, rev):
    """The host folder of the build that REV names in this checkout, with that commit; or the newest build's folder with
    None"""
    if rev is None:
        return posixpath.join(cfg.root, "builds", "latest"), None
    try:
        got = subprocess.run(["git", "-C", str(jobs.CHECKOUT), "rev-parse", "--verify", "--quiet", rev + "^{commit}"],
                             capture_output=True, text=True)
    except OSError as e:
        raise SmokeError("cannot run git to find %r: %s" % (rev, e.strerror)) from None
    if got.returncode:
        raise SmokeError("no commit %r in this checkout" % rev)
    sha = got.stdout.strip()
    return jobs.build_dir(cfg, sha), sha


def read_build(cfg, folder):
    """The build's build.txt, read on the host, as its key: value lines"""
    try:
        text = remote.run(cfg.host, "cat " + shlex.quote(posixpath.join(folder, "build.txt"))).stdout
    except remote.RemoteError as e:
        raise SmokeError(str(e)) from None
    info = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            info[key.strip()] = value.strip()
    return info


def in_job(a):
    """The in-job half. It waits for the hold, then launches the game once, waits for frames presents, reads the status,
    quits the game and checks the verdict; it returns once the game has ended."""
    jobs.wait_hold(a.hold)
    env = jobs.launch_env(a.job, dict(a.env or []))
    launch = ctl("--json", "launch", os.path.join(a.job, "game"), "--data", os.path.join(a.job, "data"),
                 "--run", os.path.join(a.job, "run"), "--headless", "--timeout", str(WAIT_SECONDS),
                 *[word for kv in env.items() for word in ("--env", "%s=%s" % kv)])
    if launch.returncode:
        print("smoke: FAIL launch: %s" % message(launch))
        return 1
    info = json.loads(launch.stdout)
    status, why = observe(info["socket"], a.frames)
    if status is not None:
        print("device: %s" % status.get("renderer"))
        print("presents: %s" % status.get("frame"))
    stop = ctl("--sock", info["socket"], "quit")
    if why is None and stop.returncode:
        why = "quit: " + message(stop)
    if why is None:
        why = verdict(status, a.expect, a.frames, info["log"])
    print("smoke: pass" if why is None else "smoke: FAIL %s" % why)
    wait_for_exit(info["pid"])
    return 0 if why is None else 1


def observe(sock, frames):
    """Waits for frames presents, then reads the status: (the status reply, None), or (None, why it failed)"""
    waited = ctl("--sock", sock, "--json", "wait", "frame", "--frame", str(frames), "--timeout", str(WAIT_SECONDS))
    if waited.returncode:
        return None, "wait: " + message(waited)
    status = ctl("--sock", sock, "--json", "status")
    if status.returncode:
        return None, "status: " + message(status)
    return json.loads(status.stdout), None


def verdict(status, expect, frames, log):
    """Why the run fails, or None: the renderer is expect's on Vulkan, the game is at frames presents, it replayed some
    frames, and the run's log has no fallback to another GPU"""
    want = "%s (Vulkan, native)" % expect
    if status.get("renderer") != want:
        return "the renderer is %r, not %r" % (status.get("renderer"), want)
    if (status.get("frame") or 0) < frames:
        return "the game is at frame %s, short of %d presents" % (status.get("frame"), frames)
    if (status.get("replayed") or 0) <= 0:
        return "the renderer replayed no frames (replayed %r)" % (status.get("replayed"),)
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            fell_back = any(FALLBACK in line for line in f)
    except OSError as e:
        return "cannot read the run's log %s: %s" % (log, e.strerror)
    if fell_back:
        return "the log shows the renderer fell back to another GPU (%s)" % FALLBACK
    return None


def ctl(*args):
    """hookshot_ctl.py from the job's tools, run with this interpreter; its completed process, its output as UTF-8"""
    return subprocess.run([sys.executable, "-I", str(CTL), *args], capture_output=True, encoding="utf-8", errors="replace")


def message(proc):
    return proc.stderr.strip() or "exit status %d" % proc.returncode


def wait_for_exit(pid):
    """Returns once process pid has ended: gone, or a zombie that its parent has not reaped"""
    while _running(pid):
        time.sleep(0.2)


def _running(pid):
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"
