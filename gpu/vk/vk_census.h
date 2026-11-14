// SPDX-License-Identifier: GPL-3.0-or-later
// Renderer census: counts and shares of what the renderer records, written to the play log as fixed-key lines.
// The switch is RX_VK_CENSUS. Each call site is an `if (play_census::on)` test of one constant, so with the switch off
// nothing is counted or written. The lines hold counts only: no program, pipeline, view, descriptor, submit value or
// address, in decimal or hex. Functions taking `dry` do nothing when it is set (the replay's dry pass).
#pragma once

#include "compat/hookshot_env.h"
#include <array>
#include <cstdint>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <vector>

namespace mkwvk::play_census {

inline const bool on = env_on("RX_VK_CENSUS");

enum Group { kProgram, kTargets, kRenderState, kVertex, kViewport, kBindings, kGroupCount };
enum PipeState { kTopology, kMultisample, kVertexInput, kColorFormats, kDepthFormat, kBlend, kRaster, kPipeStateCount };
enum Push { kFull, kDelta, kNone, kPoolSet };
enum Cause { kTargetChange, kClear, kCopy, kHazard, kCompute, kUpscale, kEvent, kBarrier, kSplit, kRecordingEnd, kSubmit, kOther, kCauseCount };

namespace detail {

// Every key of a window in the order it is written. A group, pipeline state, cause or slot bucket maps to its key by
// adding its enum value, so each run of keys below is in the same order as its enum.
enum Key {
    kPresents, kDraws, kMeshDraws, kNvnBarriers, kSlotChanges,
    kDrawsSlots0, kDrawsSlots1, kDrawsSlots2, kDrawsSlots3_4, kDrawsSlots5_8, kDrawsSlots9Up,
    kUniformOnlyDraws, kRecordings, kRecordingsWithDraw,
    kInheritNone, kInheritProgram, kInheritTargets, kInheritRenderState, kInheritVertex, kInheritViewport, kInheritBindings,
    kFramePipelines, kFrameStageSets, kFrameSets, kFrameViews,
    kFramePipelinesMax, kFrameStageSetsMax, kFrameSetsMax, kFrameViewsMax,
    kPipelinesMade,
    kVariantTopology, kVariantMultisample, kVariantVertex, kVariantColorFormats, kVariantDepthFormat, kVariantBlend, kVariantRaster,
    kNewPrograms, kNewVariants,
    kProgramsVariants1, kProgramsVariants2, kProgramsVariants3, kProgramsVariants4_7, kProgramsVariants8_15, kProgramsVariants16Up,
    kPushesFull, kPushesDelta, kPushesNone, kPoolSets, kWritesPushed, kWritesFull, kDescReusedDraws,
    kReadbacks, kReadbacksAge0, kReadbacksAge1, kReadbacksAge2, kReadbacksAge3Up, kReadbacksAgeUnknown,
    kPassBreaksTargetChange, kPassBreaksClear, kPassBreaksCopy, kPassBreaksHazard, kPassBreaksCompute, kPassBreaksUpscale,
    kPassBreaksEvent, kPassBreaksBarrier, kPassBreaksSplit, kPassBreaksRecordingEnd, kPassBreaksSubmit, kPassBreaksOther,
    kFullBarriersRecorded, kFullBarriersLeftOut,
    kKeyCount
};

inline constexpr const char* kKeyNames[kKeyCount] = {
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
};

constexpr int kPairsPerLine = 12;
constexpr int64_t kPresentsPerWindow = 30;
constexpr unsigned kAllGroups = (1u << kGroupCount) - 1;

// Global state, under mu. The summed and _max keys in n restart at each emission; the programs_variants keys do not.
struct Accumulator {
    int64_t n[kKeyCount] = {};
    int64_t presents_total = 0;
    int64_t windows = 0;
    int64_t barriers_recorded = 0;
    int64_t barriers_left_out = 0;
    std::set<uint64_t> pipelines, stage_sets, sets, views;   // this frame's distinct ids
    std::map<uint64_t, std::set<uint64_t>> variants;          // per program key, its distinct variants
    std::map<uint64_t, std::array<uint64_t, kPipeStateCount>> first_state;   // per program key, its first pipeline's state
    std::map<uint64_t, int64_t> noted;                        // submit value -> presents when its recording began
};
inline std::mutex mu;
inline Accumulator acc;

// Per thread: the open recording, the slots and uniform updates since this thread's last draw, the pending pass cause.
struct Recording {
    bool open = false;
    bool drawn = false;
    unsigned set = 0;
};
inline thread_local Recording rec;
inline thread_local int64_t pending_slots = 0;
inline thread_local int64_t pending_uniforms = 0;
inline thread_local Cause pending_cause = kOther;

// Callers hold mu.
inline void add(int k, int64_t v = 1) { acc.n[k] += v; }
inline void note_max(int k, int64_t v) {
    if (v > acc.n[k]) acc.n[k] = v;
}
inline bool cumulative(int k) { return k >= kProgramsVariants1 && k <= kProgramsVariants16Up; }

inline void close_set(std::set<uint64_t>& ids, Key sum, Key max) {
    const int64_t size = static_cast<int64_t>(ids.size());
    add(sum, size);
    note_max(max, size);
    ids.clear();
}

inline void close_frame() {
    close_set(acc.pipelines, kFramePipelines, kFramePipelinesMax);
    close_set(acc.stage_sets, kFrameStageSets, kFrameStageSetsMax);
    close_set(acc.sets, kFrameSets, kFrameSetsMax);
    close_set(acc.views, kFrameViews, kFrameViewsMax);
}

inline int variant_bucket(size_t n) {
    if (n <= 1) return 0;
    if (n == 2) return 1;
    if (n == 3) return 2;
    if (n <= 7) return 3;
    if (n <= 15) return 4;
    return 5;
}

// The cumulative histogram of distinct variants per program key, from the sets (which only grow).
inline void fold_variants() {
    for (int k = kProgramsVariants1; k <= kProgramsVariants16Up; k++) acc.n[k] = 0;
    for (const auto& entry : acc.variants) add(kProgramsVariants1 + variant_bucket(entry.second.size()));
}

inline Key slots_bucket(int64_t slots) {
    if (slots == 0) return kDrawsSlots0;
    if (slots == 1) return kDrawsSlots1;
    if (slots == 2) return kDrawsSlots2;
    if (slots <= 4) return kDrawsSlots3_4;
    if (slots <= 8) return kDrawsSlots5_8;
    return kDrawsSlots9Up;
}

// Writes one window as lines of kPairsPerLine keys, with the window index on each. The window is taken under mu, and
// emit is called after mu is released. only_if_counted: nothing is taken when every summed count is zero.
inline void emit_window(void (*emit)(const char*), bool only_if_counted) {
    std::array<int64_t, kKeyCount> values{};
    int64_t index = 0;
    {
        std::lock_guard<std::mutex> lock(mu);
        fold_variants();
        if (only_if_counted) {
            bool counted = false;
            for (int k = 0; k < kKeyCount; k++) counted = counted || (!cumulative(k) && acc.n[k] != 0);
            if (!counted) return;
        }
        index = ++acc.windows;
        for (int k = 0; k < kKeyCount; k++) {
            values[k] = acc.n[k];
            if (!cumulative(k)) acc.n[k] = 0;
        }
    }
    for (int first = 0; first < kKeyCount; first += kPairsPerLine) {
        const int last = first + kPairsPerLine < kKeyCount ? first + kPairsPerLine : kKeyCount;
        std::string line = "vk: census: window=" + std::to_string(index);
        for (int k = first; k < last; k++) line += std::string(" ") + kKeyNames[k] + "=" + std::to_string(values[k]);
        emit(line.c_str());
    }
}

}  // namespace detail

// A recording begins on this thread; the first draw of it is the one that inherits state.
inline void recording_begin(bool dry) {
    if (dry) return;
    detail::rec = detail::Recording{true, false, 0};
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::add(detail::kRecordings);
}

inline void state_set(Group g, bool dry) {
    if (!dry && detail::rec.open) detail::rec.set |= 1u << g;
}

inline void slot_changed(bool dry) {
    if (!dry) detail::pending_slots++;
}

inline void uniform_update(bool dry) {
    if (!dry) detail::pending_uniforms++;
}

inline void draw(bool mesh, bool dry) {
    if (dry) return;
    const int64_t slots = detail::pending_slots;
    const bool uniform_only = slots == 0 && detail::pending_uniforms > 0;
    detail::pending_slots = 0;
    detail::pending_uniforms = 0;
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::add(detail::kDraws);
    if (mesh) detail::add(detail::kMeshDraws);
    detail::add(detail::kSlotChanges, slots);
    detail::add(detail::slots_bucket(slots));
    if (uniform_only) detail::add(detail::kUniformOnlyDraws);
    detail::Recording& r = detail::rec;
    if (r.open && !r.drawn) {
        r.drawn = true;
        detail::add(detail::kRecordingsWithDraw);
        if (r.set == detail::kAllGroups) detail::add(detail::kInheritNone);
        for (int g = 0; g < kGroupCount; g++)
            if (!(r.set & (1u << g))) detail::add(detail::kInheritProgram + g);
    }
}

inline void nvn_barrier(bool dry) {
    if (dry) return;
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::add(detail::kNvnBarriers);
}

inline void program(uint64_t key, uint64_t variant) {
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::acc.stage_sets.insert(key);
    const bool known = detail::acc.variants.count(key) != 0;
    auto& seen = detail::acc.variants[key];
    if (!known) detail::add(detail::kNewPrograms);
    if (seen.insert(variant).second) detail::add(detail::kNewVariants);
}

inline void frame_pipeline(uint64_t id) {
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::acc.pipelines.insert(id);
}

inline void frame_set(uint64_t contents) {
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::acc.sets.insert(contents);
}

inline void frame_view(uint64_t view) {
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::acc.views.insert(view);
}

inline void pipeline_made(uint64_t program_key, const uint64_t (&state)[kPipeStateCount]) {
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::add(detail::kPipelinesMade);
    const auto first = detail::acc.first_state.find(program_key);
    if (first == detail::acc.first_state.end()) {
        std::array<uint64_t, kPipeStateCount> copy{};
        for (int s = 0; s < kPipeStateCount; s++) copy[s] = state[s];
        detail::acc.first_state.emplace(program_key, copy);
        return;
    }
    for (int s = 0; s < kPipeStateCount; s++)
        if (state[s] != first->second[s]) detail::add(detail::kVariantTopology + s);
}

inline void push(Push kind, uint32_t writes, uint32_t full_writes) {
    std::lock_guard<std::mutex> lock(detail::mu);
    switch (kind) {
    case kFull:
        detail::add(detail::kPushesFull);
        detail::add(detail::kWritesPushed, writes);
        break;
    case kDelta:
        detail::add(detail::kPushesDelta);
        detail::add(detail::kWritesPushed, writes);
        break;
    case kNone:
        detail::add(detail::kPushesNone);
        break;
    case kPoolSet:
        detail::add(detail::kPoolSets);
        detail::add(detail::kWritesPushed, writes);
        break;
    }
    detail::add(detail::kWritesFull, full_writes);
}

inline void desc_reused() {
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::add(detail::kDescReusedDraws);
}

inline void recording_value(uint64_t value) {
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::acc.noted[value] = detail::acc.presents_total;
}

inline void readback(uint64_t producer_value) {
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::add(detail::kReadbacks);
    const auto noted = detail::acc.noted.find(producer_value);
    if (noted == detail::acc.noted.end()) {
        detail::add(detail::kReadbacksAgeUnknown);
        return;
    }
    const int64_t age = detail::acc.presents_total - noted->second;
    if (age == 0) detail::add(detail::kReadbacksAge0);
    else if (age == 1) detail::add(detail::kReadbacksAge1);
    else if (age == 2) detail::add(detail::kReadbacksAge2);
    else detail::add(detail::kReadbacksAge3Up);
}

inline void pass_cause(Cause c) { detail::pending_cause = c; }

inline void pass_end(bool was_open) {
    const Cause cause = detail::pending_cause;
    detail::pending_cause = kOther;
    if (!was_open) return;
    std::lock_guard<std::mutex> lock(detail::mu);
    detail::add(detail::kPassBreaksTargetChange + static_cast<int>(cause));
}

// Closes the frame. Every kPresentsPerWindow-th present of the play emits the window.
inline void present(int64_t barriers_recorded_total, int64_t barriers_left_out_total, void (*emit)(const char*)) {
    bool due = false;
    {
        std::lock_guard<std::mutex> lock(detail::mu);
        detail::Accumulator& a = detail::acc;
        detail::add(detail::kPresents);
        detail::add(detail::kFullBarriersRecorded, barriers_recorded_total - a.barriers_recorded);
        detail::add(detail::kFullBarriersLeftOut, barriers_left_out_total - a.barriers_left_out);
        a.barriers_recorded = barriers_recorded_total;
        a.barriers_left_out = barriers_left_out_total;
        detail::close_frame();
        a.presents_total++;
        due = a.presents_total % detail::kPresentsPerWindow == 0;
    }
    if (due) detail::emit_window(emit, false);
}

// Emits the window now when any summed count is nonzero since the last emission. Closes no frame.
inline void flush(void (*emit)(const char*)) { detail::emit_window(emit, true); }

}  // namespace mkwvk::play_census
