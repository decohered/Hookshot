"""The bench gate: whether a bench is void. A floor's two baseline correctness runs measure the image tolerance and
the determinism label (tolerance); a bench's screenshots are compared against that tolerance (images_beyond); and its
logs are checked for flash lines, for validation errors new in the candidate, and for hang lines (void_reasons). An
image difference is FLIP's mean when a flip executable is given, else the mean absolute RGB channel difference over
255, measured on the screenshots' own pixels. No margin is a constant here: the tolerance is what the baseline's own
two runs differ by.

Stdlib only. The commands import this module as a sibling; tests/test_harness_gate.py checks it.
"""
import operator
import re
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

SIGNATURE = b"\x89PNG\r\n\x1a\n"
FLASH = re.compile(r"vk: (?:region )?flash at present ")
HANG = re.compile(r"vk: (?:the GPU has finished nothing for |GPU hang: |FATAL: )")
VALIDATION_ERROR = re.compile(r"Validation Error: \[ ([^\s\]]+) \]")
FLIP_MEAN = re.compile(r"^\s*Mean:\s*([0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)\s*$", re.MULTILINE)


class GateError(Exception):
    """Every refusal: a PNG that cannot be read, a difference that cannot be measured, runs whose shots do not match"""


def _file_bytes(path):
    try:
        return Path(path).read_bytes()
    except OSError as e:
        raise GateError(f"cannot read {path}: {e}") from e


def _chunks(data, path):
    """(type, body) for each chunk up to and including IEND, each checksum verified; a short or corrupt file is
    refused"""
    if data[:len(SIGNATURE)] != SIGNATURE:
        raise GateError(f"{path}: not a PNG")
    chunks = []
    pos = len(SIGNATURE)
    while True:
        if pos + 12 > len(data):
            raise GateError(f"{path}: ends before its IEND chunk")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        end = pos + 12 + length
        if end > len(data):
            raise GateError(f"{path}: the chunk at byte {pos} runs past the end of the file")
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:end - 4]
        (crc,) = struct.unpack(">I", data[end - 4:end])
        if zlib.crc32(kind + body) != crc:
            raise GateError(f"{path}: its {kind!r} chunk fails its checksum")
        chunks.append((kind, body))
        pos = end
        if kind == b"IEND":
            return chunks


def _header(chunks, path):
    """(width, height, bit depth, colour type, compression, filter method, interlace) from the IHDR chunk, the first
    chunk"""
    if chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        raise GateError(f"{path}: its first chunk is not IHDR")
    width, height, depth, color, compression, method, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    if not width or not height:
        raise GateError(f"{path}: an image with no pixels, {width}x{height}")
    return width, height, depth, color, compression, method, interlace


def _size(path):
    """The width and height of a PNG, read from its header: the sizes a difference needs, without decoding the pixels"""
    width, height = _header(_chunks(_file_bytes(path), path), path)[:2]
    return width, height


def _paeth(left, up, corner):
    estimate = left + up - corner
    to_left, to_up, to_corner = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
    if to_left <= to_up and to_left <= to_corner:
        return left
    if to_up <= to_corner:
        return up
    return corner


def _unfilter(raw, stride, height, bpp, path):
    """The pixel bytes of the scanlines in raw, each row's filter undone from the row above it"""
    out = bytearray()
    prior = bytes(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = raw[start + 1:start + 1 + stride]
        row = bytearray(line)
        if kind == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif kind == 2:
            row = bytearray((x + p) & 0xFF for x, p in zip(line, prior))
        elif kind == 3:
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prior[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left, corner = (row[i - bpp], prior[i - bpp]) if i >= bpp else (0, 0)
                row[i] = (row[i] + _paeth(left, prior[i], corner)) & 0xFF
        elif kind != 0:
            raise GateError(f"{path}: row {y} has filter type {kind}, not 0 to 4")
        out += row
        prior = row
    return out


def read_png(path):
    """(width, height, RGB bytes) of an 8-bit, non-interlaced RGB or RGBA PNG, its alpha dropped and every row filter
    undone; any other PNG, and a corrupt one, is a GateError"""
    chunks = _chunks(_file_bytes(path), path)
    width, height, depth, color, compression, method, interlace = _header(chunks, path)
    if depth != 8 or color not in (2, 6):
        raise GateError(f"{path}: needs an 8-bit RGB or RGBA PNG, not bit depth {depth} and colour type {color}")
    if compression or method or interlace:
        raise GateError(f"{path}: needs a non-interlaced PNG with the standard compression and filter methods")
    bpp = 3 if color == 2 else 4
    stride = width * bpp
    try:
        raw = zlib.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"))
    except zlib.error as e:
        raise GateError(f"{path}: its image data does not inflate: {e}") from e
    if len(raw) != height * (stride + 1):
        raise GateError(f"{path}: its image data holds {len(raw)} bytes, not {height * (stride + 1)}")
    pixels = _unfilter(raw, stride, height, bpp, path)
    if bpp == 3:
        return width, height, bytes(pixels)
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = pixels[0::4], pixels[1::4], pixels[2::4]
    return width, height, bytes(rgb)


def _same_bytes(a, b):
    return _file_bytes(a) == _file_bytes(b)


def _metric(flip):
    return "pixel" if flip is None else "flip"


def _pixel_mean(a, b):
    """The mean absolute difference of two equal-length RGB byte strings, over 255"""
    return sum(map(abs, map(operator.sub, a, b))) / (len(a) * 255)


def _copy_png(source, target):
    """Copy source's bytes to target: FLIP chooses its image loader by extension, so each input reaches it as a .png"""
    data = _file_bytes(source)
    try:
        target.write_bytes(data)
    except OSError as e:
        raise GateError(f"cannot copy {source} to {target}: {e}") from e


def _flip_mean(flip, reference, test):
    """FLIP's mean of two screenshots. The screenshots are copied to a scratch folder under .png names, since FLIP
    refuses a name without one; the copies are removed when FLIP has run."""
    with tempfile.TemporaryDirectory() as scratch:
        reference_png = Path(scratch) / "reference.png"
        test_png = Path(scratch) / "test.png"
        _copy_png(reference, reference_png)
        _copy_png(test, test_png)
        argv = [str(flip), "-r", str(reference_png), "-t", str(test_png), "-nerm"]
        try:
            done = subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace")
        except OSError as e:
            raise GateError(f"cannot run {flip}: {e}") from e
        if done.returncode:
            printed = "\n".join(text for text in (done.stdout.strip(), done.stderr.strip()) if text)
            raise GateError(f"{flip} exited {done.returncode}: {printed}")
        means = FLIP_MEAN.findall(done.stdout)
        if len(means) != 1:
            raise GateError(f"{flip} printed {len(means)} Mean lines, not one")
        return float(means[0])


def difference(reference, test, flip=None):
    """(metric, difference) of two screenshots: FLIP's mean when flip names its executable, else the mean absolute
    RGB channel difference over 255. Byte-identical files are 0.0 without being decoded, or FLIP run."""
    metric = _metric(flip)
    if _same_bytes(reference, test):
        return metric, 0.0
    if flip is None:
        width, height, reference_rgb = read_png(reference)
        test_width, test_height, test_rgb = read_png(test)
        if (width, height) != (test_width, test_height):
            raise GateError(f"{reference} is {width}x{height} and {test} is {test_width}x{test_height}: "
                            "different sizes")
        return metric, _pixel_mean(reference_rgb, test_rgb)
    reference_size, test_size = _size(reference), _size(test)
    if reference_size != test_size:
        raise GateError(f"{reference} is {reference_size} and {test} is {test_size}: different sizes")
    return metric, _flip_mean(flip, reference, test)


def tolerance(first, second, flip=None):
    """The image tolerance and determinism label of a floor's two baseline correctness runs. first and second map
    each shot's name to its PNG. The tolerance is the largest difference between the two runs' same shot; the label
    is deterministic when every pair is byte-identical."""
    if first.keys() != second.keys():
        names = ", ".join(sorted(first.keys() ^ second.keys()))
        raise GateError(f"the two baseline runs have different shots: {names}")
    shots = {}
    deterministic = True
    for name in sorted(first):
        if not _same_bytes(first[name], second[name]):
            deterministic = False
        shots[name] = difference(first[name], second[name], flip)[1]
    return {
        "metric": _metric(flip),
        "tolerance": max(shots.values(), default=0.0),
        "determinism": "deterministic" if deterministic else "nondeterministic",
        "shots": shots,
    }


def images_beyond(tol, reference, test, flip=None):
    """The sorted names of the shots whose difference, measured as the tolerance was, is strictly more than the
    tolerance, and every name present on one side only"""
    if tol["metric"] != _metric(flip):
        raise GateError(f"the tolerance is a {tol['metric']} measure, but these images are measured by "
                        f"{_metric(flip)}")
    beyond = set(reference) ^ set(test)
    for name in set(reference) & set(test):
        if difference(reference[name], test[name], flip)[1] > tol["tolerance"]:
            beyond.add(name)
    return sorted(beyond)


def flashes(log):
    """The lines of a log that report a flash on the screen"""
    return [line for line in log.splitlines() if FLASH.search(line)]


def hangs(log):
    """The lines of a log that report a GPU hang, or a fatal error in the renderer"""
    return [line for line in log.splitlines() if HANG.search(line)]


def validation_errors(log):
    """The message ids of the validation errors in a log; a validation warning is not one"""
    return set(VALIDATION_ERROR.findall(log))


def void_reasons(tol, a_shots, b_shots, a_log, b_log, timed_logs, flip=None):
    """Why a bench is void, one reason per cause, and none when it stands. The causes: a shot of b past the tolerance
    from a; a flash line in either correctness log; a validation error in b's log that a's log lacks; a hang line in
    any log, correctness or timed."""
    reasons = []
    beyond = images_beyond(tol, a_shots, b_shots, flip)
    if beyond:
        reasons.append(f"screenshots beyond the tolerance {tol['tolerance']!r} ({tol['metric']}): "
                       f"{', '.join(beyond)}")
    correctness = [("correctness log a", a_log), ("correctness log b", b_log)]
    for label, log in correctness:
        found = flashes(log)
        if found:
            reasons.append(f"flash in {label}: {len(found)} line(s), first: {found[0]}")
    for error in sorted(validation_errors(b_log) - validation_errors(a_log)):
        reasons.append(f"validation error {error} in correctness log b, not in correctness log a")
    timed = [(f"timed log {n}", log) for n, log in enumerate(timed_logs, 1)]
    for label, log in correctness + timed:
        found = hangs(log)
        if found:
            reasons.append(f"hang in {label}: {len(found)} line(s), first: {found[0]}")
    return reasons
