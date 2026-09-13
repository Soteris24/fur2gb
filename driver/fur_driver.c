#include "fur_driver.h"
#include <gb/gb.h>
#include <string.h>

/* Note Frequency Table for Game Boy (Notes 0 to 95, corresponding to Furnace notes 60 to 155, C-0 to B-7) */
/* Formula: hw_reg = 2048 - period, matching Furnace DivPitchTable */
static const uint16_t note_freqs[96] = {
    /* Octave 0 (Notes 60..71) */
    0x0001, 0x0001, 0x0001, 0x0001, 0x0001, 0x0001, 0x0001, 0x0001, 0x0001, 0x0001, 0x0001, 0x0001,
    /* Octave 1 (Notes 72..83) */
    0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000, 0x0000,
    /* Octave 2 (Notes 84..95) */
    0x002C, 0x009C, 0x0106, 0x016B, 0x01C9, 0x0222, 0x0277, 0x02C6, 0x0311, 0x0358, 0x039B, 0x03DA,
    /* Octave 3 (Notes 96..107) */
    0x0416, 0x044E, 0x0483, 0x04B5, 0x04E5, 0x0511, 0x053B, 0x0563, 0x0589, 0x05AC, 0x05CE, 0x05ED,
    /* Octave 4 (Notes 108..119) */
    0x060B, 0x0627, 0x0642, 0x065B, 0x0672, 0x0689, 0x069E, 0x06B2, 0x06C4, 0x06D6, 0x06E7, 0x06F7,
    /* Octave 5 (Notes 120..131) */
    0x0705, 0x0714, 0x0721, 0x072D, 0x0739, 0x0744, 0x074F, 0x0759, 0x0762, 0x076B, 0x0773, 0x077B,
    /* Octave 6 (Notes 132..143) */
    0x0783, 0x078A, 0x0790, 0x0797, 0x079D, 0x07A2, 0x07A7, 0x07AC, 0x07B1, 0x07B6, 0x07BA, 0x07BE,
    /* Octave 7 (Notes 144..155) */
    0x07C1, 0x07C5, 0x07C8, 0x07CB, 0x07CE, 0x07D1, 0x07D4, 0x07D6, 0x07D9, 0x07DB, 0x07DD, 0x07DF
};

/* Noise Pitch Table from Furnace (matches noiseTable[256]) */
static const uint8_t noise_table[69] = {
    0,
    0xf7, 0xf6, 0xf5, 0xf4,
    0xe7, 0xe6, 0xe5, 0xe4,
    0xd7, 0xd6, 0xd5, 0xd4,
    0xc7, 0xc6, 0xc5, 0xc4,
    0xb7, 0xb6, 0xb5, 0xb4,
    0xa7, 0xa6, 0xa5, 0xa4,
    0x97, 0x96, 0x95, 0x94,
    0x87, 0x86, 0x85, 0x84,
    0x77, 0x76, 0x75, 0x74,
    0x67, 0x66, 0x65, 0x64,
    0x57, 0x56, 0x55, 0x54,
    0x47, 0x46, 0x45, 0x44,
    0x37, 0x36, 0x35, 0x34,
    0x27, 0x26, 0x25, 0x24,
    0x17, 0x16, 0x15, 0x14,
    0x07, 0x06, 0x05, 0x04,
    0x03, 0x02, 0x01, 0x00
};

/* 64-step Sine Wave Table for Vibrato LFO (-127 to +127) */
static const int8_t vib_sine_table[64] = {
      0,  12,  25,  37,  49,  60,  71,  81,  90,  98, 106, 112, 117, 122, 125, 126,
    127, 126, 125, 122, 117, 112, 106,  98,  90,  81,  71,  60,  49,  37,  25,  12,
      0, -12, -25, -37, -49, -60, -71, -81, -90, -98,-106,-112,-117,-122,-125,-126,
   -127,-126,-125,-122,-117,-112,-106, -98, -90, -81, -71, -60, -49, -37, -25, -12
};

/* Game Boy Wave Volume Map matching gbVolMap in Furnace gb.cpp */
static const uint8_t gb_vol_map[16] = {
    0x00, 0x00, 0x00, 0x00,
    0x60, 0x60, 0x60, 0x60,
    0x40, 0x40, 0x40, 0x40,
    0x20, 0x20, 0x20, 0x20
};

/* Internal Channel State */
typedef struct {
    bool active;
    uint8_t base_note;          /* Note 0..95 */
    uint8_t target_note;        /* For portamento */
    uint16_t current_freq;      /* 11-bit frequency period */
    uint16_t target_freq;       /* Target frequency for portamento */
    uint16_t current_linear_pitch; /* 1/128ths of a semitone */
    bool key_on;                /* Trigger note on next APU write */
    uint8_t volume;             /* Base volume (0..15) */
    uint8_t out_vol;            /* Evaluated volume */
    uint8_t last_kill;          /* For Zombie Mode envelope tracking */
    uint8_t duty;               /* 0..3 (for pulse), or noise mode (0/1 for noise) */
    uint8_t ins;                /* Current instrument index */
    uint8_t last_ins;           /* Last active instrument index (0xFF = none) */
    uint16_t wave_id;           /* Current wavetable ID (CH3) */
    
    /* MODAL EFFECTS (persist across rows and notes until cancelled) */
    uint8_t modal_arp;          /* 00xy: 0 = none, x/y semitones */
    uint8_t arp_stage;          /* 0, 1, 2 */
    uint8_t modal_vib_rate;     /* Vibrato speed */
    uint8_t modal_vib_depth;    /* Vibrato depth */
    uint8_t vib_pos;            /* LFO phase */
    uint8_t modal_porta_speed;  /* Portamento speed */
    
    /* ROW-LOCAL OR ONE-SHOT EFFECTS */
    uint8_t slide_speed;        /* Pitch slide rate */
    int8_t slide_dir;           /* +1 up, -1 down, 0 none */
    uint8_t cut_ticks;          /* Delayed note cut counter (ECxx) */
    int8_t vol_slide;           /* Volume slide per tick (0Axy) */
    
    /* MACRO EVALUATION */
    uint8_t macro_vol_step;
    uint8_t macro_arp_step;
    uint8_t macro_duty_step;
    uint8_t macro_wave_step;
    
    /* Pattern decoding pointer */
    const uint8_t *pat_ptr;
    uint8_t rle_count;          /* Empty rows to skip */
} fur_channel_t;

static const fur_song_t *cur_song = NULL;
static bool is_playing = false;
static uint8_t cur_order = 0;
static uint16_t cur_row = 0;
static uint8_t speed_idx = 0;
static uint8_t tick_counter = 0;
static uint16_t tempo_accum = 0;
static fur_channel_t channels[4];
static int8_t solo_channel = -1; /* -1 = all unmuted, 0..3 = CH1..CH4 solo */

/* Forward declarations */
static void fur_init_channel_pattern(uint8_t ch, uint8_t pat_idx);
static void fur_decode_channel_row(uint8_t ch);
static void fur_step_channel(uint8_t ch);
static void fur_load_wave(uint16_t wave_id);
static void fur_set_hardware_vol(uint8_t ch, uint8_t vol);

void fur_driver_set_solo(int8_t solo_ch) {
    solo_channel = solo_ch;
    if (solo_channel >= 0 && solo_channel < 4) {
        NR51_REG = (0x11 << solo_channel);
    } else {
        NR51_REG = 0xFF;
    }
}

void fur_driver_init(const fur_song_t *song) {
    uint8_t i;
    cur_song = song;
    if (!cur_song) {
        is_playing = false;
        return;
    }

    /* Turn on APU, enable sound channels, set max master volume */
    NR52_REG = 0x80;
    if (solo_channel >= 0 && solo_channel < 4) {
        NR51_REG = (0x11 << solo_channel);
    } else {
        NR51_REG = 0xFF; /* Panning: all 4 channels to left & right */
    }
    NR50_REG = 0x77; /* Master volume max */

    cur_order = 0;
    cur_row = 0;
    speed_idx = 0;
    tick_counter = 0;
    tempo_accum = 0;
    is_playing = true;

    memset(channels, 0, sizeof(channels));

    for (i = 0; i < 4; i++) {
        channels[i].ins = 0xFF;
        channels[i].last_ins = 0xFF;
        channels[i].volume = 15;
        channels[i].out_vol = 15;
        channels[i].last_kill = 15;
        channels[i].duty = (i == 3) ? 0 : 2; /* Pulse: 50%, Noise: 15-bit LFSR */
        channels[i].wave_id = 0xFFFF;
    }

    /* Preload wave 0 into Wave RAM if song has wavetables */
    if (cur_song->wave_count > 0) {
        fur_load_wave(0);
        channels[2].wave_id = 0;
    }

    /* Set up pattern pointers for Order 0 */
    fur_init_channel_pattern(0, cur_song->order_ch1[0]);
    fur_init_channel_pattern(1, cur_song->order_ch2[0]);
    fur_init_channel_pattern(2, cur_song->order_ch3[0]);
    fur_init_channel_pattern(3, cur_song->order_ch4[0]);

    /* Decode initial Row 0 immediately */
    fur_decode_channel_row(0);
    fur_decode_channel_row(1);
    fur_decode_channel_row(2);
    fur_decode_channel_row(3);

    /* Step initial tick 0 across all 4 channels */
    for (i = 0; i < 4; i++) {
        fur_step_channel(i);
    }

    tick_counter = cur_song->speeds[0] - 1;
    speed_idx = 1;
}

void fur_driver_stop(void) {
    is_playing = false;
    /* Silence all channels */
    NR12_REG = 0; NR14_REG = 0x80;
    NR22_REG = 0; NR24_REG = 0x80;
    NR30_REG = 0; NR32_REG = 0;
    NR42_REG = 0; NR44_REG = 0x80;
}

uint8_t fur_driver_get_order(void) {
    return cur_order;
}

uint16_t fur_driver_get_row(void) {
    return cur_row;
}

bool fur_driver_is_playing(void) {
    return is_playing;
}

static void fur_init_channel_pattern(uint8_t ch, uint8_t pat_idx) {
    channels[ch].rle_count = 0;
    if (pat_idx == 0xFF) {
        channels[ch].pat_ptr = NULL;
        return;
    }
    switch (ch) {
        case 0: channels[ch].pat_ptr = cur_song->patterns_ch1[pat_idx]; break;
        case 1: channels[ch].pat_ptr = cur_song->patterns_ch2[pat_idx]; break;
        case 2: channels[ch].pat_ptr = cur_song->patterns_ch3[pat_idx]; break;
        case 3: channels[ch].pat_ptr = cur_song->patterns_ch4[pat_idx]; break;
    }
}

static void fur_load_wave(uint16_t wave_id) {
    uint8_t i;
    const uint8_t *src;
    volatile uint8_t *dst = (volatile uint8_t *)0xFF30;
    if (!cur_song || wave_id >= cur_song->wave_count) return;
    src = cur_song->waves[wave_id];

    /* Game Boy Wave RAM update safety sequence: */
    NR30_REG = 0x00; /* Disable DAC */
    for (i = 0; i < 16; i++) {
        dst[i] = src[i];
    }
    NR30_REG = 0x80; /* Enable DAC */
}

/* Hardware Zombie Mode Volume Stepping matching Furnace platform/gb.cpp lines 379-390 */
static void fur_set_hardware_vol(uint8_t ch, uint8_t vol) {
    fur_channel_t *c = &channels[ch];
    volatile uint8_t *nrx2;
    int8_t kill_delta;
    uint8_t j;

    if (ch == 0) nrx2 = (volatile uint8_t *)0xFF12;
    else if (ch == 1) nrx2 = (volatile uint8_t *)0xFF17;
    else if (ch == 3) nrx2 = (volatile uint8_t *)0xFF21;
    else return;

    kill_delta = (int8_t)c->last_kill - (int8_t)vol + 1;
    if (kill_delta < 0) kill_delta += 16;
    c->last_kill = vol;

    if (kill_delta != 1) {
        *nrx2 = (vol << 4) | 8;
        for (j = 0; j < (uint8_t)kill_delta; j++) {
            *nrx2 = 0x09;
            *nrx2 = 0x11;
            *nrx2 = 0x08;
        }
    }
}

static void fur_decode_channel_row(uint8_t ch) {
    fur_channel_t *c = &channels[ch];
    uint8_t mask;
    uint8_t note = 0;
    uint8_t ins = 0xFF;
    uint8_t vol = 0xFF;
    uint8_t fx1_type = 0, fx1_val = 0;
    uint8_t fx2_type = 0, fx2_val = 0;
    const fur_instrument_t *instrument = NULL;
    bool is_porta = false;

    if (!c->pat_ptr) return;

    /* Handle RLE empty rows */
    if (c->rle_count > 0) {
        c->rle_count--;
        return;
    }

    mask = *c->pat_ptr++;
    if (mask == FUR_PATTERN_END) {
        c->pat_ptr = NULL;
        return;
    }

    /* Bit 7 indicates RLE count of empty rows */
    if (mask & 0x80) {
        c->rle_count = (mask & 0x7F) - 1;
        return;
    }

    if (mask & 0x01) note = *c->pat_ptr++;
    if (mask & 0x02) ins  = *c->pat_ptr++;
    if (mask & 0x04) vol  = *c->pat_ptr++;
    if (mask & 0x08) {
        fx1_type = *c->pat_ptr++;
        fx1_val  = *c->pat_ptr++;
    }
    if (mask & 0x10) {
        fx2_type = *c->pat_ptr++;
        fx2_val  = *c->pat_ptr++;
    }

    /* Process instrument */
    bool ins_changed = false;
    if (ins != 0xFF && ins < cur_song->ins_count) {
        if (ins != c->last_ins) {
            c->last_ins = ins;
            ins_changed = true;
        }
        c->ins = ins;
        c->macro_vol_step = 0;
        c->macro_arp_step = 0;
        c->macro_duty_step = 0;
        c->macro_wave_step = 0;
    }
    if (c->ins < cur_song->ins_count) {
        instrument = &cur_song->instruments[c->ins];
    }

    /* Process volume column */
    if (vol != 0xFF) {
        c->volume = (vol > 15) ? 15 : vol;
        c->out_vol = c->volume;
    } else if (ins_changed && instrument && !instrument->soft_env && ch != 2) {
        c->volume = instrument->env_vol;
        c->out_vol = c->volume;
    }

    /* Reset row-local cuts */
    c->cut_ticks = 0;

    /* Process Effects (Effect 1 & Effect 2) */
    #define PROCESS_FX(t, v) do { \
        if (t == FUR_FX_ARP) { \
            c->modal_arp = v; \
            c->arp_stage = 0; \
        } else if (t == FUR_FX_VIBRATO) { \
            if (v == 0) { \
                c->modal_vib_rate = 0; \
                c->modal_vib_depth = 0; \
            } else { \
                c->modal_vib_rate = (v >> 4); \
                c->modal_vib_depth = (v & 0x0F); \
            } \
        } else if (t == FUR_FX_PORTA) { \
            if (v != 0) { \
                is_porta = true; \
                c->modal_porta_speed = v; \
            } else { \
                c->modal_porta_speed = 0; \
            } \
        } else if (t == FUR_FX_SLIDE_UP) { \
            if (v == 0) { c->slide_speed = 0; c->slide_dir = 0; } \
            else { c->slide_speed = v; c->slide_dir = 1; } \
        } else if (t == FUR_FX_SLIDE_DOWN) { \
            if (v == 0) { c->slide_speed = 0; c->slide_dir = 0; } \
            else { c->slide_speed = v; c->slide_dir = -1; } \
        } else if (t == FUR_FX_DUTY_CYCLE) { \
            c->duty = (v & 3); \
        } else if (t == FUR_FX_SET_WAVE && ch == 2) { \
            if (v != c->wave_id) { \
                c->wave_id = v; \
                fur_load_wave(v); \
            } \
        } else if (t == FUR_FX_NOISE_MODE && ch == 3) { \
            c->duty = (v & 1); \
        } else if (t == FUR_FX_SWEEP && ch == 0) { \
            NR10_REG = v; \
        } else if (t == FUR_FX_NOTE_CUT) { \
            c->cut_ticks = (v == 0) ? 1 : (v + 1); \
        } else if (t == FUR_FX_VOL_SLIDE) { \
            if ((v & 0xF0) != 0) c->vol_slide = (v >> 4); \
            else if ((v & 0x0F) != 0) c->vol_slide = -(int8_t)(v & 0x0F); \
            else c->vol_slide = 0; \
        } else if (t == FUR_FX_PANNING) { \
            uint8_t pan = 0; \
            if (v & 0xF0) pan |= 0x10; \
            if (v & 0x0F) pan |= 0x01; \
            if (pan == 0) pan = 0x11; \
            if (solo_channel >= 0) { \
                if (ch == (uint8_t)solo_channel) NR51_REG = (pan << ch); \
            } else { \
                NR51_REG = (NR51_REG & ~(0x11 << ch)) | (pan << ch); \
            } \
        } else if (t == 0x05) { \
            /* 05xy: vol slide + vibrato */ \
            if ((v & 0xF0) != 0) c->vol_slide = (v >> 4); \
            else if ((v & 0x0F) != 0) c->vol_slide = -(int8_t)(v & 0x0F); \
            else c->vol_slide = 0; \
        } else if (t == 0x06) { \
            /* 06xy: vol slide + porta */ \
            is_porta = true; \
            if ((v & 0xF0) != 0) c->vol_slide = (v >> 4); \
            else if ((v & 0x0F) != 0) c->vol_slide = -(int8_t)(v & 0x0F); \
            else c->vol_slide = 0; \
        } else if (t == 0xFF) { \
            /* FFxx: stop song */ \
            is_playing = false; \
        } else if (t == FUR_FX_VOL_UP) { \
            if (c->volume + v <= 15) c->volume += v; else c->volume = 15; \
            c->out_vol = c->volume; \
        } else if (t == FUR_FX_VOL_DOWN) { \
            if (c->volume >= v) c->volume -= v; else c->volume = 0; \
            c->out_vol = c->volume; \
        } \
    } while(0)

    if (fx1_type != 0 || fx1_val != 0) PROCESS_FX(fx1_type, fx1_val);
    if (fx2_type != 0 || fx2_val != 0) PROCESS_FX(fx2_type, fx2_val);

    /* Process Note Trigger / Portamento */
    if (note == FUR_NOTE_OFF) {
        c->active = false;
        c->modal_arp = 0;
        c->vol_slide = 0;
        c->key_on = false;
        if (ch == 2) NR30_REG = 0x00;
        else if (ch == 0) { NR12_REG = 0x08; NR14_REG = 0x80; }
        else if (ch == 1) { NR22_REG = 0x08; NR24_REG = 0x80; }
        else if (ch == 3) { NR42_REG = 0x08; NR44_REG = 0x80; }
    } else if (note != FUR_NOTE_NONE) {
        uint8_t note_idx = (note >= 60) ? (note - 60) : note;
        if (note_idx >= 96) note_idx = 95;

        if (is_porta && c->active) {
            /* Tone portamento: target note changed, NO retrigger */
            c->target_note = note_idx;
            c->target_freq = (ch == 3) ? (note_idx < 69 ? noise_table[note_idx] : 0) : note_freqs[note_idx];
        } else {
            /* Authentic Note-On */
            c->active = true;
            c->key_on = true;
            c->base_note = note_idx;
            c->target_note = note_idx;
            c->current_freq = (ch == 3) ? (note_idx < 69 ? noise_table[note_idx] : 0) : note_freqs[note_idx];
            c->target_freq = c->current_freq;
            c->arp_stage = 0;
            c->macro_vol_step = 0;
            c->macro_arp_step = 0;
            c->macro_duty_step = 0;
            c->macro_wave_step = 0;

            /* Immediately initialize duty & volume from initial macro steps */
            if (instrument && instrument->duty_macro && instrument->duty_macro->len > 0) {
                c->duty = (uint8_t)instrument->duty_macro->values[0] & 3;
            }
            if (instrument && instrument->vol_macro && instrument->vol_macro->len > 0) {
                c->out_vol = ((uint16_t)c->volume * (uint8_t)instrument->vol_macro->values[0]) / 15;
            } else {
                c->out_vol = c->volume;
            }
            c->last_kill = c->out_vol;

            /* Set Initial Hardware Registers */
            if (ch == 0) {
                if (instrument && instrument->soft_env) {
                    NR11_REG = (c->duty << 6) | 0x3F;
                    NR12_REG = (c->out_vol << 4) | 0x08;
                } else {
                    uint8_t env_dir = instrument ? instrument->env_dir : 0;
                    uint8_t env_len = instrument ? instrument->env_len : 2;
                    uint8_t sl = instrument ? instrument->sound_len : 64;
                    NR11_REG = (c->duty << 6) | ((64 - sl) & 0x3F);
                    NR12_REG = (c->volume << 4) | (env_dir << 3) | env_len;
                }
            } else if (ch == 1) {
                if (instrument && instrument->soft_env) {
                    NR21_REG = (c->duty << 6) | 0x3F;
                    NR22_REG = (c->out_vol << 4) | 0x08;
                } else {
                    uint8_t env_dir = instrument ? instrument->env_dir : 0;
                    uint8_t env_len = instrument ? instrument->env_len : 2;
                    uint8_t sl = instrument ? instrument->sound_len : 64;
                    NR21_REG = (c->duty << 6) | ((64 - sl) & 0x3F);
                    NR22_REG = (c->volume << 4) | (env_dir << 3) | env_len;
                }
            } else if (ch == 2) {
                /* Wave Channel 3 */
                if (instrument && instrument->wave_macro && instrument->wave_macro->len > 0) {
                    uint16_t wid = (uint8_t)instrument->wave_macro->values[0];
                    if (wid != c->wave_id) {
                        c->wave_id = wid;
                        fur_load_wave(wid);
                    }
                }
                NR30_REG = 0x80;
                NR32_REG = gb_vol_map[c->out_vol];
            } else if (ch == 3) {
                if (instrument && instrument->soft_env) {
                    NR41_REG = 0x3F;
                    NR42_REG = (c->out_vol << 4) | 0x08;
                } else {
                    uint8_t env_dir = instrument ? instrument->env_dir : 0;
                    uint8_t env_len = instrument ? instrument->env_len : 2;
                    uint8_t sl = instrument ? instrument->sound_len : 64;
                    NR41_REG = (64 - sl) & 0x3F;
                    NR42_REG = (c->volume << 4) | (env_dir << 3) | env_len;
                }
            }
        }
    }
}

static void fur_step_channel(uint8_t ch) {
    fur_channel_t *c = &channels[ch];
    const fur_instrument_t *ins = NULL;
    uint16_t freq;
    int16_t vib_offset = 0;
    uint8_t note = c->base_note;
    uint8_t len_bit = 0;

    if (!c->active) return;

    if (c->ins < cur_song->ins_count) {
        ins = &cur_song->instruments[c->ins];
    }

    /* 1. Delayed Note Cut (ECxx) */
    if (c->cut_ticks > 0) {
        c->cut_ticks--;
        if (c->cut_ticks == 0) {
            c->active = false;
            c->modal_arp = 0;
            if (ch == 2) NR30_REG = 0x00;
            else if (ch == 0) NR12_REG = 0x08;
            else if (ch == 1) NR22_REG = 0x08;
            else if (ch == 3) NR42_REG = 0x08;
            return;
        }
    }

    /* 2. Step Persistent Volume Slide (0Axy) */
    if (c->vol_slide != 0) {
        int16_t new_vol = (int16_t)c->volume + c->vol_slide;
        if (new_vol >= 15) {
            new_vol = 15;
            c->vol_slide = 0;
        } else if (new_vol <= 0) {
            new_vol = 0;
            c->vol_slide = 0;
        }
        c->volume = (uint8_t)new_vol;
        c->out_vol = c->volume;
        if (ins && ins->soft_env) {
            if (ch == 2) NR32_REG = gb_vol_map[c->out_vol];
            else fur_set_hardware_vol(ch, c->out_vol);
        }
    }

    /* 3. Step Tone Portamento (03xx, 06xy) directly in period domain */
    if (c->modal_porta_speed > 0 && ch != 3) {
        if (c->current_freq < c->target_freq) {
            if (c->current_freq + c->modal_porta_speed >= c->target_freq) {
                c->current_freq = c->target_freq;
            } else {
                c->current_freq += c->modal_porta_speed;
            }
        } else if (c->current_freq > c->target_freq) {
            if (c->current_freq <= c->target_freq + c->modal_porta_speed) {
                c->current_freq = c->target_freq;
            } else {
                c->current_freq -= c->modal_porta_speed;
            }
        }
    }

    /* 4. Step Pitch Slide (01xx / 02xx) */
    if (c->slide_dir != 0 && ch != 3) {
        if (c->slide_dir > 0) {
            if (c->current_freq + c->slide_speed >= 2047) c->current_freq = 2047;
            else c->current_freq += c->slide_speed;
        } else {
            if (c->current_freq <= c->slide_speed) c->current_freq = 0;
            else c->current_freq -= c->slide_speed;
        }
    }

    /* 5. Step Instrument Macros */
    if (ins) {
        /* Volume macro */
        if (ins->vol_macro && ins->vol_macro->len > 0) {
            uint8_t m_vol = (uint8_t)ins->vol_macro->values[c->macro_vol_step];
            c->out_vol = ((uint16_t)c->volume * m_vol) / 15;
            c->macro_vol_step++;
            if (c->macro_vol_step >= ins->vol_macro->len) {
                if (ins->vol_macro->loop != 0xFF) c->macro_vol_step = ins->vol_macro->loop;
                else c->macro_vol_step = ins->vol_macro->len - 1;
            }
            if (ch == 2) {
                NR32_REG = gb_vol_map[c->out_vol];
            } else if (ins->soft_env) {
                fur_set_hardware_vol(ch, c->out_vol);
            }
        }
        /* Arp macro */
        if (ins->arp_macro && ins->arp_macro->len > 0) {
            int8_t offset = ins->arp_macro->values[c->macro_arp_step];
            if ((int16_t)note + offset >= 0 && (int16_t)note + offset < 96) {
                note = (uint8_t)(note + offset);
            }
            c->macro_arp_step++;
            if (c->macro_arp_step >= ins->arp_macro->len) {
                if (ins->arp_macro->loop != 0xFF) c->macro_arp_step = ins->arp_macro->loop;
                else c->macro_arp_step = ins->arp_macro->len - 1;
            }
        }
        /* Duty macro */
        if (ins->duty_macro && ins->duty_macro->len > 0) {
            c->duty = (uint8_t)ins->duty_macro->values[c->macro_duty_step] & 3;
            c->macro_duty_step++;
            if (c->macro_duty_step >= ins->duty_macro->len) {
                if (ins->duty_macro->loop != 0xFF) c->macro_duty_step = ins->duty_macro->loop;
                else c->macro_duty_step = ins->duty_macro->len - 1;
            }
        }
        /* Wave macro (CH3) */
        if (ch == 2 && ins->wave_macro && ins->wave_macro->len > 0) {
            uint16_t wid = (uint8_t)ins->wave_macro->values[c->macro_wave_step];
            if (wid != c->wave_id) {
                c->wave_id = wid;
                fur_load_wave(wid);
                c->key_on = true;
            }
            c->macro_wave_step++;
            if (c->macro_wave_step >= ins->wave_macro->len) {
                if (ins->wave_macro->loop != 0xFF) c->macro_wave_step = ins->wave_macro->loop;
                else c->macro_wave_step = ins->wave_macro->len - 1;
            }
        }
    }

    /* 6. Frequency Resolution */
    if (ch == 3) {
        freq = (note < 69) ? noise_table[note] : 0;
    } else if (c->modal_arp != 0) {
        uint8_t arp_semi = 0;
        if (c->arp_stage == 1) arp_semi = (c->modal_arp >> 4);
        else if (c->arp_stage == 2) arp_semi = (c->modal_arp & 0x0F);
        
        c->arp_stage++;
        if (c->arp_stage >= 3) c->arp_stage = 0;

        if (note + arp_semi < 96) freq = note_freqs[note + arp_semi];
        else freq = c->current_freq;
    } else if (ins && ins->arp_macro && ins->arp_macro->len > 0) {
        freq = note_freqs[note];
    } else {
        freq = c->current_freq;
    }

    /* 7. Step Modal Vibrato (04xy) */
    if (c->modal_vib_depth > 0 && ch != 3) {
        c->vib_pos = (c->vib_pos + c->modal_vib_rate) & 63;
        vib_offset = ((int16_t)vib_sine_table[c->vib_pos] * (int16_t)c->modal_vib_depth) / 15;
        if ((int32_t)freq + vib_offset > 2047) freq = 2047;
        else if ((int32_t)freq + vib_offset < 0) freq = 0;
        else freq = (uint16_t)(freq + vib_offset);
    }

    /* 8. Hardware length counter bit 6 (NRx4 |= 0x40 if sound_len < 64 and !soft_env) */
    if (ins && !ins->soft_env && ins->sound_len < 64) {
        len_bit = 0x40;
    }

    /* 9. Write updated frequency & volume to Game Boy APU */
    if (ch == 0) {
        NR11_REG = (c->duty << 6) | (ins && !ins->soft_env && ins->sound_len < 64 ? ((64 - ins->sound_len) & 0x3F) : 0x3F);
        NR13_REG = (uint8_t)(freq & 0xFF);
        NR14_REG = (uint8_t)((freq >> 8) & 0x07) | (c->key_on ? 0x80 : 0x00) | len_bit;
        c->key_on = false;
    } else if (ch == 1) {
        NR21_REG = (c->duty << 6) | (ins && !ins->soft_env && ins->sound_len < 64 ? ((64 - ins->sound_len) & 0x3F) : 0x3F);
        NR23_REG = (uint8_t)(freq & 0xFF);
        NR24_REG = (uint8_t)((freq >> 8) & 0x07) | (c->key_on ? 0x80 : 0x00) | len_bit;
        c->key_on = false;
    } else if (ch == 2) {
        NR32_REG = gb_vol_map[c->out_vol];
        NR33_REG = (uint8_t)(freq & 0xFF);
        NR34_REG = (uint8_t)((freq >> 8) & 0x07) | (c->key_on ? 0x80 : 0x00) | len_bit;
        c->key_on = false;
    } else if (ch == 3) {
        NR43_REG = (uint8_t)(freq & 0xF7) | (c->duty ? 0x08 : 0x00);
        if (c->key_on) {
            NR44_REG = 0x80 | len_bit;
            c->key_on = false;
        }
    }
}

void fur_driver_tick(void) {
    uint8_t i;
    if (!is_playing || !cur_song) return;

    /* Virtual tempo engine (Furnace playback.cpp lines 2241-2247) */
    tempo_accum += cur_song->virtual_tempo_n;
    while (tempo_accum >= cur_song->virtual_tempo_d) {
        tempo_accum -= cur_song->virtual_tempo_d;

        if (tick_counter == 0) {
            /* Advance Row */
            cur_row++;
            if (cur_row >= cur_song->pattern_len) {
                cur_row = 0;
                cur_order++;
                if (cur_order >= cur_song->order_len) {
                    cur_order = cur_song->loop_order;
                }
                fur_init_channel_pattern(0, cur_song->order_ch1[cur_order]);
                fur_init_channel_pattern(1, cur_song->order_ch2[cur_order]);
                fur_init_channel_pattern(2, cur_song->order_ch3[cur_order]);
                fur_init_channel_pattern(3, cur_song->order_ch4[cur_order]);
            }

            /* Process Row across all 4 channels */
            fur_decode_channel_row(0);
            fur_decode_channel_row(1);
            fur_decode_channel_row(2);
            fur_decode_channel_row(3);

            /* Next speed from groove / speeds array */
            tick_counter = cur_song->speeds[speed_idx % cur_song->speed_len];
            speed_idx++;
        }

        tick_counter--;
    }

    /* Step intermediate tick updates for all 4 channels (once per 60Hz frame) */
    for (i = 0; i < 4; i++) {
        fur_step_channel(i);
    }
}
