"""The renderer census's call sites in gpu/vk/vk_replay.cpp (the census is gpu/vk/vk_census.h), read from the source.

No game, no GPU, no compiler: the file is read as text and each site is checked where the census places it:
  - the include, with the other includes;
  - replay()'s loop: one `if (play_census::on) {` directly after `avail` and directly before the op switch, classifying each
    op to its call with `dry` as one if/else chain on h.op (no other text, and no census call outside the chain);
  - each of the six `s.bind_gen++` is directly followed, in the same block, by `if (play_census::on) play_census::slot_changed(dry);`,
    and there is no other slot_changed call;
  - each of the ten end_pass calls is directly preceded by `if (play_census::on) play_census::pass_cause(play_census::<cause>);` for the
    cause its site ends a pass for; each transfer call that ends a pass (copy_b2t twice, copy_t2t, clear_tex) is preceded by
    its cause inside its `if (!dry)` braces;
  - begin_recording notes its value beside memory_recording_value; invalidate calls readback at once on its writeback value;
  - log_frame_stats calls present once per present (outside its every-120th block), with barrier_stats read first; finish
    calls flush right after its waits; both pass the same emit function, which is a plain function in this file that logs
    its line with logf("%s", line) and is defined before finish;
  - every census call in the file is under `if (play_census::on)`: the source rule of tests/test_harness_census.py.

python -I -B tests/test_harness_census_replay.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "gpu" / "vk" / "vk_replay.cpp"
sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves this folder off the path)
from srccut import CutError, block   # noqa: E402
from test_harness_census import code, unguarded_calls   # noqa: E402

AVAIL = "const uint32_t avail = h.size - 8;"
BLOCK_OPEN = "if (play_census::on) {"
# one link of the op classification: `if (h.op == NOP_A || h.op == NOP_B) play_census::<call>;`, or `else if` after the first
CLASS_LINK = re.compile(r"(else\s+)?if \((h\.op == NOP_\w+(?:\s*\|\|\s*h\.op == NOP_\w+)*)\)\s*(play_census::\w+\([^;]*\));")
DRAW = ["NOP_DRAW", "NOP_DRAW_IDX", "NOP_DRAW_INDIRECT"]
# the op classification of replay()'s census block: each call, and the ops it is made for
CLASSIFY = [
    ("play_census::recording_begin(dry)", ["NOP_BEGIN"]),
    ("play_census::draw(false, dry)", DRAW),
    ("play_census::draw(true, dry)", ["NOP_DRAW_MESH"]),
    ("play_census::nvn_barrier(dry)", ["NOP_BARRIER"]),
    ("play_census::uniform_update(dry)", ["NOP_UPDATE_UB"]),
    ("play_census::state_set(play_census::kProgram, dry)", ["NOP_BIND_PROGRAM"]),
    ("play_census::state_set(play_census::kTargets, dry)", ["NOP_SET_RT"]),
    ("play_census::state_set(play_census::kRenderState, dry)", ["NOP_BIND_STATE", "NOP_STENCIL", "NOP_POLY_OFFSET",
                                                       "NOP_BLEND_COLOR", "NOP_PRIMITIVE_RESTART", "NOP_DEPTH_CLAMP",
                                                       "NOP_RASTER_DISCARD"]),
    ("play_census::state_set(play_census::kVertex, dry)", ["NOP_BIND_VATTR", "NOP_BIND_VSTREAM", "NOP_BIND_VBUF"]),
    ("play_census::state_set(play_census::kViewport, dry)", ["NOP_VIEWPORTS", "NOP_SCISSOR", "NOP_DEPTH_RANGE"]),
    ("play_census::state_set(play_census::kBindings, dry)", ["NOP_BIND_BUFS", "NOP_BIND_UBO", "NOP_BIND_SSBO", "NOP_BIND_TEX",
                                                    "NOP_BIND_SAMP", "NOP_BIND_IMG", "NOP_SET_TEX_POOL",
                                                    "NOP_SET_SAMP_POOL", "NOP_PUSH_CONST"]),
]
EXPECTED = {op: call for call, ops in CLASSIFY for op in ops}

SLOT_CHANGED = "if (play_census::on) play_census::slot_changed(dry);"
BUMPS = 6
PASS_CAUSE = "if (play_census::on) play_census::pass_cause(play_census::{});"
# the cause of each end_pass call in the file, in order: event, copy, clear, barrier, split (three), recording end (two), submit
END_PASS_CAUSES = ["kEvent", "kCopy", "kClear", "kBarrier", "kSplit", "kSplit", "kSplit", "kRecordingEnd",
                   "kRecordingEnd", "kSubmit"]
# the transfer calls that end a pass, in order, with the cause each one is made under
TRANSFERS = [("copy_b2t", "kCopy"), ("copy_b2t", "kCopy"), ("copy_t2t", "kCopy"), ("clear_tex", "kClear")]
RECORDING_VALUE = "if (play_census::on) play_census::recording_value(value);"
READBACK = "if (play_census::on) play_census::readback(v);"
EMIT_LINE = 'logf("%s", line);'


def code_text(text: str) -> str:
    """The source with comments cut and string literals emptied, one line at a time (the source rule's reading)"""
    return "\n".join(code(line) for line in text.splitlines())


def line_of(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def cut(text: str, marker: str, problems: list[str]) -> str | None:
    """The block from marker to the brace that closes it, or None with a problem when it is missing or repeated"""
    try:
        return block(text, marker, "gpu/vk/vk_replay.cpp")
    except CutError as err:
        problems.append(str(err))
        return None


def check_include(text: str, problems: list[str]) -> None:
    include = re.search(r'^#include "vk_census\.h"[ \t]*$', text, re.M)
    if not include:
        problems.append('no #include "vk_census.h" among the includes')
    elif include.start() > text.index("namespace mkwvk {"):
        problems.append('#include "vk_census.h" comes after namespace mkwvk {, not with the other includes')


def check_replay_loop(text: str, problems: list[str]) -> None:
    if text.count(AVAIL) != 1:
        problems.append("%r is not in replay() exactly once" % AVAIL)
        return
    after = text.index(AVAIL) + len(AVAIL)
    start = text.find(BLOCK_OPEN, after)
    if start < 0 or text[after:start].strip():
        problems.append("replay(): no `if (play_census::on) {` directly after the avail line (the op classification)")
        return
    try:
        blk = block(text[start:], BLOCK_OPEN, "gpu/vk/vk_replay.cpp", unique=False)
    except CutError as err:
        problems.append(str(err))
        return
    switch = text.find("switch (h.op) {", start + len(blk))
    if switch < 0 or text[start + len(blk):switch].strip():
        problems.append("replay(): the census block is not directly before the op switch")
        return
    blk_code = code_text(blk)
    inner = blk_code[blk_code.index("{") + 1:blk_code.rindex("}")]
    links = list(CLASS_LINK.finditer(inner))
    if len(re.findall(r"play_census::(?!on\b)\w+\(", inner)) != len(links):
        problems.append("replay(): a census call in the op classification is not under an if on h.op")
    pos = 0
    for n, link in enumerate(links):
        if inner[pos:link.start()].strip() or bool(link.group(1)) != (n > 0):
            problems.append("replay(): the op classification is not one if/else chain, at: %s" % " ".join(link.group(0).split())[:80])
        pos = link.end()
    if inner[pos:].strip():
        problems.append("replay(): the op classification has text after its chain: %s" % " ".join(inner[pos:].split())[:80])
    got = {}
    for link in links:
        for op in re.findall(r"NOP_\w+", link.group(2)):
            if op in got:
                problems.append("replay(): %s is classified twice in the census block" % op)
            got[op] = " ".join(link.group(3).split())
    for op in sorted(set(EXPECTED) | set(got)):
        want, have = EXPECTED.get(op), got.get(op)
        if want != have:
            problems.append("replay(): %s is classified to %s, want %s" % (op, have or "no call", want or "no call"))


def check_bind_gen(codetext: str, problems: list[str]) -> None:
    bumps = list(re.finditer(r"s\.bind_gen\+\+", codetext))
    if len(bumps) != BUMPS:
        problems.append("expected %d `s.bind_gen++` statements in the source, found %d" % (BUMPS, len(bumps)))
    for bump in bumps:
        where = line_of(codetext, bump.start())
        # (the statement's own prefix: a case label is no condition, and an if or else before it is one)
        prefix = re.split(r"[;{}]", codetext[:bump.start()])[-1]
        if re.sub(r"\bcase\s+[\w:]+\s*:|\bdefault\s*:", "", prefix).strip():
            problems.append("line %d: s.bind_gen++ is the unbraced body of `%s`, so a slot_changed after it would not share "
                            "its condition" % (where, prefix.strip()))
            continue
        end = codetext.index(";", bump.start()) + 1
        if not re.match(r"\s*" + re.escape(SLOT_CHANGED), codetext[end:]):
            problems.append("line %d: s.bind_gen++ is not directly followed by `%s`" % (where, SLOT_CHANGED))
    slots = len(re.findall(r"play_census::slot_changed\(", codetext))
    if slots != len(bumps):
        problems.append("expected %d play_census::slot_changed calls, one per s.bind_gen++, found %d" % (len(bumps), slots))


def check_pass_causes(codetext: str, problems: list[str]) -> None:
    ends = [m.start() for m in re.finditer(r"\bend_pass\(", codetext)]
    if len(ends) != len(END_PASS_CAUSES):
        problems.append("expected %d end_pass calls in the source, found %d" % (len(END_PASS_CAUSES), len(ends)))
    for at, cause in zip(ends, END_PASS_CAUSES):
        stmt = PASS_CAUSE.format(cause)
        if not re.search(re.escape(stmt) + r"\s*\Z", codetext[:at]):
            problems.append("line %d: end_pass is not directly preceded by `%s`" % (line_of(codetext, at), stmt))
    calls = [(m.start(), m.group(1)) for m in re.finditer(r"\b(copy_b2t|copy_t2t|clear_tex)\(", codetext)]
    if [name for _, name in calls] != [name for name, _ in TRANSFERS]:
        problems.append("expected transfer calls %s in the source, found %s"
                        % ([name for name, _ in TRANSFERS], [name for _, name in calls]))
    for (at, name), (_, cause) in zip(calls, TRANSFERS):
        stmt = PASS_CAUSE.format(cause)
        pattern = r"if \(!dry\) \{\s*(?:ParLock lk;\s*)?" + re.escape(stmt) + r"\s*\Z"
        if not re.search(pattern, codetext[:at]):
            problems.append("line %d: %s is not directly preceded by `%s` in its `if (!dry)` braces"
                            % (line_of(codetext, at), name, stmt))
    pauses = len(re.findall(r"play_census::pass_cause\(", codetext))
    if pauses != len(ends) + len(calls):
        problems.append("expected %d play_census::pass_cause calls, one per end_pass and transfer, found %d"
                        % (len(ends) + len(calls), pauses))


def check_begin_recording(text: str, codetext: str, problems: list[str]) -> None:
    body = cut(text, "Recording& begin_recording(uint64_t value) {", problems)
    if body is not None:
        beside = re.compile(r"memory_recording_value\(value, completed\(\)\);\s*" + re.escape(RECORDING_VALUE) +
                            r"|" + re.escape(RECORDING_VALUE) + r"\s*memory_recording_value\(value, completed\(\)\);")
        if not beside.search(code_text(body)):
            problems.append("begin_recording: `%s` is not beside memory_recording_value(value, completed());" % RECORDING_VALUE)
    if codetext.count("play_census::recording_value(") != 1:
        problems.append("expected one play_census::recording_value call, in begin_recording")


def check_readback(text: str, codetext: str, problems: list[str]) -> None:
    body = cut(text, "void invalidate(uint64_t gpu, uint64_t size) {", problems)
    if body is not None:
        direct = re.compile(r"const uint64_t v = memory_writeback_value\(host->gpu_guest\(gpu\), size, nullptr, &part, "
                            r"&whole\);\s*" + re.escape(READBACK))
        if not direct.search(code_text(body)):
            problems.append("invalidate: `%s` does not directly follow the writeback value `v`" % READBACK)
    if codetext.count("play_census::readback(") != 1:
        problems.append("expected one play_census::readback call, in invalidate")


def check_frame_stats_and_flush(text: str, codetext: str, problems: list[str]) -> None:
    names = set()
    stats = cut(text, "void log_frame_stats() {", problems)
    if stats is not None:
        if stats.find(BLOCK_OPEN) < 0 or stats.find(BLOCK_OPEN) > stats.find("if (++fps_n == 120)"):
            problems.append("log_frame_stats: its `if (play_census::on) {` is not once per present, before the every-120th block")
        else:
            try:
                guard = code_text(block(stats, BLOCK_OPEN, "gpu/vk/vk_replay.cpp", unique=False))
            except CutError as err:
                problems.append(str(err))
                guard = ""
            found = re.search(r"barrier_stats\(&made, &left\);\s*play_census::present\(made, left, (\w+)\);", guard)
            if found:
                names.add(found.group(1))
            else:
                problems.append("log_frame_stats: its census block does not read barrier_stats(&made, &left) and call "
                                "play_census::present(made, left, <emit>)")
    if codetext.count("play_census::present(") != 1:
        problems.append("expected one play_census::present call, in log_frame_stats")
    fin = cut(text, "void finish() {", problems)
    if fin is not None:
        found = re.search(r"wait_value\(pushed\.load\(\)\);\s*if \(play_census::on\) play_census::flush\((\w+)\);", code_text(fin))
        if found:
            names.add(found.group(1))
        else:
            problems.append("finish: no `if (play_census::on) play_census::flush(<emit>);` directly after its waits")
    if codetext.count("play_census::flush(") != 1:
        problems.append("expected one play_census::flush call, in finish")
    if len(names) > 1:
        problems.append("log_frame_stats and finish pass different emit functions: %s" % ", ".join(sorted(names)))
    for name in names:
        emit_head = "void %s(const char* line) {" % name
        emit = cut(text, emit_head, problems)
        if emit is not None and EMIT_LINE not in emit:
            problems.append("%s does not log its line with `%s`" % (name, EMIT_LINE))
        if text.find(emit_head) > text.find("void finish() {"):
            problems.append("%s is defined after finish, which passes it to flush" % name)


def check_source_rule(text: str, problems: list[str]) -> None:
    for number in unguarded_calls(text):
        problems.append("line %d: census call outside `if (play_census::on)`" % number)


def main() -> None:
    text = SOURCE.read_text(encoding="utf-8")
    codetext = code_text(text)
    problems = []
    check_include(text, problems)
    check_replay_loop(text, problems)
    check_bind_gen(codetext, problems)
    check_pass_causes(codetext, problems)
    check_begin_recording(text, codetext, problems)
    check_readback(text, codetext, problems)
    check_frame_stats_and_flush(text, codetext, problems)
    check_source_rule(text, problems)
    if problems:
        raise SystemExit("FAIL: census call sites in gpu/vk/vk_replay.cpp:\n" + "\n".join("  " + p for p in problems))
    print("PASS: census call sites in gpu/vk/vk_replay.cpp: include, op classification, slot changes, pass causes, "
          "readbacks, recording values, frame stats, flush and the source rule")


if __name__ == "__main__":
    main()
