# Furnace Game Boy source map

The vendored Furnace source is the authority for this phase.

| Subsystem | Source |
|---|---|
| Register names and GB model setup | `furnace/src/engine/platform/gb.cpp`: `setFlags`, `reset` |
| Channel initialization | `gb.cpp`: `reset`, `dispatch` (`DIV_CMD_NOTE_ON`) |
| Note processing | `gb.cpp`: `dispatch` |
| Hard note-off | `gb.cpp`: `dispatch` (`DIV_CMD_NOTE_OFF`) |
| Release | `gb.cpp`: `dispatch` (`DIV_CMD_NOTE_OFF_ENV`, `DIV_CMD_ENV_RELEASE`) |
| Standard macros | `gb.cpp`: `tick`, `chan[i].std.next()` |
| Volume macro/zombie mode | `gb.cpp`: `tick`, `killIt` handling |
| Arpeggio | `gb.cpp`: `tick`, `handleArp`/`calcArp` path |
| Pitch | `gb.cpp`: `tick`, `std.pitch`; `DivPitchTable` |
| Portamento | `gb.cpp`: `dispatch` (`DIV_CMD_NOTE_PORTA`) |
| Duty/noise mode | `gb.cpp`: `tick`, `DIV_CMD_STD_NOISE_MODE` |
| Hardware sweep | `gb.cpp`: `DIV_CMD_GB_SWEEP_*`, hardware sequence |
| Wave selection | `gb.cpp`: `DIV_CMD_WAVE`, `tick` |
| Wave RAM/WaveSynth | `gb.cpp`: `updateWave`, `tick`; `waveSynth.h/.cpp` |
| Panning | `gb.cpp`: `DIV_CMD_PANNING`, `tick` |
| Backend register writes | `gb.cpp`: `rWrite`, `immWrite`, `writes` queue |
| Logical row/tick scheduling | `furnace/src/engine/playback.cpp`: `nextTick`, `nextRow`, `processRow` |
| Virtual tempo | `playback.cpp`: `nextTick`, `tempoAccum`, `virtualTempoN/D` |
| Order transitions | `playback.cpp`: `nextOrder`, `nextRow`, `changeOrd/changePos` |
| Compatibility flags | `playback.cpp`, `gb.cpp`: `song.compatFlags.*` |
| Tuning/pitch configuration | `gb.cpp`: `notifyPitchTable`; `DivPitchTable` |

The backend update order is significant:

1. standard macro engine advances;
2. software envelope/arpeggio/pitch/duty/pan/phase-reset changes are applied;
3. WaveSynth and hardware sequence state advance;
4. sweep changes are emitted;
5. frequency/key-on/key-off register writes are emitted;
6. zombie-mode writes are emitted.

The tracer records the resulting intentional writes without replacing this
ordering.
