"""hx calibrate: the method calibrated against itself, as one bench job of one build in both arms. Two series run in order
against the newest recorded floor of this device, scene and method, each judged by hx_bench.judge and recorded as one
calibration run (calibration.json) with its expected call, its actual call and whether they match:

- none: A vs A, expects no change;
- delay: arm B's runs planted with RX_VK_SUBMIT_DELAY_US, the smallest whole number of microseconds at or above
  2 x floor x T (T the median of the floor's run medians, in us; at least 1), expects slower.

    hx calibrate --device D --scene S [--build REV]

The floor is read back from the record, never measured, and it must be a floor of this build: the newest one of this device,
scene and method is refused when its floor.json names another build's a_sha. The delay is planted only on the synchronous
replay path, so the device's environment must not turn RX_VK_ASYNC on (an integer other than 0, as the renderer reads it):
that is refused too. A series that does not match, or does not run through, is recorded and the next series runs; nothing
is rerun.

Exit status: 0 both series match; 1 a series does not match or did not run through (recorded); 2 the calibration could not
run (no recorded floor of this device, scene and method; that floor measured on another build; RX_VK_ASYNC on in the
device's environment; a job that will not start) or a record is not written. The client returns the job's status; 255 is
ssh lost, and the job goes on.
"""
import argparse
import dataclasses
import math
import os
import re
import statistics
import sys
from decimal import Decimal

import config
import hx_bench
import hx_floor
import hx_run
import jobs
import record
import runrecord
import scenes
import verdict

CALIBRATION_FILE = "calibration.json"
DELAY_KEY = "RX_VK_SUBMIT_DELAY_US"
SERIES = ("none", "delay")
EXPECTED = {"none": "no change", "delay": "slower"}
ASYNC_KEY = "RX_VK_ASYNC"
LEADING_INT = re.compile(r"[ \t\n\v\f\r]*([+-]?[0-9]+)")


class CalibrateError(Exception):
    """A calibration that cannot run: the message is its one line"""


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx calibrate",
                                description="Calibrate the method on one build in both arms: two series against the newest "
                                            "recorded floor (A vs A, a planted submit delay), each recorded.")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--scene", required=True, metavar="S", help="a scene of the suite")
    p.add_argument("--build", metavar="REV", help="the build of this checkout's commit REV (default: the newest build)")
    return p.parse_args(argv)


def parse_in_job(argv):
    p = hx_floor.in_job_parser("hx calibrate --in-job", "The in-job half, run inside a bench job on the host.")
    return p.parse_args(argv)


def main(argv):
    if argv[:1] == ["--in-job"]:
        return in_job(parse_in_job(argv[1:]))
    try:
        a = parse_client(argv)
        cfg = config.load()
        folder, _, closure = hx_floor.resolve(cfg, a.build)
        return hx_floor.client("calibrate", cfg, a.device, a.scene, (folder, closure), (folder, closure))
    except (hx_floor.FloorError, config.ConfigError, verdict.MethodError, jobs.JobError) as e:
        print("hx calibrate: %s" % e, file=sys.stderr)
        return 2


def plant_us(floor, a_medians, b_medians):
    """RX_VK_SUBMIT_DELAY_US: the smallest positive whole number of microseconds at or above 2 x floor x T, T the median of
    the two arms' run medians (ms) in us. Every frame has at least one submit, so the delay adds at least that fraction of a
    frame. The arithmetic is decimal on the values' shortest forms, so a bound that is a whole number is not rounded up."""
    t_ms = statistics.median([*a_medians, *b_medians])
    bound = 2 * Decimal(repr(floor)) * Decimal(repr(t_ms)) * 1000
    return max(1, math.ceil(bound))


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def floor_medians(run_id, floor):
    """The floor's two arms' run medians (ms), which the delay is sized from. CalibrateError unless each is a non-empty list
    of positive numbers."""
    arms = (floor.get("a_medians"), floor.get("b_medians"))
    if not all(isinstance(xs, list) and xs and all(_positive(x) for x in xs) for xs in arms):
        raise CalibrateError("floor %s has no run medians to size the delay from" % run_id)
    return arms


def names_floor(results, run_id):
    """Whether a run's manifest names floor.json. FloorError when its manifest cannot be read."""
    try:
        manifest, _ = record.read_manifest(results, run_id)
    except record.RecordError as e:
        raise hx_floor.FloorError(str(e)) from None
    return isinstance(manifest, dict) and isinstance(manifest.get("files"), dict) and hx_floor.FLOOR_FILE in manifest["files"]


def newest_floor(results, device, scene, method):
    """The run id of the newest recorded floor of this device, scene and method, read back from the record. The runs are read
    newest first; a run that names no floor is passed over, as is a floor that failed or one of another device, scene or
    method. CalibrateError when none is recorded."""
    want = dataclasses.asdict(method)
    for run_id in reversed(record.run_ids(results)):
        if names_floor(results, run_id):
            floor = hx_floor.read_floor(results, run_id)
            if (floor.get("kind"), floor.get("outcome"), floor.get("device"), floor.get("scene"), floor.get("method")) == \
                    ("floor", "recorded", device, scene, want):
                return run_id
    raise CalibrateError("no recorded floor of device %s, scene %s and this method" % (device, scene))


def async_on(value):
    """Whether the renderer reads an RX_VK_ASYNC value as on: C's atoi(value) != 0, the integer at the value's start after any
    blanks (gpu/vk/vk_replay.cpp:4616-4621). A value with no leading integer is off."""
    found = LEADING_INT.match(value)
    return found is not None and int(found.group(1)) != 0


def refuse_async(env):
    """CalibrateError when the device's environment turns RX_VK_ASYNC on. The delay is planted only on the synchronous replay
    path, so a calibration with it on would plant nothing. The last pair of a name is the one each run gets (run_series)."""
    value = dict(env or []).get(ASYNC_KEY)
    if value is not None and async_on(value):
        raise CalibrateError("the device's environment sets %s=%s: the delay is planted only on the synchronous replay"
                             % (ASYNC_KEY, value))


def refuse_other_build(run_id, floor, build_sha):
    """CalibrateError when the floor was measured on another build than the calibrated one, its a_sha not build_sha: a
    calibration judges against the floor of its own build."""
    if floor.get("a_sha") != build_sha:
        raise CalibrateError("floor %s is of build %s, not of this build %s: a calibration judges against a floor of its own "
                             "build" % (run_id, floor.get("a_sha"), build_sha))


def plant(series, floor, medians):
    """The environment one series plants in arm B's runs: none plants nothing, delay the submit delay"""
    if series == "delay":
        return [(DELAY_KEY, str(plant_us(floor, *medians)))]
    return []


def with_env(argv, pairs):
    """argv with each NAME=VALUE of pairs added as an --env option just before the run's --scene"""
    at = argv.index("--scene")
    extra = []
    for name, value in pairs:
        extra += ["--env", "%s=%s" % (name, value)]
    return argv[:at] + extra + argv[at:]


def run_index(argv):
    """A run's index, from the number of its sub-job folder runs/<index> in its argv"""
    flag = "--in-job" if "--in-job" in argv else "--correctness-in-job"
    return int(os.path.basename(argv[argv.index(flag) + 1]))


def planted(plan, pairs):
    """hx_floor.launch for one series: the pairs are added to the argv of arm B's runs, timed and correctness, and to no
    other run. A run's arm is its position's in the plan, since both arms share one build folder."""
    def launch(argv):
        arm, _ = plan[run_index(argv)]
        if arm == "B" and pairs:
            argv = with_env(argv, pairs)
        return hx_floor.launch(argv)
    return launch


def keep_series_folder(job, series):
    """run_series names each run's folder runs/<index> and fails on a folder that exists, so a series' folder is moved to
    <job>/<series> once it has run and been recorded. The next series then starts with no runs folder."""
    runs = os.path.join(job, "runs")
    if os.path.isdir(runs):
        os.rename(runs, os.path.join(job, series))


def in_job(a):
    """The in-job half. It reads the suite and the method, and the newest recorded floor of this device, scene and method
    (never measured). Each refusal is exit 2 before any run: a device environment that turns RX_VK_ASYNC on, and a newest
    floor measured on another build than this one. Then the two series run in order, each judged by hx_bench.judge and
    recorded as one calibration run. Returns 0 when both match, 1 when one does not (recorded), 2 when the calibration
    could not run or a record is not written."""
    results = runrecord.results_dir(a.root)
    try:
        refuse_async(a.env)
        suite_data, suite, method, a_sha, b_sha = hx_floor.preamble(a)
        floor_id = newest_floor(results, a.device, a.scene, method)
        floor = hx_bench.check_floor(results, argparse.Namespace(floor=floor_id, device=a.device, scene=a.scene), method)
        refuse_other_build(floor_id, floor, a_sha)
        medians = floor_medians(floor_id, floor)
    except (hx_run.RunError, scenes.SceneError, verdict.MethodError, hx_floor.FloorError, CalibrateError) as e:
        print("hx calibrate: could not run: %s" % e, file=sys.stderr)
        return 2
    plan = verdict.plan(method.runs_per_arm)
    job = os.path.abspath(a.job)
    matched = True
    for series in SERIES:
        pairs = plant(series, floor["floor"], medians)
        series_run = hx_floor.run_series(a, method, lambda: jobs.wait_hold(a.hold), launch=planted(plan, pairs))
        entries = series_run["entries"]
        outcome, reason, void_reasons, measured = hx_bench.judge(results, series_run, floor, method)
        expected = EXPECTED[series]
        match = outcome == expected
        payload = {
            "kind": "calibration", "name": series, "expected": expected, "actual": outcome, "match": match,
            "reason": reason, "void_reasons": void_reasons, "plant": dict(pairs), "device": a.device,
            "expect": a.expect, "scene": a.scene, "a_sha": a_sha, "b_sha": b_sha, "suite_sha256": suite.sha256,
            "method": dataclasses.asdict(method), "floor_run": floor_id, "floor": floor["floor"],
            "validation": series_run["validation"], "series": entries,
            **(measured or {key: None for key in hx_bench.BENCH_MEASURED}),
        }
        first = next((e["run"] for e in entries if e["run"] is not None), None)
        try:
            run_id = hx_floor.write_verdict(a.root, CALIBRATION_FILE, payload, suite_data, first)
        except (OSError, hx_floor.FloorError, record.RecordError) as e:
            print("hx calibrate: the calibration is not recorded: %s" % e, file=sys.stderr)
            return 2
        print("calibration %s: %s" % (series, run_id))
        print("%s: %s (expected %s): %s" % (series, outcome, expected, "match" if match else "MISMATCH"))
        detail = reason or "; ".join(void_reasons)
        if detail:
            print("  %s" % detail)
        matched = matched and match
        try:
            keep_series_folder(job, series)
        except OSError as e:
            print("hx calibrate: the next series cannot start: %s" % e, file=sys.stderr)
            return 2
    return 0 if matched else 1
