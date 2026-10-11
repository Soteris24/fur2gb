/*
 * furplayer - replays Furnace Game Boy songs compiled by tools/fur2gb.py.
 *
 * A song is the exact register-write stream Furnace's own GB backend produces, tick by tick.
 * The player only adds the backend's anti-click wave rotation, which it computes like Furnace.
 *
 * Usage:
 *     fur_play(&my_song, BANK(my_song));
 *     then once per Furnace tick:   fur_tick();
 *     or once per VBlank:           fur_frame();   (runs 0-2 ticks to keep the song's tick rate)
 *
 * fur_tick() and fur_frame() live in bank 0, restore the caller's ROM bank and may be called
 * from an interrupt handler.
 */
#ifndef FURPLAYER_H
#define FURPLAYER_H

#include <gbdk/platform.h>
#include <stdint.h>

#define FUR_MAX_CHUNKS 32   /* 16 KB each */
#define FUR_STREAMS 5       /* A (row commands), B0-B3 (per-channel tick): NSTREAMS in fur2gb.py */
#define FUR_STACK_DEPTH 4   /* must match MAX_DEPTH in tools/fur2gb.py */
#define FUR_CTX_SIZE (5 + FUR_STACK_DEPTH * 5)

typedef struct {
    uint8_t bank;
    const uint8_t *ptr;
} fur_chunk_t;

typedef struct {
    uint8_t chunk;
    uint16_t offset;
} fur_entry_t;

typedef struct {
    uint8_t chunk_count;
    uint8_t wave_chunk;      /* chunk holding the base waves, 0xFF if none */
    uint8_t anticlick;       /* Furnace's anti-click wave rotation enabled */
    uint16_t ac_step;        /* anti-click period step per tick: trunc(2097152 / tick Hz) */
    uint32_t period;         /* tick period in 1/16 CPU cycles (4194304 Hz clock) */
    fur_entry_t entries[FUR_STREAMS];
    const fur_chunk_t *chunks;
} fur_song_t;

/* Start a song (bank = BANK() of the song header). Powers the APU off and on so it starts
   from the same state as Furnace's emulated chip. */
void fur_play(const fur_song_t *song, uint8_t bank) NONBANKED;
/* Stop playback and silence the APU. */
void fur_stop(void) NONBANKED;
/* Play one Furnace tick. */
void fur_tick(void) NONBANKED;
/* Call once per frame (VBlank, 70224 cycles): plays as many ticks as the song's tick rate needs. */
void fur_frame(void) NONBANKED;

extern uint8_t fur_playing;  /* 0 once the song stopped (songs without a loop) */

#endif
