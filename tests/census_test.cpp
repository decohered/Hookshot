// SPDX-License-Identifier: GPL-3.0-or-later
// The renderer census driven by stub calls, with no GPU or game: every key's value in each emitted window, the line
// grammar and key order, and that no fed identifier, value or variant reaches the output. Prints play_census::on (the switch
// as the environment set it) for tests/test_harness_census.py to compare.
#include "vk_census.h"
#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <thread>
#include <utility>
#include <vector>

// The renderer's program census (gpu/vk/vk_programs.h) is a variable mkwvk::census in every file that hooks this one:
// declared here too, so a census namespace of that name would not compile.
namespace mkwvk {
struct ProgramCensus;
extern ProgramCensus census;
}  // namespace mkwvk
namespace c = mkwvk::play_census;
static int failures, checks;
static void check(bool ok, const char* what) {
    ++checks;
    if (!ok) ++failures, std::printf("FAIL: %s\n", what);
}

// The play-log keys in the order a window writes them.
static const char* const kKeys[] = {
    "presents", "draws", "mesh_draws", "nvn_barriers", "slot_changes", "draws_slots_0", "draws_slots_1",
    "draws_slots_2", "draws_slots_3_4", "draws_slots_5_8", "draws_slots_9_up", "uniform_only_draws", "recordings",
    "recordings_with_draw", "first_draw_inherits_none", "first_draw_inherits_program", "first_draw_inherits_targets",
    "first_draw_inherits_render_state", "first_draw_inherits_vertex", "first_draw_inherits_viewport",
    "first_draw_inherits_bindings", "frame_pipelines", "frame_stage_sets", "frame_sets", "frame_views",
    "frame_pipelines_max", "frame_stage_sets_max", "frame_sets_max", "frame_views_max", "pipelines_made",
    "variant_state_topology", "variant_state_multisample", "variant_state_vertex", "variant_state_color_formats",
    "variant_state_depth_format", "variant_state_blend", "variant_state_raster", "new_programs", "new_variants",
    "programs_variants_1", "programs_variants_2", "programs_variants_3", "programs_variants_4_7",
    "programs_variants_8_15", "programs_variants_16_up", "pushes_full", "pushes_delta", "pushes_none", "pool_sets",
    "writes_pushed", "writes_full", "desc_reused_draws", "readbacks", "readbacks_age_0", "readbacks_age_1",
    "readbacks_age_2", "readbacks_age_3_up", "readbacks_age_unknown", "pass_breaks_target_change", "pass_breaks_clear",
    "pass_breaks_copy", "pass_breaks_hazard", "pass_breaks_compute", "pass_breaks_upscale", "pass_breaks_event",
    "pass_breaks_barrier", "pass_breaks_split", "pass_breaks_recording_end", "pass_breaks_submit", "pass_breaks_other",
    "full_barriers_recorded", "full_barriers_left_out",
};
static_assert(sizeof kKeys / sizeof kKeys[0] == 72, "the play log has 72 keys");
constexpr size_t kKeyCount = sizeof kKeys / sizeof kKeys[0];

// Fed identifiers: large and distinctive, so no count can equal one and a substring match means it leaked.
static uint64_t program_key(uint64_t n) { return 0x7A11C0DE0000ull + n; }
static uint64_t variant_id(uint64_t n) { return 0x5A0000000000ull + n; }
static uint64_t pipeline_id(uint64_t n) { return 0x1D0000000000ull + n; }
static uint64_t set_id(uint64_t n) { return 0x5E0000000000ull + n; }
static uint64_t view_id(uint64_t n) { return 0x710000000000ull + n; }
static uint64_t submit_value(uint64_t n) { return 0x9E0000000000ull + n; }
static uint64_t pipe_state(uint64_t n) { return 0x57A700000000ull + n; }

static std::vector<std::string> lines;
static void capture(const char* line) { lines.push_back(line); }

struct Window {
    int64_t index = 0;
    std::vector<std::pair<std::string, int64_t>> pairs;
};

static bool is_digit(char ch) { return ch >= '0' && ch <= '9'; }
static bool is_key_char(char ch) { return (ch >= 'a' && ch <= 'z') || is_digit(ch) || ch == '_'; }

// One line: "vk: census: window=<n>", then " <key>=<decimal>" pairs. False when it does not match that grammar.
static bool parse_line(const std::string& line, int64_t& index, std::vector<std::pair<std::string, int64_t>>& pairs) {
    static const std::string prefix = "vk: census: window=";
    if (line.compare(0, prefix.size(), prefix) != 0) return false;
    size_t at = prefix.size(), start = at;
    while (at < line.size() && is_digit(line[at])) at++;
    if (at == start) return false;
    index = std::stoll(line.substr(start, at - start));
    while (at < line.size()) {
        if (line[at] != ' ') return false;
        start = ++at;
        while (at < line.size() && is_key_char(line[at])) at++;
        if (at == start || at == line.size() || line[at] != '=') return false;
        const std::string key = line.substr(start, at - start);
        start = ++at;
        while (at < line.size() && is_digit(line[at])) at++;
        if (at == start) return false;
        pairs.emplace_back(key, std::stoll(line.substr(start, at - start)));
    }
    return !pairs.empty();
}

// The emitted lines grouped into windows. Each line is checked for grammar, length and pair count; window indices count
// up from 1.
static std::vector<Window> collect() {
    std::vector<Window> out;
    for (const std::string& line : lines) {
        Window w;
        if (!parse_line(line, w.index, w.pairs)) {
            std::printf("  line: %s\n", line.c_str());
            check(false, "every line matches the grammar");
            continue;
        }
        check(line.size() <= 1000, "every line is at most 1000 bytes");
        check(w.pairs.size() <= 12, "a line carries at most 12 pairs");
        if (out.empty() || out.back().index != w.index) {
            check(w.index == (out.empty() ? 1 : out.back().index + 1), "window indices count up from 1 by one");
            out.push_back(Window{w.index, {}});
        }
        out.back().pairs.insert(out.back().pairs.end(), w.pairs.begin(), w.pairs.end());
    }
    return out;
}

static int64_t value(const Window& w, const char* key) {
    for (const auto& p : w.pairs)
        if (p.first == key) return p.second;
    return -1;
}

// Every key of the window, with the value the scenario expects; a key not listed is 0.
static void expect(const Window& w, const std::map<std::string, int64_t>& want) {
    for (const auto& entry : want) {
        bool known = false;
        for (const char* key : kKeys) known = known || entry.first == key;
        check(known, "expected values name only keys of the play log");
    }
    for (const char* key : kKeys) {
        const auto found = want.find(key);
        const int64_t exp = found == want.end() ? 0 : found->second;
        const int64_t got = value(w, key);
        ++checks;
        if (got != exp) {
            ++failures;
            std::printf("FAIL: window %lld %s=%lld, want %lld\n", (long long)w.index, key, (long long)got, (long long)exp);
        }
    }
}

int main() {
    std::printf("play_census::on=%d\n", c::on ? 1 : 0);
    const uint64_t kA = program_key(1), kB = program_key(2), kC = program_key(3), kD = program_key(4), kE = program_key(5),
                   kF = program_key(6);

    // Dry calls count nothing, open no recording and leave no pending state; flush emits nothing counted.
    c::recording_begin(true);
    c::state_set(c::kProgram, true);
    c::slot_changed(true);
    c::uniform_update(true);
    c::draw(true, true);
    c::nvn_barrier(true);
    c::flush(capture);
    check(lines.empty(), "dry calls count nothing and flush emits nothing");

    // Window 1 (flushed, before any present): draws, recordings and first-draw inheritance.
    c::recording_begin(false);                                 // R1
    for (auto g : {c::kProgram, c::kTargets, c::kVertex, c::kViewport, c::kBindings}) c::state_set(g, false);
    c::state_set(c::kRenderState, true);                       // dry: render state stays inherited
    c::slot_changed(false);
    c::slot_changed(false);
    c::draw(false, false);                                     // D1: 2 slots; inherits render state
    c::uniform_update(false);
    c::draw(true, false);                                      // D2: uniform only, mesh
    c::uniform_update(true);                                   // dry: not pending
    c::draw(false, false);                                     // D3: nothing pending, not uniform only
    c::recording_begin(false);                                 // R2: no group set
    for (int i = 0; i < 9; i++) c::slot_changed(false);
    c::draw(false, false);                                     // D4: 9 slots; inherits every group
    for (int i = 0; i < 4; i++) c::slot_changed(false);
    c::draw(true, false);                                      // D5: 4 slots, mesh
    for (int i = 0; i < 6; i++) c::slot_changed(false);
    c::draw(false, false);                                     // D6: 6 slots
    c::slot_changed(false);
    c::draw(false, false);                                     // D7: 1 slot
    for (int i = 0; i < 3; i++) c::nvn_barrier(false);
    for (int i = 0; i < 5; i++) c::slot_changed(true);         // dry: not pending
    c::draw(false, false);                                     // D8: nothing pending
    for (int i = 0; i < 3; i++) c::slot_changed(false);
    std::thread([] {                                           // T: its own 7 slots, no recording
        for (int i = 0; i < 7; i++) c::slot_changed(false);
        c::draw(true, false);
    }).join();
    c::draw(false, false);                                     // D9: the main thread's 3 slots

    c::pass_cause(c::kClear);
    c::pass_end(true);                                         // clear
    c::pass_end(true);                                         // other
    c::pass_cause(c::kCopy);
    c::pass_end(false);                                        // a closed pass is not counted; its cause is spent
    c::pass_end(true);                                         // other, not copy
    c::pass_cause(c::kHazard);
    c::pass_cause(c::kBarrier);
    c::pass_end(true);                                         // the last cause set wins: barrier
    for (int i = 0; i < 2; i++) {
        c::pass_cause(c::kCopy);
        c::pass_end(true);                                     // copy, twice
    }
    c::pass_cause(c::kHazard);
    c::pass_end(true);                                         // hazard

    c::push(c::kFull, 4, 6);
    c::push(c::kDelta, 2, 3);
    c::push(c::kNone, 9, 5);                                   // writes_pushed does not take a none push
    c::push(c::kPoolSet, 7, 0);
    c::desc_reused();
    c::desc_reused();

    c::recording_value(submit_value(1));
    c::readback(submit_value(1));                              // age 0
    c::readback(submit_value(50));                             // never noted

    c::program(kA, variant_id(1));
    c::program(kA, variant_id(1));
    c::program(kA, variant_id(2));
    c::program(kB, variant_id(7));
    for (uint64_t v = 1; v <= 3; v++) c::program(kF, variant_id(v));   // F: exactly three variants

    const uint64_t a0[c::kPipeStateCount] = {pipe_state(1), pipe_state(2), pipe_state(3), pipe_state(4), pipe_state(5),
                                             pipe_state(6), pipe_state(7)};
    const uint64_t a1[c::kPipeStateCount] = {pipe_state(11), pipe_state(2), pipe_state(3), pipe_state(4), pipe_state(5),
                                             pipe_state(6), pipe_state(18)};
    const uint64_t b0[c::kPipeStateCount] = {pipe_state(21), pipe_state(21), pipe_state(21), pipe_state(21),
                                             pipe_state(21), pipe_state(21), pipe_state(21)};
    const uint64_t b1[c::kPipeStateCount] = {pipe_state(21), pipe_state(22), pipe_state(21), pipe_state(23),
                                             pipe_state(21), pipe_state(24), pipe_state(21)};
    c::pipeline_made(kA, a0);                                  // first of A: stores its state
    c::pipeline_made(kA, a1);                                  // topology and raster differ
    c::pipeline_made(kB, b0);
    c::pipeline_made(kB, b0);                                  // identical: no difference
    c::pipeline_made(kB, b1);                                  // multisample, color formats and blend differ

    c::frame_pipeline(pipeline_id(1));
    c::frame_pipeline(pipeline_id(2));
    c::frame_pipeline(pipeline_id(1));                         // distinct within the frame
    c::frame_set(set_id(1));
    c::frame_view(view_id(1));
    c::frame_view(view_id(2));
    c::frame_view(view_id(3));
    c::flush(capture);
    check(lines.size() == 6, "flush emits window 1 when counts are nonzero");

    // Window 2: presents 1 to 30. The 30th present emits it.
    c::present(5, 2, capture);                                 // present 1: barrier growth from zero
    c::frame_pipeline(pipeline_id(1));
    c::frame_pipeline(pipeline_id(1));
    c::frame_pipeline(pipeline_id(1));
    c::frame_set(set_id(1));
    c::frame_view(view_id(1));
    c::program(kA, variant_id(1));
    c::present(8, 2, capture);                                 // present 2: frame of one pipeline, one set, one view
    c::recording_value(submit_value(2));                       // noted at 2 presents
    c::present(8, 2, capture);                                 // present 3
    c::readback(submit_value(2));                              // age 1
    c::recording_value(submit_value(3));                       // noted at 3
    c::present(8, 2, capture);                                 // present 4
    c::present(8, 2, capture);                                 // present 5
    c::readback(submit_value(3));                              // age 2
    c::recording_value(submit_value(4));                       // noted at 5
    for (int i = 0; i < 4; i++) c::present(8, 2, capture);     // presents 6 to 9
    c::readback(submit_value(4));                              // age 4
    c::readback(submit_value(2));                              // age 7
    c::readback(submit_value(99));                             // never noted

    c::pass_cause(c::kTargetChange);
    c::pass_end(true);
    c::pass_cause(c::kCompute);
    c::pass_end(true);
    c::pass_cause(c::kUpscale);
    c::pass_end(true);
    c::pass_cause(c::kEvent);
    c::pass_end(true);
    c::pass_cause(c::kSplit);
    c::pass_end(true);
    c::pass_cause(c::kRecordingEnd);
    c::pass_end(true);
    c::pass_cause(c::kSubmit);
    c::pass_end(true);

    c::recording_begin(false);                                 // R3: every group set
    for (auto g : {c::kProgram, c::kTargets, c::kRenderState, c::kVertex, c::kViewport, c::kBindings}) c::state_set(g, false);
    c::draw(false, false);                                     // first draw: inherits none
    for (int i = 0; i < 3; i++) c::slot_changed(false);
    std::thread([] { c::draw(false, false); }).join();         // a draw only: no recording on that thread
    c::draw(true, false);                                      // the main thread's 3 slots
    c::uniform_update(false);
    c::draw(false, false);                                     // uniform only
    c::nvn_barrier(false);
    c::nvn_barrier(false);

    c::program(kC, variant_id(1));                             // a new program
    c::program(kA, variant_id(3));                             // A's variants: 1, 2, 3 ...
    for (uint64_t v = 4; v <= 7; v++) c::program(kA, variant_id(v));   // ... to 7

    const uint64_t c0[c::kPipeStateCount] = {pipe_state(31), pipe_state(31), pipe_state(31), pipe_state(31),
                                             pipe_state(31), pipe_state(31), pipe_state(31)};
    const uint64_t a2[c::kPipeStateCount] = {pipe_state(1), pipe_state(2), pipe_state(8), pipe_state(4), pipe_state(9),
                                             pipe_state(6), pipe_state(7)};
    c::pipeline_made(kC, c0);
    c::pipeline_made(kA, a0);                                  // same as A's first
    c::pipeline_made(kA, a2);                                  // vertex and depth differ

    c::frame_pipeline(pipeline_id(4));
    c::frame_pipeline(pipeline_id(5));
    c::frame_set(set_id(2));
    c::frame_set(set_id(3));
    c::frame_view(view_id(9));
    c::present(8, 2, capture);                                 // present 10: stage sets {C, A}
    for (int i = 0; i < 20; i++) c::present(8, 2, capture);    // presents 11 to 30: window 2 emitted at 30
    check(lines.size() == 12, "the 30th present emits window 2");

    // Window 3: presents 31 to 60.
    for (uint64_t v = 1; v <= 8; v++) c::program(kD, variant_id(v));
    for (uint64_t v = 1; v <= 16; v++) c::program(kE, variant_id(v));
    c::frame_pipeline(pipeline_id(6));
    c::frame_view(view_id(4));
    c::frame_view(view_id(5));
    c::draw(false, false);                                     // a draw in a recording that has drawn
    c::present(10, 5, capture);                                // present 31: barrier growth 2 and 3
    for (int i = 0; i < 28; i++) c::present(10, 5, capture);   // presents 32 to 59
    c::present(10, 5, capture);                                // present 60: window 3 emitted
    check(lines.size() == 18, "the 60th present emits window 3");
    c::flush(capture);
    check(lines.size() == 18, "flush emits nothing when every summed count is zero");

    // Window 4 (flushed): a view pending in the frame is not closed by flush.
    c::frame_view(view_id(9));
    c::nvn_barrier(false);
    c::flush(capture);
    check(lines.size() == 24, "flush emits window 4 for a counted barrier");

    // Window 5: present 61 closes the frame with that view; presents 61 to 90.
    c::present(10, 5, capture);
    for (int i = 0; i < 29; i++) c::present(10, 5, capture);
    check(lines.size() == 30, "the 90th present emits window 5");

    // Every line, every window.
    const std::vector<Window> windows = collect();
    check(windows.size() == 5, "five windows are emitted");
    for (const Window& w : windows) {
        std::vector<std::string> keys;
        for (const auto& p : w.pairs) keys.push_back(p.first);
        check(keys == std::vector<std::string>(kKeys, kKeys + kKeyCount), "every window carries every key once, in order");
    }
    if (windows.size() == 5) {
        expect(windows[0], {{"draws", 10}, {"mesh_draws", 3}, {"nvn_barriers", 3}, {"slot_changes", 32},
                            {"draws_slots_0", 3}, {"draws_slots_1", 1}, {"draws_slots_2", 1}, {"draws_slots_3_4", 2},
                            {"draws_slots_5_8", 2}, {"draws_slots_9_up", 1}, {"uniform_only_draws", 1},
                            {"recordings", 2}, {"recordings_with_draw", 2}, {"first_draw_inherits_program", 1},
                            {"first_draw_inherits_targets", 1}, {"first_draw_inherits_render_state", 2},
                            {"first_draw_inherits_vertex", 1}, {"first_draw_inherits_viewport", 1},
                            {"first_draw_inherits_bindings", 1}, {"pipelines_made", 5}, {"variant_state_topology", 1},
                            {"variant_state_multisample", 1}, {"variant_state_color_formats", 1},
                            {"variant_state_blend", 1}, {"variant_state_raster", 1}, {"new_programs", 3},
                            {"new_variants", 6}, {"programs_variants_1", 1}, {"programs_variants_2", 1},
                            {"programs_variants_3", 1}, {"pass_breaks_copy", 2}, {"pass_breaks_hazard", 1},
                            {"pushes_full", 1}, {"pushes_delta", 1}, {"pushes_none", 1}, {"pool_sets", 1},
                            {"writes_pushed", 13}, {"writes_full", 14}, {"desc_reused_draws", 2}, {"readbacks", 2},
                            {"readbacks_age_0", 1}, {"readbacks_age_unknown", 1}, {"pass_breaks_clear", 1},
                            {"pass_breaks_other", 2}, {"pass_breaks_barrier", 1}});
        expect(windows[1], {{"presents", 30}, {"draws", 4}, {"mesh_draws", 1}, {"nvn_barriers", 2},
                            {"slot_changes", 3}, {"draws_slots_0", 3}, {"draws_slots_3_4", 1},
                            {"uniform_only_draws", 1}, {"recordings", 1}, {"recordings_with_draw", 1},
                            {"first_draw_inherits_none", 1}, {"frame_pipelines", 5}, {"frame_stage_sets", 6},
                            {"frame_sets", 4}, {"frame_views", 5}, {"frame_pipelines_max", 2},
                            {"frame_stage_sets_max", 3}, {"frame_sets_max", 2}, {"frame_views_max", 3},
                            {"pipelines_made", 3}, {"variant_state_vertex", 1}, {"variant_state_depth_format", 1},
                            {"new_programs", 1}, {"new_variants", 6}, {"programs_variants_1", 2},
                            {"programs_variants_3", 1}, {"programs_variants_4_7", 1}, {"readbacks", 5},
                            {"readbacks_age_1", 1},
                            {"readbacks_age_2", 1}, {"readbacks_age_3_up", 2}, {"readbacks_age_unknown", 1},
                            {"pass_breaks_target_change", 1}, {"pass_breaks_compute", 1}, {"pass_breaks_upscale", 1},
                            {"pass_breaks_event", 1}, {"pass_breaks_split", 1}, {"pass_breaks_recording_end", 1},
                            {"pass_breaks_submit", 1}, {"full_barriers_recorded", 8}, {"full_barriers_left_out", 2}});
        expect(windows[2], {{"presents", 30}, {"draws", 1}, {"draws_slots_0", 1}, {"frame_pipelines", 1},
                            {"frame_stage_sets", 2}, {"frame_views", 2}, {"frame_pipelines_max", 1},
                            {"frame_stage_sets_max", 2}, {"frame_views_max", 2}, {"new_programs", 2},
                            {"new_variants", 24}, {"programs_variants_1", 2}, {"programs_variants_3", 1},
                            {"programs_variants_4_7", 1},
                            {"programs_variants_8_15", 1}, {"programs_variants_16_up", 1},
                            {"full_barriers_recorded", 2}, {"full_barriers_left_out", 3}});
        expect(windows[3], {{"nvn_barriers", 1}, {"programs_variants_1", 2}, {"programs_variants_3", 1},
                            {"programs_variants_4_7", 1},
                            {"programs_variants_8_15", 1}, {"programs_variants_16_up", 1}});
        expect(windows[4], {{"presents", 30}, {"frame_views", 1}, {"frame_views_max", 1},
                            {"programs_variants_1", 2}, {"programs_variants_3", 1}, {"programs_variants_4_7", 1},
                            {"programs_variants_8_15", 1}, {"programs_variants_16_up", 1}});
    }

    // No fed identifier, value or variant appears in the output, in decimal or hex.
    std::string text;
    for (const std::string& line : lines) text += line + "\n";
    std::vector<uint64_t> fed;
    for (uint64_t n = 1; n <= 6; n++) fed.push_back(program_key(n));
    for (uint64_t n = 1; n <= 16; n++) fed.push_back(variant_id(n));
    for (uint64_t n = 1; n <= 6; n++) fed.push_back(pipeline_id(n));
    for (uint64_t n = 1; n <= 3; n++) fed.push_back(set_id(n));
    for (uint64_t n = 1; n <= 9; n++) fed.push_back(view_id(n));
    for (uint64_t n : {1, 2, 3, 4, 50, 99}) fed.push_back(submit_value(n));
    for (uint64_t n = 1; n <= 31; n++) fed.push_back(pipe_state(n));
    for (uint64_t v : fed) {
        char dec[32], hex[32], upper[32];
        std::snprintf(dec, sizeof dec, "%llu", (unsigned long long)v);
        std::snprintf(hex, sizeof hex, "%llx", (unsigned long long)v);
        std::snprintf(upper, sizeof upper, "%llX", (unsigned long long)v);
        ++checks;
        if (text.find(dec) != std::string::npos || text.find(hex) != std::string::npos ||
            text.find(upper) != std::string::npos) {
            ++failures;
            std::printf("FAIL: fed identifier %s reaches the output\n", hex);
        }
    }

    std::printf("census: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
