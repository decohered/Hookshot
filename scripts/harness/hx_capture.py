"""hx capture: hx smoke's run of a build's game, with the recorder (RX_CAPTURE) and a PNG of every present switched on, written
as one record run. The capture itself (the recorder's calls.rxc and blobs/) stays on the host, under the record root; the
run names it by hashes, counts and sizes, and holds the game's log and one screenshot per present.

    hx capture --device D [--build REV] [--frames N]
    hx capture --device D [--build REV] --scene NAME [--seconds S] [--at-scene]
    hx capture --device D [--build REV] --scene NAME --at-scene --presents N [--after K]
    hx capture --trim RUN [--keep-registered] --device D

The client half (on this PC) checks the device and the build, then starts a check job (jobs.py) that runs the in-job half
on the host. That waits for the hold, gathers the run's facts before the game starts (so a device the machine does not
list refuses the check with nothing run), launches the game once through the control bridge with the job's own
hookshot_ctl.py, waits for the frames, quits the game and waits for it to end (the recorder closes at exit), checks the
verdict and writes the run. A failing verdict, and a launch that fails, write their run too, marked FAIL. Each run's
shots folder (the renderer's PNGs) is removed once the run is written; the run keeps its screenshots.

With --scene, the in-job half plays the suite's scene as hx run plays it (hx_run.play): the game starts with the scene's
save and slot, the scene's route is played and its window held, and the run names the scene, the suite's sha256 and the
window's first and last replayed presents. The build's game runs whatever the scene's cache, and --frames does not
apply: the scene is played for --seconds S (default 20) in place of the suite's seconds, and its frames are S times
hx_run.FPS.

With --at-scene (with --scene), the recording starts at the scene rather than at the game's start. The game starts with
RX_CAPTURE_START naming the job's capture-start file, absent at launch; the scene is played as hx run plays it, and the
file is made once the route has arrived, just before the window (a load scene: before its arrive), and the next present,
which writes the snapshot and holds the game while it does, is waited for before the window begins. The run names
at_scene and start_present, the present the game's log says the capture started at, and its screenshots are the live
presents from that one on, renumbered from 1. A log without that start line FAILs the run.

With --presents N (with --at-scene), the window is N presents after the start rather than the scene's seconds: the game
gets RX_CAPTURE_PRESENTS=N, so the recorder keeps N presents from its start. The run names presents_recorded (N), after
(the --after K, 0 by default) and end_present, the present the game's log says the capture ended at; it must be the
start's present + N - 1, or the run FAILs (so does a log with no such line). With --after K, one wait of K presents
comes before the start file is made, so the capture starts K presents later than it would. The screenshots are the N
presents the capture recorded, renumbered from 1. --presents needs --at-scene, --after needs --presents, and --seconds
does not apply with --presents: each is refused with exit 2 before any remote call.

With --trim RUN (with --device D, and --keep-registered when given), no game starts: a check job on the host writes RUN's
working set as a new capture run. The new capture folder's calls.rxc is RUN's, byte for byte, except that each POOL page and
PAGES entry outside the read set names the zero page; its blobs/ holds the zero page and hard links to the blobs the new
calls.rxc names. The read set is the pages each submit reads (analysis.py's scan), the storage of each texture a present,
nvn_read_texels or nvn_write_texels names, and with --keep-registered the storage of each texture nvn_register_texture
names. The run is RUN's capture.json with its new capture, calls and blobs, trimmed_from and keep_registered, and RUN's
screenshots. The run's build and device are RUN's, so --device must name RUN's device; --scene, --frames, --build and the
scene flags do not apply.

Exit status: 0 pass, 1 FAIL (the verdict), 2 the check could not run (an unknown device, a build that is missing or
refused, an unknown or held-out scene, a suite that cannot be read, a job that will not start, a run that cannot be
written; for --trim, a run that is not a capture or a capture analysis.py refuses, with nothing written). The client
returns the job's status; 255 is ssh lost, and the job goes on.

run_facts, present_shots and SWITCHES are also what hx play uses.
"""
import argparse
import bisect
import dataclasses
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import struct
import subprocess
import sys

import analysis
import bridge
import config
import game
import hx_build
import hx_run
import hx_smoke
import jobs
import record
import remote
import scenes

SWITCHES = {"RX_VK_SHOT_EVERY": "1", "RX_VK_PIPE_SKIP": "0"}
START_FILE = "capture-start"
PRESENT_SHOT = re.compile(r"vk_([0-9]{6})\.png")
START_LINE = re.compile(r"rx capture: started at present ([0-9]+)")
END_LINE = re.compile(r"rx capture: ended at present ([0-9]+)")
COMMAND_TIMEOUT = 60
CHUNK = 1 << 20
DEFAULT_SECONDS = 20
GPU_GUEST = 68                   # the CALL kind of gpu_guest: 64 + its MkwVkHost field (host/rx_capture.h)


class CaptureError(Exception):
    pass


def positive_seconds(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError("not a number of seconds (at least 1): %r" % text)
    return value


def non_negative(text):
    try:
        value = int(text)
    except ValueError:
        value = -1
    if value < 0:
        raise argparse.ArgumentTypeError("not a number of presents (at least 0): %r" % text)
    return value


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx capture", description="Run a build's game headless on one device with the "
                                                               "recorder and a PNG of every present on, and write the run.")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--build", metavar="REV", help="the build of this checkout's commit REV (default: the newest build)")
    p.add_argument("--frames", type=hx_smoke.positive,
                   help="presents to wait for (default %d; not with --scene)" % hx_smoke.DEFAULT_FRAMES)
    p.add_argument("--scene", metavar="NAME", help="capture a scene of the suite, played as hx run plays it")
    p.add_argument("--seconds", type=positive_seconds, metavar="S",
                   help="the scene's length in seconds (default %d; with --scene)" % DEFAULT_SECONDS)
    p.add_argument("--at-scene", action="store_true",
                   help="start the recording at the scene, once its route has arrived (with --scene)")
    p.add_argument("--presents", type=hx_smoke.positive, metavar="N",
                   help="record N presents from the recording's start, the window (with --at-scene)")
    p.add_argument("--after", type=non_negative, metavar="K",
                   help="wait K presents before the recording starts (with --presents; default 0)")
    p.add_argument("--trim", metavar="RUN", help="write RUN's working set as a new capture run (with --device; no game)")
    p.add_argument("--keep-registered", action="store_true",
                   help="keep the storage of each texture nvn_register_texture names (with --trim)")
    return p.parse_args(argv)


def parse_in_job(argv):
    p = argparse.ArgumentParser(prog="hx capture --in-job", description="The in-job half, run inside a check job on the "
                                                                       "host.")
    p.add_argument("job", metavar="JOB", help="the job's folder")
    p.add_argument("--hold", required=True, metavar="PATH", help="the hold file: no launch while it exists")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--frames", type=hx_smoke.positive, help="presents to wait for (a run without --scene)")
    p.add_argument("--scene", metavar="NAME", help="the scene to play (with --root, and without --frames)")
    p.add_argument("--seconds", type=positive_seconds, metavar="S",
                   help="the scene's length in seconds, in place of the suite's (with --scene)")
    p.add_argument("--at-scene", action="store_true",
                   help="start the recording at the scene, once its route has arrived (with --scene)")
    p.add_argument("--presents", type=hx_smoke.positive, metavar="N",
                   help="record N presents from the recording's start, the window (with --at-scene)")
    p.add_argument("--after", type=non_negative, metavar="K",
                   help="wait K presents before the recording starts (with --presents; default 0)")
    p.add_argument("--root", metavar="DIR", help="the harness root: its scenes folder holds the suite (with --scene)")
    p.add_argument("--env", type=hx_smoke.pair, action="append", metavar="NAME=VALUE",
                   help="the device's environment for the game")
    p.add_argument("--device", required=True, metavar="D", help="the device's name in the configuration")
    p.add_argument("--capture", required=True, metavar="DIR", help="the capture's folder: new, under the record root")
    p.add_argument("--record", required=True, metavar="DIR", help="the record root")
    p.add_argument("--build-folder", required=True, metavar="DIR", help="the build's folder on the host")
    p.add_argument("--closure", required=True, metavar="PATH", help="the build's closure, a /nix/store path")
    p.add_argument("--trim", metavar="RUN", help="write RUN's working set as a new capture run (instead of a launch)")
    p.add_argument("--keep-registered", action="store_true",
                   help="keep the storage of each texture nvn_register_texture names (with --trim)")
    a = p.parse_args(argv)
    if a.trim is not None:
        if (a.scene is not None or a.frames is not None or a.root is not None or a.seconds is not None or a.at_scene
                or a.presents is not None or a.after is not None):
            p.error("--trim takes no scene, frames or scene flags")
        return a
    if a.keep_registered:
        p.error("--keep-registered applies only with --trim")
    scene_run = a.scene is not None
    if (a.root is not None) != scene_run or (a.frames is not None) == scene_run:
        p.error("a scene run takes --scene and --root; any other run takes --frames")
    if a.seconds is not None and not scene_run:
        p.error("--seconds applies only with --scene")
    if a.at_scene and not scene_run:
        p.error("--at-scene applies only with --scene")
    if a.presents is not None and not a.at_scene:
        p.error("--presents applies only with --at-scene")
    if a.after is not None and a.presents is None:
        p.error("--after applies only with --presents")
    if a.seconds is not None and a.presents is not None:
        p.error("--seconds does not apply with --presents: the window is the presents")
    return a


def main(argv):
    try:
        if argv[:1] == ["--in-job"]:
            return in_job(parse_in_job(argv[1:]))
        return client(parse_client(argv))
    except (CaptureError, hx_run.RunError, hx_smoke.SmokeError, scenes.SceneError, config.ConfigError, jobs.JobError,
            record.RecordError, analysis.AnalysisError, OSError) as e:
        print("capture: %s" % e, file=sys.stderr)
        return 2


def client(a):
    if a.trim is None and a.keep_registered:
        raise CaptureError("--keep-registered does not apply without --trim: it keeps the textures a trim keeps")
    if a.trim is not None:
        return trim_client(a)
    if a.scene is not None and a.frames is not None:
        raise CaptureError("--frames does not apply with --scene: the scene's seconds are its frames")
    if a.scene is None and a.seconds is not None:
        raise CaptureError("--seconds does not apply without --scene: a run without --scene takes --frames")
    if a.scene is None and a.at_scene:
        raise CaptureError("--at-scene does not apply without --scene: it starts the recording at a scene")
    if a.presents is not None and not a.at_scene:
        raise CaptureError("--presents does not apply without --at-scene: the presents are counted from the recording's "
                           "start")
    if a.after is not None and a.presents is None:
        raise CaptureError("--after does not apply without --presents: it waits before the recording's presents")
    if a.presents is not None and a.seconds is not None:
        raise CaptureError("--seconds does not apply with --presents: the window is the presents")
    cfg = config.load()
    device = device_of(cfg, a.device)
    folder, rev_sha = hx_smoke.build_folder(cfg, a.build)
    sha, closure = read_build_checked(cfg, folder, rev_sha)
    print("build: %s" % sha)
    job_id = jobs.new_id("check")
    job = jobs.job_dir(cfg, job_id)
    record_root = posixpath.join(cfg.root, "results")
    capture = posixpath.join(record_root, "captures", job_id)
    frames = [] if a.scene is not None else ["--frames", str(a.frames or hx_smoke.DEFAULT_FRAMES)]
    argv = [cfg.enter_closure, closure, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "capture", "--in-job", job, "--hold", cfg.hold, "--expect", device.expect, *frames,
            "--device", a.device, "--capture", capture, "--record", record_root, "--build-folder", folder,
            "--closure", closure]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    if a.scene is not None:
        argv += ["--scene", a.scene, "--root", cfg.root]
        if a.presents is None:
            argv += ["--seconds", str(a.seconds or DEFAULT_SECONDS)]
        if a.at_scene:
            argv.append("--at-scene")
        if a.presents is not None:
            argv += ["--presents", str(a.presents)]
        if a.after is not None:
            argv += ["--after", str(a.after)]
    jobs.start(cfg, job_id, argv, game=posixpath.join(folder, "game"))
    print("job: %s" % job_id)
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("capture: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def trim_client(a):
    """hx capture --trim: refuses what needs no host (the flags that do not apply, the run id), then reads the run on the host
    and refuses a run that is not a capture or was captured on another device, then starts a check job that writes the trimmed
    run. What only the host can see (whether analysis.py reads the capture) is refused by the job, before it writes."""
    refused = [flag for flag, value in (("--scene", a.scene), ("--frames", a.frames), ("--build", a.build),
                                        ("--seconds", a.seconds), ("--presents", a.presents), ("--after", a.after))
               if value is not None] + (["--at-scene"] if a.at_scene else [])
    if refused:
        raise CaptureError("--trim takes only RUN, --keep-registered and --device; %s does not apply with it"
                           % ", ".join(refused))
    cfg = config.load()
    device = device_of(cfg, a.device)
    doc = read_run(cfg, a.trim)
    refusal = trim_refusal(a.trim, doc, a.device)
    if refusal is not None:
        raise CaptureError(refusal)
    build = doc.get("build")
    if not isinstance(build, str) or not record.GIT_COMMIT.fullmatch(build):
        raise CaptureError("run %s names no build commit: %r" % (a.trim, build))
    folder = jobs.build_dir(cfg, build)
    sha, closure = read_build_checked(cfg, folder, build)
    print("build: %s" % sha)
    job_id = jobs.new_id("check")
    job = jobs.job_dir(cfg, job_id)
    record_root = posixpath.join(cfg.root, "results")
    capture = posixpath.join(record_root, "captures", job_id)
    argv = [cfg.enter_closure, closure, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "capture", "--in-job", job, "--hold", cfg.hold, "--expect", device.expect, "--trim", a.trim]
    if a.keep_registered:
        argv.append("--keep-registered")
    argv += ["--device", a.device, "--capture", capture, "--record", record_root, "--build-folder", folder,
             "--closure", closure]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    jobs.start(cfg, job_id, argv)
    print("job: %s" % job_id)
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("capture: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def device_of(cfg, name):
    """The configuration's device named name; an unknown one is refused"""
    device = cfg.devices.get(name)
    if device is None:
        raise CaptureError("no device %r; the devices are: %s" % (name, ", ".join(sorted(cfg.devices))))
    return device


def read_build_checked(cfg, folder, rev_sha):
    """The sha and closure of the build in folder, from its build.txt on the host; the sha must be rev_sha when one is given"""
    info = hx_smoke.read_build(cfg, folder)
    sha, closure = info.get("sha"), info.get("closure")
    if not sha or not closure:
        raise CaptureError("%s: build.txt needs sha and closure lines" % folder)
    if not record.GIT_COMMIT.fullmatch(sha) or not record.NIX_STORE_PATH.fullmatch(closure):
        raise CaptureError("%s: build.txt's sha must be a 40-hex commit and its closure a /nix/store/ path" % folder)
    if rev_sha is not None and sha != rev_sha:
        raise CaptureError("the build in %s says sha %s, not %s" % (folder, sha, rev_sha))
    return sha, closure


def read_run(cfg, run_id):
    """A run's capture.json, read on the host"""
    if not record.RUN_ID.fullmatch(run_id):
        raise CaptureError("not a run id: %r" % run_id)
    path = posixpath.join(cfg.root, "results", "runs", run_id, "files", "capture.json")
    try:
        text = remote.run(cfg.host, "cat " + shlex.quote(path)).stdout
    except remote.RemoteError as e:
        raise CaptureError("run %s: cannot read its capture.json: %s" % (run_id, e)) from None
    return parse_run(run_id, text)


def parse_run(run_id, text):
    """A run's capture.json text as its document, which must be an object"""
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise CaptureError("run %s: its capture.json is not JSON: %s" % (run_id, e)) from None
    if not isinstance(doc, dict):
        raise CaptureError("run %s: its capture.json is not an object" % run_id)
    return doc


def trim_refusal(run_id, doc, device):
    """Why the run cannot be trimmed on device, or None: it is a capture run, captured on that device"""
    if "kind" in doc or not isinstance(doc.get("capture"), str):
        return "run %s is not a capture run" % run_id
    if doc.get("device") != device:
        return "run %s was captured on %r, not %r" % (run_id, doc.get("device"), device)
    return None


def in_job(a):
    """The in-job half: it waits for the hold, gathers the run's facts, launches the game once with the recorder and the
    renderer switches on, waits for the frames (or, with --scene, plays the scene), quits the game, and writes the run once
    the game has ended, then removes the run's shots folder. A scene is loaded before anything is written, so an unknown or
    held-out one stops before the game."""
    if a.trim is not None:
        return trim_in_job(a)
    jobs.wait_hold(a.hold)
    loaded = None if a.scene is None else hx_run.load_scene(a.root, a.scene, False)
    device_env = dict(a.env or [])
    facts = run_facts(a.build_folder, os.path.join(a.job, "game"), a.closure, {**device_env, **SWITCHES}, a.expect)
    shots = os.path.join(a.capture, "shots")
    os.makedirs(shots)
    free = shutil.disk_usage(a.capture).free
    captures = {"RX_CAPTURE": a.capture, "RX_VK_SHOTS": shots, **SWITCHES}
    if loaded is None:
        env = jobs.launch_env(a.job, device_env)
        env.update(captures)
        status, why, log = game_run(a.job, env, a.expect, a.frames)
        frames, scene_fields, taken = a.frames, {}, present_shots(shots)
    else:
        _, suite, scene = loaded
        if a.seconds is not None:
            scene = dataclasses.replace(scene, seconds=a.seconds)
        start = os.path.join(a.job, START_FILE) if a.at_scene else None
        extra = captures if start is None else {**captures, "RX_CAPTURE_START": start}
        if a.presents is not None:
            extra = {**extra, "RX_CAPTURE_PRESENTS": str(a.presents)}
        after = a.after or 0
        status, window, why, log, present, end = scene_run(a.job, a.root, scene, device_env, extra, a.expect, start,
                                                           a.presents, after)
        frames = scene.seconds * hx_run.FPS if a.presents is None else a.presents
        scene_fields = {"scene": scene.name, "suite_sha256": suite.sha256, "window": window, "seconds": scene.seconds}
        if start is None:
            taken = present_shots(shots)
        else:
            scene_fields.update(at_scene=True, start_present=present)
            if a.presents is not None:
                scene_fields.update(presents_recorded=a.presents, after=after, end_present=end)
            taken = {} if present is None else present_shots(shots, present, a.presents)
    if status is not None:
        print("device: %s" % status.get("renderer"))
        print("presents: %s" % status.get("frame"))
    verdict_text = "pass" if why is None else "FAIL: %s" % why
    print("capture: %s" % verdict_text)
    run_id = write_capture_run(a, facts, status, verdict_text, log, free, frames, scene_fields, taken)
    print("run: %s" % run_id)
    shutil.rmtree(shots)
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


def scene_run(job, root, scene, device_env, extra, expect, start, presents=None, after=0):
    """Starts the game with the scene's save and slot and extra in its environment, plays the scene, and quits the game. The
    scene is played as hx run does, or with start (an at-scene run) by play_at_scene, with presents (when given) as its
    window and after presents waited before its start. Returns the window's last status, the window (first: the first
    status's replayed presents plus one; last: the last status's; None when play did not return), why the run fails (None
    when it passes), the game's log (None when the game did not start), and, for an at-scene run, the present the log says
    the capture started at (None when it did not) and, with presents, the present it ended at (None when it did not). The
    log is read once the game has ended."""
    try:
        running = game.start(job, os.path.join(job, "game"), device_env, save=scenes.save_dir(root, scene.save),
                             slot=scene.slot, extra=extra)
    except game.GameError as e:
        return None, None, "launch: %s" % e, None, None, None
    last = window = why = None
    try:
        if start is None:
            first, last, _ = hx_run.play(running, scene, expect)
        else:
            first, last, _ = play_at_scene(running, scene, expect, start, presents, after)
        window = {"first": first["replayed"] + 1, "last": last["replayed"]}
    except hx_run.FAILURES + (CaptureError,) as e:
        why = str(e)
    try:
        running.quit(hx_run.QUIT_SECONDS)
    except hx_run.FAILURES as e:
        why = why or str(e)
    if why is None and fell_back(running.log):
        why = "the log shows the renderer fell back to another GPU (%s)" % hx_smoke.FALLBACK
    present = end = None
    if start is not None:
        present = log_present(running.log, START_LINE)
        if why is None and present is None:
            why = "the log has no 'rx capture: started at present' line: the capture did not start"
    if presents is not None:
        end = log_present(running.log, END_LINE)
        if why is None and end is None:
            why = "the log has no 'rx capture: ended at present' line: the capture did not end"
        elif why is None and end != present + presents - 1:
            why = ("the capture ended at present %d, not %d: %d presents from its start at present %d"
                   % (end, present + presents - 1, presents, present))
    return last, window, why, running.log, present, end


def play_at_scene(running, scene, expect, start, presents=None, after=0):
    """hx_run.play, with the capture's start file made once the route has arrived and the window's start (the hour, a
    moving scene's inputs: hx_run.start_window) is done: just before the window, or for a load scene just before its
    arrive, and the present that starts the capture waited for (start_capture). The window is presents presents when
    given, else the scene's seconds. The other requests and their order are hx_run.play's, so the window is played as the
    live one is. Returns what hx_run.play returns."""
    conn = running.connect()
    try:
        session = bridge.Session(conn)
        session.enter_play(hx_run.PRESSES_TO_PLAY)
        session.check_device(expect)
        for step in scene.route:
            hx_run.route_step(session, step)
        frames = scene.seconds * hx_run.FPS if presents is None else presents
        if scene.kind == "load":
            first = session.status()
            start_capture(session, start, after)
            here = session.arrive(scene.load_to)
            hx_run.start_window(running, session, scene)
            _, last = session.window(frames, here)
        else:
            hx_run.start_window(running, session, scene)
            here = session.status().get("map")
            start_capture(session, start, after)
            first, last = session.window(frames, here, allow_event=scene.kind == "cutscene")
        return first, last, here
    finally:
        conn.close()


def start_capture(session, start, after):
    """Waits for after presents when there are any, makes the capture's start file, then waits for the present that finds
    it. The recorder starts the capture at that present and writes the snapshot of every live pool and object there, holding
    the game for as long as that takes (43 s for a light scene on a desktop GPU), longer than one of the window's present
    waits allows; so the present is waited for with hx_run.WAIT_SECONDS. The file is made once, and a file already there
    (the job's is new) is refused: the capture would start before the scene."""
    if after:
        session.call("wait", event="present", frames=after, timeout=hx_run.WAIT_SECONDS)
    try:
        with open(start, "x", encoding="utf-8"):
            pass
    except OSError as e:
        raise CaptureError("cannot make the capture's start file %s: %s" % (start, e.strerror or e)) from None
    session.call("wait", event="present", frames=1, timeout=hx_run.WAIT_SECONDS)


def fell_back(log):
    """Whether the game's log shows the renderer fell back to another GPU"""
    with open(log, encoding="utf-8", errors="replace") as f:
        return any(hx_smoke.FALLBACK in line for line in f)


def log_present(log, pattern):
    """The present named by the first line of the game's log that pattern (START_LINE or END_LINE) matches, or None when no
    line does"""
    with open(log, encoding="utf-8", errors="replace") as f:
        for text in f:
            found = pattern.search(text)
            if found:
                return int(found.group(1))
    return None


def write_capture_run(a, facts, status, verdict_text, log, free, frames, scene_fields, screenshots):
    """Writes the run: capture.json (with scene_fields for a scene), the game's log when it launched, and the screenshots
    (the run's present/NNNNNN.png files, as present_shots names them). Returns the run's id."""
    doc = {
        "capture": a.capture,
        "build": facts["shas"]["hookshot"],
        "device": a.device,
        "frames": frames,
        "presents": None if status is None else status.get("frame"),
        "calls": file_entry(os.path.join(a.capture, "calls.rxc")),
        "blobs": blob_stats(os.path.join(a.capture, "blobs")),
        "free_bytes_before": free,
        "verdict": verdict_text,
        **scene_fields,
    }
    files = {"capture.json": (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")}
    if log is not None:
        with open(log, "rb") as f:
            files["game.log"] = f.read()
    run_id, _ = record.write_run(a.record, facts, files, screenshots, journal=record.write_journal)
    return run_id


def trim_in_job(a):
    """The in-job half of --trim: it waits for the hold, reads the run and repeats the refusals, and checks the run's build. It
    then reads the working set (working_set; a capture analysis.py refuses raises before anything is written) and writes the
    new capture: a new folder under the record root with the trimmed calls.rxc and blobs, and a run with that capture's
    capture.json and the run's screenshots (read before the folder is made). The folder is removed when anything fails
    before the run that names it is written."""
    jobs.wait_hold(a.hold)
    manifest, _ = record.read_manifest(a.record, a.trim)
    with open(os.path.join(a.record, "runs", a.trim, "files", "capture.json"), encoding="utf-8") as f:
        doc = parse_run(a.trim, f.read())
    refusal = trim_refusal(a.trim, doc, a.device)
    if refusal is not None:
        raise CaptureError(refusal)
    facts = run_facts(a.build_folder, os.path.join(a.build_folder, "game"), a.closure, dict(a.env or []), a.expect)
    if facts["shas"]["hookshot"] != doc.get("build"):
        raise CaptureError("the build in %s says sha %s, not the run's %s"
                           % (a.build_folder, facts["shas"]["hookshot"], doc.get("build")))
    shots = run_screenshots(a.record, a.trim, manifest)
    source = doc["capture"]
    pages = working_set(source, a.keep_registered)
    zero = bytes(analysis.PAGE)
    os.makedirs(a.capture)
    written = []

    def journal(line):   # (the run is written, and names the folder, once its journal line is due)
        written.append(True)
        record.write_journal(line)

    try:
        calls = os.path.join(a.capture, "calls.rxc")
        named = write_trimmed_calls(os.path.join(source, "calls.rxc"), calls, pages, zero)
        link_blobs(os.path.join(source, "blobs"), os.path.join(a.capture, "blobs"), named, zero)
        capture_doc = {
            **doc,
            "capture": a.capture,
            "calls": file_entry(calls),
            "blobs": blob_stats(os.path.join(a.capture, "blobs")),
            "trimmed_from": a.trim,
            "keep_registered": a.keep_registered,
        }
        files = {"capture.json": (json.dumps(capture_doc, indent=2, sort_keys=True) + "\n").encode("utf-8")}
        run_id, _ = record.write_run(a.record, facts, files, shots, journal=journal)
    except (OSError, record.RecordError):
        if not written:
            shutil.rmtree(a.capture, ignore_errors=True)
        raise
    print("run: %s" % run_id)
    return 0


def working_set(folder, keep_registered):
    """The pages of a capture that its trim keeps: the pages each submit reads, and the storage pages of the textures its
    present and texel calls name (and, with keep_registered, of those its register calls name). Each of those pages,
    page by page, is kept when it has a POOL or PAGES record; any other page is translated by the capture's gpu_guest
    answers (gpu_guests), and the guest pages it lands on are kept. The scan is analysis.py's, over every frame (the
    window analysis.analyze takes by default), so a capture analysis.py refuses raises AnalysisError here, before anything
    is written. The subclass keeps what the scan takes in its own methods: the ranges each submit reads go to _pages_read,
    and the calls' textures are those of _call."""
    guests = gpu_guests(folder)
    keys = sorted(guests)

    def landing(first, last, recorded):
        """The guest pages that the GPU pages first to last land on, for those with no record in recorded"""
        out = set()
        lo, hi = bisect.bisect_left(keys, first), bisect.bisect_right(keys, last)
        for page in keys[lo:hi]:
            if page not in recorded:
                out.update(guests[page])
        return out

    class WorkingSet(analysis._Scan):
        def __init__(self, presents):
            super().__init__(folder, 1, presents, False, presents)
            self.read = set()
            self.textures = set()

        def _pages_read(self, ranges):
            pages = super()._pages_read(ranges)
            self.read.update(pages)
            for addr, size in ranges:
                self.read.update(landing(addr >> 12, (addr + size - 1) >> 12, self.pages))
            return pages

        def _call(self, at, args, in_window):
            super()._call(at, args, in_window)
            kind = at[1]
            if kind in (analysis.CALL_PRESENT, analysis.CALL_READ_TEXELS, analysis.CALL_WRITE_TEXELS):
                key = self._tex_ref(args[0], at)
            elif kind == analysis.CALL_REGISTER_TEXTURE and keep_registered:
                key = self._tex_ref(args[1], at)
            else:
                return
            if key is not None:
                self.textures.add(key)

    _, presents = analysis._count(folder)
    scan = WorkingSet(presents)
    for index, kind, payload in analysis._records(folder, payloads=True):
        scan.record(index, kind, payload)
    pages = set(scan.read)
    for gpu, size, _ in scan.textures:
        if gpu and size:
            first, last = gpu >> 12, (gpu + size - 1) >> 12
            pages.update(page for page in range(first, last + 1) if page in scan.pages)
            pages.update(landing(first, last, scan.pages))
    return pages


def gpu_guests(folder):
    """The capture's gpu_guest answers, wherever they are in it, as {GPU page: the guest pages it lands on}. A CALL's
    argument is a GPU address, and its ANSWER (matched by seq) is the guest address the player translated it to; 0 is
    unmapped. The player maps a page linearly, so the page's first byte lands at the answer less the argument's offset
    in its page, and that byte's guest page is the one the page lands on (the answer for the page's own address when
    the offset is 0)."""
    args, guests = {}, {}
    for index, kind, payload in analysis._records(folder, payloads=True):
        at = (index, kind)
        if kind == GPU_GUEST:
            args[index] = analysis._arg(analysis._args(payload), 0, at)
        elif kind == GPU_GUEST | analysis.ANSWER:
            analysis._check_size(payload, 16, at)
            seq, result = struct.unpack("<QQ", payload)
            if seq in args and result:
                addr = args[seq]
                guests.setdefault(addr >> 12, set()).add((result - addr % analysis.PAGE) >> 12)
    return guests


def write_trimmed_calls(source, target, pages, zero):
    """Writes target, a new calls.rxc: source's records byte for byte, except that each POOL page and PAGES entry whose page is
    not in pages names the zero page (zero is its bytes). Returns the digests of the blobs the new file names: each OBJECT's
    and GUEST's, and each POOL and PAGES entry's. working_set has read source whole, so its records are sound."""
    zero_sha = hashlib.sha256(zero).digest()
    named = set()
    with open(source, "rb") as src, open(target, "xb") as out:
        out.write(src.read(analysis.HEADER.size))
        while True:
            head = src.read(analysis.RECORD.size)
            if not head:
                return named
            kind, total, _, _, _ = analysis.RECORD.unpack(head)
            payload = bytearray(src.read(total - analysis.RECORD.size))
            if kind == analysis.POOL:
                start, size = struct.unpack_from("<QQ", payload)
                first = start >> 12
                count = ((start + size - 1) >> 12) - first + 1 if size else 0
                for k in range(count):
                    _entry_digest(payload, 16 + 32 * k, first + k in pages, named, zero_sha)
            elif kind == analysis.PAGES:
                (count,) = struct.unpack_from("<Q", payload)
                for k in range(count):
                    (page,) = struct.unpack_from("<Q", payload, 8 + 40 * k)
                    _entry_digest(payload, 16 + 40 * k, page in pages, named, zero_sha)
            elif kind in (analysis.OBJECT, analysis.GUEST):
                named.add(bytes(payload[16:48]))
            out.write(head)
            out.write(payload)


def _entry_digest(payload, at, kept, named, zero_sha):
    """The 32-byte digest at payload[at:at+32] names the zero page unless its page is kept; it is named either way"""
    if not kept:
        payload[at:at + 32] = zero_sha
    named.add(bytes(payload[at:at + 32]))


def link_blobs(source, target, named, zero):
    """Makes target, a new blobs folder: the zero page's blob (zero is its bytes), and a hard link to each named blob of source
    other than the zero page's, which is written"""
    zero_sha = hashlib.sha256(zero).digest()
    os.mkdir(target)
    with open(os.path.join(target, zero_sha.hex()), "xb") as f:
        f.write(zero)
    for found in named:
        if found != zero_sha:
            os.link(os.path.join(source, found.hex()), os.path.join(target, found.hex()))


def run_screenshots(root, run_id, manifest):
    """A run's screenshots by name, read from the record root; each must hold the bytes its manifest names"""
    shots = {}
    for name, entry in manifest["screenshots"].items():
        with open(os.path.join(root, "screenshots", entry["sha256"]), "rb") as f:
            data = f.read()
        if record.sha256_bytes(data) != entry["sha256"]:
            raise CaptureError("run %s: screenshot %s is not the bytes it is named by" % (run_id, name))
        shots[name] = data
    return shots


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


def present_shots(shots_dir, from_present=None, count=None):
    """Each present's PNG the renderer wrote into shots_dir (vk_NNNNNN.png), by its name in a run: present/NNNNNN.png. With
    from_present (an at-scene run's capture start), only the presents from it on, each renumbered from 1: present N is
    present/000001.png; with count too, only the count presents from it on."""
    shots = {}
    for name in sorted(os.listdir(shots_dir)):
        found = PRESENT_SHOT.fullmatch(name)
        if not found:
            continue
        number = int(found.group(1))
        if from_present is not None:
            if number < from_present or (count is not None and number >= from_present + count):
                continue
            number = number - from_present + 1
        with open(os.path.join(shots_dir, name), "rb") as f:
            shots["present/%06d.png" % number] = f.read()
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
