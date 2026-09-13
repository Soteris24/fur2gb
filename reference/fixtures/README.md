# Reference fixture corpus

The required fixture names are reserved here so each semantic family gets a
stable trace filename:

`01_note_on`, `02_note_off`, `03_release`, `04_hw_envelope`,
`05_sw_volume_macro`, `06_arp`, `07_pitch_macro`, `08_vibrato`,
`09_portamento`, `10_slide`, `11_volume_slide`, `12_retrigger`,
`13_note_cut`, `14_duty`, `15_noise`, `16_hw_sweep`, `17_wave_macro`,
`18_wavesynth`, `19_panning`, `20_effect_memory`, `21_pattern_break`,
`22_pattern_jump`, `23_groove`, `24_virtual_tempo`, `25_order_loop`,
`26_complex_multichannel`.

The repository currently contains only compressed binary `.fur` modules and
has no supported headless Furnace command for authoring a new module. This
phase does not fabricate `.fur` bytes. The fixtures will be authored through
Furnace's native module writer once the tracer build is available; each will
be committed together with its reference JSONL trace.
