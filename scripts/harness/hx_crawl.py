"""hx crawl: a build's game started from the crawl file's save and, for each variant (age), each destination with a map for it
visited: arrived on, settled, and the camera turned once around (the right stick held full right for pan_frames) inside
one window whose trace costs are kept. Writes the cost map (scenes/costmaps/<job id>.json) and one record run. A
destination that fails is kept with its error and is not retried. A segment is one start of the game from the save. A start
waits settle_frames presents and then for play with no event or load within the settle limit; when that does not come,
the segment's first destination is entered with that error, nothing is sent for it, and the segment ends. A segment also
ends after a failed destination, after a destination named in last when visits follow it, and before a later visit when
the game is not in play with no event or load within the settle limit or the game closes the connection; the visit not sent
then is made first by the next segment, so no visit is sent twice or dropped. At a segment's end the game is quit, its
outputs kept as that segment, and it is started again from the save in the same job, enters play, checks the device and
goes on. Each segment's log, frame trace, frame log and kernel log (the journal's kernel lines from its launch to the next
segment's) are in the record under segments/<n>/.

    hx crawl --device D [--build REV]

Exit status: 0 every destination measured; 1 a destination failed, or the run failed (the verdict); 2 the crawl could not
run (an unknown device, a bad crawl file, a build that is missing or refused, a job or a game that will not start). The
client returns the job's status; 255 is ssh lost, and the job goes on.

The crawl file is <root>/scenes/crawl.toml, outside git:

    save = "<folder>"               the folder of the save under scenes/saves
    slot = "01"                     optional: two digits
    pan_frames = 45                 presents the right stick is held full right for one turn around (at least 1)
    settle_frames = 5               presents waited after each arrival (at least 0)
    variants = ["child", "adult"]   optional: a non-empty subset of child and adult (default both)
    last = [2, 0]                   optional: distinct destination indexes, visited after the others of each variant in
                                    this order (default none); each one ends its segment when visits follow it
"""
import argparse
import dataclasses
import getpass
import json
import os
import posixpath
import subprocess
import sys
import time
import tomllib

import bridge
import config
import game
import hx_smoke
import jobs
import metrics
import record
import runrecord
import scenes

CRAWL_KEYS = ("save", "slot", "pan_frames", "settle_frames", "variants", "last")
VARIANTS = ("child", "adult")
PRESSES_TO_PLAY = 30         # presses of A from the title into play, at most (the title and file screens take ten, E1-D14)
MIN_PRESENT_TIMEOUT = 60     # seconds: a wait for presents is given at least this and a second a present (a slow start)
QUIT_SECONDS = 60
RECORD_OUTPUTS = (("log", "log.txt"), ("frame_trace", "hookshot_frames.csv"), ("frame_log", "frametimes.csv"))


class CrawlError(Exception):
    """The crawl cannot run: its file, its build, its job or its game is refused. The message says why."""


@dataclasses.dataclass(frozen=True)
class Crawl:
    save: str
    slot: str | None
    pan_frames: int
    settle_frames: int
    variants: tuple[str, ...]
    last: tuple[int, ...] = ()


@dataclasses.dataclass(frozen=True)
class Started:
    """What the in-job half gathers before the game starts, for the record after it has quit"""
    job_id: str
    job: str
    cache: str
    sha: str
    build_txt: str
    hashes: str
    crawl_text: str
    device_env: dict[str, str]


@dataclasses.dataclass(frozen=True)
class Segment:
    """One start of the game, read after it quit and before the next start: its number from 1, its launch as whole seconds
    since the epoch (taken before the start), its outputs by name as bytes (those that exist), and its exit status (None
    when it did not quit)"""
    number: int
    launched: int
    outputs: dict[str, bytes]
    exit_status: int | None


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx crawl", description="Visit each destination of a build's game in each variant, "
                                                             "pan once around at each, and write the cost map and a run.")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--build", metavar="REV", help="the build of this checkout's commit REV (default: the newest build)")
    return p.parse_args(argv)


def pair(text):
    name, eq, value = text.partition("=")
    if not eq or not name:
        raise argparse.ArgumentTypeError("not NAME=VALUE: %r" % text)
    return name, value


def parse_in_job(argv):
    p = argparse.ArgumentParser(prog="hx crawl --in-job", description="The in-job half, run inside a bench job on the host.")
    p.add_argument("--in-job", required=True, metavar="JOB", help="the job's folder")
    p.add_argument("--hold", required=True, metavar="PATH", help="the hold file: no launch while it exists")
    p.add_argument("--root", required=True, metavar="DIR", help="the harness root")
    p.add_argument("--build-folder", required=True, metavar="DIR", help="the build's folder")
    p.add_argument("--device", required=True, metavar="D", help="the device's name in the configuration")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--env", type=pair, action="append", metavar="NAME=VALUE", help="the device's environment for the game")
    return p.parse_args(argv)


def main(argv):
    if argv[:1] == ["--in-job"]:
        return in_job(parse_in_job(argv))
    try:
        return client(parse_client(argv))
    except (CrawlError, hx_smoke.SmokeError, config.ConfigError, jobs.JobError) as e:
        print("crawl: %s" % e, file=sys.stderr)
        return 2


def client(a):
    cfg = config.load()
    device = cfg.devices.get(a.device)
    if device is None:
        raise CrawlError("no device %r; the devices are: %s" % (a.device, ", ".join(sorted(cfg.devices))))
    folder, rev_sha = hx_smoke.build_folder(cfg, a.build)
    info = hx_smoke.read_build(cfg, folder)
    sha, closure = info.get("sha"), info.get("closure")
    if not sha or not closure:
        raise CrawlError("%s: build.txt needs sha and closure lines" % folder)
    if rev_sha is not None and sha != rev_sha:
        raise CrawlError("the build in %s says sha %s, not %s" % (folder, sha, rev_sha))
    print("build: %s" % sha)
    job_id = jobs.new_id("bench")
    job = jobs.job_dir(cfg, job_id)
    argv = [cfg.enter_closure, closure, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "crawl", "--in-job", job, "--hold", cfg.hold, "--root", cfg.root, "--build-folder", folder,
            "--device", a.device, "--expect", device.expect]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    jobs.start(cfg, job_id, argv)
    print("job: %s" % job_id)
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("crawl: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise CrawlError("cannot read %s: %s" % (path, e.strerror)) from None
    except UnicodeDecodeError:
        raise CrawlError("%s: not UTF-8 text" % path) from None


def crawl_path(root):
    return posixpath.join(scenes.scene_dir(root), "crawl.toml")


def parse_crawl(text, where):
    """The crawl file's text as a Crawl: CrawlError naming the key that is unknown or has a bad value"""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CrawlError(f"{where}: not valid TOML: {e}") from None
    for key in doc:
        if key not in CRAWL_KEYS:
            raise CrawlError(f"{where}: unknown key {key}")
    return Crawl(
        save=_match(where, doc, "save", scenes.NAME, "a folder name: letters, digits, _ or -"),
        slot=_match(where, doc, "slot", scenes.SLOT, "two digits") if "slot" in doc else None,
        pan_frames=_whole(where, doc, "pan_frames", 1),
        settle_frames=_whole(where, doc, "settle_frames", 0),
        variants=_variants(where, doc),
        last=_last(where, doc),
    )


def _need(where, doc, key):
    if key not in doc:
        raise CrawlError(f"{where}: missing key {key}")
    return doc[key]


def _match(where, doc, key, pattern, shape):
    value = _need(where, doc, key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CrawlError(f"{where}: {key} must be {shape}: {value!r}")
    return value


def _whole(where, doc, key, low):
    """An int that is not a bool, at least low"""
    value = _need(where, doc, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise CrawlError(f"{where}: {key} must be a whole number, at least {low}: {value!r}")
    return value


def _variants(where, doc):
    if "variants" not in doc:
        return VARIANTS
    value = doc["variants"]
    if (not isinstance(value, list) or not value or not all(isinstance(v, str) and v in VARIANTS for v in value)
            or len(set(value)) != len(value)):
        raise CrawlError(f"{where}: variants must be a non-empty list of child and adult, each once: {value!r}")
    return tuple(value)


def _last(where, doc):
    """The destination indexes visited last: a list of distinct whole numbers, at least 0; none when the key is absent"""
    if "last" not in doc:
        return ()
    value = doc["last"]
    if (not isinstance(value, list) or not all(isinstance(i, int) and not isinstance(i, bool) and i >= 0 for i in value)
            or len(set(value)) != len(value)):
        raise CrawlError(f"{where}: last must be a list of distinct whole numbers, at least 0: {value!r}")
    return tuple(value)


def build_fields(text, where):
    """build.txt's key: value lines by key; its sha and closure lines are required"""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    if not fields.get("sha") or not fields.get("closure"):
        raise CrawlError("%s: build.txt needs sha and closure lines" % where)
    return fields


def copy_game(build_folder, device, game_dir):
    """Copies the build's game into the job: its warm copy for this device when there is one, else its game folder.
    Returns the cache it came from: warm or cold"""
    warm = os.path.join(build_folder, "warm", device, "game")
    if os.path.isdir(warm):
        source, cache = warm, "warm"
    else:
        source, cache = os.path.join(build_folder, "game"), "cold"
    try:
        done = subprocess.run(["cp", "-a", "--reflink=auto", source, game_dir], capture_output=True, text=True)
    except OSError as e:
        raise CrawlError("cannot run cp: %s" % e.strerror) from None
    if done.returncode:
        raise CrawlError("cannot copy %s: %s" % (source, done.stderr.strip()))
    return cache


def binary_hashes(game_dir):
    """sha256sum's output for the game's binaries, run in the job's game folder"""
    try:
        done = subprocess.run(["sha256sum", "hookshot", "hookshot_vk.so"], cwd=game_dir, capture_output=True, text=True)
    except OSError as e:
        raise CrawlError("cannot run sha256sum: %s" % e.strerror) from None
    if done.returncode:
        raise CrawlError("sha256sum of the game: %s" % done.stderr.strip())
    return done.stdout


def in_job(a):
    """The in-job half. It waits for the hold, copies the build's game into the job and starts it with the crawl file's
    save; it visits the destinations segment by segment (see visit_segment). A segment that ends with visits still to make
    is quit, and the game is started again from the save as the next segment, which makes the first of them; this goes on
    until every destination is visited or the crawl halts (a game that will not start again, or a check outside a visit
    that fails). Then it writes the cost map and the run's record. Exit 0, 1 or 2."""
    jobs.wait_hold(a.hold)
    job = os.path.abspath(a.in_job)
    job_id = os.path.basename(job)
    try:
        crawl_text = read_text(crawl_path(a.root))
        crawl = parse_crawl(crawl_text, crawl_path(a.root))
        build_txt = read_text(os.path.join(a.build_folder, "build.txt"))
        sha = build_fields(build_txt, a.build_folder)["sha"]
        game_dir = os.path.join(job, "game")
        cache = copy_game(a.build_folder, a.device, game_dir)
        hashes = binary_hashes(game_dir)
    except CrawlError as e:
        print("crawl: could not run: %s" % e)
        return 2
    device_env = dict(a.env or [])
    started = Started(job_id=job_id, job=job, cache=cache, sha=sha, build_txt=build_txt, hashes=hashes,
                      crawl_text=crawl_text, device_env=device_env)
    save = scenes.save_dir(a.root, crawl.save)
    try:
        g, launched = start_game(job, game_dir, device_env, save, crawl.slot, 1)
    except game.GameError as e:
        print("crawl: could not run: %s" % e)
        return 2
    segments, entries, problems = [], [], []
    plan = None   # the visits still to make: None until the first segment has listed the destinations
    while True:
        number = len(segments) + 1
        try:
            plan = measure_game(g, crawl, a.expect, number, plan, entries)
            halt = None
        except (bridge.BridgeError, game.GameError, game.ctl.Failed) as e:
            halt = str(e)
        exit_status = quit_game(g, problems)
        segments.append(Segment(number, launched, g.outputs(), exit_status))
        if halt is not None:
            problems.append(halt)
            not_reached(plan, number, halt, entries)
            break
        if not plan:
            break
        try:
            g, launched = start_game(job, game_dir, device_env, save, crawl.slot, number + 1)
        except game.GameError as e:
            why = "the game would not start again: %s" % e
            problems.append(why)
            not_reached(plan, number + 1, why, entries)
            break
    return finish(a, crawl, started, segments, entries, problems)


def start_game(job, game_dir, device_env, save, slot, number):
    """Starts segment number's game from the save, and returns the game with its launch: whole seconds since the epoch,
    taken before the start, where the segment's kernel log span begins. The first segment's saves go to the job's data
    folder; each restart's go to a folder of its own, since a start copies the save into an empty folder and refuses one
    that holds saves"""
    data = os.path.join(job, "data") if number == 1 else os.path.join(job, "data-%d" % number)
    launched = int(time.time())
    return game.start(job, game_dir, device_env, save=save, slot=slot, extra={"XDG_DATA_HOME": data}), launched


def quit_game(g, problems):
    """Quits the game and returns its exit status, or None when it did not quit: that problem is named"""
    try:
        return g.quit(QUIT_SECONDS)
    except game.GameError as e:
        problems.append(str(e))
        return None


def measure_game(g, crawl, expect, number, plan, entries):
    """Runs segment number's visits over one connection to the game, which is closed after. Returns the visits still to
    make"""
    conn = g.connect()
    try:
        return visit_segment(bridge.Session(conn), crawl, expect, number, plan, entries)
    finally:
        conn.close()


def visit_segment(session, crawl, expect, number, plan, entries):
    """Checks the game's device, then visits the plan's destinations in turn, appending each one's entry. A segment's start
    waits settle_frames presents, then for play with no event or load within the settle limit; when that does not come,
    the first destination's entry carries the error, nothing is sent for it, and the segment ends. Before each later visit
    the game must be in play with no event or load within the settle limit, and its connection must stay open; else the
    segment ends with that visit not sent.
    The segment also ends after a failed destination, and after a destination named in crawl.last when visits follow it.
    The visits it does not make are returned, and the next segment makes the first of them. The first segment lists the
    destinations (plan is None). A start is at the save's age, so the age goes with the segment's first arrival and with
    each variant's first."""
    session.enter_play(PRESSES_TO_PLAY)
    session.check_device(expect)
    if plan is None:
        listing = session.call("teleport").get("destinations")
        if not isinstance(listing, list):
            raise bridge.BridgeError("the game listed no destinations")
        plan = plan_visits(crawl, listing)
    remaining, switched = list(plan), None   # (switched: the variant whose age the game was last switched to)
    if remaining:   # the start: its presents, then play with no event or load, before the first visit is sent
        try:
            settle_presents(session, crawl)
            session.settle(bridge.SETTLE_SECONDS)
        except (bridge.BridgeError, game.ctl.Failed) as e:
            variant, index = remaining.pop(0)
            entries.append({"destination": index, "variant": variant, "segment": number, "error": str(e)})
            return remaining
    while remaining:
        if switched is not None:   # (the start has settled above; a later visit settles before it is sent)
            try:
                session.settle(bridge.SETTLE_SECONDS)
            except (bridge.BridgeError, game.ctl.Failed):
                break   # not in play with no event or load, or the connection closed: the visit stays in remaining
        variant, index = remaining.pop(0)
        entry = visit(session, crawl, index, variant, variant if variant != switched else None, number)
        entries.append(entry)
        switched = variant
        if "error" in entry or index in crawl.last:
            break   # a failed destination, or one named last: the visits still to make are for the next segment
    return remaining


def plan_visits(crawl, listing):
    """The visits in order: each variant's destinations with a map for it, in visit_order, as (variant, index) pairs"""
    return [(variant, dest["index"]) for variant in crawl.variants
            for dest in visit_order([d for d in listing if d.get(variant)], crawl.last)]


def not_reached(plan, number, why, entries):
    """An entry with its error for each visit the crawl did not make, naming the segment it would have been in"""
    entries.extend({"destination": index, "variant": variant, "segment": number, "error": "not reached: %s" % why}
                   for variant, index in plan or [])


def visit_order(dests, last):
    """The destinations with their indexes not in last, in the listing's order, then those in last in the order last gives.
    Each destination comes once."""
    return [d for d in dests if d["index"] not in last] + [d for i in last for d in dests if d["index"] == i]


def settle_presents(session, crawl):
    """Waits settle_frames presents, after an arrival and at a segment's start"""
    if crawl.settle_frames:
        session.call("wait", event="present", frames=crawl.settle_frames,
                     timeout=max(MIN_PRESENT_TIMEOUT, crawl.settle_frames))


def visit(session, crawl, index, variant, age, segment):
    """One destination in one variant, in segment: arrived on (switching to age first when given), settled, the stick turned
    right for pan_frames inside one window. An event may play in the window; a map change or a load fails it. A bridge
    refusal, or a connection the game closes, is the entry's error; nothing is retried."""
    entry = {"destination": index, "variant": variant, "segment": segment}
    try:
        entry["map"] = session.arrive(index, age=age)
        settle_presents(session, crawl)
        session.call("stick", stick="R", x=1, y=0, frames=crawl.pan_frames, wait=False)
        first, last = session.window(crawl.pan_frames, entry["map"], allow_event=True)
    except (bridge.BridgeError, game.ctl.Failed) as e:
        entry["error"] = str(e)
        return entry
    entry.update(first_replayed=first.get("replayed"), last_replayed=last.get("replayed"), draws=last.get("draws"))
    return entry


def finish(a, crawl, started, segments, entries, problems):
    """After the last segment has quit: each segment's trace costs, kernel log and fallback check, the verdict, the cost map
    and the run's record. Returns the exit status: 0 when every destination was measured and the run passed, 1 when not, 2
    when the record cannot be written."""
    machine = runrecord.machine_outputs()
    attach_traces(entries, {seg.number: decoded(seg.outputs.get("frame_trace")) for seg in segments})
    files, summaries = {}, []
    ends = [later.launched for later in segments[1:]] + [None]
    for seg, until in zip(segments, ends):
        log_text, trace_text, frame_text = (decoded(seg.outputs.get(key)) for key in ("log", "frame_trace", "frame_log"))
        kernel = kernel_text(seg.launched, until)
        summary = metrics.summary(log_text, trace_text, frame_text, kernel, seg.exit_status)
        summaries.append({"segment": seg.number, "summary": summary})
        fell = metrics.fallback(log_text) if log_text is not None else None
        if fell:
            problems.append("segment %d: the log shows the renderer fell back to another GPU: %s" % (seg.number, fell))
        for key, name in RECORD_OUTPUTS:
            if seg.outputs.get(key) is not None:
                files["segments/%d/%s" % (seg.number, name)] = seg.outputs[key]
        if kernel is not None:
            files["segments/%d/kernel.txt" % seg.number] = kernel.encode("utf-8")
    problems.extend("destination %s (%s): %s" % (e["destination"], e["variant"], e["error"])
                    for e in entries if "error" in e)
    count = sum(1 for e in entries if "error" not in e)
    print("destinations: %d measured, %d failed" % (count, len(entries) - count))
    if problems:
        for problem in problems:
            print("crawl: FAIL %s" % problem)
    else:
        print("crawl: pass")
    costmap = {"build_sha": started.sha, "device": a.device, "cache": started.cache, "pan_frames": crawl.pan_frames,
               "settle_frames": crawl.settle_frames, "entries": entries}
    files.update({"costmap.json": encode_json(costmap), "metrics.json": encode_json({"segments": summaries}),
                  "crawl.toml": started.crawl_text.encode("utf-8")})
    home, user = os.path.expanduser("~"), getpass.getuser()
    try:
        path = write_costmap(a.root, started.job_id, files["costmap.json"])
        print("costmap: %s" % path)
        facts = runrecord.facts(started.build_txt, read_diff(a.build_folder), started.hashes,
                                game.run_env(started.job, started.device_env), a.expect, machine, home, user)
        run_id, digest = runrecord.write(a.root, facts, files)
    except (OSError, runrecord.RunRecordError, record.RecordError) as e:
        print("crawl: could not write the record: %s" % e)
        return 2
    print("run: %s" % run_id)
    print("manifest: %s" % digest)
    return 1 if problems else 0


def kernel_text(launched, until):
    """The kernel log from launched to until (whole seconds since the epoch; to the end when until is None), as journalctl -k
    prints it; None when it cannot be read"""
    args = ["journalctl", "-k", "--since", "@%d" % launched]
    if until is not None:
        args += ["--until", "@%d" % until]
    try:
        done = subprocess.run(args + ["--no-pager"], capture_output=True)
    except OSError:
        return None
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None


def attach_traces(entries, traces):
    """Each entry's trace: the stats of every trace column over its window in its own segment's frame trace, or a gap
    naming why there are none. traces maps each segment's number to its frame trace text, None when it was not written"""
    parsed = {number: _trace_rows(text) for number, text in traces.items()}
    for entry in entries:
        rows, why = parsed.get(entry["segment"], (None, "frame trace: not written"))
        entry["trace"] = _window_trace(entry, rows, why)


def _trace_rows(trace_text):
    """(the frame trace's rows, None), or (None, why) when it is missing or refused"""
    if trace_text is None:
        return None, "frame trace: not written"
    try:
        return metrics.trace_rows(trace_text), None
    except metrics.MetricsError as e:
        return None, "frame trace: %s" % e


def _window_trace(entry, rows, why):
    if "error" in entry:
        return {"gap": "no window: the visit failed"}
    if rows is None:
        return {"gap": why}
    first, last = entry.get("first_replayed"), entry.get("last_replayed")
    if not isinstance(first, int) or not isinstance(last, int):
        return {"gap": "no replayed frames to window"}
    part = metrics.window(rows, first, last)
    if not part:
        return {"gap": "no trace frames from %d to %d" % (first, last)}
    return metrics.column_stats(part)


def write_costmap(root, job_id, data):
    folder = os.path.join(scenes.scene_dir(root), "costmaps")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, job_id + ".json")
    with open(path, "wb") as f:
        f.write(data)
    return path


def read_diff(build_folder):
    """The build's dirty diff, or '' when the checkout was clean"""
    try:
        with open(os.path.join(build_folder, "dirty.diff"), encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def decoded(data):
    """An output's bytes as text, or None when it was not written"""
    return None if data is None else data.decode("utf-8", "replace")


def encode_json(value):
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")
