# Trace granularity findings

The current trace is emitted at the real `DivPlatformGB` write macros
(`rWrite` and `immWrite`) and at each backend `tick()` call.

## Covered directly

The trace is sufficient to observe the ordered values Furnace intentionally
queues for:

* note-trigger frequency and control writes;
* note-off writes;
* volume/envelope register changes;
* duty and noise configuration;
* hardware sweep writes;
* wave selection and wave-RAM updates;
* panning/mixer writes;
* macro, arpeggio, pitch, vibrato, portamento, and slide effects when they
  produce a backend write.

## Not covered as semantic records

Some Furnace state transitions can occur without a GB register write in that
same update. Examples include macro position/release state, internal
portamento targets, pending hardware-sequence waits, and hardware-maintained
envelope/length/sweep evolution after a trigger. The current trace therefore
cannot by itself distinguish “state changed but the backend deliberately
emitted nothing” from “state did not change.”

This is intentional for the first oracle milestone: the stream is a faithful
record of backend intent and write order, not a second model of the APU.
Before conformance fixtures depend on a no-write semantic distinction, the
backend should emit narrowly scoped semantic records at the relevant state
transition sites. It should not dump every internal field.

## Observed output

`MenuLoop.fur` produced 3,606 update records and 92,283 write records with
the current export command. Wave-RAM and channel writes are interleaved in
the backend's actual queue order. Two independent runs produced the same
SHA-256:

`D728D6E58EE9A7816C89897B55AD7EC06F343CF210BD8104DFAC7ADC90F21433`
