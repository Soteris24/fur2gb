"""A small DMG APU model for comparing two register-write streams by what they sound like.

Two drivers can play the same thing with different writes (hUGEDriver cuts a note by turning the
DAC off, Furnace by writing volume 0; Furnace rewrites NRx1 on every note...). This model applies
each tick's writes at the start of the tick, then runs the frame sequencer (envelopes, length,
sweep) until the next tick, and reports per tick what each channel outputs:

    None                                   silent (off, DAC off, volume 0 or level 0)
    ("sq", volume, period, duty, pan)      CH1 / CH2
    ("wave", level, period, wave, pan)     CH3 (wave: the 16 wave RAM bytes)
    ("noise", volume, nr43, pan)           CH4

plus the master volume (NR50). Writes inside one tick count as simultaneous (their order matters
only for what they leave behind), which is what both drivers do: all of a tick's writes take a
few hundred cycles.
"""

from __future__ import annotations

POST_BOOT = {
    0x10: 0x80, 0x11: 0xBF, 0x12: 0xF3, 0x13: 0xFF, 0x14: 0xBF, 0x16: 0x3F, 0x17: 0x00,
    0x18: 0xFF, 0x19: 0xBF, 0x1A: 0x7F, 0x1B: 0xFF, 0x1C: 0x9F, 0x1D: 0xFF, 0x1E: 0xBF,
    0x20: 0xFF, 0x21: 0x00, 0x22: 0x00, 0x23: 0xBF, 0x24: 0x77, 0x25: 0xF3, 0x26: 0xF1,
}
CPU = 4194304


class _Env:
    def __init__(self):
        self.vol = 0
        self.timer = 0
        self.updating = False

    def trigger(self, nrx2):
        self.vol = nrx2 >> 4
        self.timer = nrx2 & 7
        self.updating = True

    def clock(self, nrx2):
        period = nrx2 & 7
        if not period or not self.updating:
            return
        self.timer -= 1
        if self.timer <= 0:
            self.timer = period
            if nrx2 & 8:
                if self.vol < 15:
                    self.vol += 1
                else:
                    self.updating = False
            else:
                if self.vol > 0:
                    self.vol -= 1
                else:
                    self.updating = False

    def zombie(self, old, new):
        """DMG: writing NRx2 while the channel plays (Pan Docs, "zombie mode")"""
        v = self.vol
        if not (old & 7) and self.updating:
            v += 1
        elif not (old & 8):
            v += 2
        if (old ^ new) & 8:
            v = 16 - v
        self.vol = v & 15


class Apu:
    def __init__(self, init=None):
        self.r = dict(POST_BOOT)
        if init:
            self.r.update(init)
        self.wave = [0] * 16
        self.on = [False] * 4
        self.env = [_Env(), _Env(), None, _Env()]
        self.length = [0, 0, 0, 0]
        self.period = [0, 0, 0]
        self.sweep_shadow = 0
        self.sweep_timer = 0
        self.sweep_on = False
        self.fs_step = 0          # frame sequencer (512 Hz)
        self.fs_frac = 0.0        # cycles since the last step

    # -- writes --
    def dac(self, ch):
        if ch == 2:
            return bool(self.r.get(0x1A, 0) & 0x80)
        return bool(self.r.get((0x12, 0x17, 0, 0x21)[ch], 0) & 0xF8)

    def write(self, a, v):
        old = self.r.get(a, 0)
        self.r[a] = v
        if 0x30 <= a <= 0x3F:
            self.wave[a - 0x30] = v
            return
        ch = {0x10: 0, 0x11: 0, 0x12: 0, 0x13: 0, 0x14: 0, 0x16: 1, 0x17: 1, 0x18: 1, 0x19: 1,
              0x1A: 2, 0x1B: 2, 0x1C: 2, 0x1D: 2, 0x1E: 2, 0x20: 3, 0x21: 3, 0x22: 3, 0x23: 3}.get(a)
        if ch is None:
            return
        base = (0x10, 0x15, 0x1A, 0x1F)[ch]
        reg = a - base
        if reg == 1:
            self.length[ch] = (256 if ch == 2 else 64) - (v if ch == 2 else v & 63)
        elif reg == 2 and ch != 2:
            if self.on[ch]:
                self.env[ch].zombie(old, v)
            if not self.dac(ch):
                self.on[ch] = False
        elif reg == 0 and ch == 2:
            if not self.dac(2):
                self.on[2] = False
        elif reg == 4 and v & 0x80:
            self.trigger(ch)

    def trigger(self, ch):
        if self.length[ch] == 0:
            self.length[ch] = 256 if ch == 2 else 64
        if ch != 2:
            self.env[ch].trigger(self.r.get((0x12, 0x17, 0, 0x21)[ch], 0))
        if ch == 0:
            nr10 = self.r.get(0x10, 0)
            self.sweep_shadow = self.freq(0)
            self.sweep_timer = (nr10 >> 4) & 7 or 8
            self.sweep_on = bool(nr10 & 0x70) or bool(nr10 & 7)
            if nr10 & 7 and self._sweep_calc() > 2047:
                self.on[0] = False
                return
        self.on[ch] = self.dac(ch)

    def freq(self, ch):
        lo, hi = ((0x13, 0x14), (0x18, 0x19), (0x1D, 0x1E))[ch]
        return self.r.get(lo, 0) | ((self.r.get(hi, 0) & 7) << 8)

    def _sweep_calc(self):
        nr10 = self.r.get(0x10, 0)
        d = self.sweep_shadow >> (nr10 & 7)
        return self.sweep_shadow - d if nr10 & 8 else self.sweep_shadow + d

    # -- time --
    def _step(self):
        s = self.fs_step
        if s % 2 == 0:      # length, 256 Hz
            for ch in range(4):
                en = self.r.get((0x14, 0x19, 0x1E, 0x23)[ch], 0) & 0x40
                if en and self.length[ch] > 0:
                    self.length[ch] -= 1
                    if self.length[ch] == 0:
                        self.on[ch] = False
        if s in (2, 6) and self.sweep_on:   # sweep, 128 Hz
            nr10 = self.r.get(0x10, 0)
            self.sweep_timer -= 1
            if self.sweep_timer <= 0:
                self.sweep_timer = (nr10 >> 4) & 7 or 8
                if (nr10 >> 4) & 7:
                    n = self._sweep_calc()
                    if n > 2047:
                        self.on[0] = False
                    elif nr10 & 7:
                        self.sweep_shadow = n
                        self.r[0x13] = n & 0xFF
                        self.r[0x14] = (self.r.get(0x14, 0) & ~7) | (n >> 8)
                        if self._sweep_calc() > 2047:
                            self.on[0] = False
        if s == 7:          # envelopes, 64 Hz
            for ch in (0, 1, 3):
                self.env[ch].clock(self.r.get((0x12, 0x17, 0, 0x21)[ch], 0))
        self.fs_step = (s + 1) & 7

    def advance(self, cycles):
        self.fs_frac += cycles
        while self.fs_frac >= CPU / 512:
            self.fs_frac -= CPU / 512
            self._step()

    # -- output --
    def output(self):
        nr51 = self.r.get(0x25, 0)
        out = []
        for ch in range(4):
            pan = ((nr51 >> ch) & 1) | (((nr51 >> (ch + 4)) & 1) << 1)
            if not self.on[ch] or not self.dac(ch) or not pan:
                out.append(None)
                continue
            if ch < 2:
                v = self.env[ch].vol
                duty = self.r.get((0x11, 0x16)[ch], 0) >> 6
                out.append(("sq", v, self.freq(ch), duty, pan) if v else None)
            elif ch == 2:
                level = (self.r.get(0x1C, 0) >> 5) & 3
                out.append(("wave", level, self.freq(2), tuple(self.wave), pan) if level else None)
            else:
                v = self.env[3].vol
                out.append(("noise", v, self.r.get(0x22, 0), pan) if v else None)
        return out, self.r.get(0x24, 0) & 0x77


def play(ticks, hz, init_writes=()):
    """Audible state at each tick of a [[(addr, val), ...]] stream played at hz ticks/s"""
    apu = Apu()
    for a, v in init_writes:
        apu.write(a, v)
    res = []
    for writes in ticks:
        for a, v in writes:
            apu.write(a, v)
        res.append(apu.output())
        apu.advance(CPU / hz)
    return res
