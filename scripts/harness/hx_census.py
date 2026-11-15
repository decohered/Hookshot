"""hx census: a census play of a capture, recorded as a run. The capture is played once through the renderer of the named build
on one device, with RX_VK_CENSUS=1 in the player's environment, as hx play plays it (the same player, the same PNG of every
present, the same diff against live). The census the renderer logs is parsed from the play log into the run. The run is a
play run, so hx play can take it as --against, with the census: its totals, the curve of presents and new variants, its
window count, and versus_live_differing, the count of presents whose diff from live is not 0 pixels. The play log stays in
the host's record, as hx play's does; the census in the run holds fixed key names and counts only.

    hx census <capture-run> --device D --build SHA

SHA is the build's full commit id: its folder is played, and its build.txt must name that commit.

Exit status: 0 pass, 1 FAIL (the verdict: the build or the player failed, the player named another device or fell back to
another GPU, it wrote no present, its presents cannot be compared, or its census is missing or refused), 2 the check could
not run (an unknown device, a malformed run or build id, a run that is not a capture, a build folder of another commit, a
device the machine does not list, a job that will not start, a run that cannot be written). The client returns the job's
status; 255 is ssh lost, and the job goes on.

Only stdlib. The census lines are read as gpu/vk/vk_census.h writes them.
"""
import argparse
import json
import os
import posixpath
import re
import sys

import config
import hx_capture
import hx_play
import hx_smoke
import jobs
import record

CENSUS_SWITCH = "RX_VK_CENSUS"
# every census key, in the order gpu/vk/vk_census.h writes them (its kKeyNames)
KEYS = (
    "presents", "draws", "mesh_draws", "nvn_barriers", "slot_changes",
    "draws_slots_0", "draws_slots_1", "draws_slots_2", "draws_slots_3_4", "draws_slots_5_8", "draws_slots_9_up",
    "uniform_only_draws", "recordings", "recordings_with_draw",
    "first_draw_inherits_none", "first_draw_inherits_program", "first_draw_inherits_targets",
    "first_draw_inherits_render_state", "first_draw_inherits_vertex", "first_draw_inherits_viewport",
    "first_draw_inherits_bindings",
    "frame_pipelines", "frame_stage_sets", "frame_sets", "frame_views",
    "frame_pipelines_max", "frame_stage_sets_max", "frame_sets_max", "frame_views_max",
    "pipelines_made",
    "variant_state_topology", "variant_state_multisample", "variant_state_vertex", "variant_state_color_formats",
    "variant_state_depth_format", "variant_state_blend", "variant_state_raster",
    "new_programs", "new_variants",
    "programs_variants_1", "programs_variants_2", "programs_variants_3", "programs_variants_4_7",
    "programs_variants_8_15", "programs_variants_16_up",
    "pushes_full", "pushes_delta", "pushes_none", "pool_sets", "writes_pushed", "writes_full", "desc_reused_draws",
    "readbacks", "readbacks_age_0", "readbacks_age_1", "readbacks_age_2", "readbacks_age_3_up", "readbacks_age_unknown",
    "pass_breaks_target_change", "pass_breaks_clear", "pass_breaks_copy", "pass_breaks_hazard", "pass_breaks_compute",
    "pass_breaks_upscale", "pass_breaks_event", "pass_breaks_barrier", "pass_breaks_split", "pass_breaks_recording_end",
    "pass_breaks_submit", "pass_breaks_other",
    "full_barriers_recorded", "full_barriers_left_out",
)
MARKER = "vk: census: window="
# a census line: the window number, then " key=count" pairs and nothing else (text before the marker is the log's own)
CENSUS_LINE = re.compile(r"vk: census: window=([0-9]+)((?: \S+)*)$")
PAIR = re.compile(r"([^=]+)=(.*)")
DECIMAL = re.compile(r"[0-9]+")


class CensusError(Exception):
    pass


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx census", description="Play a capture once with the renderer's census on, on one "
                                                             "device and the named build, and write the run.")
    p.add_argument("run", metavar="RUN", help="the capture run: its presents are the live comparison")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--build", required=True, metavar="SHA", help="the build's full commit id: its folder is played")
    return p.parse_args(argv)


def parse_in_job(argv):
    p = argparse.ArgumentParser(prog="hx census --in-job", description="The in-job half, run inside a check job on the host.")
    p.add_argument("job", metavar="JOB", help="the job's folder")
    p.add_argument("--hold", required=True, metavar="PATH", help="the hold file: no launch while it exists")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--env", type=hx_smoke.pair, action="append", metavar="NAME=VALUE",
                   help="the device's environment for the player")
    p.add_argument("--device", required=True, metavar="D", help="the device's name in the configuration")
    p.add_argument("--live", required=True, metavar="RUN", help="the capture run")
    p.add_argument("--record", required=True, metavar="DIR", help="the record root")
    p.add_argument("--build-folder", required=True, metavar="DIR", help="the build's folder on the host")
    p.add_argument("--closure", required=True, metavar="PATH", help="the build's closure, a /nix/store path")
    return p.parse_args(argv)


def main(argv):
    try:
        if argv[:1] == ["--in-job"]:
            return in_job(parse_in_job(argv[1:]))
        return client(parse_client(argv))
    except (CensusError, hx_play.PlayError, hx_capture.CaptureError, hx_smoke.SmokeError, config.ConfigError,
            jobs.JobError, record.RecordError, OSError) as e:
        print("census: %s" % e, file=sys.stderr)
        return 2


def client(a):
    cfg = config.load()
    device = cfg.devices.get(a.device)
    if device is None:
        raise CensusError("no device %r; the devices are: %s" % (a.device, ", ".join(sorted(cfg.devices))))
    if not record.RUN_ID.fullmatch(a.run):
        raise CensusError("not a run id: %r" % a.run)
    if not record.GIT_COMMIT.fullmatch(a.build):
        raise CensusError("not a build commit id (40 lowercase hex digits): %r" % a.build)
    live = hx_play.read_run_doc(cfg, a.run)
    refusal = hx_play.pair_refusal(a.run, live, None, None, a.device)
    if refusal is not None:
        raise CensusError(refusal)
    folder = jobs.build_dir(cfg, a.build)
    info = hx_smoke.read_build(cfg, folder)
    sha, closure = info.get("sha"), info.get("closure")
    if sha != a.build:
        raise CensusError("the build in %s says sha %s, not %s" % (folder, sha, a.build))
    if not closure or not record.NIX_STORE_PATH.fullmatch(closure):
        raise CensusError("%s: build.txt's closure must be a /nix/store/ path" % folder)
    print("build: %s" % sha)
    job_id = jobs.new_id("check")
    job = jobs.job_dir(cfg, job_id)
    record_root = posixpath.join(cfg.root, "results")
    argv = [cfg.enter_closure, closure, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "census", "--in-job", job, "--hold", cfg.hold, "--expect", device.expect, "--device", a.device,
            "--live", a.run, "--record", record_root, "--build-folder", folder, "--closure", closure]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    jobs.start(cfg, job_id, argv, game=posixpath.join(folder, "game"))
    print("job: %s" % job_id)
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("census: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def in_job(a):
    """The in-job half: hx play's, with the census switch on for the player. The census is parsed from the play log, and
    the run holds it with the presents' diffs from live and the count of those that differ."""
    jobs.wait_hold(a.hold)
    live_doc, live = hx_play.load_run(a.record, a.live)
    refusal = hx_play.pair_refusal(a.live, live_doc, None, None, a.device)
    if refusal is not None:
        raise CensusError(refusal)
    device_env = dict(a.env or [])
    facts = hx_capture.run_facts(a.build_folder, os.path.join(a.job, "game"), a.closure,
                                 {**device_env, **hx_capture.SWITCHES}, a.expect)
    shots = os.path.join(a.job, "shots")
    os.makedirs(shots)
    play_log = os.path.join(a.job, "play.log")
    player, why = hx_play.build_player(a.build_folder, a.job)
    plays, judged, census = {}, None, None
    if player is not None:
        env = {**os.environ, **jobs.launch_env(a.job, device_env), **hx_capture.SWITCHES, CENSUS_SWITCH: "1",
               "RX_VK_SHOTS": shots}
        code = hx_play.run_player(player, live_doc["capture"], os.path.join(a.job, "game", "hookshot_vk.so"), env,
                                  play_log)
        plays = hx_capture.present_shots(shots)
        log = hx_play.read_text(play_log)
        why = hx_play.player_verdict(code, log, a.expect, plays)
        try:
            judged = hx_play.judge(plays, live, None)
        except hx_play.PlayError as e:
            why = why or "cannot compare the presents: %s" % e
        try:
            census = parse_census(log)
        except CensusError as e:
            why = why or "census: %s" % e
        print("device: %s" % (", ".join(hx_play.device_names(log)) or "none named"))
        print("presents: %d" % len(plays))
    verdict = "pass" if why is None else "FAIL: %s" % why
    print("census: %s" % verdict)
    doc = {
        "kind": hx_play.PLAY,
        "capture": live_doc["capture"],
        "capture_build": live_doc.get("build"),
        "build": facts["shas"]["hookshot"],
        "device": a.device,
        "live": a.live,
        "against": None,
        "presents": None if player is None else len(plays),
        "verdict": verdict,
        **(judged or hx_play.NO_JUDGEMENT),
        "versus_live_differing": None if judged is None else differing(judged["versus_live"]),
        "census": census,
    }
    files = {"capture.json": (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"),
             "build.log": hx_play.read_bytes(os.path.join(a.job, "build.log"))}
    if player is not None:
        files["play.log"] = hx_play.read_bytes(play_log)
    run_id, _ = record.write_run(a.record, facts, files, plays, journal=record.write_journal)
    print("run: %s" % run_id)
    return 0 if why is None else 1


def differing(versus_live):
    """The presents whose diff from live is not 0 pixels: those that differ in pixels, and those only live has"""
    return sum(1 for diff in versus_live.values() if diff.get("pixels") != 0)


def parse_census(text):
    """The census of a play log: {"totals": {key: count}, "curve": [[presents at the end of each window, that window's new
    variants], ...], "windows": the window count}. Raises CensusError on a play log with no census window or one it refuses."""
    return summarize(read_windows(text))


def read_windows(text):
    """The census windows of a play log, in order, each a {key: count}. A line without the marker is the renderer's other
    text, and is skipped. Refused: a census line that is not a window number then key=count pairs; a key that is not in
    KEYS; a count that is not decimal; a key a window names twice; a window that starts out of order (a new window must be
    the next number); a window without every key. The reasons name no text of the log, only window numbers and keys."""
    windows = []
    for number_of_line, raw in enumerate(text.splitlines(), 1):
        if MARKER not in raw:
            continue
        found = CENSUS_LINE.search(raw)
        if found is None:
            raise CensusError("line %d is a census line with text other than key=count pairs" % number_of_line)
        number = int(found.group(1))
        if number == len(windows) + 1:
            windows.append({})
        elif not 1 <= number <= len(windows):
            raise CensusError("window %d is out of order: window %d is next" % (number, len(windows) + 1))
        window = windows[number - 1]
        for token in found.group(2).split():
            pair = PAIR.fullmatch(token)
            if pair is None:
                raise CensusError("line %d is a census line with text other than key=count pairs" % number_of_line)
            key, count = pair.groups()
            if key not in KEYS:
                raise CensusError("window %d names a key that is not in the census" % number)
            if not DECIMAL.fullmatch(count):
                raise CensusError("window %d's %s is not a decimal count" % (number, key))
            if key in window:
                raise CensusError("window %d names %s twice" % (number, key))
            window[key] = int(count)
    if not windows:
        raise CensusError("the play log has no census window")
    for number, window in enumerate(windows, 1):
        missing = [key for key in KEYS if key not in window]
        if missing:
            raise CensusError("window %d lacks %s" % (number, ", ".join(missing)))
    return windows


def summarize(windows):
    """The totals of the windows: each key's sum over them, except that a *_max key's total is its largest window value and
    a programs_variants key's total is the last window's value (a histogram). Then the curve and the window count."""
    totals = {}
    for key in KEYS:
        counts = [window[key] for window in windows]
        if key.endswith("_max"):
            totals[key] = max(counts)
        elif key.startswith("programs_variants_"):
            totals[key] = counts[-1]
        else:
            totals[key] = sum(counts)
    curve, presents = [], 0
    for window in windows:
        presents += window["presents"]
        curve.append([presents, window["new_variants"]])
    return {"totals": totals, "curve": curve, "windows": len(windows)}
