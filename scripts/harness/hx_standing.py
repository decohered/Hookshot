"""hx standing: two builds of this checkout, their A-B-B-A timed runs as one bench job, each run under a bridgeless driver that
presses through HOS_INPUT_LIVE, and the interval between them, recorded as unconfirmed. The driver reads the game's status
lines from its log: v2.0.14 has no control bridge, so nothing here waits on a socket (docs/control.md).

    hx standing A B --device D --scene S

A and B are commits of this checkout whose builds are on the host. The client starts one bench job. Its in-job half
(standing --in-job) runs the method's timed runs, A-B-B-A, each as its own process through its arm's closure
(standing --run-in-job), waiting for the hold before each and stopping at the first run that does not pass. Each run is a
record run (standing_run.json, log.txt, hookshot_frames.csv, kernel.txt). The comparison is one record run, standing.json:
unconfirmed, with the interval of B against A and each run's warm median, and no faster, slower, pass or fail call (R2).

Exit status: 0 the comparison is recorded; 1 a run did not pass (recorded, and the series ends there); 2 the standing could
not run. The client returns the job's status; 255 is ssh lost, and the job goes on.
"""
import argparse
import dataclasses
import getpass
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

import config
import game
import hx_floor
import hx_run
import hx_smoke
import jobs
import metrics
import record
import remote
import runrecord
import scenes
import verdict

PRESS = "0:A"           # a press of A, as a line of the live-input file
MAX_PRESSES = 30
PLAY_DRAWS = 150        # draws a frame at play, at least
PLAY_LINES = 3          # status lines in a row that show PLAY_DRAWS
SETTLE_PRESENTS = 600   # presents waited after play before the window
FLUSH_PRESENTS = 120    # presents past the window before the game is ended: the trace is flushed every 60 rows
POLL_SECONDS = 0.5      # between two looks at the game's log
TERM_SECONDS = 10       # SIGTERM's grace before the leader is left to SIGKILL
STATUS = re.compile(r"vk: ([0-9]+) presents, .*?a frame: ([0-9]+) draws")
STANDING_FILE = "standing.json"
STANDING_RUN_FILE = "standing_run.json"
KERNEL_FILE = "kernel.txt"
MEASURED = ("interval", "a_medians", "b_medians")
ARMS = ("A", "B")


class StandingError(Exception):
    """A standing that cannot be read or measured: the message is its one line"""


class RunFailed(Exception):
    """A run that does not pass: its message is the reason it is recorded with"""


class _Stop(Exception):
    """Raised before a wait once a run has not passed: the series ends there"""


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx standing",
                                description="Run two builds' A-B-B-A timed runs as one bench job under a bridgeless driver, "
                                            "and record the interval between them as unconfirmed.")
    p.add_argument("a", metavar="A", help="the commit of build A: a commit of this checkout")
    p.add_argument("b", metavar="B", help="the commit of build B: a commit of this checkout")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--scene", required=True, metavar="S", help="a scene of the suite")
    return p.parse_args(argv)


def parse_in_job(argv):
    return hx_floor.in_job_parser("hx standing --in-job",
                                  "The in-job half, run inside a bench job on the host.").parse_args(argv)


def parse_run_in_job(argv):
    p = argparse.ArgumentParser(prog="hx standing --run-in-job",
                                description="One timed run of the standing series, run as its own process through its arm's "
                                            "closure.")
    p.add_argument("job", metavar="SUB_JOB", help="the run's sub-job folder")
    p.add_argument("--root", required=True, metavar="DIR", help="the harness root: its scenes folder holds the suite")
    p.add_argument("--build-folder", required=True, metavar="DIR", help="the arm's build folder")
    p.add_argument("--device", required=True, metavar="D", help="the device's name in the configuration")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--env", type=hx_smoke.pair, action="append", metavar="NAME=VALUE",
                   help="the device's environment for the game")
    p.add_argument("--scene", required=True, metavar="NAME", help="the scene to run")
    return p.parse_args(argv)


def main(argv):
    if argv[:1] == ["--in-job"]:
        return in_job(parse_in_job(argv[1:]))
    if argv[:1] == ["--run-in-job"]:
        return run_in_job(parse_run_in_job(argv[1:]))[0]
    try:
        a = parse_client(argv)
        cfg = config.load()
        a_folder, _, a_closure = hx_floor.resolve(cfg, a.a)
        b_folder, _, b_closure = hx_floor.resolve(cfg, a.b)
        return client(cfg, a.device, a.scene, (a_folder, a_closure), (b_folder, b_closure))
    except (hx_floor.FloorError, config.ConfigError, verdict.MethodError, jobs.JobError) as e:
        print("hx standing: %s" % e, file=sys.stderr)
        return 2


def timed_plan(runs_per_arm):
    """verdict.plan's timed runs only: A-B-B-A, runs_per_arm of each arm"""
    return [(arm, kind) for arm, kind in verdict.plan(runs_per_arm) if kind == "timed"]


def client(cfg, device_name, scene, a, b, start=jobs.start, follow=jobs.follow):
    """Reads the suite on the host and its method, prints the builds, the method and the plan's run count, then starts one
    bench job that runs standing's in-job half with both arms and follows it. a and b are (host folder, closure). A device
    that is not configured, a suite that cannot be read, or a method that is refused stops before any job starts. Returns
    the job's status."""
    device = cfg.devices.get(device_name)
    if device is None:
        raise hx_floor.FloorError("no device %r; the devices are: %s" % (device_name, ", ".join(sorted(cfg.devices))))
    suite_path = posixpath.join(scenes.scene_dir(cfg.root), hx_run.SUITE)
    try:
        text = remote.run(cfg.host, "cat " + shlex.quote(suite_path)).stdout
    except remote.RemoteError as e:
        raise hx_floor.FloorError(str(e)) from None
    method = verdict.load_method(text.encode("utf-8"), suite_path)
    print("build A: %s" % a[0])
    print("build B: %s" % b[0])
    print("method: confidence %g, runs per arm %d, warm-up %g s, %d resamples, seed %d"
          % (method.confidence, method.runs_per_arm, method.warmup_seconds, method.resamples, method.seed))
    print("plan: %d timed runs (A-B-B-A), each its own process" % len(timed_plan(method.runs_per_arm)))
    job_id = jobs.new_id("bench")
    job = jobs.job_dir(cfg, job_id)
    argv = [cfg.enter_closure, a[1], "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "standing", "--in-job", job, "--hold", cfg.hold, "--root", cfg.root, "--enter-closure", cfg.enter_closure,
            "--a-folder", a[0], "--a-closure", a[1], "--b-folder", b[0], "--b-closure", b[1],
            "--device", device_name, "--expect", device.expect]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    argv += ["--scene", scene]
    start(cfg, job_id, argv)
    print("job: %s" % job_id)
    status = follow(cfg, job_id)
    if status == 255:
        print("hx standing: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def run_argv(enter_closure, closure, hx_py, sub_job, root, build_folder, device, expect, env, scene):
    """The argv of one timed run: standing --run-in-job in the arm's closure, with the device's environment (env: NAME to
    VALUE, in its order) and the scene"""
    argv = [enter_closure, closure, "python", "-I", hx_py, "standing", "--run-in-job", sub_job, "--root", root,
            "--build-folder", build_folder, "--device", device, "--expect", expect]
    for name, value in env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    return argv + ["--scene", scene]


def standing_env(job, device_env):
    """The game's environment for a standing run: game.run_env's run conditions, with the device's variables and
    HOS_NO_PACING, and no control bridge (HOS_CONTROL: v2.0.14 has none to connect to)"""
    env = game.run_env(job, {**device_env, **hx_floor.TIMED_ENV})
    env.pop("HOS_CONTROL")
    return env


class Game:
    """A started game: its process, its log read by file offset, and its live-input file. status holds each complete status
    line so far, as (presents, draws)."""

    def __init__(self, proc, log_path, live_path, killpg):
        self.proc = proc
        self.log_path = log_path
        self.live_path = live_path
        self.killpg = killpg
        self.status = []
        self._offset = 0
        self._partial = b""

    def read_status(self):
        """Reads what the game has appended to its log since the last read, and keeps its complete status lines"""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(self._offset)
                data = f.read()
        except FileNotFoundError:
            return
        self._offset += len(data)
        *complete, self._partial = (self._partial + data).split(b"\n")
        for line in complete:
            found = STATUS.search(line.decode("utf-8", "replace"))
            if found:
                self.status.append((int(found.group(1)), int(found.group(2))))

    def press(self):
        with open(self.live_path, "a", encoding="ascii") as f:
            f.write(PRESS + "\n")

    def poll(self):
        return self.proc.poll()

    def end(self):
        """Ends the game's process group: SIGTERM, the leader given TERM_SECONDS to go, then SIGKILL to the group. A group
        with no members left is not an error. Returns the leader's exit status."""
        self._signal(signal.SIGTERM)
        try:
            self.proc.wait(TERM_SECONDS)
        except subprocess.TimeoutExpired:
            pass   # (the leader is still running: SIGKILL below ends it)
        self._signal(signal.SIGKILL)
        return self.proc.wait()

    def _signal(self, sig):
        try:
            self.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass   # (the group has no members left)


def start(job, game_dir, env, save, slot, popen=subprocess.Popen, killpg=os.killpg):
    """The game in game_dir under env, started as game.start starts it but without its bridge wait: its stdout and stderr to
    the job's run/log.txt, in a session of its own, stdin from /dev/null. The scene's save folder is copied into the job's
    data first (with slot, that slot alone). GameError when the game is missing, its save is refused or it will not start."""
    exe = os.path.join(game_dir, "hookshot")
    if not os.path.isfile(exe):
        raise game.GameError("no game at %s" % exe)
    run_dir = os.path.join(job, "run")
    os.makedirs(run_dir, mode=0o700, exist_ok=True)
    process_env = dict(os.environ)
    process_env.update(env)
    try:
        game.ctl.copy_save(save, process_env["XDG_DATA_HOME"], slot)
    except game.ctl.Failed as e:
        raise game.GameError(str(e)) from e
    log = os.path.join(run_dir, "log.txt")
    with open(log, "wb") as out:
        try:
            proc = popen([exe], cwd=game_dir, env=process_env, stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            raise game.GameError("the game would not start (%s): %s" % (exe, e)) from e
    return Game(proc, log, env["HOS_INPUT_LIVE"], killpg)


def playing(status):
    """Whether the last PLAY_LINES status lines all show at least PLAY_DRAWS draws a frame"""
    last = status[-PLAY_LINES:]
    return len(last) == PLAY_LINES and all(draws >= PLAY_DRAWS for _, draws in last)


class Play:
    """One run's driver over its game: the presses sent, the presents at play, and the window's first and last frame, each as
    far as the run has got. Every wait polls the game's status lines until its condition holds, within hx_run.WAIT_SECONDS
    of its start."""

    def __init__(self, running, clock, sleep):
        self.running = running
        self.clock = clock
        self.sleep = sleep
        self.presses = 0
        self.at_play = None
        self.first = None
        self.last = None

    def run(self, frames):
        self.until(lambda: len(self.running.status) >= 1, "the first status line")
        while not playing(self.running.status):
            if self.presses == MAX_PRESSES:
                raise RunFailed("no play after %d presses" % MAX_PRESSES)
            seen = len(self.running.status)
            self.running.press()
            self.presses += 1
            self.until(lambda: len(self.running.status) >= seen + 2, "two status lines after press %d" % self.presses)
        self.at_play = self.running.status[-1][0]
        settle = self.at_play + SETTLE_PRESENTS
        self.until(lambda: self.running.status[-1][0] >= settle, "presents %d (the settle)" % settle)
        self.first = self.running.status[-1][0]
        self.last = self.first + frames - 1
        flush = self.last + 1 + FLUSH_PRESENTS
        self.until(lambda: self.running.status[-1][0] >= flush, "presents %d (the trace's flush)" % flush)

    def until(self, ready, what):
        """Polls the game until ready() holds. RunFailed when the game ends first, or when hx_run.WAIT_SECONDS pass from the
        call."""
        deadline = self.clock() + hx_run.WAIT_SECONDS
        while True:
            self.running.read_status()
            if ready():
                return
            ended = self.running.poll()
            if ended is not None:
                self.running.read_status()
                if ready():
                    return
                raise RunFailed("the game ended (%d) before %s" % (ended, what))
            if self.clock() >= deadline:
                raise RunFailed("%s did not come within %d s" % (what, hx_run.WAIT_SECONDS))
            self.sleep(POLL_SECONDS)


def window_ms(text, first, last):
    """The ms column's values of the trace's rows whose frame is in [first, last], in order. The header names the frame and
    ms columns among others (the trace's other columns vary by version): a header without either, a row whose field count is
    not the header's, a frame or ms that is not a number, and an ms that is not finite are refused, and so is a window with no
    row. A last line without its newline is a write that was cut short, and is not a row. StandingError on any refusal."""
    rows = text.split("\n")[:-1]
    if not rows:
        raise StandingError("the trace has no header line")
    header = rows[0].split(",")
    if "frame" not in header or "ms" not in header:
        raise StandingError("the trace's header has no frame or no ms column")
    frame_at, ms_at = header.index("frame"), header.index("ms")
    values = []
    for number, line in enumerate(rows[1:], start=2):
        fields = line.split(",")
        if len(fields) != len(header):
            raise StandingError("line %d has %d fields, the header %d" % (number, len(fields), len(header)))
        try:
            frame, ms = int(fields[frame_at]), float(fields[ms_at])
        except ValueError:
            raise StandingError("line %d: its frame or ms is not a number" % number) from None
        if not math.isfinite(ms):
            raise StandingError("line %d: its ms is not finite" % number)
        if first <= frame <= last:
            values.append(ms)
    if not values:
        raise StandingError("the trace has no frames from %d to %d" % (first, last))
    return values


def run_reason(log, trace, expect, first, last):
    """Why a run whose game has ended is a FAIL, or None when it passes its checks: its log is missing, has no line
    'vk: <expect>, Vulkan' (its device), or says the renderer fell back to another GPU; or its trace is missing or has no frame
    in the window."""
    if log is None:
        return "the run's log is missing, so the device line cannot be checked"
    text = log.decode("utf-8", "replace")
    device = "vk: %s, Vulkan" % expect
    if not any(device in line for line in text.splitlines()):   # (each line carries the host's '[hos <thread>] ' prefix)
        return "the log has no line '%s'" % device
    fallback = metrics.fallback(text)
    if fallback is not None:
        return "the renderer fell back to another GPU: %s" % fallback
    if trace is None:
        return "the frame trace is missing"
    try:
        window_ms(trace.decode("utf-8", "replace"), first, last)
    except StandingError as e:
        return "the frame trace: %s" % e
    return None


def _read(path):
    """A file's bytes, or None when it was not written"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def run_in_job(a, popen=subprocess.Popen, killpg=os.killpg, clock=time.monotonic, sleep=time.sleep,
               kernel=hx_run.kernel_text, machine=runrecord.machine_outputs, journal=record.write_journal):
    """hx standing --run-in-job: one timed run, as its own process. The arm's cold game is copied into the sub-job and started
    with the scene's save and slot under standing_env; the driver plays it (Play) and ends its process group; then its log and
    trace are read, the device line, the fallback and the window are checked, and the run is recorded. Returns (status, run
    id): 0 pass, 1 FAIL (recorded), 2 the run could not run or is not recorded (run id None)."""
    job = os.path.abspath(a.job)
    game_dir = os.path.join(job, "game")
    try:
        _, suite, scene = hx_run.load_scene(a.root, a.scene, False)
        build_txt = hx_run.read_text(os.path.join(a.build_folder, "build.txt"))
        hx_run.copy_game(os.path.join(a.build_folder, "game"), game_dir)
        hashes = hx_run.binary_hashes(game_dir)
    except (hx_run.RunError, scenes.SceneError) as e:
        print("hx standing: %s" % e, file=sys.stderr)
        return 2, None
    env = standing_env(job, dict(a.env or []))
    launched = int(time.time())
    try:
        running = start(job, game_dir, env, scenes.save_dir(a.root, scene.save), scene.slot, popen, killpg)
    except game.GameError as e:
        print("hx standing: the game did not start: %s" % e, file=sys.stderr)
        return 2, None
    print("device: %s" % a.device)
    play = Play(running, clock, sleep)
    why = None
    try:
        play.run(scene.seconds * hx_run.FPS)
    except (RunFailed, OSError) as e:
        why = str(e)
    finally:
        exit_status = running.end()
    log = _read(running.log_path)
    trace = _read(os.path.join(game_dir, hx_run.RUN_FILES["frame_trace"]))
    kernel_text = kernel(launched)
    if why is None:
        why = run_reason(log, trace, a.expect, play.first, play.last)
    fields = {
        "kind": "standing_run", "device": a.device, "expect": a.expect, "scene": a.scene, "suite_sha256": suite.sha256,
        "presses": play.presses, "presents_at_play": play.at_play, "window": {"first": play.first, "last": play.last},
        "exit_status": exit_status, "verdict": "FAIL" if why else "pass", "reason": why,
    }
    files = {STANDING_RUN_FILE: hx_run.dump(fields)}
    if log is not None:
        files[hx_run.RUN_FILES["log"]] = log
    if trace is not None:
        files[hx_run.RUN_FILES["frame_trace"]] = trace
    if kernel_text is not None:
        files[KERNEL_FILE] = kernel_text.encode("utf-8")
    try:
        facts = runrecord.facts(build_txt, hx_run.read_diff(a.build_folder), hashes, env, a.expect, machine(),
                                os.path.expanduser("~"), getpass.getuser())
        facts["suite_sha256"] = suite.sha256
        run_id, _ = runrecord.write(a.root, facts, files, journal)
    except (OSError, runrecord.RunRecordError, record.RecordError) as e:
        print("hx standing: the run is not recorded: %s" % e, file=sys.stderr)
        return 2, None
    print("run: %s" % run_id)
    print("standing run: %s" % ("FAIL %s" % why if why else "pass"))
    return (1 if why else 0), run_id


def run_series(a, method, wait, launch=hx_floor.launch):
    """The method's timed runs in order. Before each run, wait() (the hold: it returns the seconds it held). Each run is
    launched as its own process through its arm's closure: run_argv's standing --run-in-job with the device's environment.
    Each run has a sub-job folder of its own, runs/<index>, with data/ and run/ (mode 0700), and loses its game folder after
    it runs. The first run whose status is not 0, or whose folder cannot be made or cleared, ends the series: nothing is
    rerun. Returns the entries of the runs that ran (a run whose folder could not be made has no id and status 2) and why
    the series ended early (None when every run passed)."""
    job = os.path.abspath(a.job)
    hx_py = os.path.join(job, "tools", "scripts", "harness", "hx.py")
    builds = {"A": (a.a_folder, a.a_closure), "B": (a.b_folder, a.b_closure)}
    env = dict(a.env or [])
    entries, holds = [], []
    failure = None

    def pause():
        if failure is not None:
            raise _Stop()
        holds.append(wait())

    def run_one(index, arm, kind):
        nonlocal failure
        folder, closure = builds[arm]
        sub = os.path.join(job, "runs", "%02d" % index)
        status, run_id = 2, None
        try:
            os.makedirs(os.path.join(sub, "data"))
            os.makedirs(os.path.join(sub, "run"), mode=0o700)
            argv = run_argv(a.enter_closure, closure, hx_py, sub, a.root, folder, a.device, a.expect, env, a.scene)
            status, run_id = launch(argv)
            game_dir = os.path.join(sub, "game")
            if os.path.lexists(game_dir):
                shutil.rmtree(game_dir)
        except OSError as e:
            failure = "run %d (arm %s): its folder: %s" % (index, arm, e)
        entries.append({"index": index, "arm": arm, "kind": kind, "held_s": holds[-1], "run": run_id, "status": status})
        if failure is None and status != 0:
            failure = "run %d (arm %s) ended with status %d" % (index, arm, status)
        return status

    try:
        verdict.series(timed_plan(method.runs_per_arm), run_one, pause)
    except _Stop:
        pass
    return {"entries": entries, "failure": failure}


def read_back(results, run_id, name):
    """A run's file, read back from the record and checked against its manifest entry: StandingError when the manifest does
    not name it, or the file is missing or has changed"""
    try:
        manifest, _ = record.read_manifest(results, run_id)
    except record.RecordError as e:
        raise StandingError(str(e)) from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    entry = files.get(name) if isinstance(files, dict) else None
    if not (isinstance(entry, dict) and isinstance(entry.get("sha256"), str) and isinstance(entry.get("size"), int)):
        raise StandingError("run %s: its manifest does not name %s" % (run_id, name))
    try:
        data = (Path(results) / "runs" / run_id / "files" / name).read_bytes()
    except OSError as e:
        raise StandingError("run %s, %s: cannot be read: %s" % (run_id, name, e.strerror or e)) from None
    if record.sha256_bytes(data) != entry["sha256"] or len(data) != entry["size"]:
        raise StandingError("run %s, %s: does not match its manifest" % (run_id, name))
    return data


def run_frames(results, run_id):
    """A timed run's frame times in ms over its window: the ms column of its trace between the window's first and last frame,
    as its standing_run.json gives them"""
    try:
        payload = json.loads(read_back(results, run_id, STANDING_RUN_FILE).decode("utf-8"))
    except ValueError as e:
        raise StandingError("run %s: its standing_run.json is not JSON: %s" % (run_id, e)) from None
    window = payload.get("window") if isinstance(payload, dict) else None
    if not isinstance(window, dict) or not (isinstance(window.get("first"), int) and isinstance(window.get("last"), int)):
        raise StandingError("run %s: its standing_run.json has no window of frames" % run_id)
    text = read_back(results, run_id, hx_run.RUN_FILES["frame_trace"]).decode("utf-8", "replace")
    try:
        return window_ms(text, window["first"], window["last"])
    except StandingError as e:
        raise StandingError("run %s: %s" % (run_id, e)) from None


def _medians(runs, method):
    return [statistics.median(verdict.warm(run, method.warmup_seconds)) for run in runs]


def measure(results, entries, method):
    """The standing's measured values from a series whose every run passed: the interval of B against A over the arms' timed
    runs (verdict.bootstrap), and each run's warm median. StandingError or VerdictError when a value cannot be measured."""
    runs = {arm: [] for arm in ARMS}
    for e in entries:
        if e["run"] is None:
            raise StandingError("run %d (arm %s) printed no run id" % (e["index"], e["arm"]))
        runs[e["arm"]].append(run_frames(results, e["run"]))
    low, high = verdict.bootstrap(runs["A"], runs["B"], method)
    return {"interval": [low, high], "a_medians": _medians(runs["A"], method),
            "b_medians": _medians(runs["B"], method)}


def in_job(a, launch=hx_floor.launch, journal=record.write_journal):
    """The in-job half: reads the scene, the method and the arms' shas, runs the timed plan (the hold before each run, stopping
    at the first run that does not pass), measures the interval and the medians when every run passed, and records the
    comparison as standing.json. Returns 0 when it is recorded, 1 when a run did not pass (recorded), 2 when it could not
    run."""
    try:
        suite_data, suite, method, a_sha, b_sha = hx_floor.preamble(a)
    except (hx_run.RunError, scenes.SceneError, verdict.MethodError, hx_floor.FloorError) as e:
        print("hx standing: could not run: %s" % e, file=sys.stderr)
        return 2
    series = run_series(a, method, lambda: jobs.wait_hold(a.hold), launch)
    entries, reason, measured = series["entries"], series["failure"], None
    if reason is None:
        try:
            measured = measure(runrecord.results_dir(a.root), entries, method)
        except (StandingError, verdict.VerdictError) as e:
            reason = str(e)
    payload = {
        "kind": "standing", "unconfirmed": True, "reason": reason, "device": a.device, "expect": a.expect,
        "scene": a.scene, "a_sha": a_sha, "b_sha": b_sha, "suite_sha256": suite.sha256,
        "method": dataclasses.asdict(method), "series": entries,
        **(measured or {key: None for key in MEASURED}),
    }
    first = next((e["run"] for e in entries if e["run"] is not None), None)
    try:
        run_id = hx_floor.write_verdict(a.root, STANDING_FILE, payload, suite_data, first, journal=journal)
    except (OSError, hx_floor.FloorError, record.RecordError) as e:
        print("hx standing: the standing is not recorded: %s" % e, file=sys.stderr)
        return 2
    print("standing: %s" % run_id)
    if reason is not None:
        print("standing: the series ended early: %s" % reason)
        return 1
    low, high = measured["interval"]
    print("interval of B against A %+.2f%% to %+.2f%% (unconfirmed)" % (low * 100, high * 100))
    return 0
