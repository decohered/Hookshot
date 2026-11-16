/* RX_CAPTURE=<dir>: the recorder of the Vulkan back end's boundary (host/rx_capture.c).

   With the variable set to a folder, every call the game makes into the renderer (the rx_nvn_* entries of
   gpu/render/rx_nvn.h, which reach the renderer's MkwVkRenderer entries) and every question the renderer asks the host
   (the functions of MkwVkHost, gpu/vk/vk_api.h) is written to that folder with its arguments and its answer. Written with
   them: the bytes their pointers reach, the memory pools, and the dirty pages before each submit. Every call is forwarded
   unchanged. The recorder observes; it never alters a call or an answer, and its lock is held only while it writes,
   never across a forwarded call. Written with them too: the guest bytes the renderer reads by address (a program's
   control sections and code, a deferred bind's arrays), as GUEST records.

   Use, in the executable (host/rx_backend.c):
     host = rx_capture_host(host, marks, npages);   before the renderer is made; the renderer is given the result
     renderer = rx_capture_renderer(renderer);      the table the executable calls
     rx_capture_end();                              writes out and closes the capture (also run at process exit)
   With RX_CAPTURE unset or empty, rx_capture_host and rx_capture_renderer return their argument and write nothing.
   rx_capture_host starts one capture per process: a second call while it runs returns the same table.
   If the folder or its calls.rxc cannot be made or opened, rx_capture_host logs one line through host->log and returns
   the host. If a write fails later, the recorder logs one line, stops recording, and the calls still go through.
   RX_CAPTURE_START=<path>, with RX_CAPTURE set and non-empty, arms the capture: from the end of rx_capture_renderer, the
   recorder writes nothing until the first nvn_present that finds a file at <path>. That present starts the capture with a
   snapshot of the renderer's live state (RXC_SNAPSHOT, below), and records it and every later call as usual. The calls
   before the file are forwarded and not kept.
   A path too long for the recorder: one line through host->log, and the game goes on without the capture.
   RX_CAPTURE_PRESENTS=<N>, with RX_CAPTURE set and non-empty, ends the capture after its N-th present: that present's
   CALL and ANSWER are the last records, calls.rxc is closed as rx_capture_end closes it, and the host logs one line,
   "rx capture: ended at present M". The presents are counted from the renderer's start, as the start line counts them
   (M is that count): unarmed, the capture ends at present N; armed, the start's present is the first, so it ends at the
   start's present plus N - 1. The later calls are forwarded unchanged, and nothing is written. Unset or empty: no limit.
   Any other value (digits only, a number of at least 1 within 64 bits) is refused: one line through host->log naming
   RX_CAPTURE_PRESENTS, no folder is made, and rx_capture_host returns the host.

   THE FORMAT, version 1. All integers are little-endian and nothing is padded. A reader needs nothing else.

   <dir>/calls.rxc
     File header, 16 bytes: magic "RXCAPT01" (8 bytes); u32 format version (1); u32 MKWVK_API_VERSION.
     Then the records, one after another. Record header, 32 bytes: u32 kind; u32 total (the record's size, this header
     included: 32 plus the payload, which is a multiple of 8 bytes); u64 seq (1, 2, 3, ..., one per record, in file order);
     u64 thread (GetCurrentThreadId() of the thread that made the record); u64 queue (the queue of a submit_q or a
     fence_q, on its call and its answer; 0 otherwise). The payload follows, by kind:

   <dir>/blobs/<64 lowercase hex digits>
     The bytes of one distinct content, named by the SHA-256 of those bytes, written whole (under a temporary name, then
     renamed). A page is RXC_PAGE (4096) bytes: a guest page's content is the 4096 bytes at its guest address.

   Record kinds:
     RXC_OBJECT (1). Payload: u64 address; u64 size; u8[32] sha256. The bytes [address, address + size) are the blob named
       by sha256. An OBJECT is written before the call that reads those bytes when they differ from the last OBJECT of
       that address (size or sha256), and after the call for the bytes a call or its answer writes (see the calls).
     RXC_POOL (2). Payload: u64 start; u64 size; then one u8[32] sha256 per page: the pages from start >> 12 through
       (start + size - 1) >> 12, in order. Written for nvn_pool_created (start is the gpu address, which is the guest
       address there), and for each pool the host lists when rx_capture_renderer starts the capture.
     RXC_PAGES (3). Payload: u64 count; then count times: u64 page (the guest page number, address >> 12); u8[32] sha256.
       The pages are ascending and unique. They are every page inside a pool the host lists whose soft-write mark is set
       at that moment, and every page the renderer's collect_written or collect_written_in answered since the previous
       PAGES. Written before the OBJECTs and the CALL of each nvn_submit and nvn_submit_q; a program's creation writes
       none (the code it reads is a GUEST).
     RXC_GUEST (4). Payload: u64 address; u64 size; u8[32] sha256. The bytes [address, address + size) of guest memory are
       the blob named by sha256. These are the guest bytes the renderer reads by address that the pools may not hold: the
       control section of each stage of a program (RXC_CONTROL_SIZE = 0xd8 bytes at each nonzero control, for the stages
       below nstage and 6), a deferred bind's array at src (NOP_BIND_BUFS: n * 16 bytes; NOP_BIND_TEX, NOP_BIND_SAMP
       and NOP_BIND_IMG: n * 8 bytes; n clamped to the array's 16 or 32 entries), and the code a program's creation
       reads: for each stage below nstage and 6 whose control section the renderer takes (its control_end,
       gpu/vk/vk_shader.cpp: the u32 magic 0x98761234 at +0; the u32 code length at +0x38, nonzero; the u32 c1 offset at
       +0x40; the u32 end at +0x44, at most 16 MB; the u16 stage at +0x56, at most 7; the code length and the c1 offset
       at most the end), the end bytes at the stage's data, when they lie in guest memory. Written before the CALL that
       reads them, after the call's OBJECTs, when they differ from the last GUEST of that address (size or sha256). Not a
       page: the player writes it into guest memory at its address, and it is not listed in a collection's pages.
     RXC_SNAPSHOT (5). Payload: u64 first, the seq of the snapshot's first record. Written once, after the snapshot's last
       record, by the start of an armed capture (RX_CAPTURE_START, above). The snapshot is the renderer's live state at
       that present, as the CALL and ANSWER records that rebuild it (they are not forwarded: the renderer has that state
       already). The start makes no call into the renderer: the POOL and GUEST records hold the guest memory as it is at
       the start, and the renderer's state is left as it is. The host's answers the renderer needs about the live
       textures are asked at the same time, with no lock held, and kept as the recorder's own questions (they are not
       forwarded): the textures are listed under the lock and asked with no lock held, and a texture made between its
       listing and the snapshot is kept with no answers of its own. In order: a POOL for each pool the host lists; the
       calls nvn_pool_created for each pool made, in the order made; nvn_set_texture_pool_size, when a size was given;
       then the answers: a CALL and ANSWER of gpu_guest(page) for each page of the storages of the live and registered
       textures that lie outside every listed pool (a storage of no bytes is none), the page's address as the argument,
       each page once, ascending; then for each live texture and each level below its d.levels
       (at least level 0), a CALL and ANSWER of tex_level_offset(d, level) and of tex_level_tiles(d, level, tile_h,
       tile_d), each with d as an OBJECT before the CALL (the descriptor the texture was created with, at the texture's
       address plus the offset of d) and the tiles' outputs as OBJECTs before the ANSWER; then nvn_texture_created for
       each live texture, ascending by address; nvn_register_texture for each texture registration, ascending by id;
       nvn_register_sampler for each sampler registration, ascending by id; nvn_program_created for each live program,
       ascending by address. Each of those calls has its OBJECT and GUEST records before its CALL, as any call does: the
       texture, view, sampler and program objects are the bytes their calls saw, and the pools and guest bytes are read
       at the start. The records have the present's thread and queue 0. The present that started the capture, and every
       later call, follow as usual.
     A CALL: its kind, 16 + i for the renderer's boundary entry i or 64 + j for the MkwVkHost field j, in the orders of the
       lists below. Payload: one u64 per argument, in the function's argument order: a pointer its address; an integer its
       value (a signed integer sign-extended); a float its IEEE bits in the low 32 bits.
     An ANSWER: the call's kind | RXC_ANSWER (0x10000). Payload: u64 call (the seq of the CALL record); u64 result, the
       return value encoded as the arguments are (0 for a void function).

   Order, for the records of one call made by one thread: [PAGES] [POOL] [OBJECT...] [GUEST...] CALL [the calls the
   forwarded call makes] [OBJECT... for the outputs] ANSWER. A call the renderer makes into the host while it handles a
   submit sits between that submit's CALL and ANSWER. Records of other threads may sit anywhere in between; match an ANSWER to its CALL by seq.
   The recorder's own queries of the host (guest_base, guest_size, gpu_pools, fmt_bpb) are not calls and are not recorded.

   Pointer data, the OBJECTs a call writes:
     nvn_submit, nvn_submit_q (handles, n): the handles array (n * 8 bytes). For each handle, its recording: the bytes from
       its NOP_BEGIN record through its NOP_END record, walked by each record's nvn_op_hdr.size. Then the pointers of those
       records: NOP_SET_RT: color[0..n-1] and depth (nvn_tex); NOP_BIND_PROGRAM: prog (nvn_prog); NOP_SET_TEX_POOL and
       NOP_SET_SAMP_POOL: pool (nvn_dpool); NOP_COPY_B2T, NOP_COPY_T2B and NOP_CLEAR_TEX: tex (nvn_tex); NOP_COPY_T2T: src
       and dst (nvn_tex). An nvn_tex's pool (nvn_pool) and an nvn_dpool's pool (nvn_pool) are objects too. The guest bytes
       of the same records are GUEST records (see RXC_GUEST): the control sections of a NOP_BIND_PROGRAM's program, and the
       deferred arrays of NOP_BIND_BUFS, NOP_BIND_TEX, NOP_BIND_SAMP and NOP_BIND_IMG.
     The texel bytes of a region, for nvn_read_texels (output) and nvn_write_texels (input): with the format's bytes per
       block bpb and block width bw and height bh (the host's fmt_bpb), wb = ceil(width / bw) * bpb and hb = ceil(height / bh),
       the bytes are (depth - 1) * image_pitch + (hb - 1) * row_pitch + wb, depth taken as 1 when the region has none.
       0 when fmt_bpb fails or the region is empty.

   The calls, in kind order (args; the pointer data written; the result):
     16 nvn_submit(handles, n). Objects as above. Void.
     17 nvn_submit_q(handles, n, queue). As nvn_submit; queue is also the record header's queue. Void.
     18 nvn_fence(). Result: the fence.
     19 nvn_fence_q(queue). Result: the fence.
     20 nvn_pushed(). Result: the count.
     21 nvn_replayed(). Result: the count.
     22 nvn_wait(fence, timeout_ns). Result: 1 or 0 (int).
     23 wait_writebacks_reading(p, n). Object: [p, p + n) before the call. Void.
     24 nvn_finish(). Void.
     25 nvn_present(t, crop). Objects: the nvn_tex t (and its pool); crop as int[4] (16 bytes, when not NULL). Void.
     26 nvn_register_texture(id, t, v). Objects: the nvn_tex t (and its pool); v as an nvn_view (16 bytes, when not NULL). Void.
     27 nvn_register_sampler(id, p). Object: the nvn_samp_par p. Void.
     28 nvn_set_texture_pool_size(n). Void.
     29 nvn_texture_released(t). Objects: t (and its pool). Void.
     30 nvn_texture_created(t). Objects: t (and its pool). Void.
     31 nvn_program_released(p). Object: the nvn_prog p. Void.
     32 nvn_program_created(p). Object: the nvn_prog p. Guests: its stages' control sections and code (see RXC_GUEST).
       Void.
     33 nvn_read_texels(t, level, layer0, r, dst, row_pitch, image_pitch). Objects before: t (and its pool), r (the
       NVNcopyRegion). Output after: the texel bytes at dst. Void.
     34 nvn_write_texels(t, level, layer0, r, src, row_pitch, image_pitch). Objects before: t (and its pool), r, and the
       texel bytes at src. Void.
     35 nvn_invalidate(gpu, size). Void.
     36 nvn_pool_created(gpu, size). A POOL [gpu, gpu + size) before the call. Void.
     37 nvn_vmap_changing(gpu, size). Void.
     38 nvn_event_signal(gpu, value). Void.
     64 log(line). Object: the NUL-terminated line before the call. Void.
     65 guest_base(). Result: the address.
     66 guest_size(). Result: the size.
     67 gpu_ptr(gpu, avail). Output after: the u64 at avail, when not NULL. Result: the pointer.
     68 gpu_guest(gpu). Result: the guest address.
     69 gpu_read(gpu, dst, n). Output after: the n bytes at dst. Result: 1 or 0.
     70 gpu_write(gpu, src, n). Object before: the n bytes at src. Result: 1 or 0.
     71 collect_written(pages). Output after: the count u32 page numbers at *pages (count * 4 bytes, when count > 0).
       Result: the count. The pages are kept for the next PAGES.
     72 free_pages(pages). Void.
     73 mark_written(p, n). Object before: [p, p + n). Void.
     74 ticks(). Result: the tick.
     75 now_ns(). Result: the time (signed, sign-extended).
     76 tex_layout(d, o). Object before: the nvn_texdesc d. Output after: the nvn_texlayout o. Void.
     77 tex_level_offset(d, level). Object before: d. Result: the offset.
     78 fmt_bpb(format, bw, bh). Outputs after: the int at bw and at bh, when not NULL. Result: bytes per block (int).
     79 tex_level_tiles(d, level, tile_h, tile_d). Object before: d. Outputs after: the unsigned at tile_h and at tile_d,
       when not NULL. Void.
     80 tex_linear_stride(d). Object before: d. Result: the stride.
     81 depth_minus_w(). Result: 0 or 1.
     82 gpu_pools(out, max). Output after: count * 16 bytes at out (the {start, size} entries). Result: the count.
     83 maps_generation(). Result: the number.
     84 vmap_generation(). Result: the number.
     85 reserved_cpus(). Result: the count.
     86 collect_written_in(ranges, n, pages). Object before: the n {lo, hi} ranges (n * 16 bytes). Output after: the count
       page numbers at *pages, as collect_written. Result: the count.
     87 guest_alloc(size). Result: the address (0 when none is left).
     88 watch_guest_writes(guest, size). Void.
     89 pad_raw(). Result: the bits.
     90 binding(set, button, slot). Result: the key or pad code (int).
     91 bind(set, button, slot, code). Void.
     92 aspect_ratio(). Result: the float's IEEE bits.
     93 take_key_press(key). Result: 1 or 0. (Not wrapped when the host's entry is NULL.)
     94 pointer(out). Output after: the MkwVkPointer at out. Result: 1 or 0.
     95 mic_count(). Result: the count.
     96 mic_name(index). Output after: the NUL-terminated name at the result. Result: the address (0 for none).
     97 mic_select(mode, name, index). Object before: the NUL-terminated name at name, when not NULL. Void.
     98 gyro_status(). Result: the status (int).
     99 log_flush(). Void.
     100 prompt_face(). Result: the face (int).
     101 romfs_dir(). Output after: the NUL-terminated folder at the result. Result: the address (0 for none).
     102 wide_menus(keep_mask). Result: the float's IEEE bits.
     103 slept_ns(out). Output after: the two int64 at out (16 bytes). Void.
     104 fps_timing(on). Void.
     105 prompts_live(). Result: the HOS_PROMPTS_LIVE_* bits (int).
     106 screen_size(width, height). Outputs after: the int at width and at height, when not NULL. Result: 1 or 0.
     107 wide_cover(). Result: the float's IEEE bits.

   Every call has an ANSWER. Void calls answer with result 0.
*/
#pragma once

#include <stdint.h>

#include "../gpu/vk/vk_api.h"

#ifdef __cplusplus
extern "C" {
#endif

#define RXC_FORMAT 1
#define RXC_PAGE 4096u
#define RXC_CONTROL_SIZE 0xd8u   /* a program's control section: the renderer's kControlSize (gpu/vk/vk_shader.cpp) */
#define RXC_ANSWER 0x10000u

enum {
    RXC_OBJECT = 1,
    RXC_POOL = 2,
    RXC_PAGES = 3,
    RXC_GUEST = 4,
    RXC_SNAPSHOT = 5,
    RXC_NVN_SUBMIT = 16,
    RXC_NVN_SUBMIT_Q,
    RXC_NVN_FENCE,
    RXC_NVN_FENCE_Q,
    RXC_NVN_PUSHED,
    RXC_NVN_REPLAYED,
    RXC_NVN_WAIT,
    RXC_WAIT_WRITEBACKS_READING,
    RXC_NVN_FINISH,
    RXC_NVN_PRESENT,
    RXC_NVN_REGISTER_TEXTURE,
    RXC_NVN_REGISTER_SAMPLER,
    RXC_NVN_SET_TEXTURE_POOL_SIZE,
    RXC_NVN_TEXTURE_RELEASED,
    RXC_NVN_TEXTURE_CREATED,
    RXC_NVN_PROGRAM_RELEASED,
    RXC_NVN_PROGRAM_CREATED,
    RXC_NVN_READ_TEXELS,
    RXC_NVN_WRITE_TEXELS,
    RXC_NVN_INVALIDATE,
    RXC_NVN_POOL_CREATED,
    RXC_NVN_VMAP_CHANGING,
    RXC_NVN_EVENT_SIGNAL,
    RXC_HOST_LOG = 64,
    RXC_HOST_GUEST_BASE,
    RXC_HOST_GUEST_SIZE,
    RXC_HOST_GPU_PTR,
    RXC_HOST_GPU_GUEST,
    RXC_HOST_GPU_READ,
    RXC_HOST_GPU_WRITE,
    RXC_HOST_COLLECT_WRITTEN,
    RXC_HOST_FREE_PAGES,
    RXC_HOST_MARK_WRITTEN,
    RXC_HOST_TICKS,
    RXC_HOST_NOW_NS,
    RXC_HOST_TEX_LAYOUT,
    RXC_HOST_TEX_LEVEL_OFFSET,
    RXC_HOST_FMT_BPB,
    RXC_HOST_TEX_LEVEL_TILES,
    RXC_HOST_TEX_LINEAR_STRIDE,
    RXC_HOST_DEPTH_MINUS_W,
    RXC_HOST_GPU_POOLS,
    RXC_HOST_MAPS_GENERATION,
    RXC_HOST_VMAP_GENERATION,
    RXC_HOST_RESERVED_CPUS,
    RXC_HOST_COLLECT_WRITTEN_IN,
    RXC_HOST_GUEST_ALLOC,
    RXC_HOST_WATCH_GUEST_WRITES,
    RXC_HOST_PAD_RAW,
    RXC_HOST_BINDING,
    RXC_HOST_BIND,
    RXC_HOST_ASPECT_RATIO,
    RXC_HOST_TAKE_KEY_PRESS,
    RXC_HOST_POINTER,
    RXC_HOST_MIC_COUNT,
    RXC_HOST_MIC_NAME,
    RXC_HOST_MIC_SELECT,
    RXC_HOST_GYRO_STATUS,
    RXC_HOST_LOG_FLUSH,
    RXC_HOST_PROMPT_FACE,
    RXC_HOST_ROMFS_DIR,
    RXC_HOST_WIDE_MENUS,
    RXC_HOST_SLEPT_NS,
    RXC_HOST_FPS_TIMING,
    RXC_HOST_PROMPTS_LIVE,
    RXC_HOST_SCREEN_SIZE,
    RXC_HOST_WIDE_COVER,
};

/* the recorder: the table the executable gives the renderer (see the top of this file) */
const MkwVkHost *rx_capture_host(const MkwVkHost *host, const uint8_t *marks, uint64_t npages);
const MkwVkRenderer *rx_capture_renderer(const MkwVkRenderer *r);
void rx_capture_end(void);

#ifdef __cplusplus
}
#endif
