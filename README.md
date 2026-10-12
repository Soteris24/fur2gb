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

Compile `driver/furplayer.c`, `driver/furplayer_core.c` and `driver/furplayer_tick.s` together with the generated
files. `furplayer_core.c` and `furplayer_tick.s` must be in bank 0; `furplayer.c` may be banked (define
`FUR_API` as `BANKED` then):

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
- `fur_set_mute(mask)` stops the song writing to the channels in `mask` (bit n = channel n+1), so a
  sound effect can use them. Unmuting channel 3 reloads the song's current wave.
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

`--mute N` builds the ROM with channels muted (`fur_mute = N`). The ROM must then make exactly
Furnace's writes minus those channels'.

For each song:

1. Furnace plays it for 4 or more loops and records every register write (the reference).
2. The converter's Python model of the player must reproduce all of them.
3. A test ROM runs headless in Mesen, and every write to `$FF10-$FF3F` made by each `fur_tick()`
   call is captured.
4. The ROM's writes must equal Furnace's, in the same order and tick by tick, for every traced
   tick, through the loop jumps.

It prints `PASS` or the first differing write, with its order and row.

## uge2fur: hUGETracker songs to Furnace

`tools/uge2fur.py` converts a hUGETracker module (`.uge`, any version) into a Furnace module for
the Game Boy that plays like hUGEDriver plays it, and that you can keep editing in Furnace:

```
python tools/uge2fur.py song.uge -o song.fur
```

The song keeps its structure: the same orders and patterns (a pattern that has to differ between
orders is split, identical ones are shared), one Furnace instrument per hUGETracker instrument
(subpatterns become macros and Game Boy hardware sequences), the same waves, speed and tick rate.
hUGETracker effects become their Furnace equivalents.

hUGEDriver doesn't work like Furnace, so the converter plays the song with a model of hUGEDriver
(`tools/hugedriver.py`) and writes what Furnace needs to do the same on every row: stops for effects
that last one row in hUGETracker but continue in Furnace, legato for notes without an instrument
(hUGEDriver doesn't restart those), arpeggio orders that line up with hUGEDriver's global tick
counter, its square-wave vibrato (`E308`, `E402`), fine pitch for the six notes its note table has
1-2 units away from equal temperament, and its quirks (a CH3 note without an instrument silences
the channel, arpeggios and slides turn the note length off, `9xx` on noise ORs into NR43, ...).

**Checking.** With the reference Furnace built (step 1 above), `uge2fur.py` renders the result and
compares what every channel outputs on every tick (`tools/apumodel.py`: volume, period, duty,
wave, panning) with hUGEDriver's output, and prints how many ticks differ. Differences that don't
matter are counted apart: 1-2 period units (hUGEDriver's note table, under 3 cents) and arpeggios
or vibratos at another point of their cycle. `tools/uge2fur_debug.py song.uge TICK` shows both
sides around a tick. On hUGETracker's 23 sample songs and 17 Pocket Dash songs, most songs match
on 99-100% of the ticks.

**The model.** `tools/hugedriver.py` follows hUGEDriver (hUGETracker 1.0.11) instruction by
instruction; `tests/verify_hugedriver.py` checks it against the real driver (built with
hUGETracker's `uge2source` and rgbds, run in Mesen: `HUGETRACKER`, `MESEN`): 49 of 52 songs make
the same writes for 20000 ticks, the other 3 hit a hUGETracker export bug the model reports (a
pattern used on two channels only exports the instruments of the first one; the driver then reads
garbage, in hUGETracker too).

What can't be converted exactly (the converter warns): `5xx` (NR50 master volume) and `6xx`
(assembly routines) have no Furnace equivalent; `8xx` that turns a channel off in both speakers;
slides past the top or bottom of the period range (hUGEDriver wraps them around); a few effects
inside subpatterns (`3xx`, `7xx`, `Exx`).

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
| Bank 0 | about 700 bytes (`furplayer_tick.s` and `furplayer_core.c`) |
| CPU | `fur_tick()` takes about 1.5-6k cycles on average and up to about 12k on the busiest ticks |

For comparison, hUGEDriver in Pocket Dash costs about 4k cycles per frame on average and 14k at
most.

## Layout

```
driver/      the Game Boy player (furplayer.h/.c, furplayer_core.c, furplayer_tick.s)
tools/       fur2gb.py (converter), furtrace.py (Furnace runner), build_listen.py,
             build_furnace.sh + furnace_trace.patch (reference Furnace)
             uge2fur.py (hUGETracker -> Furnace), uge.py (.uge reader), hugedriver.py (driver
             model), furfile.py (.fur writer), apumodel.py (APU output comparison),
             uge2fur_debug.py
tests/       verify_rom.py (+ verify_rom.lua for Mesen), rom/ (test and listening ROM mains),
             verify_hugedriver.py + huge_reference.py + huge_ref/ (driver model vs real driver)
test_songs/  example songs
```
