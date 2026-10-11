/* furplayer: song setup and frame pacing. The per-tick interpreter is in furplayer_tick.s. */
#include <gb/gb.h>
#include <string.h>
#include "furplayer.h"

/* State shared with furplayer_tick.s (keep names and sizes in sync) */
uint8_t fur_playing;
/* per stream: idle ticks, bank, ptr lo, ptr hi, call depth, then frames of
   {bank, ptr lo, ptr hi, units lo, units hi} */
uint8_t fur_ctx[FUR_STREAMS * FUR_CTX_SIZE];
fur_chunk_t fur_chunks[FUR_MAX_CHUNKS];
uint8_t fur_wave_bank;
const uint8_t *fur_wave_ptr;
uint8_t fur_ac_on;
uint16_t fur_ac_step;
uint16_t fur_ac_cnt;
uint8_t fur_ac_pos;
uint16_t fur_ac_freq;          /* ch3 period as Furnace's chan[2].freq, 0 until first set */
uint8_t fur_nr33;
uint8_t fur_ac_q;              /* ac_step / freq (low byte), ac_step % freq */
uint16_t fur_ac_r;
uint32_t fur_period;           /* tick period, 1/16 cycles */
static uint32_t fur_acc;       /* fur_frame() time accumulator, 1/16 cycles */

#define FRAME_TIME (70224UL * 16)

void fur_play(const fur_song_t *song, uint8_t bank) NONBANKED {
    uint8_t save = CURRENT_BANK;
    uint8_t i;
    fur_playing = 0;
    SWITCH_ROM(bank);
    {
        const fur_song_t s = *song;
        memcpy(fur_chunks, s.chunks, s.chunk_count * sizeof(fur_chunk_t));
        for (i = 0; i < FUR_STREAMS; i++) {
            uint8_t *c = fur_ctx + i * FUR_CTX_SIZE;
            const fur_chunk_t *ch = &fur_chunks[s.entries[i].chunk];
            const uint8_t *p = ch->ptr + s.entries[i].offset;
            c[0] = 0;
            c[1] = ch->bank;
            c[2] = (uint8_t)(uint16_t)p;
            c[3] = (uint8_t)((uint16_t)p >> 8);
            c[4] = 0;
        }
        fur_ac_on = s.anticlick;
        fur_ac_step = s.ac_step;
        fur_period = s.period;
        if (s.wave_chunk != 0xFF) {
            fur_wave_bank = fur_chunks[s.wave_chunk].bank;
            fur_wave_ptr = fur_chunks[s.wave_chunk].ptr;
        }
    }
    SWITCH_ROM(save);

    fur_ac_cnt = 0;
    fur_ac_pos = 0;
    fur_ac_freq = 0;
    fur_nr33 = 0;
    fur_ac_q = 0;
    fur_ac_r = 0;
    /* the first fur_frame() plays exactly tick 0 */
    fur_acc = (fur_period > FRAME_TIME) ? fur_period - FRAME_TIME : 0;

    /* Furnace's chip starts zeroed: power cycle (clears NR10-NR51) and clear wave RAM */
    NR52_REG = 0x00;
    for (i = 0; i < 16; i++) AUD3WAVE[i] = 0;
    fur_playing = 1;
}

void fur_stop(void) NONBANKED {
    fur_playing = 0;
    NR52_REG = 0x00;
}

void fur_frame(void) NONBANKED {
    if (!fur_playing) return;
    fur_acc += FRAME_TIME;
    while (fur_acc >= fur_period) {
        fur_acc -= fur_period;
        fur_tick();
    }
}
