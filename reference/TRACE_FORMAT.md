# Furnace Game Boy reference trace

The trace is emitted by the instrumented `DivPlatformGB` backend, not by a
second Python model.

Each JSONL record is one of:

* `update`: one call to `DivPlatformGB::tick()`, with Furnace's current order
  and row context.
* `write`: one intentional register or wave-RAM write requested by the
  backend, in the exact order in which the backend queued it.

Initialization writes occur before the first `update` record and use
`update: 0`. They are still intentional backend writes and preserve their
relative order.

Write records contain:

* `update`: backend update number
* `channel`: `0` through `3`, or `-1` for global registers
* `register`: symbolic register name
* `address`: Furnace's GB register address
* `value`: resulting byte value
* `write_order`: zero-based order within the update
* `intent`: always `furnace_backend`

This trace does **not** claim to model hardware-maintained envelope, length,
sweep, or oscillator state. Those states evolve after writes inside the GB
APU. The `intent` field distinguishes Furnace decisions from later hardware
behavior. Hardware-state tracing will be added only if a conformance test
requires it.

Enable tracing in an instrumented Furnace build with:

```powershell
$env:FUR2GB_GB_TRACE = "reference\traces\song.jsonl"
```

The normal WAV/Mel/DTW verifier is not an oracle and must not be used to
claim semantic or bit-level equivalence.
