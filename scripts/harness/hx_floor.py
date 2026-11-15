"""hx floor: one device's noise floor for one scene, measured as one bench job. The method's A-B-B-A plan (verdict.plan) runs
with one build in both arms: each timed run is an hx run --in-job in the build's closure, and each arm adds one correctness
run (the validation layer when vulkaninfo on the host lists it; no screenshot, DL185). The series waits for the hold before
every run and stops at the first run that does not pass; nothing is rerun. The floor is recorded as one record run
(floor.json) whose facts are the first run's.

    hx floor --device D --scene S [--build REV]

Exit status: 0 the floor is recorded; 1 the floor failed (recorded: a run did not pass, a run's frames or log cannot be read
back, or the baseline is void); 2 the floor could not run (an unknown device, scene or build, a suite or method that is
refused, a job that will not start, a record that is not written). The client returns the job's status; 255 is ssh lost, and
the job goes on.

run_argv, correctness_argv, launch, correctness_run, validation_available, run_series, in_job_parser, preamble, arm_runs and
read_floor are public: hx bench runs the same series and reads the same record. Every run, timed or correctness, is its own
process through its arm's closure (the closure-entry script replaces the environment with the closure's), so the in-job half
only launches runs and never runs one itself.
"""
import argparse
import dataclasses
import getpass
import json
import os
import posixpath
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import config
import game
import gate
import hx_run
import hx_smoke
import jobs
import metrics
import record
import remote
import runrecord
import scenes
import verdict

TIMED_ENV = {"HOS_NO_PACING": "1"}
VALIDATION_ENV = {"RX_VK_VALIDATION": "1"}
VALIDATION_LAYER = "VK_LAYER_KHRONOS_validation"
FLOOR_FILE = "floor.json"
ARMS = ("A", "B")
MEASURED = ("floor", "interval", "a_medians", "b_medians")
RUN_LINE = re.compile(r"run: ([0-9]{6})")
SHA256 = re.compile(r"[0-9a-f]{64}")


class FloorError(Exception):
    """A floor that cannot run or be read back: the message is its one line"""


class _Stop(Exception):
    """Raised before a wait once a run has not passed: the series ends there"""


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx floor", description="Measure one device's noise floor for one scene: the A-B-B-A "
                                                         "series of one build as one bench job, and its record.")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--scene", required=True, metavar="S", help="a scene of the suite")
    p.add_argument("--build", metavar="REV", help="the build of this checkout's commit REV (default: the newest build)")
    return p.parse_args(argv)


def in_job_parser(prog, description):
    """The in-job half's options: hx bench's in-job parser is this one with its own --floor added"""
    p = argparse.ArgumentParser(prog=prog, description=description)
    p.add_argument("job", metavar="JOB", help="the job's folder")
    p.add_argument("--hold", required=True, metavar="PATH", help="the hold file: no launch while it exists")
    p.add_argument("--root", required=True, metavar="DIR", help="the harness root: its scenes folder holds the suite")
    p.add_argument("--enter-closure", required=True, metavar="PATH", help="the closure-entry script the runs start with")
    p.add_argument("--a-folder", required=True, metavar="DIR", help="arm A's build folder")
    p.add_argument("--a-closure", required=True, metavar="PATH", help="arm A's closure")
    p.add_argument("--b-folder", required=True, metavar="DIR", help="arm B's build folder")
    p.add_argument("--b-closure", required=True, metavar="PATH", help="arm B's closure")
    p.add_argument("--device", required=True, metavar="D", help="the device's name in the configuration")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--env", type=hx_smoke.pair, action="append", metavar="NAME=VALUE",
                   help="the device's environment for the game")
    p.add_argument("--scene", required=True, metavar="NAME", help="the scene to run")
    return p


def parse_in_job(argv):
    return in_job_parser("hx floor --in-job", "The in-job half, run inside a bench job on the host.").parse_args(argv)


def parse_correctness_in_job(argv):
    p = argparse.ArgumentParser(prog="hx floor --correctness-in-job",
                                description="One correctness run of the floor, run as its own process through its arm's "
                                            "closure.")
    p.add_argument("job", metavar="SUB_JOB", help="the run's sub-job folder")
    p.add_argument("--root", required=True, metavar="DIR", help="the harness root: its scenes folder holds the suite")
    p.add_argument("--build-folder", required=True, metavar="DIR", help="the arm's build folder")
    p.add_argument("--device", required=True, metavar="D", help="the device's name in the configuration")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--env", type=hx_smoke.pair, action="append", metavar="NAME=VALUE",
                   help="the environment for the game")
    p.add_argument("--scene", required=True, metavar="NAME", help="the scene to run")
    return p.parse_args(argv)


def main(argv):
    if argv[:1] == ["--in-job"]:
        return in_job(parse_in_job(argv[1:]))
    if argv[:1] == ["--correctness-in-job"]:
        return correctness_in_job(parse_correctness_in_job(argv[1:]))
    try:
        a = parse_client(argv)
        cfg = config.load()
        folder, _, closure = resolve(cfg, a.build)
        return client("floor", cfg, a.device, a.scene, (folder, closure), (folder, closure))
    except (FloorError, config.ConfigError, verdict.MethodError, jobs.JobError) as e:
        print("hx floor: %s" % e, file=sys.stderr)
        return 2


def resolve(cfg, rev):
    """The host folder, sha and closure of the build of REV (the newest build when None). A build whose build.txt lacks
    either line, or names another commit than REV, is refused."""
    try:
        folder, rev_sha = hx_smoke.build_folder(cfg, rev)
        info = hx_smoke.read_build(cfg, folder)
    except hx_smoke.SmokeError as e:
        raise FloorError(str(e)) from None
    sha, closure = info.get("sha"), info.get("closure")
    if not sha or not closure:
        raise FloorError("%s: build.txt needs sha and closure lines" % folder)
    if rev_sha is not None and sha != rev_sha:
        raise FloorError("the build in %s says sha %s, not %s" % (folder, sha, rev_sha))
    return folder, sha, closure


def client(command, cfg, device_name, scene, a, b, extra=(), start=jobs.start, follow=jobs.follow):
    """Reads the suite on the host and its method, prints the builds, the method and the plan's run count, then starts one
    bench job that runs command's in-job half with both arms and follows it. a and b are (host folder, closure); extra
    are arguments appended to the job's argv after the scene. A device that is not configured, a suite that cannot be
    read, or a method that is refused stops before any job starts. Returns the job's status."""
    device = cfg.devices.get(device_name)
    if device is None:
        raise FloorError("no device %r; the devices are: %s" % (device_name, ", ".join(sorted(cfg.devices))))
    suite_path = posixpath.join(scenes.scene_dir(cfg.root), hx_run.SUITE)
    try:
        text = remote.run(cfg.host, "cat " + shlex.quote(suite_path)).stdout
    except remote.RemoteError as e:
        raise FloorError(str(e)) from None
    method = verdict.load_method(text.encode("utf-8"), suite_path)
    print("build A: %s" % a[0])
    print("build B: %s" % b[0])
    print("method: confidence %g, runs per arm %d, warm-up %g s, %d resamples, seed %d"
          % (method.confidence, method.runs_per_arm, method.warmup_seconds, method.resamples, method.seed))
    print("plan: %d runs (A-B-B-A timed runs, then one correctness run per arm)"
          % len(verdict.plan(method.runs_per_arm)))
    job_id = jobs.new_id("bench")
    job = jobs.job_dir(cfg, job_id)
    argv = [cfg.enter_closure, a[1], "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            command, "--in-job", job, "--hold", cfg.hold, "--root", cfg.root, "--enter-closure", cfg.enter_closure,
            "--a-folder", a[0], "--a-closure", a[1], "--b-folder", b[0], "--b-closure", b[1],
            "--device", device_name, "--expect", device.expect]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    argv += ["--scene", scene, *extra]
    start(cfg, job_id, argv)
    print("job: %s" % job_id)
    status = follow(cfg, job_id)
    if status == 255:
        print("hx floor: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def run_argv(enter_closure, closure, hx_py, sub_job, hold, root, build_folder, device, expect, env, scene):
    """The argv of one timed run: hx run --in-job in the build's closure, with the device's environment (env: NAME to VALUE,
    in its order) and the scene. The one place a timed run's launch argv is built."""
    argv = [enter_closure, closure, "python", "-I", hx_py, "run", "--in-job", sub_job, "--hold", hold, "--root", root,
            "--build-folder", build_folder, "--device", device, "--expect", expect]
    for name, value in env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    return argv + ["--scene", scene]


def correctness_argv(enter_closure, closure, hx_py, sub_job, root, build_folder, device, expect, env, scene):
    """The argv of one correctness run: hx floor --correctness-in-job in the arm's closure, with the environment (env: NAME
    to VALUE, in its order) and the scene. A correctness run is its own process like a timed run, for the same reason: the
    closure-entry script gives the process its closure's environment."""
    argv = [enter_closure, closure, "python", "-I", hx_py, "floor", "--correctness-in-job", sub_job, "--root", root,
            "--build-folder", build_folder, "--device", device, "--expect", expect]
    for name, value in env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    return argv + ["--scene", scene]


def launch(argv, run=subprocess.run, out=sys.stdout):
    """Runs one run's argv once and echoes its output to out. Returns its exit status and the id from its 'run: <id>' line,
    None when it has none. A command that cannot be started is status 2."""
    try:
        done = run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        print("hx floor: cannot run %s: %s" % (argv[0], e.strerror or e), file=out, flush=True)
        return 2, None
    text = done.stdout.decode("utf-8", "replace")
    out.write(text)
    out.flush()
    run_id = None
    for line in text.splitlines():
        found = RUN_LINE.fullmatch(line.strip())
        if found and run_id is None:
            run_id = found.group(1)
    return done.returncode, run_id


def validation_available(run=subprocess.run):
    """Whether this host's Vulkan lists the validation layer: vulkaninfo --summary names it. A missing program or a nonzero
    exit is not available."""
    try:
        done = run(["vulkaninfo", "--summary"], capture_output=True)
    except OSError:
        return False
    return done.returncode == 0 and VALIDATION_LAYER in done.stdout.decode("utf-8", "replace")


def correctness_run(sub_job, root, build_folder, device, expect, env, scene_name, start=game.start, play=hx_run.play,
                    machine=runrecord.machine_outputs, journal=record.write_journal):
    """hx run's in-job flow for one correctness run: the build's game copied into the sub-job (no warm copy is made), started
    with the scene's save and slot and env, played, and its window held. No screenshot is taken (DL185): the run is recorded
    with hx run's files and no screenshots, and its log is what the gate reads. Returns its status (0 pass, 1 FAIL
    recorded, 2 could not run) and its run id, None when it is not recorded. Nothing is retried."""
    job = os.path.abspath(sub_job)
    game_dir = os.path.join(job, "game")
    try:
        suite_data, suite, scene = hx_run.load_scene(root, scene_name, False)
        build_txt = hx_run.read_text(os.path.join(build_folder, "build.txt"))
        hx_run.copy_game(hx_run.game_source(build_folder, device, scene.cache), game_dir)
        hashes = hx_run.binary_hashes(game_dir)
    except (hx_run.RunError, scenes.SceneError) as e:
        print("hx floor: %s" % e, file=sys.stderr)
        return 2, None
    launched = int(time.time())
    try:
        running = start(job, game_dir, env, save=scenes.save_dir(root, scene.save), slot=scene.slot)
    except game.GameError as e:
        print("hx floor: the game did not start: %s" % e, file=sys.stderr)
        return 2, None
    print("device: %s" % device)
    why, first, last, here = None, None, None, None
    try:
        first, last, here = play(running, scene, expect)
    except (*hx_run.FAILURES, OSError) as e:
        why = str(e)
    exit_status = None
    try:
        exit_status = running.quit(hx_run.QUIT_SECONDS)
    except game.GameError as e:
        why = why or str(e)
    found = running.outputs()
    log, trace, frame_log = (hx_run.text_of(found, name) for name in ("log", "frame_trace", "frame_log"))
    if why is None and log is None:
        why = "the run's log is missing, so the device fallback check cannot run"
    fallback = None if log is None else metrics.fallback(log)
    if why is None and fallback is not None:
        why = "the renderer fell back to another GPU: %s" % fallback
    kernel = hx_run.kernel_text(launched)
    report = metrics.summary(log, trace, frame_log, kernel, exit_status)
    report["window"] = hx_run.window_metrics(first, last, trace, frame_log)
    files = {hx_run.RUN_FILES[name]: content for name, content in found.items()}
    if kernel is not None:
        files["kernel.txt"] = kernel.encode("utf-8")
    files["metrics.json"] = hx_run.dump(report)
    scene_fields = hx_run.scene_report(argparse.Namespace(device=device, final=False), scene, suite, first, last, here,
                                       exit_status, why)
    files["scene.json"] = hx_run.dump(scene_fields)
    files[hx_run.SUITE] = suite_data
    try:
        facts = runrecord.facts(build_txt, hx_run.read_diff(build_folder), hashes, game.run_env(job, env), expect,
                                machine(), os.path.expanduser("~"), getpass.getuser())
        facts["suite_sha256"] = suite.sha256
        run_id, _ = record.write_run(runrecord.results_dir(root), facts, files, {}, journal)
    except (OSError, runrecord.RunRecordError, record.RecordError) as e:
        print("hx floor: the run is not recorded: %s" % e, file=sys.stderr)
        return 2, None
    print("run: %s" % run_id)
    print("correctness: %s" % ("FAIL %s" % why if why else "pass"))
    return (1 if why else 0), run_id


def correctness_in_job(a):
    """hx floor --correctness-in-job: one correctness run, as its own process. Returns the run's status."""
    status, _ = correctness_run(a.job, a.root, a.build_folder, a.device, a.expect, dict(a.env or []), a.scene)
    return status


def run_series(a, method, wait, launch=launch):
    """The plan's runs in order. Before each run, wait() (the hold: it returns the seconds it held). Each run is launched
    as its own process through its arm's closure: a timed run is run_argv's hx run --in-job with the device's environment
    and TIMED_ENV; a correctness run is correctness_argv's hx floor --correctness-in-job with the same environment and, when
    the validation layer is listed, VALIDATION_ENV. Each run has a sub-job folder of its own, runs/<index>, with data/ and
    run/ (mode 0700), and loses its game folder after it runs. The first run whose status is not 0, or whose folder cannot
    be made or cleared, ends the series: nothing is rerun. Returns the entries of the runs that ran (a run whose folder
    could not be made has no id and status 2), whether the validation layer was used, and why the series ended early
    (None when every run passed)."""
    job = os.path.abspath(a.job)
    hx_py = os.path.join(job, "tools", "scripts", "harness", "hx.py")
    builds = {"A": (a.a_folder, a.a_closure), "B": (a.b_folder, a.b_closure)}
    device_env = {**dict(a.env or []), **TIMED_ENV}
    validation = validation_available()
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
            if kind == "timed":
                argv = run_argv(a.enter_closure, closure, hx_py, sub, a.hold, a.root, folder, a.device, a.expect,
                                device_env, a.scene)
            else:
                env = {**device_env, **VALIDATION_ENV} if validation else device_env
                argv = correctness_argv(a.enter_closure, closure, hx_py, sub, a.root, folder, a.device, a.expect, env,
                                        a.scene)
            status, run_id = launch(argv)
            game_dir = os.path.join(sub, "game")
            if os.path.lexists(game_dir):
                shutil.rmtree(game_dir)
        except OSError as e:
            failure = "run %d (arm %s, %s): its folder: %s" % (index, arm, kind, e)
        entries.append({"index": index, "arm": arm, "kind": kind, "held_s": holds[-1], "run": run_id,
                        "status": status})
        if failure is None and status != 0:
            failure = "run %d (arm %s, %s) ended with status %d" % (index, arm, kind, status)
        return status

    try:
        verdict.series(verdict.plan(method.runs_per_arm), run_one, pause)
    except _Stop:
        pass
    return {"entries": entries, "validation": validation, "failure": failure}


def write_verdict(root, name, payload, suite_data, first_run, journal=record.write_journal):
    """One record run under the record root: the payload as name (sorted, indented JSON) and the suite file's bytes, with the
    first run's facts. With no first run, every fact is a gap. Returns the run's id."""
    results = runrecord.results_dir(root)
    if first_run is None:
        facts = {key: {"gap": "no run of the series was recorded"} for key in record.FACT_KEYS}
    else:
        manifest, _ = record.read_manifest(results, first_run)
        facts = manifest.get("facts") if isinstance(manifest, dict) else None
        if not isinstance(facts, dict):
            raise FloorError("run %s: its manifest has no facts" % first_run)
    files = {name: hx_run.dump(payload), hx_run.SUITE: suite_data}
    run_id, _ = record.write_run(results, facts, files, {}, journal)
    return run_id


def _manifest(results, run_id):
    """A run's manifest, which must name its files and screenshots: FloorError otherwise"""
    try:
        manifest, _ = record.read_manifest(results, run_id)
    except record.RecordError as e:
        raise FloorError(str(e)) from None
    if not (isinstance(manifest, dict) and isinstance(manifest.get("files"), dict)
            and isinstance(manifest.get("screenshots"), dict)):
        raise FloorError("run %s: its manifest does not name files and screenshots" % run_id)
    return manifest


def _checked(path, entry, shown):
    """The bytes of the file at path, checked against its manifest entry's sha256 and size. FloorError when the entry is not
    a sha256 and size, or the file is missing, unreadable or changed."""
    if not (isinstance(entry, dict) and isinstance(entry.get("sha256"), str) and SHA256.fullmatch(entry["sha256"])
            and isinstance(entry.get("size"), int)):
        raise FloorError("%s: its manifest entry is not a sha256 and size" % shown)
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise FloorError("%s: cannot be read: %s" % (shown, e.strerror or e)) from None
    if record.sha256_bytes(data) != entry["sha256"] or len(data) != entry["size"]:
        raise FloorError("%s: does not match its manifest" % shown)
    return data


def _run_file(results, run_id, manifest, name):
    """One of a run's files, read back and checked against its manifest entry"""
    if name not in manifest["files"]:
        raise FloorError("run %s: its manifest does not name %s" % (run_id, name))
    return _checked(Path(results) / "runs" / run_id / "files" / name, manifest["files"][name],
                    "run %s, %s" % (run_id, name))


def run_frames(results, run_id):
    """A timed run's frame times in ms over its window: the frames of the frame log from the window's first replayed count to
    its last, as scene.json gives them (the window hx run writes)"""
    manifest = _manifest(results, run_id)
    try:
        scene = json.loads(_run_file(results, run_id, manifest, "scene.json").decode("utf-8"))
    except ValueError as e:
        raise FloorError("run %s: its scene.json is not JSON: %s" % (run_id, e)) from None
    window = scene.get("window") if isinstance(scene, dict) else None
    lo, hi = (window or {}).get("first"), (window or {}).get("last")
    if not isinstance(lo, int) or not isinstance(hi, int):
        raise FloorError("run %s: its scene has no window of replayed frames" % run_id)
    text = _run_file(results, run_id, manifest, hx_run.RUN_FILES["frame_log"]).decode("utf-8", "replace")
    try:
        rows = metrics.window(metrics.frame_times(text), lo, hi)
    except metrics.MetricsError as e:
        raise FloorError("run %s: its frame log: %s" % (run_id, e)) from None
    if not rows:
        raise FloorError("run %s: its frame log has no frames from %d to %d" % (run_id, lo, hi))
    return [row["frametime_ms"] for row in rows]


def run_log(results, run_id):
    """A run's renderer log as text"""
    manifest = _manifest(results, run_id)
    return _run_file(results, run_id, manifest, hx_run.RUN_FILES["log"]).decode("utf-8", "replace")


def read_floor(results, run_id):
    """A recorded run's floor.json, read back and checked against its manifest, as its payload. FloorError when the run does
    not name it, the file is changed, or it is not a JSON object."""
    manifest = _manifest(results, run_id)
    try:
        payload = json.loads(_run_file(results, run_id, manifest, FLOOR_FILE).decode("utf-8"))
    except ValueError as e:
        raise FloorError("run %s: its floor.json is not JSON: %s" % (run_id, e)) from None
    if not isinstance(payload, dict):
        raise FloorError("run %s: its floor.json is not a JSON object" % run_id)
    return payload


def arm_runs(entries):
    """A series' run ids: each arm's timed runs in order, and each arm's correctness run. FloorError when a run printed
    no id."""
    unnamed = [e for e in entries if e["run"] is None]
    if unnamed:
        e = unnamed[0]
        raise FloorError("run %d (arm %s, %s) printed no run id" % (e["index"], e["arm"], e["kind"]))
    timed = {arm: [e["run"] for e in entries if e["kind"] == "timed" and e["arm"] == arm] for arm in ARMS}
    correct = {e["arm"]: e["run"] for e in entries if e["kind"] == "correctness"}
    return timed, correct


def measure(results, entries, method):
    """The floor's measured values from a series whose every run passed: the noise floor and its interval over the two arms'
    timed runs, and each run's median warm frame time. A void baseline (a flash in either correctness log, a validation
    error only in B, or a hang in any log) is a FloorError naming it. A refused value raises FloorError or VerdictError."""
    timed, correct = arm_runs(entries)
    a_runs = [run_frames(results, run) for run in timed["A"]]
    b_runs = [run_frames(results, run) for run in timed["B"]]
    low, high = verdict.bootstrap(a_runs, b_runs, method)
    reasons = gate.void_reasons(run_log(results, correct["A"]), run_log(results, correct["B"]),
                                [run_log(results, run) for run in timed["A"] + timed["B"]])
    if reasons:
        raise FloorError("the baseline is void: " + "; ".join(reasons))
    return {"floor": verdict.noise_floor(a_runs, b_runs, method), "interval": [low, high],
            "a_medians": _medians(a_runs, method), "b_medians": _medians(b_runs, method)}


def _medians(runs, method):
    return [statistics.median(verdict.warm(run, method.warmup_seconds)) for run in runs]


def build_sha(text, folder):
    """The sha of a build.txt's sha line"""
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "sha" and value.strip():
            return value.strip()
    raise FloorError("%s: build.txt needs a sha line" % folder)


def preamble(a):
    """The in-job half's reads before any run: the suite's bytes, the suite, its method, and each arm's build sha from its
    build.txt. Raises FloorError, hx_run.RunError, scenes.SceneError or verdict.MethodError."""
    suite_data, suite, _ = hx_run.load_scene(a.root, a.scene, False)
    method = verdict.load_method(suite_data, suite.path)
    a_sha = build_sha(hx_run.read_text(os.path.join(a.a_folder, "build.txt")), a.a_folder)
    b_sha = build_sha(hx_run.read_text(os.path.join(a.b_folder, "build.txt")), a.b_folder)
    return suite_data, suite, method, a_sha, b_sha


def in_job(a):
    """The in-job half. It reads the scene and the method from the suite and the arms' shas from their build.txt, runs the
    series (the hold before every run), measures the floor when every run passed, and records the floor run. Returns 0 when
    the floor is recorded, 1 when it failed and is recorded, 2 when it could not run."""
    try:
        suite_data, suite, method, a_sha, b_sha = preamble(a)
    except (hx_run.RunError, scenes.SceneError, verdict.MethodError, FloorError) as e:
        print("hx floor: could not run: %s" % e, file=sys.stderr)
        return 2
    series = run_series(a, method, lambda: jobs.wait_hold(a.hold))
    entries, reason, measured = series["entries"], series["failure"], None
    if reason is None:
        try:
            measured = measure(runrecord.results_dir(a.root), entries, method)
        except (FloorError, verdict.VerdictError) as e:
            reason = str(e)
    payload = {
        "kind": "floor", "outcome": "failed" if reason else "recorded", "reason": reason,
        "device": a.device, "expect": a.expect, "scene": a.scene, "a_sha": a_sha, "b_sha": b_sha,
        "suite_sha256": suite.sha256, "method": dataclasses.asdict(method), "validation": series["validation"],
        "series": entries, **(measured or {key: None for key in MEASURED}),
    }
    first = next((e["run"] for e in entries if e["run"] is not None), None)
    try:
        run_id = write_verdict(a.root, FLOOR_FILE, payload, suite_data, first)
    except (OSError, FloorError, record.RecordError) as e:
        print("hx floor: the floor is not recorded: %s" % e, file=sys.stderr)
        return 2
    print("floor: %s" % run_id)
    if reason is not None:
        print("floor: FAIL %s" % reason)
        return 1
    print("noise floor %.2f%% (interval %.2f%% to %.2f%%)"
          % (measured["floor"] * 100, measured["interval"][0] * 100, measured["interval"][1] * 100))
    print("floor: recorded")
    return 0
