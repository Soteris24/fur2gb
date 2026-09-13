#ifndef _FUR_DRIVER_H
#define _FUR_DRIVER_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#define FUR_NOTE_NONE       0
#define FUR_NOTE_OFF        254
#define FUR_PATTERN_END     255

/* Effect Types */
#define FUR_FX_NONE         0x00
#define FUR_FX_ARP          0x00 /* 00xy: Arpeggio (x, y semitones) - MODAL */
#define FUR_FX_SLIDE_UP     0x01 /* 01xx: Pitch slide up */
#define FUR_FX_SLIDE_DOWN   0x02 /* 02xx: Pitch slide down */
#define FUR_FX_PORTA        0x03 /* 03xx: Tone portamento - MODAL */
#define FUR_FX_VIBRATO      0x04 /* 04xy: Vibrato (x=speed, y=depth) - MODAL */
#define FUR_FX_PORTA_VOL    0x05 /* 05xx: Portamento + volume slide */
#define FUR_FX_VIB_VOL      0x06 /* 06xx: Vibrato + volume slide */
#define FUR_FX_PANNING      0x08 /* 08xx: Set panning */
#define FUR_FX_SET_SPEED    0x09 /* 09xx: Set speed */
#define FUR_FX_VOL_SLIDE    0x0A /* 0Axy: Volume slide */
#define FUR_FX_PAT_BREAK    0x0D /* 0Dxx: Pattern break */
#define FUR_FX_SET_WAVE     0x10 /* 10xx: Set wave table ID on CH3 */
#define FUR_FX_NOISE_MODE   0x11 /* 11xx: Set noise LFSR mode (0: 15-bit, 1: 7-bit) */
#define FUR_FX_DUTY_CYCLE   0x12 /* 12xx: Set duty cycle (0..3) */
#define FUR_FX_SWEEP        0x13 /* 13xy: Set sweep on CH1 */
#define FUR_FX_NOTE_CUT     0xEC /* ECxx: Delayed note cut after xx ticks */
#define FUR_FX_VOL_UP       0xF8 /* F8xx: Single volume slide up */
#define FUR_FX_VOL_DOWN     0xF9 /* F9xx: Single volume slide down */

/* Instrument Macro Definition */
typedef struct {
    uint8_t len;
    uint8_t loop;               /* 0xFF if no loop */
    const int8_t *values;
} fur_macro_t;

/* Instrument Definition */
typedef struct {
    uint8_t env_vol;            /* Initial volume (0..15) */
    uint8_t env_dir;            /* 0 = fade down, 1 = fade up */
    uint8_t env_len;            /* Envelope length/speed (0..7) */
    uint8_t sound_len;          /* Sound length (0..64) */
    uint8_t soft_env;           /* 1 if using software volume envelope */
    const fur_macro_t *vol_macro;
    const fur_macro_t *arp_macro;
    const fur_macro_t *duty_macro;
    const fur_macro_t *wave_macro;
} fur_instrument_t;

/* Song Definition */
typedef struct {
    const char *title;
    uint8_t tick_rate;          /* 60 (Hz) */
    uint8_t virtual_tempo_n;    /* Virtual tempo numerator */
    uint8_t virtual_tempo_d;    /* Virtual tempo denominator */
    uint8_t speed_len;
    const uint8_t *speeds;
    uint16_t pattern_len;       /* Rows per pattern */
    uint8_t order_len;          /* Total orders */
    uint8_t loop_order;         /* Order to loop to */
    const uint8_t *order_ch1;
    const uint8_t *order_ch2;
    const uint8_t *order_ch3;
    const uint8_t *order_ch4;
    const uint8_t *const *patterns_ch1;
    const uint8_t *const *patterns_ch2;
    const uint8_t *const *patterns_ch3;
    const uint8_t *const *patterns_ch4;
    uint16_t wave_count;
    const uint8_t (*waves)[16]; /* 16 bytes per 32-sample wave */
    uint8_t ins_count;
    const fur_instrument_t *instruments;
} fur_song_t;

/* Driver Public API */
void fur_driver_init(const fur_song_t *song);
void fur_driver_tick(void);
void fur_driver_stop(void);
void fur_driver_set_solo(int8_t solo_ch);
uint8_t fur_driver_get_order(void);
uint16_t fur_driver_get_row(void);
bool fur_driver_is_playing(void);

#endif /* _FUR_DRIVER_H */
