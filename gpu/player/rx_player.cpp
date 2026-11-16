/* The player of a capture (the format is in host/rx_capture.h): rx_play replays the boundary calls of
   <dir>/calls.rxc into the renderer table mkwvk_init returns, with no game. The renderer is given the player's host
   table, which answers from the capture; the renderer is then started (with rx_play's window; the executable gives
   none, so the renderer is headless and its presents render offscreen), the boundary calls are replayed in the order
   the capture recorded them, and the renderer's nvn_finish ends the play.

   What the player answers from the capture:
   - guest memory is a window of the size the capture's guest_size answered (the first one), holding each POOL's and
     PAGES' pages at their guest addresses, and each GUEST's bytes at its address (the guest bytes the renderer reads by
     address that the pools may not hold, written before the call that reads them). gpu_ptr, gpu_guest, gpu_read and
     gpu_write answer from the window for the addresses in it, gpu_ptr answers NULL with 0 bytes outside it, and
     guest_base and guest_size are the window. collect_written and collect_written_in list the pages the player wrote
     since the renderer's last collection: the POOL and PAGES it replayed, and the gpu_write and mark_written calls inside
     the window (a GUEST is not listed). gpu_pools lists the POOL records replayed so far.
   - the window is made as the game makes its window (make_window): a memfd's shared mapping where the game uses one,
     which hos_posix_guest_memfd returns for the renderer's dma-buf imports, otherwise anonymous memory without
     transparent huge pages. The switch is the game's, HOS_GUEST_MEMFD.
   - the objects a boundary call points at: each pointer argument, and the pointer fields of the objects it reaches (a
     recording's records, nvn_tex.pool, nvn_dpool.pool), are the player's own copy of the recorded object. There is one
     copy per recorded address. An OBJECT for an address whose copy exists makes that copy stale; before the next
     boundary call is replayed, every OBJECT, POOL, PAGES and GUEST record before it is applied, and then each stale
     copy is rewritten from its latest bytes, pointer fields included. The recorder writes a call's objects container-first, so
     a copy's pointees can arrive after it. A copy the renderer kept therefore shows the latest recorded bytes. A copy
     whose recorded size changes gets new storage, and the old storage stays alive for the play.
   - the host's texture-layout questions (tex_layout, tex_level_offset, fmt_bpb, tex_level_tiles, tex_linear_stride and
     depth_minus_w) are functions of their inputs: each is answered from a recorded answer to the same question (the
     same function, value arguments and input bytes, compared by SHA-256) anywhere in the capture: the latest one
     recorded before the boundary call being replayed, else the earliest one recorded after it.
   - gpu_guest outside the window answers the recorded answer for that exact address when there is one (the latest
     before the boundary call being replayed, as for the other questions below); else, when the capture recorded
     gpu_guest for the address's 4 KB page (asked at the page's base) with a guest address in the window, anywhere
     in the capture, that answer plus the address's offset in the page; else 0, counted unanswered. gpu_read and
     gpu_write of a range outside the window whose every 4 KB page translates so into the window (each page as
     gpu_guest translates it) read and write the window there, a write marking the pages as an in-window write
     does, and are not counted unanswered; any other range is answered as the other questions are.
   - every other host question: the latest answer the capture recorded before the boundary call being replayed,
     for the same function, the same value arguments and the same input bytes (compared by SHA-256). A recorded
     gpu_ptr names the game's memory, which the capture does not hold, so the player does not return it.
   - a question with an answer fills its output buffers from the answer's output objects. With none recorded the result
     is zero or NULL and the output buffers are zeroed.
   - a capture that holds a SNAPSHOT (RXC_SNAPSHOT: the start of an armed capture, host/rx_capture.h) replays the calls
     the snapshot rebuilds like any other boundary calls. Each nvn_wait's fence is then the fence the renderer gave: the
     value the play's renderer returned for the latest replayed nvn_fence or nvn_fence_q whose recorded answer was that
     fence, or 0 when no replayed call's recorded answer was (a fence handed out before the start). A capture with no
     SNAPSHOT replays each nvn_wait with its recorded fence.
   At the play's end the player writes one line to stderr naming every host question kind, with how many questions of it
   found no recorded answer (a line for a play that ran to its end; a refused capture plays nothing and gets none).
   A play that ran to its end also writes one line naming the capture's bytes on disk (calls.rxc and each blob) and
   its load time: the time from the player's start to the renderer's start and the state it is given. After that, one
   more line names where the load went: "rx_player: load phases: read R ms, index I ms, renderer S ms, snapshot P ms".
   R is the reading of calls.rxc, I the rest of the load before the renderer is given anything (the parsing, the checks,
   the answers' index, the records applied before the renderer starts), S the renderer's init and start, and P the
   time from the renderer's start to the end of replaying the capture's SNAPSHOT record and everything before it (0 for
   a capture with no SNAPSHOT). R + I + S is the load line's time. A refused or failed play writes neither line.
   Before the renderer is given anything, the capture is checked to parse, to be of this version, to name no page or
   GUEST past the guest memory, and to hold at most one SNAPSHOT, of 8 bytes, whose first is at least 1 and below
   its own seq; a capture that fails stops with one line on stderr. A blob is read when the play uses its bytes (an
   OBJECT's when a call or a stale copy first reaches it, its bytes then kept until a later OBJECT names another blob
   for the address; a POOL's, PAGES' or GUEST's when its record is applied, a POOL's or PAGES' blobs by up to
   hardware_concurrency() threads, a batch at a time, which also write the batch's pages before its lowest failing
   one; a read's or question's output when it is asked), and the first read of each blob checks its size and its
   SHA-256 against its name: a blob that is missing or does not match stops the play at that read, after the calls
   before it, with one line on stderr naming it (of a POOL's or PAGES' blobs, the lowest page's that fails). */
#include "rx_player.h"

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <iterator>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <system_error>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

#include "../../host/rx_capture.h"
#include "../render/rx_nvn.h"

namespace {

using Hash = std::array<uint8_t, 32>;

constexpr size_t kNone = static_cast<size_t>(-1);
constexpr size_t kRecordHeader = 32;
constexpr uint64_t kPageBytes = RXC_PAGE;
constexpr uint64_t kBatchPages = 4096;   /* a POOL's or PAGES' pages whose blobs are fetched at once (place_pages) */

/* ---- bytes: the file's integers are little-endian ---- */

uint64_t rd64(const uint8_t *p) {
    uint64_t v = 0;
    for (int i = 7; i >= 0; i--) v = v << 8 | p[i];
    return v;
}

uint32_t rd32(const uint8_t *p) {
    return static_cast<uint32_t>(p[0]) | static_cast<uint32_t>(p[1]) << 8 | static_cast<uint32_t>(p[2]) << 16 |
           static_cast<uint32_t>(p[3]) << 24;
}

Hash hash_at(const uint8_t *p) {
    Hash h;
    memcpy(h.data(), p, h.size());
    return h;
}

uint64_t addr_of(const void *p) { return static_cast<uint64_t>(reinterpret_cast<uintptr_t>(p)); }

uint8_t *bytes_at(uint64_t addr) { return reinterpret_cast<uint8_t *>(static_cast<uintptr_t>(addr)); }

/* an int argument as the recorder stores it: sign-extended */
uint64_t sx(int v) { return static_cast<uint64_t>(static_cast<int64_t>(v)); }

float float_of(uint64_t bits) {
    const uint32_t b = static_cast<uint32_t>(bits);
    float f;
    memcpy(&f, &b, sizeof f);
    return f;
}

std::string hex_of(const uint8_t *p, size_t n) {
    static const char digits[] = "0123456789abcdef";
    std::string s;
    for (size_t i = 0; i < n; i++) {
        s += digits[p[i] >> 4];
        s += digits[p[i] & 15];
    }
    return s;
}

/* ---- SHA-256 (FIPS 180-4), of whole buffers: the recorder's (host/rx_capture.c keeps its own copy, static) ---- */
const uint32_t kK256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

uint32_t rotr32(uint32_t x, unsigned n) { return (x >> n) | (x << (32 - n)); }

void sha256_block(uint32_t h[8], const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 | (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        const uint32_t s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        const uint32_t s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; i++) {
        const uint32_t s1 = rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25);
        const uint32_t ch = (e & f) ^ (~e & g);
        const uint32_t t1 = hh + s1 + ch + kK256[i] + w[i];
        const uint32_t s0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
        const uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        const uint32_t t2 = s0 + maj;
        hh = g, g = f, f = e, e = d + t1, d = c, c = b, b = a, a = t1 + t2;
    }
    h[0] += a, h[1] += b, h[2] += c, h[3] += d, h[4] += e, h[5] += f, h[6] += g, h[7] += hh;
}

void sha256(const uint8_t *data, size_t n, uint8_t out[32]) {
    uint32_t h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    const size_t whole = n - n % 64;
    for (size_t i = 0; i < whole; i += 64) sha256_block(h, data + i);
    uint8_t tail[128] = {0};
    const size_t rest = n - whole;
    memcpy(tail, data + whole, rest);
    tail[rest] = 0x80;
    const size_t len = rest + 1 + 8 <= 64 ? 64 : 128;
    const uint64_t bits = static_cast<uint64_t>(n) * 8;
    for (int i = 0; i < 8; i++) tail[len - 1 - static_cast<size_t>(i)] = static_cast<uint8_t>(bits >> (8 * i));
    for (size_t i = 0; i < len; i += 64) sha256_block(h, tail + i);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = static_cast<uint8_t>(h[i] >> 24);
        out[4 * i + 1] = static_cast<uint8_t>(h[i] >> 16);
        out[4 * i + 2] = static_cast<uint8_t>(h[i] >> 8);
        out[4 * i + 3] = static_cast<uint8_t>(h[i]);
    }
}

/* the whole file at path, into out; 0, or the errno that stopped it */
int read_file(const std::string &path, std::vector<uint8_t> &out) {
    FILE *f = fopen(path.c_str(), "rb");
    if (!f) return errno;
    out.clear();
    uint8_t buf[1 << 16];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, f)) > 0) out.insert(out.end(), buf, buf + n);
    const int err = ferror(f) ? (errno ? errno : EIO) : 0;
    fclose(f);
    return err;
}

/* ---- the calls: which kinds exist, how many arguments each takes, which host questions the capture answers ---- */

bool is_boundary(uint32_t k) { return k >= RXC_NVN_SUBMIT && k <= RXC_NVN_EVENT_SIGNAL; }
bool is_host(uint32_t k) { return k >= RXC_HOST_LOG && k <= RXC_HOST_WIDE_COVER; }
bool is_call(uint32_t k) { return is_boundary(k) || is_host(k); }

/* the host kinds' names, from RXC_HOST_LOG, for the player's log */
const char *const kHostNames[] = {
    "log", "guest_base", "guest_size", "gpu_ptr", "gpu_guest", "gpu_read", "gpu_write", "collect_written",
    "free_pages", "mark_written", "ticks", "now_ns", "tex_layout", "tex_level_offset", "fmt_bpb", "tex_level_tiles",
    "tex_linear_stride", "depth_minus_w", "gpu_pools", "maps_generation", "vmap_generation", "reserved_cpus",
    "collect_written_in", "guest_alloc", "watch_guest_writes", "pad_raw", "binding", "bind", "aspect_ratio",
    "take_key_press", "pointer", "mic_count", "mic_name", "mic_select", "gyro_status", "log_flush", "prompt_face",
    "romfs_dir", "wide_menus", "slept_ns", "fps_timing", "prompts_live", "screen_size", "wide_cover",
};
static_assert(std::size(kHostNames) == RXC_HOST_WIDE_COVER - RXC_HOST_LOG + 1, "a name for each host kind");

/* the arguments of a CALL of kind k (-1: not a call) */
int call_nargs(uint32_t k) {
    switch (k) {
    case RXC_NVN_FENCE:
    case RXC_NVN_PUSHED:
    case RXC_NVN_REPLAYED:
    case RXC_NVN_FINISH:
    case RXC_HOST_GUEST_BASE:
    case RXC_HOST_GUEST_SIZE:
    case RXC_HOST_TICKS:
    case RXC_HOST_NOW_NS:
    case RXC_HOST_DEPTH_MINUS_W:
    case RXC_HOST_MAPS_GENERATION:
    case RXC_HOST_VMAP_GENERATION:
    case RXC_HOST_RESERVED_CPUS:
    case RXC_HOST_PAD_RAW:
    case RXC_HOST_ASPECT_RATIO:
    case RXC_HOST_MIC_COUNT:
    case RXC_HOST_GYRO_STATUS:
    case RXC_HOST_LOG_FLUSH:
    case RXC_HOST_PROMPT_FACE:
    case RXC_HOST_ROMFS_DIR:
    case RXC_HOST_PROMPTS_LIVE:
    case RXC_HOST_WIDE_COVER:
        return 0;
    case RXC_NVN_FENCE_Q:
    case RXC_NVN_SET_TEXTURE_POOL_SIZE:
    case RXC_NVN_TEXTURE_RELEASED:
    case RXC_NVN_TEXTURE_CREATED:
    case RXC_NVN_PROGRAM_RELEASED:
    case RXC_NVN_PROGRAM_CREATED:
    case RXC_HOST_LOG:
    case RXC_HOST_GPU_GUEST:
    case RXC_HOST_COLLECT_WRITTEN:
    case RXC_HOST_FREE_PAGES:
    case RXC_HOST_TEX_LINEAR_STRIDE:
    case RXC_HOST_GUEST_ALLOC:
    case RXC_HOST_TAKE_KEY_PRESS:
    case RXC_HOST_POINTER:
    case RXC_HOST_MIC_NAME:
    case RXC_HOST_WIDE_MENUS:
    case RXC_HOST_SLEPT_NS:
    case RXC_HOST_FPS_TIMING:
        return 1;
    case RXC_NVN_WAIT:
    case RXC_WAIT_WRITEBACKS_READING:
    case RXC_NVN_PRESENT:
    case RXC_NVN_REGISTER_SAMPLER:
    case RXC_NVN_INVALIDATE:
    case RXC_NVN_POOL_CREATED:
    case RXC_NVN_VMAP_CHANGING:
    case RXC_NVN_EVENT_SIGNAL:
    case RXC_HOST_GPU_PTR:
    case RXC_HOST_MARK_WRITTEN:
    case RXC_HOST_TEX_LAYOUT:
    case RXC_HOST_TEX_LEVEL_OFFSET:
    case RXC_HOST_GPU_POOLS:
    case RXC_HOST_WATCH_GUEST_WRITES:
    case RXC_HOST_SCREEN_SIZE:
    case RXC_NVN_SUBMIT:
        return 2;
    case RXC_NVN_SUBMIT_Q:
    case RXC_NVN_REGISTER_TEXTURE:
    case RXC_HOST_GPU_READ:
    case RXC_HOST_GPU_WRITE:
    case RXC_HOST_FMT_BPB:
    case RXC_HOST_COLLECT_WRITTEN_IN:
    case RXC_HOST_BINDING:
    case RXC_HOST_MIC_SELECT:
        return 3;
    case RXC_HOST_TEX_LEVEL_TILES:
    case RXC_HOST_BIND:
        return 4;
    case RXC_NVN_READ_TEXELS:
    case RXC_NVN_WRITE_TEXELS:
        return 7;
    default:
        return -1;
    }
}

/* A question's arguments: the values, the bytes it reads (In) and the bytes it writes (Out), each pointer's size. */
enum class Arg : uint8_t { Value, In, Out };

struct Desc {
    int n = 0;
    Arg dir[4] = {Arg::Value, Arg::Value, Arg::Value, Arg::Value};
    uint64_t size[4] = {0, 0, 0, 0};
};

/* The host questions the capture answers, by their arguments: false for the questions the player answers itself and for
   the notifications that answer nothing (log, bind, mic_select, fps_timing, log_flush, watch_guest_writes). */
bool host_desc(uint32_t kind, const uint64_t *a, Desc *d) {
    *d = Desc();
    const auto put = [d](Arg dir, uint64_t size) {
        d->dir[d->n] = dir;
        d->size[d->n] = size;
        d->n++;
    };
    switch (kind) {
    case RXC_HOST_GPU_GUEST:
        put(Arg::Value, 0);
        return true;
    case RXC_HOST_GPU_READ:
        put(Arg::Value, 0);
        put(Arg::Out, a[2]);
        put(Arg::Value, 0);
        return true;
    case RXC_HOST_GPU_WRITE:
        put(Arg::Value, 0);
        put(Arg::In, a[2]);
        put(Arg::Value, 0);
        return true;
    case RXC_HOST_TEX_LAYOUT:
        put(Arg::In, sizeof(nvn_texdesc));
        put(Arg::Out, sizeof(nvn_texlayout));
        return true;
    case RXC_HOST_TEX_LEVEL_OFFSET:
        put(Arg::In, sizeof(nvn_texdesc));
        put(Arg::Value, 0);
        return true;
    case RXC_HOST_FMT_BPB:
        put(Arg::Value, 0);
        put(Arg::Out, 4);
        put(Arg::Out, 4);
        return true;
    case RXC_HOST_TEX_LEVEL_TILES:
        put(Arg::In, sizeof(nvn_texdesc));
        put(Arg::Value, 0);
        put(Arg::Out, 4);
        put(Arg::Out, 4);
        return true;
    case RXC_HOST_TEX_LINEAR_STRIDE:
        put(Arg::In, sizeof(nvn_texdesc));
        return true;
    case RXC_HOST_GUEST_ALLOC:
    case RXC_HOST_TAKE_KEY_PRESS:
    case RXC_HOST_MIC_NAME:
    case RXC_HOST_WIDE_MENUS:
        put(Arg::Value, 0);
        return true;
    case RXC_HOST_BINDING:
        put(Arg::Value, 0);
        put(Arg::Value, 0);
        put(Arg::Value, 0);
        return true;
    case RXC_HOST_POINTER:
        put(Arg::Out, sizeof(MkwVkPointer));
        return true;
    case RXC_HOST_SLEPT_NS:
        put(Arg::Out, 2 * sizeof(int64_t));
        return true;
    case RXC_HOST_SCREEN_SIZE:
        put(Arg::Out, 4);
        put(Arg::Out, 4);
        return true;
    case RXC_HOST_TICKS:
    case RXC_HOST_NOW_NS:
    case RXC_HOST_DEPTH_MINUS_W:
    case RXC_HOST_MAPS_GENERATION:
    case RXC_HOST_VMAP_GENERATION:
    case RXC_HOST_RESERVED_CPUS:
    case RXC_HOST_PAD_RAW:
    case RXC_HOST_ASPECT_RATIO:
    case RXC_HOST_MIC_COUNT:
    case RXC_HOST_GYRO_STATUS:
    case RXC_HOST_PROMPT_FACE:
    case RXC_HOST_ROMFS_DIR:
    case RXC_HOST_PROMPTS_LIVE:
    case RXC_HOST_WIDE_COVER:
        return true;
    default:
        return false;
    }
}

/* whether the capture answers a host kind as a question: host_desc's answer, which depends on the kind alone */
bool is_question(uint32_t kind) {
    const uint64_t none[4] = {0, 0, 0, 0};
    Desc d;
    return host_desc(kind, none, &d);
}

/* the host questions that are functions of their inputs (the NVN texture layout, gpu/vk/vk_api.h): a recorded
   answer to the same question answers it anywhere in the capture */
bool is_layout(uint32_t kind) {
    switch (kind) {
    case RXC_HOST_TEX_LAYOUT:
    case RXC_HOST_TEX_LEVEL_OFFSET:
    case RXC_HOST_FMT_BPB:
    case RXC_HOST_TEX_LEVEL_TILES:
    case RXC_HOST_TEX_LINEAR_STRIDE:
    case RXC_HOST_DEPTH_MINUS_W:
        return true;
    default:
        return false;
    }
}

/* the key of a question: its kind, its value arguments, and for each input what input(p, n) gives (its SHA-256) */
template <class Input>
std::string key_of(uint32_t kind, const uint64_t *a, const Desc &d, Input input) {
    std::string k(reinterpret_cast<const char *>(&kind), sizeof kind);
    for (int j = 0; j < d.n; j++) {
        if (d.dir[j] == Arg::Value)
            k.append(reinterpret_cast<const char *>(&a[j]), sizeof a[j]);
        else if (d.dir[j] == Arg::In)
            k += input(a[j], d.size[j]);
    }
    return k;
}

/* ---- the capture, as the player holds it ---- */

struct Rec {   /* one record of calls.rxc: its kind, its size, its seq, and where it starts in the file */
    uint32_t kind;
    uint32_t total;
    uint64_t seq;
    size_t at;
};

/* what a boundary pointer points to: its fields that are pointers are copies too */
enum class Shape { Plain, Tex, DPool, Recording, Handles };

struct Latest {   /* an object as the records stand at some point: its size and the SHA-256 of its bytes */
    uint64_t size = 0;
    Hash hash{};
};

struct Obj {   /* one recorded address: the latest OBJECT's blob, its bytes once read, and the player's copy */
    Latest blob;
    bool read = false;               /* the blob's bytes are in bytes: read once for this blob, then kept */
    std::vector<uint8_t> bytes;
    std::unique_ptr<uint8_t[]> copy;
    size_t copy_size = 0;
    Shape shape = Shape::Plain;   /* the shape the copy was last given out in */
};

struct Triple {   /* an output object of an answer: the argument it fills (-1: the string the result points to) */
    int arg;
    uint64_t size;
    Hash hash;
};

struct Answer {
    uint64_t result = 0;
    std::vector<Triple> outs;
    std::vector<uint8_t> text;   /* the string the result points to, once a question returned it */
    bool text_read = false;
};

const Triple *find_arg(const Answer &ans, int arg) {
    for (const Triple &t : ans.outs)
        if (t.arg == arg) return &t;
    return nullptr;
}

struct Piece {   /* a part of the window: n bytes at offset at */
    uint64_t at;
    uint64_t n;
};

/* the guest memory is made as the game makes its window (host/hos_main.c): a memfd's shared mapping where the game uses
   one, so the renderer can import it as dma-bufs, otherwise anonymous memory without transparent huge pages. The switch
   is the game's: HOS_GUEST_MEMFD set nonzero for the memfd, 0 for anonymous memory; unset, the memfd where /dev/udmabuf
   opens and udmabuf's size_limit_mb reads nonzero */
int guest_memfd = -1;   /* the window's memfd (-1: anonymous memory), for hos_posix_guest_memfd */

bool wants_memfd() {
    if (const char *e = getenv("HOS_GUEST_MEMFD")) return atoi(e) != 0;
    const int u = open("/dev/udmabuf", O_RDWR | O_CLOEXEC);
    if (u < 0) return false;
    close(u);
    unsigned long long limit_mb = 0;
    if (FILE *f = fopen("/sys/module/udmabuf/parameters/size_limit_mb", "r")) {
        if (fscanf(f, "%llu", &limit_mb) != 1) limit_mb = 0;
        fclose(f);
    }
    return limit_mb > 0;
}

/* a memfd of size bytes that its seals keep at that size, as the game's: -1 when it cannot be made */
int make_memfd(uint64_t size) {
    const int fd = memfd_create("hookshot-guest", MFD_CLOEXEC | MFD_ALLOW_SEALING);
    if (fd < 0) return -1;
    if (ftruncate(fd, static_cast<off_t>(size)) != 0 || fcntl(fd, F_ADD_SEALS, F_SEAL_SHRINK | F_SEAL_GROW) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

/* size bytes of guest memory, zeroed as calloc zeroed it: the memfd's shared mapping, or anonymous memory */
uint8_t *make_window(uint64_t size) {
    if (wants_memfd()) {
        const int fd = make_memfd(size);
        if (fd >= 0) {
            void *p = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_NORESERVE, fd, 0);
            if (p != MAP_FAILED) {
                guest_memfd = fd;
                return static_cast<uint8_t *>(p);
            }
            close(fd);
        }
    }
    void *p = mmap(nullptr, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (p == MAP_FAILED) return nullptr;
    /* (issue 312, as host/hos_main.c: no transparent huge pages in the window. Where THP is not built in the advice
       fails, and there are no huge pages to keep out) */
    (void)madvise(p, size, MADV_NOHUGEPAGE);
    return static_cast<uint8_t *>(p);
}

/* fn(j) for each j below n, on up to hardware_concurrency() threads (this one among them); all are done on return */
template <class Fn>
void for_each_index(size_t n, Fn fn) {
    const size_t workers = std::max<size_t>(1, std::thread::hardware_concurrency());
    std::atomic<size_t> next{0};
    const auto run = [&]() {
        for (size_t j = next++; j < n; j = next++) fn(j);
    };
    std::vector<std::thread> helpers;
    for (size_t t = 1; t < std::min(workers, n); t++) {
        try {
            helpers.emplace_back(run);
        } catch (const std::system_error &) {
            break;   /* no thread to be had: the ones that started, and this one, do the rest */
        }
    }
    run();
    for (std::thread &t : helpers) t.join();
}

struct Blob {   /* a blob fetch read: its bytes, its SHA-256 if not verified yet, and its read's failure line */
    std::string err;
    std::vector<uint8_t> bytes;
    bool hashed = false;
    Hash got{};
};

struct Player {
    std::string dir;                                        /* the capture's folder */
    std::mutex error_mu;                                    /* guards error: a host question's thread may fail it */
    std::string error;                                      /* the first failure: the line rx_play writes */
    std::vector<uint8_t> file;                              /* calls.rxc, whole */
    std::vector<Rec> recs;
    std::unordered_map<uint64_t, size_t> call_of_seq;       /* a CALL's seq: its record */
    std::vector<size_t> answer_of;                          /* per record: the ANSWER of a CALL (kNone: none) */
    std::mutex verified_mu;                                 /* guards verified: a host question's thread reads blobs */
    std::unordered_map<std::string, uint64_t> verified;     /* blobs checked: name → size */
    std::unordered_map<std::string, std::vector<size_t>> question_index;   /* question key → ANSWERs, ascending */
    std::unordered_map<uint64_t, uint64_t> page_guest;      /* a page's base → its gpu_guest answer (in the window) */
    std::unordered_map<size_t, Answer> answers;             /* by ANSWER record */
    std::unordered_map<uint64_t, Obj> objs;                 /* by recorded address */
    std::set<uint64_t> stale;                               /* the addresses whose copies wait for flush */
    std::vector<std::unique_ptr<uint8_t[]>> retired;        /* copies that got new storage: kept alive for the play */
    std::vector<std::array<uint64_t, 2>> pools;             /* the POOL records replayed: {start, size} */
    std::set<uint32_t> pending;                             /* the pages written since the renderer's last collection */
    uint8_t *window = nullptr;                              /* the guest memory */
    uint64_t window_size = 0;
    size_t ref = 0;                                         /* the boundary call replayed: answers before it count */
    bool snapshot = false;                                  /* the capture holds a SNAPSHOT (check_payloads) */
    std::unordered_map<uint64_t, uint64_t> played_fence;    /* a recorded fence answer: the play's latest answer */
    /* unanswered host questions, by kind - RXC_HOST_LOG (the renderer asks from several threads) */
    std::array<std::atomic<size_t>, std::size(kHostNames)> unanswered{};
    std::chrono::steady_clock::time_point began = std::chrono::steady_clock::now();   /* the player's start */
    /* the load's marks (report_phases): the reading's end, the renderer's init about to start, the renderer's start,
       and the end of the SNAPSHOT's replay */
    std::chrono::steady_clock::time_point read_done, index_done, started, snapshot_done;

    explicit Player(std::string d) : dir(std::move(d)) {}
    Player(const Player &) = delete;
    Player &operator=(const Player &) = delete;
    ~Player() {
        if (window) munmap(window, window_size);
        if (guest_memfd >= 0) close(guest_memfd);
        guest_memfd = -1;
    }

    bool fail(std::string msg) {
        const std::lock_guard<std::mutex> lock(error_mu);
        if (error.empty()) error = std::move(msg);
        return false;
    }
    bool failed() {
        const std::lock_guard<std::mutex> lock(error_mu);
        return !error.empty();
    }
    std::string failure() {
        const std::lock_guard<std::mutex> lock(error_mu);
        return error;
    }
    bool bad(size_t i, const char *what) {
        return fail(dir + "/calls.rxc does not parse: record " + std::to_string(i) + " " + what);
    }
    const uint8_t *payload(const Rec &r) const { return &file[r.at + kRecordHeader]; }
    size_t payload_size(const Rec &r) const { return r.total - kRecordHeader; }
    std::vector<uint64_t> args_of(const Rec &r) const {
        std::vector<uint64_t> a(payload_size(r) / 8);
        for (size_t k = 0; k < a.size(); k++) a[k] = rd64(payload(r) + 8 * k);
        return a;
    }
    long long ms_since_start(std::chrono::steady_clock::time_point t) const {
        return std::chrono::duration_cast<std::chrono::milliseconds>(t - began).count();
    }

    bool load();
    bool scan_records();
    bool check_payloads();
    bool window_setup();
    bool check_bounds();
    bool index_answers();
    bool read_blob(const Hash &h, uint64_t size, std::vector<uint8_t> &out);
    Blob fetch(const Hash &h);
    std::string check_blob(const Hash &h, uint64_t size, const Blob &b, bool insert);
    bool play(mkwvk_init_fn init, void *win);
    bool run(mkwvk_init_fn init, void *win, size_t start);
    uint64_t stored_bytes() const;
    bool apply(size_t i);
    bool flush();
    void set_latest(uint64_t addr, const Latest &blob);
    bool place_pages(const Rec &r);
    bool replay(const MkwVkRenderer *r, size_t i);
    void note_fence(size_t i, uint64_t played);
    uint64_t wait_fence(uint64_t fence) const;
    void prefill(size_t i, uint64_t dst);
    uint8_t *copy_of(uint64_t addr, Shape shape, bool optional);
    bool patch_recording(uint8_t *c, size_t n, uint64_t addr);
    bool patch_record(uint32_t op, uint8_t *pl, size_t plen);
    Answer *recorded(uint32_t kind, const uint64_t *a, const Desc &d);
    Answer *ask(uint32_t kind, const uint64_t *a);
    bool guest_of(uint64_t gpu, uint64_t *guest);
    bool window_pieces(uint64_t gpu, uint64_t n, std::vector<Piece> &out);
    void report_unanswered() const;
    void report_phases() const;
    const char *text_of(Answer *ans);
    void mark(uint64_t off, uint64_t n);
};

Player *g_player = nullptr;   /* the player whose replay the host's questions are answered in */

bool Player::load() {
    const std::string path = dir + "/calls.rxc";
    const int err = read_file(path, file);
    if (err) return fail("cannot read " + path + ": " + strerror(err));
    read_done = std::chrono::steady_clock::now();
    if (file.size() < 16 || memcmp(file.data(), "RXCAPT01", 8) != 0)
        return fail(path + " does not parse: no RXCAPT01 header");
    const uint32_t format = rd32(&file[8]);
    const uint32_t api = rd32(&file[12]);
    if (format != RXC_FORMAT) return fail(path + " does not parse: format version " + std::to_string(format));
    if (api != MKWVK_API_VERSION)
        return fail("version mismatch: " + path + " is MKWVK_API_VERSION " + std::to_string(api) +
                    ", this player is " + std::to_string(MKWVK_API_VERSION));
    return scan_records() && check_payloads() && window_setup() && check_bounds() && index_answers();
}

bool Player::scan_records() {
    const std::string path = dir + "/calls.rxc";
    for (size_t at = 16; at < file.size();) {
        const size_t i = recs.size();
        if (file.size() - at < kRecordHeader)
            return fail(path + " does not parse: record " + std::to_string(i) + " is cut short");
        const uint8_t *h = &file[at];
        const Rec r{rd32(h), rd32(h + 4), rd64(h + 8), at};
        if (r.total < kRecordHeader || r.total % 8 || r.total > file.size() - at)
            return fail(path + " does not parse: record " + std::to_string(i) + " is cut short");
        if (r.seq != i + 1)
            return fail(path + " does not parse: record " + std::to_string(i) + " has seq " + std::to_string(r.seq));
        recs.push_back(r);
        at += r.total;
    }
    return true;
}

bool Player::check_payloads() {
    answer_of.assign(recs.size(), kNone);
    for (size_t i = 0; i < recs.size(); i++) {
        const Rec &r = recs[i];
        const uint8_t *p = payload(r);
        const size_t n = payload_size(r);
        if (r.kind == RXC_OBJECT) {
            if (n != 48) return bad(i, "is an OBJECT of the wrong length");
        } else if (r.kind == RXC_GUEST) {
            if (n != 48) return bad(i, "is a GUEST of the wrong length");
        } else if (r.kind == RXC_POOL) {
            if (n < 16 || (n - 16) % 32) return bad(i, "is a POOL of the wrong length");
            const uint64_t start = rd64(p), size = rd64(p + 8);
            if (size && start + size - 1 < start) return bad(i, "is a POOL past the end of the address space");
            const uint64_t count = size ? (start + size - 1) / kPageBytes - start / kPageBytes + 1 : 0;
            if (count != (n - 16) / 32) return bad(i, "is a POOL whose pages do not cover its size");
        } else if (r.kind == RXC_PAGES) {
            if (n < 8 || rd64(p) > (n - 8) / 40 || n != 8 + 40 * rd64(p))
                return bad(i, "is a PAGES of the wrong length");
        } else if (r.kind == RXC_SNAPSHOT) {
            if (n != 8) return bad(i, "is a SNAPSHOT of the wrong length");
            if (snapshot) return bad(i, "is a second SNAPSHOT");
            if (rd64(p) < 1 || rd64(p) >= r.seq)
                return bad(i, "is a SNAPSHOT whose first record is not one of the records before it");
            snapshot = true;
        } else if (r.kind & RXC_ANSWER) {
            const uint32_t base = r.kind & ~static_cast<uint32_t>(RXC_ANSWER);
            const auto call = call_of_seq.find(rd64(p));
            if (!is_call(base) || n != 16 || call == call_of_seq.end() || recs[call->second].kind != base)
                return bad(i, "is an ANSWER to no call of its kind");
            answer_of[call->second] = i;
        } else if (is_call(r.kind)) {
            if (n != 8 * static_cast<size_t>(call_nargs(r.kind))) return bad(i, "is a CALL of the wrong length");
            call_of_seq[r.seq] = i;
        } else {
            return bad(i, "is of an unknown kind");
        }
    }
    return true;
}

/* the guest memory: the first guest_size answer's size, or, when the capture answers none, the pages its POOLs and
   PAGES name */
bool Player::window_setup() {
    bool answered = false;
    uint64_t guest = 0, pages_named = 0;
    for (const Rec &r : recs) {
        const uint8_t *p = payload(r);
        const bool guest_size =
            (r.kind & RXC_ANSWER) && (r.kind & ~static_cast<uint32_t>(RXC_ANSWER)) == RXC_HOST_GUEST_SIZE;
        if (guest_size && !answered) {
            guest = rd64(p + 8);
            answered = true;
        }
        if (r.kind == RXC_POOL && rd64(p + 8))
            pages_named = std::max(pages_named, (rd64(p) + rd64(p + 8) - 1) / kPageBytes + 1);
        if (r.kind == RXC_PAGES)
            for (uint64_t k = 0; k < rd64(p); k++) pages_named = std::max(pages_named, rd64(p + 8 + 40 * k) + 1);
    }
    window_size = answered ? guest : pages_named * kPageBytes;
    if (!window_size) return true;
    window = make_window(window_size);
    return window ? true : fail("cannot make the capture's " + std::to_string(window_size) + "-byte guest memory");
}

/* every page and GUEST the records name is in the guest memory */
bool Player::check_bounds() {
    const uint64_t pages = window_size / kPageBytes;
    for (size_t i = 0; i < recs.size(); i++) {
        const Rec &r = recs[i];
        const uint8_t *p = payload(r);
        if (r.kind == RXC_GUEST) {
            const uint64_t addr = rd64(p), size = rd64(p + 8);
            if (addr > window_size || size > window_size - addr) return bad(i, "names bytes past the guest memory");
        } else if (r.kind == RXC_POOL) {
            const uint64_t first = rd64(p) / kPageBytes;
            for (uint64_t k = 0; k < (payload_size(r) - 16) / 32; k++)
                if (first + k >= pages) return bad(i, "names a page past the guest memory");
        } else if (r.kind == RXC_PAGES) {
            for (uint64_t k = 0; k < rd64(p); k++)
                if (rd64(p + 8 + 40 * k) >= pages) return bad(i, "names a page past the guest memory");
        }
    }
    return true;
}

/* the blob named h, of size bytes, into out. Its first read checks its SHA-256 against its name, and a name read with
   two sizes is refused; every read checks the size it is given, so the bytes are always that size */
bool Player::read_blob(const Hash &h, uint64_t size, std::vector<uint8_t> &out) {
    Blob b = fetch(h);
    const std::string err = check_blob(h, size, b, true);
    if (!err.empty()) return fail(err);
    out = std::move(b.bytes);
    return true;
}

/* the blob named h, read by any thread: its bytes, and their SHA-256 when the blob is not verified yet (check_blob) */
Blob Player::fetch(const Hash &h) {
    Blob b;
    const std::string name = hex_of(h.data(), h.size());
    const int err = read_file(dir + "/blobs/" + name, b.bytes);
    if (err == ENOENT) {
        b.err = "missing blob " + name;
    } else if (err) {
        b.err = "cannot read blob " + name + ": " + strerror(err);
    } else {
        const std::lock_guard<std::mutex> lock(verified_mu);
        b.hashed = verified.find(name) == verified.end();
    }
    if (b.hashed) sha256(b.bytes.data(), b.bytes.size(), b.got.data());
    return b;
}

/* the checks of a blob fetched for size bytes, as read_blob gives them: a name read with two sizes is refused, the size
   is checked at each read, and the SHA-256 at the first. Returns the line the blob fails with, or "" when it is good,
   and a good blob is verified when insert is set. A blob not verified now was not verified when it was fetched, so it
   was hashed. */
std::string Player::check_blob(const Hash &h, uint64_t size, const Blob &b, bool insert) {
    if (!b.err.empty()) return b.err;
    const std::string name = hex_of(h.data(), h.size());
    const std::lock_guard<std::mutex> lock(verified_mu);
    const auto seen = verified.find(name);
    if (seen != verified.end() && seen->second != size)
        return dir + "/calls.rxc does not parse: blob " + name + " is named for two sizes";
    if (b.bytes.size() != size)
        return dir + "/calls.rxc does not parse: blob " + name + " is " + std::to_string(b.bytes.size()) +
               " bytes, not " + std::to_string(size);
    if (seen != verified.end()) return "";
    if (memcmp(b.got.data(), h.data(), h.size()) != 0) return "blob " + name + " does not match its name";
    if (insert) verified.emplace(name, size);
    return "";
}

/* The answers, indexed by the question they answer: a forward pass that knows each object as the records stand, so that
   a question's input bytes are those the question was asked with, and an answer's outputs are the objects the call
   left. */
bool Player::index_answers() {
    std::unordered_map<uint64_t, Latest> latest;
    std::unordered_map<size_t, std::string> keys;   /* a host CALL's question key */
    const auto input = [&latest](uint64_t p, uint64_t n) -> std::string {
        if (n == 0) return "e";
        if (!p) return "n";
        const auto it = latest.find(p);
        if (it == latest.end() || it->second.size != n) return "u";
        return "h" + std::string(it->second.hash.begin(), it->second.hash.end());
    };
    for (size_t i = 0; i < recs.size(); i++) {
        const Rec &r = recs[i];
        const uint8_t *p = payload(r);
        if (r.kind == RXC_OBJECT) {
            latest[rd64(p)] = Latest{rd64(p + 8), hash_at(p + 16)};
        } else if (is_host(r.kind)) {
            const std::vector<uint64_t> a = args_of(r);
            Desc d;
            if (host_desc(r.kind, a.data(), &d)) keys[i] = key_of(r.kind, a.data(), d, input);
        } else if (r.kind & RXC_ANSWER) {
            const uint32_t base = r.kind & ~static_cast<uint32_t>(RXC_ANSWER);
            const size_t call = call_of_seq.at(rd64(p));
            const std::vector<uint64_t> a = args_of(recs[call]);
            Answer ans;
            ans.result = rd64(p + 8);
            Desc d;
            if (is_host(base) && host_desc(base, a.data(), &d)) {
                const auto key = keys.find(call);
                for (int j = 0; key != keys.end() && j < d.n; j++) {
                    if (d.dir[j] != Arg::Out || !a[j]) continue;
                    const auto l = latest.find(a[j]);
                    if (l != latest.end() && l->second.size == d.size[j])
                        ans.outs.push_back({j, d.size[j], l->second.hash});
                }
                if (key != keys.end() && (base == RXC_HOST_MIC_NAME || base == RXC_HOST_ROMFS_DIR) && ans.result) {
                    const auto l = latest.find(ans.result);
                    if (l != latest.end()) ans.outs.push_back({-1, l->second.size, l->second.hash});
                }
                if (key != keys.end()) question_index[key->second].push_back(i);
                if (base == RXC_HOST_GPU_GUEST && a[0] % kPageBytes == 0 && ans.result && ans.result < window_size)
                    page_guest.emplace(a[0], ans.result);
                answers.emplace(i, std::move(ans));
            } else if (base == RXC_NVN_READ_TEXELS) {
                const auto l = latest.find(a[4]);
                if (l != latest.end()) ans.outs.push_back({4, l->second.size, l->second.hash});
                answers.emplace(i, std::move(ans));
            }
        }
    }
    return true;
}

/* ---- the replay ---- */

/* the state an OBJECT, POOL, PAGES or GUEST record sets; the other records change no state here */
bool Player::apply(size_t i) {
    const Rec &r = recs[i];
    const uint8_t *p = payload(r);
    if (r.kind == RXC_OBJECT) {
        set_latest(rd64(p), Latest{rd64(p + 8), hash_at(p + 16)});
    } else if (r.kind == RXC_GUEST) {   /* (its range is in the window: check_bounds; not a page, so not pending) */
        std::vector<uint8_t> bytes;
        if (!read_blob(hash_at(p + 16), rd64(p + 8), bytes)) return false;
        if (!bytes.empty()) memcpy(window + rd64(p), bytes.data(), bytes.size());
    } else if (r.kind == RXC_POOL) {
        pools.push_back({rd64(p), rd64(p + 8)});
        if (!place_pages(r)) return false;
    } else if (r.kind == RXC_PAGES) {
        if (!place_pages(r)) return false;
    }
    return true;
}

/* the pages of a POOL or PAGES record, in batches of kBatchPages. A batch's blobs are fetched and checked by up to
   hardware_concurrency() workers (this thread among them), changing no state; the batch's pages before its lowest
   failing page are then written by the workers, and this thread verifies those blobs and lists those pages in page
   order. The lowest failing page stops the record with its line, so the window, the pending pages and the verified blobs
   are those a one-page-at-a-time read leaves. The record is done when its last batch is. */
bool Player::place_pages(const Rec &r) {
    const uint8_t *p = payload(r);
    const bool pool = r.kind == RXC_POOL;
    const uint64_t count = pool ? (payload_size(r) - 16) / 32 : rd64(p);
    const auto page_of = [&](uint64_t k) { return pool ? rd64(p) / kPageBytes + k : rd64(p + 8 + 40 * k); };
    const auto hash_of = [&](uint64_t k) { return pool ? hash_at(p + 16 + 32 * k) : hash_at(p + 16 + 40 * k); };
    std::vector<Blob> got;
    for (uint64_t base = 0; base < count; base += kBatchPages) {
        const size_t n = static_cast<size_t>(std::min(kBatchPages, count - base));
        got.assign(n, Blob());
        std::vector<std::string> err(n);
        for_each_index(n, [&](size_t j) {
            const Hash h = hash_of(base + j);
            got[j] = fetch(h);
            err[j] = check_blob(h, kPageBytes, got[j], false);
        });
        size_t lowest = 0;   /* the batch's first failing page, or n */
        while (lowest < n && err[lowest].empty()) lowest++;
        for_each_index(lowest, [&](size_t j) {
            memcpy(window + page_of(base + j) * kPageBytes, got[j].bytes.data(), kPageBytes);
        });
        for (size_t j = 0; j < lowest; j++) {
            const std::string e = check_blob(hash_of(base + j), kPageBytes, got[j], true);
            if (!e.empty()) return fail(e);
            pending.insert(static_cast<uint32_t>(page_of(base + j)));
        }
        if (lowest < n) return fail(err[lowest]);
    }
    return true;
}

/* a read's output starts as the answer the capture recorded for the read: the object it started with is the answer's
   blob at dst, read when the read's copy of dst is built */
void Player::prefill(size_t i, uint64_t dst) {
    if (!dst || answer_of[i] == kNone) return;
    const auto ans = answers.find(answer_of[i]);
    if (ans == answers.end()) return;
    const Triple *t = find_arg(ans->second, 4);
    if (t) set_latest(dst, Latest{t->size, t->hash});
}

/* the blob an OBJECT (or a read's output) gives an address: its copy, when there is one, is stale until flush */
void Player::set_latest(uint64_t addr, const Latest &blob) {
    Obj &o = objs[addr];
    if (o.blob.size != blob.size || o.blob.hash != blob.hash) {   /* another blob: its bytes are read again when used */
        o.read = false;
        o.bytes = std::vector<uint8_t>();
    }
    o.blob = blob;
    if (o.copy) stale.insert(addr);
}

/* the stale copies are rewritten, each in the shape it was last given out in. Called before each boundary call is
   replayed, when every record before the call is applied, so each pointer field names the object it points to */
bool Player::flush() {
    std::set<uint64_t> due;
    due.swap(stale);
    for (const uint64_t addr : due) {
        const Shape shape = objs.at(addr).shape;
        if (!copy_of(addr, shape, false)) return false;
    }
    return true;
}

/* the copy of the object recorded at addr, as its shape says: the copy is rewritten from the recorded blob, read here,
   and its pointer fields point to the copies of what they point to. optional: no object there is NULL, not an error. */
uint8_t *Player::copy_of(uint64_t addr, Shape shape, bool optional) {
    if (!addr) return nullptr;
    const auto it = objs.find(addr);
    if (it == objs.end()) {
        if (!optional) fail("does not replay: no object is recorded at " + std::to_string(addr));
        return nullptr;
    }
    Obj &o = it->second;
    o.shape = shape;
    const size_t n = o.blob.size;
    const bool fits = shape == Shape::Tex ? n >= sizeof(nvn_tex)
                      : shape == Shape::DPool ? n >= sizeof(nvn_dpool)
                      : shape == Shape::Recording ? n >= 8
                                                  : true;
    if (!fits) {
        fail("does not replay: the object recorded at " + std::to_string(addr) + " is too short for its record");
        return nullptr;
    }
    if (!o.read) {
        if (!read_blob(o.blob.hash, n, o.bytes)) return nullptr;
        o.read = true;
    }
    if (!o.copy || o.copy_size != n) {
        if (o.copy) retired.push_back(std::move(o.copy));
        o.copy.reset(new uint8_t[n ? n : 1]);
        o.copy_size = n;
    }
    uint8_t *c = o.copy.get();
    if (n) memcpy(c, o.bytes.data(), n);
    switch (shape) {
    case Shape::Plain:
        break;
    case Shape::Tex: {
        auto *t = reinterpret_cast<nvn_tex *>(c);
        t->pool = reinterpret_cast<const nvn_pool *>(copy_of(addr_of(t->pool), Shape::Plain, false));
        break;
    }
    case Shape::DPool: {
        auto *d = reinterpret_cast<nvn_dpool *>(c);
        d->pool = reinterpret_cast<const nvn_pool *>(copy_of(addr_of(d->pool), Shape::Plain, false));
        break;
    }
    case Shape::Recording:
        if (!patch_recording(c, n, addr)) return nullptr;
        break;
    case Shape::Handles: {
        auto *hs = reinterpret_cast<uint64_t *>(c);
        for (size_t k = 0; k < n / 8; k++) hs[k] = addr_of(copy_of(hs[k], Shape::Recording, false));
        break;
    }
    }
    return c;
}

/* a recording's records, BEGIN to END, with their pointers pointing to the copies */
bool Player::patch_recording(uint8_t *c, size_t n, uint64_t addr) {
    for (size_t at = 0;;) {
        if (n - at < sizeof(nvn_op_hdr))
            return fail("does not replay: the recording at " + std::to_string(addr) + " has no END");
        nvn_op_hdr h;
        memcpy(&h, c + at, sizeof h);
        if (h.size < sizeof h || h.size % 8 || h.size > n - at || h.op >= NOP_COUNT_)
            return fail("does not replay: the recording at " + std::to_string(addr) + " has a malformed record");
        if (h.op == NOP_END) return true;
        if (!patch_record(h.op, c + at + sizeof h, h.size - sizeof h)) return false;
        at += h.size;
    }
}

bool Player::patch_record(uint32_t op, uint8_t *pl, size_t plen) {
    const auto short_record = [this]() { return fail("does not replay: a record is shorter than its kind"); };
    switch (op) {
    case NOP_SET_RT: {
        if (plen < sizeof(nvn_p_set_rt)) return short_record();
        auto *s = reinterpret_cast<nvn_p_set_rt *>(pl);
        const int lim = s->n < 8 ? s->n : 8;
        for (int k = 0; k < lim; k++)
            s->color[k] = reinterpret_cast<const nvn_tex *>(copy_of(addr_of(s->color[k]), Shape::Tex, false));
        s->depth = reinterpret_cast<const nvn_tex *>(copy_of(addr_of(s->depth), Shape::Tex, false));
        return !failed();
    }
    case NOP_BIND_PROGRAM: {
        if (plen < sizeof(nvn_p_bind_program)) return short_record();
        auto *b = reinterpret_cast<nvn_p_bind_program *>(pl);
        b->prog = reinterpret_cast<const nvn_prog *>(copy_of(addr_of(b->prog), Shape::Plain, false));
        return !failed();
    }
    case NOP_SET_TEX_POOL:
    case NOP_SET_SAMP_POOL: {
        if (plen < sizeof(nvn_p_set_pool)) return short_record();
        auto *s = reinterpret_cast<nvn_p_set_pool *>(pl);
        s->pool = reinterpret_cast<const nvn_dpool *>(copy_of(addr_of(s->pool), Shape::DPool, false));
        return !failed();
    }
    case NOP_COPY_B2T:
    case NOP_COPY_T2B: {
        if (plen < sizeof(nvn_p_copy_bt)) return short_record();
        auto *c = reinterpret_cast<nvn_p_copy_bt *>(pl);
        c->tex = reinterpret_cast<const nvn_tex *>(copy_of(addr_of(c->tex), Shape::Tex, false));
        return !failed();
    }
    case NOP_COPY_T2T: {
        if (plen < sizeof(nvn_p_copy_tt)) return short_record();
        auto *c = reinterpret_cast<nvn_p_copy_tt *>(pl);
        c->src = reinterpret_cast<const nvn_tex *>(copy_of(addr_of(c->src), Shape::Tex, false));
        c->dst = reinterpret_cast<const nvn_tex *>(copy_of(addr_of(c->dst), Shape::Tex, false));
        return !failed();
    }
    case NOP_CLEAR_TEX: {
        if (plen < sizeof(nvn_p_clear_tex)) return short_record();
        auto *c = reinterpret_cast<nvn_p_clear_tex *>(pl);
        c->tex = reinterpret_cast<const nvn_tex *>(copy_of(addr_of(c->tex), Shape::Tex, false));
        return !failed();
    }
    default:
        return true;
    }
}

/* a fence call replayed: in a capture with a snapshot, the play's answer is kept under the fence the capture recorded
   for the call (its ANSWER), for the waits that name that fence */
void Player::note_fence(size_t i, uint64_t played) {
    if (!snapshot || answer_of[i] == kNone) return;
    played_fence[rd64(payload(recs[answer_of[i]]) + 8)] = played;
}

/* the fence an nvn_wait is played with: in a capture with a snapshot, the play's answer to the latest replayed fence
   whose recorded answer is the wait's fence, or 0 when none is (a fence handed out before the start); else the recorded
   fence */
uint64_t Player::wait_fence(uint64_t fence) const {
    if (!snapshot) return fence;
    const auto it = played_fence.find(fence);
    return it == played_fence.end() ? 0 : it->second;
}

/* one boundary CALL: its arguments as the player's copies, then the renderer's entry */
bool Player::replay(const MkwVkRenderer *r, size_t i) {
    const std::vector<uint64_t> a = args_of(recs[i]);
    const auto tex = [this](uint64_t x) { return reinterpret_cast<const nvn_tex *>(copy_of(x, Shape::Tex, true)); };
    const auto plain = [this](uint64_t x) { return copy_of(x, Shape::Plain, true); };
    switch (recs[i].kind) {
    case RXC_NVN_SUBMIT: {
        const auto *handles = reinterpret_cast<const uint64_t *>(copy_of(a[0], Shape::Handles, true));
        if (failed()) return false;
        r->nvn_submit(handles, static_cast<int>(a[1]));
        break;
    }
    case RXC_NVN_SUBMIT_Q: {
        const auto *handles = reinterpret_cast<const uint64_t *>(copy_of(a[0], Shape::Handles, true));
        if (failed()) return false;
        r->nvn_submit_q(handles, static_cast<int>(a[1]), a[2]);
        break;
    }
    case RXC_NVN_FENCE:
        note_fence(i, r->nvn_fence());
        break;
    case RXC_NVN_FENCE_Q:
        note_fence(i, r->nvn_fence_q(a[0]));
        break;
    case RXC_NVN_PUSHED:
        r->nvn_pushed();
        break;
    case RXC_NVN_REPLAYED:
        r->nvn_replayed();
        break;
    case RXC_NVN_WAIT:
        r->nvn_wait(wait_fence(a[0]), a[1]);
        break;
    case RXC_WAIT_WRITEBACKS_READING: {
        const void *p = plain(a[0]);
        if (failed()) return false;
        r->wait_writebacks_reading(p, a[1]);
        break;
    }
    case RXC_NVN_FINISH:
        r->nvn_finish();
        break;
    case RXC_NVN_PRESENT: {
        const nvn_tex *t = tex(a[0]);
        const auto *crop = reinterpret_cast<const int *>(plain(a[1]));
        if (failed()) return false;
        r->nvn_present(t, crop);
        break;
    }
    case RXC_NVN_REGISTER_TEXTURE: {
        const nvn_tex *t = tex(a[1]);
        const auto *v = reinterpret_cast<const nvn_view *>(plain(a[2]));
        if (failed()) return false;
        r->nvn_register_texture(static_cast<int>(a[0]), t, v);
        break;
    }
    case RXC_NVN_REGISTER_SAMPLER: {
        const auto *p = reinterpret_cast<const nvn_samp_par *>(plain(a[1]));
        if (failed()) return false;
        r->nvn_register_sampler(static_cast<int>(a[0]), p);
        break;
    }
    case RXC_NVN_SET_TEXTURE_POOL_SIZE:
        r->nvn_set_texture_pool_size(static_cast<int>(a[0]));
        break;
    case RXC_NVN_TEXTURE_RELEASED: {
        const nvn_tex *t = tex(a[0]);
        if (failed()) return false;
        r->nvn_texture_released(t);
        break;
    }
    case RXC_NVN_TEXTURE_CREATED: {
        const nvn_tex *t = tex(a[0]);
        if (failed()) return false;
        r->nvn_texture_created(t);
        break;
    }
    case RXC_NVN_PROGRAM_RELEASED: {
        const auto *p = reinterpret_cast<const nvn_prog *>(plain(a[0]));
        if (failed()) return false;
        r->nvn_program_released(p);
        break;
    }
    case RXC_NVN_PROGRAM_CREATED: {
        const auto *p = reinterpret_cast<const nvn_prog *>(plain(a[0]));
        if (failed()) return false;
        r->nvn_program_created(p);
        break;
    }
    case RXC_NVN_READ_TEXELS: {
        const nvn_tex *t = tex(a[0]);
        const auto *region = reinterpret_cast<const NVNcopyRegion *>(plain(a[3]));
        prefill(i, a[4]);
        uint8_t *dst = plain(a[4]);
        if (failed()) return false;
        r->nvn_read_texels(t, static_cast<unsigned>(a[1]), static_cast<unsigned>(a[2]), region, dst, a[5], a[6]);
        break;
    }
    case RXC_NVN_WRITE_TEXELS: {
        const nvn_tex *t = tex(a[0]);
        const auto *region = reinterpret_cast<const NVNcopyRegion *>(plain(a[3]));
        const auto *src = plain(a[4]);
        if (failed()) return false;
        r->nvn_write_texels(t, static_cast<unsigned>(a[1]), static_cast<unsigned>(a[2]), region, src, a[5], a[6]);
        break;
    }
    case RXC_NVN_INVALIDATE:
        r->nvn_invalidate(a[0], a[1]);
        break;
    case RXC_NVN_POOL_CREATED:
        r->nvn_pool_created(a[0], a[1]);
        break;
    case RXC_NVN_VMAP_CHANGING:
        r->nvn_vmap_changing(a[0], a[1]);
        break;
    case RXC_NVN_EVENT_SIGNAL:
        r->nvn_event_signal(a[0], static_cast<uint32_t>(a[1]));
        break;
    default:
        return fail("does not replay: call kind " + std::to_string(recs[i].kind) + " is not a boundary call");
    }
    return !failed();
}

/* the answer the capture recorded to a host question (not counted as unanswered): the latest before the call being
   replayed with the same question; a layout question with none before it, the earliest after it. NULL: none. */
Answer *Player::recorded(uint32_t kind, const uint64_t *a, const Desc &d) {
    const std::string key = key_of(kind, a, d, [](uint64_t p, uint64_t n) -> std::string {
        if (n == 0) return "e";
        if (!p) return "n";
        Hash h;
        sha256(bytes_at(p), n, h.data());
        return "h" + std::string(h.begin(), h.end());
    });
    const auto found = question_index.find(key);
    if (found == question_index.end()) return nullptr;
    const std::vector<size_t> &at = found->second;
    const auto after = std::lower_bound(at.begin(), at.end(), ref);
    if (after != at.begin()) return &answers.at(*std::prev(after));
    if (is_layout(kind) && after != at.end()) return &answers.at(*after);
    return nullptr;
}

/* A host question: its answer from the capture (see recorded), its output buffers filled, or none (NULL) when the
   capture recorded none. */
Answer *Player::ask(uint32_t kind, const uint64_t *a) {
    Desc d;
    const bool question = host_desc(kind, a, &d);
    Answer *ans = recorded(kind, a, d);
    if (!ans && question) unanswered[kind - RXC_HOST_LOG].fetch_add(1, std::memory_order_relaxed);
    for (int j = 0; j < d.n; j++) {
        if (d.dir[j] != Arg::Out || !a[j]) continue;
        uint8_t *dst = bytes_at(a[j]);
        const Triple *t = ans ? find_arg(*ans, j) : nullptr;
        if (!t) {
            memset(dst, 0, d.size[j]);
            continue;
        }
        std::vector<uint8_t> bytes;
        if (!read_blob(t->hash, t->size, bytes)) return nullptr;
        memcpy(dst, bytes.data(), std::min<size_t>(bytes.size(), d.size[j]));
    }
    return ans;
}

/* the string an answer returned (NUL-terminated, as the recorder kept it), or NULL */
const char *Player::text_of(Answer *ans) {
    if (!ans) return nullptr;
    const Triple *t = find_arg(*ans, -1);
    if (!t) return nullptr;
    if (!ans->text_read) {
        if (!read_blob(t->hash, t->size, ans->text)) return nullptr;
        if (ans->text.empty() || ans->text.back() != 0) {
            fail(dir + "/calls.rxc does not parse: a string answer is not NUL-terminated");
            return nullptr;
        }
        ans->text_read = true;
    }
    return reinterpret_cast<const char *>(ans->text.data());
}

/* the pages of [off, off + n) of the window are written */
void Player::mark(uint64_t off, uint64_t n) {
    if (!n) return;
    for (uint64_t page = off / kPageBytes; page <= (off + n - 1) / kPageBytes; page++)
        pending.insert(static_cast<uint32_t>(page));
}

/* gpu_guest's answer for an address outside the window: the recorded answer for it, else, when the capture recorded the
   answer for its page with a guest address in the window, that answer plus the address's offset in the page. False when
   neither was recorded. */
bool Player::guest_of(uint64_t gpu, uint64_t *guest) {
    const uint64_t a[1] = {gpu};
    Desc d;
    host_desc(RXC_HOST_GPU_GUEST, a, &d);
    if (const Answer *ans = recorded(RXC_HOST_GPU_GUEST, a, d)) {
        *guest = ans->result;
        return true;
    }
    const auto page = page_guest.find(gpu - gpu % kPageBytes);
    if (page == page_guest.end()) return false;
    *guest = page->second + gpu % kPageBytes;
    return true;
}

/* the pieces of the window that a range outside it reads or writes, in order: each 4 KB page of [gpu, gpu + n) has its
   bytes at the address gpu_guest gives the page's first byte in the range (an address in the window gives itself), and
   that address and the piece lie in the window. False when a page does not, or n is 0. */
bool Player::window_pieces(uint64_t gpu, uint64_t n, std::vector<Piece> &out) {
    if (!n || n > UINT64_MAX - gpu) return false;
    out.clear();
    for (uint64_t done = 0; done < n;) {
        const uint64_t at = gpu + done;
        const uint64_t len = std::min(n - done, kPageBytes - at % kPageBytes);
        uint64_t guest = at;
        if (at >= window_size && (!guest_of(at, &guest) || !guest)) return false;   /* (0: the host's unmapped) */
        if (guest >= window_size || len > window_size - guest) return false;
        out.push_back({guest, len});
        done += len;
    }
    return true;
}

/* ---- the host's questions (MkwVkHost) ---- */

uint64_t value_of(uint32_t kind, const uint64_t *a) {
    const Answer *ans = g_player->ask(kind, a);
    return ans ? ans->result : 0;
}

void h_log(const char *line) {
    if (line) fprintf(stderr, "%s\n", line);
}

uint8_t *h_guest_base(void) { return g_player->window; }

uint64_t h_guest_size(void) { return g_player->window_size; }

/* outside the window: NULL with no bytes. A recorded pointer names the game's memory, which the capture does not
   hold, and the renderer dereferences what this returns. */
uint8_t *h_gpu_ptr(uint64_t gpu, uint64_t *avail) {
    Player &p = *g_player;
    if (gpu < p.window_size) {
        if (avail) *avail = p.window_size - gpu;
        return p.window + gpu;
    }
    if (avail) *avail = 0;
    return nullptr;
}

uint64_t h_gpu_guest(uint64_t gpu) {
    if (gpu < g_player->window_size) return gpu;
    uint64_t guest = 0;
    if (g_player->guest_of(gpu, &guest)) return guest;
    const uint64_t a[1] = {gpu};
    return value_of(RXC_HOST_GPU_GUEST, a);
}

int h_gpu_read(uint64_t gpu, void *dst, uint64_t n) {
    Player &p = *g_player;
    if (gpu <= p.window_size && n <= p.window_size - gpu) {
        if (n) memcpy(dst, p.window + gpu, n);
        return 1;
    }
    std::vector<Piece> pieces;
    if (p.window_pieces(gpu, n, pieces)) {
        auto *out = static_cast<uint8_t *>(dst);
        for (const Piece &c : pieces) {
            memcpy(out, p.window + c.at, c.n);
            out += c.n;
        }
        return 1;
    }
    const uint64_t a[3] = {gpu, addr_of(dst), n};
    return static_cast<int>(value_of(RXC_HOST_GPU_READ, a));
}

int h_gpu_write(uint64_t gpu, const void *src, uint64_t n) {
    Player &p = *g_player;
    if (gpu <= p.window_size && n <= p.window_size - gpu) {
        if (n) {
            memcpy(p.window + gpu, src, n);
            p.mark(gpu, n);
        }
        return 1;
    }
    std::vector<Piece> pieces;
    if (p.window_pieces(gpu, n, pieces)) {
        const auto *in = static_cast<const uint8_t *>(src);
        for (const Piece &c : pieces) {
            memcpy(p.window + c.at, in, c.n);
            p.mark(c.at, c.n);
            in += c.n;
        }
        return 1;
    }
    const uint64_t a[3] = {gpu, addr_of(src), n};
    return static_cast<int>(value_of(RXC_HOST_GPU_WRITE, a));
}

/* the pages in got, as a list the renderer frees with free_pages */
int64_t emit_pages(const std::vector<uint32_t> &got, uint32_t **pages) {
    *pages = nullptr;
    if (got.empty()) return 0;
    auto *out = static_cast<uint32_t *>(malloc(got.size() * sizeof(uint32_t)));
    if (!out) {
        g_player->fail("out of memory");
        return 0;
    }
    memcpy(out, got.data(), got.size() * sizeof *out);
    *pages = out;
    return static_cast<int64_t>(got.size());
}

int64_t h_collect_written(uint32_t **pages) {
    Player &p = *g_player;
    const std::vector<uint32_t> got(p.pending.begin(), p.pending.end());
    p.pending.clear();
    return emit_pages(got, pages);
}

int64_t h_collect_written_in(const uint64_t (*ranges)[2], int n, uint32_t **pages) {
    Player &p = *g_player;
    std::vector<uint32_t> got;
    for (auto it = p.pending.begin(); it != p.pending.end();) {
        bool in = false;
        for (int r = 0; r < n && !in; r++)
            in = *it >= ranges[r][0] / kPageBytes && *it < (ranges[r][1] + kPageBytes - 1) / kPageBytes;
        if (in) {
            got.push_back(*it);
            it = p.pending.erase(it);
        } else {
            ++it;
        }
    }
    return emit_pages(got, pages);
}

void h_free_pages(uint32_t *pages) { free(pages); }

void h_mark_written(const void *mem, uint64_t n) {
    Player &p = *g_player;
    const uint64_t at = addr_of(mem), base = addr_of(p.window);
    if (n && p.window && at >= base && at - base < p.window_size)
        p.mark(at - base, std::min(n, p.window_size - (at - base)));
}

uint64_t h_ticks(void) { return value_of(RXC_HOST_TICKS, nullptr); }

int64_t h_now_ns(void) { return static_cast<int64_t>(value_of(RXC_HOST_NOW_NS, nullptr)); }

void h_tex_layout(const nvn_texdesc *d, nvn_texlayout *o) {
    const uint64_t a[2] = {addr_of(d), addr_of(o)};
    value_of(RXC_HOST_TEX_LAYOUT, a);
}

uint64_t h_tex_level_offset(const nvn_texdesc *d, unsigned level) {
    const uint64_t a[2] = {addr_of(d), level};
    return value_of(RXC_HOST_TEX_LEVEL_OFFSET, a);
}

int h_fmt_bpb(int format, int *bw, int *bh) {
    const uint64_t a[3] = {sx(format), addr_of(bw), addr_of(bh)};
    return static_cast<int>(value_of(RXC_HOST_FMT_BPB, a));
}

void h_tex_level_tiles(const nvn_texdesc *d, unsigned level, unsigned *tile_h, unsigned *tile_d) {
    const uint64_t a[4] = {addr_of(d), level, addr_of(tile_h), addr_of(tile_d)};
    value_of(RXC_HOST_TEX_LEVEL_TILES, a);
}

uint32_t h_tex_linear_stride(const nvn_texdesc *d) {
    const uint64_t a[1] = {addr_of(d)};
    return static_cast<uint32_t>(value_of(RXC_HOST_TEX_LINEAR_STRIDE, a));
}

int h_depth_minus_w(void) { return static_cast<int>(value_of(RXC_HOST_DEPTH_MINUS_W, nullptr)); }

/* the POOL records replayed so far, at most max */
int h_gpu_pools(uint64_t (*out)[2], int max) {
    int n = 0;
    for (const auto &pool : g_player->pools) {
        if (n >= max) break;
        out[n][0] = pool[0];
        out[n][1] = pool[1];
        n++;
    }
    return n;
}

uint32_t h_maps_generation(void) { return static_cast<uint32_t>(value_of(RXC_HOST_MAPS_GENERATION, nullptr)); }

uint32_t h_vmap_generation(void) { return static_cast<uint32_t>(value_of(RXC_HOST_VMAP_GENERATION, nullptr)); }

uint64_t h_reserved_cpus(void) { return value_of(RXC_HOST_RESERVED_CPUS, nullptr); }

uint64_t h_guest_alloc(uint64_t size) {
    const uint64_t a[1] = {size};
    return value_of(RXC_HOST_GUEST_ALLOC, a);
}

void h_watch_guest_writes(uint64_t, uint64_t) {}

uint32_t h_pad_raw(void) { return static_cast<uint32_t>(value_of(RXC_HOST_PAD_RAW, nullptr)); }

int h_binding(int set, int button, int slot) {
    const uint64_t a[3] = {sx(set), sx(button), sx(slot)};
    return static_cast<int>(value_of(RXC_HOST_BINDING, a));
}

void h_bind(int, int, int, int) {}

float h_aspect_ratio(void) { return float_of(value_of(RXC_HOST_ASPECT_RATIO, nullptr)); }

int h_take_key_press(int key) {
    const uint64_t a[1] = {sx(key)};
    return static_cast<int>(value_of(RXC_HOST_TAKE_KEY_PRESS, a));
}

int h_pointer(MkwVkPointer *out) {
    const uint64_t a[1] = {addr_of(out)};
    return static_cast<int>(value_of(RXC_HOST_POINTER, a));
}

int h_mic_count(void) { return static_cast<int>(value_of(RXC_HOST_MIC_COUNT, nullptr)); }

const char *h_mic_name(int index) {
    const uint64_t a[1] = {sx(index)};
    return g_player->text_of(g_player->ask(RXC_HOST_MIC_NAME, a));
}

void h_mic_select(int, const char *, int) {}

int h_gyro_status(void) { return static_cast<int>(value_of(RXC_HOST_GYRO_STATUS, nullptr)); }

void h_log_flush(void) {}

int h_prompt_face(void) { return static_cast<int>(value_of(RXC_HOST_PROMPT_FACE, nullptr)); }

const char *h_romfs_dir(void) { return g_player->text_of(g_player->ask(RXC_HOST_ROMFS_DIR, nullptr)); }

float h_wide_menus(uint32_t keep_mask) {
    const uint64_t a[1] = {keep_mask};
    return float_of(value_of(RXC_HOST_WIDE_MENUS, a));
}

void h_slept_ns(int64_t out[2]) {
    const uint64_t a[1] = {addr_of(out)};
    value_of(RXC_HOST_SLEPT_NS, a);
}

void h_fps_timing(int) {}

int h_prompts_live(void) { return static_cast<int>(value_of(RXC_HOST_PROMPTS_LIVE, nullptr)); }

int h_screen_size(int *width, int *height) {
    const uint64_t a[2] = {addr_of(width), addr_of(height)};
    return static_cast<int>(value_of(RXC_HOST_SCREEN_SIZE, a));
}

float h_wide_cover(void) { return float_of(value_of(RXC_HOST_WIDE_COVER, nullptr)); }

const MkwVkHost kHost = {
    .version = MKWVK_API_VERSION,
    .log = h_log,
    .guest_base = h_guest_base,
    .guest_size = h_guest_size,
    .gpu_ptr = h_gpu_ptr,
    .gpu_guest = h_gpu_guest,
    .gpu_read = h_gpu_read,
    .gpu_write = h_gpu_write,
    .collect_written = h_collect_written,
    .free_pages = h_free_pages,
    .mark_written = h_mark_written,
    .ticks = h_ticks,
    .now_ns = h_now_ns,
    .tex_layout = h_tex_layout,
    .tex_level_offset = h_tex_level_offset,
    .fmt_bpb = h_fmt_bpb,
    .tex_level_tiles = h_tex_level_tiles,
    .tex_linear_stride = h_tex_linear_stride,
    .depth_minus_w = h_depth_minus_w,
    .gpu_pools = h_gpu_pools,
    .maps_generation = h_maps_generation,
    .vmap_generation = h_vmap_generation,
    .reserved_cpus = h_reserved_cpus,
    .collect_written_in = h_collect_written_in,
    .guest_alloc = h_guest_alloc,
    .watch_guest_writes = h_watch_guest_writes,
    .pad_raw = h_pad_raw,
    .binding = h_binding,
    .bind = h_bind,
    .aspect_ratio = h_aspect_ratio,
    .take_key_press = h_take_key_press,
    .pointer = h_pointer,
    .mic_count = h_mic_count,
    .mic_name = h_mic_name,
    .mic_select = h_mic_select,
    .gyro_status = h_gyro_status,
    .log_flush = h_log_flush,
    .prompt_face = h_prompt_face,
    .romfs_dir = h_romfs_dir,
    .wide_menus = h_wide_menus,
    .slept_ns = h_slept_ns,
    .fps_timing = h_fps_timing,
    .prompts_live = h_prompts_live,
    .screen_size = h_screen_size,
    .wide_cover = h_wide_cover,
};

/* ---- the replay's entry: the renderer is given the host table, started, and given the boundary calls ---- */
bool Player::play(mkwvk_init_fn init, void *win) {
    size_t first = 0;
    while (first < recs.size() && !is_boundary(recs[first].kind)) first++;
    /* the state before the renderer starts is the records before the first call's own (the OBJECT, POOL, PAGES and GUEST
       records just before it, which describe what that call makes): the renderer starts before the first call */
    size_t start = first;
    while (start > 0 && (recs[start - 1].kind == RXC_OBJECT || recs[start - 1].kind == RXC_POOL ||
                         recs[start - 1].kind == RXC_PAGES || recs[start - 1].kind == RXC_GUEST))
        start--;
    ref = first;
    for (size_t i = 0; i < start; i++)
        if (!apply(i)) return false;
    g_player = this;
    const bool ok = run(init, win, start);
    g_player = nullptr;
    return ok;
}

/* the play's end, when it ran to the end: one line on stderr naming every host question kind, with how many questions
   of it found no recorded answer */
void Player::report_unanswered() const {
    std::string counts;
    for (uint32_t kind = RXC_HOST_LOG; kind <= RXC_HOST_WIDE_COVER; kind++) {
        if (!is_question(kind)) continue;
        counts += std::string(counts.empty() ? "" : ", ") + kHostNames[kind - RXC_HOST_LOG] + " " +
                  std::to_string(unanswered[kind - RXC_HOST_LOG].load(std::memory_order_relaxed));
    }
    fprintf(stderr, "rx_player: unanswered host questions: %s\n", counts.c_str());
}

/* the play's end, when it ran to the end: one line on stderr with where the load went. The marks are rounded down to
   whole milliseconds from the player's start, so the reading, the index and the renderer add up to the load line's
   time */
void Player::report_phases() const {
    const long long read_at = ms_since_start(read_done), index_at = ms_since_start(index_done),
                    renderer_at = ms_since_start(started), snapshot_at = ms_since_start(snapshot_done);
    fprintf(stderr, "rx_player: load phases: read %lld ms, index %lld ms, renderer %lld ms, snapshot %lld ms\n",
            read_at, index_at - read_at, renderer_at - index_at, snapshot_at - renderer_at);
}

/* the bytes the capture holds on disk: calls.rxc, and each blob once at the size the records that name it give it (the
   blobs are the contents the records name, each stored once: check_blobs) */
uint64_t Player::stored_bytes() const {
    uint64_t total = file.size();
    std::set<Hash> named;
    const auto add = [&named, &total](const Hash &h, uint64_t size) {
        if (named.insert(h).second) total += size;
    };
    for (const Rec &r : recs) {
        const uint8_t *p = payload(r);
        if (r.kind == RXC_OBJECT || r.kind == RXC_GUEST) {
            add(hash_at(p + 16), rd64(p + 8));
        } else if (r.kind == RXC_POOL) {
            for (uint64_t k = 0; k < (payload_size(r) - 16) / 32; k++) add(hash_at(p + 16 + 32 * k), kPageBytes);
        } else if (r.kind == RXC_PAGES) {
            for (uint64_t k = 0; k < rd64(p); k++) add(hash_at(p + 16 + 40 * k), kPageBytes);
        }
    }
    return total;
}

bool Player::run(mkwvk_init_fn init, void *win, size_t start) {
    index_done = std::chrono::steady_clock::now();
    const MkwVkRenderer *r = init(&kHost);
    if (!r) return fail("the renderer's mkwvk_init gave no table");
    if (r->version != MKWVK_API_VERSION)
        return fail("version mismatch: the renderer is MKWVK_API_VERSION " + std::to_string(r->version) +
                    ", this player is " + std::to_string(MKWVK_API_VERSION));
    if (!r->start(win)) return fail("the renderer did not start");
    started = std::chrono::steady_clock::now();
    snapshot_done = started;   /* the snapshot's time is 0 unless a SNAPSHOT is replayed below */
    bool ok = true;
    for (size_t i = start; i < recs.size() && ok; i++) {
        const uint32_t k = recs[i].kind;
        if (is_boundary(k)) {
            ref = i;
            ok = flush() && replay(r, i);
        } else {
            ok = apply(i);
        }
        if (k == RXC_SNAPSHOT) snapshot_done = std::chrono::steady_clock::now();
    }
    r->nvn_finish();
    if (!ok || failed()) return false;
    report_unanswered();
    return true;
}

/* the folder the executable is in */
std::string exe_dir() {
    char buf[4096];
    const ssize_t n = readlink("/proc/self/exe", buf, sizeof buf - 1);
    if (n <= 0) return ".";
    buf[n] = 0;
    const std::string path(buf);
    const size_t slash = path.rfind('/');
    return slash == std::string::npos ? "." : path.substr(0, slash);
}

}  // namespace

/* the window's memfd, found by the renderer's dlsym (vk_memory.cpp import_dma_buf), as the game's executable exports it
   (host/hos_main.c; hx play links the player with -Wl,--export-dynamic-symbol=hos_posix_*). -1: anonymous memory */
extern "C" int hos_posix_guest_memfd(void) { return guest_memfd; }

int rx_play(const char *capture_dir, mkwvk_init_fn init, void *window) {
    Player p(capture_dir);
    const bool ok = p.load() && p.play(init, window);
    if (!ok) {
        fprintf(stderr, "rx_player: %s\n", p.failure().c_str());
        return 1;
    }
    fprintf(stderr, "rx_player: loaded %llu bytes in %lld ms\n", static_cast<unsigned long long>(p.stored_bytes()),
            p.ms_since_start(p.started));
    p.report_phases();
    return 0;
}

int main(int argc, char **argv) {
    const char *dir = nullptr;
    std::string lib;
    bool usage = false;
    for (int i = 1; i < argc && !usage; i++) {
        if (!strcmp(argv[i], "--renderer")) {
            if (i + 1 >= argc || !lib.empty()) usage = true;
            else lib = argv[++i];
        } else if (argv[i][0] == '-' || dir) {
            usage = true;
        } else {
            dir = argv[i];
        }
    }
    if (!dir || usage) {
        fprintf(stderr, "usage: rx_player <capture_dir> [--renderer <library>]\n");
        return 2;
    }
    if (lib.empty()) lib = exe_dir() + "/hookshot_vk.so";
    void *handle = dlopen(lib.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        fprintf(stderr, "rx_player: cannot load the renderer %s: %s\n", lib.c_str(), dlerror());
        return 1;
    }
    void *init = dlsym(handle, "mkwvk_init");
    if (!init) {
        fprintf(stderr, "rx_player: %s has no mkwvk_init\n", lib.c_str());
        return 1;
    }
    const int status = rx_play(dir, reinterpret_cast<mkwvk_init_fn>(init), nullptr);
    /* the renderer's threads may still run: end at once, as the game does (ExitProcess in host/posix/win32_posix.c);
       exit() would run static destructors and exit handlers under them */
    fflush(NULL);
    _exit(status);
}
