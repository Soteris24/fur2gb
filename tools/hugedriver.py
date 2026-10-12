"""A model of hUGEDriver (the version in hUGETracker 1.0.11): the APU register writes each
hUGE_dosound call makes, in order. Written from hUGEDriver.asm, instruction by instruction where
it matters (the driver's quirks are its behaviour); tests/verify_hugedriver.py checks it against
the real driver running in an emulator.

    drv = HugeDriver(uge.read("song.uge"))
    for _ in range(n):
        writes = drv.tick()        # [(0x10..0x3f, value), ...]

Things the real driver does that depend on what the linker put after its note table (notes out of
the 0..71 range from arpeggios / subpattern offsets) or on code bytes (3xx / 7xx in subpatterns),
and 6xx (calls the song's own assembly routine), can't be known from the song: the model records
them in `undefined` (tick, message) and does something plausible.
"""

from __future__ import annotations

from uge import NOTE_TABLE, LAST_NOTE, NO_NOTE, Song

NO_WAVE = 100

# what reading an APU register returns (DMG): written value | mask
READ_MASK = {
    0x10: 0x80, 0x11: 0x3F, 0x12: 0x00, 0x13: 0xFF, 0x14: 0xBF,
    0x15: 0xFF, 0x16: 0x3F, 0x17: 0x00, 0x18: 0xFF, 0x19: 0xBF,
    0x1A: 0x7F, 0x1B: 0xFF, 0x1C: 0x9F, 0x1D: 0xFF, 0x1E: 0xBF,
    0x1F: 0xFF, 0x20: 0xFF, 0x21: 0x00, 0x22: 0x00, 0x23: 0xBF,
    0x24: 0x00, 0x25: 0x00, 0x26: 0x70,
}

ENV = (0x12, 0x17, 0x1C, 0x21)      # NRx2 of each channel (CH3: NR32)

# APU registers when a DMG's boot ROM hands over (what the driver reads back before writing)
POST_BOOT = {
    0x10: 0x80, 0x11: 0xBF, 0x12: 0xF3, 0x13: 0xFF, 0x14: 0xBF, 0x16: 0x3F, 0x17: 0x00,
    0x18: 0xFF, 0x19: 0xBF, 0x1A: 0x7F, 0x1B: 0xFF, 0x1C: 0x9F, 0x1D: 0xFF, 0x1E: 0xBF,
    0x20: 0xFF, 0x21: 0x00, 0x22: 0x00, 0x23: 0xBF, 0x24: 0x77, 0x25: 0xF3, 0x26: 0xF1,
}


def exported_limits(song):
    """What hUGETracker's export includes (codegen.pas FindUsedStuff, after OptimizeSong merges
    identical patterns): ({0: highest duty, 1: wave, 2: noise instrument}, highest waveform).
    A pattern's instruments only count for the first channel that plays it (a hUGETracker bug):
    an instrument used only on a later channel isn't exported, and the driver reads whatever
    follows the table."""
    first, canon = {}, {}
    for k, pat in song.patterns.items():
        canon[k] = first.setdefault(repr(pat), k)
    hi = {0: -1, 1: -1, 2: -1}
    hi_wave = -1
    for ins in song.wave:
        if ins.subpattern_enabled:
            for c in ins.subpattern:
                if c.effect == 9:
                    hi_wave = max(hi_wave, c.param)
    used = set()
    for ch in range(4):
        typ = (0, 0, 1, 2)[ch]
        for k in song.orders[ch]:
            k = canon.get(k, k)
            if k in used:
                continue
            used.add(k)
            for c in song.patterns.get(k, []):
                if c.effect == 9 and ch == 2:
                    hi_wave = max(hi_wave, c.param)
                if c.instrument == 0:
                    continue
                hi[typ] = max(hi[typ], c.instrument)
                if ch == 2:
                    hi_wave = max(hi_wave, song.wave[c.instrument - 1].waveform)
    return hi, hi_wave


def swap(a):
    return ((a << 4) | (a >> 4)) & 0xFF


def note_poly(note):
    """get_note_poly: NR43 value of a note (A = note id, 8 bits)"""
    a = (~((note + 192) & 0xFF)) & 0xFF
    if a < 7:
        return a
    h = a
    l = ((a >> 2) - 1) & 0xFF
    return ((h & 3) + 4) | swap(l)


class _Chan:
    __slots__ = ("period", "target", "note", "highmask", "table", "table_row")

    def __init__(self):
        self.period = 0       # 16 bits (CH4: only the low byte is the poly)
        self.target = 0       # toneporta target
        self.note = 0
        self.highmask = 0
        self.table = None     # exported subpattern rows, or None
        self.table_row = 0


class HugeDriver:
    def __init__(self, song: Song):
        self.song = song
        self.ticks_per_row = song.ticks_per_row & 0xFF
        self.current_wave = NO_WAVE
        self.counter = 0
        self.tick_no = 0
        self.row_break = 0
        self.next_order = 0
        self.row = 0
        self.order = 0                   # index (the driver keeps index * 2)
        self.order_cnt = (song.order_count * 2) & 0xFF
        self.ch = [_Chan() for _ in range(4)]
        self.step_width4 = 0
        # DMG registers after the boot ROM, then the usual sound setup (NR52, NR51, NR50)
        self.regs = dict(POST_BOOT)
        self.regs.update({0x26: 0x80, 0x25: 0xFF, 0x24: 0x77})
        self.wave_bytes = [(w[2 * i] << 4) | w[2 * i + 1] for w in song.waves for i in range(16)]
        self.exported = exported_limits(song)
        self.calls = 0
        self.undefined = []
        self._w = []

    # -- registers --
    def write(self, addr, val):
        val &= 0xFF
        self.regs[addr] = val
        self._w.append((addr, val))

    def read(self, addr):
        return (self.regs.get(addr, 0) | READ_MASK.get(addr, 0)) & 0xFF

    def _undef(self, msg):
        self.undefined.append((self.calls, msg))

    # -- song data --
    def cell(self, channel):
        pat = self.song.pattern(channel, self.order) if self.order < len(self.song.orders[channel]) else None
        if pat is None:
            self._undef(f"channel {channel + 1} has no pattern in order {self.order}")
            return NO_NOTE, 0, 0, 0
        c = pat[self.row]
        return c.note & 0xFF, c.instrument & 15, c.effect & 15, c.param & 0xFF

    def note_period(self, note):
        """get_note_period: the word at note_table + (2 * note) & 0xFF"""
        idx = ((note * 2) & 0xFF) >> 1
        if idx >= LAST_NOTE:
            self._undef(f"note {note} is out of the note table")
            return 0
        return NOTE_TABLE[idx]

    # -- driver routines --
    def update_channel_freq(self, b, de, h=0):
        de &= 0xFFFF
        if b == 3:
            a = note_poly(de & 0xFF) | self.step_width4
            self.write(0x22, a)
            self.write(0x23, h)
            return
        self.ch[b].period = de
        base = 0x13 + 5 * b
        self.write(base, de & 0xFF)
        self.write(base + 1, (de >> 8) | h)

    def play_note(self, b):
        c = self.ch[b]
        if b in (0, 1):
            base = 0x13 + 5 * b
            self.write(base, c.period & 0xFF)
            self.write(base + 1, c.highmask | ((c.period >> 8) & 0xFF))
        elif b == 2:
            nr51 = self.read(0x25)
            self.write(0x25, nr51 & 0xBB)
            self.write(0x1A, 0x00)
            self.write(0x1A, 0xFF)
            self.write(0x1D, c.period & 0xFF)
            self.write(0x1E, c.highmask | ((c.period >> 8) & 0xFF))
            self.write(0x25, nr51)
        else:
            self.write(0x22, c.period & 0xFF)
            self.write(0x23, c.highmask)

    def update_ch3_waveform(self, a):
        self.current_wave = a
        ofs = swap(a)   # the driver adds swap(id) to the waves pointer: id * 16 for ids 0..15
        if a > self.exported[1] or ofs + 16 > len(self.wave_bytes):
            self._undef(f"waveform {a} isn't in the exported waves")
        data = (self.wave_bytes + [0] * 16)[ofs:ofs + 16]
        nr51 = self.read(0x25)
        self.write(0x25, nr51 & 0xBB)
        self.write(0x1A, 0x00)
        for i in range(16):
            self.write(0x30 + i, data[i])
        self.write(0x1A, 0x80)
        self.write(0x25, nr51)

    def note_cut(self, b):
        self.write(ENV[b], 0)
        if b != 2:
            self.write(ENV[b] + 2, 0xFF)

    def do_effect(self, b, eff, param, from_table=False):
        """Returns False when the effect stops the row's note from playing (tick 0: 3xx, 7xx)"""
        if eff == 0 and param == 0:
            return True
        t = self.tick_no
        z = t == 0
        c = param
        ch = self.ch[b]
        skip = from_table   # the table entry skips the handler's first instruction (ret cc)

        if eff == 0x0:      # arpeggio (runs on every tick)
            d = ch.note
            a = (self.counter - 1) & 0xFF
            a %= 3
            if a == 0:
                a = (c & 15) + d
            elif a == 1:
                a = (swap(c) & 15) + d
            else:
                a = d
            self.update_channel_freq(b, self.note_period(a & 0xFF))
        elif eff == 0x1:    # porta up
            if z and not skip:
                return True
            self.update_channel_freq(b, (ch.period + c) & 0xFFFF)
        elif eff == 0x2:    # porta down
            if z and not skip:
                return True
            self.update_channel_freq(b, (ch.period - c) & 0xFFFF)
        elif eff == 0x3:    # tone portamento
            if skip:
                self._undef("3xx in a subpattern (the driver runs part of an instruction)")
            if z and not skip:
                ch.target = self.note_period(ch.note)
                return False
            de, hl = ch.period, ch.target
            if hl < de:
                de2 = de - c
                if de2 < 0 or (de2 & 0x8000) or de2 < hl:
                    de2 = hl
                de = de2 & 0xFFFF
            elif hl > de:
                de = (de + c) & 0xFFFF
                if hl < de:
                    de = hl
            ch.period = de
            h = ch.highmask
            ch.highmask &= 0x7F
            self.update_channel_freq(b, de, h)
        elif eff == 0x4:    # vibrato
            if z and not skip:
                return True
            e = c >> 4
            base = self.note_period(ch.note)
            if (self.counter & e) == 0:
                self.update_channel_freq(b, (base + (c & 15)) & 0xFFFF)
            else:
                self.update_channel_freq(b, base)
        elif eff == 0x5:    # master volume
            if not z and not skip:
                return True
            self.write(0x24, c)
        elif eff == 0x6:    # call routine
            self._undef(f"6{c:02X} calls the song's routine {c & 15} (assembly code)")
        elif eff == 0x7:    # note delay
            if skip:
                self._undef("7xx in a subpattern (the driver runs part of an instruction)")
                return True
            if z:
                return False
            if t == c:
                self.play_note(b)
        elif eff == 0x8:    # pan
            if not z and not skip:
                return True
            self.write(0x25, c)
        elif eff == 0x9:    # duty / waveform / noise mode
            if not z and not skip:
                return True
            if b == 0:
                self.write(0x11, c)
            elif b == 1:
                self.write(0x16, c)
            elif b == 3:
                self.write(0x22, (self.read(0x22) & ~8 & 0xFF) | c)
            else:
                self.update_ch3_waveform(c)
                self.play_note(2)
        elif eff == 0xA:    # volume slide (a retrigger at a new volume)
            if not z and not skip:
                return True
            down, up = c & 15, c >> 4
            env = ENV[b]
            a = (self.read(env) & 0xF0) >> 4
            a -= down
            if a < 0:
                a = 0
            a += up
            if a >= 16:
                a = 15
            self.write(env, swap(a))
            self.write(env + 2, self.read(env + 2) | 0x80)
            self.play_note(b)
        elif eff == 0xB:    # position jump
            if not z and not skip:
                return True
            if self.row_break == 0:
                self.row_break = 1
            self.next_order = c
        elif eff == 0xC:    # set volume
            if not z and not skip:
                return True
            sc = swap(c)
            if b in (0, 1):
                env = ENV[b]
                self.write(env, (self.read(env) & 0x0F) | sc)
                self.play_note(b)
            elif b == 2:
                if sc >= 0xA0:
                    v = 0x20
                elif sc >= 0x50:
                    v = 0x40
                elif sc == 0:
                    v = 0
                else:
                    v = 0x60
                self.write(0x1C, v)
            else:
                self.write(0x21, sc)
                self.play_note(3)
        elif eff == 0xD:    # pattern break
            if not z and not skip:
                return True
            self.row_break = c
        elif eff == 0xE:    # note cut
            if skip:
                if z:
                    self.note_cut(b)
            elif t == c:
                self.note_cut(b)
        elif eff == 0xF:    # set speed
            if not z and not skip:
                return True
            self.ticks_per_row = c
        return True

    def do_table(self, b):
        ch = self.ch[b]
        row = ch.table_row
        ch.table_row = (row + 1) & 0xFF
        if row >= len(ch.table):
            self._undef(f"channel {b + 1} subpattern reads past its 32 rows")
            cell = ch.table[row % len(ch.table)]
        else:
            cell = ch.table[row]
        jump = cell.volume & 0x1F
        if jump:
            ch.table_row = jump - 1
        note = cell.note & 0x7F
        if note != NO_NOTE:
            n = (ch.note + ((note - 36) & 0xFF)) & 0xFF
            h = ch.highmask & 0x7F
            if b == 3:
                self.update_channel_freq(3, n, h)
            else:
                self.update_channel_freq(b, self.note_period(n), h)
        self.do_effect(b, cell.effect, cell.param, from_table=True)

    def _instrument(self, b, number):
        if number > self.exported[0][(0, 0, 1, 2)[b]]:
            self._undef(f"channel {b + 1} instrument {number} isn't exported (hUGETracker only "
                        f"exports a pattern's instruments for the first channel that plays it)")
        return self.song.instrument(b, number)

    def row_channel(self, b):
        note, ins, eff, par = self.cell(b)
        ch = self.ch[b]
        valid = note < LAST_NOTE
        if b < 3:
            if valid:
                ch.note = note
                period = self.note_period(note)
                if eff != 3:
                    ch.period = period
                if ins == 0:
                    ch.highmask &= 0x7F
                else:
                    i = self._instrument(b, ins)
                    if b == 0:
                        self.write(0x10, i.nr10())
                    if b in (0, 1):
                        self.write(0x11 + 5 * b, i.nrx1())
                        self.write(0x12 + 5 * b, i.envelope())
                    else:
                        self.write(0x1B, i.nr31())
                        self.write(0x1C, i.nr32())
                        if (i.waveform & 0xFF) != self.current_wave:
                            self.update_ch3_waveform(i.waveform & 0xFF)
                    ch.table = i.table()
                    ch.table_row = 0
                    ch.highmask = i.highmask()
        else:
            if valid:
                ch.note = note
                ch.period = note_poly(note)
                if ins == 0:
                    ch.highmask &= 0x7F
                else:
                    i = self._instrument(3, ins)
                    self.write(0x21, i.envelope())
                    ch.table = i.table()
                    ch.table_row = 0
                    byte = i.noise_byte()
                    self.write(0x20, byte & 0x3F)
                    self.step_width4 = swap(byte & 0x80)
                    ch.period |= self.step_width4
                    ch.highmask = (byte & 0x40) | 0x80
        play = self.do_effect(b, eff, par)
        if valid and play:
            self.play_note(b)
        if ch.table:
            self.do_table(b)

    def effects_channel(self, b):
        note, ins, eff, par = self.cell(b)
        if par:
            self.do_effect(b, eff, par)
        if self.ch[b].table:
            self.do_table(b)

    def tick_time(self):
        self.counter = (self.counter + 1) & 0xFF
        self.tick_no = (self.tick_no + 1) & 0xFF
        if (self.ticks_per_row - self.tick_no) & 0xFF:
            return
        self.tick_no = 0
        if self.row_break:
            start_row = self.row_break - 1
            self.row_break = 0
            if self.next_order:
                a = ((self.next_order - 1) * 2) & 0xFF
                self.next_order = 0
                self._set_order(a, start_row)
                return
            self._new_order(start_row)
            return
        row = self.row + 1
        if row != 64:
            self.row = row
            return
        self._new_order(0)

    def _new_order(self, start_row):
        a = (self.order * 2 + 2) & 0xFF
        if a == self.order_cnt:
            a = 0
        self._set_order(a, start_row)

    def _set_order(self, a, start_row):
        if a & 1 or a // 2 >= self.song.order_count:
            self._undef(f"jump to order {a // 2}, past the song's {self.song.order_count} orders")
            a = 0
        self.order = a // 2
        self.row = start_row & 0xFF
        if self.row >= 64:
            self._undef(f"pattern break to row {self.row}")
            self.row &= 63

    def tick(self):
        """One hUGE_dosound call: its APU writes [(addr, val)]"""
        self._w = []
        if self.tick_no == 0:
            for b in range(4):
                self.row_channel(b)
        else:
            for b in range(4):
                self.effects_channel(b)
        self.tick_time()
        self.calls += 1
        return self._w

    def position(self):
        return self.order, self.row, self.tick_no
