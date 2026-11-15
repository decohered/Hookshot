"""hx run: one scene of the suite run once as a bench job, and its record. The suite (scenes/suite.toml under the harness root,
which lives outside git) names the scene: a held-out scene runs only with --final, and a warm scene only once this build has
a warm copy of its game for this device, which a passing cold run makes.

The client half (on this PC) checks the device, reads the build's build.txt on the host and starts a bench
job. The in-job half (on the host) waits for the hold, selects the scene from the suite file, copies the build's game (its
warm copy for a warm scene) into the job, starts the game with the scene's save and slot, enters play, checks the device,
plays the scene's route, sets the hour and holds its window (seconds of presents) through the control bridge
(docs/control.md). It quits the game and writes one record: the raw outputs, the metrics, the scene's file and the suite
file.

    hx run <scene> --device D [--build REV] [--final]

Exit status: 0 pass, 1 FAIL (the verdict, recorded), 2 the run could not run (an unknown device, scene or held-out scene, a
suite or build that is missing or refused, a warm scene with no warm copy, a job or a game that will not start, a run that
is not recorded). A passing run whose warm copy cannot be made is still a pass: the warning goes to stderr. The client
returns the job's status; 255 is ssh lost, and the job goes on.
"""
import argparse
import dataclasses
import getpass
import json
import os
import posixpath
import shutil
import subprocess
import sys
import time

import bridge
import config
import game
import hx_smoke
import jobs
import metrics
import record
import runrecord
import scenes

SUITE = "suite.toml"
RUN_FILES = {"log": "log.txt", "frame_trace": "hookshot_frames.csv", "frame_log": "frametimes.csv"}
PRESSES_TO_PLAY = 30    # presses of A into play, at most
HOUR_FRAMES = 60        # presents waited after the hour is set
FPS = 60                # presents a second: a scene's seconds are this many presents
WAIT_SECONDS = 1800     # a wait's or an input's timeout: a fresh build prepares its shaders at its first start
QUIT_SECONDS = 60
FAILURES = (bridge.BridgeError, game.GameError, game.ctl.Failed)


class RunError(Exception):
    """A run that cannot run: the message is its one line"""


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx run", description="Run one scene of the suite once as a bench job and write its "
                                                         "record.")
    p.add_argument("scene", metavar="SCENE", help="a scene of the suite")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--build", metavar="REV", help="the build of this checkout's commit REV (default: the newest build)")
    p.add_argument("--final", action="store_true", help="allow a held-out scene (a final run)")
    return p.parse_args(argv)


def parse_in_job(argv):
    p = argparse.ArgumentParser(prog="hx run --in-job", description="The in-job half, run inside a bench job on the host.")
    p.add_argument("job", metavar="JOB", help="the job's folder")
    p.add_argument("--hold", required=True, metavar="PATH", help="the hold file: no launch while it exists")
    p.add_argument("--root", required=True, metavar="DIR", help="the harness root: its scenes folder holds the suite")
    p.add_argument("--build-folder", required=True, metavar="DIR", help="the build's folder: its build.txt, dirty.diff, game "
                                                                       "and warm copies")
    p.add_argument("--device", required=True, metavar="D", help="the device's name in the configuration")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--env", type=hx_smoke.pair, action="append", metavar="NAME=VALUE",
                   help="the device's environment for the game")
    p.add_argument("--scene", required=True, metavar="NAME", help="the scene to run")
    p.add_argument("--final", action="store_true", help="allow a held-out scene (a final run)")
    return p.parse_args(argv)


def main(argv):
    if argv[:1] == ["--in-job"]:
        return in_job(parse_in_job(argv[1:]))
    try:
        return client(parse_client(argv))
    except (RunError, scenes.SceneError, config.ConfigError, jobs.JobError, hx_smoke.SmokeError) as e:
        print("hx run: %s" % e, file=sys.stderr)
        return 2


def client(a):
    cfg = config.load()
    device = cfg.devices.get(a.device)
    if device is None:
        raise RunError("no device %r; the devices are: %s" % (a.device, ", ".join(sorted(cfg.devices))))
    folder, rev_sha = hx_smoke.build_folder(cfg, a.build)
    info = hx_smoke.read_build(cfg, folder)
    sha, closure = info.get("sha"), info.get("closure")
    if not sha or not closure:
        raise RunError("%s: build.txt needs sha and closure lines" % folder)
    if rev_sha is not None and sha != rev_sha:
        raise RunError("the build in %s says sha %s, not %s" % (folder, sha, rev_sha))
    print("build: %s" % sha)
    job_id = jobs.new_id("bench")
    job = jobs.job_dir(cfg, job_id)
    argv = [cfg.enter_closure, closure, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "run", "--in-job", job, "--hold", cfg.hold, "--root", cfg.root, "--build-folder", folder,
            "--device", a.device, "--expect", device.expect]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    argv += ["--scene", a.scene]
    if a.final:
        argv.append("--final")
    jobs.start(cfg, job_id, argv)
    print("job: %s" % job_id)
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("hx run: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def in_job(a):
    """The in-job half. Returns 0 for a pass, 1 for a FAIL (recorded) and 2 when the run could not run."""
    jobs.wait_hold(a.hold)
    job = os.path.abspath(a.job)
    game_dir = os.path.join(job, "game")
    try:
        suite_data, suite, scene = load_scene(a.root, a.scene, a.final)
        build_txt = read_text(os.path.join(a.build_folder, "build.txt"))
        copy_game(game_source(a.build_folder, a.device, scene.cache), game_dir)
        hashes = binary_hashes(game_dir)
    except (RunError, scenes.SceneError) as e:
        print("hx run: %s" % e, file=sys.stderr)
        return 2
    device_env = dict(a.env or [])
    launched = int(time.time())
    try:
        running = game.start(job, game_dir, device_env, save=scenes.save_dir(a.root, scene.save), slot=scene.slot)
    except game.GameError as e:
        print("hx run: the game did not start: %s" % e, file=sys.stderr)
        return 2
    print("device: %s" % a.device)
    why, first, last, here = None, None, None, None
    try:
        first, last, here = play(running, scene, a.expect)
    except FAILURES as e:
        why = str(e)
    exit_status = None
    try:
        exit_status = running.quit(QUIT_SECONDS)
    except game.GameError as e:
        why = why or str(e)
    found = running.outputs()
    log, trace, frame_log = (text_of(found, name) for name in ("log", "frame_trace", "frame_log"))
    if why is None and log is None:
        why = "the run's log is missing, so the device fallback check cannot run"
    fallback = None if log is None else metrics.fallback(log)
    if why is None and fallback is not None:
        why = "the renderer fell back to another GPU: %s" % fallback
    kernel = kernel_text(launched)
    report = metrics.summary(log, trace, frame_log, kernel, exit_status)
    report["window"] = window_metrics(first, last, trace, frame_log)
    files = {RUN_FILES[name]: content for name, content in found.items()}
    if kernel is not None:
        files["kernel.txt"] = kernel.encode("utf-8")
    files["metrics.json"] = dump(report)
    files["scene.json"] = dump(scene_report(a, scene, suite, first, last, here, exit_status, why))
    files[SUITE] = suite_data
    verdict = ("hx run: FAIL %s" % why) if why else "hx run: pass"
    try:
        run_id, digest = write_run(a, game.run_env(job, device_env), files, suite.sha256, build_txt, hashes)
    except (OSError, RunError, runrecord.RunRecordError, record.RecordError) as e:
        print("hx run: the run is not recorded: %s" % e, file=sys.stderr)
        print(verdict)
        return 2
    print("run: %s" % run_id)
    print("manifest: %s" % digest)
    print("suite: %s" % suite.sha256)
    print(verdict)
    if why is None:
        try:
            prepare_warm(a.build_folder, a.device, game_dir)
        except (OSError, RunError) as e:
            print("hx run: warning: the warm copy is not made: %s" % e, file=sys.stderr)
    return 1 if why else 0


def load_scene(root, name, final):
    """The suite file's bytes, the suite parsed from them, and the scene selected from it"""
    path = posixpath.join(scenes.scene_dir(root), SUITE)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise RunError("%s: cannot read it: %s" % (path, e.strerror or e)) from None
    suite = scenes.parse(data, path)
    return data, suite, scenes.select(suite, name, final)


def play(running, scene, expect):
    """Plays the scene on the game: in play and on the device, the route played, then the window held. The game's day
    advances per present, so the hour is set once the route's wall-timed steps are done: a scene with an hour sets it
    after its route and before its window, and a load scene sets it after its arrival, before its window. Returns the
    window's first status, its last, and the map it was held on. The bridge's and the game's errors are raised."""
    conn = running.connect()
    try:
        session = bridge.Session(conn)
        session.enter_play(PRESSES_TO_PLAY)
        session.check_device(expect)
        for step in scene.route:
            route_step(session, step)
        frames = scene.seconds * FPS
        if scene.kind == "load":
            first = session.status()
            here = session.arrive(scene.load_to)
            start_window(running, session, scene)
            _, last = session.window(frames, here)
        else:
            start_window(running, session, scene)
            here = session.status().get("map")
            first, last = session.window(frames, here, allow_event=scene.kind == "cutscene")
        return first, last, here
    finally:
        conn.close()


def start_window(running, session, scene):
    """What the window's stretch starts with: the scene's hour, set and waited for its presents when it has one, then a
    moving scene's input sequence, started without a wait so that it runs through the window"""
    if scene.hour is not None:
        running.set_hour(scene.hour)
        session.call("wait", event="present", frames=HOUR_FRAMES, timeout=WAIT_SECONDS)
    if scene.motion == "moving":
        session.call("sequence", script=scene.inputs, wait=False)


def route_step(session, step):
    """One route step: a teleport, an age switch (then settled), an input script, or a wait for presents"""
    ((kind, value),) = step.items()
    if kind == "teleport":
        session.arrive(value)
    elif kind == "age":
        session.age(value)
        session.settle(bridge.SETTLE_SECONDS)
    elif kind == "inputs":
        session.call("sequence", script=value)
    else:
        session.call("wait", event="present", frames=value, timeout=WAIT_SECONDS)


def game_source(build_folder, device, cache):
    """The build's game for a cold scene, or its warm copy for this device for a warm one. RunError when a warm scene has no
    warm copy: a passing cold run of this build on this device makes it."""
    if cache == "cold":
        return os.path.join(build_folder, "game")
    warm = warm_game(build_folder, device)
    if not os.path.isdir(warm):
        raise RunError("the scene is warm, and this build has no warm copy for %s yet (%s): a passing cold run makes it"
                       % (device, warm))
    return warm


def warm_game(build_folder, device):
    return os.path.join(build_folder, "warm", device, "game")


def prepare_warm(build_folder, device, job_game):
    """Makes the build's warm copy for this device from the job's game when there is none. It is copied to a temporary name
    beside it and renamed into place, so a copy that stops half way is never taken as warm; a copy that exists is kept."""
    dest = warm_game(build_folder, device)
    if os.path.lexists(dest):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    if os.path.lexists(part):
        shutil.rmtree(part)
    copy_game(job_game, part)
    os.rename(part, dest)


def copy_game(source, dest):
    """Copies source to dest, which must not exist yet, with cp -a and a reflink where the filesystem allows"""
    try:
        done = subprocess.run(["cp", "-a", "--reflink=auto", source, dest], capture_output=True, text=True)
    except OSError as e:
        raise RunError("cannot run cp: %s" % (e.strerror or e)) from None
    if done.returncode:
        raise RunError("cannot copy %s: %s" % (source, done.stderr.strip()))


def binary_hashes(game_dir):
    """sha256sum's output for the game's binaries, run in the job's game folder"""
    try:
        done = subprocess.run(["sha256sum", "hookshot", "hookshot_vk.so"], cwd=game_dir, capture_output=True, text=True)
    except OSError as e:
        raise RunError("cannot run sha256sum: %s" % (e.strerror or e)) from None
    if done.returncode:
        raise RunError("sha256sum of the game: %s" % (done.stderr.strip() or "exit status %d" % done.returncode))
    return done.stdout


def window_metrics(first, last, trace, frame_log):
    """The trace's and the frame log's stats over the frames from the first status's replayed count to the last's"""
    lo, hi = (first or {}).get("replayed"), (last or {}).get("replayed")
    if not isinstance(lo, int) or not isinstance(hi, int):
        return {"gap": "window: no status with a replayed count"}
    return {"first": lo, "last": hi,
            "trace": windowed("frame trace", trace, lambda text: trace_window(text, lo, hi)),
            "frame_time_ms": windowed("frame log", frame_log, lambda text: frame_window(text, lo, hi))}


def trace_window(text, lo, hi):
    rows = metrics.window(metrics.trace_rows(text), lo, hi)
    return {"rows": len(rows), "columns": metrics.column_stats(rows)}


def frame_window(text, lo, hi):
    return metrics.stats([row["frametime_ms"] for row in metrics.window(metrics.frame_times(text), lo, hi)])


def windowed(name, text, parse):
    """parse(text), or a gap naming the output when it is missing or refused"""
    if text is None:
        return {"gap": "%s: not written" % name}
    try:
        return parse(text)
    except metrics.MetricsError as e:
        return {"gap": "%s: %s" % (name, e)}


def scene_report(a, scene, suite, first, last, here, exit_status, why):
    """The scene's fields, the run's device and final flag, the suite's hash, the window's bounds and map, and the verdict"""
    return {**dataclasses.asdict(scene), "final": a.final, "device": a.device, "suite_sha256": suite.sha256,
            "window": {"map": here, "first": (first or {}).get("replayed"), "last": (last or {}).get("replayed")},
            "exit_status": exit_status, "verdict": "FAIL" if why else "pass", "reason": why}


def kernel_text(launched):
    """The kernel log since the launch, as journalctl prints it; None when it cannot be read"""
    try:
        done = subprocess.run(["journalctl", "-k", "--since", "@%d" % launched, "--no-pager"], capture_output=True)
    except OSError:
        return None
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None


def write_run(a, env, files, suite_sha, build_txt, hashes):
    """The run's record: its facts from the build folder, the game's binaries and the machine, and the suite's hash, written
    once. Returns the run's id and the sha256 of its manifest."""
    facts = runrecord.facts(build_txt, read_diff(a.build_folder), hashes, env, a.expect, runrecord.machine_outputs(),
                            os.path.expanduser("~"), getpass.getuser())
    facts["suite_sha256"] = suite_sha
    return runrecord.write(a.root, facts, files)


def read_diff(build_folder):
    """The build's dirty diff, or '' when the checkout was clean"""
    try:
        with open(os.path.join(build_folder, "dirty.diff"), encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise RunError("cannot read %s: %s" % (path, e.strerror or e)) from None
    except UnicodeDecodeError:
        raise RunError("%s: not UTF-8 text" % path) from None


def text_of(found, name):
    """An output's text, or None when it was not written"""
    data = found.get(name)
    return None if data is None else data.decode("utf-8", "replace")


def dump(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
