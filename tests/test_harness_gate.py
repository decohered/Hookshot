"""scripts/harness/gate.py, the bench gate: whether a bench is void, from its logs alone (DL185: no verdict compares
screenshots, so the gate takes no picture). A flash line in either correctness log, a validation error new in correctness
log b, and a hang line in any log each void a bench on their own; a validation error both correctness logs share, a
validation warning, and a flash in a timed log do not. No game log or host is read: the logs are text this test writes.

python -I -B tests/test_harness_gate.py
"""
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
import gate

FLASH = ("vk: flash at present 13: 40 of 960 cells differ from both neighbours, 30 brighter, peak 120, cells 0,0-3,3 "
         "(now present 13, texture frame 9)")
REGION_FLASH = "vk: region flash at present 12: 3 regions, peak +5 at region 1,2"
HANG = "vk: GPU hang: the timeline at 4, 5 sent"
VALIDATION = "Validation Error: [ VUID-vkCmdDraw-None-09600 ] Object 0: handle = 0x1, msg = example"
WARNING = "Validation Warning: [ VUID-vkCmdDraw-None-02696 ] Object 0: handle = 0x1, msg = example"


class LogTests(unittest.TestCase):
    def test_flashes_are_the_two_flash_lines_only(self):
        log = "\n".join(["vk: presented 12", REGION_FLASH, "vk: flashing the menu", FLASH, "vk: GPU recovered"])
        self.assertEqual(gate.flashes(log), [REGION_FLASH, FLASH])

    def test_hangs_are_the_three_hang_lines_only(self):
        lines = [
            "vk: the GPU has finished nothing for 10 s: the timeline at 4, 5 sent (RX_VK_GPU_HANG_SEC=60)",
            HANG,
            "vk: FATAL: the GPU hung: no submission finished in 60 s",
        ]
        log = "\n".join(["vk: presented 12", *lines, "vk: GPU recovered"])
        self.assertEqual(gate.hangs(log), lines)

    def test_validation_errors_are_the_ids_of_error_lines_only(self):
        log = "\n".join([VALIDATION, WARNING, "Validation Error: [ VUID-vkQueueSubmit-pSignalSemaphores-00067 ] x"])
        self.assertEqual(gate.validation_errors(log),
                         {"VUID-vkCmdDraw-None-09600", "VUID-vkQueueSubmit-pSignalSemaphores-00067"})


class VoidTests(unittest.TestCase):
    def reasons(self, a_log="", b_log="", timed=()):
        return gate.void_reasons(a_log, b_log, list(timed))

    def test_the_gate_takes_the_two_correctness_logs_and_the_timed_logs_and_no_pictures(self):
        self.assertEqual(list(inspect.signature(gate.void_reasons).parameters), ["a_log", "b_log", "timed_logs"])

    def test_clean_logs_stand(self):
        self.assertEqual(self.reasons(), [])

    def test_a_flash_in_either_correctness_log_voids_on_its_own(self):
        for label, kwargs in (("log a", {"a_log": REGION_FLASH}), ("log b", {"b_log": FLASH})):
            with self.subTest(label):
                reasons = self.reasons(**kwargs)
                self.assertEqual(len(reasons), 1)
                self.assertIn("flash", reasons[0])

    def test_a_flash_in_a_timed_log_does_not_void(self):
        self.assertEqual(self.reasons(timed=[FLASH]), [])

    def test_a_validation_error_new_in_b_voids_on_its_own(self):
        reasons = self.reasons(b_log=VALIDATION)
        self.assertEqual(len(reasons), 1)
        self.assertIn("VUID-vkCmdDraw-None-09600", reasons[0])

    def test_a_validation_error_already_in_a_does_not_void(self):
        self.assertEqual(self.reasons(a_log=VALIDATION, b_log=VALIDATION), [])

    def test_a_validation_error_only_in_a_does_not_void(self):
        self.assertEqual(self.reasons(a_log=VALIDATION), [])

    def test_a_validation_warning_in_b_does_not_void(self):
        self.assertEqual(self.reasons(b_log=WARNING), [])

    def test_a_hang_in_any_log_voids_on_its_own(self):
        cases = (
            ("correctness log a", HANG, "", ()),
            ("correctness log b", "", HANG, ()),
            ("timed log", "", "", (HANG,)),
        )
        for label, a_log, b_log, timed in cases:
            with self.subTest(label):
                reasons = self.reasons(a_log=a_log, b_log=b_log, timed=timed)
                self.assertEqual(len(reasons), 1)
                self.assertIn("hang", reasons[0])

    def test_each_cause_gives_its_own_reason(self):
        reasons = self.reasons(a_log=REGION_FLASH, b_log=VALIDATION, timed=[HANG])
        self.assertEqual(len(reasons), 3)


if __name__ == "__main__":
    unittest.main()
