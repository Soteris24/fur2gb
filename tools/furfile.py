"""Write Furnace modules (.fur, format version 230 = Furnace 0.6.8.2) for one Game Boy chip.

Mirrors DivEngine::saveFur (src/engine/fileOps/fur.cpp), DivInstrument::putInsData2 and
DivWavetable::putWaveData of Furnace 0.6.8.2, so the result is what Furnace itself would save.

    song = FurSong(name="...", hz=59.73, speed=6)
    song.instruments.append(FurInstrument(name="lead", env_vol=15, ...))
    song.waves.append([0..15] * 32)
    song.orders = [[0, 1], [0, 1], [0, 0], [0, 0]]       # pattern index per channel per order
    song.patterns[(channel, index)] = [Row(note=..., ins=..., effects=[(0x0F, 6)]), ...]
    write(song, "out.fur")
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

VERSION = 230
SYSTEM_GB = 0x04
CHANNELS = 4
NOTE_OFF = 180

# DivSong() defaults of Furnace 0.6.8.2 (song.h): compatibility flags of a new song
DEFAULT_COMPAT = dict(
    limitSlides=0, linearPitch=2, loopModality=2, properNoiseLayout=1, waveDutyIsVol=0,
    resetMacroOnPorta=0, legacyVolumeSlides=0, compatibleArpeggio=0, noteOffResetsSlides=1,
    targetResetsSlides=1, arpNonPorta=0, algMacroBehavior=0, brokenShortcutSlides=0,
    ignoreDuplicateSlides=0, stopPortaOnNoteOff=0, continuousVibrato=0, brokenDACMode=0,
    oneTickCut=0, newInsTriggersInPorta=1, arp0Reset=1,
    brokenSpeedSel=0, noSlidesOnFirstTick=0, rowResetsArpPos=0, ignoreJumpAtEnd=0,
    buggyPortaAfterSlide=0, gbInsAffectsEnvelope=1, sharedExtStat=1,
    ignoreDACModeOutsideIntendedChannel=0, e1e2AlsoTakePriority=0, newSegaPCM=1, fbPortaPause=0,
    snDutyReset=0, pitchMacroIsLinear=1, pitchSlideSpeed=4, oldOctaveBoundary=0, noOPN2Vol=0,
    newVolumeScaling=1, volMacroLinger=1, brokenOutVol=0, e1e2StopOnSameNote=0,
    brokenPortaArp=0, snNoLowPeriods=0, delayBehavior=2, jumpTreatment=0, autoSystem=1,
    disableSampleMacro=0, brokenOutVol2=0, oldArpStrategy=0,
    brokenPortaLegato=0, brokenFMOff=0, preNoteNoEffect=0, oldDPCM=0, resetArpPhaseOnNewNote=0,
    ceilVolumeScaling=0, oldAlwaysSetVolume=0, oldSampleOffset=0,
)

# macro codes (MA feature)
MACRO_VOL, MACRO_ARP, MACRO_DUTY, MACRO_WAVE, MACRO_PITCH = 0, 1, 2, 3, 4
MACRO_PAN_L, MACRO_PHASE_RESET = 12, 14
# GB hardware sequence commands
HW_ENVELOPE, HW_SWEEP, HW_WAIT, HW_WAIT_REL, HW_LOOP, HW_LOOP_REL = range(6)


@dataclass
class Macro:
    values: list
    loop: int = 255       # 255: no loop
    release: int = 255
    mode: int = 0         # pitch/arp: 0 absolute... see Furnace (arp: fixed bit 0x40000000 in values)
    delay: int = 0
    speed: int = 1


@dataclass
class FurInstrument:
    name: str = ""
    env_vol: int = 15
    env_dir: int = 0          # 1: up
    env_len: int = 0
    sound_len: int = 64       # 64: no length
    always_init: bool = False
    soft_env: bool = False
    double_wave: bool = False
    hw_seq: list = field(default_factory=list)      # [(cmd, data16)]
    macros: dict = field(default_factory=dict)      # code -> Macro


@dataclass
class Row:
    note: int = -1            # 0..179 (0 = C-(-5)), NOTE_OFF, -1 empty
    ins: int = -1
    vol: int = -1
    effects: list = field(default_factory=list)     # [(code or -1, value or -1)]


@dataclass
class FurSong:
    name: str = ""
    author: str = ""
    comment: str = ""
    album: str = ""
    system_name: str = "Game Boy"
    hz: float = 60.0
    speed: int = 6
    pattern_len: int = 64
    highlight: tuple = (4, 16)
    tuning: float = 440.0
    chip_flags: dict = field(default_factory=lambda: {"chipType": 0})
    compat: dict = field(default_factory=dict)      # overrides of DEFAULT_COMPAT
    instruments: list = field(default_factory=list)
    waves: list = field(default_factory=list)       # each: list of 0..15 (width 32)
    orders: list = field(default_factory=lambda: [[0] for _ in range(CHANNELS)])
    patterns: dict = field(default_factory=dict)    # (channel, index) -> [Row] * pattern_len
    effect_cols: list = field(default_factory=lambda: [1] * CHANNELS)
    channel_names: list = field(default_factory=lambda: [""] * CHANNELS)


class _W:
    def __init__(self):
        self.b = bytearray()

    def tell(self):
        return len(self.b)

    def c(self, v):
        self.b += struct.pack("<B", v & 0xFF)

    def s(self, v):
        self.b += struct.pack("<H", v & 0xFFFF)

    def i(self, v):
        self.b += struct.pack("<I", v & 0xFFFFFFFF)

    def f(self, v):
        self.b += struct.pack("<f", v)

    def raw(self, data):
        self.b += data

    def string(self, s):
        self.b += s.encode("utf-8") + b"\0"

    def put_i(self, at, v):
        self.b[at:at + 4] = struct.pack("<I", v & 0xFFFFFFFF)

    def put_s(self, at, v):
        self.b[at:at + 2] = struct.pack("<H", v & 0xFFFF)

    def block(self, magic):
        self.raw(magic)
        at = self.tell()
        self.i(0)
        return at

    def end_block(self, at):
        self.put_i(at, self.tell() - at - 4)


def _macro(w: _W, code: int, m: Macro):
    vals = m.values
    if not vals:
        return
    lo, hi = min(vals), max(vals)
    if lo >= 0 and hi <= 255:
        ws = 0
    elif lo >= -128 and hi <= 127:
        ws = 64
    elif lo >= -32768 and hi <= 32767:
        ws = 128
    else:
        ws = 192
    w.c(code & 31)
    w.c(len(vals))
    w.c(m.loop)
    w.c(m.release)
    w.c(m.mode)
    w.c(ws)
    w.c(m.delay)
    w.c(m.speed)
    for v in vals:
        if ws == 0 or ws == 64:
            w.c(v)
        elif ws == 128:
            w.s(v)
        else:
            w.i(v)


def _feature(w: _W, code: bytes, body):
    w.raw(code)
    at = w.tell()
    w.s(0)
    body()
    w.put_s(at, w.tell() - at - 2)


def _instrument(w: _W, ins: FurInstrument):
    at = w.block(b"INS2")
    w.s(VERSION)
    w.c(2)          # DIV_INS_GB
    w.c(0)
    if ins.name:
        _feature(w, b"NA", lambda: w.string(ins.name))

    def gb():
        w.c(((ins.env_len & 7) << 5) | (16 if ins.env_dir else 0) | (ins.env_vol & 15))
        w.c(ins.sound_len)
        w.c((4 if ins.double_wave else 0) | (2 if ins.always_init else 0) | (1 if ins.soft_env else 0))
        w.c(len(ins.hw_seq))
        for cmd, data in ins.hw_seq:
            w.c(cmd)
            w.s(data)
    _feature(w, b"GB", gb)

    if ins.macros:
        def ma():
            w.s(8)
            for code in sorted(ins.macros):
                _macro(w, code, ins.macros[code])
            w.c(0xFF)
        _feature(w, b"MA", ma)
    w.raw(b"EN")
    w.end_block(at)


def _wave(w: _W, data):
    at = w.block(b"WAVE")
    w.c(0)
    w.i(len(data))
    w.i(0)
    w.i(15)
    for v in data:
        w.i(v)
    w.end_block(at)


def _pattern(w: _W, chan, index, rows, cols, plen):
    at = w.block(b"PATN")
    w.c(0)
    w.c(chan)
    w.s(index)
    w.string("")
    empty = 0
    for j in range(plen):
        r = rows[j] if j < len(rows) else Row()
        mask = 0
        emask = 0
        fx = list(r.effects) + [(-1, -1)] * (cols - len(r.effects))
        if r.note >= 0:
            mask |= 1
        if r.ins >= 0:
            mask |= 2
        if r.vol >= 0:
            mask |= 4
        for k in range(cols):
            code, val = fx[k]
            if k == 0:
                if code >= 0:
                    mask |= 8
                if val >= 0:
                    mask |= 16
            elif k < 4:
                if code >= 0 or val >= 0:
                    mask |= 32
            else:
                if code >= 0 or val >= 0:
                    mask |= 64
            if code >= 0:
                emask |= 1 << (2 * k)
            if val >= 0:
                emask |= 2 << (2 * k)
        if mask == 0:
            empty += 1
            if empty > 127:
                w.c(128 | (empty - 2))
                empty = 0
            continue
        if empty > 1:
            w.c(128 | (empty - 2))
        elif empty:
            w.c(0)
        empty = 0
        w.c(mask)
        if mask & 32:
            w.c(emask & 0xFF)
        if mask & 64:
            w.c(emask >> 8)
        if mask & 1:
            w.c(r.note)
        if mask & 2:
            w.c(r.ins)
        if mask & 4:
            w.c(r.vol)
        if mask & 8:
            w.c(fx[0][0])
        if mask & 16:
            w.c(fx[0][1])
        for k in range(1, cols):
            code, val = fx[k]
            if (k < 4 and mask & 32) or (k >= 4 and mask & 64):
                if code >= 0:
                    w.c(code)
                if val >= 0:
                    w.c(val)
    w.c(0xFF)
    w.end_block(at)


def to_bytes(song: FurSong, compress=True) -> bytes:
    compat = dict(DEFAULT_COMPAT)
    compat.update(song.compat)
    ord_len = max(len(o) for o in song.orders)
    if ord_len > 256:
        raise ValueError("Furnace songs have at most 256 orders")
    for ch in range(CHANNELS):
        if any(not 0 <= p <= 255 for p in song.orders[ch]):
            raise ValueError("pattern indexes are 0..255")
    if len(song.instruments) > 256 or len(song.waves) > 256:
        raise ValueError("at most 256 instruments and wavetables")
    pats = []
    for ch in range(CHANNELS):
        seen = set()
        for p in song.orders[ch]:
            if p not in seen:
                seen.add(p)
                pats.append((ch, p))

    w = _W()
    w.raw(b"-Furnace module-")
    w.s(VERSION)
    w.s(0)
    w.i(32)
    w.i(0)
    w.i(0)

    info = w.block(b"INFO")
    w.c(0)                      # time base
    w.c(song.speed)
    w.c(song.speed)
    w.c(1)                      # arp speed
    w.f(song.hz)
    w.s(song.pattern_len)
    w.s(ord_len)
    w.c(song.highlight[0])
    w.c(song.highlight[1])
    w.s(len(song.instruments))
    w.s(len(song.waves))
    w.s(0)
    w.i(len(pats))
    for i in range(32):
        w.c(SYSTEM_GB if i == 0 else 0)
    for i in range(32):
        w.c(64)                 # systemVol 1.0 * 64
    for i in range(32):
        w.c(0)                  # systemPan 0
    flag_ptr = w.tell()
    for i in range(32):
        w.i(0)
    w.string(song.name)
    w.string(song.author)
    w.f(song.tuning)
    for k in ("limitSlides", "linearPitch", "loopModality", "properNoiseLayout", "waveDutyIsVol",
              "resetMacroOnPorta", "legacyVolumeSlides", "compatibleArpeggio", "noteOffResetsSlides",
              "targetResetsSlides", "arpNonPorta", "algMacroBehavior", "brokenShortcutSlides",
              "ignoreDuplicateSlides", "stopPortaOnNoteOff", "continuousVibrato", "brokenDACMode",
              "oneTickCut", "newInsTriggersInPorta", "arp0Reset"):
        w.c(compat[k])
    ptrs = w.tell()
    for _ in range(len(song.instruments) + len(song.waves) + len(pats)):
        w.i(0)
    for ch in range(CHANNELS):
        o = song.orders[ch]
        for j in range(ord_len):
            w.c(o[j] if j < len(o) else 0)
    for ch in range(CHANNELS):
        w.c(song.effect_cols[ch])
    for ch in range(CHANNELS):
        w.c(1 | 2)              # shown, shown in the oscilloscope
    for ch in range(CHANNELS):
        w.c(0)
    for ch in range(CHANNELS):
        w.string(song.channel_names[ch])
    for ch in range(CHANNELS):
        w.string("")
    w.string(song.comment)
    w.f(1.0)                    # master volume
    for k in ("brokenSpeedSel", "noSlidesOnFirstTick", "rowResetsArpPos", "ignoreJumpAtEnd",
              "buggyPortaAfterSlide", "gbInsAffectsEnvelope", "sharedExtStat",
              "ignoreDACModeOutsideIntendedChannel", "e1e2AlsoTakePriority", "newSegaPCM",
              "fbPortaPause", "snDutyReset", "pitchMacroIsLinear", "pitchSlideSpeed",
              "oldOctaveBoundary", "noOPN2Vol", "newVolumeScaling", "volMacroLinger", "brokenOutVol",
              "e1e2StopOnSameNote", "brokenPortaArp", "snNoLowPeriods", "delayBehavior",
              "jumpTreatment", "autoSystem", "disableSampleMacro", "brokenOutVol2", "oldArpStrategy"):
        w.c(compat[k])
    w.s(150)                    # virtual tempo
    w.s(150)
    w.string("")                # first subsong name, comment
    w.string("")
    w.c(0)                      # no additional subsongs
    w.c(0)
    w.c(0)
    w.c(0)
    w.string(song.system_name)
    w.string(song.album)
    for _ in range(4):
        w.string("")
    w.f(1.0)                    # chip volume, panning, front/rear
    w.f(0.0)
    w.f(0.0)
    w.i(0)                      # patchbay
    w.c(1)                      # automatic patchbay
    for k in ("brokenPortaLegato", "brokenFMOff", "preNoteNoEffect", "oldDPCM",
              "resetArpPhaseOnNewNote", "ceilVolumeScaling", "oldAlwaysSetVolume", "oldSampleOffset"):
        w.c(compat[k])
    w.c(1)                      # speed pattern: one speed
    for i in range(16):
        w.c(song.speed if i == 0 else 0)
    w.c(0)                      # no grooves
    adir_ptr = w.tell()
    w.i(0)
    w.i(0)
    w.i(0)
    w.end_block(info)

    flags = "".join(f"{k}={str(v).lower() if isinstance(v, bool) else v}\n" for k, v in song.chip_flags.items())
    flag_at = 0
    if flags:
        flag_at = w.tell()
        at = w.block(b"FLAG")
        w.string(flags)
        w.end_block(at)
    w.put_i(flag_ptr, flag_at)

    adirs = []
    for _ in range(3):
        adirs.append(w.tell())
        at = w.block(b"ADIR")
        w.i(0)
        w.end_block(at)
    for k, a in enumerate(adirs):
        w.put_i(adir_ptr + 4 * k, a)

    table = []
    for ins in song.instruments:
        table.append(w.tell())
        _instrument(w, ins)
    for data in song.waves:
        table.append(w.tell())
        _wave(w, data)
    for ch, p in pats:
        table.append(w.tell())
        _pattern(w, ch, p, song.patterns.get((ch, p), []), song.effect_cols[ch], song.pattern_len)
    for k, a in enumerate(table):
        w.put_i(ptrs + 4 * k, a)

    data = bytes(w.b)
    return zlib.compress(data) if compress else data


def write(song: FurSong, path, compress=True):
    Path(path).write_bytes(to_bytes(song, compress))
