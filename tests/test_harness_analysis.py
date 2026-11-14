"""scripts/harness/analysis.py on synthetic captures written here in the format of host/rx_capture.h (version 1). Each
statistic is checked against an answer worked out by hand: an exact and a normalised repeat of a recording, a page listed
twice unchanged and never changed, the K curve, a cross-queue wait, a texture first used, made by the GPU, changed and
written, two captures for texture_repeat, a window, draws and mesh draws per frame, whole-capture totals, the unbound
memory, the trailing work after the last present. The output names no address, page, blob or program, and a malformed
capture raises AnalysisError. No game, dump, GPU or network is used.

python -I -B tests/test_harness_analysis.py
"""
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "harness"))
from analysis import AnalysisError, analyze

PAGE = 4096
BEGIN, END, BARRIER, SIGNAL, WAIT = 2, 0, 28, 4, 48
SET_RT, BIND_PROGRAM, BIND_UBO, BIND_TEX = 8, 14, 19, 21
DRAW, DRAW_IDX, DRAW_INDIRECT, DRAW_MESH = 25, 26, 45, 47

HANDLES = 0x6A00_0000_0100              # the handle array every submit names
REC_A, REC_B, REC_C = 0x6A00_0001_0000, 0x6A00_0001_0100, 0x6A00_0001_0200
REC_D, REC_E = 0x6A00_0001_0300, 0x6A00_0001_0400
TARGET_A, TARGET_B = 0x6A00_0002_0000, 0x6A00_0002_1000   # two nvn_tex objects with the same bytes
UNKNOWN_A, UNKNOWN_B = 0x6A00_0003_0000, 0x6A00_0003_1000  # pointers with no object
TEX_X, TEX_Y, TEX_Z, TEX_W = 0x6A00_0004_0000, 0x6A00_0004_1000, 0x6A00_0004_2000, 0x6A00_0004_3000
SRC_TEXELS = 0x6A00_0005_0000           # the texel bytes a write copies from
PROGRAM = 0x6A00_0006_0000              # a program a recording binds
EVENT = 0x0003_0000                     # the GPU address of an event
GPU_X, GPU_Y, GPU_Z, GPU_W, GPU_U = 0x40000, 0x50000, 0x60000, 0x80000, 0x90000

ZERO = bytes(PAGE)
ONE_BYTE_CHANGED = b"\x01" * 10 + bytes(PAGE - 10)   # differs from ZERO in 10 bytes
X0 = bytes(range(256)) * 16                         # X's storage: one page
Y0, Y1 = b"\x02" * PAGE, b"\x03" * PAGE
Z0, Z1 = ZERO, b"\xff" * PAGE                       # Z changes in every byte
U0 = b"\x04" * PAGE
U1 = b"\x05" * 5 + b"\x04" * (PAGE - 5)             # U differs from U0 in 5 bytes


def body(size, *fields):
    """A payload of size bytes, with each (offset, struct format, value) written into it"""
    out = bytearray(size)
    for offset, fmt, value in fields:
        struct.pack_into(fmt, out, offset, value)
    return bytes(out)


def op(code, payload=b""):
    """One command record: its op, its size (header included, 8-aligned) and its payload"""
    size = 8 + len(payload)
    size += -size % 8
    return struct.pack("<II", code, size) + payload + bytes(size - 8 - len(payload))


def recording(*records):
    return b"".join(records)


def texture_bytes(fmt, gpu, size):
    """An nvn_tex's bytes (128): format at +8, storage size at +56, storage gpu at +96"""
    return body(128, (8, "<i", fmt), (56, "<Q", size), (96, "<Q", gpu))


PLAIN = recording(op(BEGIN), op(BARRIER), op(END))


def target(addr):
    """A recording that renders into the nvn_tex at addr"""
    return recording(op(BEGIN), op(SET_RT, body(216, (0, "<i", 1), (8, "<Q", addr))), op(END))


def use_textures(*ids):
    """A BIND_TEX of the registered texture ids"""
    return op(BIND_TEX, body(24 + 8 * len(ids), (8, "<i", len(ids)),
                             *[(24 + 8 * k, "<Q", ident) for k, ident in enumerate(ids)]))


def read_range(addr, size):
    """A BIND_UBO of a range of memory"""
    return op(BIND_UBO, body(24, (8, "<Q", addr), (16, "<Q", size)))


def draw_indexed(count):
    """A DRAW_IDX of count 1-byte indices from GPU_U"""
    return op(DRAW_IDX, body(32, (4, "<i", 0), (8, "<i", count), (24, "<Q", GPU_U)))


def draw_indirect():
    """A DRAW_INDIRECT: one draw of 16 bytes from GPU_W, its count at GPU_W + 16"""
    return op(DRAW_INDIRECT, body(48, (12, "<i", 1), (16, "<i", 16), (32, "<Q", GPU_W), (40, "<Q", GPU_W + 16)))


def draw_mesh():
    """A DRAW_MESH, read as its kind alone"""
    return op(DRAW_MESH)


class Capture:
    """A capture folder written record by record: calls.rxc in the version 1 format, and the blobs its records name"""

    def __init__(self, folder):
        self.folder = folder
        self.records = []
        os.makedirs(os.path.join(folder, "blobs"), exist_ok=True)

    def blob(self, data):
        sha = hashlib.sha256(data).digest()
        with open(os.path.join(self.folder, "blobs", sha.hex()), "wb") as f:
            f.write(data)
        return sha

    def add(self, kind, payload=b"", queue=0):
        self.records.append((kind, payload, queue))
        return len(self.records)

    def object(self, addr, data):
        self.add(1, struct.pack("<QQ", addr, len(data)) + self.blob(data))

    def pool(self, start, pages):
        """A POOL over the pages (each PAGE bytes) laid from start"""
        shas = [self.blob(page) for page in pages]
        self.add(2, struct.pack("<QQ", start, PAGE * len(pages)) + b"".join(shas))

    def pages(self, entries):
        """A PAGES listing (page number, bytes) entries"""
        payload = struct.pack("<Q", len(entries))
        for page, data in entries:
            payload += struct.pack("<Q", page) + self.blob(data)
        self.add(3, payload)

    def call(self, kind, *args, queue=0):
        seq = self.add(kind, struct.pack("<%dQ" % len(args), *args), queue)
        self.add(kind | 0x10000, struct.pack("<QQ", seq, 0), queue)

    def submit(self, array, recordings, queue=0):
        for addr, data in recordings:
            self.object(addr, data)
        self.object(array, struct.pack("<%dQ" % len(recordings), *[addr for addr, _ in recordings]))
        if queue:
            self.call(17, array, len(recordings), queue, queue=queue)
        else:
            self.call(16, array, len(recordings))

    def present(self, tex=0):
        self.call(25, tex, 0)

    def register(self, ident, addr, fmt, gpu, size):
        self.object(addr, texture_bytes(fmt, gpu, size))
        self.call(26, ident, addr, 0)

    def write(self, seqs=None):
        """calls.rxc: the header, then each record with its seq (seqs replaces some by record number)"""
        with open(os.path.join(self.folder, "calls.rxc"), "wb") as f:
            f.write(b"RXCAPT01" + struct.pack("<II", 1, 0))
            for number, (kind, payload, queue) in enumerate(self.records, 1):
                seq = (seqs or {}).get(number, number)
                f.write(struct.pack("<IIQQQ", kind, 32 + len(payload), seq, 0, queue) + payload)


def scan(build, window=None, against=None):
    """analyze on a capture that build writes into a temporary folder: (the result, the capture's records)"""
    with tempfile.TemporaryDirectory() as folder:
        c = Capture(os.path.join(folder, "capture"))
        build(c)
        c.write()
        return analyze(c.folder, window=window, against=against), len(c.records)


def summary(count, total, mean, p50, p90, p99, peak):
    return {"count": count, "total": total, "mean": mean, "p50": p50, "p90": p90, "p99": p99, "max": peak}


def build_repeat(c):
    same = texture_bytes(7, GPU_X, PAGE)
    c.object(TARGET_A, same)
    c.object(TARGET_B, same)
    c.submit(HANDLES, [(REC_A, PLAIN)])
    c.submit(HANDLES, [(REC_A, PLAIN)])                  # the same bytes again: an exact repeat
    c.submit(HANDLES, [(REC_B, target(TARGET_A))])
    c.submit(HANDLES, [(REC_C, target(TARGET_B))])       # its target's bytes match B's: a repeat after normalising
    c.submit(HANDLES, [(REC_D, target(UNKNOWN_A))])      # no object: the pointer stays as it is
    c.submit(HANDLES, [(REC_E, target(UNKNOWN_B))])
    c.present()


class RepeatTest(unittest.TestCase):
    def test_exact_and_normalised_repeats_are_counted_apart(self):
        result, _ = scan(build_repeat)
        plain, target_size = len(PLAIN), len(target(TARGET_A))
        self.assertEqual(result["repeat"], {
            "recordings": 6,
            "bytes": 2 * plain + 4 * target_size,
            "exact": {"recordings": 1, "bytes": plain},
            "normalised": {"recordings": 2, "bytes": plain + target_size},
        })

    def test_a_window_counts_only_its_frames_submits(self):
        def build(c):
            c.submit(HANDLES, [(REC_A, PLAIN)])
            c.present()
            c.submit(HANDLES, [(REC_A, PLAIN)])
            c.submit(HANDLES, [(REC_A, PLAIN)])
            c.present()
        result, _ = scan(build, window=(2, 2))
        self.assertEqual(result["submits"], 2)
        self.assertEqual(result["repeat"]["recordings"], 2)
        self.assertEqual(result["repeat"]["exact"], {"recordings": 2, "bytes": len(PLAIN)*2})


def build_pages(c):
    c.pool(0x20000, [ZERO, Y0])                 # pages 0x20 and 0x21
    c.pages([(0x20, ZERO)])                     # frame 1: listed with the hash it was given
    c.submit(HANDLES, [(REC_A, recording(op(BEGIN), read_range(0x20000, PAGE), op(END)))])
    c.present()
    c.pages([(0x20, ZERO)])                     # frame 2: the same hash again: unchanged, never changed
    c.submit(HANDLES, [(REC_A, recording(op(BEGIN), read_range(0x20000, PAGE), op(END)))])
    c.present()
    c.pages([(0x20, ONE_BYTE_CHANGED), (0x30, Y1)])   # frame 3: 0x20 changed in 10 bytes, 0x30 first
    c.submit(HANDLES, [(REC_A, recording(op(BEGIN), read_range(0x20000, PAGE), op(END)))])
    c.submit(HANDLES, [(REC_A, recording(op(BEGIN), read_range(0x20000, PAGE), op(END)))])
    c.present()


class PagesTest(unittest.TestCase):
    def test_a_page_listed_twice_with_one_hash_is_unchanged(self):
        result, _ = scan(build_pages)
        self.assertEqual(result["pages"], {
            "listed": 4,
            "first": 1,
            "unchanged": 2,
            "changed": 1,
            "changed_bytes": summary(1, 10, 10.0, 10, 10, 10, 10),
            "changed_read": 1,
            "changed_read_share": 1.0,
        })

    def test_the_k_curve_counts_presents_since_a_change_and_the_next_change(self):
        # S1 (frame 1) reads 0x20 last changed by the POOL in frame 1: K 0; S2's PAGES lists it unchanged.
        # S2 (frame 2): K 1; S3's PAGES lists it changed. S3 (frame 3): K 0, its next submit's PAGES is empty.
        # S4 (frame 3): K 0 and no next submit, so it is left out.
        result, _ = scan(build_pages)
        self.assertEqual(result["cold"], [
            {"presents_since_change": 0, "reads": 2, "changed": 0},
            {"presents_since_change": 1, "reads": 1, "changed": 1},
        ])


def build_sync(c):
    signal = body(8, (0, "<Q", EVENT))
    c.call(22, 1, 0)                            # frame 1: a fence wait, a writeback wait, a finish
    c.call(23, 0, 0)
    c.call(24)
    c.submit(HANDLES, [(REC_A, recording(op(BEGIN), op(SIGNAL, signal), op(BARRIER), op(END)))], queue=0)
    c.submit(HANDLES, [(REC_B, recording(op(BEGIN), op(WAIT, signal), op(END)))], queue=1)
    c.submit(HANDLES, [(REC_C, recording(op(BEGIN), op(SIGNAL, signal), op(WAIT, signal), op(END)))], queue=1)
    c.present()
    c.call(35, 0x50000, 64)                     # frame 2: two invalidates
    c.call(35, 0x50000, 64)
    c.submit(HANDLES, [(REC_D, recording(op(BEGIN), op(WAIT, signal), op(END)))], queue=0)
    c.present()


class SyncTest(unittest.TestCase):
    def test_per_frame_counts_and_a_cross_queue_wait(self):
        # Frame 1: REC_B waits on an event queue 0 signalled (cross); REC_C signals and waits on queue 1 (not cross).
        # Frame 2: REC_D, on queue 0, waits on an event queue 1 signalled last (cross).
        result, _ = scan(build_sync)
        self.assertEqual(result["sync"], {
            "frames": 2,
            "invalidates": summary(2, 2, 1.0, 0, 2, 2, 2),
            "read_texels": summary(2, 0, 0.0, 0, 0, 0, 0),
            "writeback_waits": summary(2, 1, 0.5, 0, 1, 1, 1),
            "fence_waits": summary(2, 1, 0.5, 0, 1, 1, 1),
            "finishes": summary(2, 1, 0.5, 0, 1, 1, 1),
            "barriers": summary(2, 1, 0.5, 0, 1, 1, 1),
            "event_waits": summary(2, 3, 1.5, 1, 2, 2, 2),
            "cross_queue_waits": summary(2, 2, 1.0, 1, 1, 1, 1),
            "draws": summary(2, 0, 0.0, 0, 0, 0, 0),
            "mesh_draws": summary(2, 0, 0.0, 0, 0, 0, 0),
        })


def build_draws(c):
    c.submit(HANDLES, [(REC_A, recording(op(BEGIN), op(DRAW), draw_indexed(6), draw_mesh(), op(END))),
                       (REC_B, recording(op(BEGIN), draw_indirect(), op(END)))])
    c.present()                                 # frame 1: 3 draws, 1 mesh draw
    c.submit(HANDLES, [(REC_C, recording(op(BEGIN), draw_indexed(6), draw_mesh(), draw_mesh(), op(END))),
                       (REC_D, recording(op(BEGIN), draw_indirect(), op(DRAW), draw_indexed(3), op(END)))])
    c.present()                                 # frame 2: 4 draws, 2 mesh draws
    c.submit(HANDLES, [(REC_E, recording(op(BEGIN), op(DRAW), draw_mesh(), op(END)))])
    c.present()                                 # frame 3: 1 draw, 1 mesh draw


class DrawTest(unittest.TestCase):
    def test_draws_and_mesh_draws_per_frame_in_the_window(self):
        # Frame 1 draws: DRAW, the indexed draw, the indirect draw. Frame 2: indexed, indirect, DRAW, indexed.
        # A mesh draw is a mesh draw only. Frame 3 is outside the window: its one draw and one mesh draw are not counted.
        result, _ = scan(build_draws, window=(1, 2))
        self.assertEqual(result["sync"]["draws"], summary(2, 7, 3.5, 3, 4, 4, 4))
        self.assertEqual(result["sync"]["mesh_draws"], summary(2, 3, 1.5, 1, 2, 2, 2))

    def test_every_frame_counts_without_a_window(self):
        # Per frame, draws 3, 4, 1 and mesh draws 1, 2, 1.
        result, _ = scan(build_draws)
        self.assertEqual(result["sync"]["draws"], summary(3, 8, 8 / 3, 3, 4, 4, 4))
        self.assertEqual(result["sync"]["mesh_draws"], summary(3, 4, 4 / 3, 1, 2, 2, 2))


def build_whole(c):
    c.submit(HANDLES, [(REC_A, recording(op(BEGIN), op(BARRIER), op(DRAW), op(END)))])
    c.present()                                 # frame 1: 1 barrier, 1 draw
    c.submit(HANDLES, [(REC_B, recording(op(BEGIN), op(BARRIER), draw_mesh(), draw_mesh(), op(END)))])
    c.present()                                 # frame 2: 1 barrier, 2 mesh draws
    c.submit(HANDLES, [(REC_C, recording(op(BEGIN), op(BARRIER), op(BARRIER), op(BARRIER), draw_indexed(6),
                                         op(END)))])
    c.present()                                 # frame 3: 3 barriers, 1 draw
    c.submit(HANDLES, [(REC_D, recording(op(BEGIN), op(DRAW), draw_mesh(), op(BARRIER), op(END)))])
    c.submit(HANDLES, [(REC_E, recording(op(BEGIN), op(DRAW), draw_indexed(3), draw_mesh(), draw_mesh(), draw_mesh(),
                                         op(BARRIER), op(END)))])
    # frame 4 is incomplete: no present follows it, so its two submits are trailing work and none of it is whole


WHOLE_RESULT = {"frames": 3, "presents": 3, "draws": 2, "mesh_draws": 2, "barriers": 5}
TRAILING_RESULT = {"draws": 3, "mesh_draws": 4, "barriers": 2}   # REC_D: 1, 1, 1; REC_E: 2, 3, 1


class WholeTest(unittest.TestCase):
    def test_whole_and_trailing_split_the_work_whatever_the_window(self):
        for window in (None, (2, 2), (3, 99), (4, 5)):
            with self.subTest(window=window):
                result, _ = scan(build_whole, window=window)
                self.assertEqual(result["whole"], WHOLE_RESULT)
                self.assertEqual(result["trailing"], TRAILING_RESULT)

    def test_sync_still_covers_only_the_window_beside_whole(self):
        # Frame 2 alone: no draws, 2 mesh draws, 1 barrier. Whole counts frames 1 to 3 (the window is frame 2).
        result, _ = scan(build_whole, window=(2, 2))
        self.assertEqual(result["sync"]["draws"], summary(1, 0, 0.0, 0, 0, 0, 0))
        self.assertEqual(result["sync"]["mesh_draws"], summary(1, 2, 2.0, 2, 2, 2, 2))
        self.assertEqual(result["sync"]["barriers"], summary(1, 1, 1.0, 1, 1, 1, 1))
        self.assertEqual(result["whole"], WHOLE_RESULT)


def build_textures(c):
    c.pool(GPU_X, [X0])
    c.pool(GPU_Y, [Y0, Y1])
    c.pool(GPU_Z, [Z0])
    c.pool(GPU_U, [U0, U0])
    c.register(1, TEX_X, 7, GPU_X, PAGE)
    c.register(2, TEX_Y, 7, GPU_Y, 2 * PAGE)
    c.register(3, TEX_Z, 42, GPU_Z, PAGE)
    c.register(4, TEX_W, 7, GPU_W, PAGE)          # no pool: its page has no record
    c.submit(HANDLES, [(REC_A, recording(
        op(BEGIN), use_textures(1, 3, 4), op(SET_RT, body(216, (0, "<i", 1), (8, "<Q", TEX_Y))),
        read_range(GPU_U, PAGE), op(END)))])
    c.present(TEX_X)                              # frame 1
    c.pages([(GPU_Z >> 12, Z1), ((GPU_U >> 12) + 1, U1)])   # frame 2: Z's page and U's second page change
    c.submit(HANDLES, [(REC_B, recording(op(BEGIN), use_textures(2, 3), op(END)))])
    c.object(SRC_TEXELS, b"\x05" * 100)
    c.call(34, TEX_Z, 0, 0, 0, SRC_TEXELS, 0, 0)  # writes 100 bytes into Z
    c.present()


TEXTURE_FIRST_FRAME_RESULT = [
    {"format": 7, "first_use_textures": 2, "first_use_bytes": 2 * PAGE,
     "gpu_produced_textures": 1, "gpu_produced_bytes": 2 * PAGE,
     "changed_uses": 0, "changed_bytes": 0, "written_calls": 0, "written_bytes": 0},
    {"format": 42, "first_use_textures": 1, "first_use_bytes": PAGE,
     "gpu_produced_textures": 0, "gpu_produced_bytes": 0,
     "changed_uses": 1, "changed_bytes": PAGE, "written_calls": 1, "written_bytes": 100},
]


class TexturesTest(unittest.TestCase):
    def test_first_use_gpu_made_changed_and_written_textures(self):
        result, _ = scan(build_textures)
        self.assertEqual(result["textures"], TEXTURE_FIRST_FRAME_RESULT)

    def test_a_window_counts_its_frames_only(self):
        result, _ = scan(build_textures, window=(2, 99))
        self.assertEqual(result["window"], {"first": 2, "last": 2, "frames": 1})
        self.assertEqual(result["textures"], [
            {"format": 7, "first_use_textures": 0, "first_use_bytes": 0,
             "gpu_produced_textures": 1, "gpu_produced_bytes": 2 * PAGE,
             "changed_uses": 0, "changed_bytes": 0, "written_calls": 0, "written_bytes": 0},
            {"format": 42, "first_use_textures": 0, "first_use_bytes": 0,
             "gpu_produced_textures": 0, "gpu_produced_bytes": 0,
             "changed_uses": 1, "changed_bytes": PAGE, "written_calls": 1, "written_bytes": 100},
        ])
        self.assertEqual(result["submits"], 1)
        self.assertEqual(result["sync"]["frames"], 1)

    def test_pages_of_a_frame_one_change_read_by_frame_two_are_counted(self):
        result, _ = scan(build_textures)
        self.assertEqual(result["pages"]["changed"], 2)
        self.assertEqual(result["pages"]["changed_read"], 1)
        self.assertEqual(result["pages"]["changed_read_share"], 0.5)


def build_against(c):
    c.pool(GPU_X, [X0])
    c.register(1, TEX_X, 7, GPU_X, PAGE)
    c.submit(HANDLES, [(REC_A, recording(op(BEGIN), use_textures(1), op(END)))])
    c.present()


class TextureRepeatTest(unittest.TestCase):
    def test_textures_first_used_here_are_matched_against_another_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            main = Capture(os.path.join(folder, "main"))
            build_textures(main)
            main.write()
            other = Capture(os.path.join(folder, "other"))
            build_against(other)
            other.write()
            result = analyze(main.folder, against=(other.folder, None))
            self.assertNotIn("texture_repeat", analyze(main.folder))
        # X's storage bytes are the other capture's X; Z's are not; W's page has no record.
        self.assertEqual(result["texture_repeat"], {
            "textures": 3,
            "bytes": 3 * PAGE,
            "repeated_textures": 1,
            "repeated_bytes": PAGE,
            "unknown_textures": 1,
            "unknown_bytes": PAGE,
        })


def build_unbound(c):
    build_textures(c)


class UnboundTest(unittest.TestCase):
    def test_mapped_memory_no_buffer_or_texture_uses(self):
        # Mapped: 6 pages (X 1, Y 2, Z 1, U 2). Buffers: the read of page 0x90. Textures: X, Y, Z (W is not mapped).
        # Unbound: page 0x91. Its PAGES change (5 bytes) is in the window.
        result, _ = scan(build_unbound)
        self.assertEqual(result["unbound"], {
            "upper_bound": True,
            "mapped_bytes": 6 * PAGE,
            "buffer_bytes": PAGE,
            "texture_bytes": 4 * PAGE,
            "unbound_bytes": PAGE,
            "unbound_pages": 1,
            "page_changes": 1,
            "changed_bytes": 5,
            "frames": 2,
        })


def build_identity(c):
    base = 0x7F30_0000_0000
    c.pool(base, [ZERO])
    c.object(PROGRAM, b"\x07" * 48)
    c.submit(HANDLES, [(REC_A, recording(op(BEGIN), op(BIND_PROGRAM, body(16, (0, "<Q", PROGRAM))),
                                         read_range(base, PAGE), op(END)))])
    c.pages([(base >> 12, ONE_BYTE_CHANGED)])
    c.submit(HANDLES, [(REC_A, recording(op(BEGIN), op(BIND_PROGRAM, body(16, (0, "<Q", PROGRAM))),
                                         read_range(base, PAGE), op(END)))])
    c.present()


class IdentityTest(unittest.TestCase):
    def test_the_output_names_no_address_page_blob_or_program(self):
        base = 0x7F30_0000_0000
        with tempfile.TemporaryDirectory() as folder:
            c = Capture(os.path.join(folder, "capture"))
            build_identity(c)
            c.write()
            text = json.dumps(analyze(c.folder))
            names = os.listdir(os.path.join(c.folder, "blobs"))
        for number in (base, base >> 12, PROGRAM, REC_A, HANDLES):
            self.assertNotIn(str(number), text)
            self.assertNotIn(format(number, "x"), text)
        for name in names:
            self.assertNotIn(name, text)


def build_no_complete_frame(c):
    c.pool(GPU_X, [X0])
    c.submit(HANDLES, [(REC_A, PLAIN)])                 # 1 barrier
    c.submit(HANDLES, [(REC_B, recording(op(BEGIN), op(DRAW), op(DRAW), draw_mesh(), op(BARRIER), op(BARRIER),
                                         op(END)))])   # 2 draws, 1 mesh draw, 2 barriers
    c.pages([(GPU_X >> 12, Z1)])


class IncompleteFrameTest(unittest.TestCase):
    def test_with_no_present_all_the_work_is_trailing_and_none_is_whole(self):
        result, records = scan(build_no_complete_frame)
        self.assertEqual(result["window"], {"first": 1, "last": 0, "frames": 0})
        self.assertEqual(result["presents"], 0)
        self.assertEqual(result["submits"], 0)
        self.assertEqual(result["repeat"]["recordings"], 0)
        self.assertEqual(result["pages"]["listed"], 0)
        self.assertEqual(result["sync"]["barriers"], summary(0, 0, None, None, None, None, None))
        self.assertEqual(result["whole"], {"frames": 0, "presents": 0, "draws": 0, "mesh_draws": 0, "barriers": 0})
        self.assertEqual(result["trailing"], {"draws": 2, "mesh_draws": 1, "barriers": 3})
        self.assertEqual(result["textures"], [])
        self.assertEqual(result["cold"], [])
        self.assertEqual(result["records"], records)


def malformed(case, folder):
    """A capture broken in one way: the name of the case says which"""
    c = Capture(folder)
    if case == "handle array too short":
        c.object(HANDLES, struct.pack("<Q", REC_A))
        c.object(REC_A, PLAIN)
        c.call(16, HANDLES, 2)
    elif case == "recording with no object":
        c.object(HANDLES, struct.pack("<Q", REC_A))
        c.call(16, HANDLES, 1)
    elif case == "recording too short":
        c.object(REC_A, b"\x00" * 4)
        c.object(HANDLES, struct.pack("<Q", REC_A))
        c.call(16, HANDLES, 1)
    elif case == "missing blob":
        c.submit(HANDLES, [(REC_A, PLAIN)])
        os.remove(os.path.join(folder, "blobs", hashlib.sha256(PLAIN).hexdigest()))
    elif case == "wrong seq":
        c.submit(HANDLES, [(REC_A, PLAIN)])
        c.present()
        c.write(seqs={3: 9})
        return
    else:
        c.submit(HANDLES, [(REC_A, PLAIN)])
        c.present()
    c.write()
    path = os.path.join(folder, "calls.rxc")
    with open(path, "rb") as f:
        data = bytearray(f.read())
    if case == "bad magic":
        data[0:8] = b"NOTCAPT1"
    elif case == "bad version":
        struct.pack_into("<I", data, 8, 2)
    elif case == "cut short":
        del data[-5:]
    with open(path, "wb") as f:
        f.write(data)


MALFORMED_CASES = ("bad magic", "bad version", "cut short", "wrong seq", "missing blob",
                   "handle array too short", "recording with no object", "recording too short")


class MalformedTest(unittest.TestCase):
    def test_each_malformed_capture_raises_naming_a_record_and_no_address_or_blob(self):
        for case in MALFORMED_CASES:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                capture = os.path.join(folder, "capture")
                malformed(case, capture)
                with self.assertRaises(AnalysisError) as raised:
                    analyze(capture)
                message = str(raised.exception)
                self.assertNotIn(str(REC_A), message)
                self.assertNotIn(hashlib.sha256(PLAIN).hexdigest(), message)

    def test_a_wrong_seq_names_its_record_index_and_kind(self):
        with tempfile.TemporaryDirectory() as folder:
            capture = os.path.join(folder, "capture")
            malformed("wrong seq", capture)
            with self.assertRaisesRegex(AnalysisError, "record 3 \\(kind 16\\)"):
                analyze(capture)


if __name__ == "__main__":
    unittest.main()
