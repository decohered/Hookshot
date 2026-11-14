"""The harness's verdict on timed runs: the A-B-B-A plan of timed runs and correctness runs, the warm-up cut of each
run's frames, a seeded bootstrap of the change between two arms' run medians, the noise floor measured from a baseline
against a baseline, and a verdict that calls B slower, faster or no change against that floor. series runs a plan in
order and calls wait() before each run, so a hold can pause it: every run runs exactly once and nothing is retried. The
method's constants come from the [method] table of a suite file (load_method). No margin in ms, fps or percent decides
a verdict: the floor is measured from the same kind of runs.

Stdlib only. The commands import this module as a sibling; tests/test_harness_verdict.py checks it.
"""
import dataclasses
import math
import random
import statistics
import tomllib
from collections.abc import Callable

ARM_CYCLE = "ABBA"
METHOD_KEYS = ("confidence", "runs_per_arm", "warmup_seconds", "resamples", "seed")
MIN_RUNS_PER_ARM = 5


class MethodError(Exception):
    """A method that cannot be read or has a value out of range, or a plan that cannot be made from it"""


class VerdictError(Exception):
    """A verdict that cannot be computed: an arm with too few runs, a run the warm-up leaves empty, a bad floor"""


@dataclasses.dataclass(frozen=True)
class Method:
    confidence: float
    runs_per_arm: int
    warmup_seconds: float
    resamples: int
    seed: int


def load_method(data: bytes, where: str) -> Method:
    """The method in a suite file's bytes: its [method] table, with the other tables ignored. where names the file."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise MethodError(f"{where}: not UTF-8 text: {e}") from None
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise MethodError(f"{where}: not valid TOML: {e}") from None
    table = doc.get("method")
    if not isinstance(table, dict):
        raise MethodError(f"{where}: no [method] table")
    for key in table:
        if key not in METHOD_KEYS:
            raise MethodError(f"{where}: unknown key method.{key}")
    for key in METHOD_KEYS:
        if key not in table:
            raise MethodError(f"{where}: missing key method.{key}")
    confidence = _number(where, table, "confidence")
    if not 0 < confidence < 1:
        raise MethodError(f"{where}: method.confidence must be above 0 and below 1: {confidence!r}")
    runs_per_arm = _integer(where, table, "runs_per_arm")
    if runs_per_arm < MIN_RUNS_PER_ARM:
        raise MethodError(f"{where}: method.runs_per_arm must be at least {MIN_RUNS_PER_ARM}: {runs_per_arm!r}")
    warmup_seconds = _number(where, table, "warmup_seconds")
    if not warmup_seconds >= 0:
        raise MethodError(f"{where}: method.warmup_seconds must be at least 0: {warmup_seconds!r}")
    resamples = _integer(where, table, "resamples")
    if resamples < 1:
        raise MethodError(f"{where}: method.resamples must be at least 1: {resamples!r}")
    seed = _integer(where, table, "seed")
    return Method(confidence=float(confidence), runs_per_arm=runs_per_arm, warmup_seconds=float(warmup_seconds),
                  resamples=resamples, seed=seed)


def _number(where: str, table: dict[str, object], key: str) -> int | float:
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MethodError(f"{where}: method.{key} must be a number: {value!r}")
    return value


def _integer(where: str, table: dict[str, object], key: str) -> int:
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise MethodError(f"{where}: method.{key} must be an integer: {value!r}")
    return value


def plan(runs_per_arm: int) -> list[tuple[str, str]]:
    """The timed runs first: 2 x runs_per_arm of them, the cycle A, B, B, A cut at that length, so each arm gets
    runs_per_arm. Then one correctness run for each arm."""
    if isinstance(runs_per_arm, bool) or not isinstance(runs_per_arm, int) or runs_per_arm < MIN_RUNS_PER_ARM:
        raise MethodError(f"runs_per_arm must be an integer of at least {MIN_RUNS_PER_ARM}: {runs_per_arm!r}")
    timed = [(ARM_CYCLE[i % len(ARM_CYCLE)], "timed") for i in range(2 * runs_per_arm)]
    return timed + [("A", "correctness"), ("B", "correctness")]


def warm(frame_ms: list[float], warmup_seconds: float) -> list[float]:
    """The frames that are kept after the warm-up: frame i is kept when the frame times before it sum to at least
    warmup_seconds x 1000 ms."""
    start = warmup_seconds * 1000
    elapsed = 0.0
    kept = []
    for ms in frame_ms:
        if elapsed >= start:
            kept.append(ms)
        elapsed += ms
    if not kept:
        raise VerdictError(f"a warm-up of {warmup_seconds} s leaves no frames of the {len(frame_ms)} in a run")
    return kept


def bootstrap(a: list[list[float]], b: list[list[float]], method: Method) -> tuple[float, float]:
    """The interval, at method.confidence, of the change from arm A to arm B: positive means B's frames take longer"""
    return _interval(_medians(a, method, "A"), _medians(b, method, "B"), method)


def noise_floor(a: list[list[float]], b: list[list[float]], method: Method) -> float:
    """The floor for a baseline against a baseline: the larger bound of their interval, in magnitude"""
    low, high = bootstrap(a, b, method)
    return max(abs(low), abs(high))


def verdict(a: list[list[float]], b: list[list[float]], floor: float, method: Method) -> dict[str, object]:
    """Calls B slower when the interval lies above the floor, faster when it lies below minus the floor,
    else no change"""
    if not floor >= 0:
        raise VerdictError(f"the noise floor must be a number of at least 0: {floor!r}")
    a_medians = _medians(a, method, "A")
    b_medians = _medians(b, method, "B")
    low, high = _interval(a_medians, b_medians, method)
    if low > floor:
        call = "slower"
    elif high < -floor:
        call = "faster"
    else:
        call = "no change"
    return {"verdict": call, "low": low, "high": high, "floor": floor, "a_medians": a_medians,
            "b_medians": b_medians, "method": dataclasses.asdict(method)}


def series(order: list[tuple[str, str]], run: Callable[[int, str, str], object],
           wait: Callable[[], float]) -> list[dict[str, object]]:
    """Each position of order in turn: wait() first, then run(i, arm, kind) once. wait returns the seconds it held, so a
    hold shows as the entry after the gap. An exception from either propagates; nothing runs twice."""
    entries = []
    for index, (arm, kind) in enumerate(order):
        held = wait()
        result = run(index, arm, kind)
        entries.append({"index": index, "arm": arm, "kind": kind, "held_s": held, "result": result})
    return entries


def _medians(runs: list[list[float]], method: Method, arm: str) -> list[float]:
    """Each run's median warm frame time in ms, for an arm with at least method.runs_per_arm runs"""
    if len(runs) < method.runs_per_arm:
        raise VerdictError(f"arm {arm} has {len(runs)} runs; the method needs {method.runs_per_arm} per arm")
    medians = [statistics.median(warm(run, method.warmup_seconds)) for run in runs]
    if any(m <= 0 for m in medians):
        raise VerdictError(f"arm {arm} has a run whose median frame time is not positive")
    return medians


def _interval(a_medians: list[float], b_medians: list[float], method: Method) -> tuple[float, float]:
    """The equal-tailed percentile interval of median(B) / median(A) - 1 over method.resamples resamples, each arm's run
    medians drawn with replacement within the arm, all from one random.Random(method.seed)"""
    rng = random.Random(method.seed)
    changes = []
    for _ in range(method.resamples):
        a_draw = [rng.choice(a_medians) for _ in a_medians]
        b_draw = [rng.choice(b_medians) for _ in b_medians]
        changes.append(statistics.median(b_draw) / statistics.median(a_draw) - 1)
    changes.sort()
    tail = (1 - method.confidence) / 2
    return _percentile(changes, tail), _percentile(changes, 1 - tail)


def _percentile(ordered: list[float], fraction: float) -> float:
    """The value fraction of the way through an ascending list, interpolated between the two values either side"""
    position = fraction * (len(ordered) - 1)
    below = math.floor(position)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (position - below)
