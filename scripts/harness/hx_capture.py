"""hx capture: hx smoke's run of a build's game, with the recorder (RX_CAPTURE) and a PNG of every present switched on, written
as one record run. The capture itself (the recorder's calls.rxc and blobs/) stays on the host, under the record root; the
run names it by hashes, counts and sizes, and holds the game's log and one screenshot per present.

    hx capture --device D [--build REV] [--frames N]

The client half (on this PC) checks the device and the build, then starts a check job (jobs.py) that runs the in-job half
on the host. That waits for the hold, gathers the run's facts before the game starts (so a device the machine does not
list refuses the check with nothing run), launches the game once through the control bridge with the job's own
hookshot_ctl.py, waits for the frames, quits the game and waits for it to end (the recorder closes at exit), checks the
verdict and writes the run. A failing verdict, and a launch that fails, write their run too, marked FAIL.

Exit status: 0 pass, 1 FAIL (the verdict), 2 the check could not run (an unknown device, a build that is missing or
refused, a job that will not start, a run that cannot be written). The client returns the job's status; 255 is ssh lost,
and the job goes on.

run_facts, present_shots and SWITCHES are also what hx play uses.
"""
import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys

import config
import hx_build
import hx_smoke
import jobs
import record

SWITCHES = {"RX_VK_SHOT_EVERY": "1", "RX_VK_PIPE_SKIP": "0"}
PRESENT_SHOT = re.compile(r"vk_([0-9]{6})\.png")
COMMAND_TIMEOUT = 60
CHUNK = 1 << 20


class CaptureError(Exception):
    pass


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx capture", description="Run a build's game headless on one device with the "
                                                               "recorder and a PNG of every present on, and write the run.")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--build", metavar="REV", help="the build of this checkout's commit REV (default: the newest build)")
    p.add_argument("--frames", type=hx_smoke.positive, default=hx_smoke.DEFAULT_FRAMES,
                   help="presents to wait for (default %d)" % hx_smoke.DEFAULT_FRAMES)
    return p.parse_args(argv)


def parse_in_job(argv):
    p = argparse.ArgumentParser(prog="hx capture --in-job", description="The in-job half, run inside a check job on the "
                                                                       "host.")
    p.add_argument("job", metavar="JOB", help="the job's folder")
    p.add_argument("--hold", required=True, metavar="PATH", help="the hold file: no launch while it exists")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--frames", type=hx_smoke.positive, required=True, help="presents to wait for")
    p.add_argument("--env", type=hx_smoke.pair, action="append", metavar="NAME=VALUE",
                   help="the device's environment for the game")
    p.add_argument("--device", required=True, metavar="D", help="the device's name in the configuration")
    p.add_argument("--capture", required=True, metavar="DIR", help="the capture's folder: new, under the record root")
    p.add_argument("--record", required=True, metavar="DIR", help="the record root")
    p.add_argument("--build-folder", required=True, metavar="DIR", help="the build's folder on the host")
    p.add_argument("--closure", required=True, metavar="PATH", help="the build's closure, a /nix/store path")
    return p.parse_args(argv)


def main(argv):
    try:
        if argv[:1] == ["--in-job"]:
            return in_job(parse_in_job(argv[1:]))
        return client(parse_client(argv))
    except (CaptureError, hx_smoke.SmokeError, config.ConfigError, jobs.JobError, record.RecordError, OSError) as e:
        print("capture: %s" % e, file=sys.stderr)
        return 2


def client(a):
    cfg = config.load()
    device = cfg.devices.get(a.device)
    if device is None:
        raise CaptureError("no device %r; the devices are: %s" % (a.device, ", ".join(sorted(cfg.devices))))
    folder, rev_sha = hx_smoke.build_folder(cfg, a.build)
    info = hx_smoke.read_build(cfg, folder)
    sha, closure = info.get("sha"), info.get("closure")
    if not sha or not closure:
        raise CaptureError("%s: build.txt needs sha and closure lines" % folder)
    if not record.GIT_COMMIT.fullmatch(sha) or not record.NIX_STORE_PATH.fullmatch(closure):
        raise CaptureError("%s: build.txt's sha must be a 40-hex commit and its closure a /nix/store/ path" % folder)
    if rev_sha is not None and sha != rev_sha:
        raise CaptureError("the build in %s says sha %s, not %s" % (folder, sha, rev_sha))
    print("build: %s" % sha)
    job_id = jobs.new_id("check")
    job = jobs.job_dir(cfg, job_id)
    record_root = posixpath.join(cfg.root, "results")
    capture = posixpath.join(record_root, "captures", job_id)
    argv = [cfg.enter_closure, closure, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "capture", "--in-job", job, "--hold", cfg.hold, "--expect", device.expect, "--frames", str(a.frames),
            "--device", a.device, "--capture", capture, "--record", record_root, "--build-folder", folder,
            "--closure", closure]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    jobs.start(cfg, job_id, argv, game=posixpath.join(folder, "game"))
    print("job: %s" % job_id)
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("capture: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def in_job(a):
    """The in-job half: it waits for the hold, gathers the run's facts, launches the game once with the recorder and the
    renderer switches on, waits for the frames, quits the game, and writes the run once the game has ended."""
    jobs.wait_hold(a.hold)
    device_env = dict(a.env or [])
    facts = run_facts(a.build_folder, os.path.join(a.job, "game"), a.closure, {**device_env, **SWITCHES}, a.expect)
    shots = os.path.join(a.capture, "shots")
    os.makedirs(shots)
    free = shutil.disk_usage(a.capture).free
    env = jobs.launch_env(a.job, device_env)
    env.update(SWITCHES)
    env.update({"RX_CAPTURE": a.capture, "RX_VK_SHOTS": shots})
    status, why, log = game_run(a.job, env, a.expect, a.frames)
    if status is not None:
        print("device: %s" % status.get("renderer"))
        print("presents: %s" % status.get("frame"))
    verdict_text = "pass" if why is None else "FAIL: %s" % why
    print("capture: %s" % verdict_text)
    run_id = write_capture_run(a, facts, status, verdict_text, log, free)
    print("run: %s" % run_id)
    return 0 if why is None else 1


def game_run(job, env, expect, frames):
    """Launches the game with env, waits for frames presents, quits it and waits for it to end. Returns the status reply
    (None when it failed), why the run fails (None when it passes) and the run's log (None when it did not launch)."""
    launch = hx_smoke.ctl("--json", "launch", os.path.join(job, "game"), "--data", os.path.join(job, "data"),
                          "--run", os.path.join(job, "run"), "--headless", "--timeout", str(hx_smoke.WAIT_SECONDS),
                          *[word for kv in env.items() for word in ("--env", "%s=%s" % kv)])
    if launch.returncode:
        return None, "launch: " + hx_smoke.message(launch), None
    info = json.loads(launch.stdout)
    status, why = hx_smoke.observe(info["socket"], frames)
    stop = hx_smoke.ctl("--sock", info["socket"], "quit")
    if why is None and stop.returncode:
        why = "quit: " + hx_smoke.message(stop)
    hx_smoke.wait_for_exit(info["pid"])
    if why is None:
        why = hx_smoke.verdict(status, expect, frames, info["log"])
    return status, why, info["log"]


def write_capture_run(a, facts, status, verdict_text, log, free):
    """Writes the run: capture.json, the game's log when it launched, and one screenshot per present the renderer wrote.
    Returns the run's id."""
    doc = {
        "capture": a.capture,
        "build": facts["shas"]["hookshot"],
        "device": a.device,
        "frames": a.frames,
        "presents": None if status is None else status.get("frame"),
        "calls": file_entry(os.path.join(a.capture, "calls.rxc")),
        "blobs": blob_stats(os.path.join(a.capture, "blobs")),
        "free_bytes_before": free,
        "verdict": verdict_text,
    }
    files = {"capture.json": (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")}
    if log is not None:
        with open(log, "rb") as f:
            files["game.log"] = f.read()
    run_id, _ = record.write_run(a.record, facts, files, present_shots(os.path.join(a.capture, "shots")),
                                 journal=record.write_journal)
    return run_id


def run_facts(build_folder, game_dir, closure, env, expect):
    """A run's facts, under the record's keys. The build: its commit (build.txt's sha), the build's dirty.diff or "", and
    the sha256 of the game folder's hookshot and hookshot_vk.so. The closure and env as given. The machine: the commands of
    record.MACHINE_COMMANDS run with env, the GPU being the one vulkaninfo lists under deviceName expect."""
    info = parse_build(build_folder)
    outputs = command_outputs(env)
    facts = record.build_facts({"hookshot": info.get("sha", "")}, read_diff(build_folder), binary_sums(game_dir),
                               closure, env)
    facts.update(record.machine_facts(gpu_index(outputs.get("vulkaninfo"), expect), outputs))
    return facts


def parse_build(folder):
    """The build's build.txt on this host, as its key: value lines"""
    with open(os.path.join(folder, "build.txt"), encoding="utf-8") as f:
        text = f.read()
    info = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            info[key.strip()] = value.strip()
    return info


def read_diff(folder):
    """The build's dirty.diff as text, its bytes kept (undecodable bytes are escaped), or "" for a clean build"""
    try:
        with open(os.path.join(folder, "dirty.diff"), "rb") as f:
            return f.read().decode("utf-8", "surrogateescape")
    except FileNotFoundError:
        return ""


def binary_sums(game_dir):
    """sha256sum's text for the game folder's binaries: '<sha256>  <name>' for each, in hx build's order"""
    lines = []
    for name in hx_build.BINARIES:
        digest, _ = file_sha256(os.path.join(game_dir, name))
        lines.append("%s  %s" % (digest, name))
    return "\n".join(lines) + "\n"


def command_outputs(env):
    """Each of record.MACHINE_COMMANDS run with env on top of this process's environment, by name: its stdout, or None
    when it fails, is absent, or does not finish within COMMAND_TIMEOUT seconds"""
    run_env = {**os.environ, **env}
    outputs = {}
    for name, argv in record.MACHINE_COMMANDS.items():
        try:
            done = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace", env=run_env,
                                  timeout=COMMAND_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            outputs[name] = None
        else:
            outputs[name] = done.stdout if done.returncode == 0 else None
    return outputs


def gpu_index(text, expect):
    """The index of the one GPU in vulkaninfo --summary's text whose deviceName is expect"""
    if text is None:
        raise CaptureError("vulkaninfo gave no output, so no GPU can be named %r" % expect)
    found, current = [], None
    for line in text.splitlines():
        header = record.VULKAN_GPU.fullmatch(line)
        field = record.VULKAN_FIELD.fullmatch(line)
        if header:
            current = int(header.group(1))
        elif field and current is not None and field.group(1) == "deviceName" and field.group(2).strip() == expect:
            found.append(current)
    if len(found) != 1:
        raise CaptureError("vulkaninfo lists %d GPUs named %r; the device needs exactly one" % (len(found), expect))
    return found[0]


def present_shots(shots_dir):
    """Each present's PNG the renderer wrote into shots_dir (vk_NNNNNN.png), by its name in a run: present/NNNNNN.png"""
    shots = {}
    for name in sorted(os.listdir(shots_dir)):
        found = PRESENT_SHOT.fullmatch(name)
        if found:
            with open(os.path.join(shots_dir, name), "rb") as f:
                shots["present/%s.png" % found.group(1)] = f.read()
    return shots


def file_sha256(path):
    """(sha256 hex, size) of a file, read in full"""
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def file_entry(path):
    """A file's sha256 and size; a file that cannot be read is a gap"""
    try:
        digest, size = file_sha256(path)
    except OSError as e:
        return {"gap": str(e)}
    return {"sha256": digest, "size": size}


def blob_stats(folder):
    """The number of files in the recorder's blobs folder and their total size; a folder that cannot be read is a gap"""
    try:
        paths = [os.path.join(folder, name) for name in os.listdir(folder)]
        sizes = [os.path.getsize(path) for path in paths if os.path.isfile(path)]
    except OSError as e:
        return {"gap": str(e)}
    return {"count": len(sizes), "bytes": sum(sizes)}
