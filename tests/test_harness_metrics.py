"""scripts/harness/metrics.py on short fixture texts written here with neutral values. The trace keeps its 23 columns
and is refused when its header is another (F4); column_stats gives the 22 columns but frame their stats; the frame log
gives the frame times; the renderer's log gives the replay time's 13 parts, the GPU time, the RSS and VRAM lines, the
device fallback line and the hang lines, and the kernel log gives the ring timeouts and Xids (R5). summary reports each
of them, names a missing or malformed output as a gap, and keeps the exit status. No dump, GPU, host or network is used.

python -I -B tests/test_harness_metrics.py
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
import metrics

HEADER = ("frame,ms,pipe_waits,pipe_wait_ms,left_out,translate_ms,uploads,upload_mb,textures,memsync_ms,pipelines_ms,"
          "uploads_ms,programs_ms,slot_wait_ms,gpu_behind_ms,draw_ms,desc_ms,cmds_ms,tex_lookup_ms,textures_gone,"
          "late_wait_ms,readback_wait_ms,tex_frame_ms")
COLUMNS = tuple(HEADER.split(","))
PARTS = ("submit", "present", "draw", "descriptors", "texture lookups", "uploads", "pipelines", "programs", "commands",
         "dispatch", "memory sync", "queue submit", "slot wait")
FRAME_HEADER = "frame,time_s,frametime_ms"
FRAME_LOG = "\n".join([FRAME_HEADER, "0,0.0000,16.000", "1,0.0167,17.000", "2,0.0333,18.000"]) + "\n"

REPLAY = ("[hos main] vk: a frame's replay time (ms): submit 1.10, present 0.20, draw 2.30, descriptors 0.40, "
          "texture lookups 0.50, uploads 0.60, pipelines 0.00, programs 0.00, commands 0.70, dispatch 0.80, "
          "memory sync 0.90, queue submit 1.00, slot wait 0.10")
REPLAY_SHORT = "[hos main] vk: a frame's replay time (ms): submit 1.10"
GPU = "[hos main] vk: GPU time 12.3 ms a frame (the longest submission 4.5 ms); 0.5 textures made a frame"
GPU_BAD = "[hos main] vk: GPU time soon ms a frame"
RSS = "[hos main] vk: memory: working set 1234 MB (peak 1500), private 900 MB, staging 64 MB in 4 arenas"
RSS_BAD = "[hos main] vk: memory: working set big MB"
VRAM = ("[hos main] vk: VRAM: 3000 MB used of 6000 MB allowed (1000 MB of it free in the blocks); textures 2000 MB "
        "(budget 2500 MB), 120 textures")
FALLBACK = "[hos main] vk: preferred device 1 not available or lacks Vulkan 1.3, falling back to auto selection"
WATCH = "[hos main] vk: GPU hang watch: work that finishes nothing for 30 s ends the game (RX_VK_GPU_HANG_SEC)"
STALL = "[hos main] vk: the GPU has finished nothing for 10 s: the timeline at 5, 7 sent (RX_VK_GPU_HANG_SEC=30)"
HANG = "[hos gpu hang watch] vk: GPU hang: the timeline at 5, 7 sent"
FATAL = "vk: FATAL: a fixture reason"
RING = "[  812.4] amdgpu 0000:03:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=1234, emitted seq=1236"
XID = "[  913.0] NVRM: Xid (PCI:0000:01:00): 31, pid=4321, Ch 00000008"
USB = "[  100.0] usb 1-1: new high-speed USB device number 2 using xhci_hcd"


def text_of(*lines):
    return "\n".join(lines) + "\n"


def row(frame, ms=1.0):
    """A trace row: the frame, ms in its column and 0.5 in each other one"""
    return [str(frame), str(ms)] + ["0.5"] * 21


def trace_text(*rows, header=HEADER):
    return "\n".join([header] + [",".join(fields) for fields in rows]) + "\n"


def outputs(**changes):
    """The five arguments of summary for a complete run, with any of them replaced"""
    found = {"log": text_of(REPLAY, GPU, RSS, VRAM), "trace": trace_text(row(0, 1.0), row(1, 3.0)),
             "frame_log": FRAME_LOG, "kernel": text_of(USB), "exit_status": 0}
    found.update(changes)
    return found


class TraceTest(unittest.TestCase):
    def test_the_trace_has_the_renderers_23_columns(self):
        self.assertEqual(tuple(metrics.TRACE_COLUMNS), COLUMNS)
        self.assertEqual(len(COLUMNS), 23)

    def test_each_row_keeps_every_column_by_name(self):
        rows = metrics.trace_rows(trace_text(row(0, 1.25), row(1, 2.5)))
        self.assertEqual([r["frame"] for r in rows], [0, 1])
        self.assertEqual(set(rows[0]), set(COLUMNS))
        self.assertEqual(rows[1]["ms"], 2.5)
        self.assertEqual(rows[1]["draw_ms"], 0.5)

    def test_a_header_missing_a_column_or_with_an_extra_one_is_refused(self):
        without = ",".join(c for c in COLUMNS if c != "tex_frame_ms")
        for case, header in (("missing", without), ("extra", HEADER + ",gpu_extra_ms")):
            with self.subTest(case=case):
                with self.assertRaisesRegex(metrics.MetricsError, "header"):
                    metrics.trace_rows(trace_text(row(0), header=header))

    def test_a_reordered_header_is_refused(self):
        swapped = HEADER.replace("frame,ms,pipe_waits", "frame,pipe_waits,ms")
        self.assertNotEqual(swapped, HEADER)
        with self.assertRaisesRegex(metrics.MetricsError, "header"):
            metrics.trace_rows(trace_text(row(0), header=swapped))

    def test_a_row_with_the_wrong_number_of_fields_is_refused(self):
        with self.assertRaisesRegex(metrics.MetricsError, "fields"):
            metrics.trace_rows(trace_text(row(0)[:-1]))

    def test_a_value_that_is_not_a_finite_number_is_refused(self):
        for field, value in (("frame", "1.5"), ("ms", "x"), ("ms", "nan"), ("draw_ms", "inf")):
            fields = row(0)
            fields[COLUMNS.index(field)] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(metrics.MetricsError):
                    metrics.trace_rows(trace_text(fields))

    def test_crlf_line_endings_read_like_lf(self):
        lf = trace_text(row(0, 1.0), row(1, 2.0))
        self.assertEqual(metrics.trace_rows(lf.replace("\n", "\r\n")), metrics.trace_rows(lf))

    def test_window_keeps_the_frames_from_first_to_last_inclusive(self):
        rows = metrics.trace_rows(trace_text(*[row(f) for f in range(6)]))
        self.assertEqual([r["frame"] for r in metrics.window(rows, 2, 4)], [2, 3, 4])

    def test_column_stats_cover_every_column_but_frame(self):
        rows = metrics.trace_rows(trace_text(*[row(f, ms) for f, ms in enumerate([1.0, 2.0, 3.0, 4.0, 100.0])]))
        stats = metrics.column_stats(rows)
        self.assertEqual(set(stats), set(COLUMNS[1:]))
        self.assertEqual(stats["ms"], {"n": 5, "mean": 22.0, "p50": 3.0, "p95": 100.0, "p99": 100.0, "max": 100.0})
        self.assertEqual(stats["draw_ms"]["max"], 0.5)

    def test_column_stats_of_no_rows_is_refused(self):
        with self.assertRaises(metrics.MetricsError):
            metrics.column_stats([])


class FrameLogTest(unittest.TestCase):
    def test_frame_times_keep_each_frame(self):
        rows = metrics.frame_times(FRAME_LOG)
        self.assertEqual([r["frametime_ms"] for r in rows], [16.0, 17.0, 18.0])
        self.assertEqual(rows[2], {"frame": 2, "time_s": 0.0333, "frametime_ms": 18.0})

    def test_a_frame_log_with_another_header_is_refused(self):
        with self.assertRaisesRegex(metrics.MetricsError, "header"):
            metrics.frame_times("frame,time_s\n0,0.0\n")


class LogTest(unittest.TestCase):
    def test_the_replay_line_gives_its_13_parts_by_name(self):
        values = (1.1, 0.2, 2.3, 0.4, 0.5, 0.6, 0.0, 0.0, 0.7, 0.8, 0.9, 1.0, 0.1)
        self.assertEqual(metrics.log_metrics(text_of(REPLAY))["replay_ms"], [dict(zip(PARTS, values))])

    def test_gpu_time_and_the_longest_submission(self):
        found = metrics.log_metrics(text_of(GPU))
        self.assertEqual(found["gpu_ms"], [12.3])
        self.assertEqual(found["longest_submission_ms"], [4.5])

    def test_rss_and_vram(self):
        found = metrics.log_metrics(text_of(RSS, VRAM))
        self.assertEqual(found["rss_mb"], [{"working_set_mb": 1234, "peak_mb": 1500, "private_mb": 900}])
        self.assertEqual(found["vram_mb"], [{"used_mb": 3000, "allowed_mb": 6000}])

    def test_the_hang_lines_are_reported_and_the_watch_start_is_not(self):
        self.assertEqual(metrics.log_metrics(text_of(WATCH, STALL, HANG, FATAL))["hangs"], [STALL, HANG, FATAL])

    def test_the_device_fallback_line_is_reported(self):
        self.assertEqual(metrics.fallback(text_of(REPLAY, FALLBACK)), FALLBACK)
        self.assertIsNone(metrics.fallback(text_of(REPLAY, GPU)))

    def test_a_metric_line_without_its_shape_is_refused(self):
        for line in (REPLAY_SHORT, GPU_BAD, RSS_BAD):
            with self.subTest(line=line[:40]):
                with self.assertRaises(metrics.MetricsError):
                    metrics.log_metrics(text_of(line))

    def test_other_log_lines_are_ignored(self):
        found = metrics.log_metrics(text_of("[hos main] vk: 4 presents, 59.9 fps", WATCH))
        self.assertEqual(found, {"replay_ms": [], "gpu_ms": [], "longest_submission_ms": [], "rss_mb": [],
                                 "vram_mb": [], "hangs": []})


class KernelTest(unittest.TestCase):
    def test_ring_timeouts_and_xids_are_reported(self):
        self.assertEqual(metrics.kernel_hangs(text_of(USB, RING, XID)), [RING, XID])

    def test_a_kernel_log_without_them_reports_none(self):
        self.assertEqual(metrics.kernel_hangs(text_of(USB)), [])


class SummaryTest(unittest.TestCase):
    def test_summary_gives_frame_time_trace_replay_gpu_rss_and_vram(self):
        s = metrics.summary(**outputs())
        self.assertEqual(s["frame_time_ms"]["n"], 3)
        self.assertEqual(s["frame_time_ms"]["p50"], 17.0)
        self.assertEqual(s["trace"]["rows"], 2)
        self.assertEqual(set(s["trace"]["columns"]), set(COLUMNS[1:]))
        self.assertEqual(s["trace"]["columns"]["ms"]["mean"], 2.0)
        self.assertEqual(set(s["replay_ms"]), set(PARTS))
        self.assertEqual(s["replay_ms"]["submit"]["max"], 1.1)
        self.assertEqual(s["gpu_ms"]["p50"], 12.3)
        self.assertEqual(s["longest_submission_ms"]["max"], 4.5)
        self.assertEqual(s["rss_mb"]["working_set_mb"]["max"], 1234)
        self.assertEqual(s["vram_mb"]["used_mb"]["mean"], 3000)
        self.assertIsNone(s["fallback"])
        self.assertEqual(s["hangs"], {"log": [], "exit_status": 0, "kernel": []})

    def test_the_summary_is_json_ready(self):
        s = metrics.summary(**outputs(log=text_of(REPLAY, GPU, RSS, VRAM, FALLBACK)))
        self.assertEqual(json.loads(json.dumps(s)), s)

    def test_the_device_fallback_line_reaches_the_summary(self):
        s = metrics.summary(**outputs(log=text_of(REPLAY, GPU, RSS, VRAM, FALLBACK)))
        self.assertEqual(s["fallback"], FALLBACK)

    def test_exit_status_3_the_fatal_exit_is_reported(self):
        s = metrics.summary(**outputs(log=text_of(REPLAY, FATAL), exit_status=3))
        self.assertEqual(s["hangs"]["exit_status"], 3)
        self.assertEqual(s["hangs"]["log"], [FATAL])

    def test_hang_lines_and_a_kernel_ring_timeout_are_reported(self):
        s = metrics.summary(**outputs(log=text_of(REPLAY, GPU, STALL, HANG), kernel=text_of(RING)))
        self.assertEqual(s["hangs"], {"log": [STALL, HANG], "exit_status": 0, "kernel": [RING]})

    def test_a_missing_output_is_a_named_gap(self):
        self.assertEqual(metrics.summary(None, None, None, None, None), {
            "frame_time_ms": {"gap": "frame log: not written"},
            "trace": {"gap": "trace: not written"},
            "replay_ms": {"gap": "log: not written"},
            "gpu_ms": {"gap": "log: not written"},
            "longest_submission_ms": {"gap": "log: not written"},
            "rss_mb": {"gap": "log: not written"},
            "vram_mb": {"gap": "log: not written"},
            "fallback": {"gap": "log: not written"},
            "hangs": {"log": {"gap": "log: not written"}, "exit_status": {"gap": "exit status: not recorded"},
                      "kernel": {"gap": "kernel log: not written"}},
        })

    def test_a_refused_trace_is_a_gap_and_the_other_sections_stay(self):
        s = metrics.summary(**outputs(trace=trace_text(row(0), header=HEADER + ",gpu_extra_ms")))
        self.assertEqual(s["trace"], {"gap": "trace: header refused (missing [], extra ['gpu_extra_ms'])"})
        self.assertEqual(s["gpu_ms"]["n"], 1)
        self.assertEqual(s["frame_time_ms"]["n"], 3)

    def test_a_malformed_log_line_gaps_every_log_section(self):
        s = metrics.summary(**outputs(log=text_of(REPLAY_SHORT, GPU)))
        self.assertTrue(s["replay_ms"]["gap"].startswith("log: replay time line"))
        for key in ("gpu_ms", "longest_submission_ms", "rss_mb", "vram_mb", "fallback"):
            self.assertEqual(s[key], s["replay_ms"])
        self.assertEqual(s["hangs"]["log"], s["replay_ms"])
        self.assertEqual(s["frame_time_ms"]["n"], 3)

    def test_a_log_without_one_metric_gaps_only_that_metric(self):
        s = metrics.summary(**outputs(log=text_of(GPU, RSS, VRAM)))
        self.assertEqual(s["replay_ms"], {"gap": "replay time: no line in the log"})
        self.assertEqual(s["gpu_ms"]["n"], 1)
        self.assertIsNone(s["fallback"])

    def test_a_frame_log_or_a_trace_without_rows_is_a_gap(self):
        s = metrics.summary(**outputs(frame_log=FRAME_HEADER + "\n", trace=trace_text()))
        self.assertEqual(s["frame_time_ms"], {"gap": "frame log: no values to summarise"})
        self.assertEqual(s["trace"], {"gap": "trace: no rows to summarise"})


if __name__ == "__main__":
    unittest.main()
