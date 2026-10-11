# fur2gb

Plays Furnace Game Boy songs on real hardware, identical to Furnace.

## How it works

Instead of re-implementing Furnace's playback engine on the Game Boy, **Furnace itself is the
compiler**:

1. `tools/furtrace.py` runs Furnace 0.6.8.2 (the release the songs are written in) with a small
   patch (`tools/furnace_trace.patch`) that logs every register write its GB backend makes, tick by
   tick, plus the loop points.
2. `tools/fur2gb.py` turns that log into a compact bytecode stream: register writes, empty-tick
   runs, wave loads and `CALL`s back into earlier data for repeated passages (LZ-style), split into
   16 KB banks. It looks for the first loop pass that repeats unchanged and jumps back to it.
3. `driver/furplayer` (bank 0, the tick routine in assembly) replays the stream.

Every effect, macro (ADSR/LFO included), WaveSynth, hardware sequence, compat flag, groove and tick
rate is therefore exactly Furnace's behaviour, with no per-feature porting.

The one piece of Furnace state the player computes itself is the GB backend's **anti-click** wave
rotation. When Furnace rewrites wave RAM it rotates the wave by how far the wave channel has
played, which depends on time, so it differs on every loop. The player runs the same integer
arithmetic as `DivPlatformGB::tick()`:

```
cnt += trunc(2097152 / tickHz); pos += cnt / freq; cnt %= freq
```

It stores only the base waves. The converter checks this model against the position Furnace
reports at every single wave rewrite.

## Verification

`tests/verify_rom.py song.fur ...` proves the ROM matches Furnace:

1. Furnace plays the song for 4 loops and records every register write (the reference).
2. The converter's Python model of the player must reproduce every write of every tick.
3. A test ROM is built with GBDK and run headless in Mesen. Every write to `$FF10-$FF3F` made by
   each `fur_tick()` call is captured.
4. The ROM's writes must equal Furnace's, in the same order and tick by tick, for all traced
   ticks: the intro, the first loop, and repeats 2-4 (which exercise the loop jump and the
   anti-click state carried across loops).

The only intentional difference is that **NR50's VIN bits are cleared** (Furnace writes `$FF`,
the player writes `$77`). Furnace's emulated chip ignores VIN, so its audio is unchanged, and on
hardware VIN enabled with nothing driving it can add noise.

Not covered by the write comparison: when inside a tick the writes land. Furnace applies one write
per 16 cycles at the start of the tick; the player takes about 30-100 cycles per write. The GB's
512 Hz frame sequencer (envelope, length and sweep clocks) also runs freely from DIV on hardware.

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

## Rebuilding the reference Furnace

```
sh tools/build_furnace.sh
```

The script clones v0.6.8.2, applies the trace patch and builds the CLI with MSYS2 UCRT64.
