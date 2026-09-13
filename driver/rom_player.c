#include <gb/gb.h>
#include "fur_driver.h"

extern const fur_song_t test_song;

void main(void) {
#ifdef SOLO_CHANNEL
    fur_driver_set_solo(SOLO_CHANNEL);
#endif
    fur_driver_init(&test_song);

    while (1) {
        wait_vbl_done();
        fur_driver_tick();
    }
}
