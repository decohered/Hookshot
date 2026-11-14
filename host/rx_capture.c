/* The recorder of RX_CAPTURE=<dir>: the format and the API are in rx_capture.h.

   Every wrapper does three things in this order. It asks the host for what the record needs (the pools, the sizes of
   the texel buffers, the recording's pointers): those are host calls and read calls, made with no lock held. It takes
   the lock and writes its records (objects, pools, dirty pages, the call). It lets go of the lock and forwards the call
   unchanged. Then, for the answer, it takes the lock again and writes the output objects and the answer. A host call is
   never made with the lock held, and a log line is written only after the lock is let go. */
#include "rx_capture.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include "../gpu/render/rx_nvn.h"

#define ADDR(x) ((uint64_t)(uintptr_t)(x))
#define I(x) ((uint64_t)(int64_t)(x))
#define RXC_MAX_POOLS 4096
#define RXC_PATH 1024
#define RXC_DIR_MAX 900   /* the folder's name, so that its files' names fit RXC_PATH */

/* ---- state: the lock covers everything below it except the host and renderer tables, which are set once ---- */
static SRWLOCK lock = SRWLOCK_INIT;
static int active;                       /* a capture is writing (read and written under the lock, atomically) */
static FILE *calls;                      /* <dir>/calls.rxc */
static char dir[RXC_PATH];               /* the folder */
static const MkwVkHost *real_host;       /* the host as the executable gave it */
static const MkwVkRenderer *real_rend;   /* the renderer as the executable gave it */
static const uint8_t *marks;             /* the soft write watch's marks: read, never written */
static uint64_t marks_n;
static MkwVkHost wrapped_host;
static MkwVkRenderer wrapped_rend;
static uint64_t seq;                     /* the last record's sequence number */
static int pools_warned;                 /* (set with an atomic exchange, before the lock) */

/* a table of 32-byte keys: an address (in the first eight bytes) or a blob's SHA-256 */
struct ent {
    uint8_t key[32];
    uint64_t size;      /* an address's last record: its size (0: none yet) */
    uint8_t hash[32];   /* an address's last record: its SHA-256 */
    uint8_t used;
};
struct tab {
    struct ent *e;
    size_t cap, n;
};
static struct tab objects, blobs;        /* the last OBJECT of each address; the blobs written */
static struct tab guests;                /* the last GUEST of each guest address */

/* a list of 64-bit values: pages, or (address, size) pairs */
struct u64s {
    uint64_t *v;
    size_t n, cap;
    int oom;
};
static struct u64s collected;            /* the pages the renderer's collections answered since the last submit */
static char pending[512];                /* a line for host->log, written once the lock is let go */

/* what a wrapper gathered before it took the lock */
struct pre {
    struct u64s objs;                    /* the input objects: (address, size) pairs */
    struct u64s guests;                  /* the guest ranges the call reads: (address, size) pairs */
    int oom;
    int dirty;                           /* a submit: its dirty pages first */
    uint64_t (*pools)[2];
    int npools;
    int pool;                            /* a pool made: its POOL record first */
    uint64_t pool_start, pool_size;
    const uint8_t *base;                 /* the guest memory, for the pools, the dirty pages and the guest ranges */
    uint64_t guest_bytes;
    uint64_t guest_pages;
};

static void put32(uint8_t *p, uint32_t v) {
    for (int i = 0; i < 4; i++) p[i] = (uint8_t)(v >> (8 * i));
}

static void put64(uint8_t *p, uint64_t v) {
    for (int i = 0; i < 8; i++) p[i] = (uint8_t)(v >> (8 * i));
}

static uint64_t float_bits(float f) {
    uint32_t b;
    memcpy(&b, &f, sizeof b);
    return b;
}

/* ---- SHA-256 (FIPS 180-4), of whole buffers ---- */
static const uint32_t K256[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static uint32_t rotr32(uint32_t x, unsigned n) { return (x >> n) | (x << (32 - n)); }

static void sha256_block(uint32_t h[8], const uint8_t *p) {
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
        const uint32_t t1 = hh + s1 + ch + K256[i] + w[i];
        const uint32_t s0 = rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22);
        const uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        const uint32_t t2 = s0 + maj;
        hh = g, g = f, f = e, e = d + t1, d = c, c = b, b = a, a = t1 + t2;
    }
    h[0] += a, h[1] += b, h[2] += c, h[3] += d, h[4] += e, h[5] += f, h[6] += g, h[7] += hh;
}

static void sha256(const uint8_t *data, size_t n, uint8_t out[32]) {
    uint32_t h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    const size_t whole = n - n % 64;
    for (size_t i = 0; i < whole; i += 64) sha256_block(h, data + i);
    uint8_t tail[128] = {0};
    const size_t rest = n - whole;
    memcpy(tail, data + whole, rest);
    tail[rest] = 0x80;
    const size_t len = rest + 1 + 8 <= 64 ? 64 : 128;
    const uint64_t bits = (uint64_t)n * 8;
    for (int i = 0; i < 8; i++) tail[len - 1 - (size_t)i] = (uint8_t)(bits >> (8 * i));
    for (size_t i = 0; i < len; i += 64) sha256_block(h, tail + i);
    for (int i = 0; i < 8; i++) {   /* (the digest is big-endian, unlike the record fields) */
        out[4 * i] = (uint8_t)(h[i] >> 24);
        out[4 * i + 1] = (uint8_t)(h[i] >> 16);
        out[4 * i + 2] = (uint8_t)(h[i] >> 8);
        out[4 * i + 3] = (uint8_t)h[i];
    }
}

static void hex_of(const uint8_t h[32], char out[65]) {
    static const char digits[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {
        out[2 * i] = digits[h[i] >> 4];
        out[2 * i + 1] = digits[h[i] & 15];
    }
    out[64] = 0;
}

/* ---- growable lists and tables ---- */
static void u64s_push(struct u64s *l, uint64_t v) {
    if (l->oom) return;
    if (l->n == l->cap) {
        const size_t cap = l->cap ? 2 * l->cap : 64;
        uint64_t *p = realloc(l->v, cap * sizeof *p);
        if (!p) {
            l->oom = 1;
            return;
        }
        l->v = p;
        l->cap = cap;
    }
    l->v[l->n++] = v;
}

static void objs_add(struct u64s *o, uint64_t addr, uint64_t size) {
    if (!addr || !size) return;
    u64s_push(o, addr);
    u64s_push(o, size);
}

static size_t slot_of(const uint8_t key[32], size_t cap) {
    uint64_t h;
    memcpy(&h, key, 8);
    h *= 0x9E3779B97F4A7C15ull;
    return (size_t)(h ^ (h >> 29)) & (cap - 1);
}

static int tab_grow(struct tab *t) {
    const size_t cap = t->cap ? 2 * t->cap : 256;
    struct ent *e = calloc(cap, sizeof *e);
    if (!e) return 0;
    for (size_t i = 0; i < t->cap; i++) {
        if (!t->e[i].used) continue;
        size_t j = slot_of(t->e[i].key, cap);
        while (e[j].used) j = (j + 1) & (cap - 1);
        e[j] = t->e[i];
    }
    free(t->e);
    t->e = e;
    t->cap = cap;
    return 1;
}

/* the entry of key; with create, made when absent (NULL: out of memory) */
static struct ent *tab_find(struct tab *t, const uint8_t key[32], int create) {
    if (create && (t->n + 1) * 2 > t->cap && !tab_grow(t)) return NULL;
    if (!t->cap) return NULL;
    for (size_t i = slot_of(key, t->cap);; i = (i + 1) & (t->cap - 1)) {
        struct ent *e = &t->e[i];
        if (!e->used) {
            if (!create) return NULL;
            memcpy(e->key, key, 32);
            e->used = 1;
            t->n++;
            return e;
        }
        if (!memcmp(e->key, key, 32)) return e;
    }
}

static void key_addr(uint8_t key[32], uint64_t addr) {
    memset(key, 0, 32);
    put64(key, addr);
}

/* ---- the lock, the failure, the log line ---- */
static int recording(void) { return __atomic_load_n(&active, __ATOMIC_RELAXED); }

static int rec_lock(void) {
    AcquireSRWLockExclusive(&lock);
    if (recording()) return 1;
    ReleaseSRWLockExclusive(&lock);
    return 0;
}

static void rec_unlock(void) {
    char line[sizeof pending];
    line[0] = 0;
    if (pending[0]) {
        memcpy(line, pending, sizeof line);
        pending[0] = 0;
    }
    ReleaseSRWLockExclusive(&lock);
    if (line[0]) real_host->log(line);
}

/* stops the capture: one line says why, once (the lock is held) */
static void stop_locked(const char *what, int err) {
    if (!recording()) return;
    __atomic_store_n(&active, 0, __ATOMIC_RELAXED);
    if (err)
        snprintf(pending, sizeof pending, "rx capture: %s: %s; recording stops, the game goes on", what, strerror(err));
    else
        snprintf(pending, sizeof pending, "rx capture: %s; recording stops, the game goes on", what);
    if (calls) fclose(calls);
    calls = NULL;
}

/* ---- records (the lock is held) ---- */
static uint64_t emit(uint32_t kind, uint64_t thread, uint64_t queue, const uint8_t *payload, size_t n) {
    uint8_t h[32];
    if (n > UINT32_MAX - sizeof h) {
        stop_locked("a record is too large", EOVERFLOW);
        return 0;
    }
    const uint64_t s = seq + 1;
    put32(h, kind);
    put32(h + 4, (uint32_t)(sizeof h + n));
    put64(h + 8, s);
    put64(h + 16, thread);
    put64(h + 24, queue);
    if (fwrite(h, 1, sizeof h, calls) != sizeof h || (n && fwrite(payload, 1, n, calls) != n)) {
        stop_locked("writing calls.rxc", errno);
        return 0;
    }
    seq = s;
    return s;
}

/* the blob of content h (its bytes at data, n long), written once, whole: under a temporary name, then renamed */
static int blob_store(const uint8_t h[32], const uint8_t *data, size_t n) {
    char hex[65], path[RXC_PATH], tmp[RXC_PATH];
    if (tab_find(&blobs, h, 0)) return 1;
    hex_of(h, hex);
    snprintf(path, sizeof path, "%s/blobs/%s", dir, hex);
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    FILE *f = fopen(tmp, "wb");
    if (!f) {
        stop_locked("creating a blob", errno);
        return 0;
    }
    const int wrote = fwrite(data, 1, n, f) == n;
    if (fclose(f) != 0 || !wrote) {
        (void)remove(tmp);   /* (our own temporary file) */
        stop_locked("writing a blob", errno);
        return 0;
    }
    if (!MoveFileExA(tmp, path, MOVEFILE_REPLACE_EXISTING)) {
        (void)remove(tmp);
        stop_locked("renaming a blob", 0);
        return 0;
    }
    if (!tab_find(&blobs, h, 1)) {
        stop_locked("the blob table", ENOMEM);
        return 0;
    }
    return 1;
}

/* the page at src, copied once: the copy is hashed into h and written as the blob named by h (the game's threads may
   write the page meanwhile, so the name must be the hash of the bytes stored) */
static int store_page(const uint8_t *src, uint8_t h[32]) {
    uint8_t page[RXC_PAGE];
    memcpy(page, src, RXC_PAGE);
    sha256(page, RXC_PAGE, h);
    return blob_store(h, page, RXC_PAGE);
}

/* the record of kind (RXC_OBJECT or RXC_GUEST) for [addr, addr + n), its bytes at src, when they differ from the last
   record of that address in t; the range is copied once, and the copy is hashed and written (see store_page) */
static int emit_range(struct tab *t, uint32_t kind, const uint8_t *src, uint64_t addr, uint64_t n, uint64_t thread) {
    uint8_t h[32], key[32], pl[48];
    uint8_t *mem = malloc(n);
    if (!mem) {
        stop_locked("out of memory", ENOMEM);
        return 0;
    }
    memcpy(mem, src, n);
    sha256(mem, n, h);
    key_addr(key, addr);
    struct ent *e = tab_find(t, key, 1);
    if (!e) {
        stop_locked("the record table", ENOMEM);
        free(mem);
        return 0;
    }
    if (e->size == n && !memcmp(e->hash, h, 32)) {
        free(mem);
        return 1;
    }
    const int stored = blob_store(h, mem, n);
    free(mem);
    if (!stored) return 0;
    e->size = n;
    memcpy(e->hash, h, 32);
    put64(pl, addr);
    put64(pl + 8, n);
    memcpy(pl + 16, h, 32);
    return emit(kind, thread, 0, pl, sizeof pl) != 0;
}

/* an OBJECT for [addr, addr + n) of host memory (see emit_range) */
static int emit_object(uint64_t addr, uint64_t n, uint64_t thread) {
    return emit_range(&objects, RXC_OBJECT, (const uint8_t *)(uintptr_t)addr, addr, n, thread);
}

/* a GUEST for [addr, addr + n) of guest memory (see emit_range): the range must lie in the guest memory */
static int emit_guest(const struct pre *p, uint64_t addr, uint64_t n, uint64_t thread) {
    if (addr > p->guest_bytes || n > p->guest_bytes - addr) {
        stop_locked("a guest read is past guest memory", 0);
        return 0;
    }
    return emit_range(&guests, RXC_GUEST, p->base + addr, addr, n, thread);
}

/* a POOL: the pages of [start, start + size), each its SHA-256 (base: the guest memory) */
static int emit_pool(uint64_t start, uint64_t size, const uint8_t *base, uint64_t guest_pages, uint64_t thread) {
    const uint64_t first = start / RXC_PAGE;
    const uint64_t count = size ? (start + size - 1) / RXC_PAGE - first + 1 : 0;
    uint8_t *pl = malloc(16 + 32 * count);
    if (!pl) {
        stop_locked("out of memory", ENOMEM);
        return 0;
    }
    put64(pl, start);
    put64(pl + 8, size);
    for (uint64_t i = 0; i < count; i++) {
        const uint64_t pg = first + i;
        if (pg >= guest_pages) {
            stop_locked("a pool reaches past guest memory", 0);
            free(pl);
            return 0;
        }
        if (!store_page(base + pg * RXC_PAGE, pl + 16 + 32 * i)) {
            free(pl);
            return 0;
        }
    }
    const int ok = emit(RXC_POOL, thread, 0, pl, 16 + 32 * count) != 0;
    free(pl);
    return ok;
}

static int cmp_u64(const void *a, const void *b) {
    const uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}

/* the PAGES of the dirty pages: the pages of the pools whose mark is set now, and the pages the renderer's collections
   answered since the last PAGES, ascending and unique (the marks are read, never written: the renderer still gets
   them) */
static int emit_dirty(const struct pre *p, uint64_t thread) {
    struct u64s all = {0}, pages = {0};
    uint8_t *pl = NULL;
    int ok = 0;
    for (int i = 0; marks && i < p->npools; i++) {
        const uint64_t start = p->pools[i][0], size = p->pools[i][1];
        if (!size) continue;
        for (uint64_t pg = start / RXC_PAGE; pg <= (start + size - 1) / RXC_PAGE && pg < marks_n; pg++) {
            if (__atomic_load_n(&marks[pg], __ATOMIC_RELAXED)) u64s_push(&all, pg);
        }
    }
    for (size_t i = 0; i < collected.n; i++) u64s_push(&all, collected.v[i]);
    collected.n = 0;
    if (all.oom) {
        stop_locked("out of memory", ENOMEM);
        goto done;
    }
    if (all.n) qsort(all.v, all.n, sizeof *all.v, cmp_u64);
    for (size_t i = 0; i < all.n; i++) {
        if (i && all.v[i] == all.v[i - 1]) continue;
        u64s_push(&pages, all.v[i]);
    }
    if (pages.oom) {
        stop_locked("out of memory", ENOMEM);
        goto done;
    }
    const size_t npages = pages.n;
    pl = malloc(8 + npages * 40 + 1);
    if (!pl) {
        stop_locked("out of memory", ENOMEM);
        goto done;
    }
    put64(pl, npages);
    for (size_t i = 0; i < npages; i++) {
        uint8_t *at = pl + 8 + i * 40;
        const uint64_t pg = pages.v[i];
        if (pg >= p->guest_pages) {
            stop_locked("a dirty page is past guest memory", 0);
            goto done;
        }
        put64(at, pg);
        if (!store_page(p->base + pg * RXC_PAGE, at + 8)) goto done;
    }
    ok = emit(RXC_PAGES, thread, 0, pl, 8 + npages * 40) != 0;
done:
    free(pl);
    free(all.v);
    free(pages.v);
    return ok;
}

/* ---- what a call needs before its lock ---- */
static void pre_guest(struct pre *p) {
    p->base = real_host->guest_base();
    p->guest_bytes = real_host->guest_size();
    p->guest_pages = p->guest_bytes / RXC_PAGE;
}

static void pre_pools(struct pre *p) {
    p->pools = malloc(RXC_MAX_POOLS * sizeof *p->pools);
    if (!p->pools) {
        p->oom = 1;
        return;
    }
    p->npools = real_host->gpu_pools(p->pools, RXC_MAX_POOLS);
    if (p->npools == RXC_MAX_POOLS && !__atomic_exchange_n(&pools_warned, 1, __ATOMIC_RELAXED))
        real_host->log("rx capture: the host lists as many memory pools as the capture reads: the rest are not in the "
                       "dirty pages");
}

static void pre_free(struct pre *p) {
    free(p->objs.v);
    free(p->guests.v);
    free(p->pools);
}

static void add_tex(struct u64s *o, const nvn_tex *t) {
    if (!t) return;
    objs_add(o, ADDR(t), sizeof *t);
    if (t->pool) objs_add(o, ADDR(t->pool), sizeof *t->pool);
}

static void add_dpool(struct u64s *o, const nvn_dpool *d) {
    if (!d) return;
    objs_add(o, ADDR(d), sizeof *d);
    if (d->pool) objs_add(o, ADDR(d->pool), sizeof *d->pool);
}

/* the control sections of a program's stages: each stage's RXC_CONTROL_SIZE bytes, which the renderer reads at the
   program's creation and at its binding (gpu/vk/vk_shader.cpp) */
static void add_control_guests(struct u64s *g, const nvn_prog *prog) {
    if (!prog) return;
    for (uint32_t i = 0; i < prog->nstage && i < 6; i++) objs_add(g, prog->sd[i].control, RXC_CONTROL_SIZE);
}

/* the renderer's control_end (gpu/vk/vk_shader.cpp): whether the control section at ctl is one the renderer takes, and
   the end of its code (the u32 at +0x44) */
static int control_end(const uint8_t *ctl, uint32_t *end_out) {
    uint32_t magic, len, c1o, end;
    uint16_t st;
    memcpy(&magic, ctl, 4);
    memcpy(&len, ctl + 0x38, 4);
    memcpy(&c1o, ctl + 0x40, 4);
    memcpy(&end, ctl + 0x44, 4);
    memcpy(&st, ctl + 0x56, 2);
    if (magic != 0x98761234u || !len || end > (16u << 20) || c1o > end || len > end || st > 7) return 0;
    *end_out = end;
    return 1;
}

/* the code a program's creation reads (gpu/vk/vk_shader.cpp, read_code_head): for each stage whose control section the
   renderer takes, the end bytes at its data, when they lie in guest memory (a data past it, such as a virtual pool's
   GPU address, is not kept). The control section is read from p->base before the lock, as the pre-call reads are */
static void add_code_guests(const struct pre *p, struct u64s *g, const nvn_prog *prog) {
    if (!prog) return;
    for (uint32_t i = 0; i < prog->nstage && i < 6; i++) {
        const uint64_t ctl = prog->sd[i].control, data = prog->sd[i].data;
        uint8_t sec[RXC_CONTROL_SIZE];
        uint32_t end = 0;
        if (!ctl || !data || ctl > p->guest_bytes || RXC_CONTROL_SIZE > p->guest_bytes - ctl) continue;
        memcpy(sec, p->base + ctl, sizeof sec);
        if (!control_end(sec, &end) || data > p->guest_bytes || end > p->guest_bytes - data) continue;
        objs_add(g, data, end);
    }
}

/* a count from a record, clamped to the entries of the array it counts (the renderer reads no more than that) */
static uint64_t entries(int32_t n, int32_t max) { return n < 0 ? 0 : n > max ? (uint64_t)max : (uint64_t)n; }

/* the pointers of one record of a recording (its payload at rec + 8): the textures, programs and descriptor pools it
   reaches go to o; the guest bytes it reads by address (a program's control sections, a deferred bind's array) go to g,
   since the pools may not hold them */
static void add_record_pointers(struct u64s *o, struct u64s *g, const uint8_t *rec, uint32_t op) {
    const uint8_t *pl = rec + sizeof(nvn_op_hdr);
    switch (op) {
    case NOP_SET_RT: {
        const nvn_p_set_rt *s = (const nvn_p_set_rt *)pl;
        for (int i = 0; i < s->n && i < 8; i++) add_tex(o, s->color[i]);
        add_tex(o, s->depth);
        break;
    }
    case NOP_BIND_PROGRAM: {
        const nvn_prog *prog = ((const nvn_p_bind_program *)pl)->prog;
        objs_add(o, ADDR(prog), sizeof(nvn_prog));
        add_control_guests(g, prog);
        break;
    }
    case NOP_BIND_BUFS: {
        const nvn_p_bind_bufs *b = (const nvn_p_bind_bufs *)pl;
        objs_add(g, b->src, 16 * entries(b->n, 16));
        break;
    }
    case NOP_BIND_TEX:
    case NOP_BIND_SAMP:
    case NOP_BIND_IMG: {
        const nvn_p_bind_handles *b = (const nvn_p_bind_handles *)pl;
        objs_add(g, b->src, 8 * entries(b->n, 32));
        break;
    }
    case NOP_SET_TEX_POOL:
    case NOP_SET_SAMP_POOL:
        add_dpool(o, ((const nvn_p_set_pool *)pl)->pool);
        break;
    case NOP_COPY_B2T:
    case NOP_COPY_T2B:
        add_tex(o, ((const nvn_p_copy_bt *)pl)->tex);
        break;
    case NOP_COPY_T2T: {
        const nvn_p_copy_tt *c = (const nvn_p_copy_tt *)pl;
        add_tex(o, c->src);
        add_tex(o, c->dst);
        break;
    }
    case NOP_CLEAR_TEX:
        add_tex(o, ((const nvn_p_clear_tex *)pl)->tex);
        break;
    default:
        break;
    }
}

/* a recording, BEGIN to END, walked by each record's size as the replay walks it, and its pointers and guest bytes */
static void add_recording(struct u64s *o, struct u64s *g, uint64_t h) {
    const uint8_t *r = (const uint8_t *)(uintptr_t)h;
    uint64_t n = 0;
    for (;;) {
        nvn_op_hdr hdr;
        memcpy(&hdr, r + n, sizeof hdr);
        if (hdr.size < 8 || (hdr.size & 7) || hdr.op >= NOP_COUNT_) break;
        add_record_pointers(o, g, r + n, hdr.op);
        n += hdr.size;
        if (hdr.op == NOP_END) break;
    }
    objs_add(o, h, n);
}

/* the bytes a texel copy reads or writes: as the replay sizes them (host/rx_backend.c) */
static uint64_t texel_bytes(const nvn_tex *t, const NVNcopyRegion *r, uint64_t row, uint64_t img) {
    if (!t || !r) return 0;
    int bw = 1, bh = 1;
    const int bpb = real_host->fmt_bpb(t->d.format, &bw, &bh);
    if (bpb <= 0 || bw <= 0 || bh <= 0 || r->width <= 0 || r->height <= 0) return 0;
    const uint64_t wb = (uint64_t)((r->width + bw - 1) / bw) * (uint64_t)bpb;
    const uint64_t hb = (uint64_t)((r->height + bh - 1) / bh);
    const uint64_t depth = r->depth > 0 ? (uint64_t)r->depth : 1;
    return (depth - 1) * img + (hb - 1) * row + wb;
}

/* ---- the call and its answer ---- */
/* writes the call's records (the lock taken here): its PAGES, POOL, input OBJECTs and GUEST ranges, then the CALL; 0 when
   not recorded */
static uint64_t call_begin(uint32_t kind, uint64_t queue, const uint64_t *args, size_t nargs, struct pre *p) {
    uint64_t call = 0;
    if (rec_lock()) {
        const uint64_t thread = GetCurrentThreadId();
        int ok = !p->oom && !p->objs.oom && !p->guests.oom;
        if (!ok) stop_locked("out of memory", ENOMEM);
        if (ok && p->dirty) ok = emit_dirty(p, thread) != 0;
        if (ok && p->pool) ok = emit_pool(p->pool_start, p->pool_size, p->base, p->guest_pages, thread);
        for (size_t i = 0; ok && i + 1 < p->objs.n; i += 2) ok = emit_object(p->objs.v[i], p->objs.v[i + 1], thread);
        for (size_t i = 0; ok && i + 1 < p->guests.n; i += 2)
            ok = emit_guest(p, p->guests.v[i], p->guests.v[i + 1], thread);
        if (ok) {
            uint8_t pl[8 * 8];
            for (size_t i = 0; i < nargs; i++) put64(pl + 8 * i, args[i]);
            call = emit(kind, thread, queue, pl, 8 * nargs);
        }
        rec_unlock();
    }
    pre_free(p);
    return call;
}

/* the answer: the output OBJECTs, the pages a collection returned (kept for the next PAGES), then the ANSWER */
static void answer_with(uint32_t kind, uint64_t queue, uint64_t call, uint64_t result, struct u64s *out,
                        const uint32_t *pages, size_t npages) {
    if (call && rec_lock()) {
        const uint64_t thread = GetCurrentThreadId();
        int ok = !out->oom;
        if (!ok) stop_locked("out of memory", ENOMEM);
        for (size_t i = 0; ok && i + 1 < out->n; i += 2) ok = emit_object(out->v[i], out->v[i + 1], thread);
        for (size_t i = 0; ok && i < npages; i++) u64s_push(&collected, pages[i]);
        if (ok && collected.oom) {
            stop_locked("out of memory", ENOMEM);
            ok = 0;
        }
        if (ok) {
            uint8_t pl[16];
            put64(pl, call);
            put64(pl + 8, result);
            emit(kind | RXC_ANSWER, thread, queue, pl, sizeof pl);
        }
        rec_unlock();
    }
    free(out->v);
}

static void answer(uint32_t kind, uint64_t queue, uint64_t call, uint64_t result) {
    struct u64s none = {0};
    answer_with(kind, queue, call, result, &none, NULL, 0);
}

/* the answer of a function whose output is a list of pages (collect_written and collect_written_in) */
static void answer_pages(uint32_t kind, uint64_t call, uint64_t result, uint32_t **pages) {
    struct u64s out = {0};
    const int64_t n = (int64_t)result;
    const uint32_t *list = pages && n > 0 ? *pages : NULL;
    const size_t count = list ? (size_t)n : 0;
    objs_add(&out, ADDR(list), 4 * (uint64_t)count);
    answer_with(kind, 0, call, result, &out, list, count);
}

/* ---- the renderer's boundary (the rx_nvn_* entries): each one records its call, forwards it, then answers ---- */
static void cap_nvn_submit(const uint64_t *handles, int n) {
    const uint64_t a[2] = {ADDR(handles), I(n)};
    struct pre p = {0};
    if (recording()) {
        p.dirty = 1;
        pre_guest(&p);
        pre_pools(&p);
        if (handles && n > 0) {
            objs_add(&p.objs, ADDR(handles), 8 * (uint64_t)n);
            for (int i = 0; i < n; i++) add_recording(&p.objs, &p.guests, handles[i]);
        }
    }
    const uint64_t call = call_begin(RXC_NVN_SUBMIT, 0, a, 2, &p);
    real_rend->nvn_submit(handles, n);
    answer(RXC_NVN_SUBMIT, 0, call, 0);
}

static void cap_nvn_submit_q(const uint64_t *handles, int n, uint64_t queue) {
    const uint64_t a[3] = {ADDR(handles), I(n), queue};
    struct pre p = {0};
    if (recording()) {
        p.dirty = 1;
        pre_guest(&p);
        pre_pools(&p);
        if (handles && n > 0) {
            objs_add(&p.objs, ADDR(handles), 8 * (uint64_t)n);
            for (int i = 0; i < n; i++) add_recording(&p.objs, &p.guests, handles[i]);
        }
    }
    const uint64_t call = call_begin(RXC_NVN_SUBMIT_Q, queue, a, 3, &p);
    real_rend->nvn_submit_q(handles, n, queue);
    answer(RXC_NVN_SUBMIT_Q, queue, call, 0);
}

static uint64_t cap_nvn_fence(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_NVN_FENCE, 0, NULL, 0, &p);
    const uint64_t r = real_rend->nvn_fence();
    answer(RXC_NVN_FENCE, 0, call, r);
    return r;
}

static uint64_t cap_nvn_fence_q(uint64_t queue) {
    const uint64_t a[1] = {queue};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_NVN_FENCE_Q, queue, a, 1, &p);
    const uint64_t r = real_rend->nvn_fence_q(queue);
    answer(RXC_NVN_FENCE_Q, queue, call, r);
    return r;
}

static uint64_t cap_nvn_pushed(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_NVN_PUSHED, 0, NULL, 0, &p);
    const uint64_t r = real_rend->nvn_pushed();
    answer(RXC_NVN_PUSHED, 0, call, r);
    return r;
}

static uint64_t cap_nvn_replayed(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_NVN_REPLAYED, 0, NULL, 0, &p);
    const uint64_t r = real_rend->nvn_replayed();
    answer(RXC_NVN_REPLAYED, 0, call, r);
    return r;
}

static int cap_nvn_wait(uint64_t fence, uint64_t timeout_ns) {
    const uint64_t a[2] = {fence, timeout_ns};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_NVN_WAIT, 0, a, 2, &p);
    const int r = real_rend->nvn_wait(fence, timeout_ns);
    answer(RXC_NVN_WAIT, 0, call, I(r));
    return r;
}

static void cap_wait_writebacks_reading(const void *p, uint64_t n) {
    const uint64_t a[2] = {ADDR(p), n};
    struct pre pre = {0};
    objs_add(&pre.objs, ADDR(p), n);
    const uint64_t call = call_begin(RXC_WAIT_WRITEBACKS_READING, 0, a, 2, &pre);
    real_rend->wait_writebacks_reading(p, n);
    answer(RXC_WAIT_WRITEBACKS_READING, 0, call, 0);
}

static void cap_nvn_finish(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_NVN_FINISH, 0, NULL, 0, &p);
    real_rend->nvn_finish();
    answer(RXC_NVN_FINISH, 0, call, 0);
}

static void cap_nvn_present(const nvn_tex *t, const int crop[4]) {
    const uint64_t a[2] = {ADDR(t), ADDR(crop)};
    struct pre p = {0};
    add_tex(&p.objs, t);
    objs_add(&p.objs, ADDR(crop), 4 * sizeof(int));
    const uint64_t call = call_begin(RXC_NVN_PRESENT, 0, a, 2, &p);
    real_rend->nvn_present(t, crop);
    answer(RXC_NVN_PRESENT, 0, call, 0);
}

static void cap_nvn_register_texture(int id, const nvn_tex *t, const nvn_view *v) {
    const uint64_t a[3] = {I(id), ADDR(t), ADDR(v)};
    struct pre p = {0};
    add_tex(&p.objs, t);
    objs_add(&p.objs, ADDR(v), sizeof *v);
    const uint64_t call = call_begin(RXC_NVN_REGISTER_TEXTURE, 0, a, 3, &p);
    real_rend->nvn_register_texture(id, t, v);
    answer(RXC_NVN_REGISTER_TEXTURE, 0, call, 0);
}

static void cap_nvn_register_sampler(int id, const nvn_samp_par *sp) {
    const uint64_t a[2] = {I(id), ADDR(sp)};
    struct pre p = {0};
    objs_add(&p.objs, ADDR(sp), sizeof *sp);
    const uint64_t call = call_begin(RXC_NVN_REGISTER_SAMPLER, 0, a, 2, &p);
    real_rend->nvn_register_sampler(id, sp);
    answer(RXC_NVN_REGISTER_SAMPLER, 0, call, 0);
}

static void cap_nvn_set_texture_pool_size(int n) {
    const uint64_t a[1] = {I(n)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_NVN_SET_TEXTURE_POOL_SIZE, 0, a, 1, &p);
    real_rend->nvn_set_texture_pool_size(n);
    answer(RXC_NVN_SET_TEXTURE_POOL_SIZE, 0, call, 0);
}

static void cap_nvn_texture_released(const nvn_tex *t) {
    const uint64_t a[1] = {ADDR(t)};
    struct pre p = {0};
    add_tex(&p.objs, t);
    const uint64_t call = call_begin(RXC_NVN_TEXTURE_RELEASED, 0, a, 1, &p);
    real_rend->nvn_texture_released(t);
    answer(RXC_NVN_TEXTURE_RELEASED, 0, call, 0);
}

static void cap_nvn_texture_created(const nvn_tex *t) {
    const uint64_t a[1] = {ADDR(t)};
    struct pre p = {0};
    add_tex(&p.objs, t);
    const uint64_t call = call_begin(RXC_NVN_TEXTURE_CREATED, 0, a, 1, &p);
    real_rend->nvn_texture_created(t);
    answer(RXC_NVN_TEXTURE_CREATED, 0, call, 0);
}

static void cap_nvn_program_released(const nvn_prog *prog) {
    const uint64_t a[1] = {ADDR(prog)};
    struct pre p = {0};
    objs_add(&p.objs, ADDR(prog), sizeof *prog);
    const uint64_t call = call_begin(RXC_NVN_PROGRAM_RELEASED, 0, a, 1, &p);
    real_rend->nvn_program_released(prog);
    answer(RXC_NVN_PROGRAM_RELEASED, 0, call, 0);
}

static void cap_nvn_program_created(const nvn_prog *prog) {
    const uint64_t a[1] = {ADDR(prog)};
    struct pre p = {0};
    if (recording()) {
        pre_guest(&p);
        objs_add(&p.objs, ADDR(prog), sizeof *prog);
        add_control_guests(&p.guests, prog);
        add_code_guests(&p, &p.guests, prog);
    }
    const uint64_t call = call_begin(RXC_NVN_PROGRAM_CREATED, 0, a, 1, &p);
    real_rend->nvn_program_created(prog);
    answer(RXC_NVN_PROGRAM_CREATED, 0, call, 0);
}

static void cap_nvn_read_texels(const nvn_tex *t, unsigned level, unsigned layer0, const NVNcopyRegion *r, uint8_t *dst,
                                uint64_t row_pitch, uint64_t image_pitch) {
    const uint64_t a[7] = {ADDR(t), level, layer0, ADDR(r), ADDR(dst), row_pitch, image_pitch};
    const uint64_t out_n = texel_bytes(t, r, row_pitch, image_pitch);
    struct pre p = {0};
    add_tex(&p.objs, t);
    objs_add(&p.objs, ADDR(r), sizeof *r);
    const uint64_t call = call_begin(RXC_NVN_READ_TEXELS, 0, a, 7, &p);
    real_rend->nvn_read_texels(t, level, layer0, r, dst, row_pitch, image_pitch);
    struct u64s out = {0};
    objs_add(&out, ADDR(dst), out_n);
    answer_with(RXC_NVN_READ_TEXELS, 0, call, 0, &out, NULL, 0);
}

static void cap_nvn_write_texels(const nvn_tex *t, unsigned level, unsigned layer0, const NVNcopyRegion *r,
                                 const uint8_t *src, uint64_t row_pitch, uint64_t image_pitch) {
    const uint64_t a[7] = {ADDR(t), level, layer0, ADDR(r), ADDR(src), row_pitch, image_pitch};
    struct pre p = {0};
    add_tex(&p.objs, t);
    objs_add(&p.objs, ADDR(r), sizeof *r);
    objs_add(&p.objs, ADDR(src), texel_bytes(t, r, row_pitch, image_pitch));
    const uint64_t call = call_begin(RXC_NVN_WRITE_TEXELS, 0, a, 7, &p);
    real_rend->nvn_write_texels(t, level, layer0, r, src, row_pitch, image_pitch);
    answer(RXC_NVN_WRITE_TEXELS, 0, call, 0);
}

static void cap_nvn_invalidate(uint64_t gpu, uint64_t size) {
    const uint64_t a[2] = {gpu, size};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_NVN_INVALIDATE, 0, a, 2, &p);
    real_rend->nvn_invalidate(gpu, size);
    answer(RXC_NVN_INVALIDATE, 0, call, 0);
}

static void cap_nvn_pool_created(uint64_t gpu, uint64_t size) {
    const uint64_t a[2] = {gpu, size};
    struct pre p = {0};
    if (recording()) pre_guest(&p);
    p.pool = 1;
    p.pool_start = gpu;
    p.pool_size = size;
    const uint64_t call = call_begin(RXC_NVN_POOL_CREATED, 0, a, 2, &p);
    real_rend->nvn_pool_created(gpu, size);
    answer(RXC_NVN_POOL_CREATED, 0, call, 0);
}

static void cap_nvn_vmap_changing(uint64_t gpu, uint64_t size) {
    const uint64_t a[2] = {gpu, size};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_NVN_VMAP_CHANGING, 0, a, 2, &p);
    real_rend->nvn_vmap_changing(gpu, size);
    answer(RXC_NVN_VMAP_CHANGING, 0, call, 0);
}

static void cap_nvn_event_signal(uint64_t gpu, uint32_t value) {
    const uint64_t a[2] = {gpu, value};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_NVN_EVENT_SIGNAL, 0, a, 2, &p);
    real_rend->nvn_event_signal(gpu, value);
    answer(RXC_NVN_EVENT_SIGNAL, 0, call, 0);
}

/* ---- the host's questions (MkwVkHost): each one records its call, forwards it, then answers ---- */
static void cap_log(const char *line) {
    const uint64_t a[1] = {ADDR(line)};
    struct pre p = {0};
    if (line) objs_add(&p.objs, ADDR(line), strlen(line) + 1);
    const uint64_t call = call_begin(RXC_HOST_LOG, 0, a, 1, &p);
    real_host->log(line);
    answer(RXC_HOST_LOG, 0, call, 0);
}

static uint8_t *cap_guest_base(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_GUEST_BASE, 0, NULL, 0, &p);
    uint8_t *r = real_host->guest_base();
    answer(RXC_HOST_GUEST_BASE, 0, call, ADDR(r));
    return r;
}

static uint64_t cap_guest_size(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_GUEST_SIZE, 0, NULL, 0, &p);
    const uint64_t r = real_host->guest_size();
    answer(RXC_HOST_GUEST_SIZE, 0, call, r);
    return r;
}

static uint8_t *cap_gpu_ptr(uint64_t gpu, uint64_t *avail) {
    const uint64_t a[2] = {gpu, ADDR(avail)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_GPU_PTR, 0, a, 2, &p);
    uint8_t *r = real_host->gpu_ptr(gpu, avail);
    struct u64s out = {0};
    objs_add(&out, ADDR(avail), sizeof *avail);
    answer_with(RXC_HOST_GPU_PTR, 0, call, ADDR(r), &out, NULL, 0);
    return r;
}

static uint64_t cap_gpu_guest(uint64_t gpu) {
    const uint64_t a[1] = {gpu};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_GPU_GUEST, 0, a, 1, &p);
    const uint64_t r = real_host->gpu_guest(gpu);
    answer(RXC_HOST_GPU_GUEST, 0, call, r);
    return r;
}

static int cap_gpu_read(uint64_t gpu, void *dst, uint64_t n) {
    const uint64_t a[3] = {gpu, ADDR(dst), n};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_GPU_READ, 0, a, 3, &p);
    const int r = real_host->gpu_read(gpu, dst, n);
    struct u64s out = {0};
    objs_add(&out, ADDR(dst), n);
    answer_with(RXC_HOST_GPU_READ, 0, call, I(r), &out, NULL, 0);
    return r;
}

static int cap_gpu_write(uint64_t gpu, const void *src, uint64_t n) {
    const uint64_t a[3] = {gpu, ADDR(src), n};
    struct pre p = {0};
    objs_add(&p.objs, ADDR(src), n);
    const uint64_t call = call_begin(RXC_HOST_GPU_WRITE, 0, a, 3, &p);
    const int r = real_host->gpu_write(gpu, src, n);
    answer(RXC_HOST_GPU_WRITE, 0, call, I(r));
    return r;
}

static int64_t cap_collect_written(uint32_t **pages) {
    const uint64_t a[1] = {ADDR(pages)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_COLLECT_WRITTEN, 0, a, 1, &p);
    const int64_t r = real_host->collect_written(pages);
    answer_pages(RXC_HOST_COLLECT_WRITTEN, call, I(r), pages);
    return r;
}

static void cap_free_pages(uint32_t *pages) {
    const uint64_t a[1] = {ADDR(pages)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_FREE_PAGES, 0, a, 1, &p);
    real_host->free_pages(pages);
    answer(RXC_HOST_FREE_PAGES, 0, call, 0);
}

static void cap_mark_written(const void *mem, uint64_t n) {
    const uint64_t a[2] = {ADDR(mem), n};
    struct pre p = {0};
    objs_add(&p.objs, ADDR(mem), n);
    const uint64_t call = call_begin(RXC_HOST_MARK_WRITTEN, 0, a, 2, &p);
    real_host->mark_written(mem, n);
    answer(RXC_HOST_MARK_WRITTEN, 0, call, 0);
}

static uint64_t cap_ticks(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_TICKS, 0, NULL, 0, &p);
    const uint64_t r = real_host->ticks();
    answer(RXC_HOST_TICKS, 0, call, r);
    return r;
}

static int64_t cap_now_ns(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_NOW_NS, 0, NULL, 0, &p);
    const int64_t r = real_host->now_ns();
    answer(RXC_HOST_NOW_NS, 0, call, I(r));
    return r;
}

static void cap_tex_layout(const nvn_texdesc *d, nvn_texlayout *o) {
    const uint64_t a[2] = {ADDR(d), ADDR(o)};
    struct pre p = {0};
    objs_add(&p.objs, ADDR(d), sizeof *d);
    const uint64_t call = call_begin(RXC_HOST_TEX_LAYOUT, 0, a, 2, &p);
    real_host->tex_layout(d, o);
    struct u64s out = {0};
    objs_add(&out, ADDR(o), sizeof *o);
    answer_with(RXC_HOST_TEX_LAYOUT, 0, call, 0, &out, NULL, 0);
}

static uint64_t cap_tex_level_offset(const nvn_texdesc *d, unsigned level) {
    const uint64_t a[2] = {ADDR(d), level};
    struct pre p = {0};
    objs_add(&p.objs, ADDR(d), sizeof *d);
    const uint64_t call = call_begin(RXC_HOST_TEX_LEVEL_OFFSET, 0, a, 2, &p);
    const uint64_t r = real_host->tex_level_offset(d, level);
    answer(RXC_HOST_TEX_LEVEL_OFFSET, 0, call, r);
    return r;
}

static int cap_fmt_bpb(int format, int *bw, int *bh) {
    const uint64_t a[3] = {I(format), ADDR(bw), ADDR(bh)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_FMT_BPB, 0, a, 3, &p);
    const int r = real_host->fmt_bpb(format, bw, bh);
    struct u64s out = {0};
    objs_add(&out, ADDR(bw), sizeof *bw);
    objs_add(&out, ADDR(bh), sizeof *bh);
    answer_with(RXC_HOST_FMT_BPB, 0, call, I(r), &out, NULL, 0);
    return r;
}

static void cap_tex_level_tiles(const nvn_texdesc *d, unsigned level, unsigned *tile_h, unsigned *tile_d) {
    const uint64_t a[4] = {ADDR(d), level, ADDR(tile_h), ADDR(tile_d)};
    struct pre p = {0};
    objs_add(&p.objs, ADDR(d), sizeof *d);
    const uint64_t call = call_begin(RXC_HOST_TEX_LEVEL_TILES, 0, a, 4, &p);
    real_host->tex_level_tiles(d, level, tile_h, tile_d);
    struct u64s out = {0};
    objs_add(&out, ADDR(tile_h), sizeof *tile_h);
    objs_add(&out, ADDR(tile_d), sizeof *tile_d);
    answer_with(RXC_HOST_TEX_LEVEL_TILES, 0, call, 0, &out, NULL, 0);
}

static uint32_t cap_tex_linear_stride(const nvn_texdesc *d) {
    const uint64_t a[1] = {ADDR(d)};
    struct pre p = {0};
    objs_add(&p.objs, ADDR(d), sizeof *d);
    const uint64_t call = call_begin(RXC_HOST_TEX_LINEAR_STRIDE, 0, a, 1, &p);
    const uint32_t r = real_host->tex_linear_stride(d);
    answer(RXC_HOST_TEX_LINEAR_STRIDE, 0, call, r);
    return r;
}

static int cap_depth_minus_w(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_DEPTH_MINUS_W, 0, NULL, 0, &p);
    const int r = real_host->depth_minus_w();
    answer(RXC_HOST_DEPTH_MINUS_W, 0, call, I(r));
    return r;
}

static int cap_gpu_pools(uint64_t (*out)[2], int max) {
    const uint64_t a[2] = {ADDR(out), I(max)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_GPU_POOLS, 0, a, 2, &p);
    const int r = real_host->gpu_pools(out, max);
    struct u64s o = {0};
    objs_add(&o, ADDR(out), r > 0 ? 16 * (uint64_t)r : 0);
    answer_with(RXC_HOST_GPU_POOLS, 0, call, I(r), &o, NULL, 0);
    return r;
}

static uint32_t cap_maps_generation(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_MAPS_GENERATION, 0, NULL, 0, &p);
    const uint32_t r = real_host->maps_generation();
    answer(RXC_HOST_MAPS_GENERATION, 0, call, r);
    return r;
}

static uint32_t cap_vmap_generation(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_VMAP_GENERATION, 0, NULL, 0, &p);
    const uint32_t r = real_host->vmap_generation();
    answer(RXC_HOST_VMAP_GENERATION, 0, call, r);
    return r;
}

static uint64_t cap_reserved_cpus(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_RESERVED_CPUS, 0, NULL, 0, &p);
    const uint64_t r = real_host->reserved_cpus();
    answer(RXC_HOST_RESERVED_CPUS, 0, call, r);
    return r;
}

static int64_t cap_collect_written_in(const uint64_t (*ranges)[2], int n, uint32_t **pages) {
    const uint64_t a[3] = {ADDR(ranges), I(n), ADDR(pages)};
    struct pre p = {0};
    objs_add(&p.objs, ADDR(ranges), n > 0 ? 16 * (uint64_t)n : 0);
    const uint64_t call = call_begin(RXC_HOST_COLLECT_WRITTEN_IN, 0, a, 3, &p);
    const int64_t r = real_host->collect_written_in(ranges, n, pages);
    answer_pages(RXC_HOST_COLLECT_WRITTEN_IN, call, I(r), pages);
    return r;
}

static uint64_t cap_guest_alloc(uint64_t size) {
    const uint64_t a[1] = {size};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_GUEST_ALLOC, 0, a, 1, &p);
    const uint64_t r = real_host->guest_alloc(size);
    answer(RXC_HOST_GUEST_ALLOC, 0, call, r);
    return r;
}

static void cap_watch_guest_writes(uint64_t guest, uint64_t size) {
    const uint64_t a[2] = {guest, size};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_WATCH_GUEST_WRITES, 0, a, 2, &p);
    real_host->watch_guest_writes(guest, size);
    answer(RXC_HOST_WATCH_GUEST_WRITES, 0, call, 0);
}

static uint32_t cap_pad_raw(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_PAD_RAW, 0, NULL, 0, &p);
    const uint32_t r = real_host->pad_raw();
    answer(RXC_HOST_PAD_RAW, 0, call, r);
    return r;
}

static int cap_binding(int set, int button, int slot) {
    const uint64_t a[3] = {I(set), I(button), I(slot)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_BINDING, 0, a, 3, &p);
    const int r = real_host->binding(set, button, slot);
    answer(RXC_HOST_BINDING, 0, call, I(r));
    return r;
}

static void cap_bind(int set, int button, int slot, int code) {
    const uint64_t a[4] = {I(set), I(button), I(slot), I(code)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_BIND, 0, a, 4, &p);
    real_host->bind(set, button, slot, code);
    answer(RXC_HOST_BIND, 0, call, 0);
}

static float cap_aspect_ratio(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_ASPECT_RATIO, 0, NULL, 0, &p);
    const float r = real_host->aspect_ratio();
    answer(RXC_HOST_ASPECT_RATIO, 0, call, float_bits(r));
    return r;
}

static int cap_take_key_press(int key) {
    const uint64_t a[1] = {I(key)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_TAKE_KEY_PRESS, 0, a, 1, &p);
    const int r = real_host->take_key_press(key);
    answer(RXC_HOST_TAKE_KEY_PRESS, 0, call, I(r));
    return r;
}

static int cap_pointer(MkwVkPointer *out) {
    const uint64_t a[1] = {ADDR(out)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_POINTER, 0, a, 1, &p);
    const int r = real_host->pointer(out);
    struct u64s o = {0};
    objs_add(&o, ADDR(out), sizeof *out);
    answer_with(RXC_HOST_POINTER, 0, call, I(r), &o, NULL, 0);
    return r;
}

static int cap_mic_count(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_MIC_COUNT, 0, NULL, 0, &p);
    const int r = real_host->mic_count();
    answer(RXC_HOST_MIC_COUNT, 0, call, I(r));
    return r;
}

static const char *cap_mic_name(int index) {
    const uint64_t a[1] = {I(index)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_MIC_NAME, 0, a, 1, &p);
    const char *r = real_host->mic_name(index);
    struct u64s out = {0};
    if (r) objs_add(&out, ADDR(r), strlen(r) + 1);
    answer_with(RXC_HOST_MIC_NAME, 0, call, ADDR(r), &out, NULL, 0);
    return r;
}

static void cap_mic_select(int mode, const char *name, int index) {
    const uint64_t a[3] = {I(mode), ADDR(name), I(index)};
    struct pre p = {0};
    if (name) objs_add(&p.objs, ADDR(name), strlen(name) + 1);
    const uint64_t call = call_begin(RXC_HOST_MIC_SELECT, 0, a, 3, &p);
    real_host->mic_select(mode, name, index);
    answer(RXC_HOST_MIC_SELECT, 0, call, 0);
}

static int cap_gyro_status(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_GYRO_STATUS, 0, NULL, 0, &p);
    const int r = real_host->gyro_status();
    answer(RXC_HOST_GYRO_STATUS, 0, call, I(r));
    return r;
}

static void cap_log_flush(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_LOG_FLUSH, 0, NULL, 0, &p);
    real_host->log_flush();
    answer(RXC_HOST_LOG_FLUSH, 0, call, 0);
}

static int cap_prompt_face(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_PROMPT_FACE, 0, NULL, 0, &p);
    const int r = real_host->prompt_face();
    answer(RXC_HOST_PROMPT_FACE, 0, call, I(r));
    return r;
}

static const char *cap_romfs_dir(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_ROMFS_DIR, 0, NULL, 0, &p);
    const char *r = real_host->romfs_dir();
    struct u64s out = {0};
    if (r) objs_add(&out, ADDR(r), strlen(r) + 1);
    answer_with(RXC_HOST_ROMFS_DIR, 0, call, ADDR(r), &out, NULL, 0);
    return r;
}

static float cap_wide_menus(uint32_t keep_mask) {
    const uint64_t a[1] = {keep_mask};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_WIDE_MENUS, 0, a, 1, &p);
    const float r = real_host->wide_menus(keep_mask);
    answer(RXC_HOST_WIDE_MENUS, 0, call, float_bits(r));
    return r;
}

static void cap_slept_ns(int64_t out[2]) {
    const uint64_t a[1] = {ADDR(out)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_SLEPT_NS, 0, a, 1, &p);
    real_host->slept_ns(out);
    struct u64s o = {0};
    objs_add(&o, ADDR(out), 2 * sizeof *out);
    answer_with(RXC_HOST_SLEPT_NS, 0, call, 0, &o, NULL, 0);
}

static void cap_fps_timing(int on) {
    const uint64_t a[1] = {I(on)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_FPS_TIMING, 0, a, 1, &p);
    real_host->fps_timing(on);
    answer(RXC_HOST_FPS_TIMING, 0, call, 0);
}

static int cap_prompts_live(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_PROMPTS_LIVE, 0, NULL, 0, &p);
    const int r = real_host->prompts_live();
    answer(RXC_HOST_PROMPTS_LIVE, 0, call, I(r));
    return r;
}

static int cap_screen_size(int *width, int *height) {
    const uint64_t a[2] = {ADDR(width), ADDR(height)};
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_SCREEN_SIZE, 0, a, 2, &p);
    const int r = real_host->screen_size(width, height);
    struct u64s out = {0};
    objs_add(&out, ADDR(width), sizeof *width);
    objs_add(&out, ADDR(height), sizeof *height);
    answer_with(RXC_HOST_SCREEN_SIZE, 0, call, I(r), &out, NULL, 0);
    return r;
}

static float cap_wide_cover(void) {
    struct pre p = {0};
    const uint64_t call = call_begin(RXC_HOST_WIDE_COVER, 0, NULL, 0, &p);
    const float r = real_host->wide_cover();
    answer(RXC_HOST_WIDE_COVER, 0, call, float_bits(r));
    return r;
}

/* ---- the API ---- */
static void wrap_host(const MkwVkHost *host) {
    wrapped_host = *host;
    if (host->log) wrapped_host.log = cap_log;
    if (host->guest_base) wrapped_host.guest_base = cap_guest_base;
    if (host->guest_size) wrapped_host.guest_size = cap_guest_size;
    if (host->gpu_ptr) wrapped_host.gpu_ptr = cap_gpu_ptr;
    if (host->gpu_guest) wrapped_host.gpu_guest = cap_gpu_guest;
    if (host->gpu_read) wrapped_host.gpu_read = cap_gpu_read;
    if (host->gpu_write) wrapped_host.gpu_write = cap_gpu_write;
    if (host->collect_written) wrapped_host.collect_written = cap_collect_written;
    if (host->free_pages) wrapped_host.free_pages = cap_free_pages;
    if (host->mark_written) wrapped_host.mark_written = cap_mark_written;
    if (host->ticks) wrapped_host.ticks = cap_ticks;
    if (host->now_ns) wrapped_host.now_ns = cap_now_ns;
    if (host->tex_layout) wrapped_host.tex_layout = cap_tex_layout;
    if (host->tex_level_offset) wrapped_host.tex_level_offset = cap_tex_level_offset;
    if (host->fmt_bpb) wrapped_host.fmt_bpb = cap_fmt_bpb;
    if (host->tex_level_tiles) wrapped_host.tex_level_tiles = cap_tex_level_tiles;
    if (host->tex_linear_stride) wrapped_host.tex_linear_stride = cap_tex_linear_stride;
    if (host->depth_minus_w) wrapped_host.depth_minus_w = cap_depth_minus_w;
    if (host->gpu_pools) wrapped_host.gpu_pools = cap_gpu_pools;
    if (host->maps_generation) wrapped_host.maps_generation = cap_maps_generation;
    if (host->vmap_generation) wrapped_host.vmap_generation = cap_vmap_generation;
    if (host->reserved_cpus) wrapped_host.reserved_cpus = cap_reserved_cpus;
    if (host->collect_written_in) wrapped_host.collect_written_in = cap_collect_written_in;
    if (host->guest_alloc) wrapped_host.guest_alloc = cap_guest_alloc;
    if (host->watch_guest_writes) wrapped_host.watch_guest_writes = cap_watch_guest_writes;
    if (host->pad_raw) wrapped_host.pad_raw = cap_pad_raw;
    if (host->binding) wrapped_host.binding = cap_binding;
    if (host->bind) wrapped_host.bind = cap_bind;
    if (host->aspect_ratio) wrapped_host.aspect_ratio = cap_aspect_ratio;
    if (host->take_key_press) wrapped_host.take_key_press = cap_take_key_press;
    if (host->pointer) wrapped_host.pointer = cap_pointer;
    if (host->mic_count) wrapped_host.mic_count = cap_mic_count;
    if (host->mic_name) wrapped_host.mic_name = cap_mic_name;
    if (host->mic_select) wrapped_host.mic_select = cap_mic_select;
    if (host->gyro_status) wrapped_host.gyro_status = cap_gyro_status;
    if (host->log_flush) wrapped_host.log_flush = cap_log_flush;
    if (host->prompt_face) wrapped_host.prompt_face = cap_prompt_face;
    if (host->romfs_dir) wrapped_host.romfs_dir = cap_romfs_dir;
    if (host->wide_menus) wrapped_host.wide_menus = cap_wide_menus;
    if (host->slept_ns) wrapped_host.slept_ns = cap_slept_ns;
    if (host->fps_timing) wrapped_host.fps_timing = cap_fps_timing;
    if (host->prompts_live) wrapped_host.prompts_live = cap_prompts_live;
    if (host->screen_size) wrapped_host.screen_size = cap_screen_size;
    if (host->wide_cover) wrapped_host.wide_cover = cap_wide_cover;
}

static void wrap_renderer(const MkwVkRenderer *r) {
    wrapped_rend = *r;
    if (r->nvn_submit) wrapped_rend.nvn_submit = cap_nvn_submit;
    if (r->nvn_submit_q) wrapped_rend.nvn_submit_q = cap_nvn_submit_q;
    if (r->nvn_fence) wrapped_rend.nvn_fence = cap_nvn_fence;
    if (r->nvn_fence_q) wrapped_rend.nvn_fence_q = cap_nvn_fence_q;
    if (r->nvn_pushed) wrapped_rend.nvn_pushed = cap_nvn_pushed;
    if (r->nvn_replayed) wrapped_rend.nvn_replayed = cap_nvn_replayed;
    if (r->nvn_wait) wrapped_rend.nvn_wait = cap_nvn_wait;
    if (r->wait_writebacks_reading) wrapped_rend.wait_writebacks_reading = cap_wait_writebacks_reading;
    if (r->nvn_finish) wrapped_rend.nvn_finish = cap_nvn_finish;
    if (r->nvn_present) wrapped_rend.nvn_present = cap_nvn_present;
    if (r->nvn_register_texture) wrapped_rend.nvn_register_texture = cap_nvn_register_texture;
    if (r->nvn_register_sampler) wrapped_rend.nvn_register_sampler = cap_nvn_register_sampler;
    if (r->nvn_set_texture_pool_size) wrapped_rend.nvn_set_texture_pool_size = cap_nvn_set_texture_pool_size;
    if (r->nvn_texture_released) wrapped_rend.nvn_texture_released = cap_nvn_texture_released;
    if (r->nvn_texture_created) wrapped_rend.nvn_texture_created = cap_nvn_texture_created;
    if (r->nvn_program_released) wrapped_rend.nvn_program_released = cap_nvn_program_released;
    if (r->nvn_program_created) wrapped_rend.nvn_program_created = cap_nvn_program_created;
    if (r->nvn_read_texels) wrapped_rend.nvn_read_texels = cap_nvn_read_texels;
    if (r->nvn_write_texels) wrapped_rend.nvn_write_texels = cap_nvn_write_texels;
    if (r->nvn_invalidate) wrapped_rend.nvn_invalidate = cap_nvn_invalidate;
    if (r->nvn_pool_created) wrapped_rend.nvn_pool_created = cap_nvn_pool_created;
    if (r->nvn_vmap_changing) wrapped_rend.nvn_vmap_changing = cap_nvn_vmap_changing;
    if (r->nvn_event_signal) wrapped_rend.nvn_event_signal = cap_nvn_event_signal;
}

static int make_dir(const char *path) {
    if (CreateDirectoryA(path, NULL)) return 1;
    return GetLastError() == ERROR_ALREADY_EXISTS;
}

/* one line through the host, for a capture that does not start */
static const MkwVkHost *not_started(const MkwVkHost *host, const char *what, const char *path) {
    char line[RXC_PATH + 128];
    snprintf(line, sizeof line, "rx capture: cannot %s %s: the game goes on without the capture", what, path);
    host->log(line);
    return host;
}

/* the file header, then the table of the host; RX_CAPTURE unset or empty: the host itself */
const MkwVkHost *rx_capture_host(const MkwVkHost *host, const uint8_t *marks_in, uint64_t npages) {
    const char *folder = getenv("RX_CAPTURE");
    if (!folder || !*folder) return host;
    if (real_host) return &wrapped_host;
    if (strlen(folder) > RXC_DIR_MAX) {
        host->log("rx capture: RX_CAPTURE names a folder too long for the capture: the game goes on without it");
        return host;
    }
    snprintf(dir, sizeof dir, "%s", folder);
    char blobs_dir[RXC_PATH], calls_path[RXC_PATH];
    snprintf(blobs_dir, sizeof blobs_dir, "%s/blobs", dir);
    snprintf(calls_path, sizeof calls_path, "%s/calls.rxc", dir);
    if (!make_dir(dir)) return not_started(host, "make", dir);
    if (!make_dir(blobs_dir)) return not_started(host, "make", blobs_dir);
    FILE *f = fopen(calls_path, "wb");
    if (!f) return not_started(host, "open", calls_path);
    uint8_t header[16];
    memcpy(header, "RXCAPT01", 8);
    put32(header + 8, RXC_FORMAT);
    put32(header + 12, MKWVK_API_VERSION);
    if (fwrite(header, 1, sizeof header, f) != sizeof header) {
        fclose(f);
        return not_started(host, "write", calls_path);
    }
    if (atexit(rx_capture_end) != 0) {
        fclose(f);
        return not_started(host, "register the end of", calls_path);
    }
    AcquireSRWLockExclusive(&lock);
    real_host = host;
    marks = marks_in;
    marks_n = npages;
    calls = f;
    seq = 0;
    __atomic_store_n(&active, 1, __ATOMIC_RELAXED);
    ReleaseSRWLockExclusive(&lock);
    wrap_host(host);
    return &wrapped_host;
}

/* the renderer's table, with its pools stored (at start); with no capture, the renderer itself */
const MkwVkRenderer *rx_capture_renderer(const MkwVkRenderer *r) {
    if (!real_host) return r;
    real_rend = r;
    wrap_renderer(r);
    struct pre p = {0};
    if (recording()) {
        pre_guest(&p);
        pre_pools(&p);
    }
    if (rec_lock()) {
        const uint64_t thread = GetCurrentThreadId();
        int ok = !p.oom;
        if (!ok) stop_locked("out of memory", ENOMEM);
        for (int i = 0; ok && i < p.npools; i++)
            ok = emit_pool(p.pools[i][0], p.pools[i][1], p.base, p.guest_pages, thread);
        rec_unlock();
    }
    pre_free(&p);
    return &wrapped_rend;
}

void rx_capture_end(void) {
    if (!rec_lock()) return;
    __atomic_store_n(&active, 0, __ATOMIC_RELAXED);
    if (fflush(calls) != 0 || fclose(calls) != 0)
        snprintf(pending, sizeof pending, "rx capture: closing calls.rxc failed (%s)", strerror(errno));
    calls = NULL;
    rec_unlock();
}
