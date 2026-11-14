"""scripts/harness/gate.py, the bench gate, on synthetic fixtures this test writes from numbers it chooses: PNG
decoding (RGB and RGBA, each row filter, the refusals); the pixel and FLIP differences (a fake flip refuses a path
without a .png suffix, as FLIP does, prints the Mean the test sets, and its argv and inputs are checked); the tolerance
and determinism label measured from two baseline runs; images beyond the tolerance; and a flash line, a new validation
error and a hang line each voiding a bench on its own. No game screenshot, host or real log is read.

RUN_ALL_SKIP: the fake flip is a POSIX shell script.

python -I -B tests/test_harness_gate.py
"""
import os
import random
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

RUN_ALL_SKIP = {"windows": "the fake flip is a POSIX shell script, and it runs under sh"}

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
import gate

SIGNATURE = b"\x89PNG\r\n\x1a\n"

# A 2x2 RGB image. Row 0 is filtered with None; row 1 is filtered with each of the five filters in turn. The filtered
# bytes are worked out by hand from the pixels: row 0 is (10,20,30) (11,30,60), row 1 is (12,10,32) (13,25,70).
# The Paeth predictor for row 1's second pixel is (12,20,60): red takes the left pixel, green the upper-left, blue
# the upper.
EXPECT_RGB = bytes([10, 20, 30, 11, 30, 60, 12, 10, 32, 13, 25, 70])
ROW0 = [0, 10, 20, 30, 11, 30, 60]
ROW1_RGB = {
    0: [0, 12, 10, 32, 13, 25, 70],
    1: [1, 12, 10, 32, 1, 15, 38],
    2: [2, 2, 246, 2, 2, 251, 10],
    3: [3, 7, 0, 17, 2, 5, 24],
    4: [4, 2, 246, 2, 1, 5, 10],
}

# The same image as RGBA, with alpha 200, 100 in row 0 and 50, 150 in row 1; the alpha bytes are filtered too.
ROW0_RGBA = [0, 10, 20, 30, 200, 11, 30, 60, 100]
ROW1_RGBA = {
    0: [0, 12, 10, 32, 50, 13, 25, 70, 150],
    1: [1, 12, 10, 32, 50, 1, 15, 38, 100],
    2: [2, 2, 246, 2, 106, 2, 251, 10, 50],
    3: [3, 7, 0, 17, 206, 2, 5, 24, 75],
    4: [4, 2, 246, 2, 106, 1, 5, 10, 100],
}

# Flat grey; GREY_NEAR differs from it by 40 in total over the 12 channel values; GREY_FAR by 255 in total (red +155 and
# red -100), so the means over 12 channels are 40/(12*255) and 255/(12*255).
GREY = [(100, 100, 100)] * 4
GREY_NEAR = [(110, 100, 100), (100, 100, 100), (100, 100, 100), (100, 100, 70)]
GREY_FAR = [(255, 100, 100), (0, 100, 100), (100, 100, 100), (100, 100, 100)]
NEAR_MEAN = 40 / (12 * 255)
FAR_MEAN = 255 / (12 * 255)

FAKE_FLIP = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_FLIP_LOG"
# as real FLIP does, the loader is chosen by the extension: a path without .png is refused on stdout, exit 255
case "$2" in
  *.png) ;;
  *) echo "Failed to load image $2"; exit 255 ;;
esac
case "$4" in
  *.png) ;;
  *) echo "Failed to load image $4"; exit 255 ;;
esac
cp "$2" "$FAKE_FLIP_SEEN/reference"
cp "$4" "$FAKE_FLIP_SEEN/test"
echo "     Weighted median: 0.250000"
if [ -z "$FAKE_FLIP_NO_MEAN" ]; then
  echo "     Mean: $FAKE_FLIP_MEAN"
fi
if [ -n "$FAKE_FLIP_TWICE" ]; then
  echo "     Mean: $FAKE_FLIP_MEAN"
fi
if [ -n "$FAKE_FLIP_FAIL_STDOUT" ]; then
  echo "fake flip: refused on stdout"
  exit 3
fi
if [ -n "$FAKE_FLIP_FAIL" ]; then
  echo "fake flip: refused on stderr" >&2
  exit 3
fi
exit 0
"""

FLASH = ("vk: flash at present 13: 40 of 960 cells differ from both neighbours, 30 brighter, peak 120, cells 0,0-3,3 "
         "(now present 13, texture frame 9)")
REGION_FLASH = "vk: region flash at present 12: 3 regions, peak +5 at region 1,2"
HANG = "vk: GPU hang: the timeline at 4, 5 sent"
VALIDATION = "Validation Error: [ VUID-vkCmdDraw-None-09600 ] Object 0: handle = 0x1, msg = example"
WARNING = "Validation Warning: [ VUID-vkCmdDraw-None-02696 ] Object 0: handle = 0x1, msg = example"


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def header(width, height, depth=8, color=2, interlace=0):
    return struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace)


def assemble(ihdr, idat, extra=b""):
    return SIGNATURE + chunk(b"IHDR", ihdr) + extra + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def png(width, height, rows, color=2, depth=8, interlace=0, extra=b""):
    """A PNG whose scanlines are rows: each row is its filter type, then its filtered bytes"""
    raw = bytes(byte for row in rows for byte in row)
    return assemble(header(width, height, depth, color, interlace), zlib.compress(raw), extra)


def rgb_image(pixels, width=2, extra=b""):
    """A plain RGB PNG (every row filtered with None) of (r, g, b) pixels, row by row"""
    height = len(pixels) // width
    rows = [[0] + [c for px in pixels[y * width:(y + 1) * width] for c in px] for y in range(height)]
    return png(width, height, rows, extra=extra)


def paeth(left, up, corner):
    estimate = left + up - corner
    to_left, to_up, to_corner = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
    if to_left <= to_up and to_left <= to_corner:
        return left
    return up if to_up <= to_corner else corner


def filter_row(kind, line, prior, bpp):
    """The filtered bytes of one scanline under row filter kind: the forward of each filter, for the round trip"""
    out = []
    for i, x in enumerate(line):
        left = line[i - bpp] if i >= bpp else 0
        corner = prior[i - bpp] if i >= bpp else 0
        up = prior[i]
        predictor = (0, left, up, (left + up) // 2, paeth(left, up, corner))[kind]
        out.append((x - predictor) & 0xFF)
    return out


class GateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in [k for k in os.environ if k.startswith("FAKE_FLIP_")]:
            del os.environ[key]
        self.flip = self.dir / "flip"
        self.flip.write_text(FAKE_FLIP, encoding="utf-8")
        self.flip.chmod(0o755)
        self.flip_log = self.dir / "flip.log"
        os.environ["FAKE_FLIP_LOG"] = str(self.flip_log)
        self.seen = self.dir / "seen"
        self.seen.mkdir()
        os.environ["FAKE_FLIP_SEEN"] = str(self.seen)
        os.environ["FAKE_FLIP_MEAN"] = "0.131893"

    def image(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path


class BaselineCase(GateCase):
    """Grey, near and far shots. self.tol is measured from two baseline runs whose shot a is grey in one and near in
    the other, so its tolerance is exactly NEAR_MEAN: near is at the tolerance, far is past it."""

    def setUp(self):
        super().setUp()
        self.grey = self.image("grey.png", rgb_image(GREY))
        self.near = self.image("near.png", rgb_image(GREY_NEAR))
        self.far = self.image("far.png", rgb_image(GREY_FAR))
        self.tol = gate.tolerance({"a": self.grey, "b": self.grey}, {"a": self.near, "b": self.grey})


class ReadPngTests(GateCase):
    def test_rgb_decodes_under_each_row_filter(self):
        for kind, row in ROW1_RGB.items():
            with self.subTest(filter=kind):
                path = self.image("rgb.png", png(2, 2, [ROW0, row]))
                self.assertEqual(gate.read_png(path), (2, 2, EXPECT_RGB))

    def test_rgba_decodes_under_each_row_filter_and_drops_alpha(self):
        for kind, row in ROW1_RGBA.items():
            with self.subTest(filter=kind):
                path = self.image("rgba.png", png(2, 2, [ROW0_RGBA, row], color=6))
                self.assertEqual(gate.read_png(path), (2, 2, EXPECT_RGB))

    def test_random_full_range_rows_round_trip_under_every_filter(self):
        # the known answers above stay under 128, so they cannot show a sum wrapping past 255: random bytes do
        rng = random.Random(7)
        width, height = 5, 4
        for color, bpp in ((2, 3), (6, 4)):
            for kind in range(5):
                with self.subTest(color=color, filter=kind):
                    pixels = [[rng.randrange(256) for _ in range(width * bpp)] for _ in range(height)]
                    rows, prior = [], [0] * (width * bpp)
                    for line in pixels:
                        rows.append([kind, *filter_row(kind, line, prior, bpp)])
                        prior = line
                    path = self.image("random.png", png(width, height, rows, color=color))
                    expected = bytes(v for line in pixels for p in range(0, len(line), bpp) for v in line[p:p + 3])
                    self.assertEqual(gate.read_png(path), (width, height, expected))

    def test_other_formats_are_refused(self):
        row = [0, 1, 2, 3, 4, 5, 6]
        cases = (
            ("16-bit RGB", png(2, 1, [row], depth=16)),
            ("grayscale", png(2, 1, [row], color=0)),
            ("palette", png(2, 1, [row], color=3)),
            ("gray and alpha", png(2, 1, [row], color=4)),
            ("interlaced", png(2, 1, [row], interlace=1)),
        )
        for label, data in cases:
            with self.subTest(label):
                path = self.image("other.png", data)
                with self.assertRaises(gate.GateError):
                    gate.read_png(path)

    def test_an_unknown_row_filter_is_refused(self):
        path = self.image("filter5.png", png(2, 1, [[5, 1, 2, 3, 4, 5, 6]]))
        with self.assertRaises(gate.GateError):
            gate.read_png(path)

    def test_corrupt_files_are_refused(self):
        good = png(2, 2, [ROW0, ROW1_RGB[0]])
        idat_body = good.index(b"IDAT") + 4
        cases = (
            ("not a PNG", b"GIF89a" + good[6:]),
            ("truncated", good[:-5]),
            ("IEND checksum wrong", good[:-1] + bytes([good[-1] ^ 0xFF])),
            ("IDAT byte changed", good[:idat_body] + bytes([good[idat_body] ^ 0xFF]) + good[idat_body + 1:]),
            ("no IDAT", SIGNATURE + chunk(b"IHDR", header(2, 2)) + chunk(b"IEND", b"")),
            ("IDAT not zlib", assemble(header(2, 2), b"not zlib data")),
            ("a row missing", png(2, 2, [ROW0])),
            ("zero width", png(0, 2, [[0], [0]])),
        )
        for label, data in cases:
            with self.subTest(label):
                path = self.image("corrupt.png", data)
                with self.assertRaises(gate.GateError):
                    gate.read_png(path)

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(gate.GateError):
            gate.read_png(self.dir / "absent.png")


class DifferenceTests(GateCase):
    def grey_pair(self):
        return self.image("x.png", rgb_image(GREY)), self.image("y.png", rgb_image(GREY_NEAR))

    def test_byte_identical_files_are_zero_without_decoding_or_flip(self):
        corrupt = b"not a PNG, and never decoded"
        a = self.image("a.png", corrupt)
        b = self.image("b.png", corrupt)
        self.assertEqual(gate.difference(a, b), ("pixel", 0.0))
        self.assertEqual(gate.difference(a, b, flip=self.flip), ("flip", 0.0))
        self.assertFalse(self.flip_log.exists(), "flip ran on byte-identical files")

    def test_pixel_difference_is_the_mean_absolute_channel_difference_over_255(self):
        x, y = self.grey_pair()
        for pair in ((x, y), (y, x)):
            metric, value = gate.difference(*pair)
            self.assertEqual(metric, "pixel")
            self.assertAlmostEqual(value, NEAR_MEAN, places=12)

    def test_different_sizes_are_refused_for_both_metrics(self):
        square = self.image("square.png", rgb_image(GREY))
        strip = self.image("strip.png", rgb_image(GREY, width=4))
        with self.assertRaises(gate.GateError):
            gate.difference(square, strip)
        with self.assertRaises(gate.GateError):
            gate.difference(square, strip, flip=self.flip)
        self.assertFalse(self.flip_log.exists(), "flip ran on images of different sizes")

    def test_flip_runs_with_the_fixed_arguments_and_its_mean_is_the_difference(self):
        x, y = self.grey_pair()
        metric, value = gate.difference(x, y, flip=self.flip)
        self.assertEqual(metric, "flip")
        self.assertAlmostEqual(value, 0.131893, places=12)
        argv = self.flip_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(argv), 5)
        self.assertEqual([argv[0], argv[2], argv[4]], ["-r", "-t", "-nerm"])
        self.assertEqual(Path(argv[1]).suffix, ".png")
        self.assertEqual(Path(argv[3]).suffix, ".png")
        self.assertEqual((self.seen / "reference").read_bytes(), x.read_bytes())
        self.assertEqual((self.seen / "test").read_bytes(), y.read_bytes())

    def test_flip_measures_screenshots_stored_without_an_extension(self):
        # the record store names each screenshot by its sha256 digest, with no extension; FLIP refuses such a name, so
        # it must receive .png copies, which are gone once it has run and are never made beside the screenshots
        shots = self.dir / "shots"
        shots.mkdir()
        x = shots / "3f9a1c07e2b84d15"
        y = shots / "b61d0e9f4a72c583"
        x.write_bytes(rgb_image(GREY))
        y.write_bytes(rgb_image(GREY_NEAR))
        metric, value = gate.difference(x, y, flip=self.flip)
        self.assertEqual(metric, "flip")
        self.assertAlmostEqual(value, 0.131893, places=12)
        self.assertEqual((self.seen / "reference").read_bytes(), x.read_bytes())
        self.assertEqual((self.seen / "test").read_bytes(), y.read_bytes())
        self.assertEqual(sorted(p.name for p in shots.iterdir()), ["3f9a1c07e2b84d15", "b61d0e9f4a72c583"])
        argv = self.flip_log.read_text(encoding="utf-8").splitlines()
        self.assertFalse(Path(argv[1]).exists(), "the reference copy outlived the run")
        self.assertFalse(Path(argv[3]).exists(), "the test copy outlived the run")

    def test_a_flip_that_exits_nonzero_is_refused(self):
        x, y = self.grey_pair()
        os.environ["FAKE_FLIP_FAIL"] = "1"
        with self.assertRaises(gate.GateError) as caught:
            gate.difference(x, y, flip=self.flip)
        self.assertIn("exited 3", str(caught.exception))
        self.assertIn("fake flip: refused on stderr", str(caught.exception))

    def test_a_flip_that_fails_on_stdout_names_what_it_printed(self):
        # as real FLIP does, a load failure is printed on stdout, and stderr is empty
        x, y = self.grey_pair()
        os.environ["FAKE_FLIP_FAIL_STDOUT"] = "1"
        with self.assertRaises(gate.GateError) as caught:
            gate.difference(x, y, flip=self.flip)
        self.assertIn("exited 3", str(caught.exception))
        self.assertIn("fake flip: refused on stdout", str(caught.exception))

    def test_a_flip_with_no_mean_line_is_refused(self):
        x, y = self.grey_pair()
        os.environ["FAKE_FLIP_NO_MEAN"] = "1"
        with self.assertRaises(gate.GateError):
            gate.difference(x, y, flip=self.flip)

    def test_a_flip_whose_mean_is_not_a_number_is_refused(self):
        x, y = self.grey_pair()
        os.environ["FAKE_FLIP_MEAN"] = "not-a-number"
        with self.assertRaises(gate.GateError):
            gate.difference(x, y, flip=self.flip)

    def test_a_flip_that_prints_two_mean_lines_is_refused(self):
        x, y = self.grey_pair()
        os.environ["FAKE_FLIP_TWICE"] = "1"
        with self.assertRaises(gate.GateError):
            gate.difference(x, y, flip=self.flip)

    def test_a_flip_that_cannot_be_run_is_refused(self):
        x, y = self.grey_pair()
        with self.assertRaises(gate.GateError):
            gate.difference(x, y, flip=self.dir / "absent-flip")


class ToleranceTests(GateCase):
    def test_identical_baseline_runs_are_deterministic_with_zero_tolerance(self):
        first = {"a": self.image("run1-a.png", rgb_image(GREY)), "b": self.image("run1-b.png", rgb_image(GREY_NEAR))}
        second = {"a": self.image("run2-a.png", rgb_image(GREY)), "b": self.image("run2-b.png", rgb_image(GREY_NEAR))}
        self.assertEqual(gate.tolerance(first, second), {
            "metric": "pixel",
            "tolerance": 0.0,
            "determinism": "deterministic",
            "shots": {"a": 0.0, "b": 0.0},
        })

    def test_differing_baseline_runs_take_the_largest_shot_difference(self):
        first = {"a": self.image("r1a.png", rgb_image(GREY)), "b": self.image("r1b.png", rgb_image(GREY)),
                 "c": self.image("r1c.png", rgb_image(GREY))}
        second = {"a": self.image("r2a.png", rgb_image(GREY_NEAR)), "b": self.image("r2b.png", rgb_image(GREY)),
                  "c": self.image("r2c.png", rgb_image(GREY_FAR))}
        tol = gate.tolerance(first, second)
        self.assertEqual(tol["metric"], "pixel")
        self.assertEqual(tol["determinism"], "nondeterministic")
        self.assertEqual(sorted(tol["shots"]), ["a", "b", "c"])
        self.assertAlmostEqual(tol["shots"]["a"], NEAR_MEAN, places=12)
        self.assertEqual(tol["shots"]["b"], 0.0)
        self.assertAlmostEqual(tol["shots"]["c"], FAR_MEAN, places=12)
        self.assertAlmostEqual(tol["tolerance"], FAR_MEAN, places=12)

    def test_the_same_pixels_in_other_bytes_are_nondeterministic_with_zero_tolerance(self):
        first = {"a": self.image("plain.png", rgb_image(GREY))}
        second = {"a": self.image("text.png", rgb_image(GREY, extra=chunk(b"tEXt", b"Comment\x00same pixels")))}
        tol = gate.tolerance(first, second)
        self.assertEqual(tol["determinism"], "nondeterministic")
        self.assertEqual(tol["tolerance"], 0.0)

    def test_shot_names_must_match(self):
        x = self.image("x.png", rgb_image(GREY))
        with self.assertRaises(gate.GateError):
            gate.tolerance({"a": x}, {"b": x})

    def test_flip_measures_the_tolerance_when_given(self):
        os.environ["FAKE_FLIP_MEAN"] = "0.02"
        first = {"a": self.image("f1.png", rgb_image(GREY))}
        second = {"a": self.image("f2.png", rgb_image(GREY_NEAR))}
        tol = gate.tolerance(first, second, flip=self.flip)
        self.assertEqual(tol["metric"], "flip")
        self.assertEqual(tol["determinism"], "nondeterministic")
        self.assertAlmostEqual(tol["tolerance"], 0.02, places=12)
        self.assertAlmostEqual(tol["shots"]["a"], 0.02, places=12)

    def test_flip_measures_the_tolerance_of_shots_stored_without_an_extension(self):
        os.environ["FAKE_FLIP_MEAN"] = "0.02"
        first = {"a": self.image("0c4e7a91", rgb_image(GREY))}
        second = {"a": self.image("d2f85b13", rgb_image(GREY_NEAR))}
        tol = gate.tolerance(first, second, flip=self.flip)
        self.assertEqual(tol["metric"], "flip")
        self.assertAlmostEqual(tol["tolerance"], 0.02, places=12)
        self.assertAlmostEqual(tol["shots"]["a"], 0.02, places=12)


class ImagesBeyondTests(BaselineCase):
    def test_images_at_the_tolerance_stand(self):
        # the near image differs by exactly the tolerance: beyond means strictly more than it
        self.assertEqual(gate.images_beyond(self.tol, {"a": self.grey, "b": self.grey},
                                            {"a": self.near, "b": self.grey}), [])

    def test_images_past_the_tolerance_and_names_on_one_side_only_are_beyond_in_sorted_order(self):
        reference = {"a": self.grey, "b": self.grey, "c": self.grey}
        test = {"a": self.far, "b": self.near, "d": self.grey}
        self.assertEqual(gate.images_beyond(self.tol, reference, test), ["a", "c", "d"])

    def test_the_metric_must_be_the_tolerances(self):
        with self.assertRaises(gate.GateError):
            gate.images_beyond(self.tol, {"a": self.grey}, {"a": self.near}, flip=self.flip)
        flip_tol = gate.tolerance({"a": self.grey}, {"a": self.near}, flip=self.flip)
        with self.assertRaises(gate.GateError):
            gate.images_beyond(flip_tol, {"a": self.grey}, {"a": self.near})


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


class VoidTests(BaselineCase):
    def reasons(self, b_shots=None, a_log="", b_log="", timed=()):
        a_shots = {"a": self.grey, "b": self.grey}
        if b_shots is None:
            b_shots = {"a": self.near, "b": self.grey}
        return gate.void_reasons(self.tol, a_shots, b_shots, a_log, b_log, list(timed))

    def test_a_bench_within_tolerance_with_clean_logs_stands(self):
        self.assertEqual(self.reasons(), [])

    def test_images_beyond_the_tolerance_void_the_bench(self):
        reasons = self.reasons(b_shots={"a": self.far, "b": self.grey})
        self.assertEqual(len(reasons), 1)
        self.assertIn("beyond the tolerance", reasons[0])

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
        reasons = self.reasons(b_shots={"a": self.far, "b": self.grey}, a_log=REGION_FLASH, b_log=VALIDATION,
                               timed=[HANG])
        self.assertEqual(len(reasons), 4)


if __name__ == "__main__":
    unittest.main()
