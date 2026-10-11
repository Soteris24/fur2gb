#!/usr/bin/env python3
"""fur2gb - compile a Furnace Game Boy song into register-write streams for driver/furplayer.

Furnace itself is the compiler: the instrumented Furnace build (tools/furtrace.py) plays the song
and records every register write its GB backend makes, tick by tick. This tool turns that record
into compact bytecode that driver/furplayer replays, so every effect, macro, WaveSynth and compat
flag behaves exactly as in Furnace. The only Furnace state the player models is the GB backend's
anti-click wave rotation, which depends on playback time and so is not periodic across loops; the
model is checked against the position Furnace reports at every wave rewrite.

A song is 5 streams that the player runs in this order every tick, which is the order Furnace
writes in:
    A      writes made while Furnace processes the row (dispatch(): instrument, volume, panning...)
    B0-B3  writes made by DivPlatformGB::tick() for channel 0..3 (it only touches that channel's
           registers, plus NR51 and wave RAM)
Splitting by channel lets repeated passages of each channel compress on their own.

Stream bytecode (one tick of a stream = ops up to and including an END op):
    00-2F vv      write vv to $FF10+op (never NR33/NR34)
    30 vv         write NR33 (the player tracks ch3's period for anti-click)
    31 vv qq rl rh  write NR34; qq/rhrl = anti-click step / and % the new ch3 period
    40-7F         END: end of tick, then (op & 3F) more empty ticks for this stream
    80-81 ii      WAVE: NR30=0, then base wave (op&1)<<8|ii into wave RAM, anti-click rotated
    82            anti-click reset (phase reset macro)
    83            STOP: song ended
    84 cc ll hh nn       CALL: play nn END-units from chunk cc offset hhll, then return
    85 cc ll hh nl nh    CALL with a 16-bit unit count
    86 cc ll hh          JUMP to chunk cc offset hhll
    87 kl kh p0 p1 p2    SETRATE: anti-click step k, tick period p (1/16 cycles)
    88 rr xx nn   ZOMBIE: write xx to $FF00+rr, then nn times 09, 11, 08 (Furnace's zombie-mode
                  volume change, DivPlatformGB::tick() killIt)
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import furtrace  # noqa: E402

CHUNK_SIZE = 16384
MAX_DEPTH = 4          # player call stack size per stream
NSTREAMS = 5           # A, B0..B3
OP_NR33, OP_NR34 = 0x30, 0x31
OP_END = 0x40
OP_WAVE, OP_ACRESET, OP_STOP = 0x80, 0x82, 0x83
CALL8, CALL16, JUMP, SETRATE, ZOMBIE = 0x84, 0x85, 0x86, 0x87, 0x88
NR50_MASK = 0x77       # VIN bits off: SameBoy (Furnace) ignores them, hardware may not


def f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


def anticlick_step(hz: float) -> int:
    """antiClickPeriodCount += (chipClock>>1)/getCurHz(): float32 math, truncated (count < freq)."""
    q = f32(f32(2097152.0) / f32(max(f32(hz), 1.0)))
    k = int(q)  # count + q < 2^24 and count is integral, so trunc(count + q) == count + trunc(q)
    if k + 2048 > 0xFFFF:
        raise SystemExit(f"tick rate {hz} Hz is too slow for the 16-bit anti-click counter")
    return k


def tick_period(hz: float) -> int:
    return round(4194304 * 16 / hz)


def owner(a: int) -> int:
    """Channel whose registers `a` belongs to, -1 for global registers (NR50-NR52)."""
    if 0x10 <= a <= 0x14:
        return 0
    if 0x16 <= a <= 0x19:
        return 1
    if 0x1a <= a <= 0x1e or 0x30 <= a <= 0x3f:
        return 2
    if 0x20 <= a <= 0x23:
        return 3
    return -1


class AntiClick:
    """Mirror of DivPlatformGB's antiClickWavePos/antiClickPeriodCount, driven by register writes."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.pos = 0
        self.cnt = 0
        self.nr33 = 0
        self.nr34 = 0
        self.freq_known = False

    def freq(self):
        return 2048 - (((self.nr34 & 7) << 8) | self.nr33) if self.freq_known else 0

    def tick(self, k):
        f = self.freq()
        if self.enabled and f > 0:
            self.cnt += k
            self.pos = (self.pos + self.cnt // f) & 31
            self.cnt %= f

    def write(self, a, v):
        if a == 0x1d:
            self.nr33 = v
        elif a == 0x1e:
            self.nr34 = v
            self.freq_known = True


class AntiClickMismatch(Exception):
    pass


class NotSettled(Exception):
    pass


def canon_ticks(trace, anticlick_enabled):
    """Convert traced ticks to per-stream op bytes: a list of NSTREAMS-tuples of bytes."""
    ac = AntiClick(anticlick_enabled)
    waves = []
    wave_ids = {}
    out = []
    hz = None
    for ti, t in enumerate(trace.ticks):
        streams = [bytearray() for _ in range(NSTREAMS)]
        if ti == 0:
            ops = list(trace.init) + list(t.ops)
        else:
            ops = t.ops
            if t.hz != hz:
                # the player steps anti-click before a tick's ops: the new rate goes at the very
                # end of the previous tick (stream B3 runs last)
                out[-1][4] += (bytes([SETRATE]) + anticlick_step(t.hz).to_bytes(2, "little")
                               + tick_period(t.hz).to_bytes(3, "little"))
        hz = t.hz
        ac.tick(anticlick_step(hz))
        cur = 0           # stream receiving ops: 0 = A until the "P" marker, then 1 + channel
        in_tick = False
        i = 0
        while i < len(ops):
            op = ops[i]
            if op[0] == "P":
                in_tick = True
                cur = 1
                i += 1
                continue
            if op[0] == "V":
                _, pos, cnt, freq = op
                blk = ops[i + 1:i + 18]
                if (len(blk) != 17 or blk[0] != ("W", 0x1a, 0)
                        or any(blk[1 + k][:2] != ("W", 0x30 + k) for k in range(16))):
                    raise ValueError(f"tick {ti}: unexpected wave update {blk}")
                if (pos & 31, cnt, freq) != (ac.pos, ac.cnt, ac.freq()):
                    raise AntiClickMismatch(f"tick {ti}: Furnace pos/cnt/freq {pos & 31}/{cnt}/{freq}, "
                                            f"model {ac.pos}/{ac.cnt}/{ac.freq()}")
                r = pos & 31
                m = []
                for k in range(16):
                    v = blk[1 + k][2]
                    m += [v >> 4, v & 15]
                n = [m[(j - r) & 31] for j in range(32)]
                even = bytes((n[2 * k] << 4) | n[2 * k + 1] for k in range(16))
                odd = bytes((n[2 * k + 1] << 4) | n[(2 * k + 2) & 31] for k in range(16))
                key = even + odd
                if key not in wave_ids:
                    wave_ids[key] = len(waves)
                    waves.append(key)
                wid = wave_ids[key]
                if wid >= 512:
                    raise SystemExit("more than 512 distinct base waves")
                code = bytes([OP_WAVE | (wid >> 8), wid & 0xff])
                o = 2
                i += 18
            elif op[0] == "Z":
                ac.pos = ac.cnt = 0
                code = bytes([OP_ACRESET])
                o = 2
                i += 1
            else:
                _, a, v = op
                if not 0x10 <= a <= 0x3f:
                    raise ValueError(f"tick {ti}: write to unknown register {a:#x}")
                if 0x30 <= a <= 0x3f:
                    raise ValueError(f"tick {ti}: wave RAM write outside updateWave()")
                if a == 0x24:
                    v &= NR50_MASK
                ac.write(a, v)
                code = bytes([{0x1d: OP_NR33, 0x1e: OP_NR34}.get(a, a - 0x10), v])
                if a == 0x1e:
                    q, r = divmod(anticlick_step(hz), ac.freq())
                    code += bytes([q & 0xff, r & 0xff, r >> 8])
                o = owner(a)
                i += 1
            if in_tick and o >= 0:
                # tick() runs channel 0..3 in order, each writing only its own registers
                if 1 + o < cur:
                    raise SystemExit(f"tick {ti}: channel {o} write after channel {cur - 1} in tick()")
                cur = 1 + o
            streams[cur] += code
        out.append(streams)
    return [tuple(pack_zombie(bytes(s)) for s in t) for t in out], waves


def split_ops(code: bytes):
    """Split a tick's stream bytes (no END) into ops."""
    ops, i = [], 0
    while i < len(code):
        op = code[i]
        n = 5 if op == OP_NR34 else 2 if op < 0x32 or op in (OP_WAVE, OP_WAVE | 1) else 1 if op == OP_ACRESET             else 6 if op == SETRATE else 4 if op == ZOMBIE else None
        if n is None:
            raise ValueError(f"unexpected op {op:#x}")
        ops.append(code[i:i + n])
        i += n
    return ops


def pack_zombie(code: bytes) -> bytes:
    """NRx2 = x followed by k x (09, 11, 08) -> one ZOMBIE op."""
    ops = split_ops(code)
    out = bytearray()
    i = 0
    while i < len(ops):
        op = ops[i]
        if op[0] in (0x02, 0x07, 0x11):
            k = 0
            while (k < 255 and i + 3 * k + 3 < len(ops)
                   and [bytes(o) for o in ops[i + 1 + 3 * k:i + 4 + 3 * k]]
                   == [bytes([op[0], 0x09]), bytes([op[0], 0x11]), bytes([op[0], 0x08])]):
                k += 1
            if k:
                out += bytes([ZOMBIE, 0x10 + op[0], op[1], k])
                i += 1 + 3 * k
                continue
        out += op
        i += 1
    return bytes(out)


def analyse(trace, approx_loop=False):
    """Pick the anti-click model, canonicalise ticks and find the steady loop.
    Returns (ticks, waves, loop_start, anticlick, exact_ticks): the player matches Furnace for
    exact_ticks ticks (None = forever)."""
    try:
        ticks, waves = canon_ticks(trace, True)
        anticlick = True
    except AntiClickMismatch as e:
        first = e
        try:
            ticks, waves = canon_ticks(trace, False)
            anticlick = False
        except AntiClickMismatch:
            raise SystemExit(f"anti-click model does not match Furnace: {first}")

    loops = trace.loop_ticks()
    loop_start = None
    end = len(ticks)
    stops = [i for i, t in enumerate(trace.ticks) if not t.chip_ticked()]
    if stops:
        # FFxx: the song ends on this tick (its row writes still happen, the chip's tick() does
        # not). Furnace may still run one more tick if its audio buffer isn't finished, which
        # depends on buffer boundaries, so the player stops here.
        if loops and loops[0] < stops[0]:
            raise SystemExit(f"song stops at tick {stops[0]} after looping at {loops}")
        return ticks[:stops[0] + 1], waves, None, anticlick, None
    if loops:
        # Find the earliest loop k and the fewest loop passes p such that ticks[loops[k]:loops[k+p]]
        # repeats unchanged until the end of the trace (at least twice). p > 1 when the loop
        # isn't a whole number of ticks (virtual tempo, grooves) and its length alternates.
        n = len(loops) - 1  # complete passes traced
        for p in range(1, n // 2 + 1):
            for k in range(n - 2 * p + 1):
                s0, e0 = loops[k], loops[k + p]
                reps = [(loops[j], loops[j + p]) for j in range(k + p, n - p + 1, p)]
                if reps and all(ticks[a:b] == ticks[s0:e0] for a, b in reps):
                    return ticks[:e0], waves, s0, anticlick, None
        if approx_loop and n >= 1:
            # play intro + first pass exactly, then jump back to the loop start
            return ticks[:loops[1]], waves, loops[0], anticlick, loops[1]
        lengths = sorted({loops[k + 1] - loops[k] for k in range(n)})
        raise NotSettled(f"song does not repeat exactly within {n} loops: one pass is {lengths} ticks"
                         f" (virtual tempo or a groove that doesn't divide evenly into ticks), so"
                         f" Furnace only repeats exactly after many passes. Use --approx-loop to"
                         f" play the intro and first pass exactly and then jump back, or set the"
                         f" song's virtual tempo so one pass is a whole number of ticks")
    return ticks[:end], waves, loop_start, anticlick, None


def build_units(ticks, loop_start):
    """One stream's ticks -> units: a tick's ops + END, absorbing up to 63 following empty ticks.
    Never merges across the loop start."""
    units, loop_unit = [], None
    i = 0
    while i < len(ticks):
        if i == loop_start:
            loop_unit = len(units)
        j = i + 1
        while j < len(ticks) and not ticks[j] and j != loop_start and j - i - 1 < 63:
            j += 1
        units.append(ticks[i] + bytes([OP_END | (j - i - 1)]))
        i = j
    return units, loop_unit


def compress(units, loop_unit, min_gain=16):
    """Greedy LZ over units: items are ('L', unit) or ('C', src_item, nunits, depth).
    A CALL replays already emitted items; it never starts at or crosses the loop start, so the
    loop JUMP can land on a literal unit."""
    items = []
    cover = []                     # units covered per item
    size = []                      # bytes per item
    item_at_unit = {}              # unit index -> item index for literal items
    cands = defaultdict(list)      # (unit[i], unit[i+1]) -> literal item start units
    nunits = len(units)
    usize = [len(u) for u in units]
    i = 0
    while i < nunits:
        best = None
        if i + 1 < nunits and i != loop_unit:
            limit = nunits if loop_unit is None or i >= loop_unit else loop_unit
            for j in reversed(cands.get((units[i], units[i + 1]), [])[-48:]):
                m = 0
                while i + m < limit and j + m < i and units[j + m] == units[i + m]:
                    m += 1
                k = item_at_unit[j]
                acc = got = depth = maxd = 0
                while k < len(items) and acc < m:
                    acc += cover[k]
                    if items[k][0] == "C":
                        maxd = max(maxd, items[k][3])
                    if acc <= m and maxd + 1 <= MAX_DEPTH:
                        got = acc
                        depth = maxd + 1
                    k += 1
                if got < 2:
                    continue
                gain = sum(usize[i:i + got]) - (5 if got < 256 else 6)
                if gain >= min_gain and (best is None or gain > best[0]):
                    best = (gain, j, got, depth)
        if best:
            _, j, got, depth = best
            items.append(("C", item_at_unit[j], got, depth))
            cover.append(got)
            size.append(5 if got < 256 else 6)
            i += got
        else:
            item_at_unit[i] = len(items)
            if i + 1 < nunits:
                cands[(units[i], units[i + 1])].append(i)
            items.append(("L", i, 1, 0))
            cover.append(1)
            size.append(usize[i])
            i += 1
    loop_item = None
    if loop_unit is not None:
        u = 0
        for k, it in enumerate(items):
            if u == loop_unit:
                loop_item = k
                break
            u += cover[k]
        assert loop_item is not None and items[loop_item][0] == "L"
    return items, size, loop_item


def layout(streams, has_loop):
    """Place every stream's items into chunks of CHUNK_SIZE bytes.
    streams: [(units, items, size, loop_item)]. Returns (chunks, entry points)."""
    chunks = [bytearray()]
    entries = []
    tail = 4  # room for a JUMP to the next chunk / the loop
    for units, items, size, loop_item in streams:
        where = []
        for k, it in enumerate(items):
            if len(chunks[-1]) + size[k] + tail > CHUNK_SIZE:
                chunks[-1] += bytes([JUMP, len(chunks), 0x00, 0x00])
                chunks.append(bytearray())
            c = chunks[-1]
            where.append((len(chunks) - 1, len(c)))
            if it[0] == "L":
                c += units[it[1]]
            else:
                sc, addr = where[it[1]]
                if it[2] < 256:
                    c += bytes([CALL8, sc, addr & 0xff, addr >> 8, it[2]])
                else:
                    c += bytes([CALL16, sc, addr & 0xff, addr >> 8, it[2] & 0xff, it[2] >> 8])
        entries.append(where[0])
        if has_loop:
            sc, addr = where[loop_item]
            chunks[-1] += bytes([JUMP, sc, addr & 0xff, addr >> 8])
        else:
            chunks[-1] += bytes([OP_STOP])
    return chunks, entries


class PlayerModel:
    """Executes the bytecode exactly as driver/furplayer does; returns the writes of each tick."""

    def __init__(self, chunks, entries, waves, hz, anticlick):
        self.chunks = chunks
        self.waves = waves
        self.ac = AntiClick(anticlick)
        self.k = anticlick_step(hz)
        self.ctx = [{"pc": e, "stack": [], "idle": 0} for e in entries]
        self.playing = True

    def tick(self):
        w = []
        if not self.playing:
            return w
        self.ac.tick(self.k)
        for c in self.ctx:
            if not self.playing:
                break
            if c["idle"]:
                c["idle"] -= 1
            else:
                self.run(c, w)
        return w

    def run(self, c, w):
        def byte():
            ch, o = c["pc"]
            c["pc"] = (ch, o + 1)
            return self.chunks[ch][o]
        while True:
            op = byte()
            if op < 0x32:
                a = {OP_NR33: 0x1d, OP_NR34: 0x1e}.get(op, 0x10 + op)
                v = byte()
                w.append((a, v))
                self.ac.write(a, v)
                if op == OP_NR34:
                    byte(); byte(); byte()  # precomputed step / freq: the model divides itself
            elif OP_END <= op < 0x80:
                c["idle"] = op & 0x3f
                st = c["stack"]
                for f in st:
                    f[2] -= 1
                while st and st[-1][2] == 0:
                    c["pc"] = tuple(st.pop()[:2])
                return
            elif op in (OP_WAVE, OP_WAVE | 1):
                base = self.waves[((op & 1) << 8) | byte()]
                r = self.ac.pos
                var = base[16:] if r & 1 else base[:16]
                w.append((0x1a, 0))
                w += [(0x30 + i, var[(i + (r >> 1)) & 15]) for i in range(16)]
            elif op == OP_ACRESET:
                self.ac.pos = self.ac.cnt = 0
            elif op == OP_STOP:
                self.playing = False
                return
            elif op in (CALL8, CALL16):
                ch = byte()
                addr = byte() | (byte() << 8)
                n = byte() | ((byte() << 8) if op == CALL16 else 0)
                if len(c["stack"]) >= MAX_DEPTH:
                    raise RuntimeError("call stack overflow")
                c["stack"].append([*c["pc"], n])
                c["pc"] = (ch, addr)
            elif op == JUMP:
                ch = byte()
                c["pc"] = (ch, byte() | (byte() << 8))
            elif op == SETRATE:
                self.k = byte() | (byte() << 8)
                byte(); byte(); byte()
            elif op == ZOMBIE:
                r, x, k = byte(), byte(), byte()
                w.append((r, x))
                w += [(r, v) for _ in range(k) for v in (0x09, 0x11, 0x08)]
            else:
                raise RuntimeError(f"bad opcode {op:#x} at {c['pc']}")


def compared_ticks(trace, exact_ticks=None):
    """Ticks to compare: all of them, up to and including the FFxx stop tick, or the exact part
    of an --approx-loop song."""
    stops = [i for i, t in enumerate(trace.ticks) if not t.chip_ticked()]
    n = stops[0] + 1 if stops else len(trace.ticks)
    return min(n, exact_ticks) if exact_ticks else n


def expected_writes(trace):
    """Furnace's writes per tick (reset writes fold into tick 0), as the player must reproduce them."""
    out = []
    for ti, t in enumerate(trace.ticks):
        ops = (list(trace.init) if ti == 0 else []) + t.ops
        out.append([(o[1], o[2] & NR50_MASK if o[1] == 0x24 else o[2]) for o in ops if o[0] == "W"])
    return out


def verify_model(trace, chunks, entries, waves, anticlick, exact_ticks=None):
    """Run the player model over every traced tick (all loops) and compare with Furnace."""
    exp = expected_writes(trace)[:compared_ticks(trace, exact_ticks)]
    m = PlayerModel(chunks, entries, waves, trace.ticks[0].hz, anticlick)
    for ti, e in enumerate(exp):
        got = m.tick()
        if got != e:
            i = next(i for i in range(max(len(got), len(e)))
                     if (got[i] if i < len(got) else None) != (e[i] if i < len(e) else None))
            raise SystemExit(f"model mismatch at tick {ti} (order {trace.ticks[ti].order:02X} row "
                             f"{trace.ticks[ti].row}), write {i}: player "
                             f"{got[i] if i < len(got) else None}, Furnace {e[i] if i < len(e) else None}")
    return len(exp)


def ident(name):
    s = re.sub(r"[^0-9A-Za-z_]", "_", name)
    return ("_" + s) if s[0].isdigit() else s


def emit_c(out_dir: Path, name: str, song):
    """Write <name>.c/.h and <name>_chunkN.c (autobanked) for a compiled song."""
    chunks, entries, waves, hz, anticlick = (song[k] for k in ("chunks", "entries", "waves", "hz", "anticlick"))
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    wave_chunk = None
    if waves:
        wave_chunk = len(chunks)
        chunks = chunks + [b"".join(waves)]
        if len(chunks[-1]) > CHUNK_SIZE:
            raise SystemExit("wave pool exceeds one bank")
    for k, c in enumerate(chunks):
        sym = f"{name}_chunk{k}"
        p = out_dir / f"{sym}.c"
        lines = ["/* Generated by fur2gb - do not edit */", "#pragma bank 255", "#include <gbdk/platform.h>",
                 "#include <stdint.h>", "", f"BANKREF({sym})", f"const uint8_t {sym}[{len(c)}] = {{"]
        for o in range(0, len(c), 24):
            lines.append("    " + ", ".join(f"0x{b:02X}" for b in c[o:o + 24]) + ",")
        lines.append("};")
        p.write_text("\n".join(lines) + "\n")
        files.append(p)
    lines = ["/* Generated by fur2gb - do not edit */", "#pragma bank 255", "#include \"furplayer.h\"", ""]
    for i in range(len(chunks)):
        lines += [f"BANKREF_EXTERN({name}_chunk{i})", f"extern const uint8_t {name}_chunk{i}[];"]
    lines += ["", f"static const fur_chunk_t {name}_chunks[{len(chunks)}] = {{"]
    for i in range(len(chunks)):
        lines.append(f"    {{ BANK({name}_chunk{i}), {name}_chunk{i} }},")
    lines += ["};", "", f"BANKREF({name})", f"const fur_song_t {name} = {{",
              f"    {len(chunks)}, /* chunks */",
              f"    {wave_chunk if wave_chunk is not None else 0xFF}, /* wave pool chunk */",
              f"    {1 if anticlick else 0}, /* anti-click */",
              f"    {anticlick_step(hz)}, /* anti-click step at {hz:g} Hz */",
              f"    {tick_period(hz)}UL, /* tick period in 1/16 cycles */",
              "    { " + ", ".join(f"{{ {c}, {o} }}" for c, o in entries) + " }, /* stream entry points */",
              f"    {name}_chunks",
              "};", ""]
    p = out_dir / f"{name}.c"
    p.write_text("\n".join(lines))
    files.append(p)
    h = out_dir / f"{name}.h"
    h.write_text(f"/* Generated by fur2gb */\n#pragma once\n#include \"furplayer.h\"\n"
                 f"BANKREF_EXTERN({name})\nextern const fur_song_t {name};\n")
    files.append(h)
    return files


def compile_song(fur: Path, loops: int, subsong=None, furnace=furtrace.DEFAULT_FURNACE, approx_loop=False):
    """Trace, convert and verify against the player model. Returns (trace, song dict)."""
    try:
        furtrace.check_gb_song(fur, furnace)
    except ValueError as e:
        raise SystemExit(str(e))
    while True:
        trace = furtrace.run(fur, loops, furnace=furnace, subsong=subsong)
        try:
            ticks, waves, loop_start, anticlick, exact = analyse(trace, approx_loop and loops >= 64)
            break
        except NotSettled as e:
            if loops >= 64:
                raise SystemExit(str(e))
            loops *= 2
    streams = []
    raw = 0
    for s in range(NSTREAMS):
        units, loop_unit = build_units([t[s] for t in ticks], loop_start)
        items, size, loop_item = compress(units, loop_unit)
        raw += sum(len(u) for u in units)
        streams.append((units, items, size, loop_item))
    chunks, entries = layout(streams, loop_start is not None)
    verified = verify_model(trace, chunks, entries, waves, anticlick, exact)
    song = dict(chunks=chunks, entries=entries, waves=waves, hz=trace.ticks[0].hz, anticlick=anticlick,
                ticks=len(ticks), loop_tick=loop_start, verified=verified, raw=raw, exact_ticks=exact,
                bytes=sum(len(c) for c in chunks), wave_bytes=32 * len(waves),
                hzs=sorted({t.hz for t in trace.ticks}))
    return trace, song


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fur", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("build/songs"), help="output directory")
    ap.add_argument("-n", "--name", help="C symbol name (default: from file name)")
    ap.add_argument("--loops", type=int, default=4, help="loops to trace when looking for the steady loop")
    ap.add_argument("--subsong", type=int)
    ap.add_argument("--furnace", type=Path, default=furtrace.DEFAULT_FURNACE)
    ap.add_argument("--approx-loop", action="store_true",
                    help="for songs that never repeat exactly: exact intro and first pass, then loop")
    a = ap.parse_args()
    name = ident(a.name or a.fur.stem)
    trace, song = compile_song(a.fur, a.loops, a.subsong, a.furnace, a.approx_loop)
    files = emit_c(a.out, name, song)
    loop = f"loops at tick {song['loop_tick']}" if song["loop_tick"] is not None else "no loop (stops)"
    print(f"{name}: {song['ticks']} ticks, {loop}, {song['bytes']} B stream (raw {song['raw']} B) "
          f"+ {song['wave_bytes']} B waves, anti-click {'on' if song['anticlick'] else 'off'}, "
          f"{'/'.join(f'{h:g}' for h in song['hzs'])} Hz; "
          f"player model matches Furnace on all {song['verified']} traced ticks"
          + (" (exact until the first loop, then approximate)" if song["exact_ticks"] else ""))
    for f in files:
        print("  ", f)


if __name__ == "__main__":
    main()
