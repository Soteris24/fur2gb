/* Listening ROM: plays fur_test_song at its own tick rate (fur_frame() once per VBlank). */
#include <gb/gb.h>
#include "furplayer.h"

BANKREF_EXTERN(fur_test_song)
extern const fur_song_t fur_test_song;

void main(void) {
    fur_play(&fur_test_song, BANK(fur_test_song));
    while (1) {
        wait_vbl_done();
        fur_frame();
    }
}
