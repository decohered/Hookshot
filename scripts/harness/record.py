"""The harness's record of runs: one run written into a record root exactly once, anchored in the user's journal, and read
back. A record root holds runs/<id>/ (manifest.json and files/) and screenshots/<sha256>, which sits outside every run
folder and holds one copy per content. Each manifest names the run before it and that run's manifest hash, and the
journal line for a run carries its manifest hash, so a run changed or removed after it was written no longer matches the
chain or the journal. Nothing here edits or removes a run.

A run's facts come from its build (build_facts) and its machine (machine_facts, from the outputs of MACHINE_COMMANDS).
These parse text only and run no command; a value that cannot be gathered is recorded as a gap, never omitted.

Stdlib only. The commands import this module as a sibling; tests/test_harness_record.py checks it.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

FORMAT = 1
JOURNAL_ID = "hookshot-harness"
FACT_KEYS = ("shas", "diff", "binaries", "closure", "env", "gpu", "driver", "mesa", "kernel", "clocks", "load")

RUN_ID = re.compile(r"[0-9]{6}")
JOURNAL_LINE = re.compile(r"run=([0-9]{6}) manifest=([0-9a-f]{64})")


class RecordError(Exception):
    """Every refusal: a run that cannot be written, a manifest that cannot be read, a journal that cannot be used"""


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _entry(data):
    return {"sha256": sha256_bytes(data), "size": len(data)}


def _run_path(name):
    """A run file's path: relative and POSIX, inside the run's files folder (no root, drive, '..', '.' or backslash)"""
    if not isinstance(name, str) or "\\" in name or PureWindowsPath(name).drive:
        raise RecordError(f"not a relative path inside a run: {name!r}")
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise RecordError(f"not a relative path inside a run: {name!r}")
    return path


def run_ids(root):
    """The ids of the runs under a record root, ascending: the six-digit folders under runs/, and nothing else"""
    runs = Path(root) / "runs"
    if not runs.is_dir():
        return []
    return sorted(p.name for p in runs.iterdir() if RUN_ID.fullmatch(p.name) and p.is_dir())


def read_manifest(root, run_id):
    """A run's manifest, parsed, and the sha256 of its manifest.json bytes"""
    if not RUN_ID.fullmatch(run_id):
        raise RecordError(f"not a run id: {run_id!r}")
    try:
        data = (Path(root) / "runs" / run_id / "manifest.json").read_bytes()
    except OSError as e:
        raise RecordError(f"run {run_id}: cannot read its manifest: {e}") from e
    try:
        manifest = json.loads(data.decode("utf-8"))
    except ValueError as e:
        raise RecordError(f"run {run_id}: its manifest is not JSON: {e}") from e
    return manifest, sha256_bytes(data)


def journal_line(run_id, manifest_sha256):
    return f"run={run_id} manifest={manifest_sha256}"


def parse_journal(text):
    """(run id, manifest sha256) for each journal line of that shape; the journal's other lines are skipped"""
    found = []
    for line in text.splitlines():
        m = JOURNAL_LINE.fullmatch(line)
        if m:
            found.append((m.group(1), m.group(2)))
    return found


def _tool(run, argv, **kwargs):
    """Runs a journal tool with its output captured; a missing program or a nonzero exit is a RecordError"""
    try:
        done = run(argv, capture_output=True, **kwargs)
    except OSError as e:
        raise RecordError(f"journal: cannot run {argv[0]}: {e}") from e
    if done.returncode:
        raise RecordError(f"journal: {argv[0]} exited {done.returncode}: "
                          f"{done.stderr.decode('utf-8', 'replace').strip()}")
    return done


def write_journal(message, run=subprocess.run):
    """Anchors a message in the user's journal under JOURNAL_ID"""
    _tool(run, ["systemd-cat", "-t", JOURNAL_ID], input=message.encode("utf-8"))


def read_journal(run=subprocess.run):
    """The journal's lines under JOURNAL_ID, as journalctl prints them"""
    done = _tool(run, ["journalctl", "--user", "-t", JOURNAL_ID, "-o", "cat", "--no-pager"])
    return done.stdout.decode("utf-8", "replace")


def write_run(root, facts, files, screenshots, journal=write_journal):
    """Writes one run into a record root and anchors it; returns (run id, sha256 of its manifest.json).

    Refusals that need no disk are checked first, so a refused write leaves the root as it was. The rest is built in a
    staging folder under runs/ and moved into place by one rename, which fails when another writer took the id: a run's
    folder is complete or absent. A screenshot moves into screenshots/ only once whole, and is kept once per content. The
    journal line comes last, once the folder is complete.
    """
    root = Path(root)
    missing = [key for key in FACT_KEYS if key not in facts]
    if missing:
        raise RecordError("facts lack " + ", ".join(missing))
    entries = {}
    for name, data in files.items():
        key = _run_path(name).as_posix()
        if key in entries:
            raise RecordError(f"two files are both at {key!r}")
        entries[key] = data

    staging = None
    try:
        runs, blobs = root / "runs", root / "screenshots"
        ids = run_ids(root)
        run_id = f"{int(ids[-1]) + 1 if ids else 1:06d}"
        prior = None
        if ids:
            _, prior_sha = read_manifest(root, ids[-1])
            prior = {"id": ids[-1], "sha256": prior_sha}
        try:
            text = json.dumps({
                "format": FORMAT,
                "id": run_id,
                "written": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "prior": prior,
                "files": {key: _entry(data) for key, data in entries.items()},
                "screenshots": {name: _entry(data) for name, data in screenshots.items()},
                "facts": dict(facts),
            }, indent=2, sort_keys=True) + "\n"
        except (TypeError, ValueError) as e:
            raise RecordError(f"run {run_id}: facts are not JSON: {e}") from e
        manifest = text.encode("utf-8")
        manifest_sha = sha256_bytes(manifest)

        runs.mkdir(parents=True, exist_ok=True)
        blobs.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=runs))
        for data in screenshots.values():
            digest = sha256_bytes(data)
            blob = blobs / digest
            if blob.exists():
                if blob.read_bytes() != data:
                    raise RecordError(f"screenshots/{digest} holds other bytes")
            else:
                whole = staging / digest
                whole.write_bytes(data)
                os.replace(whole, blob)
        (staging / "files").mkdir()
        for key, data in entries.items():
            path = staging / "files" / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (staging / "manifest.json").write_bytes(manifest)
        try:
            os.rename(staging, runs / run_id)
        except OSError as e:
            raise RecordError(f"run {run_id} could not be claimed (another writer may have taken it): {e}") from e
        staging = None
    except OSError as e:
        raise RecordError(f"run not written under {root}: {e}") from e
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    try:
        journal(journal_line(run_id, manifest_sha))
    except Exception as e:
        raise RecordError(f"run {run_id} is written but not journalled: {e}") from e
    return run_id, manifest_sha


MACHINE_COMMANDS = {
    "nvidia_smi": ["nvidia-smi", "--query-gpu=uuid,name,driver_version,clocks.gr,clocks.sm,clocks.mem",
                   "--format=csv,noheader,nounits"],
    "vulkaninfo": ["vulkaninfo", "--summary"],
    "kernel": ["cat", "/proc/version"],
    "load": ["cat", "/proc/loadavg"],
    "cpu_clocks": ["sh", "-c", "grep . /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq"],
}

VULKAN_GPU = re.compile(r"GPU([0-9]+):\s*")
VULKAN_FIELD = re.compile(r"\s+(\w+)\s*=\s*(.*)")
MESA_VERSION = re.compile(r"Mesa (\S+)")
NVIDIA_VENDOR = "0x10de"
DRIVER_FIELDS = ("driverID", "driverName", "driverInfo", "driverVersion")
CPU_FREQ = re.compile(r"/sys/devices/system/cpu/(cpu[0-9]+)/cpufreq/scaling_cur_freq:([0-9]+)")
KERNEL_RELEASE = re.compile(r"Linux version (\S+)")
LOADAVG = re.compile(r"([0-9]+\.[0-9]+) ([0-9]+\.[0-9]+) ([0-9]+\.[0-9]+) [0-9]+/[0-9]+ [0-9]+")
SHA256SUM_LINE = re.compile(r"([0-9a-f]{64}) [ *](.+)")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
NIX_STORE_PATH = re.compile(r"/nix/store/[0-9a-z]{32}-[^/\s]+")


def _gap(reason):
    return {"gap": reason}


def build_facts(shas, diff, sha256sum, closure, env):
    """A run's build facts: each source's commit, the diff and the environment kept verbatim, the sha256 of each binary
    from sha256sum's output, and the closure's store path. Malformed input is a RecordError."""
    if not isinstance(shas, dict) or not shas:
        raise RecordError("shas: no source names a commit")
    for source, commit in shas.items():
        if not (isinstance(source, str) and source and isinstance(commit, str) and GIT_COMMIT.fullmatch(commit)):
            raise RecordError(f"shas: {source!r} does not name a 40-hex commit")
    if not isinstance(diff, str):
        raise RecordError("diff: not text")
    if not isinstance(closure, str) or not NIX_STORE_PATH.fullmatch(closure):
        raise RecordError(f"closure: not a /nix/store/ path: {closure!r}")
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise RecordError("env: not a mapping of names to text values")
    return {"shas": dict(shas), "diff": diff, "binaries": _binaries(sha256sum), "closure": closure, "env": dict(env)}


def _binaries(text):
    """Each binary's sha256 by path, from sha256sum's output (text or binary mode), each path once"""
    if not isinstance(text, str):
        raise RecordError("binaries: sha256sum output is not text")
    binaries = {}
    for line in text.splitlines():
        found = SHA256SUM_LINE.fullmatch(line)
        if found is None:
            raise RecordError(f"binaries: not a sha256sum line: {line!r}")
        path = found.group(2)
        if path in binaries:
            raise RecordError(f"binaries: {path!r} is hashed twice")
        binaries[path] = found.group(1)
    if not binaries:
        raise RecordError("binaries: no sha256sum output")
    return binaries


def machine_facts(chosen_gpu, outputs):
    """A run's machine facts from its commands' outputs (MACHINE_COMMANDS' names to stdout, None when a command failed or
    is absent). The chosen GPU is its index in vulkaninfo's listing: its block, its driver, and the Mesa version listed.
    The clocks of an NVIDIA GPU are matched to its nvidia-smi row by UUID. A value that cannot be gathered is a gap."""
    vulkan = outputs.get("vulkaninfo")
    if vulkan is None:
        blocks, chosen, missing = {}, None, "vulkaninfo: no output"
    else:
        blocks = _vulkan_blocks(vulkan)
        chosen = blocks.get(chosen_gpu)
        missing = f"vulkaninfo: GPU{chosen_gpu} is not listed"
    if chosen is None:
        gpu, driver, gpu_clocks = _gap(missing), _gap(missing), _gap(missing)
    else:
        nvidia = _nvidia_row(chosen, outputs.get("nvidia_smi"))
        gpu, driver, gpu_clocks = chosen, _driver(chosen_gpu, chosen, nvidia), _gpu_clocks(nvidia)
    return {
        "gpu": gpu,
        "driver": driver,
        "mesa": _mesa(blocks) if vulkan is not None else _gap(missing),
        "kernel": _kernel(outputs.get("kernel")),
        "clocks": {"gpu": gpu_clocks, "cpu_khz": _cpu_khz(outputs.get("cpu_clocks"))},
        "load": _load(outputs.get("load")),
    }


def _vulkan_blocks(text):
    """Each GPU<n> block of a vulkaninfo --summary text, by index: its key = value fields"""
    blocks, current = {}, None
    for line in text.splitlines():
        header = VULKAN_GPU.fullmatch(line)
        field = VULKAN_FIELD.fullmatch(line)
        if header:
            current = blocks.setdefault(int(header.group(1)), {})
        elif field and current is not None:
            current[field.group(1)] = field.group(2).strip()
        else:
            current = None
    return blocks


def _field(index, block, key):
    value = block.get(key)
    return value if value is not None else _gap(f"vulkaninfo: GPU{index} lists no {key}")


def _mesa(blocks):
    """The Mesa version of the first device, by index, whose driverInfo begins 'Mesa <version>'"""
    for index in sorted(blocks):
        found = MESA_VERSION.match(blocks[index].get("driverInfo", ""))
        if found:
            return found.group(1)
    return _gap("vulkaninfo: no Mesa driver is listed")


def _nvidia_row(block, text):
    """The chosen GPU's nvidia-smi row as its six fields, or None with the reason there is none"""
    if text is None:
        return None, "nvidia_smi: no output"
    uuid = block.get("deviceUUID")
    if uuid is None:
        return None, "vulkaninfo: the chosen GPU lists no deviceUUID"
    for line in text.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) == 6 and fields[0] == "GPU-" + uuid:
            return fields, None
    return None, "nvidia_smi: no parsable row for the chosen GPU"


def _driver(index, block, nvidia):
    """The chosen device's Vulkan driver; on an NVIDIA device, also the driver version nvidia-smi reports for it"""
    vulkan = {key: _field(index, block, key) for key in DRIVER_FIELDS}
    if block.get("vendorID") != NVIDIA_VENDOR:
        return {"vulkan": vulkan}
    fields, reason = nvidia
    return {"vulkan": vulkan, "nvidia_smi": _gap(reason) if fields is None else fields[2]}


def _gpu_clocks(nvidia):
    """The chosen GPU's graphics, SM and memory clocks in MHz"""
    fields, reason = nvidia
    if fields is None:
        return _gap(reason)
    return {"graphics_mhz": _mhz(fields[3]), "sm_mhz": _mhz(fields[4]), "memory_mhz": _mhz(fields[5])}


def _mhz(text):
    return int(text) if text.isdigit() else _gap(f"nvidia_smi: clock {text!r} is not a number")


def _cpu_khz(text):
    """Each CPU's current frequency in kHz, from the cpufreq grep's '<path>:<kHz>' lines"""
    if text is None:
        return _gap("cpu_clocks: no output")
    khz = {}
    for line in text.strip().splitlines():
        found = CPU_FREQ.fullmatch(line)
        if found is None:
            return _gap("cpu_clocks: a line is not a CPU's frequency")
        khz[found.group(1)] = int(found.group(2))
    return khz or _gap("cpu_clocks: no CPU frequency")


def _kernel(text):
    """The kernel release only: the rest of /proc/version names the machine's build host"""
    if text is None:
        return _gap("kernel: no output")
    found = KERNEL_RELEASE.match(text)
    return found.group(1) if found else _gap("kernel: no 'Linux version' release in the output")


def _load(text):
    if text is None:
        return _gap("load: no output")
    found = LOADAVG.fullmatch(text.strip())
    if found is None:
        return _gap("load: not a /proc/loadavg line")
    return {"1min": float(found.group(1)), "5min": float(found.group(2)), "15min": float(found.group(3))}
