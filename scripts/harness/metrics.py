"""A run's metrics from its outputs: the renderer's log, the frame trace and the frame log it writes beside it, the
kernel log and the exit status. Every value is read as text and kept raw. An output that is missing or malformed is
refused (MetricsError), and summary records the refusal as a named gap in the sections it feeds, never as a guess. The
trace keeps all 23 columns: column_stats gives each one but frame its n, mean, p50, p95, p99 and max (nearest rank).

Stdlib only. The commands import this module as a sibling; tests/test_harness_metrics.py checks it.
"""
import math
import re

TRACE_COLUMNS = ("frame", "ms", "pipe_waits", "pipe_wait_ms", "left_out", "translate_ms", "uploads", "upload_mb",
                 "textures", "memsync_ms", "pipelines_ms", "uploads_ms", "programs_ms", "slot_wait_ms", "gpu_behind_ms",
                 "draw_ms", "desc_ms", "cmds_ms", "tex_lookup_ms", "textures_gone", "late_wait_ms", "readback_wait_ms",
                 "tex_frame_ms")
FRAME_LOG_COLUMNS = ("frame", "time_s", "frametime_ms")
REPLAY_PARTS = ("submit", "present", "draw", "descriptors", "texture lookups", "uploads", "pipelines", "programs",
                "commands", "dispatch", "memory sync", "queue submit", "slot wait")
RSS_KEYS = ("working_set_mb", "peak_mb", "private_mb")
VRAM_KEYS = ("used_mb", "allowed_mb")
LOG_SECTIONS = ("replay_ms", "gpu_ms", "longest_submission_ms", "rss_mb", "vram_mb", "fallback")

REPLAY_LINE = "vk: a frame's replay time (ms):"
REPLAY_PART = re.compile(r"([a-z][a-z ]*?) (-?[0-9]+\.[0-9]+)")
GPU_LINE = "vk: GPU time "
GPU = re.compile(r"vk: GPU time (-?[0-9]+\.[0-9]+) ms a frame \(the longest submission (-?[0-9]+\.[0-9]+) ms\)")
RSS_LINE = "vk: memory: working set "
RSS = re.compile(r"working set ([0-9]+) MB \(peak ([0-9]+)\), private ([0-9]+) MB")
VRAM_LINE = "vk: VRAM: "
VRAM = re.compile(r"VRAM: ([0-9]+) MB used of ([0-9]+) MB allowed")
HANG_LINES = ("vk: the GPU has finished nothing for", "vk: GPU hang: ", "vk: FATAL: ")
FALLBACK = "falling back to auto selection"
RING_TIMEOUT = re.compile(r"amdgpu.*\bring \S+ timeout")
XID = "NVRM: Xid"


class MetricsError(Exception):
    pass


def _gap(reason):
    return {"gap": reason}


def stats(values):
    """n, mean, p50, p95, p99 and max of a non-empty list of numbers, the percentiles by nearest rank"""
    if not values:
        raise MetricsError("no values to summarise")
    ordered = sorted(values)
    n = len(ordered)

    def pct(p):
        return ordered[math.ceil(p * n / 100) - 1]

    return {"n": n, "mean": sum(ordered) / n, "p50": pct(50), "p95": pct(95), "p99": pct(99), "max": ordered[-1]}


def _value(field, column):
    """A frame is a whole number; every other column is a finite number"""
    try:
        value = int(field) if column == "frame" else float(field)
    except ValueError:
        raise MetricsError(f"{column} is not a number: {field!r}") from None
    if not math.isfinite(value):
        raise MetricsError(f"{column} is not finite: {field!r}")
    return value


def _table(text, columns):
    """The rows of a CSV text whose header names exactly these columns in this order, one value per column each"""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise MetricsError("no header line")
    header = lines[0].split(",")
    if header != list(columns):
        missing = [c for c in columns if c not in header]
        extra = [c for c in header if c not in columns]
        detail = f"missing {missing}, extra {extra}" if missing or extra else "columns out of order"
        raise MetricsError(f"header refused ({detail})")
    rows = []
    for number, line in enumerate(lines[1:], start=2):
        fields = line.split(",")
        if len(fields) != len(columns):
            raise MetricsError(f"line {number} has {len(fields)} fields, want {len(columns)}")
        rows.append({column: _value(field, column) for column, field in zip(columns, fields)})
    return rows


def trace_rows(text):
    """The frame trace's rows, each a dict of all 23 columns by name; a header other than TRACE_COLUMNS is refused"""
    return _table(text, TRACE_COLUMNS)


def window(rows, first, last):
    """The rows whose frame is in [first, last]"""
    return [row for row in rows if first <= row["frame"] <= last]


def column_stats(rows):
    """Each trace column's stats over the rows, every column but frame"""
    if not rows:
        raise MetricsError("no rows to summarise")
    return {column: stats([row[column] for row in rows]) for column in TRACE_COLUMNS[1:]}


def frame_times(text):
    """The frame log's rows, each a dict of frame, time_s and frametime_ms"""
    return _table(text, FRAME_LOG_COLUMNS)


def _shape(pattern, line):
    found = pattern.search(line)
    if found is None:
        raise MetricsError(f"metric line does not have its shape: {line.strip()!r}")
    return found


def _replay(line):
    """A replay time line's parts by name, in the renderer's order: all 13 or the line is refused"""
    parts = REPLAY_PART.findall(line.split(REPLAY_LINE, 1)[1])
    if [name for name, _ in parts] != list(REPLAY_PARTS):
        raise MetricsError(f"replay time line does not have the {len(REPLAY_PARTS)} parts in order: {line.strip()!r}")
    return {name: float(ms) for name, ms in parts}


def log_metrics(text):
    """The renderer's log: each metric's samples in the order written (replay_ms: a dict of the 13 parts; gpu_ms and
    longest_submission_ms: floats; rss_mb and vram_mb: dicts) and hangs: the stall, hang and fatal lines as written. A
    line with a metric's key that does not have that metric's shape is refused."""
    found = {"replay_ms": [], "gpu_ms": [], "longest_submission_ms": [], "rss_mb": [], "vram_mb": [], "hangs": []}
    for line in text.splitlines():
        if any(key in line for key in HANG_LINES):
            found["hangs"].append(line.strip())
        if REPLAY_LINE in line:
            found["replay_ms"].append(_replay(line))
        elif GPU_LINE in line:
            match = _shape(GPU, line)
            found["gpu_ms"].append(float(match.group(1)))
            found["longest_submission_ms"].append(float(match.group(2)))
        elif RSS_LINE in line:
            found["rss_mb"].append(dict(zip(RSS_KEYS, map(int, _shape(RSS, line).groups()))))
        elif VRAM_LINE in line:
            found["vram_mb"].append(dict(zip(VRAM_KEYS, map(int, _shape(VRAM, line).groups()))))
    return found


def fallback(text):
    """The device fallback line, when the renderer fell back to auto selection; None when it did not"""
    for line in text.splitlines():
        if FALLBACK in line:
            return line.strip()
    return None


def kernel_hangs(text):
    """The kernel log's GPU ring timeouts (amdgpu) and NVIDIA Xids, as written"""
    return [line.strip() for line in text.splitlines() if RING_TIMEOUT.search(line) or XID in line]


def _section(name, output, parse):
    """parse(output), or a gap naming the output when it is missing or refused"""
    if output is None:
        return _gap(f"{name}: not written")
    try:
        return parse(output)
    except MetricsError as e:
        return _gap(f"{name}: {e}")


def _frame_stats(text):
    return stats([row["frametime_ms"] for row in frame_times(text)])


def _trace_section(text):
    rows = trace_rows(text)
    return {"rows": len(rows), "columns": column_stats(rows)}


def _by_key(name, rows, keys):
    """Each key's stats over the rows, or a gap when the log has no such line"""
    if not rows:
        return _gap(f"{name}: no line in the log")
    return {key: stats([row[key] for row in rows]) for key in keys}


def _flat(name, values):
    """The stats of a list of values, or a gap when the log has no such line"""
    if not values:
        return _gap(f"{name}: no line in the log")
    return stats(values)


def _log_sections(log):
    """The sections the renderer's log feeds, and its hang lines, each a gap when the log is missing or refused"""
    keys = LOG_SECTIONS + ("hangs",)
    if log is None:
        return {key: _gap("log: not written") for key in keys}
    try:
        found = log_metrics(log)
    except MetricsError as e:
        return {key: _gap(f"log: {e}") for key in keys}
    return {
        "replay_ms": _by_key("replay time", found["replay_ms"], REPLAY_PARTS),
        "gpu_ms": _flat("GPU time", found["gpu_ms"]),
        "longest_submission_ms": _flat("longest submission", found["longest_submission_ms"]),
        "rss_mb": _by_key("RSS", found["rss_mb"], RSS_KEYS),
        "vram_mb": _by_key("VRAM", found["vram_mb"], VRAM_KEYS),
        "fallback": fallback(log),
        "hangs": found["hangs"],
    }


def summary(log, trace, frame_log, kernel, exit_status):
    """A run's metrics as a JSON-ready dict. Each output is its text, or None when it was not written. A missing or
    refused output is a gap in every section it feeds, and a section the output has no value for is a gap too."""
    logged = _log_sections(log)
    return {
        "frame_time_ms": _section("frame log", frame_log, _frame_stats),
        "trace": _section("trace", trace, _trace_section),
        **{key: logged[key] for key in LOG_SECTIONS},
        "hangs": {
            "log": logged["hangs"],
            "exit_status": _gap("exit status: not recorded") if exit_status is None else exit_status,
            "kernel": _section("kernel log", kernel, kernel_hangs),
        },
    }
