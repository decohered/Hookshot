"""scripts/harness/verdict.py, the harness's verdict on timed runs, against synthetic frame times only: the A-B-B-A plan
and its refusal below five runs per arm, the warm-up cut, load_method's accepted table and each refusal, the seeded
bootstrap interval, a noise floor measured on separate A-against-A data, a planted shift read as slower and its mirror
as faster, and a hold that pauses a series mid-way: the entry after it carries the held seconds, every run runs once,
and the verdict from its results equals the verdict without the hold. No game, GPU, host or network is used.

python -I -B tests/test_harness_verdict.py
"""
import dataclasses
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
import verdict

WHERE = "suite.toml"
METHOD = verdict.Method(confidence=0.95, runs_per_arm=5, warmup_seconds=1.0, resamples=2000, seed=20261113)
GOOD_METHOD = {
    "confidence": "0.95",
    "runs_per_arm": "5",
    "warmup_seconds": "1.0",
    "resamples": "2000",
    "seed": "20261113",
}


def synthetic_runs(seed, runs, scale=1.0, frames=240):
    """runs timed runs of frames frame times in ms, each drawn uniformly from 15 to 18.5 ms and scaled by scale"""
    rng = random.Random(seed)
    return [[round(scale * rng.uniform(15.0, 18.5), 3) for _ in range(frames)] for _ in range(runs)]


def suite(method=GOOD_METHOD):
    """A suite file's bytes: a [method] table with each key as TOML text, then another table that must be ignored"""
    body = "".join(f"{key} = {value}\n" for key, value in method.items())
    return f'[method]\n{body}\n[scenario]\nname = "synthetic"\n'.encode()


def runner(order, runs_by_arm):
    """A run(i, arm, kind) that returns the frame times of its timed position (each arm's runs handed out in order) or a
    correctness mark, and the list of positions it was called with"""
    handed = {arm: iter(runs) for arm, runs in runs_by_arm.items()}
    frames = {i: next(handed[arm]) for i, (arm, kind) in enumerate(order) if kind == "timed"}
    calls = []

    def run(i, arm, kind):
        calls.append(i)
        return {"frames": frames[i]} if kind == "timed" else {"checked": True}

    return run, calls


def verdict_of(entries, floor, method=METHOD):
    """The verdict from a series' timed results, each arm's runs in series order"""
    arms = {"A": [], "B": []}
    for entry in entries:
        if entry["kind"] == "timed":
            arms[entry["arm"]].append(entry["result"]["frames"])
    return verdict.verdict(arms["A"], arms["B"], floor, method)


class PlanTest(unittest.TestCase):
    def test_five_runs_per_arm_is_the_cycle_cut_at_ten_then_the_correctness_runs(self):
        self.assertEqual(verdict.plan(5), [(arm, "timed") for arm in "ABBAABBAAB"]
                         + [("A", "correctness"), ("B", "correctness")])

    def test_six_runs_per_arm_is_the_cycle_cut_at_twelve_then_the_correctness_runs(self):
        self.assertEqual(verdict.plan(6), [(arm, "timed") for arm in "ABBAABBAABBA"]
                         + [("A", "correctness"), ("B", "correctness")])

    def test_each_arm_gets_exactly_runs_per_arm_timed_runs(self):
        for n in (5, 6, 7, 8):
            with self.subTest(runs_per_arm=n):
                timed = [arm for arm, kind in verdict.plan(n) if kind == "timed"]
                self.assertEqual((timed.count("A"), timed.count("B")), (n, n))

    def test_fewer_than_five_runs_per_arm_is_refused(self):
        for n in (4, 0, -1, "5"):
            with self.subTest(runs_per_arm=n), self.assertRaises(verdict.MethodError):
                verdict.plan(n)


class WarmTest(unittest.TestCase):
    def test_frames_that_start_before_the_warmup_has_elapsed_are_dropped(self):
        # Frames start at 0, 100, 250 and 450 ms. With a 250 ms warm-up, frame 2 is the first kept: it starts at 250.
        self.assertEqual(verdict.warm([100, 150, 200, 300], 0.25), [200, 300])

    def test_no_warmup_keeps_every_frame(self):
        self.assertEqual(verdict.warm([100, 150, 200, 300], 0), [100, 150, 200, 300])

    def test_a_run_left_empty_is_refused(self):
        with self.assertRaises(verdict.VerdictError):
            verdict.warm([100, 150], 1.0)


class LoadMethodTest(unittest.TestCase):
    def refused(self, data, *names):
        with self.assertRaises(verdict.MethodError) as caught:
            verdict.load_method(data, WHERE)
        message = str(caught.exception)
        self.assertIn(WHERE, message)
        for name in names:
            self.assertIn(name, message)

    def with_value(self, key, text):
        return suite({**GOOD_METHOD, key: text})

    def test_a_valid_table_loads_and_the_other_tables_are_ignored(self):
        self.assertEqual(verdict.load_method(suite(), WHERE),
                         verdict.Method(confidence=0.95, runs_per_arm=5, warmup_seconds=1.0, resamples=2000,
                                        seed=20261113))

    def test_the_boundary_values_load(self):
        loaded = verdict.load_method(suite({"confidence": "0.5", "runs_per_arm": "5", "warmup_seconds": "0",
                                            "resamples": "1", "seed": "-3"}), WHERE)
        self.assertEqual(loaded, verdict.Method(confidence=0.5, runs_per_arm=5, warmup_seconds=0.0, resamples=1,
                                                seed=-3))

    def test_bytes_that_are_not_utf8_are_refused(self):
        self.refused(b"[method]\nconfidence = \xff\n")

    def test_invalid_toml_is_refused(self):
        self.refused(b"[method\nconfidence = 0.95\n")

    def test_a_missing_method_table_is_refused(self):
        self.refused(b'[scenario]\nname = "synthetic"\n', "method")

    def test_a_method_that_is_not_a_table_is_refused(self):
        self.refused(b"method = 3\n", "method")

    def test_each_missing_key_is_refused(self):
        for key in GOOD_METHOD:
            with self.subTest(key=key):
                self.refused(suite({k: v for k, v in GOOD_METHOD.items() if k != key}), key)

    def test_an_unknown_key_is_refused(self):
        self.refused(suite({**GOOD_METHOD, "tolerance": "0.1"}), "tolerance")

    def test_a_wrong_type_is_refused(self):
        cases = (("confidence", '"0.95"'), ("confidence", "true"), ("runs_per_arm", "5.0"), ("runs_per_arm", "true"),
                 ("warmup_seconds", '"1.0"'), ("warmup_seconds", "false"), ("resamples", "2.0"),
                 ("seed", '"1"'), ("seed", "true"))
        for key, text in cases:
            with self.subTest(key=key, value=text):
                self.refused(self.with_value(key, text), key)

    def test_a_value_out_of_range_is_refused(self):
        cases = (("confidence", "0.0"), ("confidence", "1.0"), ("confidence", "1.5"), ("confidence", "nan"),
                 ("runs_per_arm", "4"), ("warmup_seconds", "-0.5"), ("warmup_seconds", "nan"), ("resamples", "0"))
        for key, text in cases:
            with self.subTest(key=key, value=text):
                self.refused(self.with_value(key, text), key)


class BootstrapTest(unittest.TestCase):
    def test_an_arm_with_fewer_runs_than_runs_per_arm_is_refused(self):
        with self.assertRaises(verdict.VerdictError):
            verdict.bootstrap(synthetic_runs(1, 4), synthetic_runs(2, 5), METHOD)
        with self.assertRaises(verdict.VerdictError):
            verdict.bootstrap(synthetic_runs(1, 5), synthetic_runs(2, 4), METHOD)

    def test_a_run_whose_median_is_not_positive_is_refused(self):
        no_warmup = verdict.Method(confidence=0.95, runs_per_arm=5, warmup_seconds=0.0, resamples=10, seed=1)
        zeros = [[0.0] * 240 for _ in range(5)]
        with self.assertRaises(verdict.VerdictError):
            verdict.bootstrap(zeros, synthetic_runs(2, 5), no_warmup)

    def test_the_interval_is_equal_tailed(self):
        # Every A run is 10 ms; B has four runs of 10 ms and one of 20 ms. The change is 1 (20 / 10 - 1) when at least
        # three of B's five draws are the 20 ms run, a 5.8% chance. So the 90% interval's upper bound is 1 (its upper
        # tail is 5%) and the 80% interval's is 0 (its upper tail is 10%).
        a = [[10.0] * 240 for _ in range(5)]
        b = [[10.0] * 240 for _ in range(4)] + [[20.0] * 240]
        method = dataclasses.replace(METHOD, warmup_seconds=0.0, confidence=0.9)
        self.assertEqual(verdict.bootstrap(a, b, method), (0.0, 1.0))
        self.assertEqual(verdict.bootstrap(a, b, dataclasses.replace(method, confidence=0.8)), (0.0, 0.0))

    def test_the_same_inputs_give_the_same_interval(self):
        method = dataclasses.replace(METHOD, runs_per_arm=20)
        a, b = synthetic_runs(1, 20), synthetic_runs(2, 20, scale=1.02)
        first = verdict.bootstrap(a, b, method)
        self.assertEqual(first, verdict.bootstrap(a, b, method))
        self.assertLessEqual(first[0], first[1])

    def test_a_higher_confidence_gives_a_wider_interval(self):
        a, b = synthetic_runs(1, 5), synthetic_runs(2, 5, scale=1.02)
        widths = []
        for confidence in (0.5, 0.95, 0.99):
            method = dataclasses.replace(METHOD, confidence=confidence)
            low, high = verdict.bootstrap(a, b, method)
            widths.append(high - low)
        self.assertLess(widths[0], widths[1])
        self.assertLess(widths[1], widths[2])


class VerdictTest(unittest.TestCase):
    def floor(self):
        return verdict.noise_floor(synthetic_runs(1, 5), synthetic_runs(2, 5), METHOD)

    def test_the_noise_floor_is_the_larger_bound_of_the_baseline_interval(self):
        a, b = synthetic_runs(1, 5), synthetic_runs(2, 5)
        low, high = verdict.bootstrap(a, b, METHOD)
        self.assertEqual(verdict.noise_floor(a, b, METHOD), max(abs(low), abs(high)))

    def test_a_against_a_reads_no_change_against_a_floor_from_separate_a_against_a_data(self):
        floor = self.floor()
        self.assertGreater(floor, 0)
        result = verdict.verdict(synthetic_runs(3, 5), synthetic_runs(4, 5), floor, METHOD)
        self.assertEqual(result["verdict"], "no change")

    def test_noise_that_a_zero_floor_calls_slower_reads_no_change_against_the_measured_floor(self):
        # Synthetic A-against-A runs (seeds 7 and 10) whose interval lies just above zero: with no floor they read
        # slower, and against the floor measured from separate A-against-A runs (seeds 1 and 2) they read no change.
        a, b = synthetic_runs(7, 5), synthetic_runs(10, 5)
        self.assertEqual(verdict.verdict(a, b, 0.0, METHOD)["verdict"], "slower")
        self.assertEqual(verdict.verdict(a, b, self.floor(), METHOD)["verdict"], "no change")

    def test_noise_that_a_zero_floor_calls_faster_reads_no_change_against_the_measured_floor(self):
        # Synthetic A-against-A runs (seeds 10 and 14) whose interval lies just below zero: with no floor they read
        # faster, and against the floor measured from separate A-against-A runs (seeds 1 and 2) they read no change.
        a, b = synthetic_runs(10, 5), synthetic_runs(14, 5)
        self.assertEqual(verdict.verdict(a, b, 0.0, METHOD)["verdict"], "faster")
        self.assertEqual(verdict.verdict(a, b, self.floor(), METHOD)["verdict"], "no change")

    def test_a_planted_slowdown_reads_slower(self):
        result = verdict.verdict(synthetic_runs(3, 5), synthetic_runs(4, 5, scale=1.10), self.floor(), METHOD)
        self.assertEqual(result["verdict"], "slower")
        self.assertGreater(result["low"], result["floor"])

    def test_a_planted_speedup_reads_faster(self):
        result = verdict.verdict(synthetic_runs(3, 5), synthetic_runs(4, 5, scale=0.90), self.floor(), METHOD)
        self.assertEqual(result["verdict"], "faster")
        self.assertLess(result["high"], -result["floor"])

    def test_the_verdict_carries_its_inputs(self):
        floor = self.floor()
        result = verdict.verdict(synthetic_runs(3, 5), synthetic_runs(4, 5), floor, METHOD)
        self.assertEqual(set(result), {"verdict", "low", "high", "floor", "a_medians", "b_medians", "method"})
        self.assertEqual(result["floor"], floor)
        self.assertEqual(len(result["a_medians"]), 5)
        self.assertEqual(len(result["b_medians"]), 5)
        self.assertEqual(result["method"], dataclasses.asdict(METHOD))

    def test_a_negative_floor_is_refused(self):
        with self.assertRaises(verdict.VerdictError):
            verdict.verdict(synthetic_runs(3, 5), synthetic_runs(4, 5), -0.01, METHOD)


class SeriesTest(unittest.TestCase):
    def setUp(self):
        self.order = verdict.plan(5)
        self.runs = {"A": synthetic_runs(3, 5), "B": synthetic_runs(4, 5, scale=1.10)}

    def test_each_position_runs_once_after_its_wait_and_shows_what_each_returned(self):
        run, calls = runner(self.order, self.runs)
        entries = verdict.series(self.order, run, lambda: 0.0)
        self.assertEqual(calls, list(range(len(self.order))))
        self.assertEqual([(e["index"], e["arm"], e["kind"]) for e in entries],
                         [(i, arm, kind) for i, (arm, kind) in enumerate(self.order)])
        self.assertEqual({e["held_s"] for e in entries}, {0.0})
        for arm in ("A", "B"):
            with self.subTest(arm=arm):
                frames = [e["result"]["frames"] for e in entries if e["arm"] == arm and e["kind"] == "timed"]
                self.assertEqual(frames, self.runs[arm])
        self.assertEqual([e["result"] for e in entries if e["kind"] == "correctness"], [{"checked": True}] * 2)

    def test_an_exception_from_run_propagates_and_nothing_is_run_twice(self):
        calls = []

        def run(i, arm, kind):
            calls.append(i)
            if i == 2:
                raise RuntimeError("the run failed")
            return {}

        with self.assertRaises(RuntimeError):
            verdict.series(self.order, run, lambda: 0.0)
        self.assertEqual(calls, [0, 1, 2])

    def test_each_wait_comes_before_the_run_it_precedes(self):
        events = []

        def wait():
            events.append("wait")
            return 0.0

        def run(i, arm, kind):
            events.append(i)
            return {}

        verdict.series(self.order, run, wait)
        self.assertEqual(events, [x for i in range(len(self.order)) for x in ("wait", i)])

    def test_an_exception_from_wait_propagates_before_its_run(self):
        calls = []
        waited = []

        def wait():
            if len(waited) == 2:
                raise RuntimeError("the hold check failed")
            waited.append(0.0)
            return 0.0

        def run(i, arm, kind):
            calls.append(i)
            return {}

        with self.assertRaises(RuntimeError):
            verdict.series(self.order, run, wait)
        self.assertEqual(calls, [0, 1])

    def test_a_hold_mid_series_is_shown_and_the_verdict_stands(self):
        floor = verdict.noise_floor(synthetic_runs(1, 5), synthetic_runs(2, 5), METHOD)
        plain_run, plain_calls = runner(self.order, self.runs)
        plain = verdict.series(self.order, plain_run, lambda: 0.0)

        waits = iter([0.0] * 5 + [37.5] + [0.0] * len(self.order))
        held_run, held_calls = runner(self.order, self.runs)
        held = verdict.series(self.order, held_run, lambda: next(waits))

        self.assertEqual(plain_calls, list(range(len(self.order))))
        self.assertEqual(held_calls, list(range(len(self.order))))
        self.assertEqual(held[5]["held_s"], 37.5)
        self.assertEqual([e["held_s"] for e in held if e["index"] != 5], [0.0] * (len(self.order) - 1))
        self.assertEqual([e["result"] for e in held], [e["result"] for e in plain])
        self.assertEqual(verdict_of(plain, floor)["verdict"], "slower")
        self.assertEqual(verdict_of(held, floor), verdict_of(plain, floor))


if __name__ == "__main__":
    unittest.main()
