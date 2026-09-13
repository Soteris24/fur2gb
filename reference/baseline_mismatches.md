# Baseline mismatch report

This report is intentionally recorded before modifying the existing converter
or driver. It describes divergences already proven by source inspection; the
JSONL runner must be used to attach exact record numbers once an instrumented
Furnace build is available.

## Categories

* **A — Parser information loss:** hardware sequences, release-aware macros,
  pitch/panning/phase macros, raw noise notes, compatibility flags, tuning,
  chip model, and additional subsongs are discarded.
* **B — IR representation:** the generated song has no hardware-sequence,
  release, macro-rate, macro-mask, raw-frequency, or pattern-control fields.
* **C — Event semantics:** the driver treats effects as a custom modal subset
  rather than Furnace dispatch commands and standard macro state.
* **D — Tick timing:** the driver stores tick rate but schedules updates using
  a separate VBlank assumption and global pattern length.
* **E — Register calculation:** pulse/wave frequency writes do not visibly
  perform Furnace's `2048 - freq` register conversion; noise supports only a
  truncated note table.
* **F — Write ordering:** the driver has no traceable write-order contract and
  writes wave RAM through a different lifecycle.
* **G — Hardware behavior:** envelope, length, sweep, zombie mode, and wave
  hardware evolution are not represented by the current driver state.
* **H — Unsupported features:** pattern jump/break, hardware sequences,
  release, raw noise, pitch/panning/phase macros, and model-specific behavior
  are silently absent.

No driver fix is included in this phase.
