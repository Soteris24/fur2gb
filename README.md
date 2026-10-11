# fur2gb

Plays Furnace Game Boy songs on a Game Boy exactly as Furnace plays them. The ROM makes the same
register writes as Furnace's Game Boy chip, in the same order, tick by tick.

## Requirements

- Windows with [MSYS2](https://www.msys2.org/) (UCRT64: `cmake`, `ninja`, `gcc`). It is only
  needed once, to build the reference Furnace.
- Python 3.10+
- [GBDK-2020](https://github.com/gbdk-2020/gbdk-2020) 4.4+ (default `C:\gbdk`; set `GBDK_HOME` to
  change it)
- [Mesen 2](https://www.mesen.ca/), only for `tests/verify_rom.py` (set `MESEN` to point at
  `Mesen.exe`)

## Quick start

### 1. Build the reference Furnace (once)

```
sh tools/build_furnace.sh
```

This clones Furnace **v0.6.8.2** (the version the songs are written in) into `.build/`, applies
`tools/furnace_trace.patch` and builds the command-line player. It takes about 15 minutes. Set
`FUR2GB_FURNACE` to use a different traced build.

### 2. Convert a song

```
python tools/fur2gb.py "music/My Song.fur" -o build/songs -n my_song
```

The command writes `build/songs/my_song.c`, `my_song.h` and `my_song_chunkN.c`. The chunk files are
autobanked (`#pragma bank 255`), so build with `-autobank`. The converter refuses files that
aren't a single Game Boy chip.

Options:
- `--approx-loop`: for songs whose loop never repeats exactly (see *Limits*).
- `--subsong N`: convert another subsong.
- `--loops N`: how many loops to trace when looking for the repeat (default 4; doubles itself up
  to 64 when needed).

### 3. Play it

Compile `driver/furplayer.c` and `driver/furplayer_tick.s` together with the generated files:

```c
#include "furplayer.h"
#include "my_song.h"

fur_play(&my_song, BANK(my_song));
while (1) {
    wait_vbl_done();
    fur_frame();   // plays 0-2 ticks so the song keeps its own tick rate (60 Hz, 57.6 Hz, ...)
}
```

Other calls:
- `fur_tick()` plays exactly one Furnace tick, if you drive the timing yourself (for example from a
  timer interrupt).
- `fur_stop()` silences the sound chip.
- `fur_playing` becomes 0 when a song without a loop ends.

`fur_tick()` and `fur_frame()` live in bank 0, restore the caller's ROM bank and may be called
from an interrupt handler.

### 4. Listen

```
python tools/build_listen.py music/*.fur
```

The command builds one ROM per song in `build/listen/`.

### 5. Verify

```
python tests/verify_rom.py music/*.fur
```

For each song:

1. Furnace plays it for 4 or more loops and records every register write (the reference).
2. The converter's Python model of the player must reproduce all of them.
3. A test ROM runs headless in Mesen, and every write to `$FF10-$FF3F` made by each `fur_tick()`
   call is captured.
4. The ROM's writes must equal Furnace's, in the same order and tick by tick, for every traced
   tick, through the loop jumps.

It prints `PASS` or the first differing write, with its order and row.

## How it works

Instead of re-implementing Furnace's playback engine on the Game Boy, **Furnace itself is the
compiler**:

1. `tools/furtrace.py` runs the patched Furnace, which logs every register write its GB backend
   makes, tick by tick, plus the loop points.
2. `tools/fur2gb.py` turns the log into bytecode, split into 5 streams in the order Furnace writes:
   - A: writes made while Furnace processes the row (instrument, volume, panning...);
   - B0-B3: writes made by the chip's `tick()` for channel 1-4.

   Repeated passages of each stream become `CALL`s back into earlier data (LZ-style). Furnace's
   zombie-mode volume bursts become one op. The streams are split into 16 KB banks. The converter
   finds the loop pass that repeats unchanged and jumps back to it.
3. `driver/furplayer` replays the streams. Its tick routine is written in assembly.

Every effect, macro (ADSR/LFO included), WaveSynth, hardware sequence, compat flag, groove and tick
rate is therefore exactly Furnace's behaviour, with no per-feature porting.

The one piece of Furnace state the player computes itself is the GB backend's **anti-click** wave
rotation. When Furnace rewrites wave RAM it rotates the wave by how far the wave channel has
played, which depends on time, so it differs on every loop. The player uses the same integer
arithmetic as `DivPlatformGB::tick()`:

```
cnt += trunc(2097152 / tickHz); pos += cnt / freq; cnt %= freq
```

It stores only the base waves. The converter checks this model against the position Furnace
reports at every single wave rewrite.

The stream bytecode is documented at the top of `tools/fur2gb.py`.

## Limits

- **NR50's VIN bits are cleared** (Furnace writes `$FF`, the player `$77`). Furnace's emulated chip
  ignores VIN, so its audio is unchanged, and on hardware VIN enabled with nothing driving it can
  add noise.
- **FFxx (stop)**: the player stops on the stop tick. Furnace sometimes runs one more chip tick
  after it, depending on where its audio buffer ends, so that tick isn't stable in Furnace either.
- **Virtual tempo that doesn't divide evenly** (for example 127/150): Furnace advances rows on the
  virtual tempo clock but runs macros every real tick, so one pass of the song lasts a fractional
  number of ticks. The song only repeats exactly after many passes (up to N of N/150). The
  converter refuses such songs unless you pass `--approx-loop`. With it, the intro and first pass
  are exact and the stream then jumps back, so later passes can be one tick off in places. To make
  such a song exact forever, pick a virtual tempo that makes one pass a whole number of ticks.
- **The GBA (native) chip model** isn't supported (its 64-sample waves don't exist on a Game Boy).
- **Timing within a tick** isn't covered by the write comparison. Furnace applies a tick's writes
  16 cycles apart; the player takes about 30-100 cycles per write. The GB's 512 Hz frame sequencer
  (envelope, length and sweep clocks) runs freely from DIV on hardware.

## Cost

| | |
|---|---|
| ROM | 3-34 KB per song (Pocket Dash's songs) |
| RAM | about 260 bytes |
| CPU | `fur_tick()` takes about 1.5-6k cycles on average and up to about 12k on the busiest ticks |

For comparison, hUGEDriver in Pocket Dash costs about 4k cycles per frame on average and 14k at
most.

## Layout

```
driver/      the Game Boy player (furplayer.h/.c, furplayer_tick.s)
tools/       fur2gb.py (converter), furtrace.py (Furnace runner), build_listen.py,
             build_furnace.sh + furnace_trace.patch (reference Furnace)
tests/       verify_rom.py (+ verify_rom.lua for Mesen), rom/ (test and listening ROM mains)
test_songs/  example songs
```
