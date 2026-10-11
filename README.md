# fur2gb

Plays Furnace Game Boy songs on real hardware, identical to Furnace.

## How it works

Instead of re-implementing Furnace's playback engine on the Game Boy, **Furnace itself is the
compiler**:

1. `tools/furtrace.py` checks that the song uses exactly one Game Boy chip. It then runs Furnace
   0.6.8.2 (the release the songs are written in) with a small patch
   (`tools/furnace_trace.patch`) that logs every register write its GB backend makes, tick by
   tick, plus the loop points.
2. `tools/fur2gb.py` turns that log into compact bytecode and splits each tick's writes into
   5 streams, in the order Furnace makes them:
   - A: writes made while Furnace processes the row (instrument, volume, panning...);
   - B0-B3: writes made by the chip's `tick()` for channel 1-4.

   Repeated passages of each stream become `CALL`s back into earlier data (LZ-style), and
   Furnace's zombie-mode volume bursts become one op. The streams are split into 16 KB banks. The
   converter finds the loop pass that repeats unchanged and jumps back to it.
3. `driver/furplayer` replays the streams. Its tick routine is in assembly and lives in bank 0.

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

## Verification

`tests/verify_rom.py song.fur ...` proves the ROM matches Furnace:

1. Furnace plays the song for 4 or more loops and records every register write (the reference).
2. The converter's Python model of the player must reproduce every write of every tick.
3. A test ROM is built with GBDK and run headless in Mesen. Every write to `$FF10-$FF3F` made by
   each `fur_tick()` call is captured.
4. The ROM's writes must equal Furnace's, in the same order and tick by tick, for all traced
   ticks: the intro, the first loop, and the repeats (which exercise the loop jump and the
   anti-click state carried across loops).

Intentional differences and limits:

- **NR50's VIN bits are cleared** (Furnace writes `$FF`, the player `$77`). Furnace's emulated chip
  ignores VIN, so its audio is unchanged, and on hardware VIN enabled with nothing driving it can
  add noise.
- **FFxx (stop)**: the player stops on the stop tick. Furnace sometimes runs one more chip tick
  after it, depending on where its audio buffer ends, so that tick isn't stable in Furnace either.
- **Virtual tempo that doesn't divide evenly** (for example 127/150): Furnace advances rows on the
  virtual tempo clock but runs macros every real tick, so one pass of the song lasts a fractional
  number of ticks. The song only repeats exactly after many passes (up to N of N/150). The
  converter refuses such songs unless you pass `--approx-loop`. With it, the intro and first pass
  are exact and the stream then jumps back, so later passes can be one tick off in places.
- **Timing within a tick** isn't covered by the write comparison. Furnace applies a tick's writes
  16 cycles apart; the player takes about 30-100 cycles per write. The GB's 512 Hz frame sequencer
  (envelope, length and sweep clocks) runs freely from DIV on hardware.

## Usage

```
python tools/fur2gb.py song.fur -o build/songs -n my_song
```

The command writes `my_song.c`, `my_song.h` and `my_song_chunkN.c` (autobanked, `#pragma bank 255`).

```c
#include "furplayer.h"
#include "my_song.h"

fur_play(&my_song, BANK(my_song));
// then once per VBlank:
fur_frame();      // plays 0-2 ticks so the song keeps its own tick rate (60 Hz, 57.6 Hz, ...)
// or drive fur_tick() yourself at the song's tick rate
```

CPU: `fur_tick()` takes about 1.5-6k cycles on average and up to about 12k on the busiest ticks,
measured on Pocket Dash's songs. For comparison, Pocket Dash's hUGEDriver ISR is about 4k on
average and 14k at most.

## Rebuilding the reference Furnace

```
sh tools/build_furnace.sh
```

The script clones v0.6.8.2, applies the trace patch and builds the CLI with MSYS2 UCRT64.
