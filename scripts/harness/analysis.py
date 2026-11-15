"""The statistics of one capture folder: the calls.rxc and blobs/ that host/rx_capture.c writes (format 1, host/rx_capture.h).
They say how often a recording repeats, which pages change from frame to frame and how soon the GPU reads them again, what
the GPU waits on and how much it draws (in the complete frames and after the last present), how textures are first used,
and how much mapped memory no buffer or texture uses. Each statistic is counts, bytes and shares; nothing names an
address, a page, a blob or a program.

analyze(capture, window=None, against=None) -> dict. calls.rxc is read as a stream, twice: once for its records and presents
(which frames are complete), once for the statistics; a blob is read only when a statistic needs its bytes. A pointer that
names no object is unknown and adds nothing; a handle array or recording without its object, a record cut short or out of
seq, a blob missing or of the wrong size, and a bad header raise AnalysisError, which names a record index and kind only.

Stdlib only. tests/test_harness_analysis.py builds synthetic captures and checks every statistic against answers worked
out by hand.
"""
import bisect
import hashlib
import os
import struct

FORMAT = 1
MAGIC = b"RXCAPT01"
HEADER = struct.Struct("<8sII")          # magic, format version, the renderer's API version (not read)
RECORD = struct.Struct("<IIQQQ")         # kind, total (header included), seq, thread, queue
PAGE = 4096
ANSWER = 0x10000
TEXTURE_BYTES = 104                      # an nvn_tex's fields end at 104: format +8, storage size +56, storage gpu +96

OBJECT, POOL, PAGES, GUEST = 1, 2, 3, 4
SUBMIT, SUBMIT_Q = 16, 17
CALL_FENCE_WAIT, CALL_WRITEBACK_WAIT, CALL_FINISH = 22, 23, 24
CALL_PRESENT, CALL_REGISTER_TEXTURE = 25, 26
CALL_TEXTURE_RELEASED, CALL_TEXTURE_CREATED = 29, 30
CALL_READ_TEXELS, CALL_WRITE_TEXELS, CALL_INVALIDATE = 33, 34, 35
TEXTURE_CALLS = (CALL_PRESENT, CALL_TEXTURE_RELEASED, CALL_TEXTURE_CREATED, CALL_READ_TEXELS, CALL_WRITE_TEXELS)
SYNC_CALLS = {CALL_FENCE_WAIT: "fence_waits", CALL_WRITEBACK_WAIT: "writeback_waits", CALL_FINISH: "finishes",
              CALL_READ_TEXELS: "read_texels", CALL_INVALIDATE: "invalidates"}
SYNC_NAMES = ("invalidates", "read_texels", "writeback_waits", "fence_waits", "finishes", "barriers", "event_waits",
              "cross_queue_waits", "draws", "mesh_draws")

NOP_END, NOP_SIGNAL_EVENT, NOP_REPORT_COUNTER, NOP_COPY_B2B, NOP_UPDATE_UB = 0, 4, 5, 6, 7
NOP_SET_RT, NOP_BIND_PROGRAM, NOP_BIND_VBUF, NOP_BIND_UBO, NOP_BIND_SSBO = 8, 14, 18, 19, 20
NOP_BIND_TEX, NOP_DRAW, NOP_DRAW_IDX, NOP_BARRIER, NOP_SET_TEX_POOL, NOP_SET_SAMP_POOL = 21, 25, 26, 28, 29, 30
NOP_COPY_B2T, NOP_COPY_T2B, NOP_BIND_IMG, NOP_COPY_T2T, NOP_CLEAR_TEX = 31, 34, 35, 36, 38
NOP_RENDER_COND, NOP_BIND_BUFS, NOP_DRAW_INDIRECT, NOP_DISPATCH_INDIRECT = 42, 44, 45, 46
NOP_DRAW_MESH, NOP_WAIT_EVENT, NOP_CLEAR_BUF, NOP_COUNT = 47, 48, 50, 51
INDEX_BYTES = {0: 1, 1: 2, 2: 4}
TEX_FIELDS = ("first_use_textures", "first_use_bytes", "gpu_produced_textures", "gpu_produced_bytes",
              "changed_uses", "changed_bytes", "written_calls", "written_bytes")


class AnalysisError(Exception):
    """A capture that cannot be read: its header, a record, or a blob it names"""


def analyze(capture, window=None, against=None):
    """The statistics of the capture folder over the frames of window ((first, last), inclusive; None: every complete
    frame). whole counts draws, mesh draws and barriers over every complete frame, whatever the window. trailing counts
    them over the submits after the last present (all of them when there is none), whatever the window. against, when
    given, is (folder, window) of another capture, and texture_repeat compares the textures first used in this window
    with that capture's. Raises AnalysisError."""
    records, presents = _count(capture)
    first, last = _window(window, presents)
    scan = _run(capture, first, last, hashes=against is not None, complete=presents)
    result = {
        "format": FORMAT,
        "window": {"first": first, "last": last, "frames": max(0, last - first + 1)},
        "records": records,
        "presents": presents,
        "submits": scan.submits,
        "whole": {"frames": presents, "presents": presents, **scan.whole},
        "trailing": scan.trailing,
    }
    result.update(scan.statistics())
    if against is not None:
        other_folder, other_window = against
        _, other_presents = _count(other_folder)
        other_first, other_last = _window(other_window, other_presents)
        other = _run(other_folder, other_first, other_last, hashes=True, complete=other_presents)
        result["texture_repeat"] = _texture_repeat(scan.first_uses, other.first_uses)
    return result


def _window(window, presents):
    """The frames a window selects: its first (at least 1) and its last (at most the last complete frame)"""
    if window is None:
        return 1, presents
    first, last = window
    return max(first, 1), min(last, presents)


def _count(folder):
    """The number of records and of presents in calls.rxc (the payloads are skipped)"""
    records = presents = 0
    for _, kind, _ in _records(folder, payloads=False):
        records += 1
        if kind == CALL_PRESENT:
            presents += 1
    return records, presents


def _run(folder, first, last, hashes, complete):
    scan = _Scan(folder, first, last, hashes, complete)
    for index, kind, payload in _records(folder, payloads=True):
        scan.record(index, kind, payload)
    return scan


def _records(folder, payloads):
    """Each record of calls.rxc as (index, kind, payload), index counting from 1; the payload is None unless payloads"""
    path = os.path.join(folder, "calls.rxc")
    try:
        f = open(path, "rb")
    except OSError as e:
        raise AnalysisError("calls.rxc cannot be opened") from e
    with f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(HEADER.size)
        if len(head) != HEADER.size:
            raise AnalysisError("the capture's header is cut short")
        magic, version, _api = HEADER.unpack(head)
        if magic != MAGIC:
            raise AnalysisError("not a capture: bad magic")
        if version != FORMAT:
            raise AnalysisError("unsupported capture format version")
        index = 0
        while True:
            head = f.read(RECORD.size)
            if not head:
                return
            index += 1
            if len(head) != RECORD.size:
                raise AnalysisError("record %d: its header is cut short" % index)
            kind, total, seq, _thread, _queue = RECORD.unpack(head)
            at = (index, kind)
            if total < RECORD.size:
                raise _error(at, "its size is smaller than its header")
            if seq != index:
                raise _error(at, "its seq is out of order")
            body = total - RECORD.size
            if payloads:
                payload = f.read(body)
                if len(payload) != body:
                    raise _error(at, "its payload is cut short")
            else:
                f.seek(body, os.SEEK_CUR)
                if f.tell() > size:
                    raise _error(at, "its payload is cut short")
                payload = None
            yield index, kind, payload


def _error(at, what):
    return AnalysisError("record %d (kind %d): %s" % (at[0], at[1], what))


def _check_size(payload, size, at):
    if len(payload) != size:
        raise _error(at, "has a payload of the wrong size")


def _args(payload):
    return struct.unpack_from("<%dQ" % (len(payload) // 8), payload)


def _arg(args, i, at):
    if i >= len(args):
        raise _error(at, "has fewer arguments than its kind takes")
    return args[i]


def _signed(value, bits):
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def _clamp(value, low, high):
    return min(max(value, low), high)


def _field(rec, off, fmt):
    """The field at payload offset off of a record (whose payload is at +8), or None when it lies past the record's end:
    a field past the end is not read"""
    pos = 8 + off
    if pos + struct.calcsize(fmt) > len(rec):
        return None
    return struct.unpack_from(fmt, rec, pos)[0]


def _u64(rec, off):
    return _field(rec, off, "<Q")


def _s32(rec, off):
    value = _field(rec, off, "<i")
    return 0 if value is None else value


def _ranges(rec, off, count):
    """count (address, size) ranges from payload offset off, 16 bytes each; a range past the record's end is not read"""
    out = []
    for k in range(count):
        addr = _u64(rec, off + 16 * k)
        size = _u64(rec, off + 16 * k + 8)
        if addr is None or size is None:
            break
        out.append((addr, size))
    return out


def _span(addr, size):
    """The bytes [lo, hi) of a range, or None for a zero address or size"""
    if addr and size and size > 0:
        return addr, addr + size
    return None


def _walk(data, at):
    """Each record of a recording, from its start through its END: (offset, op, the record's bytes)"""
    off = 0
    while True:
        if off + 8 > len(data):
            raise _error(at, "has a recording too short for its records")
        op, size = struct.unpack_from("<II", data, off)
        if size < 8 or size & 7 or off + size > len(data) or op >= NOP_COUNT:
            raise _error(at, "has a recording with a malformed record")
        yield off, op, data[off:off + size]
        if op == NOP_END:
            return
        off += size


def _pointer_offsets(op, rec):
    """The payload offsets of a record's pointer fields to objects: the render targets, a program, a pool, a copy's and
    a clear's texture"""
    if op == NOP_SET_RT:
        return [8 + 8 * k for k in range(_clamp(_s32(rec, 0), 0, 8))] + [208]
    if op in (NOP_BIND_PROGRAM, NOP_SET_TEX_POOL, NOP_SET_SAMP_POOL, NOP_CLEAR_TEX):
        return [0]
    if op in (NOP_COPY_B2T, NOP_COPY_T2B):
        return [8]
    if op == NOP_COPY_T2T:
        return [0, 8]
    return []


def _summary(values):
    """count, total, mean, and the nearest-rank p50, p90, p99 and max of values; null beyond the count when there are none"""
    count = len(values)
    if count == 0:
        return {"count": 0, "total": 0, "mean": None, "p50": None, "p90": None, "p99": None, "max": None}
    ordered = sorted(values)
    total = sum(ordered)

    def rank(percent):
        return ordered[-(-percent * count // 100) - 1]
    return {"count": count, "total": total, "mean": total / count, "p50": rank(50), "p90": rank(90),
            "p99": rank(99), "max": ordered[-1]}


def _merge(spans):
    """The union of [lo, hi) spans, sorted and disjoint"""
    out = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _intersect(a, b):
    """The overlap of two sorted, disjoint span lists"""
    out = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if lo < hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _subtract(a, b):
    """a without b: both sorted and disjoint"""
    out = []
    starts = [lo for lo, _ in b]
    for lo, hi in a:
        cur = lo
        k = max(bisect.bisect_right(starts, cur) - 1, 0)
        while cur < hi and k < len(b):
            blo, bhi = b[k]
            if blo >= hi:
                break
            if bhi > cur:
                if blo > cur:
                    out.append((cur, blo))
                cur = bhi
            k += 1
        if cur < hi:
            out.append((cur, hi))
    return out


def _length(spans):
    return sum(hi - lo for lo, hi in spans)


def _texture_repeat(uses, other_uses):
    """The textures first used in this window (size, digest or None when a page has no record) against the other
    capture's: repeated when a digest is among the other's, unknown when the digest is None"""
    known = {digest for _, digest in other_uses if digest is not None}
    repeated = [size for size, digest in uses if digest is not None and digest in known]
    unknown = [size for size, digest in uses if digest is None]
    return {
        "textures": len(uses),
        "bytes": sum(size for size, _ in uses),
        "repeated_textures": len(repeated),
        "repeated_bytes": sum(repeated),
        "unknown_textures": len(unknown),
        "unknown_bytes": sum(unknown),
    }


class _Submit:
    """One submit as its recordings are walked: the ranges it reads and what its records count"""

    def __init__(self, at, queue, in_window):
        self.at = at
        self.queue = queue
        self.in_window = in_window
        self.ranges = []                 # (address, size) of every buffer range read and every texture storage read
        self.barriers = 0
        self.event_waits = 0
        self.cross_waits = 0
        self.draws = 0
        self.mesh_draws = 0


class _Scan:
    """One pass over a capture's records: the state the statistics follow, and what the frames in the window count"""

    def __init__(self, folder, first, last, hashes, complete):
        self.folder = folder
        self.first, self.last = first, last
        self.hashes = hashes
        self.complete = complete         # the frames 1 to complete are complete: the presents
        self.frame = 1
        self.whole = dict.fromkeys(("draws", "mesh_draws", "barriers"), 0)   # over every complete frame
        self.trailing = dict.fromkeys(("draws", "mesh_draws", "barriers"), 0)   # after the last present
        self.objects = {}                # address -> (size, sha256 of its bytes): the latest OBJECT there
        self.pages = {}                  # page -> sha256 of its last record (POOL or PAGES)
        self.page_change = {}            # page -> (record index, frame) of its last change (its first record counts)
        self._sorted_pages = None
        self.mapped = []                 # POOL ranges
        self.buffers = set()             # buffer spans written or read
        self.tex_spans = set()           # (gpu, size, format) of every texture a call or record names
        self.tex_keys = {}               # sha256 of an nvn_tex's bytes -> (gpu, size, format)
        self.regs = {}                   # texture id -> (gpu, size, format) of its latest registration
        self.signals = {}                # GPU address -> queue of the submit that last signalled it
        self.produced = set()            # GPU addresses the GPU has written: render targets, copy and clear targets
        self.tex_last = {}               # (gpu, size, format) -> record index of its last use
        self.tex_stats = {}              # format -> counts of TEX_FIELDS
        self.first_uses = []             # (size, digest or None) of each texture first used in the window, not made by the GPU
        self.submits = 0
        self.repeat = dict.fromkeys(("recordings", "bytes", "exact_recordings", "exact_bytes",
                                     "normalised_recordings", "normalised_bytes"), 0)
        self.seen_exact = set()          # sha256 of a recording's bytes, submitted before
        self.seen_normalised = set()     # sha256 of a recording's normalised bytes, submitted before
        self.page_counts = {"listed": 0, "first": 0, "unchanged": 0, "changed": 0}
        self.changed_bytes = []          # differing bytes of each changed page in the window
        self.changed_read = 0
        self.pending_changed = set()     # pages the PAGES since the last submit listed as changed
        self.pending_window = []         # the same, in the window
        self.pending_pairs = []          # (K, page) of the last submit's reads, resolved by the next submit
        self.cold = {}                   # K -> [reads, changed]
        self.window_changes = {}         # page -> [changed PAGES entries in the window, their differing bytes]
        self.sync_rows = [dict.fromkeys(SYNC_NAMES, 0) for _ in range(max(0, last - first + 1))]

    def record(self, index, kind, payload):
        if kind & ANSWER:
            return
        at = (index, kind)
        in_window = self.first <= self.frame <= self.last
        if kind == OBJECT:
            self._object(at, payload)
        elif kind == POOL:
            self._pool(at, payload)
        elif kind == PAGES:
            self._pages(at, payload, in_window)
        elif kind == GUEST:
            _check_size(payload, 48, at)
            self._need(payload[16:48], at)
        elif kind in (SUBMIT, SUBMIT_Q):
            self._submit(at, _args(payload), in_window)
        else:
            self._call(at, _args(payload), in_window)
        if kind == CALL_PRESENT:
            self.frame += 1

    def _path(self, sha):
        return os.path.join(self.folder, "blobs", sha.hex())

    def _need(self, sha, at):
        if not os.path.isfile(self._path(sha)):
            raise _error(at, "names a blob that is missing")

    def _blob(self, sha, size, at):
        """A blob's bytes, which must be size long: its record's size, or a page"""
        try:
            with open(self._path(sha), "rb") as f:
                data = f.read()
        except OSError:
            raise _error(at, "names a blob that is missing") from None
        if len(data) != size:
            raise _error(at, "names a blob of the wrong size")
        return data

    def _object_at(self, addr, at):
        if addr not in self.objects:
            raise _error(at, "names no object where it needs one")
        return self.objects[addr]

    def _object(self, at, payload):
        _check_size(payload, 48, at)
        addr, size = struct.unpack_from("<QQ", payload)
        sha = payload[16:48]
        self._need(sha, at)
        self.objects[addr] = (size, sha)

    def _set_page(self, page, sha, index):
        """A page's last record becomes sha. Returns the previous sha (None for a first record) and whether it changed"""
        prev = self.pages.get(page)
        if prev == sha:
            return prev, False
        if prev is None:
            self._sorted_pages = None
        self.pages[page] = sha
        self.page_change[page] = (index, self.frame)
        return prev, True

    def _pool(self, at, payload):
        if len(payload) < 16:
            raise _error(at, "has a payload of the wrong size")
        start, size = struct.unpack_from("<QQ", payload)
        first = start >> 12
        count = ((start + size - 1) >> 12) - first + 1 if size else 0
        _check_size(payload, 16 + 32 * count, at)
        if size:
            self.mapped.append((start, start + size))
        for k in range(count):
            sha = payload[16 + 32 * k:48 + 32 * k]
            self._need(sha, at)
            self._set_page(first + k, sha, at[0])

    def _pages(self, at, payload, in_window):
        if len(payload) < 8:
            raise _error(at, "has a payload of the wrong size")
        (count,) = struct.unpack_from("<Q", payload)
        _check_size(payload, 8 + 40 * count, at)
        for k in range(count):
            (page,) = struct.unpack_from("<Q", payload, 8 + 40 * k)
            sha = payload[16 + 40 * k:48 + 40 * k]
            self._need(sha, at)
            prev, changed = self._set_page(page, sha, at[0])
            status = "first" if prev is None else "changed" if changed else "unchanged"
            if in_window:
                self.page_counts["listed"] += 1
                self.page_counts[status] += 1
            if status == "changed":
                self.pending_changed.add(page)
                if in_window:
                    self.pending_window.append(page)
                    diff = self._differing(prev, sha, at)
                    self.changed_bytes.append(diff)
                    entry = self.window_changes.setdefault(page, [0, 0])
                    entry[0] += 1
                    entry[1] += diff

    def _differing(self, prev, sha, at):
        """The bytes that differ between two page records' contents"""
        a = int.from_bytes(self._blob(prev, PAGE, at), "little")
        b = int.from_bytes(self._blob(sha, PAGE, at), "little")
        diff = (a ^ b).to_bytes(PAGE, "little")
        return PAGE - diff.count(0)

    def _known_pages(self, first, last):
        """The pages with a record in [first, last], ascending"""
        if last - first + 1 <= len(self.pages):
            return [p for p in range(first, last + 1) if p in self.pages]
        if self._sorted_pages is None:
            self._sorted_pages = sorted(self.pages)
        lo = bisect.bisect_left(self._sorted_pages, first)
        hi = bisect.bisect_right(self._sorted_pages, last)
        return self._sorted_pages[lo:hi]

    def _pages_read(self, ranges):
        pages = set()
        for addr, size in ranges:
            pages.update(self._known_pages(addr >> 12, (addr + size - 1) >> 12))
        return pages

    def _tex_ref(self, addr, at):
        """The texture an nvn_tex pointer names, (gpu, size, format), or None: no pointer, or no object there"""
        if not addr or addr not in self.objects:
            return None
        size, sha = self.objects[addr]
        key = self.tex_keys.get(sha)
        if key is None:
            if size < TEXTURE_BYTES:
                raise _error(at, "names a texture too short for its fields")
            data = self._blob(sha, size, at)
            (fmt,) = struct.unpack_from("<i", data, 8)
            (storage_size,) = struct.unpack_from("<Q", data, 56)
            (gpu,) = struct.unpack_from("<Q", data, 96)
            key = (gpu, storage_size, fmt)
            self.tex_keys[sha] = key
        self.tex_spans.add(key)
        return key

    def _tex_stat(self, fmt):
        if fmt not in self.tex_stats:
            self.tex_stats[fmt] = dict.fromkeys(TEX_FIELDS, 0)
        return self.tex_stats[fmt]

    def _storage_digest(self, key, at):
        """The sha256 of a texture's storage bytes, from its pages' records; None when one of its pages has no record"""
        gpu, size, _ = key
        digest = hashlib.sha256()
        if size == 0:
            return digest.digest()
        first, last = gpu >> 12, (gpu + size - 1) >> 12
        if any(page not in self.pages for page in range(first, last + 1)):
            return None
        for page in range(first, last + 1):
            data = self._blob(self.pages[page], PAGE, at)
            lo = max(gpu, page * PAGE) - page * PAGE
            hi = min(gpu + size, (page + 1) * PAGE) - page * PAGE
            digest.update(data[lo:hi])
        return digest.digest()

    def _changed_overlap(self, key, since):
        """The bytes of a texture's storage on the pages changed after record since (0 when none)"""
        gpu, size, _ = key
        total = 0
        for page in self._known_pages(gpu >> 12, (gpu + size - 1) >> 12):
            change = self.page_change.get(page)
            if change and change[0] > since:
                total += min(gpu + size, (page + 1) * PAGE) - max(gpu, page * PAGE)
        return total

    def _storage(self, sub, key):
        """A texture's storage, read by the submit: its pages are read"""
        if key[0] and key[1]:
            sub.ranges.append((key[0], key[1]))

    def _use(self, sub, key):
        """A texture sampled or copied from: its storage is read, and its first use or its changed uses are counted"""
        self._storage(sub, key)
        gpu, size, fmt = key
        prev = self.tex_last.get(key)
        self.tex_last[key] = sub.at[0]
        if not sub.in_window:
            return
        if prev is not None:
            overlap = self._changed_overlap(key, prev)
            if overlap:
                stat = self._tex_stat(fmt)
                stat["changed_uses"] += 1
                stat["changed_bytes"] += overlap
        elif gpu in self.produced:
            stat = self._tex_stat(fmt)
            stat["gpu_produced_textures"] += 1
            stat["gpu_produced_bytes"] += size
        else:
            stat = self._tex_stat(fmt)
            stat["first_use_textures"] += 1
            stat["first_use_bytes"] += size
            self.first_uses.append((size, self._storage_digest(key, sub.at) if self.hashes else None))

    def _render(self, sub, addr):
        """A render target: the GPU writes it, and the submit reads its storage"""
        key = self._tex_ref(addr, sub.at)
        if key is None:
            return
        self._storage(sub, key)
        self.produced.add(key[0])

    def _produce(self, key):
        if key is not None:
            self.produced.add(key[0])

    def _write(self, addr, size):
        span = _span(addr, size)
        if span:
            self.buffers.add(span)

    def _read_range(self, sub, addr, size):
        span = _span(addr, size)
        if span:
            self.buffers.add(span)
            sub.ranges.append((addr, size))

    def _indirect(self, sub, rec, indirect_off, count_off):
        mx, stride = _s32(rec, 12), _s32(rec, 16)
        if mx > 0 and stride > 0:
            self._read_range(sub, _u64(rec, indirect_off), mx * stride)
        self._read_range(sub, _u64(rec, count_off), 4)

    def _op(self, sub, op, rec):
        at = sub.at
        if op in (NOP_DRAW, NOP_DRAW_IDX, NOP_DRAW_INDIRECT):
            sub.draws += 1
        elif op == NOP_DRAW_MESH:
            sub.mesh_draws += 1
        if op == NOP_SIGNAL_EVENT:
            gpu = _u64(rec, 0)
            self._write(gpu, 4)
            if gpu:
                self.signals[gpu] = sub.queue
        elif op == NOP_REPORT_COUNTER:
            self._write(_u64(rec, 8), 16)
        elif op == NOP_COPY_B2B:
            self._read_range(sub, _u64(rec, 0), _u64(rec, 16))
            self._write(_u64(rec, 8), _u64(rec, 16))
        elif op in (NOP_UPDATE_UB, NOP_CLEAR_BUF):
            self._write(_u64(rec, 0), _u64(rec, 8))
        elif op == NOP_BIND_VBUF:
            for addr, size in _ranges(rec, 8, _clamp(_s32(rec, 4), 0, 16)):
                self._read_range(sub, addr, size)
        elif op in (NOP_BIND_UBO, NOP_BIND_SSBO):
            self._read_range(sub, _u64(rec, 8), _u64(rec, 16))
        elif op in (NOP_BIND_TEX, NOP_BIND_IMG):
            for k in range(_clamp(_s32(rec, 8), 0, 32)):
                handle = _u64(rec, 24 + 8 * k)
                if handle is None:
                    break
                key = self.regs.get(handle & 0xFFFFF)
                if key is not None:
                    self._use(sub, key)
        elif op == NOP_DRAW_IDX:
            size = INDEX_BYTES.get(_s32(rec, 4))
            count = _s32(rec, 8)
            if size and count > 0:
                self._read_range(sub, _u64(rec, 24), count * size)
        elif op == NOP_SET_RT:
            for p in _pointer_offsets(op, rec):
                self._render(sub, _u64(rec, p))
        elif op == NOP_COPY_B2T:
            self._produce(self._tex_ref(_u64(rec, 8), at))
        elif op == NOP_CLEAR_TEX:
            self._produce(self._tex_ref(_u64(rec, 0), at))
        elif op == NOP_COPY_T2B:
            key = self._tex_ref(_u64(rec, 8), at)
            if key is not None:
                self._use(sub, key)
        elif op == NOP_COPY_T2T:
            src = self._tex_ref(_u64(rec, 0), at)
            if src is not None:
                self._use(sub, src)
            self._produce(self._tex_ref(_u64(rec, 8), at))
        elif op == NOP_RENDER_COND:
            self._read_range(sub, _u64(rec, 8), 32)
        elif op == NOP_BIND_BUFS:
            for addr, size in _ranges(rec, 24, _clamp(_s32(rec, 12), 0, 16)):
                self._read_range(sub, addr, size)
        elif op == NOP_DRAW_INDIRECT:
            self._indirect(sub, rec, 32, 40)
        elif op == NOP_DRAW_MESH:
            self._indirect(sub, rec, 24, 32)
        elif op == NOP_DISPATCH_INDIRECT:
            self._read_range(sub, _u64(rec, 0), 12)
        elif op == NOP_WAIT_EVENT:
            gpu = _u64(rec, 0)
            self._read_range(sub, gpu, 4)
            sub.event_waits += 1
            if gpu and self.signals.get(gpu, sub.queue) != sub.queue:
                sub.cross_waits += 1
        elif op == NOP_BARRIER:
            sub.barriers += 1

    def _submit(self, at, args, in_window):
        handles_addr = _arg(args, 0, at)
        count = _signed(_arg(args, 1, at), 32)
        queue = _arg(args, 2, at) if at[1] == SUBMIT_Q else 0
        handles = ()
        if count > 0:
            size, sha = self._object_at(handles_addr, at)
            if size < 8 * count:
                raise _error(at, "has a handle array too short for its count")
            handles = struct.unpack_from("<%dQ" % count, self._blob(sha, size, at))
        sub = _Submit(at, queue, in_window)
        recordings = []
        for handle in handles:
            size, sha = self._object_at(handle, at)
            data = self._blob(sha, size, at)
            normalised = bytearray(data)
            for off, op, rec in _walk(data, at):
                for p in _pointer_offsets(op, rec):
                    target = _u64(rec, p)
                    if target and target in self.objects:
                        pos = off + 8 + p
                        normalised[pos:pos + 8] = self.objects[target][1][:8]
                self._op(sub, op, rec)
            recordings.append((size, sha, hashlib.sha256(normalised).digest()))

        if in_window:
            self.submits += 1
            row = self.sync_rows[self.frame - self.first]
            row["barriers"] += sub.barriers
            row["event_waits"] += sub.event_waits
            row["cross_queue_waits"] += sub.cross_waits
            row["draws"] += sub.draws
            row["mesh_draws"] += sub.mesh_draws
            for size, sha, normalised in recordings:
                self.repeat["recordings"] += 1
                self.repeat["bytes"] += size
                if sha in self.seen_exact:
                    self.repeat["exact_recordings"] += 1
                    self.repeat["exact_bytes"] += size
                if normalised in self.seen_normalised:
                    self.repeat["normalised_recordings"] += 1
                    self.repeat["normalised_bytes"] += size
        totals = self.whole if self.frame <= self.complete else self.trailing
        totals["draws"] += sub.draws
        totals["mesh_draws"] += sub.mesh_draws
        totals["barriers"] += sub.barriers
        for _, sha, normalised in recordings:
            self.seen_exact.add(sha)
            self.seen_normalised.add(normalised)

        read = self._pages_read(sub.ranges)
        self.changed_read += sum(1 for page in self.pending_window if page in read)
        for k, page in self.pending_pairs:
            entry = self.cold.setdefault(k, [0, 0])
            entry[0] += 1
            if page in self.pending_changed:
                entry[1] += 1
        self.pending_pairs = ([(self.frame - self.page_change[page][1], page) for page in sorted(read)]
                              if in_window else [])
        self.pending_changed = set()
        self.pending_window = []

    def _call(self, at, args, in_window):
        kind = at[1]
        if kind in SYNC_CALLS and in_window:
            self.sync_rows[self.frame - self.first][SYNC_CALLS[kind]] += 1
        if kind == CALL_REGISTER_TEXTURE:
            key = self._tex_ref(_arg(args, 1, at), at)
            self.regs[_arg(args, 0, at) & 0xFFFFF] = key
        elif kind in TEXTURE_CALLS:
            key = self._tex_ref(_arg(args, 0, at), at)
            if kind == CALL_WRITE_TEXELS and key is not None and in_window:
                src = _arg(args, 4, at)
                size = self.objects[src][0] if src in self.objects else 0
                stat = self._tex_stat(key[2])
                stat["written_calls"] += 1
                stat["written_bytes"] += size

    def statistics(self):
        return {
            "repeat": {
                "recordings": self.repeat["recordings"],
                "bytes": self.repeat["bytes"],
                "exact": {"recordings": self.repeat["exact_recordings"], "bytes": self.repeat["exact_bytes"]},
                "normalised": {"recordings": self.repeat["normalised_recordings"],
                               "bytes": self.repeat["normalised_bytes"]},
            },
            "pages": self._pages_statistics(),
            "cold": [{"presents_since_change": k, "reads": reads, "changed": changed}
                     for k, (reads, changed) in sorted(self.cold.items())],
            "sync": {"frames": len(self.sync_rows),
                     **{name: _summary([row[name] for row in self.sync_rows]) for name in SYNC_NAMES}},
            "textures": [{"format": fmt, **self.tex_stats[fmt]} for fmt in sorted(self.tex_stats)],
            "unbound": self._unbound(),
        }

    def _pages_statistics(self):
        changed = self.page_counts["changed"]
        return {
            "listed": self.page_counts["listed"],
            "first": self.page_counts["first"],
            "unchanged": self.page_counts["unchanged"],
            "changed": changed,
            "changed_bytes": _summary(self.changed_bytes),
            "changed_read": self.changed_read,
            "changed_read_share": self.changed_read / changed if changed else None,
        }

    def _unbound(self):
        mapped = _merge(self.mapped)
        buffers = _merge(self.buffers)
        textures = _merge([(gpu, gpu + size) for gpu, size, _ in self.tex_spans if gpu and size])
        both = _merge(buffers + textures)
        in_both = _intersect(mapped, both)
        unbound = _subtract(mapped, both)
        pages = _merge([(lo >> 12, ((hi - 1) >> 12) + 1) for lo, hi in unbound])
        starts = [lo for lo, _ in pages]
        page_changes = changed_bytes = 0
        for page, (count, diff) in self.window_changes.items():
            k = bisect.bisect_right(starts, page) - 1
            if k >= 0 and page < pages[k][1]:
                page_changes += count
                changed_bytes += diff
        buffer_bytes = _length(_intersect(mapped, buffers))
        return {
            "upper_bound": True,
            "mapped_bytes": _length(mapped),
            "buffer_bytes": buffer_bytes,
            "texture_bytes": _length(in_both) - buffer_bytes,
            "unbound_bytes": _length(unbound),
            "unbound_pages": sum(hi - lo for lo, hi in pages),
            "page_changes": page_changes,
            "changed_bytes": changed_bytes,
            "frames": len(self.sync_rows),
        }
