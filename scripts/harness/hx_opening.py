"""hx opening: a new game taken to play by its opening's inputs alone, and its save folder kept in the scene folder's saves.

    hx opening --device D [--build REV]

The client half (on this PC) checks the device, reads the build's build.txt on the host and starts a check job. The job's
in-job half (on the host) waits for the hold, reads the opening file <root>/scenes/opening.toml, and refuses it or a save
folder that already exists. It starts the build's game with no save and the opening skipped (HOS_SKIP_OPENING=1), plays
the opening's steps through the control bridge (docs/control.md), confirms play on the device and finds a save slot the
run wrote (the game writes its first slot itself on reaching play). A pass copies the game's save folder into
<root>/scenes/saves/<save>. Once the game has started, its run is written as one record (runrecord.py), whatever the
verdict.

Exit status: 0 pass, 1 FAIL (the verdict, recorded), 2 the check could not run (an unknown device, a build that is missing
or refused, an opening file or save folder that stops the run, a game that does not start, a job that will not start).
The client returns the job's status; 255 is ssh lost, and the job goes on.
"""
import argparse
import getpass
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
import tomllib

import bridge
import config
import game as game_mod
import hx_smoke
import jobs
import metrics
import record
import runrecord
import scenes

OPENING = "opening.toml"
OPENING_KEYS = ("save", "steps")
STEP_KEYS = ("inputs", "wait", "state")
STATES = ("boot", "intro", "menu", "loading", "play", "unknown")
SAVE_NAME = re.compile(r"[A-Za-z0-9_-]+")
SKIP_OPENING = {"HOS_SKIP_OPENING": "1"}
WAIT_SECONDS = 1800   # a wait's or an input's timeout: a fresh build prepares its shaders at its first start
SETTLE_SECONDS = 120  # how long the game may take to be in play with no event or load after its steps
RUN_FILES = {"log": "log.txt", "frame_trace": "hookshot_frames.csv", "frame_log": "frametimes.csv"}


class OpeningError(Exception):
    """A check that cannot run: the message is its one line"""


class RunFailed(Exception):
    """A run that reached the game and failed: the message is the reason the verdict is FAIL"""


FAILURES = (bridge.BridgeError, game_mod.GameError, game_mod.ctl.Failed, RunFailed)


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx opening", description="Take a new game to play by its opening's inputs and keep its "
                                                             "save folder in the scene folder's saves.")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--build", metavar="REV", help="the build of this checkout's commit REV (default: the newest build)")
    return p.parse_args(argv)


def parse_in_job(argv):
    p = argparse.ArgumentParser(prog="hx opening --in-job", description="The in-job half, run inside a check job on the host.")
    p.add_argument("job", metavar="JOB", help="the job's folder")
    p.add_argument("--hold", required=True, metavar="PATH", help="the hold file: no launch while it exists")
    p.add_argument("--root", required=True, metavar="DIR", help="the harness root: its scenes folder holds the opening file")
    p.add_argument("--build-folder", required=True, metavar="DIR", help="the build's folder: its build.txt and dirty.diff")
    p.add_argument("--device", required=True, metavar="D", help="the device's name in the configuration")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--env", type=hx_smoke.pair, action="append", metavar="NAME=VALUE",
                   help="the device's environment for the game")
    return p.parse_args(argv)


def main(argv):
    if argv[:1] == ["--in-job"]:
        return in_job(parse_in_job(argv[1:]))
    try:
        return client(parse_client(argv))
    except (OpeningError, config.ConfigError, jobs.JobError, hx_smoke.SmokeError) as e:
        print("opening: %s" % e, file=sys.stderr)
        return 2


def client(a):
    cfg = config.load()
    device = cfg.devices.get(a.device)
    if device is None:
        raise OpeningError("no device %r; the devices are: %s" % (a.device, ", ".join(sorted(cfg.devices))))
    folder, rev_sha = hx_smoke.build_folder(cfg, a.build)
    info = hx_smoke.read_build(cfg, folder)
    sha, closure = info.get("sha"), info.get("closure")
    if not sha or not closure:
        raise OpeningError("%s: build.txt needs sha and closure lines" % folder)
    if rev_sha is not None and sha != rev_sha:
        raise OpeningError("the build in %s says sha %s, not %s" % (folder, sha, rev_sha))
    print("build: %s" % sha)
    job_id = jobs.new_id("check")
    job = jobs.job_dir(cfg, job_id)
    argv = [cfg.enter_closure, closure, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "opening", "--in-job", job, "--hold", cfg.hold, "--root", cfg.root, "--build-folder", folder,
            "--device", a.device, "--expect", device.expect]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    jobs.start(cfg, job_id, argv, game=posixpath.join(folder, "game"))
    print("job: %s" % job_id)
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("opening: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def in_job(a):
    """The in-job half. Returns 0 for a pass, 1 for a FAIL (recorded) and 2 when the check could not run."""
    jobs.wait_hold(a.hold)
    try:
        data, save, steps = load_opening(posixpath.join(scenes.scene_dir(a.root), OPENING))
        dest = scenes.save_dir(a.root, save)
        if os.path.lexists(dest):
            raise OpeningError("the save folder %s exists: a save is never overwritten" % dest)
    except OpeningError as e:
        print("opening: %s" % e, file=sys.stderr)
        return 2
    device_env = dict(a.env or [])
    launched = int(time.time())
    try:
        running = game_mod.start(a.job, posixpath.join(a.job, "game"), device_env, extra=SKIP_OPENING)
    except game_mod.GameError as e:
        print("opening: the game did not start: %s" % e, file=sys.stderr)
        return 2
    print("device: %s" % a.device)
    why, first, last, saves, slot = None, None, None, None, None
    try:
        first, last, saves, slot = observe(running, steps, a.expect, launched)
    except FAILURES as e:
        why = str(e)
    exit_status = None
    try:
        exit_status = running.quit()
    except game_mod.GameError as e:
        why = why or str(e)
    found = running.outputs()
    log, trace, frame_log = (text_of(found, name) for name in ("log", "frame_trace", "frame_log"))
    if why is None and log is None:
        why = "the run's log is missing, so the device fallback check cannot run"
    fallback = None if log is None else metrics.fallback(log)
    if why is None and fallback is not None:
        why = "the renderer fell back to another GPU: %s" % fallback
    if why is None:
        try:
            copy_saves(saves["path"], dest)
        except (OSError, game_mod.ctl.Failed) as e:
            why = "the save folder could not be copied to %s: %s" % (dest, e)
    kernel = kernel_text(launched)
    report = metrics.summary(log, trace, frame_log, kernel, exit_status)
    report["window"] = window_metrics(first, last, trace, frame_log)
    files = {RUN_FILES[name]: content for name, content in found.items()}
    if kernel is not None:
        files["kernel.txt"] = kernel.encode("utf-8")
    files["metrics.json"] = dump(report)
    files["opening.toml"] = data
    files["opening.json"] = dump({"save": save, "slot": slot, "verdict": "FAIL" if why else "pass", "reason": why,
                                  "exit_status": exit_status})
    if why:
        verdict = "opening: FAIL %s" % why
    else:
        verdict = "opening: pass: slot %s saved to %s" % (slot, dest)
    try:
        run_id, digest = write_run(a, game_mod.run_env(a.job, device_env, SKIP_OPENING), files)
    except (OSError, OpeningError, runrecord.RunRecordError, record.RecordError) as e:
        print("opening: the run is not recorded: %s" % e, file=sys.stderr)
        print(verdict)
        return 2
    print("run: %s" % run_id)
    print("manifest: %s" % digest)
    print(verdict)
    return 1 if why else 0


def load_opening(path):
    """The opening file's bytes, its save folder's name and its steps. OpeningError naming the file or the key refused."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise OpeningError("%s: cannot read it: %s" % (path, e.strerror or e)) from None
    save, steps = parse_opening(data, path)
    return data, save, steps


def parse_opening(data, where):
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise OpeningError("%s: not valid TOML: %s" % (where, e)) from None
    for key in doc:
        if key not in OPENING_KEYS:
            raise OpeningError("%s: unknown key %s" % (where, key))
    save = need(where, doc, "save")
    if not isinstance(save, str) or not SAVE_NAME.fullmatch(save):
        raise OpeningError("%s: save must be a folder name of letters, digits, _ or -: %r" % (where, save))
    steps = need(where, doc, "steps")
    if not isinstance(steps, list):
        raise OpeningError("%s: steps must be a list of tables" % where)
    return save, [parse_step(where, number, step) for number, step in enumerate(steps)]


def need(where, table, key):
    if key not in table:
        raise OpeningError("%s: missing key %s" % (where, key))
    return table[key]


def parse_step(where, number, step):
    """One step as (its kind, its value): a table with exactly one of STEP_KEYS, its value checked for that key"""
    label = "steps[%d]" % number
    if not isinstance(step, dict):
        raise OpeningError("%s: %s must be a table with exactly one of %s" % (where, label, ", ".join(STEP_KEYS)))
    if len(step) != 1:
        raise OpeningError("%s: %s has keys %s: a step has exactly one of %s"
                           % (where, label, ", ".join(step) or "none", ", ".join(STEP_KEYS)))
    ((kind, value),) = step.items()
    if kind == "inputs":
        if not isinstance(value, str) or not value.strip():
            raise OpeningError("%s: %s.inputs must be a non-empty bridge script" % (where, label))
    elif kind == "wait":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise OpeningError("%s: %s.wait must be a whole number of presents, at least 1: %r" % (where, label, value))
    elif kind == "state":
        if value not in STATES:
            raise OpeningError("%s: %s.state must be one of %s: %r" % (where, label, ", ".join(STATES), value))
    else:
        raise OpeningError("%s: %s has an unknown key %s" % (where, label, kind))
    return kind, value


def observe(running, steps, expect, launched):
    """Plays the steps, confirms play on expect's device and finds the save slot the run wrote. Returns the status once the
    game is in play with no event or load, the status after the saves were read, the saves reply and the slot's name.
    Raises the bridge's and the game's errors, or RunFailed when the run wrote no slot."""
    conn = running.connect()
    try:
        session = bridge.Session(conn)
        play(session, steps)
        first = session.settle(SETTLE_SECONDS)
        session.check_device(expect)
        saves = session.call("saves")
        last = session.status()
    finally:
        conn.close()
    if not isinstance(saves.get("path"), str):
        raise RunFailed("the game's saves reply names no folder")
    return first, last, saves, written_slot(saves, launched)


def play(session, steps):
    """Sends the opening's steps in order: an input script, a wait for presents, or a wait for a state, each with its timeout"""
    for kind, value in steps:
        if kind == "inputs":
            session.call("sequence", script=value, timeout=WAIT_SECONDS)
        elif kind == "wait":
            session.call("wait", event="present", frames=value, timeout=WAIT_SECONDS)
        else:
            session.call("wait", event="state", state=value, timeout=WAIT_SECONDS)


def written_slot(saves, launched):
    """The name of the newest save slot the run wrote: one whose mtime is at or after the launch's epoch second. RunFailed
    when there is none."""
    written = [s for s in saves.get("slots") or [] if isinstance(s.get("mtime"), int) and s["mtime"] >= launched]
    if not written:
        raise RunFailed("no save slot was written in this run (the launch was at %d)" % launched)
    return max(written, key=lambda s: s["mtime"])["slot"]


def copy_saves(src, dest):
    """The game's save folder copied whole to dest, which must not exist: copied beside it under a name of its own, then
    renamed into place. A link or a file that is not plain is refused (hookshot_ctl.plain_tree)."""
    game_mod.ctl.plain_tree(src)
    os.makedirs(posixpath.dirname(dest), exist_ok=True)
    part = dest + ".part"
    shutil.copytree(src, part)
    os.rename(part, dest)


def kernel_text(launched):
    """The kernel log since the launch, as journalctl prints it; None when it cannot be read"""
    try:
        done = subprocess.run(["journalctl", "-k", "--since", "@%d" % launched, "--no-pager"], capture_output=True)
    except OSError:
        return None
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None


def window_metrics(first, last, trace, frame_log):
    """The trace's and the frame log's stats over the frames from the first status's replayed count to the last's"""
    lo, hi = (first or {}).get("replayed"), (last or {}).get("replayed")
    if not isinstance(lo, int) or not isinstance(hi, int):
        return {"gap": "window: no status with a replayed count"}
    return {"first": lo, "last": hi,
            "trace": windowed("trace", trace, lambda text: trace_window(text, lo, hi)),
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


def text_of(found, name):
    """An output's text, or None when it was not written"""
    data = found.get(name)
    return None if data is None else data.decode("utf-8", "replace")


def dump(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def binaries(game_dir):
    """sha256sum's output for the game's binaries, run in the game's folder"""
    done = subprocess.run(["sha256sum", "hookshot", "hookshot_vk.so"], cwd=game_dir, capture_output=True, text=True)
    if done.returncode:
        raise OpeningError("sha256sum of the game's binaries failed: %s"
                           % (done.stderr.strip() or "exit status %d" % done.returncode))
    return done.stdout


def read_text(path, absent=None):
    """A file's text, or absent when the file does not exist and absent is given; the error otherwise"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        if absent is None:
            raise
        return absent


def write_run(a, env, files):
    """The run's record: its facts from the build folder, the game's binaries and the machine, written once. Returns the
    run's id and the sha256 of its manifest."""
    facts = runrecord.facts(read_text(posixpath.join(a.build_folder, "build.txt")),
                            read_text(posixpath.join(a.build_folder, "dirty.diff"), absent=""),
                            binaries(posixpath.join(a.job, "game")), env, a.expect, runrecord.machine_outputs(),
                            os.path.expanduser("~"), getpass.getuser())
    return runrecord.write(a.root, facts, files)
