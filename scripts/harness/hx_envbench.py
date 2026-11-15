"""hx envbench: one build benched against itself, with environment pairs added to arm B's runs only. The A-B-B-A series of one
build is one bench job, both arms the same build folder and closure. Each --b-env pair is added to arm B's timed and
correctness runs and to no arm A run (hx_calibrate.planted). The series is judged as hx bench judges it: against a recorded
floor of this device, scene and method (hx_bench.check_floor), with hx_bench.judge. One record run (envbench.json) is
written with the verdict, or the reasons it has none, and the pairs as its b_env. No pass or fail is attached: the verdict
call (faster, slower or no change) is the measurement.

    hx envbench --device D --scene S --floor ID --b-env NAME=VALUE [--b-env ...] [--build REV]

ID is the six-digit record run of a recorded floor of this device, scene and method; REV is the build of this checkout's
commit (default: the newest build). Each NAME is given once.

Exit status: 0 the verdict (faster, slower or no change) is recorded; 1 the envbench is void or failed (recorded); 2 it could
not run (an unknown device, scene or build, a floor that is refused, a job that will not start, a record that is not
written). The client returns the job's status; 255 is ssh lost, and the job goes on.
"""
import argparse
import dataclasses
import sys

import config
import hx_bench
import hx_calibrate
import hx_floor
import hx_run
import hx_smoke
import jobs
import record
import runrecord
import scenes
import verdict

ENVBENCH_FILE = "envbench.json"


def check_pairs(p, pairs):
    """Each --b-env name once: a name given twice is a usage error, since one of its values would silently win"""
    names = [name for name, _ in pairs]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        p.error("each --b-env name once; given more than once: %s" % ", ".join(repeated))


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx envbench",
                                description="Bench one build against itself, with environment pairs added to arm B's runs "
                                            "only, and judge it against a recorded floor.")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--scene", required=True, metavar="S", help="a scene of the suite")
    p.add_argument("--floor", required=True, type=hx_bench.record_id, metavar="ID",
                   help="the record run of a recorded floor of this device, scene and method (six digits)")
    p.add_argument("--b-env", required=True, action="append", type=hx_smoke.pair, metavar="NAME=VALUE",
                   help="an environment pair added to arm B's runs only (repeat for more)")
    p.add_argument("--build", metavar="REV", help="the build of this checkout's commit REV (default: the newest build)")
    a = p.parse_args(argv)
    check_pairs(p, a.b_env)
    return a


def parse_in_job(argv):
    p = hx_floor.in_job_parser("hx envbench --in-job", "The in-job half, run inside a bench job on the host.")
    p.add_argument("--floor", required=True, type=hx_bench.record_id, metavar="ID",
                   help="the record run of the floor to judge against")
    p.add_argument("--b-env", required=True, action="append", type=hx_smoke.pair, metavar="NAME=VALUE",
                   help="the environment pair added to arm B's runs only")
    a = p.parse_args(argv)
    check_pairs(p, a.b_env)
    return a


def main(argv):
    if argv[:1] == ["--in-job"]:
        return in_job(parse_in_job(argv[1:]))
    try:
        a = parse_client(argv)
        cfg = config.load()
        folder, _, closure = hx_floor.resolve(cfg, a.build)
        extra = ["--floor", a.floor]
        for name, value in a.b_env:
            extra += ["--b-env", "%s=%s" % (name, value)]
        return hx_floor.client("envbench", cfg, a.device, a.scene, (folder, closure), (folder, closure), extra=extra)
    except (hx_floor.FloorError, config.ConfigError, verdict.MethodError, jobs.JobError) as e:
        print("hx envbench: %s" % e, file=sys.stderr)
        return 2


def in_job(a):
    """The in-job half. It refuses a floor that is not a recorded floor of this device, scene and method before any run
    (exit 2), runs the series with the pairs on arm B's runs, judges it and records one envbench run. Returns 0 for a
    verdict, 1 for a void or failed envbench (recorded), and 2 when it could not run."""
    results = runrecord.results_dir(a.root)
    try:
        suite_data, suite, method, a_sha, b_sha = hx_floor.preamble(a)
        floor = hx_bench.check_floor(results, a, method)
    except (hx_run.RunError, scenes.SceneError, verdict.MethodError, hx_floor.FloorError) as e:
        print("hx envbench: could not run: %s" % e, file=sys.stderr)
        return 2
    plan = verdict.plan(method.runs_per_arm)
    series = hx_floor.run_series(a, method, lambda: jobs.wait_hold(a.hold),
                                 launch=hx_calibrate.planted(plan, a.b_env))
    outcome, reason, void_reasons, measured = hx_bench.judge(results, series, floor, method)
    entries = series["entries"]
    payload = {
        "kind": "envbench", "outcome": outcome, "reason": reason, "void_reasons": void_reasons, "b_env": dict(a.b_env),
        "device": a.device, "expect": a.expect, "scene": a.scene, "a_sha": a_sha, "b_sha": b_sha,
        "suite_sha256": suite.sha256, "method": dataclasses.asdict(method), "floor_run": a.floor,
        "floor": floor["floor"], "validation": series["validation"], "series": entries,
        **(measured or {key: None for key in hx_bench.BENCH_MEASURED}),
    }
    first = next((e["run"] for e in entries if e["run"] is not None), None)
    try:
        run_id = hx_floor.write_verdict(a.root, ENVBENCH_FILE, payload, suite_data, first)
    except (OSError, hx_floor.FloorError, record.RecordError) as e:
        print("hx envbench: the envbench is not recorded: %s" % e, file=sys.stderr)
        return 2
    print("envbench: %s" % run_id)
    if outcome == "failed":
        print("verdict: failed (%s)" % reason)
    elif outcome == "void":
        print("verdict: void (%s)" % "; ".join(void_reasons))
    else:
        low, high = measured["interval"]
        print("verdict: %s (interval %+.2f%% to %+.2f%%, floor %.2f%%)"
              % (outcome, low * 100, high * 100, floor["floor"] * 100))
    return 0 if outcome in hx_bench.VERDICTS else 1
