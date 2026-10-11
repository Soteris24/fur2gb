/* Test ROM: plays fur_test_song, one Furnace tick per frame. tests/verify_rom.py watches the
   APU writes made between fur_tick() calls and the end-of-tick marker below. */
#include <gb/gb.h>
#include "furplayer.h"

BANKREF_EXTERN(fur_test_song)
extern const fur_song_t fur_test_song;

volatile uint8_t fur_test_mark;

#ifndef FUR_TEST_MUTE
#define FUR_TEST_MUTE 0
#endif

void main(void) {
    fur_mute = FUR_TEST_MUTE;
    fur_play(&fur_test_song, BANK(fur_test_song));
    while (1) {
        wait_vbl_done();
        fur_tick();
        fur_test_mark++;
    }
}
