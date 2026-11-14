"""hx analyze: the statistics of a capture run (hx capture's run, or hx capture --scene's), computed by analysis.analyze on the
host in a check job and written as one run. With --against RUN, another capture of the same scene is the second capture
the statistics are over. The run names the captures by run id, never by folder.

    hx analyze <capture-run> [--against RUN]

The client half (on this PC) reads the runs' capture.json on the host and refuses what the in-job half would refuse, then
starts a check job (jobs.py) that runs the in-job half on the host. That waits for the hold, reads both runs from the record
root, repeats the refusals, gathers the run's facts from the build's folder, runs analysis.analyze and writes one run whose
analysis.json holds the statistics. An AnalysisError writes its run too, marked FAIL, with null statistics.

Exit status: 0 pass, 1 FAIL (analysis.analyze refused the capture), 2 the check could not run (a malformed run id, a run
that is not a capture or cannot be read, an --against run that is the run itself, is not a capture or is of another scene,
a device the configuration does not list, a build that is missing or refused, a job that will not start, a run that cannot
be written). The client returns the job's status; 255 is ssh lost, and the job goes on.
"""
import argparse
import json
import os
import posixpath
import sys

import analysis
import config
import hx_capture
import hx_play
import hx_smoke
import jobs
import record

ANALYSIS = "analysis"
ANALYSIS_JSON = "analysis.json"


class AnalyzeError(Exception):
    pass


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx analyze", description="Compute a capture run's statistics in a check job on the "
                                                              "host, and write them as one run.")
    p.add_argument("run", metavar="RUN", help="the capture run")
    p.add_argument("--against", metavar="RUN", help="another capture of the same scene: the statistics are over both")
    return p.parse_args(argv)


def parse_in_job(argv):
    p = argparse.ArgumentParser(prog="hx analyze --in-job", description="The in-job half, run inside a check job on the "
                                                                       "host.")
    p.add_argument("job", metavar="JOB", help="the job's folder")
    p.add_argument("--hold", required=True, metavar="PATH", help="the hold file: nothing runs while it exists")
    p.add_argument("--run", required=True, metavar="RUN", help="the capture run")
    p.add_argument("--against", metavar="RUN", help="another capture of the same scene")
    p.add_argument("--record", required=True, metavar="DIR", help="the record root")
    p.add_argument("--build-folder", required=True, metavar="DIR", help="the build's folder on the host")
    p.add_argument("--closure", required=True, metavar="PATH", help="the build's closure, a /nix/store path")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    return p.parse_args(argv)


def main(argv):
    try:
        if argv[:1] == ["--in-job"]:
            return in_job(parse_in_job(argv[1:]))
        return client(parse_client(argv))
    except (AnalyzeError, hx_play.PlayError, hx_capture.CaptureError, hx_smoke.SmokeError, config.ConfigError,
            jobs.JobError, record.RecordError, OSError) as e:
        print("analyze: %s" % e, file=sys.stderr)
        return 2


def client(a):
    cfg = config.load()
    for run_id in (a.run, a.against):
        if run_id is not None and not record.RUN_ID.fullmatch(run_id):
            raise AnalyzeError("not a run id: %r" % run_id)
    live = hx_play.read_run_doc(cfg, a.run)
    against = None if a.against is None else hx_play.read_run_doc(cfg, a.against)
    refusal = pair_refusal(a.run, live, a.against, against)
    if refusal is not None:
        raise AnalyzeError(refusal)
    name = live.get("device")
    device = cfg.devices.get(name) if isinstance(name, str) else None
    if device is None:
        raise AnalyzeError("run %s names no device of the configuration: %r" % (a.run, name))
    capture_build = live.get("build")
    if not isinstance(capture_build, str) or not record.GIT_COMMIT.fullmatch(capture_build):
        raise AnalyzeError("run %s names no build commit: %r" % (a.run, capture_build))
    folder = jobs.build_dir(cfg, capture_build)
    info = hx_smoke.read_build(cfg, folder)
    sha, closure = info.get("sha"), info.get("closure")
    if sha != capture_build:
        raise AnalyzeError("the build in %s says sha %s, not the capture's %s" % (folder, sha, capture_build))
    if not closure or not record.NIX_STORE_PATH.fullmatch(closure):
        raise AnalyzeError("%s: build.txt's closure must be a /nix/store/ path" % folder)
    print("build: %s" % sha)
    job_id = jobs.new_id("check")
    job = jobs.job_dir(cfg, job_id)
    argv = [cfg.enter_closure, closure, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "analyze", "--in-job", job, "--hold", cfg.hold, "--record", posixpath.join(cfg.root, "results"),
            "--run", a.run]
    if a.against is not None:
        argv += ["--against", a.against]
    argv += ["--build-folder", folder, "--closure", closure, "--expect", device.expect]
    jobs.start(cfg, job_id, argv)
    print("job: %s" % job_id)
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("analyze: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def pair_refusal(run_id, live, against_id, against):
    """Why the analysis cannot run on the capture run (and on the --against run, when given), or None"""
    if not is_capture(live):
        return "run %s is not a capture run" % run_id
    if against_id is None:
        return None
    if against_id == run_id:
        return "--against names the run itself: %s" % run_id
    if not is_capture(against):
        return "run %s is not a capture run" % against_id
    if against.get("scene") != live.get("scene"):
        return "--against run %s has scene %r, not the run's %r" % (against_id, against.get("scene"), live.get("scene"))
    return None


def is_capture(doc):
    return "kind" not in doc and isinstance(doc.get("capture"), str)


def in_job(a):
    """The in-job half: it waits for the hold, reads both runs from the record root and repeats the refusals, gathers the
    run's facts from the build's folder, runs analysis.analyze on the capture (with the --against capture and its window)
    and writes the run. An AnalysisError writes the run marked FAIL."""
    jobs.wait_hold(a.hold)
    live = load_run(a.record, a.run)
    against = None if a.against is None else load_run(a.record, a.against)
    refusal = pair_refusal(a.run, live, a.against, against)
    if refusal is not None:
        raise AnalyzeError(refusal)
    facts = hx_capture.run_facts(a.build_folder, os.path.join(a.build_folder, "game"), a.closure, {}, a.expect)
    if facts["shas"]["hookshot"] != live.get("build"):
        raise AnalyzeError("the build in %s says sha %s, not the capture's %s"
                           % (a.build_folder, facts["shas"]["hookshot"], live.get("build")))
    window = window_of(live)
    pair = None if against is None else (against["capture"], window_of(against))
    statistics, verdict = measure(live["capture"], window, pair)
    doc = {
        "kind": ANALYSIS,
        "capture_run": a.run,
        "against_run": a.against,
        "capture_build": live["build"],
        "device": live.get("device"),
        "scene": live.get("scene"),
        "window": None if window is None else list(window),
        "verdict": verdict,
        "statistics": statistics,
    }
    files = {ANALYSIS_JSON: (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")}
    run_id, _ = record.write_run(a.record, facts, files, {}, journal=record.write_journal)
    print("analyze: %s" % verdict)
    print("run: %s" % run_id)
    return 0 if verdict == "pass" else 1


def load_run(root, run_id):
    """A run's capture.json, read from the record root; the run must have a manifest that record.read_manifest reads"""
    record.read_manifest(root, run_id)
    return hx_play.parse_doc(run_id, hx_play.read_text(os.path.join(root, "runs", run_id, "files", "capture.json")))


def window_of(doc):
    """The capture's scene window as (first, last) when both are ints, else None"""
    window = doc.get("window")
    if isinstance(window, dict) and isinstance(window.get("first"), int) and isinstance(window.get("last"), int):
        return window["first"], window["last"]
    return None


def measure(capture, window, against):
    """analysis.analyze's statistics and the verdict: (the statistics, "pass"), or (None, "FAIL: <its message>")"""
    try:
        return analysis.analyze(capture, window, against), "pass"
    except analysis.AnalysisError as e:
        return None, "FAIL: %s" % e
