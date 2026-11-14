"""The renderer census's draw-path sites (gpu/vk/vk_draw.cpp), read from the source without a game, a compiler or a GPU.

Each site is checked where the census needs it: the include; pass_end first in end_pass; a pass_cause directly before each
end_pass, naming its cause (the two calls that name none excepted); program and frame_pipeline directly after the
graphics_pipeline call in draw(); pipeline_made at the new entry in graphics_pipeline, with all seven pipeline-state groups
set; frame_view where each image view is placed; frame_set and the four push outcomes in bind_descriptors under the
graphics test; desc_reused in draw()'s reuse branch. Every census call in the file must be guarded by play_census::on alone,
by the rule tests/test_harness_census.py applies to gpu/vk/*.cpp, which this file imports rather than copies.

python -I -B tests/test_harness_census_draw.py
"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "gpu/vk/vk_draw.cpp"

END_PASS = "void end_pass(VkCommandBuffer cmd) {"
DRAW = "void draw(VkCommandBuffer cmd, NvnState& s, const DrawArgs& d) {"
GRAPHICS_PIPELINE = ("PipeEntry* graphics_pipeline(const Program& p, const NvnState& s, const Targets& at, "
                     "VkPrimitiveTopology topo, const uint8_t color_types[8]) {")
BUILD_DESCRIPTORS = "void build_descriptors(const NvnState& s, const Program& p) {"
BIND_DESCRIPTORS = "bool bind_descriptors(VkCommandBuffer cmd, VkPipelineBindPoint bp, const Program& p) {"

GUARD = re.compile(r"^\s*if \(play_census::on\) \{$")
GATE = "if (bp == VK_PIPELINE_BIND_POINT_GRAPHICS)"
CAUSE = re.compile(r"^\s*if \(play_census::on\) play_census::pass_cause\(play_census::(\w+)\);$")
# The cause each end_pass call in the file names, in the file's order. The calls at 7048 (EachGuard) and 7124
# (RX_VK_BARRIER_EVERY_DRAW) name none: they count as other.
CAUSES = ["kTargetChange", "kClear", "kCopy", "kHazard", "kCopy", "kCopy", "kCopy", "kCopy",
          "kCompute", "kCompute", None, None, "kCopy", "kUpscale", "kUpscale", "kCompute"]
PIPE_GROUPS = {"kTopology", "kMultisample", "kVertexInput", "kColorFormats", "kDepthFormat", "kBlend", "kRaster"}
PUSH_OUTCOMES = [
    ("kFull", "vkCmdPushDescriptorSetKHR(cmd, bp, p.layout, 0, (uint32_t)dw().writes.size(), dw().writes.data());"),
    ("kDelta", "vkCmdPushDescriptorSetKHR(cmd, bp, p.layout, 0, (uint32_t)changed.size(), changed.data());"),
    ("kNone", "FINE_N(FC_PUSH_NONE, 1);"),
    ("kPoolSet", "ps.cmd = VK_NULL_HANDLE;   // (a set bound: what was pushed is not known to stay)"),
]


class Missing(Exception):
    pass


def span(lines: list[str], signature: str) -> tuple[int, int]:
    """(first, last) line indexes of the function whose signature line is `signature`: last is its column-0 closing brace."""
    starts = [i for i, line in enumerate(lines) if line == signature]
    if len(starts) != 1:
        raise Missing("%r is defined %d times in %s" % (signature, len(starts), SRC.relative_to(ROOT)))
    first = starts[0]
    for last in range(first + 1, len(lines)):
        if lines[last] == "}":
            return first, last
    raise Missing("%r has no closing brace at column 0" % signature)


def where(lines: list[str], first: int, last: int, pattern: str) -> list[int]:
    """The indexes in [first, last) of the lines that match the regular expression pattern."""
    rx = re.compile(pattern)
    return [i for i in range(first, last) if rx.search(lines[i])]


def exact(lines: list[str], first: int, last: int, text: str) -> list[int]:
    """The indexes in [first, last) of the lines whose stripped text is `text`."""
    return [i for i in range(first, last) if lines[i].strip() == text]


def gate_of(lines: list[str], first: int, i: int) -> bool:
    """Whether line i sits in a block that a line ending `if (play_census::on) {` opens, and that block's first line is the
    graphics test. The block must still be open at line i."""
    for g in range(i - 1, first, -1):
        if GUARD.match(lines[g]):
            depth = 0
            for j in range(g, i):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth < 1:
                    return False
            return lines[g + 1].strip().startswith(GATE)
    return False


def check_include(lines: list[str], out: list[str]) -> None:
    if lines.count('#include "vk_census.h"') != 1:
        out.append('the include: #include "vk_census.h" must appear once, with the other includes')


def check_end_pass(lines: list[str], out: list[str]) -> None:
    first, last = span(lines, END_PASS)
    body = [line.strip() for line in lines[first + 1:last] if line.strip()]
    if not body or body[0] != "if (play_census::on) play_census::pass_end(pass().open);":
        out.append("end_pass: its first statement must be if (play_census::on) play_census::pass_end(pass().open);")


def check_pass_causes(lines: list[str], out: list[str]) -> None:
    calls = [i for i, line in enumerate(lines) if re.search(r"\bend_pass\(cmd\)", line)]
    if len(calls) != len(CAUSES):
        out.append("end_pass: %d calls in %s, want %d" % (len(calls), SRC.relative_to(ROOT), len(CAUSES)))
        return
    for i, want in zip(calls, CAUSES):
        before = lines[i - 1]
        found = CAUSE.match(before)
        if want is None:
            if found or "play_census::pass_cause" in before:
                out.append("end_pass at line %d names a pass cause; it must name none" % (i + 1))
        elif not found or found.group(1) != want:
            out.append("end_pass at line %d: want the line before it to be "
                       "if (play_census::on) play_census::pass_cause(play_census::%s);" % (i + 1, want))
    named = sum(1 for line in lines if "play_census::pass_cause(" in line)
    if named != sum(1 for want in CAUSES if want):
        out.append("pass_cause: %d calls, want %d" % (named, sum(1 for want in CAUSES if want)))


def check_draw(lines: list[str], out: list[str]) -> None:
    first, last = span(lines, DRAW)
    made = exact(lines, first, last, "pe = graphics_pipeline(p, s, at, topo, types);")
    if len(made) != 1:
        out.append("draw: want one pe = graphics_pipeline(p, s, at, topo, types); call, found %d" % len(made))
    else:
        i = made[0]
        if lines[i + 1].strip() != "if (play_census::on) play_census::program(p.key, p.variant);":
            out.append("draw: directly after the graphics_pipeline call, want play_census::program(p.key, p.variant)")
        if not re.fullmatch(r"\s*if \(play_census::on\) play_census::frame_pipeline\(.*\bpe\b.*\);", lines[i + 2]):
            out.append("draw: directly after play_census::program, want play_census::frame_pipeline(<pe as an integer>)")
    for name in ("program", "frame_pipeline", "desc_reused"):
        if len(where(lines, first, last, r"play_census::%s\(" % name)) != 1:
            out.append("draw: want exactly one play_census::%s( call" % name)
    reuse = exact(lines, first, last, "st_desc_reused++;")
    if len(reuse) != 1:
        out.append("draw: want one st_desc_reused++; (the reuse branch)")
    elif not re.fullmatch(r"\s*if \(play_census::on\) play_census::desc_reused\(\);", lines[reuse[0] + 1]):
        out.append("draw: directly after st_desc_reused++;, want if (play_census::on) play_census::desc_reused();")


def check_pipeline(lines: list[str], out: list[str]) -> None:
    first, last = span(lines, GRAPHICS_PIPELINE)
    emplace = where(lines, first, last, re.escape("pipelines.emplace(key, std::make_unique<PipeEntry>())"))
    kept = where(lines, first, last, re.escape("if (kept_ready_n.load(std::memory_order_acquire))"))
    made = where(lines, first, last, r"play_census::pipeline_made\(")
    if len(emplace) != 1 or len(kept) != 1 or len(made) != 1:
        out.append("graphics_pipeline: want one new entry, one kept-ready check and one play_census::pipeline_made call")
        return
    e, k, m = emplace[0], kept[0], made[0]
    if not e < m < k:
        out.append("graphics_pipeline: play_census::pipeline_made must sit at the new entry, between the emplace and the "
                   "kept check")
        return
    if lines[e + 1].strip() != "if (play_census::on) {":
        out.append("graphics_pipeline: directly after the new entry, want if (play_census::on) {")
    if lines[m].strip() != "play_census::pipeline_made(mix(p.key, p.variant), state);":
        out.append("graphics_pipeline: want play_census::pipeline_made(mix(p.key, p.variant), state)")
    if not any(lines[j].strip() == "uint64_t state[play_census::kPipeStateCount];" for j in range(e + 1, m)):
        out.append("graphics_pipeline: want uint64_t state[play_census::kPipeStateCount]; before play_census::pipeline_made")
    if not any(re.search(r"\(p, s, at, topo, state\);", lines[j]) for j in range(e + 1, m)):
        out.append("graphics_pipeline: the state array must be filled before play_census::pipeline_made")
    assigned: dict[str, int] = {}
    rx = re.compile(r"^\s*state\[play_census::(\w+)\] = (.+);$")
    for line in lines:
        found = rx.match(line)
        if found:
            group, value = found.group(1), found.group(2)
            assigned[group] = assigned.get(group, 0) + 1
            if "mix(" not in value:
                out.append("pipeline state %s: want a mix() of the fields pipeline_key bakes in" % group)
    for group in sorted(PIPE_GROUPS - set(assigned)):
        out.append("pipeline state: group play_census::%s is never set" % group)
    for group in sorted(set(assigned) - PIPE_GROUPS):
        out.append("pipeline state: play_census::%s is not a pipeline-state group" % group)
    for group, count in sorted(assigned.items()):
        if count != 1 and group in PIPE_GROUPS:
            out.append("pipeline state: play_census::%s is set %d times, want once" % (group, count))


def check_frame_view(lines: list[str], out: list[str]) -> None:
    first, last = span(lines, BUILD_DESCRIPTORS)
    placed = where(lines, first, last, r"dw\(\)\.imgs\.push_back\(\{.*\bview\b")
    calls = where(lines, first, last, r"play_census::frame_view\(")
    if len(placed) != 2 or len(calls) != 2:
        out.append("build_descriptors: want two image-view placements and two play_census::frame_view calls, found %d and %d"
                   % (len(placed), len(calls)))
        return
    for v in placed:
        if not re.fullmatch(r"\s*if \(play_census::on\) play_census::frame_view\(.*\bview\b.*\);", lines[v - 1]):
            out.append("build_descriptors line %d: want if (play_census::on) play_census::frame_view(<view>); directly "
                       "before the placement" % (v + 1))


def check_bind(lines: list[str], out: list[str]) -> None:
    first, last = span(lines, BIND_DESCRIPTORS)
    empty = exact(lines, first, last, "if (dw().writes.empty()) return true;")
    ps = exact(lines, first, last, "Pushed& ps = pushed()[bp == VK_PIPELINE_BIND_POINT_COMPUTE];")
    sets = where(lines, first, last, r"play_census::frame_set\(")
    if len(empty) != 1 or len(ps) != 1 or len(sets) != 1:
        out.append("bind_descriptors: want the empty-writes return, the pushed-state declaration and one "
                   "play_census::frame_set")
    else:
        f = sets[0]
        if not empty[0] < f < ps[0]:
            out.append("bind_descriptors: play_census::frame_set must sit after the empty-writes return and before the "
                       "pushed state")
        if not gate_of(lines, first, f):
            out.append("bind_descriptors: play_census::frame_set must be in a if (play_census::on) { block whose test is "
                       "the graphics bind point")
        elif not any(re.search(r"\bwrite_words\(", lines[j]) for j in range(first, f)):
            out.append("bind_descriptors: play_census::frame_set must hash the writes with write_words")
    for name, anchor in PUSH_OUTCOMES:
        at = exact(lines, first, last, anchor)
        pushes = where(lines, first, last, r"play_census::push\(play_census::%s," % name)
        if len(at) != 1 or len(pushes) != 1:
            out.append("bind_descriptors: want one outcome line for %s and one play_census::push(play_census::%s, ...)"
                       % (name, name))
            continue
        a, p = at[0], pushes[0]
        returns = [j for j in range(a + 1, last) if lines[j].strip().startswith("return")]
        if not returns or not a < p < returns[0]:
            out.append("bind_descriptors: play_census::push(play_census::%s, ...) must sit after its outcome and before the "
                       "return that follows it" % name)
        if not gate_of(lines, first, p):
            out.append("bind_descriptors: play_census::push(play_census::%s, ...) must be under the graphics bind-point test"
                       % name)


def check_source_rule(text: str, out: list[str]) -> None:
    path = ROOT / "tests" / "test_harness_census.py"
    spec = importlib.util.spec_from_file_location("test_harness_census", path)
    if spec is None or spec.loader is None:
        raise SystemExit("FAIL: cannot load the source rule from %s" % path.relative_to(ROOT))
    rule = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rule)
    for number in rule.unguarded_calls(text):
        out.append("census call outside if (play_census::on) at %s line %d" % (SRC.relative_to(ROOT), number))


def main():
    text = SRC.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    checks = [check_include, check_end_pass, check_pass_causes, check_draw, check_pipeline, check_frame_view, check_bind]
    for check in checks:
        try:
            check(lines, out)
        except Missing as err:
            out.append(str(err))
    check_source_rule(text, out)
    if out:
        raise SystemExit("FAIL: census draw sites in %s:\n%s" % (SRC.relative_to(ROOT), "\n".join("  " + m for m in out)))
    print("PASS: census draw sites present and guarded in %s" % SRC.relative_to(ROOT))


if __name__ == "__main__":
    main()
