"""Read hUGETracker .uge modules (format versions 0 to 6) into the data hUGEDriver plays.

Follows hUGETracker's own reader (src/song.pas: the packed Free Pascal records, and the upgrade of
old versions to version 6) and its exporter (src/codegen.pas, instruments.pas: what the driver
gets), so a Song here is what the driver plays after "export":

  - orders: hUGETracker keeps one extra entry at the end of each channel's order list, which the
    export drops; order_cnt is the longest list minus that entry
  - instruments: 15 per channel type, as the bytes the driver reads (NRx registers + highmask)
  - subpatterns ("tables"): their first 32 rows; a row's jump is 1 + the row to jump to (0: none),
    and the last row jumps back to row 0 unless it has its own jump
  - main pattern cells: instrument 0..15 (others export as 0), the volume column is not exported
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

NO_NOTE = 90
LAST_NOTE = 72      # notes 0..71: C-3..B-8
NUM_ROWS = 64
TABLE_ROWS = 32

# include/hUGE_note_table.inc: NRx3/NRx4 period of each note
NOTE_TABLE = [
    44, 156, 262, 363, 457, 547, 631, 710, 786, 854, 923, 986,
    1046, 1102, 1155, 1205, 1253, 1297, 1339, 1379, 1417, 1452, 1486, 1517,
    1546, 1575, 1602, 1627, 1650, 1673, 1694, 1714, 1732, 1750, 1767, 1783,
    1798, 1812, 1825, 1837, 1849, 1860, 1871, 1881, 1890, 1899, 1907, 1915,
    1923, 1930, 1936, 1943, 1949, 1954, 1959, 1964, 1969, 1974, 1978, 1982,
    1985, 1988, 1992, 1995, 1998, 2001, 2004, 2006, 2009, 2011, 2013, 2015,
]

SQUARE, WAVE, NOISE = 0, 1, 2
CHANNEL_TYPE = (SQUARE, SQUARE, WAVE, NOISE)


class UgeError(ValueError):
    pass


@dataclass
class Cell:
    note: int = NO_NOTE     # 0..71, or NO_NOTE (anything >= LAST_NOTE plays nothing)
    instrument: int = 0     # 1..15, 0: none
    volume: int = 0         # subpatterns: jump (1 + target row, 0: none); unused in patterns
    effect: int = 0         # 0..15
    param: int = 0          # 0..255

    def is_empty(self):
        return self.note == NO_NOTE and not self.instrument and not self.effect and not self.param


@dataclass
class Instrument:
    type: int
    name: str = ""
    length: int = 0
    length_enabled: bool = False
    initial_volume: int = 15
    vol_sweep_up: bool = False          # envelope direction
    vol_sweep_amount: int = 0
    sweep_time: int = 0                 # NR10 (CH1 only)
    sweep_down: bool = True
    sweep_shift: int = 0
    duty: int = 2
    output_level: int = 1               # NR32 level field: 0 mute, 1 100%, 2 50%, 3 25%
    waveform: int = 0
    seven_bit: bool = False             # noise counter step
    subpattern_enabled: bool = False
    subpattern: list = field(default_factory=lambda: [Cell() for _ in range(NUM_ROWS)])

    # the bytes hUGEDriver reads (codegen.pas, instruments.pas)
    def nr10(self):
        return ((self.sweep_time & 7) << 4) | (int(self.sweep_down) << 3) | (self.sweep_shift & 7)

    def nrx1(self):
        return ((self.duty & 3) << 6) | (self.length & 63)

    def envelope(self):
        return ((self.initial_volume & 15) << 4) | (int(self.vol_sweep_up) << 3) | (self.vol_sweep_amount & 7)

    def highmask(self):
        return 0x80 | (int(self.length_enabled) << 6)

    def nr31(self):
        return self.length & 255

    def nr32(self):
        return (self.output_level & 3) << 5

    def noise_byte(self):
        """CH4 instrument's last byte: NR41 length, bit 6 length enabled, bit 7 7-bit counter"""
        return (self.length & 63) | (int(self.length_enabled) << 6) | (int(self.seven_bit) << 7)

    def table(self):
        """The exported subpattern (32 rows, jumps as the driver reads them), or None"""
        if not self.subpattern_enabled:
            return None
        rows = []
        for i in range(TABLE_ROWS):
            c = self.subpattern[i]
            jump = min(max(c.volume, 0), 32)
            if i == TABLE_ROWS - 1 and c.volume == 0:
                jump = 1
            rows.append(Cell(c.note, 0, jump, c.effect & 15, c.param & 255))
        return rows


@dataclass
class Song:
    version: int
    name: str
    artist: str
    comment: str
    duty: list         # 15 Instrument (index 0 = instrument 1)
    wave: list
    noise: list
    waves: list        # 16 x 32 samples
    ticks_per_row: int
    timer_enabled: bool
    timer_divider: int
    patterns: dict     # key -> 64 Cell
    orders: list       # 4 lists of pattern keys, as exported (one entry fewer than in the file)
    routines: list

    @property
    def order_count(self):
        return max(len(o) for o in self.orders)

    def instrument(self, channel, number):
        """The instrument a pattern cell on this channel selects (1..15)"""
        bank = (self.duty, self.duty, self.wave, self.noise)[channel]
        return bank[number - 1]

    def pattern(self, channel, order):
        """The 64 cells a channel plays in this order"""
        return self.patterns.get(self.orders[channel][order]) or [Cell() for _ in range(NUM_ROWS)]

    def tick_rate(self):
        """hUGEDriver calls per second as hUGETracker plays it: the timer (4096 Hz / (256 - TMA))
        or VBlank (59.73 Hz)"""
        if self.timer_enabled:
            return 4096 / (256 - (self.timer_divider & 255))
        return 4194304 / 70224


class _Reader:
    def __init__(self, data: bytes):
        self.d = data
        self.o = 0

    def take(self, n):
        if self.o + n > len(self.d):
            raise UgeError("file ends early (not a .uge, or damaged)")
        b = self.d[self.o:self.o + n]
        self.o += n
        return b

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def u8(self):
        return self.take(1)[0]

    def s8(self):
        return struct.unpack("<b", self.take(1))[0]

    def boolean(self):
        return self.u8() != 0

    def shortstring(self):
        b = self.take(256)
        return b[1:1 + b[0]].decode("latin-1")

    def ansistring(self):
        n = self.i32()
        if n < 0 or n > len(self.d) - self.o:
            raise UgeError("bad routine string")
        return self.take(n).decode("latin-1")


def _cell(r: _Reader, with_volume: bool) -> Cell:
    note = r.i32()
    ins = r.i32()
    vol = r.i32() if with_volume else 0
    eff = r.i32()
    par = r.u8()
    return Cell(note, ins, vol, eff, par)


def _instrument_v1(r: _Reader, macro: bool):
    """TInstrumentV1 (304 bytes) or TInstrumentV2 (+ 6 byte noise macro)"""
    t = r.i32()
    ins = Instrument(t if t in (0, 1, 2) else 0)
    ins.name = r.shortstring()
    ins.length = r.i32()
    ins.length_enabled = r.boolean()
    ins.initial_volume = r.u8()
    ins.vol_sweep_up = r.i32() == 0       # TSweepType: stUp = 0, stDown = 1
    ins.vol_sweep_amount = r.u8()
    ins.sweep_time = r.i32()
    ins.sweep_down = r.i32() != 0
    ins.sweep_shift = r.i32()
    ins.duty = r.u8()
    ins.output_level = r.i32()
    ins.waveform = r.i32()
    r.i32()                                # ShiftClockFreq (unused)
    ins.seven_bit = r.i32() != 0           # TStepWidth: swFifteen = 0, swSeven = 1
    r.i32()                                # DividingRatio (unused)
    noise_macro = [r.s8() for _ in range(6)] if macro else None
    return ins, noise_macro


def _instrument_v3(r: _Reader):
    """TInstrumentV3 (1385 bytes)"""
    t = r.i32()
    ins = Instrument(t if t in (0, 1, 2) else 0)
    ins.name = r.shortstring()
    ins.length = r.i32()
    ins.length_enabled = r.boolean()
    ins.initial_volume = r.u8()
    ins.vol_sweep_up = r.i32() == 0
    ins.vol_sweep_amount = r.u8()
    ins.sweep_time = r.i32()
    ins.sweep_down = r.i32() != 0
    ins.sweep_shift = r.i32()
    ins.duty = r.u8()
    ins.output_level = r.i32()
    ins.waveform = r.i32()
    ins.seven_bit = r.i32() != 0
    ins.subpattern_enabled = r.boolean()
    ins.subpattern = [_cell(r, True) for _ in range(NUM_ROWS)]
    return ins


def _default_bank(t):
    """UpgradeSong(TSongV2): defaults of the instruments a v1/v2 song didn't have"""
    bank = []
    for i in range(15):
        ins = Instrument(t)
        if t == WAVE:
            ins.waveform = i
        bank.append(ins)
    return bank


def _noise_note_v3(note, ins: Instrument):
    """UpgradeSong(TSongV3): a CH4 note's code in version 4 (noise notes changed meaning)"""
    if note >= LAST_NOTE:
        return note
    shift = (15 - (NOTE_TABLE[note] >> 7)) & 15
    freq = 524288 >> shift   # dividing ratio 0 (0.5): 524288 / 0.5 / 2^(shift + 1)
    return _CH4_FREQ_CODES.get(freq, note)


# constants.pas Ch4FreqToNoteCodeMap (the entries a dividing ratio of 0 can produce)
_CH4_FREQ_CODES = {524288: 63, 262144: 62, 131072: 61, 65536: 59, 32768: 55, 16384: 51, 8192: 47,
                   4096: 43, 2048: 39, 1024: 35, 512: 31, 256: 27, 128: 23, 64: 19, 32: 15, 16: 11}


def read(path) -> Song:
    data = Path(path).read_bytes()
    r = _Reader(data)
    version = r.i32()
    if version not in range(0, 7):
        raise UgeError(f"{Path(path).name}: .uge format version {version} is not supported (0 to 6)")
    name, artist, comment = r.shortstring(), r.shortstring(), r.shortstring()

    noise_macros = None
    if version <= 2:
        typed = [_instrument_v1(r, False)[0] for _ in range(15)]
        duty, wave, noise = _default_bank(SQUARE), _default_bank(WAVE), _default_bank(NOISE)
        for i, ins in enumerate(typed):
            (duty, wave, noise)[ins.type][i] = ins
    elif version <= 5:
        insts = [_instrument_v1(r, version >= 4) for _ in range(45)]
        duty = [i for i, _ in insts[0:15]]
        wave = [i for i, _ in insts[15:30]]
        noise = [i for i, _ in insts[30:45]]
        noise_macros = [m for _, m in insts[30:45]] if version >= 4 else None
    else:
        insts = [_instrument_v3(r) for _ in range(45)]
        duty, wave, noise = insts[0:15], insts[15:30], insts[30:45]

    wave_size = 33 if version <= 2 else 32
    waves = []
    for _ in range(16):
        w = r.take(wave_size)
        waves.append([b & 15 for b in w[:32]])

    ticks = r.i32()
    timer_enabled, timer_divider = False, 0
    if version >= 6:
        timer_enabled = r.boolean()
        timer_divider = r.i32()

    patterns = {}
    n = r.i32()
    if n < 0 or n > 100000:
        raise UgeError("bad pattern count")
    for i in range(n):
        key = r.i32() if version >= 5 else i
        patterns[key] = [_cell(r, version >= 6) for _ in range(NUM_ROWS)]

    orders = []
    for ch in range(4):
        n = r.i32()
        if n < 0 or n > 100000:
            raise UgeError("bad order list")
        orders.append([r.i32() for _ in range(n)])

    routines = []
    if version >= 2:
        for _ in range(16):
            routines.append(r.ansistring() if r.o < len(data) else "")

    if version <= 3:
        # version 4 changed CH4 note codes: convert the patterns CH4 plays
        ch4 = set(orders[3][:-1])
        for key in ch4:
            for c in patterns.get(key, []):
                if c.instrument and c.note != NO_NOTE and 1 <= c.instrument <= 15:
                    c.note = _noise_note_v3(c.note, noise[c.instrument - 1])
    if version <= 5:
        # version 6: subpatterns; a v4/v5 noise macro becomes the noise instrument's subpattern
        for i, ins in enumerate(noise):
            ins.subpattern = [Cell() for _ in range(NUM_ROWS)]
            if noise_macros:
                m = noise_macros[i]
                for j in range(6):
                    ins.subpattern[j + 1].note = m[j] + 36
                wrap = min(ticks, 7)
                ins.subpattern[wrap - 1].volume = wrap
                ins.subpattern_enabled = any(m)
        for bank in (duty, wave):
            for ins in bank:
                ins.subpattern_enabled = False
                ins.subpattern = [Cell() for _ in range(NUM_ROWS)]

    # export: instruments 1..15 only in patterns, orders without their last entry
    for pat in patterns.values():
        for c in pat:
            if not 0 <= c.instrument <= 15:
                c.instrument = 0
            c.effect &= 15
            c.param &= 255
    exported = [o[:-1] for o in orders]
    if max(len(o) for o in exported) == 0:
        raise UgeError(f"{Path(path).name}: the song has no orders")
    for ch, o in enumerate(exported):
        for k in o:
            if k not in patterns:
                patterns[k] = [Cell() for _ in range(NUM_ROWS)]

    return Song(version, name, artist, comment, duty, wave, noise, waves, ticks, timer_enabled,
                timer_divider, patterns, exported, routines)
