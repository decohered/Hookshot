"""hx play: a capture (hx capture's run) replayed once through its own build's renderer on one device, with a PNG of every
present, written as one run. The check job builds the player from the build's source (gpu/player/rx_player.cpp, with the
closure's compiler, as tests/test_harness_capture.py builds it) into its game folder, and runs it from there on the capture
with the job's own hookshot_vk.so, as the live run runs the game from there. The run holds the verdict and, per present,
the play's diff against the live run: the count of pixels that differ and the largest channel difference. Two images of
different sizes count every pixel of the larger as differing. A present that only one run has is marked with that run.

With --against RUN, another play of the same capture on the same device, the run also records their spread (the largest
differing-pixel count between the two plays, over the presents both have) and lists the presents whose play-vs-live count
exceeds it, and the presents only one of the runs has. Listed presents do not change the verdict.

    hx play <capture-run> --device D [--against RUN]

Exit status: 0 pass, 1 FAIL (the verdict: the build or the player failed, the player named another device or fell back
to another GPU, it wrote no present, or its presents cannot be compared), 2 the check could not run (an unknown device, a
malformed or unknown run id, a run that is not a capture, an --against run that is not a play of that capture on that
device, a device the machine does not list, a job that will not start, a run that cannot be written). The client returns
the job's status; 255 is ssh lost, and the job goes on.

Only stdlib: the PNG decoder reads the renderer's 8-bit RGB, non-interlaced PNGs with zlib and the five row filters.
"""
import argparse
import json
import os
import posixpath
import re
import shlex
import struct
import subprocess
import sys
import zlib

import config
import hx_capture
import hx_smoke
import jobs
import record
import remote

PLAY = "play"
CXX = ("clang++",)
PLAYER_SOURCE = ("gpu", "player", "rx_player.cpp")
# the device the renderer made (gpu/vk/vk_main.cpp:1589); its listing of every device (vk_main.cpp:194, "vk: device <n>:
# <name>, Vulkan ..., type <n>") is not this line
DEVICE_LINE = re.compile(r"vk: (.+?), Vulkan [0-9]+\.[0-9]+, driver (?:0x[0-9a-f]+|0); mesh shaders ")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BPP = 3
NO_JUDGEMENT = {"versus_live": None, "versus_against": None, "spread": None, "listed": None}


class PlayError(Exception):
    pass


def parse_client(argv):
    p = argparse.ArgumentParser(prog="hx play", description="Replay a capture through its build's renderer on one "
                                                           "device, with a PNG of every present, and write the run.")
    p.add_argument("run", metavar="RUN", help="the capture run: its presents are the live comparison")
    p.add_argument("--device", required=True, metavar="D", help="a device of the configuration")
    p.add_argument("--against", metavar="RUN", help="another play of this capture on D: the spread between the two plays "
                                                    "is recorded, and the presents that differ from live by more")
    return p.parse_args(argv)


def parse_in_job(argv):
    p = argparse.ArgumentParser(prog="hx play --in-job", description="The in-job half, run inside a check job on the host.")
    p.add_argument("job", metavar="JOB", help="the job's folder")
    p.add_argument("--hold", required=True, metavar="PATH", help="the hold file: no launch while it exists")
    p.add_argument("--expect", required=True, metavar="NAME", help="the device's Vulkan device name")
    p.add_argument("--env", type=hx_smoke.pair, action="append", metavar="NAME=VALUE",
                   help="the device's environment for the player")
    p.add_argument("--device", required=True, metavar="D", help="the device's name in the configuration")
    p.add_argument("--live", required=True, metavar="RUN", help="the capture run")
    p.add_argument("--against", metavar="RUN", help="another play of the capture on the device")
    p.add_argument("--record", required=True, metavar="DIR", help="the record root")
    p.add_argument("--build-folder", required=True, metavar="DIR", help="the build's folder on the host")
    p.add_argument("--closure", required=True, metavar="PATH", help="the build's closure, a /nix/store path")
    return p.parse_args(argv)


def main(argv):
    try:
        if argv[:1] == ["--in-job"]:
            return in_job(parse_in_job(argv[1:]))
        return client(parse_client(argv))
    except (PlayError, hx_capture.CaptureError, hx_smoke.SmokeError, config.ConfigError, jobs.JobError,
            record.RecordError, OSError) as e:
        print("play: %s" % e, file=sys.stderr)
        return 2


def client(a):
    cfg = config.load()
    device = cfg.devices.get(a.device)
    if device is None:
        raise PlayError("no device %r; the devices are: %s" % (a.device, ", ".join(sorted(cfg.devices))))
    for run_id in (a.run, a.against):
        if run_id is not None and not record.RUN_ID.fullmatch(run_id):
            raise PlayError("not a run id: %r" % run_id)
    live = read_run_doc(cfg, a.run)
    against = None if a.against is None else read_run_doc(cfg, a.against)
    refusal = pair_refusal(a.run, live, a.against, against, a.device)
    if refusal is not None:
        raise PlayError(refusal)
    capture_build = live.get("build")
    if not isinstance(capture_build, str) or not record.GIT_COMMIT.fullmatch(capture_build):
        raise PlayError("run %s names no build commit: %r" % (a.run, capture_build))
    folder = jobs.build_dir(cfg, capture_build)
    info = hx_smoke.read_build(cfg, folder)
    sha, closure = info.get("sha"), info.get("closure")
    if sha != capture_build:
        raise PlayError("the build in %s says sha %s, not the capture's %s" % (folder, sha, capture_build))
    if not closure or not record.NIX_STORE_PATH.fullmatch(closure):
        raise PlayError("%s: build.txt's closure must be a /nix/store/ path" % folder)
    print("build: %s" % sha)
    job_id = jobs.new_id("check")
    job = jobs.job_dir(cfg, job_id)
    record_root = posixpath.join(cfg.root, "results")
    argv = [cfg.enter_closure, closure, "python", "-I", posixpath.join(job, "tools", "scripts", "harness", "hx.py"),
            "play", "--in-job", job, "--hold", cfg.hold, "--expect", device.expect, "--device", a.device,
            "--live", a.run]
    if a.against is not None:
        argv += ["--against", a.against]
    argv += ["--record", record_root, "--build-folder", folder, "--closure", closure]
    for name, value in device.env.items():
        argv += ["--env", "%s=%s" % (name, value)]
    jobs.start(cfg, job_id, argv, game=posixpath.join(folder, "game"))
    print("job: %s" % job_id)
    status = jobs.follow(cfg, job_id)
    if status == 255:
        print("play: ssh was lost; the job goes on: hx stop %s" % job_id)
    return status


def read_run_doc(cfg, run_id):
    """A run's capture.json, read on the host; a run that cannot be read is a PlayError"""
    path = posixpath.join(cfg.root, "results", "runs", run_id, "files", "capture.json")
    try:
        text = remote.run(cfg.host, "cat " + shlex.quote(path)).stdout
    except remote.RemoteError as e:
        raise PlayError("run %s: cannot read its capture.json: %s" % (run_id, e)) from None
    return parse_doc(run_id, text)


def parse_doc(run_id, text):
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise PlayError("run %s: its capture.json is not JSON: %s" % (run_id, e)) from None
    if not isinstance(doc, dict):
        raise PlayError("run %s: its capture.json is not an object" % run_id)
    return doc


def pair_refusal(live_id, live, against_id, against, device):
    """Why the play cannot run against live, or None: live is a capture run, and against (when given) is a play of that
    capture on device"""
    if live.get("kind") == PLAY or not isinstance(live.get("capture"), str):
        return "run %s is not a capture run" % live_id
    if against is None:
        return None
    if against.get("kind") != PLAY:
        return "run %s is not a play" % against_id
    if against.get("capture") != live["capture"]:
        return "run %s plays another capture than run %s" % (against_id, live_id)
    if against.get("device") != device:
        return "run %s plays on %r, not %r" % (against_id, against.get("device"), device)
    return None


def in_job(a):
    """The in-job half: it waits for the hold, reads the capture and any --against play from the record, builds the player
    from the build's source, runs it once on the capture, judges its presents and writes the run."""
    jobs.wait_hold(a.hold)
    live_doc, live = load_run(a.record, a.live)
    against_doc, against = load_run(a.record, a.against) if a.against is not None else (None, None)
    refusal = pair_refusal(a.live, live_doc, a.against, against_doc, a.device)
    if refusal is not None:
        raise PlayError(refusal)
    device_env = dict(a.env or [])
    facts = hx_capture.run_facts(a.build_folder, os.path.join(a.job, "game"), a.closure,
                                 {**device_env, **hx_capture.SWITCHES}, a.expect)
    shots = os.path.join(a.job, "shots")
    os.makedirs(shots)
    play_log = os.path.join(a.job, "play.log")
    player, why = build_player(a.build_folder, a.job)
    plays, judged = {}, None
    if player is not None:
        env = {**os.environ, **jobs.launch_env(a.job, device_env), **hx_capture.SWITCHES, "RX_VK_SHOTS": shots}
        code = run_player(player, live_doc["capture"], os.path.join(a.job, "game", "hookshot_vk.so"), env, play_log)
        plays = hx_capture.present_shots(shots)
        log = read_text(play_log)
        why = player_verdict(code, log, a.expect, plays)
        try:
            judged = judge(plays, live, against)
        except PlayError as e:
            why = why or "cannot compare the presents: %s" % e
        print("device: %s" % (", ".join(device_names(log)) or "none named"))
        print("presents: %d" % len(plays))
    verdict = "pass" if why is None else "FAIL: %s" % why
    print("play: %s" % verdict)
    doc = {
        "kind": PLAY,
        "capture": live_doc["capture"],
        "capture_build": live_doc.get("build"),
        "build": facts["shas"]["hookshot"],
        "device": a.device,
        "live": a.live,
        "against": a.against,
        "presents": None if player is None else len(plays),
        "verdict": verdict,
        **(judged or NO_JUDGEMENT),
    }
    files = {"capture.json": (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"),
             "build.log": read_bytes(os.path.join(a.job, "build.log"))}
    if player is not None:
        files["play.log"] = read_bytes(play_log)
    run_id, _ = record.write_run(a.record, facts, files, plays, journal=record.write_journal)
    print("run: %s" % run_id)
    return 0 if why is None else 1


def load_run(root, run_id):
    """A run's capture.json and its screenshots' bytes, read from the record root: (the document, {name: bytes}). A
    screenshot whose bytes are not the ones its name is a PlayError."""
    manifest, _ = record.read_manifest(root, run_id)
    doc = parse_doc(run_id, read_text(os.path.join(root, "runs", run_id, "files", "capture.json")))
    shots = {}
    for name, entry in manifest["screenshots"].items():
        data = read_bytes(os.path.join(root, "screenshots", entry["sha256"]))
        if record.sha256_bytes(data) != entry["sha256"]:
            raise PlayError("run %s: screenshot %s is not the bytes it is named by" % (run_id, name))
        shots[name] = data
    return doc, shots


def build_player(build_folder, job):
    """Compiles and links the player from the build's own source with the closure's compiler, as the capture test does,
    into the job's game folder, beside the game's executable and hookshot_vk.so: the renderer reads the files it reads
    beside its own executable (gpu/vk/vk_shader.cpp, gpu/vk/vk_progcache.cpp). The link exports hos_posix_* as the game's
    link does (nx2/build.py), where the renderer looks them up (gpu/vk/vk_memory.cpp, host/posix/win32_posix.c). Returns
    (the executable, None), or (None, why) when a step fails; the compilers' output is in build.log."""
    source = os.path.join(build_folder, "src", *PLAYER_SOURCE)
    game = os.path.join(job, "game")
    obj, exe = os.path.join(game, "rx_player.o"), os.path.join(game, "rx_player")
    steps = ([*CXX, "-std=c++20", "-c", source, "-o", obj],
             [*CXX, obj, "-ldl", "-Wl,--export-dynamic-symbol=hos_posix_*", "-o", exe])
    with open(os.path.join(job, "build.log"), "wb") as log:
        for argv in steps:
            code = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT).returncode
            if code:
                return None, "build: %s exited %d (build.log)" % (argv[0], code)
    return exe, None


def run_player(player, capture, library, env, log):
    """Runs the player once on the capture, its output in log. Returns its exit status, or None when it does not end within
    hx_smoke.WAIT_SECONDS (it is killed)"""
    with open(log, "wb") as out:
        try:
            return subprocess.run([player, capture, "--renderer", library], env=env, stdin=subprocess.DEVNULL,
                                  stdout=out, stderr=subprocess.STDOUT, timeout=hx_smoke.WAIT_SECONDS).returncode
        except subprocess.TimeoutExpired:
            return None


def device_names(log):
    """The Vulkan device each of the renderer's device lines in the player's log names, in order"""
    return DEVICE_LINE.findall(log)


def player_verdict(code, log, expect, plays):
    """Why the player's run fails, or None: it ends with status 0 on the expected device, with no fallback to another GPU,
    and it writes at least one present"""
    if code is None:
        return "player: no end within %d s" % hx_smoke.WAIT_SECONDS
    if code:
        return "player: exit status %d" % code
    named = device_names(log)
    if not named:
        return "the log names no Vulkan device"
    other = sorted({name for name in named if name != expect})
    if other:
        return "the renderer is on %s, not %r" % (", ".join(repr(name) for name in other), expect)
    if hx_smoke.FALLBACK in log:
        return "the log shows the renderer fell back to another GPU (%s)" % hx_smoke.FALLBACK
    if not plays:
        return "the player wrote no presents"
    return None


def judge(plays, live, against):
    """The judgement of a play's presents (name -> PNG bytes). versus_live: each present's diff against live. With against
    (another play of the same capture on the same device, or None): versus_against, their spread (the largest
    differing-pixel count over the presents both have, 0 when they share none), and the listed presents: those whose
    play-vs-live count exceeds the spread, and those only one of play and live has. Without against, spread and listed
    are None."""
    versus_live = compare_presents(plays, live, "play", "live")
    if against is None:
        return {"versus_live": versus_live, "versus_against": None, "spread": None, "listed": None}
    versus_against = compare_presents(plays, against, "play", "against")
    spread = max((r["pixels"] for r in versus_against.values() if "pixels" in r), default=0)
    listed = [name for name, r in versus_live.items() if "pixels" not in r or r["pixels"] > spread]
    return {"versus_live": versus_live, "versus_against": versus_against, "spread": spread, "listed": listed}


def compare_presents(mine, theirs, mine_label, theirs_label):
    """Each present's diff, by name in order; a present only one side has is {"only": that side's label}"""
    diffs = {}
    for name in sorted(set(mine) | set(theirs)):
        if name not in theirs:
            diffs[name] = {"only": mine_label}
        elif name not in mine:
            diffs[name] = {"only": theirs_label}
        else:
            diffs[name] = diff_png(mine[name], theirs[name])
    return diffs


def diff_png(a, b):
    """The difference of two PNGs: {"pixels": the count of pixels that differ, "max_channel": the largest difference of any
    channel of any pixel}. Two images of different sizes count every pixel of the larger as differing, with no channel
    difference (max_channel null) and their sizes."""
    if a == b:
        return {"pixels": 0, "max_channel": 0}
    wa, ha, pa = decode_png(a)
    wb, hb, pb = decode_png(b)
    if (wa, ha) != (wb, hb):
        return {"pixels": max(wa * ha, wb * hb), "max_channel": None, "sizes": [[wa, ha], [wb, hb]]}
    if pa == pb:
        return {"pixels": 0, "max_channel": 0}
    pixels, largest = 0, 0
    for i in range(0, len(pa), 3):
        x, y = pa[i:i + 3], pb[i:i + 3]
        if x != y:
            pixels += 1
            largest = max(largest, max(abs(p - q) for p, q in zip(x, y)))
    return {"pixels": pixels, "max_channel": largest}


def decode_png(data):
    """(width, height, RGB bytes) of a PNG the renderer writes: 8-bit RGB (colour type 2), not interlaced. A file that is
    not such a PNG, or a chunk whose CRC fails, is a PlayError."""
    if not data.startswith(PNG_SIGNATURE):
        raise PlayError("not a PNG")
    header, idat, pos = None, [], len(PNG_SIGNATURE)
    while True:
        if pos + 12 > len(data):
            raise PlayError("PNG ends inside a chunk")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag, end = data[pos + 4:pos + 8], pos + 12 + length
        if end > len(data):
            raise PlayError("PNG ends inside its %s chunk" % tag.decode("ascii", "replace"))
        body = data[pos + 8:end - 4]
        if struct.unpack(">I", data[end - 4:end])[0] != zlib.crc32(tag + body):
            raise PlayError("PNG's %s chunk fails its CRC" % tag.decode("ascii", "replace"))
        if tag == b"IHDR":
            header = body
        elif tag == b"IDAT":
            idat.append(body)
        pos = end
        if tag == b"IEND":
            break
    if header is None or len(header) != 13 or not idat:
        raise PlayError("PNG lacks a header or its data")
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", header)
    if (depth, colour, interlace) != (8, 2, 0):
        raise PlayError("PNG is %d-bit, colour type %d, interlace %d: only 8-bit RGB, not interlaced, is read"
                        % (depth, colour, interlace))
    if width < 1 or height < 1:
        raise PlayError("PNG has no pixels")
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as e:
        raise PlayError("PNG data does not inflate: %s" % e) from None
    return width, height, unfilter(raw, width, height)


def unfilter(raw, width, height):
    """The RGB bytes of PNG scanlines: each row is a filter type byte then its filtered bytes"""
    stride = width * BPP
    if len(raw) != height * (stride + 1):
        raise PlayError("PNG data is %d bytes, not the %d its %dx%d pixels take"
                        % (len(raw), height * (stride + 1), width, height))
    out, prev = bytearray(), bytes(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        if kind > 4:
            raise PlayError("PNG row %d has filter type %d" % (y, kind))
        row = FILTERS[kind](raw[start + 1:start + 1 + stride], prev)
        out += row
        prev = row
    return bytes(out)


def _none(row, prev):
    return bytearray(row)


def _sub(row, prev):
    cur = bytearray(row)
    for i in range(BPP, len(cur)):
        cur[i] = (cur[i] + cur[i - BPP]) & 255
    return cur


def _up(row, prev):
    return bytearray((x + p) & 255 for x, p in zip(row, prev))


def _average(row, prev):
    cur = bytearray(row)
    for i in range(len(cur)):
        left = cur[i - BPP] if i >= BPP else 0
        cur[i] = (cur[i] + (left + prev[i]) // 2) & 255
    return cur


def _paeth(row, prev):
    cur = bytearray(row)
    for i in range(len(cur)):
        a = cur[i - BPP] if i >= BPP else 0
        b = prev[i]
        c = prev[i - BPP] if i >= BPP else 0
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        cur[i] = (cur[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
    return cur


FILTERS = (_none, _sub, _up, _average, _paeth)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def read_text(path):
    return read_bytes(path).decode("utf-8", errors="replace")
