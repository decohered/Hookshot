"""The bench gate: whether a bench is void, from its logs. A bench is void when a correctness log has a flash line, when
correctness log b has a validation error that log a lacks, or when any log (correctness or timed) has a hang line
(void_reasons). No verdict compares screenshots (DL185), so the gate reads no picture.

Stdlib only. The commands import this module as a sibling; tests/test_harness_gate.py checks it.
"""
import re

FLASH = re.compile(r"vk: (?:region )?flash at present ")
HANG = re.compile(r"vk: (?:the GPU has finished nothing for |GPU hang: |FATAL: )")
VALIDATION_ERROR = re.compile(r"Validation Error: \[ ([^\s\]]+) \]")


def flashes(log):
    """The lines of a log that report a flash on the screen"""
    return [line for line in log.splitlines() if FLASH.search(line)]


def hangs(log):
    """The lines of a log that report a GPU hang, or a fatal error in the renderer"""
    return [line for line in log.splitlines() if HANG.search(line)]


def validation_errors(log):
    """The message ids of the validation errors in a log; a validation warning is not one"""
    return set(VALIDATION_ERROR.findall(log))


def void_reasons(a_log, b_log, timed_logs):
    """Why a bench is void, one reason per cause, and none when it stands. The causes: a flash line in either correctness
    log; a validation error in b's log that a's log lacks; a hang line in any log, correctness or timed."""
    reasons = []
    correctness = [("correctness log a", a_log), ("correctness log b", b_log)]
    for label, log in correctness:
        found = flashes(log)
        if found:
            reasons.append(f"flash in {label}: {len(found)} line(s), first: {found[0]}")
    for error in sorted(validation_errors(b_log) - validation_errors(a_log)):
        reasons.append(f"validation error {error} in correctness log b, not in correctness log a")
    timed = [(f"timed log {n}", log) for n, log in enumerate(timed_logs, 1)]
    for label, log in correctness + timed:
        found = hangs(log)
        if found:
            reasons.append(f"hang in {label}: {len(found)} line(s), first: {found[0]}")
    return reasons
