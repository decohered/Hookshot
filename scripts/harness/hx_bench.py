"""hx bench: the A-B-B-A series of two builds of this checkout as one bench job, on one device and scene, judged against a
recorded floor of that device, scene and method (hx floor's record). The series is hx floor's (hx_floor.run_series): each run
once, through its arm's closure, waiting for the hold before each. The bench is void when the gate voids it (gate.void_reasons
over the floor's image tolerance, both correctness runs and every timed log), failed when the series ended early or a run
cannot be read back, and else the verdict's call against the floor's noise floor (verdict.verdict). One record run
(bench.json) is written with the verdict, or the reasons it has none.

    hx bench A B --device D --scene S --floor ID

A and B are commits of this checkout whose builds are on the host; ID is the six-digit record run of a recorded floor.

Exit status: 0 the verdict (faster, slower or no change) is recorded; 1 the bench is void or failed (recorded); 2 the bench
could not run (an unknown device, scene or build, a floor that is refused, a job that will not start, a record that is not
written). The client returns the job's status; 255 is ssh lost, and the job goes on.
"""
import argparse
import dataclasses
import shutil
import sys

import config
import gate
import hx_floor
import hx_run
import jobs
import record
import runrecord
import scenes
import verdict

BENCH_FILE = "bench.json"
BENCH_MEASURED = ("interval", "a_medians", "b_medians")
VERDICTS = ("faster", "slower", "no change")


def record_id(text):
    if not record.RUN_ID.fullmatch(text):
        raise argparse.ArgumentTypeError("not a six-digit record run id: %r" % text)
    return text


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx bench",
                                description="Run the A-B-B-A series of two builds as one bench job and judge it against a "
                                            "recorded floor.")
    p.add_argument("a", metavar="A", help="the commit of build A: a commit of this checkout")
    p.add_argument("b", metavar="B", help="the commit of build B: a commit of this checkout")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--scene", required=True, metavar="S", help="a scene of the suite")
    p.add_argument("--floor", required=True, type=record_id, metavar="ID",
                   help="the record run of a recorded floor of this device, scene and method (six digits)")
    return p.parse_args(argv)


def parse_in_job(argv):
    p = hx_floor.in_job_parser("hx bench --in-job", "The in-job half, run inside a bench job on the host.")
    p.add_argument("--floor", required=True, type=record_id, metavar="ID",
                   help="the record run of the floor to judge against")
    return p.parse_args(argv)


def main(argv):
    if argv[:1] == ["--in-job"]:
        return in_job(parse_in_job(argv[1:]))
    try:
        a = parse_client(argv)
        cfg = config.load()
        a_folder, _, a_closure = hx_floor.resolve(cfg, a.a)
        b_folder, _, b_closure = hx_floor.resolve(cfg, a.b)
        return hx_floor.client("bench", cfg, a.device, a.scene, (a_folder, a_closure), (b_folder, b_closure),
                               extra=("--floor", a.floor))
    except (hx_floor.FloorError, config.ConfigError, verdict.MethodError, jobs.JobError) as e:
        print("hx bench: %s" % e, file=sys.stderr)
        return 2


def check_floor(results, a, method):
    """The recorded floor the bench is judged against: a floor of this device, scene and method, recorded, with its noise
    floor and image tolerance. FloorError otherwise, before any run."""
    floor = hx_floor.read_floor(results, a.floor)
    if floor.get("kind") != "floor":
        raise hx_floor.FloorError("run %s is not a floor" % a.floor)
    if floor.get("outcome") != "recorded":
        raise hx_floor.FloorError("floor %s has outcome %r, not 'recorded'" % (a.floor, floor.get("outcome")))
    for key, want in (("device", a.device), ("scene", a.scene), ("method", dataclasses.asdict(method))):
        if floor.get(key) != want:
            raise hx_floor.FloorError("floor %s is for %s %r, not %r" % (a.floor, key, floor.get(key), want))
    image = floor.get("image")
    if not (isinstance(floor.get("floor"), (int, float)) and isinstance(image, dict)
            and "metric" in image and "tolerance" in image):
        raise hx_floor.FloorError("floor %s has no noise floor, or no image with a metric and tolerance" % a.floor)
    return floor


def judge(results, series, floor, method, flip):
    """The bench's outcome as (outcome, reason, void_reasons, measured). failed, with its reason, when the series did not run
    through or a run cannot be read back; void, with the gate's reasons, when the gate voids it; else the verdict's call
    with its interval and medians as measured. A GateError is one void reason: its own message."""
    if series["failure"] is not None:
        return "failed", series["failure"], [], None
    try:
        timed, correct = hx_floor.arm_runs(series["entries"])
        a_runs = [hx_floor.run_frames(results, run) for run in timed["A"]]
        b_runs = [hx_floor.run_frames(results, run) for run in timed["B"]]
        a_shots = hx_floor.run_shots(results, correct["A"])
        b_shots = hx_floor.run_shots(results, correct["B"])
        a_log = hx_floor.run_log(results, correct["A"])
        b_log = hx_floor.run_log(results, correct["B"])
        timed_logs = [hx_floor.run_log(results, run) for run in timed["A"] + timed["B"]]
    except hx_floor.FloorError as e:
        return "failed", str(e), [], None
    try:
        reasons = gate.void_reasons(floor["image"], a_shots, b_shots, a_log, b_log, timed_logs, flip)
    except gate.GateError as e:
        reasons = [str(e)]
    if reasons:
        return "void", None, reasons, None
    try:
        found = verdict.verdict(a_runs, b_runs, floor["floor"], method)
    except verdict.VerdictError as e:
        return "failed", str(e), [], None
    measured = {"interval": [found["low"], found["high"]], "a_medians": found["a_medians"],
                "b_medians": found["b_medians"]}
    return found["verdict"], None, [], measured


def in_job(a):
    """The in-job half. It refuses a floor that is not a recorded floor of this device, scene and method before any run
    (exit 2), runs the series, judges it and records one bench run. Returns 0 for a verdict, 1 for a void or failed bench
    (recorded), and 2 when it could not run."""
    results = runrecord.results_dir(a.root)
    try:
        suite_data, suite, method, a_sha, b_sha = hx_floor.preamble(a)
        floor = check_floor(results, a, method)
    except (hx_run.RunError, scenes.SceneError, verdict.MethodError, hx_floor.FloorError) as e:
        print("hx bench: could not run: %s" % e, file=sys.stderr)
        return 2
    series = hx_floor.run_series(a, method, lambda: jobs.wait_hold(a.hold))
    outcome, reason, void_reasons, measured = judge(results, series, floor, method, shutil.which("flip"))
    entries = series["entries"]
    payload = {
        "kind": "bench", "outcome": outcome, "reason": reason, "void_reasons": void_reasons,
        "device": a.device, "expect": a.expect, "scene": a.scene, "a_sha": a_sha, "b_sha": b_sha,
        "suite_sha256": suite.sha256, "method": dataclasses.asdict(method), "floor_run": a.floor,
        "floor": floor["floor"], "image": floor["image"], "validation": series["validation"], "series": entries,
        **(measured or {key: None for key in BENCH_MEASURED}),
    }
    first = next((e["run"] for e in entries if e["run"] is not None), None)
    try:
        run_id = hx_floor.write_verdict(a.root, BENCH_FILE, payload, suite_data, first)
    except (OSError, hx_floor.FloorError, record.RecordError) as e:
        print("hx bench: the bench is not recorded: %s" % e, file=sys.stderr)
        return 2
    print("bench: %s" % run_id)
    if outcome == "failed":
        print("verdict: failed (%s)" % reason)
    elif outcome == "void":
        print("verdict: void (%s)" % "; ".join(void_reasons))
    else:
        low, high = measured["interval"]
        print("verdict: %s (interval %+.2f%% to %+.2f%%, floor %.2f%%)"
              % (outcome, low * 100, high * 100, floor["floor"] * 100))
    return 0 if outcome in VERDICTS else 1
