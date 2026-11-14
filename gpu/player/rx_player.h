/* The player of a capture (the format is in host/rx_capture.h): rx_play replays a capture's boundary calls into the renderer
   table mkwvk_init returns, with no game. The executable rx_player (rx_player.cpp) plays a capture through a renderer
   library; the rest of the player is described at the top of rx_player.cpp. */
#pragma once

#include "../vk/vk_api.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Replays <capture_dir>/calls.rxc into the renderer init gives the player's host, started with window (NULL: headless).
   Returns 0 after the replay and the renderer's nvn_finish. Any failure before the renderer starts, and any failure in
   the replay, writes one line to stderr naming the cause and returns nonzero. */
int rx_play(const char *capture_dir, mkwvk_init_fn init, void *window);

#ifdef __cplusplus
}
#endif
