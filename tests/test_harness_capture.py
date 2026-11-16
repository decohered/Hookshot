"""host/rx_capture.c, the recorder of RX_CAPTURE=<dir> (the format is in host/rx_capture.h), on synthetic data.

A fixture, written to a temporary folder at run time, compiles the real recorder with a stub host and a stub
renderer, the Win32 calls the recorder makes, and drives both wrapped tables. The stubs log every call they are given, per
thread, and the fixture prints the bytes of the objects it passes; the capture is read back with the standard library and
compared with that log. Checked:
  - switch unset or empty: both tables are the originals, and nothing is written;
  - a folder that cannot be made: one line through host->log, and the host comes back;
  - a write that fails: one line through host->log, recording stops, and the calls still go through;
  - switch set, two threads: every boundary call and every host answer is kept in order per thread with its thread, queue
    and arguments; an object is written when its bytes differ from its last record, with those bytes; the pools are kept at
    creation and at start; the dirty pages before each submit are kept by content hash, each content once; the renderer
    gets every marked page, and the marks are still set when the recorder has scanned them;
  - switch set: the guest bytes a submit or a program's creation reads by address (a program's control section and a
    deferred bind's arrays, outside every pool) are kept as GUEST records before the call, once per change, the control
    section of a program created before the first submit that binds it included; a program's creation keeps the code its
    renderer reads there (the end bytes at a stage's data, when the stage's control section passes the renderer's test)
    as a GUEST, and no PAGES;
  - switch unset, through host/rx_backend.c's own start (rx_start, load_vulkan): the renderer is given the back end's
    own host, the calls go to its table, and nothing is written;
  - switch set, through the same start: the renderer is given the recorder's host; each of the 23 renderer entries
    reaches the renderer and is kept in order with its arguments and answers; the renderer's collection through its host
    returns the pages the game marked, and the next submit lists them;
  - a page rewritten by another thread while submits are recorded: every blob is still named by its content's SHA-256;
  - wide_cover (host kind 107, no arguments) is asked by the renderer at each submit and by the survey: each ask is
    recorded as CALL 107 with the stub's float bits as its ANSWER, and the renderer gets the stub's value;
  - switch set, RX_CAPTURE_START, and a host that lists more pools than the recorder asks for first (WIDE_POOLS): the
    start's present makes no renderer call, the snapshot holds a POOL for each, and a page that only a pool past the
    first 4096 reaches, marked after the start, is in the next submit's PAGES;
  - switch set and RX_CAPTURE_START naming a file that is not there yet: nothing is written until the first present that
    finds the file. Then calls.rxc holds the start's records, then the snapshot of the renderer's live state (the pools
    and the pool size, the live textures as they were created, the texture and sampler registrations with the latest
    registration per id, the live programs with their control sections and code; the released ones absent), then the
    present that found the file and every later call, as an unarmed capture keeps them. The host logs one line, that the
    capture started at the present that found the file, counted from the renderer's start over every present; the stub
    gets every call, and no nvn_vmap_changing before that present (the start makes no renderer call: the snapshot reads
    the guest memory as it is); the snapshot holds the host's answers about the live textures before its first texture
    (gpu_guest of each page of the storages outside the listed pools, once; tex_level_offset and tex_level_tiles of each
    level, each with its descriptor as an OBJECT before the CALL); all of this is made by
    the start's present alone, even when another present finds the file at the same time; a texture another thread
    makes while the start asks the host is kept in the snapshot, with no answers of its own;
  - RX_CAPTURE_START set without RX_CAPTURE, or set to an empty string: the capture is as without it;
  - switch set and RX_CAPTURE_PRESENTS=<N>: the capture ends after its N-th present, whose CALL and ANSWER are its last
    records, and the host logs one line that it ended at that present (counted from the renderer's start, or, armed,
    with the start's present as the first); the stub gets every call, the later ones too; unset or empty: no limit; any
    other value: no folder is made, and one line through host->log names the variable.

The player (gpu/player/rx_player.cpp, the executable rx_player and rx_play): the play scenario runs the recorder over
every boundary call with a host survey, and the capture it makes is checked; the player compiles with warnings as
errors, and plays that capture into a stub renderer library (play_stub.c) that prints the same lines the scenario
printed:
  - the stub gets the boundary calls in recorded order with the same value arguments, and each object a pointer reaches
    (recordings, textures, programs, descriptor pools, the regions and the bytes) with the bytes the recorder saw;
  - guest reads return the pool and page bytes, and the recorder's host answers come back (a question asked before a
    call returns its latest recorded answer, the same as the recorder's; a layout question asked before its first
    recorded answer returns that answer, another question returns zero); the player's log names, per host question kind,
    how many questions it found no recorded answer for;
  - a question about an address outside the guest window, in a page the capture answered gpu_guest for (the play
    scenario asks that page past the window, at its base): the stub's gpu_guest at an offset in the page gets the
    page's answer plus the offset, unless the capture recorded an answer for that address itself, which wins; a read or
    write of a range whose every page translates into the window reads or writes the window there, and the write is
    collected as an in-window write is; any other question is answered as before;
  - the renderer's reads by guest address return the live bytes: a control section at the creation of a program made
    before the first submit, a control section and a deferred array at the submit, and the code of a program created
    after its bytes were written outside every pool (the GUEST the player replays before the creation);
  - a play that runs to its end writes one line on stderr with the capture's bytes on disk (calls.rxc and each blob) and
    the player's load time, then one line with where the load went (the reading, the index, the renderer and the
    snapshot times); a stopped or refused play writes neither;
  - the executable exits 0 on the capture, and on a capture with a blob the play never reads missing or altered (a blob
    is read when the play uses its bytes, so one no use names is not checked); a missing blob, or one whose bytes do not
    match its name, that the play reads stops the play at that read, after the boundary calls before it, with one line
    on stderr naming it (a POOL's pages are read together: two damaged ones stop the play at that record, with the lower
    page's blob named); a blob the play has read is kept for the later calls that reach its object, so its file removed
    after that first read does not stop the play, which exits 0 with the stub's lines; another API version exits nonzero
    with one line on stderr naming the cause, before the renderer starts, and a renderer whose table is of another
    version is refused before any call is replayed;
  - the guest memory is made as the game makes its window: with HOS_GUEST_MEMFD=1 the renderer's lookup of
    hos_posix_guest_memfd returns a memfd whose bytes at a guest address are the window's, and with 0 it returns -1 and
    the window is anonymous memory without transparent huge pages; the play's lines are the same either way;
  - the scene scenario, recorded from the start and armed by RX_CAPTURE_START (a file made after a fence, before the
    present that starts the armed capture): the armed capture holds one snapshot, and its play gives the snapshot's
    calls before that present the same objects and guest reads the from-start play gives its calls; the play asks each
    scene texture's level 0 before that present and gets the host's answers (tiles 2, 1) from the snapshot as from the
    recorded calls; from that present on the two plays print the same lines, except each nvn_wait's fence, which the
    fence rule gives (the stub's answer
    to the latest replayed fence whose recorded answer it is, else 0); a second snapshot, a snapshot whose first record
    is not one of the records before it, or one of another length, is refused at load with one line.

python -I -B tests/test_harness_capture.py [--cc clang] [--cxx clang++]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, NoReturn

ROOT = Path(__file__).resolve().parents[1]
RECORDER = ROOT / "host" / "rx_capture.c"
HOST_DIR = ROOT / "host"
POSIX_DIR = ROOT / "host" / "posix"
API_HEADER = ROOT / "gpu" / "vk" / "vk_api.h"
PLAYER_CPP = ROOT / "gpu" / "player" / "rx_player.cpp"

PAGE = 4096
NPAGES = 16
PAT = [0, 1, 2, 3, 0, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]   # guest page p: byte i is (i * 7 + PAT[p]) & 0xff
REWRITE = {3: 0x33}   # page 3's pattern after the fixture rewrites it, before the worker's submits

ANSWER = 0x10000
OBJECT, POOL, PAGES, GUEST, SNAPSHOT = 1, 2, 3, 4, 5
# the guest bytes the fixture's submits and program creation read by address, outside every pool (the fixture's pools
# are [0, 0x4000) and [0x8000, 0xA000), and the scenario makes [0xC000, 0xD000)): a program's control section (page 10)
# and a deferred bind's buffer ranges and handles (page 11)
CONTROL_AT, CONTROL_SIZE = 0xA000, 0xD8
BUFS_AT, BUFS_N = 0xB000, 6
TEX_AT, TEX_N = 0xB100, 4
# the control section of a program the scenarios create before their first submit, which binds it: the renderer reads it
# at the creation, and the submit finds its bytes already kept (page 11, past the deferred arrays). Its stage is 9, past
# the renderer's 7, so the renderer takes no code from it
EARLY_CONTROL_AT = 0xB200
# the code of the program the scenarios create after their submits, which CONTROL_AT's section makes the renderer take:
# CODE_END bytes at CODE_AT (page 13: outside every pool, so no mark and no PAGES lists it)
CODE_AT, CODE_END = 0xD000, 0x80
PLAY_FILL = 0x51   # the code's bytes in the play: written after the last submit
HOST_POOLS = [(0x0, 0x4000), (0x8000, 0x2000)]   # the pools the fixture's host lists (st_gpu_pools)
WIDE_POOLS = 4100   # the pools the wide fixture's host lists (wide_start_mode): 8 bytes at 8 * i, more than 4096
# the host questions the start asks about the live textures, kept in the snapshot before its first texture
ASKED = {"gpu_guest", "tex_level_offset", "tex_level_tiles"}


def control_section(stage: int) -> bytes:
    # a control section as the renderer reads it (gpu/vk/vk_shader.cpp, control_end): the magic at 0, the code length at
    # 0x38 (nonzero, at most the end), the constants' offset at 0x40 (at most the end), the end of the code at 0x44 (at
    # most 16 MB), the stage at 0x56 (at most 7)
    c = bytearray(CONTROL_SIZE)
    c[0x00:0x04] = struct.pack("<I", 0x98761234)
    c[0x38:0x3C] = struct.pack("<I", 0x40)
    c[0x40:0x44] = struct.pack("<I", 0x20)
    c[0x44:0x48] = struct.pack("<I", CODE_END)
    c[0x56:0x58] = struct.pack("<H", stage)
    return bytes(c)


CONTROL_OK, CONTROL_BAD = control_section(0), control_section(9)
BOUNDARY = ["nvn_submit", "nvn_submit_q", "nvn_fence", "nvn_fence_q", "nvn_pushed", "nvn_replayed", "nvn_wait",
            "wait_writebacks_reading", "nvn_finish", "nvn_present", "nvn_register_texture", "nvn_register_sampler",
            "nvn_set_texture_pool_size", "nvn_texture_released", "nvn_texture_created", "nvn_program_released",
            "nvn_program_created", "nvn_read_texels", "nvn_write_texels", "nvn_invalidate", "nvn_pool_created",
            "nvn_vmap_changing", "nvn_event_signal"]
HOST = ["log", "guest_base", "guest_size", "gpu_ptr", "gpu_guest", "gpu_read", "gpu_write", "collect_written",
        "free_pages", "mark_written", "ticks", "now_ns", "tex_layout", "tex_level_offset", "fmt_bpb",
        "tex_level_tiles", "tex_linear_stride", "depth_minus_w", "gpu_pools", "maps_generation", "vmap_generation",
        "reserved_cpus", "collect_written_in", "guest_alloc", "watch_guest_writes", "pad_raw", "binding", "bind",
        "aspect_ratio", "take_key_press", "pointer", "mic_count", "mic_name", "mic_select", "gyro_status", "log_flush",
        "prompt_face", "romfs_dir", "wide_menus", "slept_ns", "fps_timing", "prompts_live", "screen_size", "wide_cover"]
# the host kinds the capture answers as questions (the player's host_desc), in kind order: the log names each of them
QUESTION_KINDS = ["gpu_guest", "gpu_read", "gpu_write", "ticks", "now_ns", "tex_layout", "tex_level_offset", "fmt_bpb",
                  "tex_level_tiles", "tex_linear_stride", "depth_minus_w", "maps_generation", "vmap_generation",
                  "reserved_cpus", "guest_alloc", "pad_raw", "binding", "aspect_ratio", "take_key_press", "pointer",
                  "mic_count", "mic_name", "gyro_status", "prompt_face", "romfs_dir", "wide_menus", "slept_ns",
                  "prompts_live", "screen_size", "wide_cover"]
NAMES = {16 + i: name for i, name in enumerate(BOUNDARY)}
NAMES.update({64 + j: name for j, name in enumerate(HOST)})
# the calls the recorder makes through the host itself: the stubs do not log them, so they are not compared; the fixture
# checks their answers instead
INTERNAL = {"guest_base", "guest_size", "gpu_pools", "fmt_bpb"}
QUEUE_ARG = {"nvn_submit_q": 2, "nvn_fence_q": 0}
SUBMITS = ("nvn_submit", "nvn_submit_q")
# the argument positions of the boundary calls that are pointers (their values are not compared; their bytes are)
PLAY_POINTERS = {
    "nvn_submit": (0,), "nvn_submit_q": (0,), "wait_writebacks_reading": (0,), "nvn_present": (0, 1),
    "nvn_register_texture": (1, 2), "nvn_register_sampler": (1,), "nvn_texture_released": (0,),
    "nvn_texture_created": (0,), "nvn_program_released": (0,), "nvn_program_created": (0,),
    "nvn_read_texels": (0, 3, 4), "nvn_write_texels": (0, 3, 4),
}
# the host's own collection and frees: host/rx_backend.c's real code, which logs no line for the stubs to match
REAL_HOST = {"collect_written", "free_pages"}
FENCE_ANSWER = 0x9000   # the play stub's fence answers: the n-th fence call gets FENCE_ANSWER + n (PLAY_SHARED)
BEYOND = "play host gpu_ptr beyond "   # the survey's line for gpu_ptr past the guest window
# the page past the guest window that play_late asks gpu_guest about: the host answers its base with PAGED_AT, in the
# window, and the capture keeps that answer; the player answers the page's other addresses from it (play_paged). One
# address in the page (PAGED_GPU + 0x20) is answered PAGED_OWN, not PAGED_AT + 0x20: its own recorded answer wins
# The page UNMAPPED_GPU, also past the window, answers 0 at its base: the host's unmapped, which translates nothing
PAGED_GPU, PAGED_AT, PAGED_OWN, UNMAPPED_GPU = 0x40000, 0x1000, 0x2000, 0x60000
PAGED_DEFS = "#define PAGED_GPU %#x\n#define PAGED_AT %#x\n#define PAGED_OWN %#x\n#define UNMAPPED_GPU %#x\n" % (
    PAGED_GPU, PAGED_AT, PAGED_OWN, UNMAPPED_GPU)
RACE_ROUNDS = 200   # (the fixture's count)

FWD = re.compile(r"fwd (\d+) (\w+)((?: \d+)*) => (\d+)")
OBJ = re.compile(r"obj (\w+) (\d+) (\d+) ([0-9a-f]+)")
class Record(NamedTuple):
    kind: int
    seq: int
    thread: int
    queue: int
    payload: bytes


class Call(NamedTuple):
    pos: int
    rec: Record
    name: str
    args: list[int]


# the answer to each call, by the call's seq: (record index, the call's kind, the result, the answer's record)
Answers = dict[int, tuple[int, int, int, Record]]

FIXTURE = r"""
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <windows.h>

#include "rx_capture.h"

#define NPAGES 16
#define PAGE 4096
#define P(x) ((uint64_t)(uintptr_t)(x))
#define U(x) ((uint64_t)(x))
#define WIDE_POOLS 4100   /* wide_start_mode: more pools than the recorder asks the host for first */
@PAGED@

static uint8_t guest[NPAGES * PAGE] __attribute__((aligned(4096)));
static uint8_t marks[NPAGES];
static const uint8_t pat[NPAGES] = @PAT@;
static const MkwVkHost *renderer_host;
static const MkwVkHost *wh;
static const MkwVkRenderer *wr;
static int expect_marks;
static const uint8_t *mark_src = marks;   /* the marks the renderer's submits see: the fixture's, or the back end's */
static uint32_t vmap_gen = 1;
static int wide_pools;                   /* wide_start_mode: the host lists WIDE_POOLS pools, not HOST_POOLS */
static int racing_go;                    /* race_start_mode: the start's first page ask starts a second present, once */
static int texture_race_go;              /* texture_race_start_mode: the start's ask of the storage's page makes a texture */
static atomic_int racing_checked;        /* the second present's thread has checked the start file */
static _Thread_local int is_racing;      /* this is the second present's thread */
static pthread_t racing;
static atomic_int failures;
static pthread_mutex_t out_lock = PTHREAD_MUTEX_INITIALIZER;

static void check(int ok, const char *what) {
    if (ok) return;
    atomic_fetch_add(&failures, 1);
    pthread_mutex_lock(&out_lock);
    printf("FAIL: %s\n", what);
    pthread_mutex_unlock(&out_lock);
}

/* what a stub was given, as one line: the thread, the name, the arguments, the result */
static void fwd(const char *name, uint64_t ret, int n, ...) {
    char b[512];
    int k = snprintf(b, sizeof b, "fwd %lu %s", (unsigned long)GetCurrentThreadId(), name);
    va_list ap;
    va_start(ap, n);
    for (int i = 0; i < n; i++) k += snprintf(b + k, sizeof b - (size_t)k, " %llu", (unsigned long long)va_arg(ap, uint64_t));
    va_end(ap);
    snprintf(b + k, sizeof b - (size_t)k, " => %llu\n", (unsigned long long)ret);
    pthread_mutex_lock(&out_lock);
    fputs(b, stdout);
    pthread_mutex_unlock(&out_lock);
}

/* ---- the Win32 calls the recorder makes ---- */
DWORD GetCurrentThreadId(void) {
    static _Thread_local DWORD id;
    static atomic_uint next = 100;
    if (!id) id = atomic_fetch_add(&next, 1);
    return id;
}
static DWORD last_error;
DWORD GetLastError(void) { return last_error; }
BOOL CreateDirectoryA(LPCSTR path, void *attr) {
    (void)attr;
    if (mkdir(path, 0755) == 0) return TRUE;
    last_error = errno == EEXIST ? ERROR_ALREADY_EXISTS : (DWORD)errno;
    return FALSE;
}
BOOL MoveFileExA(LPCSTR from, LPCSTR to, DWORD flags) {
    (void)flags;
    if (rename(from, to) == 0) return TRUE;
    last_error = (DWORD)errno;
    return FALSE;
}
DWORD GetFileAttributesA(LPCSTR path) {
    struct stat st;
    if (is_racing) atomic_store(&racing_checked, 1);
    return stat(path, &st) == 0 ? 0 : INVALID_FILE_ATTRIBUTES;
}

/* ---- the stub host ---- */
static void st_log(const char *line) {
    printf("host-log: %s\n", line);
    fwd("log", 0, 1, P(line));
}
static uint8_t *st_guest_base(void) { return guest; }
static uint64_t st_guest_size(void) { return sizeof guest; }
/* the memory past the guest window, which gpu_ptr answers too (the play check asks for it) */
static uint8_t spare[256];
static uint8_t *st_gpu_ptr(uint64_t gpu, uint64_t *avail) {
    uint8_t *r = NULL;
    uint64_t left = 0;
    if (gpu < sizeof guest) {
        r = guest + gpu;
        left = sizeof guest - gpu;
    } else if (gpu - sizeof guest < sizeof spare) {
        r = spare + (gpu - sizeof guest);
        left = sizeof spare - (gpu - sizeof guest);
    }
    if (avail) *avail = left;
    fwd("gpu_ptr", P(r), 2, U(gpu), P(avail));
    return r;
}
static void *racing_present(void *arg);   /* (below: the second present of race_start_mode) */
static void *texture_racing(void *arg);   /* (below: the texture of texture_race_start_mode) */
static uint64_t st_gpu_guest(uint64_t gpu) {
    if (racing_go) {   /* race_start_mode: a second present checks the start file while the start asks this page */
        racing_go = 0;
        check(pthread_create(&racing, NULL, racing_present, NULL) == 0, "the second present starts");
        for (long n = 0; !atomic_load(&racing_checked) && n < 100000000L; n++) sched_yield();
        check(atomic_load(&racing_checked), "the second present checked the start file");
    }
    if (texture_race_go && gpu == 0xC000) {   /* texture_race_start_mode: a texture is made as the start asks the page */
        texture_race_go = 0;
        check(pthread_create(&racing, NULL, texture_racing, NULL) == 0, "the texture's thread starts");
        check(pthread_join(racing, NULL) == 0, "the texture's thread ends");
    }
    /* the page past the window: its base answers in the window, and one address in it answers its own (PAGED_OWN) */
    uint64_t r = gpu;
    if (gpu == PAGED_GPU) r = PAGED_AT;
    else if (gpu == PAGED_GPU + 0x20) r = PAGED_OWN;
    else if (gpu == UNMAPPED_GPU) r = 0;
    fwd("gpu_guest", r, 1, U(gpu));
    return r;
}
static int st_gpu_read(uint64_t gpu, void *dst, uint64_t n) {
    memcpy(dst, guest + gpu, n);
    fwd("gpu_read", 1, 3, U(gpu), P(dst), U(n));
    return 1;
}
static int st_gpu_write(uint64_t gpu, const void *src, uint64_t n) {
    memcpy(guest + gpu, src, n);
    fwd("gpu_write", 1, 3, U(gpu), P(src), U(n));
    return 1;
}
static int64_t st_collect_written(uint32_t **pages) {
    uint32_t *out = malloc(NPAGES * sizeof *out);
    int64_t n = 0;
    for (uint32_t p = 0; p < NPAGES; p++) {
        if (marks[p]) {
            out[n++] = p;
            marks[p] = 0;
        }
    }
    *pages = out;
    fwd("collect_written", U(n), 1, P(pages));
    return n;
}
static void st_free_pages(uint32_t *pages) {
    free(pages);
    fwd("free_pages", 0, 1, P(pages));
}
static void st_mark_written(const void *p, uint64_t n) { fwd("mark_written", 0, 2, P(p), U(n)); }
static uint64_t st_ticks(void) {
    fwd("ticks", 12345, 0);
    return 12345;
}
static int64_t st_now_ns(void) {
    fwd("now_ns", 678, 0);
    return 678;
}
static void st_tex_layout(const nvn_texdesc *d, nvn_texlayout *o) {
    memset(o, 0, sizeof *o);
    o->size = 4096;
    o->layer_size = 4096;
    o->align = 16;
    o->layers = 1;
    o->bpb = 4;
    o->tileH = 1;
    fwd("tex_layout", 0, 2, P(d), P(o));
}
static uint64_t st_tex_level_offset(const nvn_texdesc *d, unsigned level) {
    const uint64_t r = (uint64_t)level * 256;
    fwd("tex_level_offset", r, 2, P(d), U(level));
    return r;
}
static int st_fmt_bpb(int format, int *bw, int *bh) {   /* (the recorder's own question: not logged) */
    (void)format;
    *bw = 1;
    *bh = 1;
    return 4;
}
static void st_tex_level_tiles(const nvn_texdesc *d, unsigned level, unsigned *tile_h, unsigned *tile_d) {
    *tile_h = 2;
    *tile_d = 1;
    fwd("tex_level_tiles", 0, 4, P(d), U(level), P(tile_h), P(tile_d));
}
static uint32_t st_tex_linear_stride(const nvn_texdesc *d) {
    fwd("tex_linear_stride", 0x100, 1, P(d));
    return 0x100;
}
static int st_depth_minus_w(void) {
    fwd("depth_minus_w", 0, 0);
    return 0;
}
static int st_gpu_pools(uint64_t (*out)[2], int max) {   /* (the recorder's own question: not logged) */
    static const uint64_t pools[2][2] = {{0, 0x4000}, {0x8000, 0x2000}};
    if (wide_pools) {   /* pool i is 8 bytes at 8 * i: the pools from the 4097th on reach page 8, which none before does */
        const int wn = max < WIDE_POOLS ? max : WIDE_POOLS;
        for (int i = 0; i < wn; i++) {
            out[i][0] = 8 * (uint64_t)i;
            out[i][1] = 8;
        }
        return wn;
    }
    const int n = max < 2 ? max : 2;
    for (int i = 0; i < n; i++) {
        out[i][0] = pools[i][0];
        out[i][1] = pools[i][1];
    }
    return n;
}
static uint32_t st_maps_generation(void) {
    fwd("maps_generation", 7, 0);
    return 7;
}
static uint32_t st_vmap_generation(void) {
    fwd("vmap_generation", vmap_gen, 0);
    return vmap_gen;
}
static uint64_t st_reserved_cpus(void) {
    fwd("reserved_cpus", 2, 0);
    return 2;
}
static int64_t st_collect_written_in(const uint64_t (*ranges)[2], int n, uint32_t **pages) {
    uint32_t *out = malloc(NPAGES * sizeof *out);
    int64_t k = 0;
    for (int r = 0; r < n; r++) {
        for (uint64_t p = ranges[r][0] / PAGE; p < (ranges[r][1] + PAGE - 1) / PAGE && p < NPAGES; p++) {
            if (marks[p]) {
                out[k++] = (uint32_t)p;
                marks[p] = 0;
            }
        }
    }
    *pages = out;
    fwd("collect_written_in", U(k), 3, P(ranges), U(n), P(pages));
    return k;
}
static uint64_t st_guest_alloc(uint64_t size) {
    fwd("guest_alloc", 0x40000, 1, U(size));
    return 0x40000;
}
static void st_watch_guest_writes(uint64_t at, uint64_t size) { fwd("watch_guest_writes", 0, 2, U(at), U(size)); }
static uint32_t st_pad_raw(void) {
    fwd("pad_raw", 5, 0);
    return 5;
}
static int st_binding(int set, int button, int slot) {
    const int r = 100 * set + 10 * button + slot;
    fwd("binding", U(r), 3, U(set), U(button), U(slot));
    return r;
}
static void st_bind(int set, int button, int slot, int code) { fwd("bind", 0, 4, U(set), U(button), U(slot), U(code)); }
static float st_aspect_ratio(void) {
    const float f = 1.5f;
    uint32_t bits;
    memcpy(&bits, &f, sizeof bits);
    fwd("aspect_ratio", bits, 0);
    return f;
}
static int st_pointer(MkwVkPointer *out) {
    out->x = 1;
    out->y = 2;
    out->width = 3;
    out->height = 4;
    out->held = 0;
    out->pressed = 0;
    out->wheel = 0;
    fwd("pointer", 1, 1, P(out));
    return 1;
}
static int st_mic_count(void) {
    fwd("mic_count", 1, 0);
    return 1;
}
static const char *st_mic_name(int index) {
    const char *r = "mic0";
    fwd("mic_name", P(r), 1, U(index));
    return r;
}
static void st_mic_select(int mode, const char *name, int index) { fwd("mic_select", 0, 3, U(mode), P(name), U(index)); }
static int st_gyro_status(void) {
    fwd("gyro_status", 2, 0);
    return 2;
}
static void st_log_flush(void) { fwd("log_flush", 0, 0); }
static int st_prompt_face(void) {
    fwd("prompt_face", 1, 0);
    return 1;
}
static const char *st_romfs_dir(void) {
    const char *r = "/romfs";
    fwd("romfs_dir", P(r), 0);
    return r;
}
static float st_wide_menus(uint32_t keep) {
    const float f = 0.5f;
    uint32_t bits;
    memcpy(&bits, &f, sizeof bits);
    fwd("wide_menus", bits, 1, U(keep));
    return f;
}
static float st_wide_cover(void) {
    const float f = 0.25f;
    uint32_t bits;
    memcpy(&bits, &f, sizeof bits);
    fwd("wide_cover", bits, 0);
    return f;
}
static void st_slept_ns(int64_t out[2]) {
    out[0] = 11;
    out[1] = 22;
    fwd("slept_ns", 0, 1, P(out));
}
static void st_fps_timing(int on) { fwd("fps_timing", 0, 1, U(on)); }
static int st_prompts_live(void) {
    fwd("prompts_live", 3, 0);
    return 3;
}
static int st_screen_size(int *width, int *height) {
    *width = 2560;
    *height = 1440;
    fwd("screen_size", 1, 2, P(width), P(height));
    return 1;
}
static const MkwVkHost st_host = {
    .version = MKWVK_API_VERSION,
    .log = st_log,
    .guest_base = st_guest_base,
    .guest_size = st_guest_size,
    .gpu_ptr = st_gpu_ptr,
    .gpu_guest = st_gpu_guest,
    .gpu_read = st_gpu_read,
    .gpu_write = st_gpu_write,
    .collect_written = st_collect_written,
    .free_pages = st_free_pages,
    .mark_written = st_mark_written,
    .ticks = st_ticks,
    .now_ns = st_now_ns,
    .tex_layout = st_tex_layout,
    .tex_level_offset = st_tex_level_offset,
    .fmt_bpb = st_fmt_bpb,
    .tex_level_tiles = st_tex_level_tiles,
    .tex_linear_stride = st_tex_linear_stride,
    .depth_minus_w = st_depth_minus_w,
    .gpu_pools = st_gpu_pools,
    .maps_generation = st_maps_generation,
    .vmap_generation = st_vmap_generation,
    .reserved_cpus = st_reserved_cpus,
    .collect_written_in = st_collect_written_in,
    .guest_alloc = st_guest_alloc,
    .watch_guest_writes = st_watch_guest_writes,
    .pad_raw = st_pad_raw,
    .binding = st_binding,
    .bind = st_bind,
    .aspect_ratio = st_aspect_ratio,
    .take_key_press = NULL,
    .pointer = st_pointer,
    .mic_count = st_mic_count,
    .mic_name = st_mic_name,
    .mic_select = st_mic_select,
    .gyro_status = st_gyro_status,
    .log_flush = st_log_flush,
    .prompt_face = st_prompt_face,
    .romfs_dir = st_romfs_dir,
    .wide_menus = st_wide_menus,
    .slept_ns = st_slept_ns,
    .fps_timing = st_fps_timing,
    .prompts_live = st_prompts_live,
    .screen_size = st_screen_size,
    .wide_cover = st_wide_cover,
};

/* ---- the stub renderer: what it does with the host while it handles a submit ---- */
static int count_marks(void) {
    int n = 0;
    for (int p = 0; p < NPAGES; p++) n += mark_src[p] != 0;
    return n;
}
static void renderer_work(void) {
    uint64_t avail = 0;
    uint8_t buf[16];
    renderer_host->gpu_ptr(0, &avail);
    renderer_host->gpu_read(0, buf, sizeof buf);
    uint32_t *pages = NULL;
    renderer_host->collect_written(&pages);
    renderer_host->free_pages(pages);
    check(renderer_host->wide_cover() == 0.25f, "the renderer gets the host's wide_cover");
}
static int st_start(void *hwnd) {
    (void)hwnd;
    return 1;
}
static void st_submit(const uint64_t *handles, int n) {
    fwd("nvn_submit", 0, 2, P(handles), U(n));
    check(expect_marks < 0 || count_marks() == expect_marks, "the marks are still set when the renderer gets the submit");
    renderer_work();
}
static void st_submit_q(const uint64_t *handles, int n, uint64_t queue) {
    fwd("nvn_submit_q", 0, 3, P(handles), U(n), U(queue));
    check(expect_marks < 0 || count_marks() == expect_marks, "the marks are still set when the renderer gets the queued submit");
    renderer_work();
}
static uint64_t st_fence(void) {
    fwd("nvn_fence", 100, 0);
    return 100;
}
static uint64_t st_fence_q(uint64_t queue) {
    const uint64_t r = queue * 1000;
    fwd("nvn_fence_q", r, 1, U(queue));
    return r;
}
static uint64_t st_pushed(void) {
    fwd("nvn_pushed", 5, 0);
    return 5;
}
static uint64_t st_replayed(void) {
    fwd("nvn_replayed", 4, 0);
    return 4;
}
static int st_wait(uint64_t fence, uint64_t timeout) {
    fwd("nvn_wait", 1, 2, U(fence), U(timeout));
    return 1;
}
static void st_wait_writebacks_reading(const void *p, uint64_t n) { fwd("wait_writebacks_reading", 0, 2, P(p), U(n)); }
static void st_finish(void) { fwd("nvn_finish", 0, 0); }
static void st_present(const nvn_tex *t, const int c4[4]) { fwd("nvn_present", 0, 2, P(t), P(c4)); }
static void st_register_texture(int id, const nvn_tex *t, const nvn_view *v) {
    fwd("nvn_register_texture", 0, 3, U(id), P(t), P(v));
}
static void st_register_sampler(int id, const nvn_samp_par *p) { fwd("nvn_register_sampler", 0, 2, U(id), P(p)); }
static void st_set_texture_pool_size(int n) { fwd("nvn_set_texture_pool_size", 0, 1, U(n)); }
static void st_texture_released(const nvn_tex *t) { fwd("nvn_texture_released", 0, 1, P(t)); }
static void st_texture_created(const nvn_tex *t) { fwd("nvn_texture_created", 0, 1, P(t)); }
static void st_program_released(const nvn_prog *p) { fwd("nvn_program_released", 0, 1, P(p)); }
static void st_program_created(const nvn_prog *p) { fwd("nvn_program_created", 0, 1, P(p)); }
static void st_read_texels(const nvn_tex *t, unsigned level, unsigned layer0, const NVNcopyRegion *r, uint8_t *dst,
                           uint64_t row, uint64_t img) {
    fwd("nvn_read_texels", 0, 7, P(t), U(level), U(layer0), P(r), P(dst), U(row), U(img));
    memset(dst, 0x5A, 16);
}
static void st_write_texels(const nvn_tex *t, unsigned level, unsigned layer0, const NVNcopyRegion *r,
                            const uint8_t *src, uint64_t row, uint64_t img) {
    fwd("nvn_write_texels", 0, 7, P(t), U(level), U(layer0), P(r), P(src), U(row), U(img));
}
static void st_invalidate(uint64_t gpu, uint64_t size) { fwd("nvn_invalidate", 0, 2, U(gpu), U(size)); }
static void st_pool_created(uint64_t gpu, uint64_t size) { fwd("nvn_pool_created", 0, 2, U(gpu), U(size)); }
static void st_vmap_changing(uint64_t gpu, uint64_t size) {
    vmap_gen++;   /* a mapping change, as the game's remaps are */
    fwd("nvn_vmap_changing", 0, 2, U(gpu), U(size));
}
static void st_event_signal(uint64_t gpu, uint32_t value) { fwd("nvn_event_signal", 0, 2, U(gpu), U(value)); }
static const MkwVkRenderer st_rend = {
    .version = MKWVK_API_VERSION,
    .start = st_start,
    .nvn_submit = st_submit,
    .nvn_submit_q = st_submit_q,
    .nvn_fence = st_fence,
    .nvn_fence_q = st_fence_q,
    .nvn_pushed = st_pushed,
    .nvn_replayed = st_replayed,
    .nvn_wait = st_wait,
    .wait_writebacks_reading = st_wait_writebacks_reading,
    .nvn_finish = st_finish,
    .nvn_present = st_present,
    .nvn_register_texture = st_register_texture,
    .nvn_register_sampler = st_register_sampler,
    .nvn_set_texture_pool_size = st_set_texture_pool_size,
    .nvn_texture_released = st_texture_released,
    .nvn_texture_created = st_texture_created,
    .nvn_program_released = st_program_released,
    .nvn_program_created = st_program_created,
    .nvn_read_texels = st_read_texels,
    .nvn_write_texels = st_write_texels,
    .nvn_invalidate = st_invalidate,
    .nvn_pool_created = st_pool_created,
    .nvn_vmap_changing = st_vmap_changing,
    .nvn_event_signal = st_event_signal,
    .shot = NULL,
    .setting = NULL,
};

/* ---- host/rx_backend.c, compiled as it is: the Vulkan back end's start (load_vulkan, rx_start) and its rx_* entries.
   hos.h is not included (it needs the generated a64 headers, which this test does not build): its declarations the file
   uses are here, A64_WRITE_TRACK is on as the build has it, and every host function the file names answers as st_host
   does, so the renderer's host calls are logged. The WebGPU renderer must never be called: its stubs fail. ---- */
#define HOS_H
#define A64_WRITE_TRACK 1
#define A64_DIRTY_PAGES (1ull << 20)
#define GP(a) ((void *)(guest + (a)))
uint8_t a64_dirty[A64_DIRTY_PAGES];
void hos_log(const char *fmt, ...);
void hos_log_flush(void);
uint64_t hos_region_alloc(uint64_t size, uint64_t align);
uint64_t hos_ticks(void);
int64_t hos_now_ns(void);
int hos_vcore_away(void);
void hos_vcore_back(void);

void hos_log(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    printf("hos-log: ");
    vprintf(fmt, ap);
    printf("\n");
    va_end(ap);
}
void hos_log_flush(void) {}
uint64_t hos_region_alloc(uint64_t size, uint64_t align) {
    (void)size, (void)align;
    return 0x40000;
}
uint64_t hos_ticks(void) { return st_ticks(); }
int64_t hos_now_ns(void) { return st_now_ns(); }
int hos_vcore_away(void) { return 0; }
void hos_vcore_back(void) {}
uint8_t *hos_guest_base(void) { return st_guest_base(); }
uint64_t hos_guest_size(void) { return st_guest_size(); }
uint8_t *hos_gpu_ptr(uint64_t va, uint64_t *avail) { return st_gpu_ptr(va, avail); }
uint64_t nvn_gpu_guest(uint64_t gpu) { return st_gpu_guest(gpu); }
int nvn_gpu_read(uint64_t gpu, void *dst, uint64_t n) { return st_gpu_read(gpu, dst, n); }
int nvn_gpu_write(uint64_t gpu, const void *src, uint64_t n) { return st_gpu_write(gpu, src, n); }
int hos_gpu_guest_ranges(uint64_t (*out)[2], int max) {   /* (the pools' ranges, [lo, hi)) */
    static const uint64_t ranges[2][2] = {{0, 0x4000}, {0x8000, 0xA000}};
    const int n = max < 2 ? max : 2;
    for (int i = 0; i < n; i++) out[i][0] = ranges[i][0], out[i][1] = ranges[i][1];
    return n;
}
void hos_mark_written(const void *p, size_t n) { st_mark_written(p, n); }
uint64_t hos_reserved_cpus(void) { return st_reserved_cpus(); }
int hos_gpu_pools(uint64_t (*out)[2], int max) { return st_gpu_pools(out, max); }
uint32_t hos_gpu_maps_generation(void) { return st_maps_generation(); }
uint32_t nvn_vmap_generation(void) { return st_vmap_generation(); }
void nvn_tex_layout(const nvn_texdesc *d, nvn_texlayout *o) { st_tex_layout(d, o); }
uint64_t nvn_tex_level_offset(const nvn_texdesc *d, unsigned level) { return st_tex_level_offset(d, level); }
int nvn_fmt_bpb(int format, int *bw, int *bh) { return st_fmt_bpb(format, bw, bh); }
void nvn_tex_level_tiles(const nvn_texdesc *d, unsigned level, unsigned *tile_h, unsigned *tile_d) {
    st_tex_level_tiles(d, level, tile_h, tile_d);
}
uint32_t nvn_tex_linear_stride(const nvn_texdesc *d) { return st_tex_linear_stride(d); }
int nvn_depth_minus_w(void) { return st_depth_minus_w(); }
uint32_t hos_pad_raw(void) { return st_pad_raw(); }
int hos_binding(int set, int button, int slot) { return st_binding(set, button, slot); }
void hos_bind(int set, int button, int slot, int code) { st_bind(set, button, slot, code); }
int hos_mic_count(void) { return st_mic_count(); }
const char *hos_mic_name(int index) { return st_mic_name(index); }
void hos_mic_select(int mode, const char *name, int index) { st_mic_select(mode, name, index); }
int hos_prompts_face(void) { return st_prompt_face(); }
int hos_prompts_live_parts(void) { return st_prompts_live(); }
int hos_mouse_pointer(MkwVkPointer *out) { return st_pointer(out); }
int hos_sdl_gyro_status(void) { return st_gyro_status(); }
int hos_sdl_desktop_size(int *width, int *height) { return st_screen_size(width, height); }
float hos_wide_cover(void) { return st_wide_cover(); }
void hos_watch_guest_writes(const uint8_t *data, uint64_t size) { (void)data, (void)size; }
float hos_aspect_ratio;
volatile int hos_fps_timing;
volatile int64_t hos_poll_slept_ns, hos_acquire_slept_ns;
_Thread_local int64_t hos_slept_ns[2];
volatile uint32_t hos_wide_keep_mask;
volatile float hos_wide_margin, hos_wide_ratio;
const char *hos_romfs_dir = "/romfs";

/* the WebGPU renderer is never the one chosen once the Vulkan back end starts: a call to it is a failure */
static void wgr_refused(const char *name) {
    atomic_fetch_add(&failures, 1);
    printf("FAIL: the WebGPU renderer was called: %s\n", name);
}
int wgr_start(void *hwnd) { (void)hwnd; wgr_refused("start"); return 0; }
void wgr_resize(int width, int height) { (void)width, (void)height; wgr_refused("resize"); }
const char *wgr_describe(void) { wgr_refused("describe"); return "WebGPU"; }
void wgr_stats(int64_t *draws, int64_t *skipped, int64_t *pipelines) {
    (void)draws, (void)skipped, (void)pipelines;
    wgr_refused("stats");
}
void wgr_nvn_submit(const uint64_t *handles, int n) { (void)handles, (void)n; wgr_refused("nvn_submit"); }
uint64_t wgr_nvn_fence(void) { wgr_refused("nvn_fence"); return 0; }
uint64_t wgr_nvn_pushed(void) { wgr_refused("nvn_pushed"); return 0; }
uint64_t wgr_nvn_replayed(void) { wgr_refused("nvn_replayed"); return 0; }
int wgr_nvn_wait(uint64_t fence, uint64_t timeout_ns) {
    (void)fence, (void)timeout_ns;
    wgr_refused("nvn_wait");
    return 0;
}
void wgr_wait_writebacks_reading(const void *p, uint64_t n) {
    (void)p, (void)n;
    wgr_refused("wait_writebacks_reading");
}
void wgr_nvn_finish(void) { wgr_refused("nvn_finish"); }
void wgr_nvn_present(const nvn_tex *t, const int crop[4]) { (void)t, (void)crop; wgr_refused("nvn_present"); }
void wgr_nvn_register_texture(int id, const nvn_tex *t, const nvn_view *v) {
    (void)id, (void)t, (void)v;
    wgr_refused("nvn_register_texture");
}
void wgr_nvn_register_sampler(int id, const nvn_samp_par *p) {
    (void)id, (void)p;
    wgr_refused("nvn_register_sampler");
}
void wgr_nvn_set_texture_pool_size(int n) { (void)n; wgr_refused("nvn_set_texture_pool_size"); }
void wgr_nvn_texture_released(const nvn_tex *t) { (void)t; wgr_refused("nvn_texture_released"); }
void wgr_nvn_program_released(const nvn_prog *p) { (void)p; wgr_refused("nvn_program_released"); }
void wgr_nvn_read_texels(const nvn_tex *t, unsigned level, unsigned layer0, const NVNcopyRegion *r, uint8_t *dst,
                         uint64_t row, uint64_t img) {
    (void)t, (void)level, (void)layer0, (void)r, (void)dst, (void)row, (void)img;
    wgr_refused("nvn_read_texels");
}
void wgr_nvn_write_texels(const nvn_tex *t, unsigned level, unsigned layer0, const NVNcopyRegion *r,
                          const uint8_t *src, uint64_t row, uint64_t img) {
    (void)t, (void)level, (void)layer0, (void)r, (void)src, (void)row, (void)img;
    wgr_refused("nvn_write_texels");
}
void wgr_nvn_invalidate(uint64_t gpu, uint64_t size) { (void)gpu, (void)size; wgr_refused("nvn_invalidate"); }

/* the Vulkan back end's library: load_vulkan finds mkwvk_init here, and the renderer it makes is st_rend */
static const MkwVkHost *init_host;   /* the host mkwvk_init was given */
static const MkwVkRenderer *st_init(const MkwVkHost *host) {
    init_host = host;
    renderer_host = host;
    return &st_rend;
}
HMODULE LoadLibraryA(LPCSTR name) {
    static char module;
    (void)name;
    return &module;
}
FARPROC GetProcAddress(HMODULE m, LPCSTR name) {
    (void)m;
    return strcmp(name, "mkwvk_init") == 0 ? (FARPROC)(void *)st_init : NULL;
}
DWORD GetModuleFileNameA(HMODULE m, LPSTR out, DWORD cap) {
    (void)m;
    snprintf(out, cap, "%s", "/game/hookshot");
    return (DWORD)strlen(out);
}

#include "rx_backend.c"

/* ---- the game's objects, in static memory, and two recordings of them ---- */
static uint8_t rdst[16], wsrc[16], wdata[8];
static nvn_tex tex0, tex1;
static nvn_pool pool_a, pool_c;
static nvn_dpool dpool0;
static nvn_prog prog0, prog1;
/* the control sections the programs read: prog0's (at 0xA000) passes the renderer's test, prog1's (at 0xB200) has
   stage 9 */
static const uint8_t control_ok[0xd8] = @CONTROL_OK@;
static const uint8_t control_bad[0xd8] = @CONTROL_BAD@;
static NVNcopyRegion region = {0, 0, 0, 4, 1, 1};
static nvn_view view;
static nvn_samp_par samp;
static const int crop[4] = {0, 0, 4, 1};
static uint64_t handles[2];

typedef struct { nvn_op_hdr h; } RecMark;
typedef struct { nvn_op_hdr h; nvn_p_set_rt p; } RecSetRt;
typedef struct { nvn_op_hdr h; nvn_p_bind_program p; } RecBind;
typedef struct { nvn_op_hdr h; nvn_p_set_pool p; } RecPool;
typedef struct { nvn_op_hdr h; nvn_p_copy_tt p; } RecCopy;
typedef struct { nvn_op_hdr h; nvn_p_clear_tex p; } RecClear;
typedef struct { nvn_op_hdr h; nvn_p_bind_bufs p; } RecBufs;
typedef struct { nvn_op_hdr h; nvn_p_bind_handles p; } RecHandles;
typedef struct { RecMark begin; RecSetRt rt; RecBind bind; RecBind bind2; RecBufs bufs; RecHandles tex;
                 RecMark end; } Rec1;
typedef struct { RecMark begin; RecPool pool; RecCopy copy; RecClear clear; RecMark end; } Rec2;
typedef struct { RecMark begin; RecClear clear; RecMark end; } Rec3;
_Static_assert(sizeof(RecMark) == 8 && sizeof(RecSetRt) % 8 == 0 && sizeof(RecBind) % 8 == 0 &&
               sizeof(RecBufs) % 8 == 0 && sizeof(RecHandles) % 8 == 0 &&
               sizeof(RecPool) % 8 == 0 && sizeof(RecCopy) % 8 == 0 && sizeof(RecClear) % 8 == 0,
               "the records are 8-aligned");
static Rec1 rec1;
static Rec2 rec2;
static Rec3 rec3;

static void fill_page(int p, int value) {
    for (int i = 0; i < PAGE; i++) guest[p * PAGE + i] = (uint8_t)(i * 7 + value);
}

static void build(void) {
    for (int p = 0; p < NPAGES; p++) fill_page(p, pat[p]);
    memcpy(guest + 0xA000, control_ok, sizeof control_ok);
    memcpy(guest + 0xB200, control_bad, sizeof control_bad);
    for (int i = 0; i < 16; i++) wsrc[i] = (uint8_t)(0x30 + i);
    for (int i = 0; i < 8; i++) wdata[i] = (uint8_t)(0x40 + i);
    pool_a.magic = 1;
    pool_a.size = 0x4000;
    pool_a.gpu = 0;
    pool_c.magic = 2;
    pool_c.mem = 0xC000;
    pool_c.size = 0x1000;
    pool_c.gpu = 0xC000;
    tex0.magic = 0x7e7;
    tex0.d.width = 4;
    tex0.d.height = 1;
    tex0.d.depth = 1;
    tex0.d.levels = 1;
    tex0.gpu = 0xC000;
    tex0.pool = &pool_c;
    tex0.serial = 1;
    tex1 = tex0;
    tex1.gpu = 0xC100;
    tex1.serial = 2;
    dpool0.pool = &pool_a;
    dpool0.n = 4;
    prog0.magic = 0x9061;
    prog0.nstage = 1;
    prog0.sd[0].data = 0xD000;      /* (page 13: outside every pool) */
    prog0.sd[0].control = 0xA000;   /* (page 10: outside every pool) */
    prog0.serial = 3;
    prog1.magic = 0x9062;
    prog1.nstage = 1;
    prog1.sd[0].data = 0x1000;
    prog1.sd[0].control = 0xB200;   /* (page 11, past the deferred arrays: outside every pool) */
    prog1.serial = 4;
    rec1.begin.h = (nvn_op_hdr){NOP_BEGIN, sizeof(RecMark)};
    rec1.rt.h = (nvn_op_hdr){NOP_SET_RT, sizeof(RecSetRt)};
    rec1.rt.p.n = 1;
    rec1.rt.p.color[0] = &tex0;
    rec1.bind.h = (nvn_op_hdr){NOP_BIND_PROGRAM, sizeof(RecBind)};
    rec1.bind.p.prog = &prog0;
    rec1.bind.p.stages = 1;
    rec1.bind2.h = (nvn_op_hdr){NOP_BIND_PROGRAM, sizeof(RecBind)};
    rec1.bind2.p.prog = &prog1;
    rec1.bind2.p.stages = 1;
    rec1.bufs.h = (nvn_op_hdr){NOP_BIND_BUFS, sizeof(RecBufs)};
    rec1.bufs.p.stage = 5;
    rec1.bufs.p.n = 6;
    rec1.bufs.p.src = 0xB000;   /* (page 11: outside every pool) */
    rec1.tex.h = (nvn_op_hdr){NOP_BIND_TEX, sizeof(RecHandles)};
    rec1.tex.p.stage = 5;
    rec1.tex.p.n = 4;
    rec1.tex.p.src = 0xB100;
    rec1.end.h = (nvn_op_hdr){NOP_END, sizeof(RecMark)};
    rec2.begin.h = (nvn_op_hdr){NOP_BEGIN, sizeof(RecMark)};
    rec2.pool.h = (nvn_op_hdr){NOP_SET_TEX_POOL, sizeof(RecPool)};
    rec2.pool.p.pool = &dpool0;
    rec2.copy.h = (nvn_op_hdr){NOP_COPY_T2T, sizeof(RecCopy)};
    rec2.copy.p.src = &tex0;
    rec2.copy.p.dst = &tex1;
    rec2.copy.p.sr = region;
    rec2.copy.p.dr = region;
    rec2.clear.h = (nvn_op_hdr){NOP_CLEAR_TEX, sizeof(RecClear)};
    rec2.clear.p.tex = &tex1;
    rec2.clear.p.mask = 0xF;
    rec2.end.h = (nvn_op_hdr){NOP_END, sizeof(RecMark)};
    rec3.begin.h = (nvn_op_hdr){NOP_BEGIN, sizeof(RecMark)};
    rec3.clear.h = (nvn_op_hdr){NOP_CLEAR_TEX, sizeof(RecClear)};
    rec3.clear.p.tex = &tex0;
    rec3.clear.p.mask = 0x3;
    rec3.end.h = (nvn_op_hdr){NOP_END, sizeof(RecMark)};
    handles[0] = P(&rec1);
    handles[1] = P(&rec2);
}

static void hexdump(const char *label, const void *p, size_t n) {
    printf("obj %s %llu %zu ", label, (unsigned long long)P(p), n);
    for (size_t i = 0; i < n; i++) printf("%02x", ((const uint8_t *)p)[i]);
    printf("\n");
}
static void print_objects(int k) {
    printf("submit %d\n", k);
    hexdump("handles", handles, sizeof handles);
    hexdump("rec1", &rec1, sizeof rec1);
    hexdump("rec2", &rec2, sizeof rec2);
    hexdump("tex0", &tex0, sizeof tex0);
    hexdump("pool_c", &pool_c, sizeof pool_c);
    hexdump("prog0", &prog0, sizeof prog0);
    hexdump("dpool0", &dpool0, sizeof dpool0);
    hexdump("pool_a", &pool_a, sizeof pool_a);
    hexdump("tex1", &tex1, sizeof tex1);
}

/* the host's answers, from the main thread, once each */
static void survey(void) {
    char line[] = "survey";
    wh->log(line);
    check(wh->guest_base() == guest, "guest_base answers the window");
    check(wh->guest_size() == sizeof guest, "guest_size answers the window");
    uint64_t avail = 0;
    check(wh->gpu_ptr(0x1000, &avail) == guest + 0x1000 && avail == sizeof guest - 0x1000, "gpu_ptr answers the address and its span");
    check(wh->gpu_guest(0x1000) == 0x1000, "gpu_guest answers its argument");
    uint8_t rbuf[8];
    check(wh->gpu_read(0x0, rbuf, sizeof rbuf) == 1 && memcmp(rbuf, guest, sizeof rbuf) == 0, "gpu_read copies the bytes");
    check(wh->gpu_write(0xE000, wdata, sizeof wdata) == 1, "gpu_write writes the bytes");
    uint32_t *pages = NULL;
    check(wh->collect_written(&pages) == 0, "collect_written: no marks yet");
    wh->free_pages(pages);
    wh->mark_written(guest, 16);
    check(wh->ticks() == 12345 && wh->now_ns() == 678, "the clocks");
    nvn_texdesc desc;
    memset(&desc, 0, sizeof desc);
    desc.width = 4;
    desc.height = 1;
    desc.depth = 1;
    desc.levels = 1;
    nvn_texlayout lay;
    memset(&lay, 0, sizeof lay);
    wh->tex_layout(&desc, &lay);
    check(lay.size == 4096, "tex_layout fills its output");
    check(wh->tex_level_offset(&desc, 1) == 256, "tex_level_offset");
    int bw = 0, bh = 0;
    check(wh->fmt_bpb(1, &bw, &bh) == 4 && bw == 1 && bh == 1, "fmt_bpb");
    unsigned tile_h = 0, tile_d = 0;
    wh->tex_level_tiles(&desc, 0, &tile_h, &tile_d);
    check(tile_h == 2 && tile_d == 1, "tex_level_tiles");
    check(wh->tex_linear_stride(&desc) == 0x100 && wh->depth_minus_w() == 0, "tex_linear_stride and depth_minus_w");
    uint64_t pools[4][2];
    check(wh->gpu_pools(pools, 4) == 2 && pools[1][0] == 0x8000, "gpu_pools lists the two pools");
    check(wh->maps_generation() == 7 && wh->vmap_generation() == 1 && wh->reserved_cpus() == 2, "the generations and the cpus");
    uint64_t ranges[1][2] = {{0, 0x4000}};
    uint32_t *pages2 = NULL;
    check(wh->collect_written_in(ranges, 1, &pages2) == 0, "collect_written_in: no marks in the range");
    wh->free_pages(pages2);
    check(wh->guest_alloc(0x1000) == 0x40000, "guest_alloc");
    wh->watch_guest_writes(0, 16);
    check(wh->pad_raw() == 5 && wh->binding(0, 3, 1) == 31, "pad_raw and binding");
    wh->bind(0, 3, 1, 0x41);
    check(wh->aspect_ratio() == 1.5f, "aspect_ratio");
    check(wh->take_key_press == NULL, "take_key_press stays NULL");
    MkwVkPointer ptr;
    check(wh->pointer(&ptr) == 1 && ptr.x == 1, "pointer fills its output");
    check(wh->mic_count() == 1 && strcmp(wh->mic_name(0), "mic0") == 0, "mic_count and mic_name");
    char mic[] = "mic0";
    wh->mic_select(1, mic, 0);
    check(wh->gyro_status() == 2, "gyro_status");
    wh->log_flush();
    check(wh->prompt_face() == 1 && strcmp(wh->romfs_dir(), "/romfs") == 0, "prompt_face and romfs_dir");
    check(wh->wide_menus(3) == 0.5f, "wide_menus");
    int64_t slept[2] = {0, 0};
    wh->slept_ns(slept);
    check(slept[0] == 11 && slept[1] == 22, "slept_ns fills its output");
    wh->fps_timing(0);
    check(wh->prompts_live() == 3, "prompts_live");
    int sw = 0, sh = 0;
    check(wh->screen_size(&sw, &sh) == 1 && sw == 2560 && sh == 1440, "screen_size");
    check(wh->wide_cover() == 0.25f, "wide_cover answers the stub's value");
}

/* the second thread: submits on queue 7 and waits on its fences, while the main thread reads and writes memory */
static void *worker(void *arg) {
    (void)arg;
    for (int i = 0; i < 3; i++) {
        marks[3] = 1;
        expect_marks = 1;
        wr->nvn_submit_q(handles, 2, 7);
        const uint64_t f = wr->nvn_fence_q(7);
        check(f == 7000 && wr->nvn_wait(f, 1000) == 1, "worker: the fence of queue 7 and its wait");
    }
    return NULL;
}

static int on_mode(void) {
    build();
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh != &st_host && wr != &st_rend, "switch set: both tables are the recorder's");
    renderer_host = wh;
    printf("guest %llu\n", (unsigned long long)P(guest));
    survey();
    check(wr->start == st_rend.start && wr->shot == NULL && wr->setting == NULL, "the renderer's own entries are copied and NULL stays NULL");
    wr->nvn_pool_created(0xC000, 0x1000);
    wr->nvn_program_created(&prog1);
    expect_marks = 2;
    marks[2] = 1;
    marks[5] = 1;
    print_objects(1);
    wr->nvn_submit(handles, 2);
    tex0.d.width = 5;
    expect_marks = 0;
    print_objects(2);
    wr->nvn_submit(handles, 2);
    wr->nvn_present(&tex0, crop);
    wr->nvn_register_texture(1, &tex0, &view);
    wr->nvn_register_sampler(2, &samp);
    wr->nvn_set_texture_pool_size(64);
    wr->nvn_texture_created(&tex0);
    wr->nvn_program_created(&prog0);
    wr->nvn_read_texels(&tex0, 0, 0, &region, rdst, 16, 16);
    wr->nvn_write_texels(&tex0, 0, 0, &region, wsrc, 16, 16);
    wr->nvn_texture_released(&tex1);
    wr->nvn_program_released(&prog0);
    wr->wait_writebacks_reading(guest + 0x100, 32);
    wr->nvn_invalidate(0x0, 0x1000);
    wr->nvn_event_signal(0xE000, 7);
    wr->nvn_finish();
    check(wr->nvn_fence() == 100 && wr->nvn_pushed() == 5 && wr->nvn_replayed() == 4, "fence, pushed and replayed answer");
    wr->nvn_vmap_changing(0x0, 0x4000);
    check(wh->vmap_generation() == 2, "one mapping change: the generation went from 1 to 2");
    fill_page(3, 0x33);
    pthread_t t;
    check(pthread_create(&t, NULL, worker, NULL) == 0, "the worker thread starts");
    for (int j = 0; j < 5; j++) {
        check(wh->gpu_write(0x8000, wdata, sizeof wdata) == 1, "gpu_write from the main thread");
        check(wh->gpu_guest(0x8000) == 0x8000, "gpu_guest from the main thread");
        MkwVkPointer ptr;
        check(wh->pointer(&ptr) == 1, "pointer from the main thread");
    }
    pthread_join(t, NULL);
    check(count_marks() == 0, "every mark was taken by the renderer's collections");
    printf("end\n");
    rx_capture_end();
    check(wh->gpu_guest(0x77) == 0x77 && wr->nvn_fence() == 100, "after the end the calls still go through");
    return 0;
}

/* ---- the Vulkan back end started as the executable starts it (rx_start), with the switch set and unset ---- */
static void game_mark(int p) { __atomic_store_n(&a64_dirty[p], 1, __ATOMIC_RELEASE); }   /* (as rt/a64.h's A64_MARK) */

static int hook_on_mode(void) {
    build();
    mark_src = a64_dirty;
    check(rx_start(NULL) == 1, "rx_start chooses the Vulkan back end");
    check(init_host != &vk_host && init_host->guest_base != vk_host.guest_base,
          "the renderer is given the recorder's host, not the back end's own");
    check(vk != &st_rend, "the calls go to the recorder's renderer table");
    rx_nvn_pool_created(0xC000, 0x1000);
    rx_nvn_program_created(&prog1);
    game_mark(2);
    game_mark(9);
    expect_marks = 2;
    rx_nvn_submit(handles, 2);
    game_mark(2);
    game_mark(9);
    uint32_t *pages = NULL;
    const int64_t n = renderer_host->collect_written(&pages);
    check(n == 2 && pages[0] == 2 && pages[1] == 9, "the renderer's collection through its host returns the marked pages");
    renderer_host->free_pages(pages);
    expect_marks = 0;
    rx_nvn_submit_q(handles, 2, 3);
    check(rx_nvn_fence() == 100 && rx_nvn_pushed() == 5 && rx_nvn_replayed() == 4, "fence, pushed and replayed answer");
    check(rx_nvn_fence_q(7) == 7000 && rx_nvn_wait(100, 1000) == 1, "fence_q and wait answer");
    rx_wait_writebacks_reading(guest + 0x100, 32);
    rx_nvn_finish();
    rx_nvn_present(&tex0, crop);
    rx_nvn_register_texture(1, &tex0, &view);
    rx_nvn_register_sampler(2, &samp);
    rx_nvn_set_texture_pool_size(64);
    rx_nvn_texture_created(&tex0);
    rx_nvn_program_created(&prog0);
    rx_nvn_read_texels(&tex0, 0, 0, &region, rdst, 16, 16);
    rx_nvn_write_texels(&tex0, 0, 0, &region, wsrc, 16, 16);
    rx_nvn_texture_released(&tex1);
    rx_nvn_program_released(&prog0);
    rx_nvn_invalidate(0x0, 0x1000);
    rx_nvn_vmap_changing(0x0, 0x4000);
    rx_nvn_event_signal(0xE000, 7);
    rx_capture_end();
    printf("end\n");
    check(rx_nvn_fence() == 100, "after the end the calls still go through");
    return 0;
}

static int hook_off_mode(void) {
    check(rx_start(NULL) == 1, "rx_start chooses the Vulkan back end");
    check(init_host == &vk_host, "switch unset: the renderer is given the back end's own host");
    check(vk == &st_rend, "switch unset: the calls go to the renderer's own table");
    check(rx_nvn_fence() == 100 && rx_nvn_pushed() == 5, "switch unset: the calls go through");
    return 0;
}

/* ---- a page another thread rewrites while submits are recorded: its blobs are still named by their contents ---- */
#define RACE_ROUNDS 200
static int race_done;
static void *racer(void *arg) {
    (void)arg;
    for (uint8_t v = 0; !__atomic_load_n(&race_done, __ATOMIC_ACQUIRE); v++) {
        memset(guest + 2 * PAGE, v, PAGE);   /* the page's content changes under the recorder's reads */
        marks[2] = 1;
    }
    return NULL;
}

static int race_mode(void) {
    build();
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh != &st_host && wr != &st_rend, "the capture started");
    renderer_host = wh;
    expect_marks = -1;   /* (the writer marks page 2 while the submits run: the count is not fixed) */
    pthread_t t;
    check(pthread_create(&t, NULL, racer, NULL) == 0, "the writer thread starts");
    for (int i = 0; i < RACE_ROUNDS; i++) {
        wr->wait_writebacks_reading(guest + 2 * PAGE, PAGE);   /* an OBJECT of the page */
        wr->nvn_pool_created(2 * PAGE, PAGE);                  /* a POOL of it */
        wr->nvn_submit(handles, 2);                            /* its dirty page */
    }
    __atomic_store_n(&race_done, 1, __ATOMIC_RELEASE);
    pthread_join(t, NULL);
    printf("end\n");
    rx_capture_end();
    return 0;
}

static int off_mode(void) {
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh == &st_host && wr == &st_rend, "switch unset: both tables come back as given");
    check(wh->gpu_guest(7) == 7 && wr->nvn_fence() == 100, "the calls go through");
    return 0;
}

static int badpath_mode(void) {
    wh = rx_capture_host(&st_host, marks, NPAGES);
    check(wh == &st_host, "a folder that cannot be made: the host comes back");
    wr = rx_capture_renderer(&st_rend);
    check(wr == &st_rend, "no capture started: the renderer comes back");
    check(wh->gpu_guest(9) == 9, "the calls go through");
    return 0;
}

static int fail_mode(void) {
    const char *cap = getenv("RX_CAPTURE");
    check(cap != NULL, "the folder is given");
    build();
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh != &st_host && wr != &st_rend, "the capture started");
    renderer_host = wh;
    wr->nvn_pool_created(0xC000, 0x1000);
    char from[512], to[512];
    snprintf(from, sizeof from, "%s/blobs", cap);
    snprintf(to, sizeof to, "%s/blobs.away", cap);
    check(rename(from, to) == 0, "the blobs folder is moved away");
    expect_marks = 0;
    wr->nvn_submit(handles, 2);
    check(wh->gpu_guest(0x1234) == 0x1234, "forwarding goes on after the write failed");
    check(wr->nvn_fence() == 100, "the renderer still answers after the write failed");
    printf("end\n");
    rx_capture_end();
    return 0;
}

@PLAY@

/* the play scenario: the host survey, then every boundary call once, each one's dump printed before the recorder's call
   (the dump is the same text the stub prints when the player makes the call) */
static int play_mode(void) {
    build();
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh != &st_host && wr != &st_rend, "the capture started");
    renderer_host = wh;
    play_host = wh;
    expect_marks = -1;   /* (the marks are set by the scenario, not checked at each submit) */
    play_survey(wh);
    d_pool_created(0xC000, 0x1000);
    wr->nvn_pool_created(0xC000, 0x1000);
    play_late(wh);
    d_program_created(&prog1);
    wr->nvn_program_created(&prog1);
    marks[2] = 1;
    d_submit(handles, 2);
    wr->nvn_submit(handles, 2);
    handles[0] = P(&rec3);   /* the game rewrites the handles array at its address: its first recording is a new one */
    tex0.d.width = 5;
    marks[9] = 1;
    d_submit_q(handles, 2, 3);
    wr->nvn_submit_q(handles, 2, 3);
    d_fence();
    wr->nvn_fence();
    d_fence_q(3);
    wr->nvn_fence_q(3);
    d_pushed();
    wr->nvn_pushed();
    d_replayed();
    wr->nvn_replayed();
    d_wait(100, 1000);
    wr->nvn_wait(100, 1000);
    d_wait_writebacks_reading(guest + 0x100, 32);
    wr->wait_writebacks_reading(guest + 0x100, 32);
    d_present(&tex0, crop);
    wr->nvn_present(&tex0, crop);
    d_register_texture(1, &tex0, &view);
    wr->nvn_register_texture(1, &tex0, &view);
    d_register_sampler(2, &samp);
    wr->nvn_register_sampler(2, &samp);
    d_set_texture_pool_size(64);
    wr->nvn_set_texture_pool_size(64);
    d_texture_created(&tex0);
    wr->nvn_texture_created(&tex0);
    fill_page(13, 0x51);   /* prog0's code, written after the last submit: outside every pool, so no mark lists it */
    d_program_created(&prog0);
    wr->nvn_program_created(&prog0);
    d_read_texels(&tex0, 0, 0, &region, rdst, 16, 16);
    wr->nvn_read_texels(&tex0, 0, 0, &region, rdst, 16, 16);
    d_write_texels(&tex0, 0, 0, &region, wsrc, 16, 16);
    wr->nvn_write_texels(&tex0, 0, 0, &region, wsrc, 16, 16);
    d_texture_released(&tex1);
    wr->nvn_texture_released(&tex1);
    d_program_released(&prog0);
    wr->nvn_program_released(&prog0);
    d_invalidate(0x0, 0x1000);
    wr->nvn_invalidate(0x0, 0x1000);
    d_vmap_changing(0x0, 0x4000);
    wr->nvn_vmap_changing(0x0, 0x4000);
    d_submit(handles, 2);
    wr->nvn_submit(handles, 2);
    d_event_signal(0xE000, 7);
    wr->nvn_event_signal(0xE000, 7);
    d_finish();
    wr->nvn_finish();
    printf("end\n");
    rx_capture_end();
    return 0;
}

/* the scene: the same calls whether the capture starts from the start or is armed. RX_CAPTURE_START, when set, names a
   file that is made after the calls before the scene's present; the present that finds it starts the armed capture from
   a snapshot of the state those calls made. The fence before the present is handed out before the armed start */
/* the host's texture questions about a texture the scene creates, level 0: the play asks them too (p_levels) */
static void scene_levels(const nvn_tex *t) {
    unsigned tile_h = 0, tile_d = 0;
    wh->tex_level_offset(&t->d, 0);
    wh->tex_level_tiles(&t->d, 0, &tile_h, &tile_d);
}

static int scene_mode(void) {
    const char *file = getenv("RX_CAPTURE_START");
    build();
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh != &st_host && wr != &st_rend, "the capture started");
    renderer_host = wh;
    play_host = wh;
    expect_marks = -1;   /* (the marks are set by the scenario, not checked at each submit) */
    wr->nvn_pool_created(0xC000, 0x1000);
    wr->nvn_set_texture_pool_size(64);
    wr->nvn_texture_created(&tex0);
    scene_levels(&tex0);
    tex0.d.width = 5;    /* (after its creation: the snapshot keeps the bytes the creation saw) */
    wr->nvn_texture_created(&tex1);
    scene_levels(&tex1);
    wr->nvn_register_texture(1, &tex0, &view);
    wr->nvn_register_sampler(2, &samp);
    fill_page(13, 0x51);   /* prog0's code, outside every pool: its creation reads it */
    wr->nvn_program_created(&prog0);
    wr->nvn_program_created(&prog1);
    check(wr->nvn_fence() == 100, "a fence handed out before the scene's present");
    if (file) {
        FILE *made = fopen(file, "w");
        check(made != NULL, "the start file is made");
        if (made) fclose(made);
    }
    wr->nvn_present(&tex0, crop);
    /* the window's size, which the play takes from the first guest_size answer the capture holds. The renderer asks it
       through the host table (vk_bank.cpp's descriptor_ok), so the scene asks it after the present, where an armed
       capture records it */
    check(wh->guest_size() == NPAGES * PAGE, "guest_size answers the window after the present");
    fill_page(2, 0x77);             /* a pool page the submit lists as written */
    marks[2] = 1;
    p_probe();
    wr->nvn_submit(handles, 2);
    check(wr->nvn_fence_q(3) == 3000, "a fence_q handed out after the present");
    wr->nvn_wait(3000, 1000);
    wr->nvn_wait(100, 1000);
    check(wr->nvn_fence() == 100, "a fence handed out after the present");
    wr->nvn_wait(100, 1000);
    check(wr->nvn_fence_q(3) == 3000, "the queue's fence again");
    wr->nvn_wait(3000, 1000);
    wr->nvn_register_texture(1, &tex0, &view);
    wr->nvn_read_texels(&tex0, 0, 0, &region, rdst, 16, 16);
    wr->nvn_write_texels(&tex0, 0, 0, &region, wsrc, 16, 16);
    wr->nvn_texture_released(&tex1);
    wr->nvn_program_released(&prog0);
    wr->nvn_invalidate(0x0, 0x1000);
    wr->nvn_vmap_changing(0x0, 0x4000);
    wr->nvn_event_signal(0xE000, 7);
    wr->nvn_finish();
    printf("end\n");
    rx_capture_end();
    return 0;
}

/* the armed start: RX_CAPTURE_START names a file. The state is made before the file exists, then the file, then more
   calls. The stub gets every call; the printed addresses and the bytes made at each creation are what the check reads */
static void print_addr(const char *label, const void *p) {
    printf("start addr %s %llu\n", label, (unsigned long long)P(p));
}

static int start_mode(void) {
    const char *file = getenv("RX_CAPTURE_START");
    check(file != NULL, "the start file is named");
    build();
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh != &st_host && wr != &st_rend, "the armed capture started");
    renderer_host = wh;
    expect_marks = -1;   /* (the marks are set by the scenario, not checked at each submit) */
    static nvn_tex start_tex2, start_tex3;
    static nvn_samp_par start_samp_b;
    tex0.L.size = 0x1000;   /* the storage the start asks about: tex0's, at 0xC000, is outside the listed pools */
    start_tex2 = tex0;
    start_tex2.gpu = 0x4200;   /* outside the listed pools: pages 0x4000 and 0x5000 are asked */
    start_tex2.serial = 3;
    start_tex2.d.levels = 2;
    start_tex3 = tex0;
    start_tex3.gpu = 0x1000;   /* inside the listed pool [0, 0x4000): none of its pages is asked */
    start_tex3.serial = 4;
    start_tex3.d.levels = 0;   /* (no levels: level 0 is asked all the same) */
    memset(&samp, 0x5c, sizeof samp);
    memset(&start_samp_b, 0x3e, sizeof start_samp_b);
    memset(&view, 0x21, sizeof view);
    print_addr("tex0", &tex0);
    print_addr("tex1", &tex1);
    print_addr("start_tex2", &start_tex2);
    print_addr("start_tex3", &start_tex3);
    print_addr("start_tex0_d", &tex0.d);
    print_addr("start_tex2_d", &start_tex2.d);
    print_addr("start_tex3_d", &start_tex3.d);
    print_addr("pool_c", &pool_c);
    print_addr("view", &view);
    print_addr("samp", &samp);
    print_addr("start_samp_b", &start_samp_b);
    print_addr("prog0", &prog0);
    print_addr("prog1", &prog1);
    print_addr("crop", crop);
    print_addr("handles", handles);
    /* before the file: the pools, the pool size, the textures (one released, one overwritten by value after its
       creation), the registrations (id 1 twice), the samplers, the programs (prog1 released), a submit and two
       presents */
    wr->nvn_pool_created(0xC000, 0x1000);
    wr->nvn_pool_created(0xE000, 0x800);
    wr->nvn_set_texture_pool_size(64);
    wr->nvn_texture_created(&tex0);
    hexdump("start_tex0", &tex0, sizeof tex0);
    hexdump("start_tex0_d", &tex0.d, sizeof tex0.d);
    wr->nvn_texture_created(&tex1);
    wr->nvn_texture_created(&start_tex2);
    hexdump("start_tex2", &start_tex2, sizeof start_tex2);
    hexdump("start_tex2_d", &start_tex2.d, sizeof start_tex2.d);
    wr->nvn_texture_created(&start_tex3);
    hexdump("start_tex3_d", &start_tex3.d, sizeof start_tex3.d);
    hexdump("start_pool", &pool_c, sizeof pool_c);
    tex0.d.width = 5;
    wr->nvn_texture_released(&tex1);
    wr->nvn_register_texture(1, &tex0, &view);
    wr->nvn_register_texture(1, &start_tex2, NULL);
    wr->nvn_register_texture(2, &start_tex2, &view);
    hexdump("start_view", &view, sizeof view);
    wr->nvn_register_sampler(4, &samp);
    wr->nvn_register_sampler(2, &start_samp_b);
    hexdump("start_samp", &samp, sizeof samp);
    hexdump("start_samp_b", &start_samp_b, sizeof start_samp_b);
    wr->nvn_program_created(&prog1);
    wr->nvn_program_created(&prog0);
    hexdump("start_prog0", &prog0, sizeof prog0);
    wr->nvn_program_released(&prog1);
    wr->nvn_submit(handles, 2);
    wr->nvn_present(&tex0, crop);
    wr->nvn_present(&tex0, crop);
    FILE *made = fopen(file, "w");
    check(made != NULL, "the start file is made");
    if (made) fclose(made);
    printf("start file\n");
    /* the first present after the file starts the capture; then the overwritten texture is read (its live bytes are
       written, as they differ from the created ones), a submit that marks a page, and the rest */
    wr->nvn_present(&start_tex2, crop);
    marks[2] = 1;
    hexdump("start_tex0_live", &tex0, sizeof tex0);
    wr->nvn_read_texels(&tex0, 0, 0, &region, rdst, 16, 16);
    wr->nvn_submit(handles, 2);
    wr->nvn_event_signal(0xE000, 7);
    wr->nvn_finish();
    check(wr->nvn_fence() == 100, "the renderer answers after the start");
    printf("end\n");
    rx_capture_end();
    return 0;
}

/* the race: while the start asks the host about its texture's page, a second present checks the start file. Only the
   start's present asks; the second one is an ordinary present, recorded after the start */
static void *racing_present(void *arg) {
    (void)arg;
    is_racing = 1;
    wr->nvn_present(&tex0, crop);
    return NULL;
}

static int race_start_mode(void) {
    const char *file = getenv("RX_CAPTURE_START");
    check(file != NULL, "the start file is named");
    build();
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh != &st_host && wr != &st_rend, "the armed capture started");
    renderer_host = wh;
    expect_marks = -1;
    wr->nvn_pool_created(0xC000, 0x1000);
    tex0.L.size = 0x1000;   /* the storage the start asks about: outside the listed pools */
    wr->nvn_texture_created(&tex0);
    FILE *made = fopen(file, "w");
    check(made != NULL, "the start file is made");
    if (made) fclose(made);
    printf("start file\n");
    racing_go = 1;
    wr->nvn_present(&tex0, crop);
    check(racing_go == 0, "the start's ask started the second present");
    if (racing_go == 0) check(pthread_join(racing, NULL) == 0, "the second present ends");
    printf("end\n");
    rx_capture_end();
    return 0;
}

/* the texture made while the start asks the host: while the start asks about its texture's storage, another thread
   makes a texture. The snapshot keeps it, with no answers of its own: the capture starts all the same */
static void *texture_racing(void *arg) {
    (void)arg;
    wr->nvn_texture_created(&tex1);
    return NULL;
}

static int texture_race_start_mode(void) {
    const char *file = getenv("RX_CAPTURE_START");
    check(file != NULL, "the start file is named");
    build();
    tex0.L.size = 0x1000;   /* (outside the listed pools: the start asks about it) */
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh != &st_host && wr != &st_rend, "the armed capture started");
    renderer_host = wh;
    expect_marks = -1;
    print_addr("tex0", &tex0);
    print_addr("tex1", &tex1);
    wr->nvn_texture_created(&tex0);
    FILE *made = fopen(file, "w");
    check(made != NULL, "the start file is made");
    if (made) fclose(made);
    printf("start file\n");
    texture_race_go = 1;
    wr->nvn_present(&tex0, crop);
    check(texture_race_go == 0, "the start's ask of the storage made the texture");
    printf("end\n");
    rx_capture_end();
    return 0;
}

/* the armed start with a host that lists more pools than the recorder asks for first: the snapshot holds a POOL for
   each, and the page that only a pool past the first 4096 reaches, marked after the start, is in the next submit's
   PAGES */
static int wide_start_mode(void) {
    const char *file = getenv("RX_CAPTURE_START");
    check(file != NULL, "the start file is named");
    build();
    wide_pools = 1;
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh != &st_host && wr != &st_rend, "the armed capture started");
    renderer_host = wh;
    expect_marks = -1;
    FILE *made = fopen(file, "w");
    check(made != NULL, "the start file is made");
    if (made) fclose(made);
    printf("start file\n");
    wr->nvn_present(&tex0, crop);
    marks[8] = 1;
    wr->nvn_submit(handles, 2);
    printf("end\n");
    rx_capture_end();
    return 0;
}

/* the presents' limit (RX_CAPTURE_PRESENTS): five presents, each with a fence after it, and the file (RX_CAPTURE_START,
   when set) made before the third present, so an armed capture starts there. The stub gets every call */
static int stop_mode(void) {
    const char *file = getenv("RX_CAPTURE_START");
    build();
    wh = rx_capture_host(&st_host, marks, NPAGES);
    wr = rx_capture_renderer(&st_rend);
    check(wh != &st_host && wr != &st_rend, "the capture started");
    print_addr("tex0", &tex0);
    print_addr("crop", crop);
    wr->nvn_pool_created(0xC000, 0x1000);
    for (int i = 0; i < 5; i++) {
        if (file && i == 2) {
            FILE *made = fopen(file, "w");
            check(made != NULL, "the start file is made");
            if (made) fclose(made);
        }
        wr->nvn_present(&tex0, crop);
        check(wr->nvn_fence() == 100, "a fence answers after a present");
    }
    printf("end\n");
    rx_capture_end();
    return 0;
}

int main(void) {
    const char *mode = getenv("RXC_MODE");
    if (!mode) return 2;
    int rc;
    if (!strcmp(mode, "off") || !strcmp(mode, "empty")) rc = off_mode();
    else if (!strcmp(mode, "hook-off")) rc = hook_off_mode();
    else if (!strcmp(mode, "hook-on")) rc = hook_on_mode();
    else if (!strcmp(mode, "race")) rc = race_mode();
    else if (!strcmp(mode, "badpath")) rc = badpath_mode();
    else if (!strcmp(mode, "fail")) rc = fail_mode();
    else if (!strcmp(mode, "play")) rc = play_mode();
    else if (!strcmp(mode, "start")) rc = start_mode();
    else if (!strcmp(mode, "scene")) rc = scene_mode();
    else if (!strcmp(mode, "race-start")) rc = race_start_mode();
    else if (!strcmp(mode, "texture-race-start")) rc = texture_race_start_mode();
    else if (!strcmp(mode, "wide-start")) rc = wide_start_mode();
    else if (!strcmp(mode, "stop")) rc = stop_mode();
    else rc = on_mode();
    if (atomic_load(&failures)) {
        printf("FAILED %d\n", atomic_load(&failures));
        return 1;
    }
    printf("ok %s\n", mode);
    return rc;
}
"""


# The play check's C, compiled into the fixture (the recorder's scenario) and into the stub renderer (the player's
# replay): each boundary entry prints its value arguments and the bytes of each object its pointers reach, and the host
# survey asks the host the same questions. The two programs print the same lines when the capture plays back faithfully.
PLAY_SHARED = r"""
#include <stddef.h>

/* ---- the play check: the scenario (play_mode) and the player's stub print the same lines, one for each fact ---- */
static const MkwVkHost *play_host;   /* the host the scenario asks, and the stub's questions go to */
static uint8_t play_wdata[8] = {0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47};

static void p_hex(const void *p, size_t n) {
    const uint8_t *b = p;
    for (size_t i = 0; i < n; i++) printf("%02x", b[i]);
}

/* n bytes at p, the pointers at offs zeroed: a pointer's value is the process's own, so its target is printed after the
   object that holds it */
static void p_obj(const char *label, const void *p, size_t n, const size_t *offs, size_t nptr) {
    uint8_t *b = malloc(n ? n : 1);
    memcpy(b, p, n);
    for (size_t i = 0; i < nptr; i++) memset(b + offs[i], 0, sizeof(void *));
    printf("play  obj %s ", label);
    p_hex(b, n);
    printf("\n");
    free(b);
}

static void p_pool(const nvn_pool *p) { p_obj("pool", p, sizeof *p, NULL, 0); }

static void p_tex(const char *label, const nvn_tex *t) {
    const size_t offs[1] = {offsetof(nvn_tex, pool)};
    p_obj(label, t, sizeof *t, offs, 1);
    if (t->pool) p_pool(t->pool);
}

static void p_dpool(const nvn_dpool *d) {
    const size_t offs[1] = {offsetof(nvn_dpool, pool)};
    p_obj("dpool", d, sizeof *d, offs, 1);
    if (d->pool) p_pool(d->pool);
}

static void p_prog(const nvn_prog *p) { p_obj("prog", p, sizeof *p, NULL, 0); }

/* n bytes the renderer reads at a guest address, through the host: the host's answer, then the bytes */
static void p_guest(const char *label, uint64_t gpu, size_t n) {
    uint8_t *b = malloc(n);
    const int ok = play_host->gpu_read(gpu, b, n);
    printf("play guest %s %d ", label, ok);
    p_hex(b, n);
    printf("\n");
    free(b);
}

/* a program's control sections, which the renderer reads at its creation and at its binding: 0xd8 bytes per stage */
static void p_controls(const nvn_prog *p) {
    for (uint32_t k = 0; k < p->nstage && k < 6; k++)
        if (p->sd[k].control) p_guest("control", p->sd[k].control, 0xd8);
}

/* a program's code, which the renderer reads at its creation for each stage whose control section it takes (its
   control_end, gpu/vk/vk_shader.cpp): the end bytes at the stage's data, end the u32 at +0x44 */
static void p_code(const nvn_prog *p) {
    for (uint32_t k = 0; k < p->nstage && k < 6; k++) {
        uint8_t ctl[0xd8];
        if (!p->sd[k].control || !p->sd[k].data || !play_host->gpu_read(p->sd[k].control, ctl, sizeof ctl)) continue;
        uint32_t magic, len, c1o, end;
        uint16_t st;
        memcpy(&magic, ctl, 4);
        memcpy(&len, ctl + 0x38, 4);
        memcpy(&c1o, ctl + 0x40, 4);
        memcpy(&end, ctl + 0x44, 4);
        memcpy(&st, ctl + 0x56, 2);
        if (magic != 0x98761234u || !len || end > (16u << 20) || c1o > end || len > end || st > 7) continue;
        p_guest("code", p->sd[k].data, end);
    }
}

/* a recording, BEGIN to END: each record with its pointers zeroed (their offsets are the record's), and the objects
   they point to after it */
static void p_recording(const uint8_t *rec) {
    for (size_t at = 0;;) {
        const nvn_op_hdr *h = (const nvn_op_hdr *)(rec + at);
        const uint8_t *pl = rec + at + sizeof *h;   /* the payload */
        const size_t po = sizeof *h;                 /* the payload's offset in the record */
        size_t offs[9] = {0};
        size_t nptr = 0;
        switch (h->op) {
        case NOP_SET_RT: {
            const nvn_p_set_rt *s = (const nvn_p_set_rt *)pl;
            const int lim = s->n < 8 ? s->n : 8;
            for (int i = 0; i < lim; i++) offs[nptr++] = po + offsetof(nvn_p_set_rt, color) + i * sizeof(void *);
            offs[nptr++] = po + offsetof(nvn_p_set_rt, depth);
            p_obj("op", rec + at, h->size, offs, nptr);
            for (int i = 0; i < lim; i++)
                if (s->color[i]) p_tex("color", s->color[i]);
            if (s->depth) p_tex("depth", s->depth);
            break;
        }
        case NOP_BIND_PROGRAM: {
            const nvn_p_bind_program *b = (const nvn_p_bind_program *)pl;
            offs[0] = po + offsetof(nvn_p_bind_program, prog);
            p_obj("op", rec + at, h->size, offs, 1);
            if (b->prog) p_prog(b->prog);
            if (b->prog) p_controls(b->prog);
            break;
        }
        case NOP_BIND_BUFS: {
            const nvn_p_bind_bufs *b = (const nvn_p_bind_bufs *)pl;
            p_obj("op", rec + at, h->size, NULL, 0);
            p_guest("bufs", b->src, 16 * (size_t)b->n);
            break;
        }
        case NOP_BIND_TEX: {
            const nvn_p_bind_handles *b = (const nvn_p_bind_handles *)pl;
            p_obj("op", rec + at, h->size, NULL, 0);
            p_guest("handles", b->src, 8 * (size_t)b->n);
            break;
        }
        case NOP_SET_TEX_POOL:
        case NOP_SET_SAMP_POOL: {
            const nvn_p_set_pool *s = (const nvn_p_set_pool *)pl;
            offs[0] = po + offsetof(nvn_p_set_pool, pool);
            p_obj("op", rec + at, h->size, offs, 1);
            if (s->pool) p_dpool(s->pool);
            break;
        }
        case NOP_COPY_B2T:
        case NOP_COPY_T2B: {
            const nvn_p_copy_bt *c = (const nvn_p_copy_bt *)pl;
            offs[0] = po + offsetof(nvn_p_copy_bt, tex);
            p_obj("op", rec + at, h->size, offs, 1);
            if (c->tex) p_tex("tex", c->tex);
            break;
        }
        case NOP_COPY_T2T: {
            const nvn_p_copy_tt *c = (const nvn_p_copy_tt *)pl;
            offs[0] = po + offsetof(nvn_p_copy_tt, src);
            offs[1] = po + offsetof(nvn_p_copy_tt, dst);
            p_obj("op", rec + at, h->size, offs, 2);
            if (c->src) p_tex("src", c->src);
            if (c->dst) p_tex("dst", c->dst);
            break;
        }
        case NOP_CLEAR_TEX: {
            const nvn_p_clear_tex *c = (const nvn_p_clear_tex *)pl;
            offs[0] = po + offsetof(nvn_p_clear_tex, tex);
            p_obj("op", rec + at, h->size, offs, 1);
            if (c->tex) p_tex("tex", c->tex);
            break;
        }
        default:
            p_obj("op", rec + at, h->size, NULL, 0);
            break;
        }
        if (h->op == NOP_END) return;
        at += h->size;
    }
}

static void p_handles(const uint64_t *handles, int n) {
    for (int i = 0; i < n; i++) {
        printf("play  rec %d\n", i);
        p_recording((const uint8_t *)(uintptr_t)handles[i]);
    }
}

static void play_desc(nvn_texdesc *d) {
    memset(d, 0, sizeof *d);
    d->width = 4;
    d->height = 1;
    d->depth = 1;
    d->levels = 1;
}

/* the questions a submit asks the host while the renderer handles it (asked the same way by the scenario and the
   stub) */
static void p_probe(void) {
    nvn_texdesc desc;
    play_desc(&desc);
    const uint64_t off = play_host->tex_level_offset(&desc, 1);
    printf("play probe tex_level_offset %llu\n", (unsigned long long)off);
    const uint32_t vmap = play_host->vmap_generation();
    printf("play probe vmap %u\n", vmap);
}

/* the boundary entries: each prints its value arguments, then the objects its pointers reach (the texels a read
   writes are the renderer's own, so the read's output is not printed) */
static void d_pool_created(uint64_t gpu, uint64_t size) {
    printf("play nvn_pool_created %llu %llu\n", (unsigned long long)gpu, (unsigned long long)size);
}
static void d_submit(const uint64_t *handles, int n) {
    printf("play nvn_submit %d\n", n);
    p_handles(handles, n);
    p_probe();
}
static void d_submit_q(const uint64_t *handles, int n, uint64_t queue) {
    printf("play nvn_submit_q %d %llu\n", n, (unsigned long long)queue);
    p_handles(handles, n);
}
/* the fence answers: the n-th fence call answered gets FENCE_ANSWER + n, neither the recorder's answers (100, and
   queue * 1000) nor 0, so a fence the play translates is observable */
#define FENCE_ANSWER 0x9000u
static uint64_t fences_answered;
static uint64_t d_fence(void) {
    printf("play nvn_fence\n");
    return FENCE_ANSWER + fences_answered++;
}
static uint64_t d_fence_q(uint64_t queue) {
    printf("play nvn_fence_q %llu\n", (unsigned long long)queue);
    return FENCE_ANSWER + fences_answered++;
}
static uint64_t d_pushed(void) {
    printf("play nvn_pushed\n");
    return 0;
}
static uint64_t d_replayed(void) {
    printf("play nvn_replayed\n");
    return 0;
}
static int d_wait(uint64_t fence, uint64_t timeout_ns) {
    printf("play nvn_wait %llu %llu\n", (unsigned long long)fence, (unsigned long long)timeout_ns);
    return 0;
}
static void d_wait_writebacks_reading(const void *p, uint64_t n) {
    printf("play wait_writebacks_reading %llu\n", (unsigned long long)n);
    p_obj("guest", p, n, NULL, 0);
}
static void d_finish(void) { printf("play nvn_finish\n"); }
static void d_present(const nvn_tex *t, const int crop[4]) {
    printf("play nvn_present\n");
    p_tex("tex", t);
    p_obj("crop", crop, 4 * sizeof(int), NULL, 0);
}
static void d_register_texture(int id, const nvn_tex *t, const nvn_view *v) {
    printf("play nvn_register_texture %d\n", id);
    p_tex("tex", t);
    p_obj("view", v, sizeof *v, NULL, 0);
}
static void d_register_sampler(int id, const nvn_samp_par *p) {
    printf("play nvn_register_sampler %d\n", id);
    p_obj("samp", p, sizeof *p, NULL, 0);
}
static void d_set_texture_pool_size(int n) { printf("play nvn_set_texture_pool_size %d\n", n); }
static void d_texture_released(const nvn_tex *t) {
    printf("play nvn_texture_released\n");
    p_tex("tex", t);
}
static void p_levels(const nvn_tex *t) {
    unsigned tile_h = 0, tile_d = 0;
    const uint64_t off = play_host->tex_level_offset(&t->d, 0);
    play_host->tex_level_tiles(&t->d, 0, &tile_h, &tile_d);
    printf("play live levels %llu %u %u\n", (unsigned long long)off, tile_h, tile_d);
}
static void d_texture_created(const nvn_tex *t) {
    printf("play nvn_texture_created\n");
    p_tex("tex", t);
    p_levels(t);
}
static void d_program_released(const nvn_prog *p) {
    printf("play nvn_program_released\n");
    p_prog(p);
}
static void d_program_created(const nvn_prog *p) {
    printf("play nvn_program_created\n");
    p_prog(p);
    p_controls(p);
    p_code(p);
    /* the control section's page (10) is not listed as written: a GUEST is bytes in the window, not a page */
    uint32_t *pages = NULL;
    const int64_t listed = play_host->collect_written(&pages);
    int control_listed = 0;
    for (int64_t k = 0; k < listed; k++) control_listed += (pages[k] == 10);
    printf("play collected control page %d\n", control_listed);
    play_host->free_pages(pages);
}
static void d_read_texels(const nvn_tex *t, unsigned level, unsigned layer0, const NVNcopyRegion *r, uint8_t *dst,
                          uint64_t row, uint64_t img) {
    printf("play nvn_read_texels %u %u %llu %llu\n", level, layer0, (unsigned long long)row, (unsigned long long)img);
    p_tex("tex", t);
    p_obj("region", r, sizeof *r, NULL, 0);
    (void)dst;
}
static void d_write_texels(const nvn_tex *t, unsigned level, unsigned layer0, const NVNcopyRegion *r,
                           const uint8_t *src, uint64_t row, uint64_t img) {
    printf("play nvn_write_texels %u %u %llu %llu\n", level, layer0, (unsigned long long)row, (unsigned long long)img);
    p_tex("tex", t);
    p_obj("region", r, sizeof *r, NULL, 0);
    p_obj("texels", src, 16, NULL, 0);   /* the scenario writes 16 bytes */
}
static void d_invalidate(uint64_t gpu, uint64_t size) {
    printf("play nvn_invalidate %llu %llu\n", (unsigned long long)gpu, (unsigned long long)size);
}
static void d_vmap_changing(uint64_t gpu, uint64_t size) {
    printf("play nvn_vmap_changing %llu %llu\n", (unsigned long long)gpu, (unsigned long long)size);
}
static void d_event_signal(uint64_t gpu, uint32_t value) {
    printf("play nvn_event_signal %llu %u\n", (unsigned long long)gpu, value);
}

/* the host survey: the same questions, in the same order, from the scenario and from the stub's init */
static void play_survey(const MkwVkHost *h) {
    uint8_t b[16];
    const int ok = h->gpu_read(0x100, b, sizeof b);
    printf("play host gpu_read %d ", ok);
    p_hex(b, sizeof b);
    printf("\n");
    uint64_t avail = 0;
    const uint8_t *ptr = h->gpu_ptr(0x1000, &avail);
    printf("play host gpu_ptr %d %llu %u\n", ptr != NULL, (unsigned long long)avail, ptr ? ptr[0x10] : 0u);
    /* past the window: the pointer is not dereferenced (it is the scenario's own memory when recorded), only printed */
    uint64_t beyond_avail = 0;
    const uint8_t *beyond = h->gpu_ptr(0x10040, &beyond_avail);
    printf("play host gpu_ptr beyond %llu %llu\n", (unsigned long long)(uintptr_t)beyond,
           (unsigned long long)beyond_avail);
    printf("play host gpu_guest %llu\n", (unsigned long long)h->gpu_guest(0x1000));
    printf("play host gpu_write %d\n", h->gpu_write(0xE000, play_wdata, sizeof play_wdata));
    printf("play host gpu_read2 %d ", h->gpu_read(0xE000, b, 8));
    p_hex(b, 8);
    printf("\n");
    printf("play host guest_size %llu\n", (unsigned long long)h->guest_size());
    nvn_texdesc desc;
    play_desc(&desc);
    printf("play host tex_level_offset %llu\n", (unsigned long long)h->tex_level_offset(&desc, 1));
    int bw = 0, bh = 0;
    const int bpb = h->fmt_bpb(1, &bw, &bh);
    printf("play host fmt_bpb %d %d %d\n", bpb, bw, bh);
    nvn_texlayout lay;
    memset(&lay, 0, sizeof lay);
    h->tex_layout(&desc, &lay);
    printf("play host tex_layout %llu %u %u\n", (unsigned long long)lay.size, lay.bpb, lay.tileH);
    unsigned tile_h = 0, tile_d = 0;
    h->tex_level_tiles(&desc, 0, &tile_h, &tile_d);
    printf("play host tex_level_tiles %u %u\n", tile_h, tile_d);
    printf("play host tex_linear_stride %u\n", h->tex_linear_stride(&desc));
    printf("play host depth_minus_w %d\n", h->depth_minus_w());
    uint64_t pools[4][2];
    const int np = h->gpu_pools(pools, 4);
    printf("play host gpu_pools %d", np);
    for (int i = 0; i < np; i++) printf(" %llx+%llx", (unsigned long long)pools[i][0], (unsigned long long)pools[i][1]);
    printf("\n");
    printf("play host generations %u %u\n", h->maps_generation(), h->vmap_generation());
    printf("play host reserved_cpus %llu\n", (unsigned long long)h->reserved_cpus());
    printf("play host guest_alloc %llu\n", (unsigned long long)h->guest_alloc(0x1000));
    printf("play host clocks %llu %lld\n", (unsigned long long)h->ticks(), (long long)h->now_ns());
    printf("play host pad_raw %u binding %d\n", h->pad_raw(), h->binding(0, 3, 1));
    printf("play host aspect %.6f wide %.6f\n", h->aspect_ratio(), h->wide_menus(3));
    printf("play host wide_cover %.6f\n", h->wide_cover());
    MkwVkPointer pt;
    memset(&pt, 0, sizeof pt);
    const int inside = h->pointer(&pt);
    printf("play host pointer %d %d %d %d %d\n", inside, pt.x, pt.y, pt.width, pt.height);
    const char *mic = h->mic_name(0);
    printf("play host mic %d %s\n", h->mic_count(), mic ? mic : "(none)");
    printf("play host gyro %d prompt %d live %d\n", h->gyro_status(), h->prompt_face(), h->prompts_live());
    const char *romfs = h->romfs_dir();
    printf("play host romfs %s\n", romfs ? romfs : "(none)");
    int64_t slept[2] = {0, 0};
    h->slept_ns(slept);
    printf("play host slept %lld %lld\n", (long long)slept[0], (long long)slept[1]);
    int sw = 0, sh = 0;
    const int sr = h->screen_size(&sw, &sh);
    printf("play host screen %d %d %d\n", sr, sw, sh);
    h->fps_timing(0);
    h->bind(0, 3, 1, 0x41);
    h->mic_select(1, "mic0", 0);
    h->watch_guest_writes(0, 16);
    h->log_flush();
}

/* the questions the scenario asks after its first boundary call: the capture records their answers after that call, so
   the stub, asking them at its start (before that call), finds no recorded answer before it. The scenario prints the
   live values; the stub's lines are the answers it got. */
static void play_late(const MkwVkHost *h) {
    nvn_texdesc desc;
    memset(&desc, 0, sizeof desc);
    desc.width = 8;
    desc.height = 2;
    desc.depth = 1;
    desc.levels = 1;
    nvn_texlayout lay;
    memset(&lay, 0, sizeof lay);
    h->tex_layout(&desc, &lay);
    printf("late tex_layout %llu %u %u\n", (unsigned long long)lay.size, lay.bpb, lay.tileH);
    int bw = 0, bh = 0;
    const int bpb = h->fmt_bpb(2, &bw, &bh);
    printf("late fmt_bpb %d %d %d\n", bpb, bw, bh);
    printf("late gpu_guest %llu\n", (unsigned long long)h->gpu_guest(0x20000));
    printf("late gpu_guest page %llu\n", (unsigned long long)h->gpu_guest(PAGED_GPU));
    printf("late gpu_guest unmapped %llu\n", (unsigned long long)h->gpu_guest(UNMAPPED_GPU));
    (void)h->gpu_guest(PAGED_GPU + 0x20);   /* its own answer, recorded after the first call: play_submit asks it */
}
"""

PLAY_STUB_HEAD = r"""#define _GNU_SOURCE   /* RTLD_DEFAULT and pread, for the renderer's own lookups (vk_memory.cpp) */
#include <dlfcn.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "vk_api.h"
#ifndef PLAY_STUB_SKEW
#define PLAY_STUB_SKEW 0   /* the table's version is MKWVK_API_VERSION plus this (1: another version) */
#endif
"""

PLAY_STUB_TAIL = r"""
/* an exit-time handler the renderer registers: it runs only if the player ends through exit() */
static void p_exit_handler(void) {
    printf("exit handler ran\n");
}

/* the VmFlags of the mapping that holds at: sh for a shared mapping (a memfd), nh for no transparent huge pages */
static void p_vmflags(const void *at) {
    FILE *f = fopen("/proc/self/smaps", "r");
    char line[512];
    int inside = 0;
    while (f && fgets(line, sizeof line, f)) {
        unsigned long lo = 0, hi = 0;
        if (sscanf(line, "%lx-%lx", &lo, &hi) == 2) {
            inside = (uintptr_t)at >= lo && (uintptr_t)at < hi;
        } else if (inside && strncmp(line, "VmFlags:", 8) == 0) {
            printf("memfd vmflags%s", line + 8);
            break;
        }
    }
    if (f) fclose(f);
}

/* the renderer's lookup of the window's memfd (dlsym of hos_posix_guest_memfd, as vk_memory.cpp makes it): the memfd's
   bytes from guest address 0x1000 on are the window's bytes there, and the window's VmFlags are printed */
static void p_memfd(void) {
    typedef int (*memfd_fn)(void);
    const memfd_fn fn = (memfd_fn)dlsym(RTLD_DEFAULT, "hos_posix_guest_memfd");
    const int fd = fn ? fn() : -1;
    printf("memfd lookup %d\n", fd);
    uint64_t avail = 0;
    const uint8_t *window = play_host->gpu_ptr(0x1000, &avail);
    p_vmflags(window);
    if (fd < 0) return;
    uint8_t *m = malloc(avail);
    const ssize_t n = pread(fd, m, avail, 0x1000);
    printf("memfd same %llu %d\n", (unsigned long long)avail, n == (ssize_t)avail && memcmp(m, window, avail) == 0);
    printf("memfd bytes ");
    p_hex(m, 32);
    printf("\n");
    free(m);
}

static int p_start(void *hwnd) {
    (void)hwnd;
    atexit(p_exit_handler);
    p_memfd();
    return 1;
}

/* the blob PLAY_STUB_VANISH names is removed when an nvn_submit_q arrives: the player has read it for the call, so a
   later call that reaches the same unchanged object is given the bytes the player kept */
static void p_submit_q(const uint64_t *handles, int n, uint64_t queue) {
    const char *vanish = getenv("PLAY_STUB_VANISH");
    if (vanish) unlink(vanish);
    d_submit_q(handles, n, queue);
}

/* the first submit's replay asks the address whose own answer the capture recorded after the first call (play_late):
   that answer is recorded before this submit, so the player gives it, not the page's (the page answer plus offset) */
static void p_submit(const uint64_t *handles, int n) {
    static int asked;
    if (!asked++) printf("precedence gpu_guest %llu\n", (unsigned long long)play_host->gpu_guest(PAGED_GPU + 0x20));
    d_submit(handles, n);
}

static const MkwVkRenderer play_rend = {
    .version = MKWVK_API_VERSION + PLAY_STUB_SKEW,
    .start = p_start,
    .nvn_submit = p_submit,
    .nvn_submit_q = p_submit_q,
    .nvn_fence = d_fence,
    .nvn_fence_q = d_fence_q,
    .nvn_pushed = d_pushed,
    .nvn_replayed = d_replayed,
    .nvn_wait = d_wait,
    .wait_writebacks_reading = d_wait_writebacks_reading,
    .nvn_finish = d_finish,
    .nvn_present = d_present,
    .nvn_register_texture = d_register_texture,
    .nvn_register_sampler = d_register_sampler,
    .nvn_set_texture_pool_size = d_set_texture_pool_size,
    .nvn_texture_released = d_texture_released,
    .nvn_texture_created = d_texture_created,
    .nvn_program_released = d_program_released,
    .nvn_program_created = d_program_created,
    .nvn_read_texels = d_read_texels,
    .nvn_write_texels = d_write_texels,
    .nvn_invalidate = d_invalidate,
    .nvn_pool_created = d_pool_created,
    .nvn_vmap_changing = d_vmap_changing,
    .nvn_event_signal = d_event_signal,
};

/* the questions the stub asks of the page play_late asked (PAGED_GPU, answered at PAGED_AT): an address in it, an
   address past it, and a range that starts in the page and ends in the next one, which the capture never answered.
   The scenario does not ask these, so the player answers them alone: each from the page's answer, or as before */
static void play_paged(const MkwVkHost *h) {
    printf("paged gpu_guest %llu\n", (unsigned long long)h->gpu_guest(PAGED_GPU + 0x10));
    uint8_t b[16];
    printf("paged gpu_read %d ", h->gpu_read(PAGED_GPU + 0x100, b, sizeof b));
    p_hex(b, sizeof b);
    printf("\n");
    uint32_t *pages = NULL;
    (void)h->collect_written(&pages);   /* the marks so far are cleared, so the next collection lists the write alone */
    h->free_pages(pages);
    printf("paged gpu_write %d\n", h->gpu_write(PAGED_GPU + 0x200, play_wdata, sizeof play_wdata));
    uint8_t back[8];
    printf("paged window %d ", h->gpu_read(PAGED_AT + 0x200, back, sizeof back));
    p_hex(back, sizeof back);
    printf("\n");
    const int64_t listed = h->collect_written(&pages);
    printf("paged collected");
    for (int64_t k = 0; k < listed; k++) printf(" %u", pages[k]);
    printf("\n");
    h->free_pages(pages);
    printf("paged untranslated %llu\n", (unsigned long long)h->gpu_guest(0x50010));
    printf("paged straddle %d\n", h->gpu_read(PAGED_GPU + 0xFF8, b, sizeof b));
    printf("paged unmapped gpu_guest %llu\n", (unsigned long long)h->gpu_guest(UNMAPPED_GPU + 0x10));
    printf("paged unmapped gpu_read %d\n", h->gpu_read(UNMAPPED_GPU + 0x10, b, sizeof b));
}

/* the stub renderer library: the player's host is given to init, which surveys it, then the calls come */
const MkwVkRenderer *mkwvk_init(const MkwVkHost *host) {
    play_host = host;
    play_survey(host);
    play_late(host);
    play_paged(host);
    return &play_rend;
}
"""

PLAY_STUB = PLAY_STUB_HEAD + PAGED_DEFS + PLAY_SHARED + PLAY_STUB_TAIL

def run_build(cmd: list[str], what: str) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode:
        print(r.stdout + r.stderr)
        fail(what)


def build_player(cxx_name: str, cc: str, tmp: Path) -> tuple[Path, Path, Path]:
    cxx = shutil.which(cxx_name)
    if cxx is None:
        fail("a C++ compiler (%s) is on the path" % cxx_name)
    obj, player, stub_src = tmp / "rx_player.o", tmp / "rx_player", tmp / "play_stub.c"
    stub, skew = tmp / "libstub.so", tmp / "libskew.so"
    _ = stub_src.write_text(PLAY_STUB, encoding="utf-8")
    run_build([cxx, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-c", str(PLAYER_CPP), "-o", str(obj)],
              "gpu/player/rx_player.cpp compiles against gpu/vk/vk_api.h with warnings as errors")
    run_build([cxx, str(obj), "-ldl", "-Wl,--export-dynamic-symbol=hos_posix_*", "-o", str(player)],
              "gpu/player/rx_player.cpp links into rx_player with hos_posix_* exported (as hx play links it)")
    run_build([cc, "-shared", "-fPIC", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-I", str(API_HEADER.parent),
               str(stub_src), "-o", str(stub)], "the stub renderer library builds")
    run_build([cc, "-shared", "-fPIC", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-DPLAY_STUB_SKEW=1",
               "-I", str(API_HEADER.parent), str(stub_src), "-o", str(skew)],
              "the stub renderer library of another API version builds")
    return player, stub, skew


def first_difference(got: list[str], want: list[str]) -> str:
    for i, (g, w) in enumerate(zip(got, want)):
        if g != w:
            return "line %d: %r, not %r" % (i, g, w)
    return "%d lines, not %d" % (len(got), len(want))


def check_play_capture(cap: Path, rec_out: str) -> None:
    check("ok play" in rec_out.splitlines(), "the play scenario finished")
    check(not [line for line in rec_out.splitlines() if line.startswith("FAIL")], "the play scenario's own checks held")
    records = read_capture(cap)
    check_blobs(cap, records)
    calls, _ = index_calls(records)
    check({c.name for c in calls} >= set(BOUNDARY), "play: each of the 23 renderer entries is in the capture")
    check_created(records, calls, filled(PLAY_FILL)[:CODE_END])


def play_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("play ")]


def check_guest_memory(out: str, memfd: bool) -> None:
    lines = [line for line in out.splitlines() if line.startswith("memfd ")]
    looked = [line.split() for line in lines if line.startswith("memfd lookup ")]
    check(len(looked) == 1, "the stub asked the renderer's lookup of hos_posix_guest_memfd once: %r" % lines)
    fd = int(looked[0][2])
    vm = [line.split()[2:] for line in lines if line.startswith("memfd vmflags")]
    check(len(vm) == 1, "the stub read the window's mapping in /proc/self/smaps: %r" % lines)
    if not memfd:
        check(fd == -1, "HOS_GUEST_MEMFD=0: the renderer's lookup returns -1: %r" % lines)
        check("nh" in vm[0] and "sh" not in vm[0],
              "HOS_GUEST_MEMFD=0: the window is anonymous memory without transparent huge pages: %r" % vm[0])
        return
    check(fd >= 0, "HOS_GUEST_MEMFD=1: the renderer's lookup returns the window's memfd: %r" % lines)
    check("sh" in vm[0], "HOS_GUEST_MEMFD=1: the window is a shared mapping: %r" % vm[0])
    check("memfd same %d 1" % (PAGE * NPAGES - PAGE) in lines,
          "HOS_GUEST_MEMFD=1: the memfd holds the window's bytes at every guest address from 0x1000 on: %r" % lines)
    check("memfd bytes " + page_bytes(1)[:32].hex() in lines,
          "HOS_GUEST_MEMFD=1: the memfd's bytes at guest address 0x1000 are the capture's page 1: %r" % lines)


def check_creation_reads(got: list[str]) -> None:
    # the stub's reads at each program's creation, each in its own block (from its nvn_program_created to the next
    # submit): the early program's control section is read, and it takes no code; prog0's code is read live, the bytes
    # written after the last submit, which only its GUEST can have put in the window
    starts = [i for i, line in enumerate(got) if line == "play nvn_program_created"]
    check(len(starts) == 2, "the stub's two creations: %r" % starts)
    blocks = [got[s:next((i for i in range(s + 1, len(got)) if got[i].startswith("play nvn_submit ")), len(got))]
              for s in starts]
    check("play guest control 1 " + CONTROL_BAD.hex() in blocks[0],
          "the early program's control section, read at its nvn_program_created, returns the live bytes: %r"
          % blocks[0])
    check(not [line for line in blocks[0] if line.startswith("play guest code ")],
          "the early program's control section fails the renderer's test: its creation reads no code: %r" % blocks[0])
    check("play guest code 1 " + filled(PLAY_FILL)[:CODE_END].hex() in blocks[1],
          "prog0's code, read at its nvn_program_created, returns the live bytes written after the last submit")


def load_phases(err: str) -> int:
    # a play that ran to its end writes one load phases line right after its load line: the times of the reading, the
    # index, the renderer and the snapshot, in whole milliseconds. Returns the snapshot's time
    lines = err.splitlines()
    at = [i for i, line in enumerate(lines) if line.startswith("rx_player: load phases: ")]
    check(len(at) == 1 and at[0] > 0 and lines[at[0] - 1].startswith("rx_player: loaded "),
          "a play that ran to its end writes one load phases line, right after its load line: %r" % err)
    found = re.fullmatch(r"rx_player: load phases: read \d+ ms, index \d+ ms, renderer \d+ ms, snapshot (\d+) ms",
                         lines[at[0]])
    if found is None:
        fail("the load phases line has the player's form: %r" % lines[at[0]])
    return int(found.group(1))


def check_player(cap: Path, rec_out: str, player: Path, stub: Path, guest_memfd: str) -> None:
    # gpu_ptr past the window: the scenario's host answers a pointer into its own memory, which the capture records
    # and the player does not replay; the player answers NULL and 0 bytes instead, so that line is checked on its own
    rec_beyond = [line for line in play_lines(rec_out) if line.startswith(BEYOND)]
    check(len(rec_beyond) == 1 and rec_beyond[0].split()[4] != "0",
          "the scenario's gpu_ptr past the window answers a pointer: %r" % rec_beyond)
    rec = [line for line in play_lines(rec_out) if not line.startswith(BEYOND)]
    check(bool(rec), "the play scenario printed its lines")
    records = read_capture(cap)
    calls, _ = index_calls(records)
    # the stub gets the boundary calls in recorded order with the recorded value arguments; then the player finishes
    want = [[c.name, [str(a) for i, a in enumerate(c.args) if i not in PLAY_POINTERS.get(c.name, ())]]
            for c in calls if c.name in BOUNDARY]
    want.append(["nvn_finish", []])
    env = {**os.environ, "HOS_GUEST_MEMFD": guest_memfd}
    r = subprocess.run([str(player), str(cap), "--renderer", str(stub)], capture_output=True, text=True, timeout=120,
                       env=env)
    check(r.returncode == 0, "rx_player plays the capture through the stub renderer and exits 0: %r" % r.stderr[-2000:])
    check("exit handler ran" not in r.stdout.splitlines(),
          "the renderer's exit-time handler does not run when the player ends (its threads may still run)")
    got = play_lines(r.stdout)
    beyond = [line for line in got if line.startswith(BEYOND)]
    check(beyond == [BEYOND + "0 0"], "gpu_ptr past the window answers NULL with 0 bytes: %r" % beyond)
    got = [line for line in got if not line.startswith(BEYOND)]
    check(got == rec + ["play nvn_finish"],
          "the stub's lines are the scenario's, the player's finish last: %s"
          % first_difference(got, rec + ["play nvn_finish"]))
    got_calls = [[line.split()[1], line.split()[2:]] for line in got if line.split()[1] in BOUNDARY]
    check(got_calls == want, "the stub gets the boundary calls in recorded order with their values: %s"
          % first_difference([repr(c) for c in got_calls], [repr(c) for c in want]))
    # the host's answers: guest reads return the pool and page bytes; a recorded question returns its recorded answer
    page0, page1 = page_bytes(0), page_bytes(1)
    check("play host gpu_read 1 " + page0[0x100:0x110].hex() in got, "a guest read returns the pool's bytes")
    check("play host gpu_ptr 1 %d %d" % (PAGE * NPAGES - 0x1000, page1[0x10]) in got,
          "gpu_ptr returns the guest memory of the window and its span")
    check("play host gpu_read2 1 4041424344454647" in got, "a guest write is read back from the window")
    check("play host guest_size %d" % (PAGE * NPAGES) in got, "guest_size is the recorded window")
    check("play host tex_level_offset 256" in got, "a recorded value question returns its recorded answer")
    check("play host mic 1 mic0" in got, "a recorded string answer is returned")
    check("play host wide_cover 0.250000" in got, "wide_cover answers the value the capture recorded")
    probes = [line for line in got if line.startswith("play probe ")]
    check(probes == ["play probe tex_level_offset 256", "play probe vmap 1", "play probe tex_level_offset 256",
                     "play probe vmap 2"],
          "a question asked before a submit returns the latest answer recorded before it: %r" % probes)
    # the questions the scenario asked after its first call, asked by the stub at its start: the layout questions get
    # the answers recorded after that call, the other question gets zero (the log counts the unanswered ones)
    # (the page past the window, asked at its base, is answered by the page's recorded answer, in the window)
    late = [line for line in r.stdout.splitlines() if line.startswith("late ")]
    want_late = ["late tex_layout 4096 4 1", "late fmt_bpb 4 1 1", "late gpu_guest 0",
                 "late gpu_guest page %d" % PAGED_AT, "late gpu_guest unmapped 0"]
    check(late == want_late,
          "a question asked before its first recorded answer: a layout question gets that answer, another gets zero: %r"
          % late)
    # the renderer's questions about that page, at an offset in it and past it: the translated address, the window's
    # bytes read and written there, the write collected, and the questions the capture cannot translate answered as before
    paged = [line for line in r.stdout.splitlines() if line.startswith("paged ")]
    want_paged = ["paged gpu_guest %d" % (PAGED_AT + 0x10),
                  "paged gpu_read 1 " + page_bytes(PAGED_AT // PAGE)[0x100:0x110].hex(),
                  "paged gpu_write 1",
                  "paged window 1 " + bytes(range(0x40, 0x48)).hex(),
                  "paged collected %d" % (PAGED_AT // PAGE),
                  "paged untranslated 0",
                  "paged straddle 0",
                  "paged unmapped gpu_guest 0",
                  "paged unmapped gpu_read 0"]
    check(paged == want_paged, "the paged page's questions: %s" % first_difference(paged, want_paged))
    # an address the capture recorded its own answer for, asked after that answer's call: the recorded answer, not the
    # page's translation (PAGED_AT + 0x20)
    precedence = [line for line in r.stdout.splitlines() if line.startswith("precedence ")]
    check(precedence == ["precedence gpu_guest %d" % PAGED_OWN],
          "an exact recorded answer wins over the page's: %r" % precedence)
    # the player's log names every host question kind (those the capture answers as questions, in kind order), with how
    # many questions of it found no recorded answer: gpu_guest four times (play_late's 0x20000, outside any answered
    # page, and its UNMAPPED_GPU, asked before that answer's call; play_paged's 0x50010, and its address in the unmapped
    # page, whose 0 answer translates nothing), gpu_read twice (play_paged's range whose second page has no answer, and
    # its read in the unmapped page); the rest none
    unanswered_kinds = {"gpu_guest": 4, "gpu_read": 2}
    want = "rx_player: unanswered host questions: " + ", ".join(
        "%s %d" % (name, unanswered_kinds.get(name, 0)) for name in QUESTION_KINDS)
    unanswered = [line for line in r.stderr.splitlines() if line.startswith("rx_player: unanswered")]
    check(unanswered == [want],
          "the player's log names every host question kind with the questions it found no recorded answer for: %r"
          % unanswered)
    # the player's load, on a play that ran to its end: one line on stderr with the capture's bytes on disk (calls.rxc
    # and each blob file) and the time from the player's start to the renderer's start
    loaded = [line for line in r.stderr.splitlines() if line.startswith("rx_player: loaded ")]
    found = re.fullmatch(r"rx_player: loaded (\d+) bytes in (\d+) ms", loaded[0]) if len(loaded) == 1 else None
    on_disk = (cap / "calls.rxc").stat().st_size + sum(p.stat().st_size for p in (cap / "blobs").iterdir())
    check(found is not None and int(found.group(1)) == on_disk,
          "the player's log gives the capture's %d bytes on disk and its load time: %r" % (on_disk, loaded))
    # the same play writes the load phases line after the load line; this capture holds no SNAPSHOT, so its time is 0
    check(load_phases(r.stderr) == 0, "the unarmed capture's load phases give a snapshot time of 0: %r" % r.stderr)
    # the renderer's reads by guest address return the live bytes: a control section at the program's creation and at
    # its submit, the deferred arrays at the submit, and the code at a program's creation
    check("play guest control 1 " + CONTROL_OK.hex() in got,
          "a control section read at nvn_program_created and at the submit returns the live bytes")
    check_creation_reads(got)
    check("play guest bufs 1 " + page_bytes(BUFS_AT // PAGE)[:16 * BUFS_N].hex() in got,
          "a deferred buffer array read at the submit returns the live bytes")
    check("play guest handles 1 " + page_bytes(TEX_AT // PAGE)[TEX_AT % PAGE:TEX_AT % PAGE + 8 * TEX_N].hex() in got,
          "a deferred handle array read at the submit returns the live bytes")
    check("play collected control page 0" in got,
          "a GUEST's page is not listed by the player's collection, as the recorder's is not: %r" % got)
    check_guest_memory(r.stdout, guest_memfd == "1")


def refuse_capture(cap: Path, player: Path, stub: Path, tmp: Path, case: str, edit: Callable[[Path], None],
                   *must: str) -> None:
    # a copy of cap, edited: rx_player refuses it before the renderer starts, with one stderr line naming the cause
    case_dir = tmp / case
    _ = shutil.copytree(cap, case_dir)
    edit(case_dir)
    r = subprocess.run([str(player), str(case_dir), "--renderer", str(stub)], capture_output=True, text=True,
                       timeout=120)
    lines = r.stderr.splitlines()
    check(r.returncode != 0, "%s: rx_player exits nonzero" % case)
    check(r.stdout == "", "%s: the renderer is not started" % case)
    check(len(lines) == 1 and all(m in lines[0] for m in must),
          "%s: one line on stderr naming it: %r" % (case, r.stderr))


def check_player_refuses(cap: Path, player: Path, stub: Path, tmp: Path) -> None:
    def bump_api(d: Path) -> None:
        data = bytearray((d / "calls.rxc").read_bytes())
        data[12:16] = struct.pack("<I", api_version() + 1)
        _ = (d / "calls.rxc").write_bytes(bytes(data))

    def guest_past_memory(d: Path) -> None:
        raw = (d / "calls.rxc").read_bytes()
        at = 16
        while at < len(raw) and unpack("<I", raw, at)[0] != GUEST:
            at += unpack("<I", raw, at + 4)[0]
        check(at < len(raw), "the capture holds a GUEST record")
        data = bytearray(raw)
        data[at + 32:at + 40] = struct.pack("<Q", 1 << 20)   # its address: past the 16-page guest memory
        _ = (d / "calls.rxc").write_bytes(bytes(data))

    refuse_capture(cap, player, stub, tmp, "version", bump_api, "version")
    refuse_capture(cap, player, stub, tmp, "guest", guest_past_memory, "names bytes past the guest memory")


def check_player_version(cap: Path, player: Path, skew: Path) -> None:
    r = subprocess.run([str(player), str(cap), "--renderer", str(skew)], capture_output=True, text=True, timeout=120)
    lines = r.stderr.splitlines()
    check(r.returncode != 0 and len(lines) == 1 and "the renderer is MKWVK_API_VERSION" in lines[0],
          "a renderer table of another version: rx_player exits nonzero with one line naming it: %r" % r.stderr)
    check(not [line for line in play_lines(r.stdout) if line.split()[1] in BOUNDARY],
          "a renderer table of another version: no boundary call is replayed")


def check_scene_run(out: str) -> None:
    check("ok scene" in out.splitlines(), "scene: the fixture finished the scenario")


def scene_blocks(lines: list[str]) -> list[tuple[str, ...]]:
    # the stub's lines of each boundary call: the call's line, then the object and guest lines it prints
    blocks: list[list[str]] = []
    for line in lines:
        if line.startswith(("play nvn_", "play wait_writebacks_reading")):
            blocks.append([])
        if blocks:
            blocks[-1].append(line)
    return [tuple(b) for b in blocks]


def split_at_present(lines: list[str]) -> tuple[list[str], list[str]]:
    at = [i for i, line in enumerate(lines) if line == "play nvn_present"]
    check(len(at) == 1, "the play prints one present: %d" % len(at))
    return lines[:at[0]], lines[at[0]:]


def played_fences(calls: list[Call], answers: Answers) -> list[int]:
    # the fence each nvn_wait is played with (the fence rule): the stub's answer to the latest replayed nvn_fence or
    # nvn_fence_q whose recorded answer is the wait's fence, or 0 when no replayed call's answer is. Every fence call of
    # the capture is replayed, so the n-th one is answered FENCE_ANSWER + n
    played: dict[int, int] = {}
    fences = 0
    waits: list[int] = []
    for c in calls:
        if c.name in ("nvn_fence", "nvn_fence_q"):
            played[answers[c.rec.seq][2]] = FENCE_ANSWER + fences
            fences += 1
        elif c.name == "nvn_wait":
            waits.append(played.get(c.args[0], 0))
    return waits


def play_scene(player: Path, stub: Path, cap: Path) -> tuple[list[str], str]:
    env = {**os.environ, "HOS_GUEST_MEMFD": "0"}
    r = subprocess.run([str(player), str(cap), "--renderer", str(stub)], capture_output=True, text=True, timeout=120,
                       env=env)
    check(r.returncode == 0, "rx_player plays %s and exits 0: %r" % (cap, r.stderr[-2000:]))
    return play_lines(r.stdout), r.stderr


def check_scene(cap: Path, start: Path, player: Path, stub: Path) -> None:
    # cap is the scene recorded from the start, start the same scene armed by its file. The armed capture holds one
    # snapshot, which rebuilds the calls before its present; its play gives those calls the objects and guest reads the
    # from-start play gives them. From that present on the two plays print the same lines, except each nvn_wait's fence,
    # which the fence rule gives
    records, armed_records = read_capture(cap), read_capture(start)
    check_blobs(cap, records)
    check_blobs(start, armed_records)
    check(not [r for r in records if r.kind == SNAPSHOT], "the from-start capture holds no snapshot")
    snaps = [i for i, r in enumerate(armed_records) if r.kind == SNAPSHOT]
    check(len(snaps) == 1, "the armed capture holds one snapshot, not %d" % len(snaps))
    calls, _ = index_calls(records)
    armed_calls, armed_answers = index_calls(armed_records)
    present = [c for c in calls if c.name == "nvn_present"]
    armed_present = [c for c in armed_calls if c.name == "nvn_present"]
    check(len(present) == 1 and len(armed_present) == 1, "the scene presents once in each capture")
    check(armed_present[0].pos > snaps[0], "the armed capture's present is the one after its snapshot")
    scene = ["nvn_pool_created", "nvn_set_texture_pool_size", "nvn_texture_created", "nvn_texture_created",
             "nvn_register_texture", "nvn_register_sampler", "nvn_program_created", "nvn_program_created"]
    check([c.name for c in calls if c.pos < present[0].pos and c.name in BOUNDARY] == scene + ["nvn_fence"],
          "from the start: the scene's calls, then the fence handed out before the present")
    check([c.name for c in armed_calls if c.pos < armed_present[0].pos and c.name in BOUNDARY] == scene,
          "armed: the snapshot rebuilds the scene's calls, in the format's order, and no fence")
    lines, _ = play_scene(player, stub, cap)
    armed_lines, armed_err = play_scene(player, stub, start)
    _ = load_phases(armed_err)   # the armed play runs to its end: its load phases line is written after its load line
    from_pre, from_post = split_at_present(lines)
    armed_pre, armed_post = split_at_present(armed_lines)
    # the two scene textures' level-0 questions, asked by the play before the present: the stub's tiles (2, 1) come back
    # from the recorded answers in both plays, the from-start one's live answers and the armed one's snapshot answers
    for label, pre in (("from-start", from_pre), ("armed", armed_pre)):
        check(pre.count("play live levels 0 2 1") == 2,
              "%s: each scene texture's level 0 tiles are the stub's answer: %r" % (label, pre))
    armed_blocks = scene_blocks(armed_pre)
    check([b[0].split()[1] for b in armed_blocks] == scene,
          "armed: the stub gets the snapshot's calls in record order: %r" % [b[0] for b in armed_blocks])
    want_pre = sorted(b for b in scene_blocks(from_pre) if b[0] != "play nvn_fence")
    check(sorted(armed_blocks) == want_pre,
          "armed: before the present the stub's calls, objects and guest reads are the from-start play's: %s"
          % first_difference([repr(b) for b in sorted(armed_blocks)], [repr(b) for b in want_pre]))
    check("play guest control 1 " + CONTROL_OK.hex() in armed_pre,
          "armed: the program's control section is read live before the present")
    check("play guest code 1 " + filled(PLAY_FILL)[:CODE_END].hex() in armed_pre,
          "armed: the program's code is read live before the present")
    got_waits = [line for line in from_post if line.startswith("play nvn_wait ")]
    check(got_waits == ["play nvn_wait 3000 1000", "play nvn_wait 100 1000", "play nvn_wait 100 1000",
                        "play nvn_wait 3000 1000"],
          "from the start: each nvn_wait is played with its recorded fence: %r" % got_waits)
    fences = played_fences(armed_calls, armed_answers)
    check(fences == [FENCE_ANSWER, 0, FENCE_ANSWER + 1, FENCE_ANSWER + 2],
          "armed: the fence rule's fences are the stub's latest answer for the recorded fence, else 0: %r" % fences)
    want_post: list[str] = []
    k = 0
    for line in from_post:
        if line.startswith("play nvn_wait "):
            line = "play nvn_wait %d %s" % (fences[k], line.split()[3])
            k += 1
        want_post.append(line)
    check(armed_post == want_post,
          "armed: from the present on the lines are the from-start play's, each nvn_wait's fence as the rule gives: %s"
          % first_difference(armed_post, want_post))


def check_snapshot_refuses(start: Path, player: Path, stub: Path, tmp: Path) -> None:
    raw = (start / "calls.rxc").read_bytes()
    records = read_capture(start)
    snap = next(i for i, r in enumerate(records) if r.kind == SNAPSHOT)
    at = 16   # the snapshot's record in the file
    for _ in range(snap):
        at += unpack("<I", raw, at + 4)[0]
    total = unpack("<I", raw, at + 4)[0]
    check(total == 40, "the snapshot record holds one u64, its first: %d" % total)

    def set_first(value: int) -> Callable[[Path], None]:
        def edit(d: Path) -> None:
            data = bytearray(raw)
            data[at + 32:at + 40] = struct.pack("<Q", value)
            _ = (d / "calls.rxc").write_bytes(bytes(data))
        return edit

    def second(d: Path) -> None:
        record = struct.pack("<IIQQQ", SNAPSHOT, 40, len(records) + 1, 0, 0) + struct.pack("<Q", 1)
        _ = (d / "calls.rxc").write_bytes(raw + record)

    def long(d: Path) -> None:
        head = bytearray(raw[at:at + 32])
        head[4:8] = struct.pack("<I", 48)
        _ = (d / "calls.rxc").write_bytes(raw[:at] + bytes(head) + raw[at + 32:at + 40] + bytes(8) + raw[at + total:])

    refuse_capture(start, player, stub, tmp, "snapshot-second", second, "a second SNAPSHOT")
    refuse_capture(start, player, stub, tmp, "snapshot-seq", set_first(records[snap].seq),
                   "SNAPSHOT whose first record")
    refuse_capture(start, player, stub, tmp, "snapshot-zero", set_first(0), "SNAPSHOT whose first record")
    refuse_capture(start, player, stub, tmp, "snapshot-long", long, "SNAPSHOT of the wrong length")


# the layout questions: their first argument is an input texdesc, which the player reads from the renderer's memory, not
# from the capture, so an OBJECT that only such a question takes as its input is never read by the play
LAYOUT_INPUT = ("tex_layout", "tex_level_offset", "tex_level_tiles", "tex_linear_stride")


def named_by(records: list[Record]) -> dict[str, list[int]]:
    # each blob name, with the indices of the records that name it, in record order
    named: dict[str, list[int]] = {}
    for i, r in enumerate(records):
        if r.kind in (OBJECT, GUEST):
            names = [obj_of(r)[2]]
        elif r.kind == POOL:
            names = pool_of(r)[2]
        elif r.kind == PAGES:
            names = [d for _, d in pages_of(r)]
        else:
            continue
        for name in names:
            named.setdefault(name, []).append(i)
    return named


def blob_words(cap: Path) -> set[int]:
    # every 8-byte word of every blob, at each 8-byte offset: where a pointer kept in an object's bytes would be
    words: set[int] = set()
    for path in (cap / "blobs").iterdir():
        data = path.read_bytes()
        words.update(unpack("<Q", data, at)[0] for at in range(0, len(data) - 7, 8))
    return words


def unread_blob(cap: Path, records: list[Record], calls: list[Call], answers: Answers) -> str:
    # the first blob, in record order, that only OBJECTs name, each at an address that a layout question takes as its
    # first argument and nothing else takes, that no answer returns and no blob holds: the play never reads its bytes
    words = blob_words(cap)
    results = {ans[2] for ans in answers.values()}

    def only_layout_input(addr: int) -> bool:
        uses = [c for c in calls if addr in c.args]
        return bool(uses) and all(c.name in LAYOUT_INPUT and c.args[0] == addr and addr not in c.args[1:] for c in uses)

    def never_read(addr: int) -> bool:
        return only_layout_input(addr) and addr not in results and addr not in words

    for name, at in named_by(records).items():
        if all(records[i].kind == OBJECT and never_read(obj_of(records[i])[0]) for i in at):
            return name
    fail("the capture holds a blob that only layout questions' inputs name")


def late_blob(cap: Path, records: list[Record], calls: list[Call], answers: Answers) -> tuple[str, Call]:
    # the first blob, in record order, that only OBJECTs name, whose first record is the address's latest object at the
    # first boundary call after it, and that call takes the address as a pointer. No call before that one, no answer and
    # no blob names the address, so the play reads the blob when that call is replayed, after the boundary calls before
    # it
    words = blob_words(cap)
    results = {ans[2] for ans in answers.values()}
    boundary = [c for c in calls if c.name in BOUNDARY]
    for name, at in named_by(records).items():
        if any(records[i].kind != OBJECT for i in at):
            continue
        first = at[0]
        addr = obj_of(records[first])[0]
        call = next((c for c in boundary if c.pos > first), None)
        if call is None or call.name not in PLAY_POINTERS:
            continue
        if addr not in [call.args[p] for p in PLAY_POINTERS[call.name]]:
            continue
        if not any(c.pos < first for c in boundary):
            continue
        if any(addr in c.args for c in calls if c.pos < call.pos):
            continue
        if any(records[j].kind == OBJECT and obj_of(records[j])[0] == addr for j in range(first + 1, call.pos)):
            continue
        if addr in words or addr in results:
            continue
        return name, call
    fail("the capture holds a blob the play reads after some boundary calls")


def check_player_unread(cap: Path, rec_out: str, player: Path, stub: Path, tmp: Path) -> None:
    # the play reads only the blobs its uses name: with a blob that no use names deleted, the play is the same as before
    records = read_capture(cap)
    calls, answers = index_calls(records)
    name = unread_blob(cap, records, calls, answers)
    case_dir = tmp / "unread"
    _ = shutil.copytree(cap, case_dir)
    (case_dir / "blobs" / name).unlink()
    r = subprocess.run([str(player), str(case_dir), "--renderer", str(stub)], capture_output=True, text=True,
                       timeout=120)
    check(r.returncode == 0, "the blob %s is never read: rx_player exits 0 without it: %r" % (name, r.stderr[-2000:]))
    got = [line for line in play_lines(r.stdout) if not line.startswith(BEYOND)]
    rec = [line for line in play_lines(rec_out) if not line.startswith(BEYOND)]
    want = rec + ["play nvn_finish"]
    check(got == want, "without the unread blob the stub's lines are the scenario's: %s"
          % first_difference(got, want))


def reread_blob(records: list[Record], calls: list[Call]) -> tuple[str, Call]:
    # the first blob, in record order, that one OBJECT names, at an address that the play first reaches at an
    # nvn_submit_q and reaches again at a later boundary call, with no OBJECT naming another blob for the address in
    # between: the play reads the blob at the first of those calls, and the later call is given the bytes the player kept
    boundary = [c for c in calls if c.name in BOUNDARY]
    for name, at in named_by(records).items():
        if len(at) != 1 or records[at[0]].kind != OBJECT:
            continue
        addr = obj_of(records[at[0]])[0]
        reaches = [c for c in boundary if c.pos > at[0] and c.name in PLAY_POINTERS
                   and addr in [c.args[p] for p in PLAY_POINTERS[c.name]]]
        if len(reaches) < 2 or reaches[0].name != "nvn_submit_q":
            continue
        if any(records[j].kind == OBJECT and obj_of(records[j])[0] == addr for j in range(at[0] + 1, reaches[-1].pos)):
            continue
        return name, reaches[0]
    fail("the capture holds a blob that the play reads at an nvn_submit_q and reaches again at a later call")


def check_player_reread(cap: Path, rec_out: str, player: Path, stub: Path, tmp: Path) -> None:
    # a blob the play has read is kept for the later calls that reach its object: the stub removes the blob's file
    # when the first nvn_submit_q arrives, the next call to reach the same unchanged object is given the kept bytes,
    # and the play exits 0 with the stub's lines the recording gave
    records = read_capture(cap)
    calls, _ = index_calls(records)
    name, _ = reread_blob(records, calls)
    case_dir = tmp / "reread"
    _ = shutil.copytree(cap, case_dir)
    path = case_dir / "blobs" / name
    env = {**os.environ, "PLAY_STUB_VANISH": str(path)}
    r = subprocess.run([str(player), str(case_dir), "--renderer", str(stub)], capture_output=True, text=True,
                       timeout=120, env=env)
    check(not path.exists(), "the stub removed the blob %s when the first nvn_submit_q arrived" % name)
    check(r.returncode == 0, "the blob %s is read once and kept: rx_player exits 0 without its file: %r"
          % (name, r.stderr[-2000:]))
    got = [line for line in play_lines(r.stdout) if not line.startswith(BEYOND)]
    rec = [line for line in play_lines(rec_out) if not line.startswith(BEYOND)]
    want = rec + ["play nvn_finish"]
    check(got == want, "without the blob's file the stub's lines are the scenario's: %s"
          % first_difference(got, want))


def stub_calls_before(calls: list[Call], pos: int) -> list[list[object]]:
    # the boundary calls before record pos, as the stub is given them
    return [[c.name, [str(a) for i, a in enumerate(c.args) if i not in PLAY_POINTERS.get(c.name, ())]]
            for c in calls if c.name in BOUNDARY and c.pos < pos]


def corrupt_file(path: Path) -> None:
    data = path.read_bytes()
    _ = path.write_bytes(bytes([data[0] ^ 1]) + data[1:])


def damaged_pool(records: list[Record]) -> tuple[int, str, str]:
    # the first POOL, in record order, whose two lowest pages name blobs that no record before it names and that it names
    # once: the play reads a POOL's blobs when it applies the record, so the first read of each is there. Returns the
    # record's index, the blob of its lowest page and the blob of the next page that qualifies
    named = named_by(records)
    for i, r in enumerate(records):
        if r.kind != POOL:
            continue
        fresh = [h for h in pool_of(r)[2] if named[h][0] == i and named[h].count(i) == 1]
        if len(fresh) >= 2:
            return i, fresh[0], fresh[1]
    fail("the capture holds a POOL whose two lowest pages name blobs no record before it names")


def check_player_stops(cap: Path, player: Path, stub: Path, tmp: Path) -> None:
    # a blob the play reads stops the play at that read: the boundary calls before it reach the stub, the player's
    # finish follows, and stderr is one line naming the blob
    records = read_capture(cap)
    calls, answers = index_calls(records)
    name, read_at = late_blob(cap, records, calls, answers)
    check(any(c.name in BOUNDARY and c.pos < read_at.pos for c in calls),
          "a boundary call precedes the read of %s" % name)
    want = stub_calls_before(calls, read_at.pos) + [["nvn_finish", []]]

    def stop(case: str, edit: Callable[[Path], None], expected: list[list[object]], *must: str) -> str:
        case_dir = tmp / case
        _ = shutil.copytree(cap, case_dir)
        edit(case_dir)
        r = subprocess.run([str(player), str(case_dir), "--renderer", str(stub)], capture_output=True, text=True,
                           timeout=120)
        check(r.returncode != 0, "%s: rx_player exits nonzero" % case)
        got = [[line.split()[1], line.split()[2:]] for line in play_lines(r.stdout) if line.split()[1] in BOUNDARY]
        check(got == expected, "%s: the stub gets the boundary calls before the read, then the finish: %s"
              % (case, first_difference([repr(c) for c in got], [repr(c) for c in expected])))
        lines = r.stderr.splitlines()
        check(len(lines) == 1 and all(m in lines[0] for m in must),
              "%s: one line on stderr naming the blob: %r" % (case, r.stderr))
        return r.stderr

    def remove_blob(d: Path) -> None:
        (d / "blobs" / name).unlink()

    def corrupt_blob(d: Path) -> None:
        corrupt_file(d / "blobs" / name)

    _ = stop("stop-missing", remove_blob, want, "missing blob", name)
    _ = stop("stop-corrupt", corrupt_blob, want, "does not match its name", name)
    # a POOL whose two lowest pages' blobs are both damaged: the play stops at that record, and the one line names the
    # lower page's blob, not the higher's. A POOL before the first boundary call fails the play before the renderer
    # starts, so the stub gets no call and no finish; one after a call gets the calls before it and the finish
    pool_at, low, high = damaged_pool(records)
    pool_want = stub_calls_before(calls, pool_at)
    if pool_want:
        pool_want.append(["nvn_finish", []])

    def corrupt_two(d: Path) -> None:
        corrupt_file(d / "blobs" / low)
        corrupt_file(d / "blobs" / high)

    stderr = stop("stop-pool-two", corrupt_two, pool_want, "does not match its name", low)
    check(high not in stderr, "stop-pool-two: the line names the lower page's blob, not the higher's: %r" % stderr)


def fail(msg: str) -> NoReturn:
    raise SystemExit("FAIL: " + msg)


def check(ok: bool, msg: str) -> None:
    if not ok:
        fail(msg)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def filled(value: int) -> bytes:
    return bytes((i * 7 + value) & 0xFF for i in range(PAGE))


def page_bytes(p: int, rewritten: bool = False) -> bytes:
    return filled(REWRITE[p] if rewritten and p in REWRITE else PAT[p])


def page_digest(p: int, rewritten: bool = False) -> str:
    return sha(page_bytes(p, rewritten))


def pool_pages(start: int, size: int) -> range:
    return range(start // PAGE, (start + size - 1) // PAGE + 1)


def api_version() -> int:
    m = re.search(r"#define MKWVK_API_VERSION (\d+)", API_HEADER.read_text(encoding="utf-8"))
    if m is None:
        fail("gpu/vk/vk_api.h defines MKWVK_API_VERSION")
    return int(m.group(1))


def read_capture(cap: Path) -> list[Record]:
    check((cap / "calls.rxc").is_file(), "the capture writes calls.rxc")
    data = (cap / "calls.rxc").read_bytes()
    check(data[:8] == b"RXCAPT01", "calls.rxc starts with the magic RXCAPT01")
    fmt, api = unpack("<II", data, 8)
    check(fmt == 1, "the format version is 1")
    check(api == api_version(), "the file header carries MKWVK_API_VERSION")
    records: list[Record] = []
    at = 16
    while at < len(data):
        check(len(data) - at >= 32, "a whole record header at byte %d" % at)
        kind, total, seq, thread, queue = unpack("<IIQQQ", data, at)
        check(32 <= total and at + total <= len(data) and total % 8 == 0, "record %d is whole" % len(records))
        records.append(Record(kind, seq, thread, queue, data[at + 32: at + total]))
        at += total
    check([r.seq for r in records] == list(range(1, len(records) + 1)), "seq runs 1, 2, 3, ... in file order")
    return records


def unpack(fmt: str, data: bytes, at: int = 0) -> tuple[int, ...]:
    return tuple(int(v) for v in struct.unpack_from(fmt, data, at))


def obj_of(r: Record) -> tuple[int, int, str]:
    addr, size = unpack("<QQ", r.payload)
    return addr, size, r.payload[16:48].hex()


def pool_of(r: Record) -> tuple[int, int, list[str]]:
    start, size = unpack("<QQ", r.payload)
    hashes = [r.payload[16 + 32 * i: 48 + 32 * i].hex() for i in range((len(r.payload) - 16) // 32)]
    return start, size, hashes


def pages_of(r: Record) -> list[tuple[int, str]]:
    count = unpack("<Q", r.payload)[0]
    check(len(r.payload) == 8 + 40 * count, "a PAGES record holds count entries of 40 bytes")
    return [(unpack("<Q", r.payload, 8 + 40 * i)[0], r.payload[16 + 40 * i: 48 + 40 * i].hex()) for i in range(count)]


def check_blobs(cap: Path, records: list[Record]) -> None:
    referenced: set[str] = set()
    for r in records:
        if r.kind in (OBJECT, GUEST):   # (a GUEST has the same layout: its address, its size, its SHA-256)
            referenced.add(obj_of(r)[2])
        elif r.kind == POOL:
            referenced.update(pool_of(r)[2])
        elif r.kind == PAGES:
            referenced.update(d for _, d in pages_of(r))
    files = set(os.listdir(cap / "blobs"))
    check(files == referenced, "the blobs are exactly the contents the records name, each stored once")
    for name in files:
        check(sha((cap / "blobs" / name).read_bytes()) == name, "blob %s is named by its SHA-256" % name)


def index_calls(records: list[Record]) -> tuple[list[Call], Answers]:
    calls: list[Call] = []
    answers: Answers = {}
    for i, r in enumerate(records):
        if r.kind in (OBJECT, POOL, PAGES, GUEST, SNAPSHOT):
            continue
        if r.kind & ANSWER:
            base = r.kind & ~ANSWER
            check(base in NAMES and len(r.payload) == 16, "answer %d is a known kind of 16 bytes" % i)
            call_seq, result = unpack("<QQ", r.payload)
            answers[call_seq] = (i, base, result, r)
        else:
            check(r.kind in NAMES, "record %d is a known kind" % i)
            args = list(unpack("<%dQ" % (len(r.payload) // 8), r.payload))
            calls.append(Call(i, r, NAMES[r.kind], args))
    for c in calls:
        check(c.rec.seq in answers, "%s has its answer" % c.name)
        _, base, _, ar = answers[c.rec.seq]
        check(base == c.rec.kind and ar.thread == c.rec.thread and ar.queue == c.rec.queue,
              "%s: its answer is on its thread and queue" % c.name)
        want = c.args[QUEUE_ARG[c.name]] if c.name in QUEUE_ARG else 0
        check(c.rec.queue == want, "%s carries queue %d in its header" % (c.name, want))
    return calls, answers


def fwd_call(line: str) -> tuple[str, list[int], int]:
    m = FWD.fullmatch(line)
    if m is None:
        fail("a stub's line parses: %r" % line)
    return m.group(2), [int(a) for a in m.group(3).split()], int(m.group(4))


def check_order(calls: list[Call], answers: Answers, fwd_lines: list[str], skip: set[str] = INTERNAL,
                threads: int = 2) -> None:
    kept: dict[int, list[tuple[str, tuple[int, ...], int]]] = {}
    for c in calls:
        if c.name not in skip:
            kept.setdefault(c.rec.thread, []).append((c.name, tuple(c.args), answers[c.rec.seq][2]))
    stubs: dict[int, list[tuple[str, tuple[int, ...], int]]] = {}
    for line in fwd_lines:
        m = FWD.fullmatch(line)
        if m is None:
            fail("a stub's line parses: %r" % line)
        tid, name, args, ret = m.groups()
        stubs.setdefault(int(tid), []).append((name, tuple(int(a) for a in args.split()), int(ret)))
    check(len(kept) == threads and set(kept) == set(stubs),
          "%d thread(s) made the calls, and the stubs saw the same threads" % threads)
    for tid, want in stubs.items():
        got = kept[tid]
        at = next((i for i, (g, w) in enumerate(zip(got, want)) if g != w), min(len(got), len(want)))
        check(got == want, "thread %d, call %d: kept %r, the stub got %r" % (
            tid, at, got[at:at + 1], want[at:at + 1]))


def check_pools(records: list[Record], calls: list[Call]) -> None:
    pool_at = [i for i, r in enumerate(records) if r.kind == POOL]
    expect = HOST_POOLS + [(0xC000, 0x1000)]
    check(len(pool_at) == 3, "three POOL records: the two pools at start, and the one made")
    for (start, size), i in zip(expect, pool_at):
        got_start, got_size, hashes = pool_of(records[i])
        check((got_start, got_size) == (start, size), "POOL %#x has its start and size" % start)
        check(hashes == [page_digest(p) for p in pool_pages(start, size)], "POOL %#x: each page's SHA-256" % start)
    check(pool_at[1] < calls[0].pos, "the pools at start come before the first call")
    created = [c for c in calls if c.name == "nvn_pool_created"]
    check(len(created) == 1 and created[0].pos == pool_at[2] + 1, "the pool made is recorded just before its call")


def check_submits(records: list[Record], calls: list[Call]) -> list[Call]:
    subs = [c for c in calls if c.name in SUBMITS]
    check(len(subs) == 5, "five submits: two on the main thread, then three on the worker")
    want = [[(2, page_digest(2))], [(2, page_digest(2)), (5, page_digest(5))]] + [[(3, page_digest(3, True))]] * 3
    check_dirty(records, subs, want)
    return subs


def check_dirty(records: list[Record], subs: list[Call], want: list[list[tuple[int, str]]]) -> None:
    for c, expect in zip(subs, want):
        k = c.pos - 1
        while records[k].kind in (OBJECT, GUEST):
            k -= 1
        check(records[k].kind == PAGES, "%s is preceded by its PAGES record" % c.name)
        got = pages_of(records[k])
        check(got == expect, "%s: its dirty pages are %r, not %r" % (c.name, [p for p, _ in got], [p for p, _ in expect]))


def obj_key(r: Record) -> tuple[int, int]:
    addr, size = unpack("<QQ", r.payload)
    return addr, size


def check_printed(records: list[Record], lines: list[str], subs: list[Call]) -> None:
    printed: dict[int, list[tuple[str, int, int, str]]] = {1: [], 2: []}
    section = 0
    for line in lines:
        m = re.fullmatch(r"submit (\d)", line)
        if m:
            section = int(m.group(1))
            continue
        m = OBJ.fullmatch(line)
        if m:
            printed[section].append((m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)))
    check(len(printed[1]) == 9 and len(printed[2]) == 9, "the fixture printed the nine objects of each submit")
    bounds = [-1, subs[0].pos, subs[1].pos]
    last: dict[tuple[int, int], str] = {}
    keys: set[tuple[int, int]] = set()
    for k in (1, 2):
        lo, hi = bounds[k - 1], bounds[k]
        for label, addr, size, hexd in printed[k]:
            digest = sha(bytes.fromhex(hexd))
            key = (addr, size)
            keys.add(key)
            here = [i for i in range(lo + 1, hi) if records[i].kind == OBJECT and obj_key(records[i]) == key]
            if last.get(key) == digest:
                check(not here, "%s: bytes unchanged since its last record are not written again (submit %d)" % (label, k))
            else:
                check(len(here) == 1 and obj_of(records[here[0]])[2] == digest,
                      "%s: its new bytes are written once, before submit %d" % (label, k))
            last[key] = digest
    late = [i for i in range(subs[1].pos + 1, len(records)) if records[i].kind == OBJECT and obj_key(records[i]) in keys]
    check(not late, "no object the submits used is written again after the second submit")


def last_object(records: list[Record], addr: int, size: int, before: int) -> str | None:
    for i in range(before - 1, -1, -1):
        if records[i].kind == OBJECT:
            a, s, d = obj_of(records[i])
            if (a, s) == (addr, size):
                return d
    return None


def check_survey(records: list[Record], calls: list[Call], answers: Answers, guest: int, main: int) -> None:
    def one(name: str) -> Call:
        found = [c for c in calls if c.name == name and c.rec.thread == main]
        check(len(found) == 1, "%s is called once on the main thread" % name)
        return found[0]

    def answer(c: Call) -> int:
        return answers[c.rec.seq][2]

    check(answer(one("guest_base")) == guest, "guest_base's answer is the window")
    check(answer(one("guest_size")) == NPAGES * PAGE, "guest_size's answer is the window's size")
    c = one("gpu_pools")
    check(answer(c) == 2 and last_object(records, c.args[0], 32, answers[c.rec.seq][0]) ==
          sha(struct.pack("<4Q", 0, 0x4000, 0x8000, 0x2000)), "gpu_pools answers two pools, kept as their entries")
    check(answer(one("fmt_bpb")) == 4, "fmt_bpb's answer is 4 bytes per block")
    asks = [c for c in calls if c.name == "wide_cover"]
    bits = unpack("<I", struct.pack("<f", 0.25))[0]
    check(bool(asks) and all(c.rec.kind == 107 and answers[c.rec.seq][2] == bits for c in asks),
          "wide_cover is recorded as CALL 107 with the stub's float bits as its ANSWER")


def check_bytes(records: list[Record], calls: list[Call], answers: Answers) -> None:
    for c in calls:
        ai = answers[c.rec.seq][0]
        if c.name == "gpu_read" and c.args[0] == 0:
            _, dst, n = c.args
            check(last_object(records, dst, n, ai) == sha(page_bytes(0)[:n]), "gpu_read's output bytes are kept")
        elif c.name == "gpu_write":
            _, src, n = c.args
            check(last_object(records, src, n, c.pos) == sha(bytes(0x40 + i for i in range(n))),
                  "gpu_write's input bytes are kept before the call")
        elif c.name == "log":
            check(last_object(records, c.args[0], 7, c.pos) == sha(b"survey\0"), "log's line is kept before the call")
        elif c.name == "mic_select":
            check(last_object(records, c.args[1], 5, c.pos) == sha(b"mic0\0"), "mic_select's name is kept before the call")
        elif c.name == "mic_name":
            check(last_object(records, answers[c.rec.seq][2], 5, ai) == sha(b"mic0\0"), "mic_name's answer is kept")
        elif c.name == "romfs_dir":
            check(last_object(records, answers[c.rec.seq][2], 7, ai) == sha(b"/romfs\0"), "romfs_dir's answer is kept")
        elif c.name == "nvn_read_texels":
            check(last_object(records, c.args[4], 16, ai) == sha(b"\x5a" * 16), "read_texels' texels are kept after the call")
        elif c.name == "nvn_write_texels":
            check(last_object(records, c.args[4], 16, c.pos) == sha(bytes(0x30 + i for i in range(16))),
                  "write_texels' texels are kept before the call")


def check_collects(records: list[Record], calls: list[Call], answers: Answers, main: int) -> None:
    worker = next(t for t in {c.rec.thread for c in calls} if t != main)
    want: dict[int, list[list[int]]] = {main: [[2, 5]], worker: [[3], [3], [3]]}
    got: dict[int, list[list[int]]] = {main: [], worker: []}
    for c in calls:
        if c.name != "collect_written":
            continue
        ai, _, n, _ = answers[c.rec.seq]
        if not n:
            continue
        check(len(got[c.rec.thread]) < len(want[c.rec.thread]), "no more pages than the renderer was given")
        pages = want[c.rec.thread][len(got[c.rec.thread])]
        got[c.rec.thread].append(pages)
        check(len(pages) == n, "collect_written answered %d pages" % n)
        digest = sha(struct.pack("<%dI" % len(pages), *pages))
        check(any(records[i].kind == OBJECT and obj_of(records[i])[1] == 4 * len(pages) and
                  obj_of(records[i])[2] == digest for i in range(c.pos + 1, ai)),
              "the renderer got the marked pages %r, kept as the collection's list" % pages)
    check(got == want, "the renderer got every marked page: %r, not %r" % (got, want))


def check_guests(records: list[Record], subs: list[Call]) -> None:
    # the four ranges the submits and the early creation read by guest address, each written once, before the first
    # submit's CALL, with the bytes they hold: the control section (page 10), the buffer ranges and the handles of a
    # deferred bind (page 11), and the early program's control section (page 11). The later submits read the same bytes,
    # so they write nothing; the only GUEST after the first submit is the code of the later creation (check_created)
    want = [(CONTROL_AT, CONTROL_SIZE, CONTROL_OK),
            (BUFS_AT, 16 * BUFS_N, page_bytes(BUFS_AT // PAGE)[:16 * BUFS_N]),
            (TEX_AT, 8 * TEX_N, page_bytes(TEX_AT // PAGE)[TEX_AT % PAGE:TEX_AT % PAGE + 8 * TEX_N]),
            (EARLY_CONTROL_AT, CONTROL_SIZE, CONTROL_BAD)]
    got: list[tuple[int, int, int, str]] = []
    for i, r in enumerate(records):
        if r.kind != GUEST:
            continue
        addr, size, digest = obj_of(r)
        if i < subs[0].pos:
            got.append((i, addr, size, digest))
        else:
            check((addr, size) == (CODE_AT, CODE_END),
                  "after the first submit, the only GUEST is a program's code: %#x+%d" % (addr, size))
    check(len(got) == len(want), "GUEST: one record per changed range before the first submit, %d, not %d"
          % (len(want), len(got)))
    for addr, size, data in want:
        found = [(i, digest) for i, a, s, digest in got if (a, s) == (addr, size)]
        check(len(found) == 1, "GUEST %#x+%d: written once, not %d times" % (addr, size, len(found)))
        check(found[0][1] == sha(data), "GUEST %#x+%d holds the bytes the renderer reads" % (addr, size))


def creation_records(records: list[Record], call: Call) -> list[tuple[int, Record]]:
    # the records written for a call before its CALL: its OBJECT, POOL, PAGES and GUEST records, back to the call before
    block: list[tuple[int, Record]] = []
    k = call.pos - 1
    while k >= 0 and records[k].kind in (OBJECT, POOL, PAGES, GUEST):
        block.append((k, records[k]))
        k -= 1
    return block


def check_no_pages_at_creation(records: list[Record], made: list[Call]) -> None:
    for c in made:
        check(all(r.kind != PAGES for _, r in creation_records(records, c)),
              "nvn_program_created (record %d) has no PAGES record before its CALL" % c.pos)


def check_code_at_creation(records: list[Record], made: list[Call], code: bytes) -> None:
    # prog0's control section passes the renderer's test: its code, CODE_END bytes at CODE_AT, is a GUEST before its
    # CALL
    guests = [obj_of(r) for _, r in creation_records(records, made[1]) if r.kind == GUEST]
    check([(a, s) for a, s, _ in guests] == [(CODE_AT, CODE_END)],
          "prog0's creation keeps its code as one GUEST before its CALL: %r" % [(a, s) for a, s, _ in guests])
    check(guests[0][2] == sha(code), "prog0's code GUEST holds the bytes the renderer reads at its creation")


def check_no_code_at_failing_creation(records: list[Record], made: list[Call]) -> None:
    # the early program's control section fails the renderer's test (its stage is 9): its creation keeps no code, only
    # its control section
    guests = [obj_of(r)[:2] for _, r in creation_records(records, made[0]) if r.kind == GUEST]
    check(guests == [(EARLY_CONTROL_AT, CONTROL_SIZE)],
          "the early program's creation keeps no code GUEST, only its control section: %r" % guests)


def check_created(records: list[Record], calls: list[Call], code: bytes) -> None:
    # two programs are created: the early one, before the first submit, which binds it, and prog0
    made = [c for c in calls if c.name == "nvn_program_created"]
    check(len(made) == 2, "two nvn_program_created: the early program's, then prog0's, not %d" % len(made))
    first_submit = min(c.pos for c in calls if c.name in SUBMITS)
    check(made[0].pos < first_submit, "a program is created before the first submit, which binds it")
    check_no_pages_at_creation(records, made)
    check_code_at_creation(records, made, code)
    check_no_code_at_failing_creation(records, made)


def check_on(cap: Path, out: str) -> None:
    lines = out.splitlines()
    check(not [line for line in lines if line.startswith("FAIL")], "the fixture's own checks held")
    check("ok on" in lines, "the fixture finished the scenario")
    check([line for line in lines if line.startswith("host-log:")] == ["host-log: survey"],
          "the host logs only the survey's line")
    end = lines.index("end")
    guest = int(next(line.split()[1] for line in lines[:end] if line.startswith("guest ")))
    fwd_lines = [line for line in lines[:end] if line.startswith("fwd ")]
    records = read_capture(cap)
    check_blobs(cap, records)
    calls, answers = index_calls(records)
    main = calls[0].rec.thread
    check_order(calls, answers, fwd_lines)
    check_pools(records, calls)
    subs = check_submits(records, calls)
    check_printed(records, lines, subs)
    check_guests(records, subs)
    check_created(records, calls, page_bytes(CODE_AT // PAGE)[:CODE_END])
    check_survey(records, calls, answers, guest, main)
    check_bytes(records, calls, answers)
    check_collects(records, calls, answers, main)


def check_hook_on(cap: Path, out: str) -> None:
    lines = out.splitlines()
    check(not [line for line in lines if line.startswith("FAIL")], "hook-on: the fixture's own checks held")
    check("ok hook-on" in lines and "end" in lines, "hook-on: the fixture finished the scenario")
    end = lines.index("end")
    fwd_lines = [line for line in lines[:end] if line.startswith("fwd ")]
    records = read_capture(cap)
    check_blobs(cap, records)
    calls, answers = index_calls(records)
    check({c.name for c in calls} >= set(BOUNDARY), "hook-on: each of the 23 renderer entries is in the capture")
    check_order(calls, answers, fwd_lines, skip=INTERNAL | REAL_HOST, threads=1)
    check_pools(records, calls)
    subs = [c for c in calls if c.name in SUBMITS]
    check(len(subs) == 2, "hook-on: two submits")
    check_dirty(records, subs, [[(2, page_digest(2)), (9, page_digest(9))]] * 2)
    check_guests(records, subs)
    check_created(records, calls, page_bytes(CODE_AT // PAGE)[:CODE_END])
    check_bytes(records, calls, answers)


def check_start(cap: Path, out: str) -> None:
    lines = out.splitlines()
    check(not [line for line in lines if line.startswith("FAIL")], "start: the fixture's own checks held")
    check("ok start" in lines, "start: the fixture finished the scenario")
    marker, end = lines.index("start file"), lines.index("end")
    addr = {p[2]: int(p[3]) for p in (line.split() for line in lines if line.startswith("start addr "))}
    made: dict[str, tuple[int, int, str]] = {}
    for line in lines[:end]:
        m = OBJ.fullmatch(line)
        if m and m.group(1).startswith("start_"):
            made[m.group(1)] = (int(m.group(2)), int(m.group(3)), m.group(4))
    records = read_capture(cap)
    check_blobs(cap, records)
    calls, answers = index_calls(records)
    snaps = [i for i, r in enumerate(records) if r.kind == SNAPSHOT]
    check(len(snaps) == 1, "start: the file holds one snapshot record, not %d" % len(snaps))
    snap = snaps[0]
    first = unpack("<Q", records[snap].payload)[0]
    check(records[0].kind == POOL and records[0].seq == first,
          "start: the file's first record is the snapshot's first (a POOL of the host's pools)")
    # the snapshot's calls, in the order of the format: the pools made, the pool size, the live textures (the released
    # one absent), the texture registrations (the latest per id), the sampler registrations, the live programs (the
    # released one absent). Nothing the calls made before the file is among them
    want = [("nvn_pool_created", [0xC000, 0x1000]), ("nvn_pool_created", [0xE000, 0x800]),
            ("nvn_set_texture_pool_size", [64])]
    want += [("nvn_texture_created", [t]) for t in sorted([addr["tex0"], addr["start_tex2"], addr["start_tex3"]])]
    want += [("nvn_register_texture", [1, addr["start_tex2"], 0]),
             ("nvn_register_texture", [2, addr["start_tex2"], addr["view"]])]
    want += [("nvn_register_sampler", [2, addr["start_samp_b"]]), ("nvn_register_sampler", [4, addr["samp"]]),
             ("nvn_program_created", [addr["prog0"]])]
    got = [(c.name, c.args) for c in calls if c.pos < snap and c.name in BOUNDARY]
    check(got == want, "start: the snapshot's calls rebuild the state, in order: %r, not %r" % (got, want))
    # the bytes each object holds: as its call made them (the texture the game overwrote after its creation keeps the
    # created bytes), and the guest bytes of prog0's control section and code
    for label in ("start_tex0", "start_tex2", "start_pool", "start_view", "start_samp", "start_samp_b", "start_prog0"):
        a, size, hexd = made[label]
        check(last_object(records, a, size, snap) == sha(bytes.fromhex(hexd)),
              "start: %s is kept as its call made it" % label)
    guests = [obj_of(r) for r in records[:snap] if r.kind == GUEST]
    check(guests == [(CONTROL_AT, CONTROL_SIZE, sha(CONTROL_OK)),
                     (CODE_AT, CODE_END, sha(page_bytes(CODE_AT // PAGE)[:CODE_END]))],
          "start: prog0's control section and code are kept as GUEST records: %r" % guests)
    check(not [r for r in records[:snap] if r.kind == OBJECT and obj_of(r)[0] in (addr["tex1"], addr["prog1"])],
          "start: the released texture and program are not in the snapshot")
    # the host's answers about the live textures, kept before the snapshot's first texture, on the present's thread with
    # queue 0: gpu_guest for each page of a storage outside the listed pools, once (none for the storage inside them),
    # and tex_level_offset and tex_level_tiles for each level of each live texture, at least level 0. Each takes the
    # descriptor the texture was created with: its OBJECT is before the CALL, and the stub's answers come back
    asked = [c for c in calls if c.pos < snap and c.name in ASKED]
    first_tex = min(c.pos for c in calls if c.pos < snap and c.name == "nvn_texture_created")
    check(bool(asked) and all(c.pos < first_tex for c in asked),
          "start: the host's answers are kept before the snapshot's first texture")
    check(all(c.rec.thread == records[snap].thread and c.rec.queue == 0 for c in asked),
          "start: the host's answers are on the present's thread, with queue 0")
    pages = [c.args for c in asked if c.name == "gpu_guest"]
    check(pages == [[0x4000], [0x5000], [0xC000]],
          "start: gpu_guest asks each page of the storages outside the listed pools once: %r" % pages)
    check(all(answers[c.rec.seq][2] == c.args[0] for c in asked if c.name == "gpu_guest"),
          "start: gpu_guest's answers are the stub's")
    for label, levels in (("start_tex0", 1), ("start_tex2", 2), ("start_tex3", 1)):
        d, (d_at, dsize, dhex) = addr[label + "_d"], made[label + "_d"]
        check(d_at == d, "start: %s_d is the descriptor's address" % label)
        for level in range(levels):
            offsets = [c for c in asked if c.name == "tex_level_offset" and c.args == [d, level]]
            check(len(offsets) == 1, "start: %s asks tex_level_offset of level %d once" % (label, level))
            off = offsets[0]
            check(answers[off.rec.seq][2] == level * 256,
                  "start: %s level %d's offset is the stub's answer" % (label, level))
            check(last_object(records, d, dsize, off.pos) == sha(bytes.fromhex(dhex)),
                  "start: %s's descriptor is an OBJECT before its level %d offset" % (label, level))
            tiles = [c for c in asked if c.name == "tex_level_tiles" and c.args[:2] == [d, level]]
            check(len(tiles) == 1, "start: %s asks tex_level_tiles of level %d once" % (label, level))
            t = tiles[0]
            ans = answers[t.rec.seq][0]
            check(t.pos < ans, "start: %s's level %d tiles are answered after the call" % (label, level))
            check(last_object(records, t.args[2], 4, ans) == sha(struct.pack("<I", 2)) and
                  last_object(records, t.args[3], 4, ans) == sha(struct.pack("<I", 1)),
                  "start: %s's level %d tiles are the stub's outputs" % (label, level))
            check(last_object(records, d, dsize, t.pos) == sha(bytes.fromhex(dhex)),
                  "start: %s's descriptor is an OBJECT before its level %d tiles" % (label, level))
    # the stub got every call, the ones before the file too; after the file, the capture keeps the calls as an unarmed
    # capture does: the present that found the file first, then each call with its objects and answer
    # each with the arguments the scenario passed and the stub's answer: the armed capture forwards them unchanged
    before = [("nvn_pool_created", [0xC000, 0x1000]), ("nvn_pool_created", [0xE000, 0x800]),
              ("nvn_set_texture_pool_size", [64]), ("nvn_texture_created", [addr["tex0"]]),
              ("nvn_texture_created", [addr["tex1"]]), ("nvn_texture_created", [addr["start_tex2"]]),
              ("nvn_texture_created", [addr["start_tex3"]]),
              ("nvn_texture_released", [addr["tex1"]]), ("nvn_register_texture", [1, addr["tex0"], addr["view"]]),
              ("nvn_register_texture", [1, addr["start_tex2"], 0]),
              ("nvn_register_texture", [2, addr["start_tex2"], addr["view"]]),
              ("nvn_register_sampler", [4, addr["samp"]]), ("nvn_register_sampler", [2, addr["start_samp_b"]]),
              ("nvn_program_created", [addr["prog1"]]), ("nvn_program_created", [addr["prog0"]]),
              ("nvn_program_released", [addr["prog1"]]), ("nvn_submit", [addr["handles"], 2]),
              ("nvn_present", [addr["tex0"], addr["crop"]]), ("nvn_present", [addr["tex0"], addr["crop"]])]
    forwarded = [(m.group(2), [int(v) for v in m.group(3).split()], int(m.group(4)))
                 for m in (FWD.fullmatch(line) for line in lines[:marker]) if m and m.group(2) in BOUNDARY]
    check(forwarded == [(name, args, 0) for name, args in before],
          "start: the stub got every call made before the file, in order, unchanged: %r" % forwarded)
    # the host log names the present the capture started at, counted over every present the renderer got from its start:
    # the presents before the file, and the one that found it
    started = sum(name == "nvn_present" for name, _, _ in forwarded) + 1
    logged = [line for line in lines if line.startswith("host-log:")]
    check(logged == ["host-log: rx capture: started at present %d" % started],
          "start: the host logs one line, that the capture started at present %d: %r" % (started, logged))
    post = [c for c in calls if c.pos > snap]
    check(bool(post) and post[0].name == "nvn_present" and post[0].args == [addr["start_tex2"], addr["crop"]],
          "start: the present that found the file is the first call after the snapshot")
    # (the start's line reaches the host through the recorder's own call, which calls.rxc does not keep: the stub prints
    # it as a log call, and the host-log check above reads it)
    stub_calls = [line for line in lines[marker:end] if line.startswith("fwd ") and line.split()[2] != "log"]
    names = [fwd_call(line)[0] for line in stub_calls]
    check("nvn_present" in names, "start: the stub got the start's present after the file")
    at = names.index("nvn_present")
    # the start makes no renderer call: no nvn_vmap_changing reaches the stub before its present (the host's own
    # answers about the textures do, and are checked above)
    check("nvn_vmap_changing" not in names[:at],
          "start: no nvn_vmap_changing reaches the stub before the start's present: %r" % names[:at])
    check_order(post, answers, stub_calls[at:], threads=1)
    subs = [c for c in post if c.name in SUBMITS]
    check(len(subs) == 1, "start: one submit after the file, not %d" % len(subs))
    check_dirty(records, subs, [[(2, page_digest(2))]])
    reads = [c for c in post if c.name == "nvn_read_texels"]
    check(len(reads) == 1, "start: one read of the overwritten texture after the file")
    a, size, live = made["start_tex0_live"]
    check(live != made["start_tex0"][2], "start: the game overwrote the texture after its creation")
    check(last_object(records, a, size, reads[0].pos) == sha(bytes.fromhex(live)),
          "start: the overwritten texture's live bytes are kept before the read")


def check_race_start(cap: Path, out: str) -> None:
    # a second present checks the start file while the start asks the host about its texture's page. The asks are made
    # once, by the start's present, and the second present is an ordinary one, recorded after the start
    lines = out.splitlines()
    check(not [line for line in lines if line.startswith("FAIL")], "race-start: the fixture's own checks held")
    check("ok race-start" in lines and "end" in lines, "race-start: the fixture finished the scenario")
    marker, end = lines.index("start file"), lines.index("end")
    stub: list[tuple[int, str, list[int]]] = []
    for line in lines[marker:end]:
        if not line.startswith("fwd "):
            continue
        m = FWD.fullmatch(line)
        if m is None:
            fail("a stub's line parses: %r" % line)
        stub.append((int(m.group(1)), m.group(2), [int(v) for v in m.group(3).split()]))
    check(not [name for _, name, _ in stub if name == "nvn_vmap_changing"],
          "race-start: no nvn_vmap_changing reaches the stub")
    asks = [(tid, args) for tid, name, args in stub if name == "gpu_guest"]
    check([args for _, args in asks] == [[0xC000]],
          "race-start: the start asks the host for the one page of its texture's storage: %r" % asks)
    starter = {tid for tid, _ in asks}
    check(len(starter) == 1, "race-start: the asks all come from one thread")
    presents = [tid for tid, name, _ in stub if name == "nvn_present"]
    check(len(presents) == 2 and len(set(presents)) == 2, "race-start: two presents, on two threads: %r" % presents)
    check(starter <= set(presents), "race-start: the asks come from one of the presents")
    logged = [line for line in lines if line.startswith("host-log:")]
    check(logged == ["host-log: rx capture: started at present 1"],
          "race-start: the host logs one line, that the capture started at present 1: %r" % logged)
    records = read_capture(cap)
    check_blobs(cap, records)
    calls, answers = index_calls(records)
    snaps = [i for i, r in enumerate(records) if r.kind == SNAPSHOT]
    check(len(snaps) == 1, "race-start: the file holds one snapshot, not %d" % len(snaps))
    post = [c for c in calls if c.pos > snaps[0]]
    check([c.name for c in post] == ["nvn_present", "nvn_present"],
          "race-start: after the snapshot, the two presents: %r" % [c.name for c in post])
    check(post[0].rec.thread in starter, "race-start: the start's present is recorded first, on the asks' thread")
    stub_after = [line for line in lines[marker:end] if line.startswith("fwd ") and fwd_call(line)[0] in BOUNDARY]
    check_order(post, answers, stub_after, threads=2)


def check_texture_race_start(cap: Path, out: str) -> None:
    # a texture made by another thread while the start asks the host about its storage is not one the start asked
    # about: the capture still starts, and its snapshot keeps that texture, with no answers of its own
    lines = out.splitlines()
    check(not [line for line in lines if line.startswith("FAIL")], "texture-race-start: the fixture's own checks held")
    check("ok texture-race-start" in lines and "end" in lines, "texture-race-start: the fixture finished the scenario")
    addr = {p[2]: int(p[3]) for p in (line.split() for line in lines if line.startswith("start addr "))}
    logged = [line for line in lines if line.startswith("host-log:")]
    check(logged == ["host-log: rx capture: started at present 1"],
          "texture-race-start: the host logs one line, that the capture started: %r" % logged)
    records = read_capture(cap)
    calls, _ = index_calls(records)
    snaps = [i for i, r in enumerate(records) if r.kind == SNAPSHOT]
    check(len(snaps) == 1, "texture-race-start: the file holds one snapshot, not %d" % len(snaps))
    made = sorted(c.args[0] for c in calls if c.pos < snaps[0] and c.name == "nvn_texture_created")
    check(made == sorted([addr["tex0"], addr["tex1"]]),
          "texture-race-start: the snapshot keeps both textures, the one made during the start too: %r" % made)


def check_wide_start(cap: Path, out: str) -> None:
    lines = out.splitlines()
    check(not [line for line in lines if line.startswith("FAIL")], "wide-start: the fixture's own checks held")
    check("ok wide-start" in lines, "wide-start: the fixture finished the scenario")
    marker, end = lines.index("start file"), lines.index("end")
    stub_calls = [line for line in lines[marker:end] if line.startswith("fwd ") and line.split()[2] != "log"]
    names = [fwd_call(line)[0] for line in stub_calls]
    at = names.index("nvn_present")
    check("nvn_vmap_changing" not in names[:at],
          "wide-start: no nvn_vmap_changing reaches the stub before the start's present: %r" % names[:at])
    records = read_capture(cap)
    check_blobs(cap, records)
    snaps = [i for i, r in enumerate(records) if r.kind == SNAPSHOT]
    check(len(snaps) == 1, "wide-start: the file holds one snapshot, not %d" % len(snaps))
    snap = snaps[0]
    pools = [pool_of(r)[:2] for r in records[:snap] if r.kind == POOL]
    check(len(pools) == len(records[:snap]) and pools == [(8 * i, 8) for i in range(WIDE_POOLS)],
          "wide-start: the snapshot holds a POOL for every pool the host lists, in order (%d records)" % len(pools))
    calls, _ = index_calls(records)
    subs = [c for c in calls if c.name == "nvn_submit"]
    check(len(subs) == 1, "wide-start: one submit after the file, not %d" % len(subs))
    check_dirty(records, subs, [[(8, page_digest(8))]])
    logged = [line for line in lines if line.startswith("host-log:")]
    check(logged == ["host-log: rx capture: started at present 1"],
          "wide-start: the host logs one line, that the capture started at present 1: %r" % logged)


STOP_PRESENTS = 5   # the presents of the stop scenario (stop_mode in the fixture)
PRESENT_ANSWER = (16 + BOUNDARY.index("nvn_present")) | ANSWER


def stop_calls(addr: dict[str, int]) -> list[tuple[str, list[int], int]]:
    # the stop scenario as the stub gets it: the pool, then a present and its fence, STOP_PRESENTS times. The present n
    # (counted from 1) is at index 2n - 1
    want = [("nvn_pool_created", [0xC000, 0x1000], 0)]
    for _ in range(STOP_PRESENTS):
        want += [("nvn_present", [addr["tex0"], addr["crop"]], 0), ("nvn_fence", [], 100)]
    return want


def check_stop(cap: Path, out: str, start: int | None, ends: int | None) -> None:
    # start: the present an armed capture started at (None: unarmed); ends: the present that ends the capture (None: no
    # limit). The stub gets every call; the capture holds the calls from its start to the end present's ANSWER
    lines = out.splitlines()
    check(not [line for line in lines if line.startswith("FAIL")], "stop: the fixture's own checks held")
    check("ok stop" in lines and "end" in lines, "stop: the fixture finished the scenario")
    addr = {p[2]: int(p[3]) for p in (line.split() for line in lines if line.startswith("start addr "))}
    want = stop_calls(addr)
    forwarded = [(m.group(2), [int(v) for v in m.group(3).split()], int(m.group(4)))
                 for m in (FWD.fullmatch(line) for line in lines) if m and m.group(2) in BOUNDARY]
    check(forwarded == want, "stop: the stub got every call, unchanged: %r" % forwarded)
    want_logged: list[str] = []
    if start is not None:
        want_logged.append("host-log: rx capture: started at present %d" % start)
    if ends is not None:
        want_logged.append("host-log: rx capture: ended at present %d" % ends)
    logged = [line for line in lines if line.startswith("host-log:")]
    check(logged == want_logged, "stop: the host logs %r, not %r" % (want_logged, logged))
    records = read_capture(cap)
    check_blobs(cap, records)
    calls, answers = index_calls(records)
    snaps = [i for i, r in enumerate(records) if r.kind == SNAPSHOT]
    if start is None:
        check(not snaps, "stop: an unarmed capture holds no snapshot")
        kept, first = calls, 0
    else:
        check(len(snaps) == 1, "stop: the armed capture holds one snapshot, not %d" % len(snaps))
        snap = snaps[0]
        check([(c.name, c.args) for c in calls if c.pos < snap] == [("nvn_pool_created", [0xC000, 0x1000])],
              "stop: the snapshot holds the pool made before the file")
        kept, first = [c for c in calls if c.pos > snap], 2 * start - 1
    end = len(want) if ends is None else 2 * ends
    got = [(c.name, c.args, answers[c.rec.seq][2]) for c in kept]
    check(got == want[first:end], "stop: the capture holds the calls from its start to its end, in order: %r" % got)
    if ends is not None:
        check(records[-1].kind == PRESENT_ANSWER, "stop: the capture ends with the end present's ANSWER")


def check_refused(cap: Path, out: str) -> None:
    lines = out.splitlines()
    check("ok off" in lines, "refused presents: the calls go through")
    logged = [line for line in lines if line.startswith("host-log:")]
    check(len(logged) == 1 and "RX_CAPTURE_PRESENTS" in logged[0],
          "refused presents: one line through host->log, naming the variable: %r" % logged)
    check(not cap.exists(), "refused presents: no capture folder is made")


def check_race(cap: Path, out: str) -> None:
    lines = out.splitlines()
    check("ok race" in lines, "race: the fixture's checks held")
    records = read_capture(cap)
    check_blobs(cap, records)
    calls, _ = index_calls(records)
    check(sum(c.name == "nvn_submit" for c in calls) == RACE_ROUNDS, "race: every submit was recorded")
    seen = {d for r in records if r.kind == PAGES for p, d in pages_of(r) if p == 2}
    check(len(seen) > 1, "race: page 2 changed under the submits (%d contents recorded)" % len(seen))


def check_quiet(mode: str, out: str, sub: Path) -> None:
    lines = out.splitlines()
    check("ok " + mode in lines, "%s: the fixture's checks held" % mode)
    check(not [line for line in lines if line.startswith("host-log:")], "%s: nothing is logged" % mode)
    check(os.listdir(sub) == [], "%s: nothing is written" % mode)


def check_badpath(out: str, sub: Path) -> None:
    lines = out.splitlines()
    check("ok badpath" in lines, "badpath: the fixture's checks held")
    logged = [line for line in lines if line.startswith("host-log:")]
    check(len(logged) == 1 and logged[0].startswith("host-log: rx capture"), "badpath: one line through host->log")
    check(os.listdir(sub) == [], "badpath: the folder that cannot be made is not made")


def check_fail(out: str, cap: Path) -> None:
    lines = out.splitlines()
    check("ok fail" in lines, "fail: the fixture's checks held")
    logged = [line for line in lines if line.startswith("host-log:")]
    check(len(logged) == 1 and logged[0].startswith("host-log: rx capture"), "fail: one line through host->log")
    fwd_lines = [line for line in lines if line.startswith("fwd ")]
    check(any(line.split()[2] == "nvn_submit" for line in fwd_lines), "fail: the submit went on to the renderer")
    check(any(line.split()[2] == "gpu_guest" and line.split()[3] == "4660" for line in fwd_lines),
          "fail: a call after the failure was forwarded")
    records = read_capture(cap)
    calls, _ = index_calls(records)
    check(not [c for c in calls if c.name == "nvn_submit" or 4660 in c.args], "fail: recording stopped before the submit")
    check(records[-1].kind in (OBJECT, POOL, PAGES, GUEST),
          "fail: the capture ends with a record written before the failure")


def build_fixture(cc: str, tmp: Path) -> Path:
    src, exe = tmp / "fixture.c", tmp / "fixture"
    def c_bytes(data: bytes) -> str:
        return "{" + ", ".join(str(v) for v in data) + "}"

    fixture = (FIXTURE.replace("@PAT@", "{" + ", ".join(str(v) for v in PAT) + "}")
               .replace("@CONTROL_OK@", c_bytes(CONTROL_OK)).replace("@CONTROL_BAD@", c_bytes(CONTROL_BAD))
               .replace("@PAGED@", PAGED_DEFS).replace("@PLAY@", PLAY_SHARED))
    _ = src.write_text(fixture, encoding="utf-8")
    # (-Wno-missing-field-initializers: host/rx_backend.c, which the fixture includes, has partial initializers on purpose,
    # as early_call c = {0, id} does; the host build compiles it with -Wno-everything)
    cmd = [cc, "-std=gnu11", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Wno-missing-field-initializers", "-D_GNU_SOURCE",
           "-pthread", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
           "-I", str(POSIX_DIR), "-I", str(HOST_DIR), str(src), str(RECORDER), "-o", str(exe)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode:
        print(r.stdout + r.stderr)
        fail("the fixture does not build against host/rx_capture.c")
    return exe


def run_fixture(exe: Path, mode: str, sub: Path, capture: str | Path | None = None,
                start: str | Path | None = None, presents: str | None = None) -> str:
    env = {k: v for k, v in os.environ.items()
           if k not in ("RX_CAPTURE", "RX_CAPTURE_START", "RX_CAPTURE_PRESENTS", "RX_BACKEND")}
    env["RXC_MODE"] = mode
    if capture is not None:
        env["RX_CAPTURE"] = str(capture)
    if start is not None:
        env["RX_CAPTURE_START"] = str(start)
    if presents is not None:
        env["RX_CAPTURE_PRESENTS"] = presents
    r = subprocess.run([str(exe)], cwd=str(sub), env=env, capture_output=True, text=True, timeout=120)
    if r.returncode:
        print(r.stdout[-4000:] + r.stderr[-4000:])
        fail("the fixture (%s) exited with %d" % (mode, r.returncode))
    return r.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--cc", default="clang")
    _ = parser.add_argument("--cxx", default="clang++")
    args = parser.parse_args()
    cc_name = str(args.cc)
    cxx_name = str(args.cxx)
    cc = shutil.which(cc_name)
    if cc is None:
        fail("a C compiler (%s) is on the path" % cc_name)
    with tempfile.TemporaryDirectory(prefix="rx-capture-") as tmp:
        tmp = Path(tmp)
        exe = build_fixture(cc, tmp)
        sub = tmp / "off"
        sub.mkdir()
        check_quiet("off", run_fixture(exe, "off", sub), sub)
        sub = tmp / "empty"
        sub.mkdir()
        check_quiet("empty", run_fixture(exe, "empty", sub, capture=""), sub)
        sub = tmp / "hook-off"
        sub.mkdir()
        check_quiet("hook-off", run_fixture(exe, "hook-off", sub), sub)
        sub = tmp / "race"
        sub.mkdir()
        check_race(sub / "cap", run_fixture(exe, "race", sub, capture=sub / "cap"))
        sub = tmp / "hook-on"
        sub.mkdir()
        check_hook_on(sub / "cap", run_fixture(exe, "hook-on", sub, capture=sub / "cap"))
        sub = tmp / "badpath"
        sub.mkdir()
        check_badpath(run_fixture(exe, "badpath", sub, capture=sub / "missing" / "cap"), sub)
        sub = tmp / "fail"
        sub.mkdir()
        check_fail(run_fixture(exe, "fail", sub, capture=sub / "cap"), sub / "cap")
        sub = tmp / "on"
        sub.mkdir()
        check_on(sub / "cap", run_fixture(exe, "on", sub, capture=sub / "cap"))
        sub = tmp / "on-empty-start"
        sub.mkdir()
        check_on(sub / "cap", run_fixture(exe, "on", sub, capture=sub / "cap", start=""))
        sub = tmp / "off-start"
        sub.mkdir()
        check_quiet("off", run_fixture(exe, "off", sub, start=sub / "start"), sub)
        sub = tmp / "start"
        sub.mkdir()
        check_start(sub / "cap", run_fixture(exe, "start", sub, capture=sub / "cap", start=sub / "start"))
        sub = tmp / "race-start"
        sub.mkdir()
        check_race_start(sub / "cap", run_fixture(exe, "race-start", sub, capture=sub / "cap", start=sub / "start"))
        sub = tmp / "texture-race-start"
        sub.mkdir()
        check_texture_race_start(sub / "cap", run_fixture(exe, "texture-race-start", sub, capture=sub / "cap",
                                                          start=sub / "start"))
        sub = tmp / "wide-start"
        sub.mkdir()
        check_wide_start(sub / "cap", run_fixture(exe, "wide-start", sub, capture=sub / "cap", start=sub / "start"))
        sub = tmp / "scene"
        sub.mkdir()
        check_scene_run(run_fixture(exe, "scene", sub, capture=sub / "cap"))
        sub = tmp / "scene-start"
        sub.mkdir()
        check_scene_run(run_fixture(exe, "scene", sub, capture=sub / "cap", start=sub / "start"))
        # (folder, armed, RX_CAPTURE_PRESENTS, the present an armed capture starts at, the present that ends it)
        stops = [("stop-unlimited", False, "", None, None), ("stop-unarmed", False, "2", None, 2),
                 ("stop-armed-first", True, "1", 3, 3), ("stop-armed-second", True, "2", 3, 4)]
        for folder, armed, presents, start_at, ends_at in stops:
            sub = tmp / folder
            sub.mkdir()
            start = sub / "start" if armed else None
            out = run_fixture(exe, "stop", sub, capture=sub / "cap", start=start, presents=presents)
            check_stop(sub / "cap", out, start_at, ends_at)
        for i, value in enumerate(["0", "-1", "1x", "+1", "99999999999999999999"]):
            sub = tmp / ("presents-refused-%d" % i)
            sub.mkdir()
            check_refused(sub / "cap", run_fixture(exe, "off", sub, capture=sub / "cap", presents=value))
        sub = tmp / "play"
        sub.mkdir()
        rec_out = run_fixture(exe, "play", sub, capture=sub / "cap")
        check_play_capture(sub / "cap", rec_out)
        player, stub, skew = build_player(cxx_name, cc_name, tmp)
        check_player(sub / "cap", rec_out, player, stub, "1")
        check_player(sub / "cap", rec_out, player, stub, "0")
        check_player_unread(sub / "cap", rec_out, player, stub, tmp)
        check_player_reread(sub / "cap", rec_out, player, stub, tmp)
        check_player_stops(sub / "cap", player, stub, tmp)
        check_player_refuses(sub / "cap", player, stub, tmp)
        check_player_version(sub / "cap", player, skew)
        check_scene(tmp / "scene" / "cap", tmp / "scene-start" / "cap", player, stub)
        check_snapshot_refuses(tmp / "scene-start" / "cap", player, stub, tmp)
    print("PASS: RX_CAPTURE unset or empty changes nothing; with it set, every call, answer, object, pool and dirty "
          + "page is kept in order per thread, and a failed write stops recording and logs once; the player compiles, "
          + "replays a recorded capture into a stub renderer in order with the recorded values and answers, its guest "
          + "memory is the game's window (a memfd the renderer finds with HOS_GUEST_MEMFD=1, anonymous memory with 0), "
          + "reads only the blobs its play uses, stops at a damaged one it reads, and refuses a damaged capture with "
          + "one line; a capture armed from a snapshot plays the same lines as the one recorded from the start, from "
          + "its present on, with each nvn_wait's fence translated, and a damaged snapshot is refused")


if __name__ == "__main__":
    main()
