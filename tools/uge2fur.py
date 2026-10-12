#!/usr/bin/env python3
"""uge2fur: convert a hUGETracker module (.uge) to a Furnace module (.fur) for the Game Boy.

    python tools/uge2fur.py song.uge [-o song.fur] [--no-verify]

The .fur keeps the song's structure, so it stays editable in Furnace: the same orders and
patterns, one Furnace instrument per hUGETracker instrument (subpatterns become macros), the same
waves, speed and tick rate. hUGETracker effects become their Furnace equivalents.

hUGEDriver and Furnace don't work the same way, so the converter plays the song with a model of
hUGEDriver (tools/hugedriver.py, checked against the real driver) to know what happens on every
row, and writes what Furnace needs to do the same thing: a stop for effects that only last one
row in hUGETracker but continue in Furnace, legato for notes without an instrument (hUGEDriver
doesn't retrigger those), fine pitch for the few notes hUGEDriver's note table has 1-2 units off
equal temperament, and so on.

Then it checks the result: it renders the .fur with Furnace (fur2gb's traced build, when
available) and compares what each channel outputs on every tick with what hUGEDriver outputs
(tools/apumodel.py), and reports the ticks that differ and why. Some hUGEDriver behaviours have no
Furnace equivalent (its square-wave vibrato, its arpeggio phase that follows a global tick counter,
NR50 master volume, routines...): those get the closest Furnace effect and show up in the report.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import uge  # noqa: E402
import hugedriver  # noqa: E402
import furfile as F  # noqa: E402
import apumodel  # noqa: E402

NOTE_OFS = 84   # hUGETracker C-3 (note 0) is Furnace note 84 (C-2)
# hUGEDriver note table minus Furnace's period for the same note (A-4 = 440 Hz, non-linear pitch)
FINE = {1: -1, 2: -1, 7: -1, 9: -2, 24: -1, 61: -1}
WAVE_VOL = {0: 0, 1: 15, 2: 8, 3: 4}   # NR32 level field -> Furnace wave channel volume

# Furnace's GB noise table (gb.cpp noiseTable): a noise note k plays NR43 = NOISE_TABLE[k - 60]
NOISE_TABLE = [0, 0xf7, 0xf6, 0xf5, 0xf4, 0xe7, 0xe6, 0xe5, 0xe4, 0xd7, 0xd6, 0xd5, 0xd4,
               0xc7, 0xc6, 0xc5, 0xc4, 0xb7, 0xb6, 0xb5, 0xb4, 0xa7, 0xa6, 0xa5, 0xa4,
               0x97, 0x96, 0x95, 0x94, 0x87, 0x86, 0x85, 0x84, 0x77, 0x76, 0x75, 0x74,
               0x67, 0x66, 0x65, 0x64, 0x57, 0x56, 0x55, 0x54, 0x47, 0x46, 0x45, 0x44,
               0x37, 0x36, 0x35, 0x34, 0x27, 0x26, 0x25, 0x24, 0x17, 0x16, 0x15, 0x14,
               0x07, 0x06, 0x05, 0x04, 0x03, 0x02, 0x01, 0x00]


def noise_note(n):
    """Furnace noise note playing the NR43 value hUGEDriver plays for note n"""
    p = hugedriver.note_poly(n)
    return NOISE_TABLE.index(p, 1) + 60


@dataclass(frozen=True)
class FState:
    """What the Furnace channel carries from row to row (the parts the conversion depends on)"""
    legato: int = 0
    fine: int = 0x80
    arp: int = 0
    slide: int = -1          # active 01/02/03 effect code, -1 none
    vib: int = 0
    vol3: int = 15
    ins: int = -1            # Furnace instrument last selected
    astage: int = -1         # Furnace's arpeggio stage (advances on every tick an arpeggio is on)
    vset: int = 0            # vibrato shape (E3) and range (E4) set for hUGEDriver's vibrato


@dataclass
class Report:
    warnings: list = field(default_factory=list)

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)


class Converter:
    def __init__(self, song: uge.Song):
        self.s = song
        self.rep = Report()
        self.fur = F.FurSong(
            name=song.name, author=song.artist, comment=song.comment,
            hz=song.tick_rate(), speed=max(1, min(255, song.ticks_per_row or 256)),
            compat={"linearPitch": 0, "noSlidesOnFirstTick": 1, "pitchMacroIsLinear": 0, "arp0Reset": 0,
                    "rowResetsArpPos": 1},
            chip_flags={"chipType": 0, "noAntiClick": True},
        )
        if song.ticks_per_row == 0:
            self.rep.warn("speed 0 (256 ticks per row) doesn't exist in Furnace: using 255")
        # Furnace inverts GB waves by default: store them inverted so the chip gets hUGE's bytes
        self.fur.waves = [[15 - v for v in w] for w in song.waves]
        self.ins_index = {}
        self.twins = {}
        self.uses_sweep = any(i.nr10() & 0x77 for i in song.duty)

    # -- instruments --
    def twin(self, index):
        """An identical copy of a Furnace instrument (to make Furnace see an instrument change)"""
        if index not in self.twins:
            src = self.fur.instruments[index]
            fi = replace(src, name=src.name + " (2)", hw_seq=list(src.hw_seq), macros=dict(src.macros))
            self.fur.instruments.append(fi)
            self.twins[index] = len(self.fur.instruments) - 1
        return self.twins[index]

    def noise_per_note(self, number):
        """Whether a noise instrument's subpattern depends on the note it plays (notes, or 9xx
        that ORs into NR43): then it gets a Furnace instrument per note, with the exact NR43 of
        every tick as a fixed arpeggio"""
        t = self.s.noise[number - 1].table()
        return bool(t) and any(c.note != uge.NO_NOTE or c.effect == 9 for c in t)

    def instrument(self, ch, number, env=None, note=None, nolen=False):
        """Furnace instrument for hUGETracker instrument `number` on channel ch (env: an NRx2 value
        replacing the instrument's envelope, for Cxx / Axy; note: the note, for noise instruments
        whose subpattern depends on it)"""
        typ = uge.CHANNEL_TYPE[ch]
        if not (typ == uge.NOISE and note is not None and self.noise_per_note(number)):
            note = None
        src = self.s.instrument(ch, number)
        nolen = nolen and src.length_enabled
        key = (typ, number, env, note, nolen)
        if key in self.ins_index:
            return self.ins_index[key]
        bank = ("Duty", "Wave", "Noise")[typ]
        name = src.name or f"{bank} {number}"
        if env is not None:
            name += f" (env {env:02X})"
        if note is not None:
            name += f" [{note}]"
        if nolen:
            name += " (no length)"
        fi = F.FurInstrument(name=name, always_init=True)
        e = src.envelope() if env is None else env
        fi.env_vol, fi.env_dir, fi.env_len = e >> 4, (e >> 3) & 1, e & 7
        if typ == uge.WAVE:
            fi.env_vol = 15
        if src.length_enabled and not nolen:
            if typ == uge.WAVE and src.length > 63:
                self.rep.warn(f"{name}: wave length {src.length} needs 8 bits, Furnace has 6")
            ln = src.length & 63
            if ln == 0 and typ != uge.NOISE:
                # Furnace can't enable length 0 (64 steps) on CH1-3: 1 (63 steps, 4 ms shorter)
                ln = 1
            fi.sound_len = 63 - ln
        else:
            fi.sound_len = 64
        if typ == uge.SQUARE and self.uses_sweep:
            fi.hw_seq.append((F.HW_SWEEP, src.nr10()))
        macros, hw, soft = self.table_macros(ch, src, e, note)
        fi.hw_seq += hw
        if soft:
            fi.soft_env = True
        if typ == uge.SQUARE:
            macros.setdefault(F.MACRO_DUTY, F.Macro([src.duty & 3]))
        elif typ == uge.WAVE:
            macros.setdefault(F.MACRO_WAVE, F.Macro([src.waveform & 15]))
        else:
            macros.setdefault(F.MACRO_DUTY, F.Macro([1 if src.seven_bit else 0]))
        fi.macros = macros
        dm = macros.get(F.MACRO_DUTY)
        if typ == uge.NOISE and src.length_enabled and dm and dm.loop == 255 and len(dm.values) > 1:
            # each duty macro step rewrites NR41, restarting the length: start it shorter by the
            # time of the last step so the note ends when hUGEDriver's does
            full = 64 - (src.length & 63)
            k = len(dm.values) - 1
            gap = int(256 / self.fur.hz) + 2     # must outlast a tick, or it ends between reloads
            fi.sound_len = 63 - (64 - min(64, max(gap, full - round(k * 256 / self.fur.hz))))
        self.fur.instruments.append(fi)
        self.ins_index[key] = len(self.fur.instruments) - 1
        return self.ins_index[key]

    def table_macros(self, ch, ins: uge.Instrument, env0, base=None):
        """A subpattern as Furnace macros and a hardware sequence: the table runs one row per tick
        from the note on, following its jumps; unrolled until its state repeats (that's the loop).
        Returns (macros, hw_seq, soft_env)."""
        table = ins.table()
        if not table:
            return {}, [], False
        typ = uge.CHANNEL_TYPE[ch]
        arp, pitch = 0, 0
        duty = ins.duty & 3 if typ == uge.SQUARE else (1 if ins.seven_bit else 0)
        wave = ins.waveform & 15
        level = WAVE_VOL[ins.output_level & 3]
        env = env0
        step4 = 8 if ins.seven_bit else 0
        nr43 = (hugedriver.note_poly(base) | step4) if base is not None else 0
        steps, seen = [], {}
        row = 0
        used = set()
        # with length on, the note is silent after its length: the macros can stop there (a running
        # macro rewrites its register every tick, and NRx1 rewrites restart the length)
        life = 250
        if ins.length_enabled:
            steps_len = (256 if typ == uge.WAVE else 64) - ((ins.length & 255) if typ == uge.WAVE else (ins.length & 63))
            life = min(250, int(steps_len * self.fur.hz / 256) + 1)
        while True:
            key = (row, arp, duty, wave, level, env, nr43)
            if key in seen or len(steps) >= life:
                loop = seen.get(key) if len(steps) < life else None
                break
            seen[key] = len(steps)
            c = table[row]
            nxt = (row + 1) & 0xFF
            if c.volume & 0x1F:
                nxt = (c.volume & 0x1F) - 1
            step = {"pitch": 0, "env": None}
            if c.note != uge.NO_NOTE:
                arp = c.note - 36
                used.add("arp")
                if base is not None:
                    nr43 = hugedriver.note_poly((base + arp) & 0xFF) | step4
                if pitch:
                    self.rep.warn(f"{ins.name or 'instrument'}: subpattern mixes slides and notes "
                                  f"(approximated)")
            e, par = c.effect, c.param
            if e == 9:
                if typ == uge.SQUARE:
                    duty = (par >> 6) & 3
                    used.add("duty")
                elif typ == uge.WAVE:
                    wave = par & 15
                    used.add("wave")
                    if par > 15:
                        self.rep.warn(f"{ins.name or 'instrument'}: waveform {par} doesn't exist")
                else:
                    duty = 1 if par & 8 else 0
                    used.add("duty")
                    if base is not None:
                        nr43 = (nr43 & 0xF7) | par
            elif e == 0xC and (e or par):
                sc = hugedriver.swap(par)
                if typ == uge.WAVE:
                    lvl = 0x20 if sc >= 0xA0 else 0x40 if sc >= 0x50 else 0 if sc == 0 else 0x60
                    level = {0x20: 15, 0x40: 8, 0x60: 4, 0: 0}[lvl]
                    used.add("level")
                else:
                    env = ((env & 0x0F) | sc) if typ == uge.SQUARE else sc
                    step["env"] = env
                    used.add("env")
            elif e == 0xA and par and typ != uge.WAVE:
                v = max(0, (env >> 4) - (par & 15))
                v = min(15, v + (par >> 4))
                env = v << 4
                step["env"] = env
                used.add("env")
            elif e in (1, 2) and par:
                step["pitch"] = par if e == 1 else -par
                used.add("pitch")
            elif e or par:
                self.rep.warn(f"{ins.name or 'instrument'}: subpattern effect {e:X}{par:02X} "
                              f"isn't converted")
            step.update(arp=arp, duty=duty, wave=wave, level=level, nr43=nr43)
            steps.append(step)
            row = nxt if nxt < uge.TABLE_ROWS else 0
        lp = 255 if loop is None else loop
        macros = {}
        if base is not None:
            # noise per note: the NR43 of every tick, as a fixed arpeggio (Furnace plays
            # noiseTable[fixed + 24]) and the 7-bit mode as the duty macro
            vals = []
            for x in steps:
                v = apumodel.canonical_nr43(x["nr43"]) & 0xF7
                if v not in NOISE_TABLE:
                    self.rep.warn(f"{ins.name or 'instrument'}: NR43 {v:02X} isn't in Furnace's noise table")
                    v = min(NOISE_TABLE[1:], key=lambda t: abs(t - v))
                i = NOISE_TABLE.index(v, 1) - 24
                vals.append(i | 0x40000000 if i >= 0 else i & ~0x40000000)
            macros[F.MACRO_ARP] = F.Macro(vals, loop=lp)
            duties = [1 if x["nr43"] & 8 else 0 for x in steps]
            # a running duty macro rewrites NR41 every tick (restarting the length): only when needed
            macros[F.MACRO_DUTY] = F.Macro(duties if len(set(duties)) > 1 else duties[:1], loop=lp
                                           if len(set(duties)) > 1 else 255)
            used.discard("arp")
            used.discard("duty")
        if "arp" in used:
            macros[F.MACRO_ARP] = F.Macro([x["arp"] for x in steps], loop=lp)
        if "duty" in used:
            macros[F.MACRO_DUTY] = F.Macro([x["duty"] for x in steps], loop=lp)
        if "wave" in used:
            macros[F.MACRO_WAVE] = F.Macro([x["wave"] for x in steps], loop=lp)
        if "pitch" in used:
            # relative: hUGEDriver adds to the period every tick (non-linear pitch: period units)
            macros[F.MACRO_PITCH] = F.Macro([x["pitch"] for x in steps], loop=lp, mode=1)
        # a macro whose looped part is constant ends there instead (a running macro rewrites its
        # register every tick: NRx1 for duty, which reloads the length counter)
        # (noise per note: the arpeggio macro must run as long as the duty one, Furnace only
        # writes NR43's 7-bit flag along with the noise frequency)
        noise_joint = base is not None and len(macros.get(F.MACRO_DUTY, F.Macro([0])).values) > 1
        groups = [[F.MACRO_ARP, F.MACRO_DUTY]] if noise_joint else [[c] for c in macros]
        for g in groups:
            ms = [macros[c] for c in g if c in macros]
            if not ms:
                continue
            rows = list(zip(*[m.values for m in ms]))
            loop_ = ms[0].loop
            if loop_ != 255 and len(set(rows[loop_:])) == 1:
                rows = rows[:loop_ + 1]
                loop_ = 255
            if loop_ == 255:
                while len(rows) > 1 and rows[-1] == rows[-2]:
                    rows.pop()
            for k, m in enumerate(ms):
                m.values = [r[k] for r in rows]
                m.loop = loop_
        soft = False
        if "level" in used:
            macros[F.MACRO_VOL] = F.Macro([x["level"] for x in steps], loop=lp)
            soft = True
        hw = []
        if "env" in used:
            # set envelope (restarts the note, like hUGEDriver's NRx2 write + retrigger), then wait
            sound_len = 63 - ((ins.length & 63) or (0 if typ == uge.NOISE else 1)) if ins.length_enabled else 64
            starts = {}
            i = 0
            while i < len(steps):
                starts[i] = len(hw)
                if steps[i]["env"] is not None:
                    hw.append((F.HW_ENVELOPE, steps[i]["env"] | (sound_len << 8)))
                j = i + 1
                while j < len(steps) and steps[j]["env"] is None and j != loop:
                    j += 1
                hw.append((F.HW_WAIT, j - i - 1))
                i = j
            if loop is not None:
                if loop not in starts:
                    self.rep.warn(f"{ins.name or 'instrument'}: subpattern loop isn't on a tick boundary")
                else:
                    hw.append((F.HW_LOOP, starts[loop]))
            if len(hw) > 255:
                self.rep.warn(f"{ins.name or 'instrument'}: subpattern too long for a hardware sequence")
                hw = hw[:255]
        return macros, hw, soft

    # -- rows --
    def row(self, ch, cell: uge.Cell, st: FState, hs, explicit=False):
        """Furnace row for a hUGETracker cell. st: the Furnace channel state before it, hs: the
        hUGEDriver state before the row (dict). explicit: set every state the row depends on,
        whatever st says (for rows reached with different states). Returns (Row, new state)."""
        r = F.Row()
        fx = []
        note, ins, eff, par = cell.note, cell.instrument, cell.effect, cell.param
        valid = note < uge.LAST_NOTE
        typ = uge.CHANNEL_TYPE[ch]
        new = st
        play = valid

        def need(field_, value, code):
            nonlocal new
            if explicit or getattr(new, field_) != value:
                fx.append((code, value))
            new = replace(new, **{field_: value})

        # effects that decide whether the note plays
        if valid and eff == 7:
            # the note plays on tick par; 0 or a tick past the row's end: never
            if par == 0 or par >= hs["ticks"]:
                play = False
            else:
                fx.append((0xED, par))
        if eff == 0xE and par == 0:     # cut on tick 0 (before a note plays): silence
            play = False
            r.note = F.NOTE_OFF

        if valid and play:
            r.note = noise_note(note) if typ == uge.NOISE else note + NOTE_OFS
            if ins and eff != 3:
                # arpeggio / slide / vibrato write NRx4 without the length bit in hUGEDriver:
                # from tick 1 the note has no length
                nolen = (eff in (0, 1, 2, 4) and par > 0 and hs["ticks"] > 1)
                r.ins = self.instrument(ch, ins, note=note, nolen=nolen)
                new = replace(new, ins=r.ins)
                if typ == uge.WAVE:
                    vol = WAVE_VOL[self.s.instrument(ch, ins).output_level & 3]
                    soft = self.fur.instruments[r.ins].soft_env
                    if soft:
                        vol = 15    # its volume macro has the absolute levels
                    if explicit or vol != new.vol3:
                        r.vol = vol
                    # after a volume macro, Furnace's wave volume is the macro's last value
                    new = replace(new, vol3=-1 if soft else vol)
            elif ins and eff == 3:
                # hUGEDriver loads the instrument on tick 0 and restarts the note on tick 1 (the
                # tone portamento's first step writes NRx4 with the instrument's trigger bit).
                # Furnace restarts a note when the instrument changes during a portamento: delay
                # the row by a tick, with a twin of the instrument if it is already selected.
                base_ins = self.instrument(ch, ins, note=note)
                r.ins = base_ins if (base_ins != new.ins or explicit) else self.twin(base_ins)
                if hs["ticks"] > 1:
                    fx.append((0xED, 1))
                new = replace(new, ins=r.ins)
            if typ == uge.WAVE and not ins and eff != 3:
                # hUGEDriver plays a CH3 note by turning the DAC off and on (which stops the
                # channel) and writing NR34: without an instrument there's no trigger bit, so
                # the channel stays off
                r.note = F.NOTE_OFF
            elif eff != 3 and typ != uge.NOISE:
                need("legato", 0 if ins else 1, 0xEA)
            elif eff != 3 and not ins:
                self.rep.warn("CH4 notes without an instrument change the noise without retriggering "
                              "in hUGEDriver; Furnace can't (they retrigger)")
            if typ != uge.NOISE:
                need("fine", 0x80 + FINE.get(note, 0), 0xE5)

        # the row's effect
        cont = {"arp": 0, "slide": -1, "vib": 0}
        if eff == 0 and par:
            # hUGEDriver plays +low, +high, +0 nibble by a global tick counter; Furnace 00xy plays
            # +0, +x, +y by its own stage: order x, y so they line up on this row
            # (the counter is 8 bits and 256 isn't a multiple of 3: it repeats a step every 256
            # ticks, so the best order is counted over the row's actual ticks)
            s0 = 0      # rowResetsArpPos: Furnace's arpeggio starts over on every row
            vh = (par & 15, par >> 4, 0)
            phases = [((hs["counter"] + k - 1) & 0xFF) % 3 for k in range(max(1, hs["ticks"]))]
            best = None
            for d in range(3):
                x, y = vh[(1 - d) % 3], vh[(2 - d) % 3]
                vf = (0, x, y)
                hits = sum(vf[(s0 + k) % 3] == vh[ph] for k, ph in enumerate(phases))
                if best is None or hits > best[0]:
                    best = (hits, (x << 4) | y)
            cont["arp"] = best[1] or 0x01
        elif eff in (1, 2) and par:
            cont["slide"] = eff
            fx.append((eff, par))
        elif eff == 3 and par:
            cont["slide"] = 3
            fx.append((3, par))
        elif eff == 4 and par:
            # hUGEDriver: +y period units on the ticks where (counter & x) == 0, the note's period
            # on the others. A one-bit x is a square with a period of 2x ticks: Furnace's "square
            # up" vibrato (E308) at rate 32 / x; with range E402 a depth d moves exactly d units.
            # Other x get the square of their highest bit; Furnace's rate tops out at 15 (x = 1, 2
            # get its fastest square).
            x, y = par >> 4, par & 15
            rate = 0 if x == 0 else min(15, 32 >> (x.bit_length() - 1))
            cont["vib"] = (rate << 4) | y
            if explicit or not new.vset:
                fx.append((0xE3, 0x08))
                fx.append((0xE4, 0x02))
            new = replace(new, vset=1)
        elif eff == 5:
            self.rep.warn(f"5{par:02X} (master volume NR50) has no Furnace effect")
        elif eff == 6:
            self.rep.warn(f"6{par:02X} calls an assembly routine: not converted")
        elif eff == 8:
            pass   # global: added to every channel by the caller
        elif eff == 9:
            if typ == uge.SQUARE:
                fx.append((0x12, (par >> 6) & 3))
            elif typ == uge.WAVE:
                fx.append((0x10, par & 15))
            else:
                fx.append((0x11, 1 if par & 8 else 0))
        elif eff == 0xA:
            new = self.vol_slide(ch, par, r, new, hs, fx, note if valid else hs["note"])
        elif eff == 0xB:
            fx.append((0x0B, par - 1) if par else (0x0D, 0))
        elif eff == 0xC:
            new = self.set_volume(ch, par, r, new, hs, valid and play, ins, fx, note)
        elif eff == 0xD:
            if par:
                fx.append((0x0D, par - 1))
        elif eff == 0xE and par:
            if par < hs["ticks"]:       # (a tick past the row's end never comes)
                fx.append((0xEC, par))
        elif eff == 0xF:
            fx.append((0x0F, par if par else 255))

        # hUGEDriver keeps the period its vibrato left (base + depth on an "up" tick) until
        # something sets it again: hold it with fine pitch when the vibrato stops
        if new.vib and not cont["vib"] and not (valid and play) and typ != uge.NOISE:
            lo, hi = ((0x13, 0x14), (0x18, 0x19), (0x1D, 0x1E))[ch]
            cur = hs["regs"].get(lo, 0) | ((hs["regs"].get(hi, 0) & 7) << 8)
            n = hs["note"]
            if n < uge.LAST_NOTE:
                ofs = cur - uge.NOTE_TABLE[n] + FINE.get(n, 0)
                if -128 <= ofs <= 127:
                    need("fine", 0x80 + ofs, 0xE5)
        # (rows reached with different states stop whatever they don't set)
        if cont["arp"] or new.arp or explicit:
            if explicit or cont["arp"] != new.arp:
                fx.append((0x00, cont["arp"]))
            new = replace(new, arp=cont["arp"])
        if cont["arp"]:
            new = replace(new, astage=(new.astage + hs["ticks"]) % 3)
        if cont["slide"] == -1 and (new.slide != -1 or explicit):
            fx.append((new.slide if new.slide != -1 else 0x01, 0))
        new = replace(new, slide=cont["slide"])
        if cont["vib"] or new.vib or explicit:
            if explicit or cont["vib"] != new.vib:
                fx.append((0x04, cont["vib"]))
            new = replace(new, vib=cont["vib"])
        r.effects = fx
        return r, new

    def set_volume(self, ch, par, r, st, hs, playing, ins, fx, note):
        sc = hugedriver.swap(par)
        if ch == 2:
            lvl = 0x20 if sc >= 0xA0 else 0x40 if sc >= 0x50 else 0 if sc == 0 else 0x60
            vol = {0x20: 15, 0x40: 8, 0x60: 4, 0: 0}[lvl]
            r.vol = vol
            return replace(st, vol3=vol)
        # CH1/2: NRx2 = (NRx2 & 0x0F) | swap(c); CH4: NRx2 = swap(c); then the note restarts if
        # the last note row had an instrument
        if playing:
            base_env = self.s.instrument(ch, ins).envelope() if ins else hs["regs"][hugedriver.ENV[ch]]
        else:
            base_env = hs["regs"][hugedriver.ENV[ch]]
        env = ((base_env & 0x0F) | sc) if ch < 2 else sc
        r.vol = env >> 4
        last = hs["last_ins"]
        if not playing:
            if not hs["highmask"] & 0x80:
                self.rep.warn("Cxx without a note after a note without an instrument (a 'zombie' "
                              "volume change in hUGEDriver) isn't converted")
                return st
            if last is None:
                return st
            r.note = noise_note(hs["note"]) if ch == 3 else hs["note"] + NOTE_OFS
            ins = last
        if ins and (env & 0x0F) != (self.s.instrument(ch, ins).envelope() & 0x0F):
            r.ins = self.instrument(ch, ins, env=env & 0x0F | (self.s.instrument(ch, ins).envelope() & 0xF0),
                                    note=note if playing else hs["note"])
        elif ins:
            r.ins = self.instrument(ch, ins, note=note if playing else hs["note"])
        st = replace(st, ins=r.ins if r.ins >= 0 else st.ins)
        if ch != 3 and r.note >= 0 and r.note != F.NOTE_OFF:
            if st.legato:
                fx.append((0xEA, 0))
            st = replace(st, legato=0)
        return st

    def vol_slide(self, ch, par, r, st, hs, fx, note):
        last = hs["last_ins"]
        if last is None:
            return st
        if ch == 2:
            # NR32 reads back | 0x9F: the "volume" is 8 + 2 * level bits + 1, then NR32 = vol << 4
            # (level = bits 5-6 of that) and the note restarts
            cur = ((hs["regs"].get(0x1C, 0) | 0x9F) & 0xF0) >> 4
            v = min(15, max(0, cur - (par & 15)) + (par >> 4))
            r.vol = {0: 0, 1: 15, 2: 8, 3: 4}[((v << 4) >> 5) & 3]
            if r.note < 0:
                r.note = hs["note"] + NOTE_OFS
            if st.legato:
                fx.append((0xEA, 0))
            return replace(st, vol3=r.vol, legato=0)
        cur = hs["regs"][hugedriver.ENV[ch]] >> 4
        v = max(0, cur - (par & 15))
        v = min(15, v + (par >> 4))
        r.vol = v
        if r.note < 0:
            r.note = noise_note(hs["note"]) if ch == 3 else hs["note"] + NOTE_OFS
        r.ins = self.instrument(ch, last, env=v << 4, note=note)
        if ch != 3 and st.legato:
            fx.append((0xEA, 0))
        return replace(st, ins=r.ins, legato=0 if ch != 3 else st.legato)

    def add_pan(self, ch, pan, r):
        left, right = (pan >> (ch + 4)) & 1, (pan >> ch) & 1
        if not left and not right:
            self.rep.warn(f"8{pan:02X} turns channel {ch + 1} off in both speakers: Furnace's "
                          f"panning can't (it plays it in both)")
        r.effects.append((0x08, (0xF0 if left else 0) | (0x0F if right else 0)))

    def apply(self, ch, r: F.Row, st: FState, hs):
        """The Furnace state after playing row r from state st"""
        new = st
        if r.ins >= 0:
            new = replace(new, ins=r.ins)
        if ch == 2 and r.vol >= 0:
            new = replace(new, vol3=r.vol)
        arp_on = new.arp
        for code, val in r.effects:
            if code == 0xEA:
                new = replace(new, legato=val)
            elif code == 0xE5:
                new = replace(new, fine=val)
            elif code == 0x00:
                arp_on = val
                new = replace(new, arp=val)
            elif code in (1, 2, 3):
                new = replace(new, slide=code if val else -1)
            elif code == 0x04:
                new = replace(new, vib=val)
            elif code == 0xE4:
                new = replace(new, vset=1)
        if arp_on:
            new = replace(new, astage=(new.astage + hs["ticks"]) % 3)
        return new

    # -- song --
    def walk(self, max_rows=20000):
        """The rows the song plays, in order, with the hUGEDriver state before each:
        [(order, row, hs per channel)], and the index the song loops back to"""
        drv = hugedriver.HugeDriver(self.s)
        path, seen = [], {}
        last_ins = [None] * 4
        while len(path) < max_rows:
            if drv.tick_no == 0:
                key = (drv.order, drv.row, drv.ticks_per_row, drv.row_break, drv.next_order)
                if key in seen:
                    return path, seen[key], drv
                seen[key] = len(path)
                hs = []
                for ch in range(4):
                    c = drv.ch[ch]
                    hs.append({"note": c.note, "highmask": c.highmask, "regs": dict(drv.regs),
                               "last_ins": last_ins[ch], "counter": drv.counter, "ticks": 0})
                path.append((drv.order, drv.row, hs))
                for ch in range(4):
                    cell = self.s.pattern(ch, drv.order)[drv.row]
                    if cell.note < uge.LAST_NOTE and cell.instrument:
                        last_ins[ch] = cell.instrument
            drv.tick()
            for h in path[-1][2]:
                h["ticks"] += 1
        self.rep.warn("the song didn't loop within 20000 rows")
        return path, 0, drv

    def convert(self):
        s = self.s
        path, loop_at, _ = self.walk()
        # Patterns are converted per order (a row's Furnace version can depend on what the other
        # channels do in that order, e.g. hUGETracker's 8xx pans all channels) and merged again
        # when they come out the same.
        # Each row's Furnace version depends on the Furnace state it is reached with. Play the path
        # (twice: the loop's second pass enters rows with the loop's end state) with the rows
        # chosen so far, see what each executed row needs in its state, and settle: a row always
        # reached with what it needs keeps that version; one needed differently in different
        # places gets the explicit version (sets every state it depends on).
        order_seq = list(range(len(path))) + list(range(loop_at, len(path)))
        default_hs = {"note": 0, "highmask": 0, "regs": dict(apumodel.POST_BOOT), "last_ins": None,
                      "counter": 1, "ticks": s.ticks_per_row or 256}
        chosen = {}       # (ch, order, row) -> Row
        explicit = set()
        for _ in range(6):
            needs = {}    # (ch, order, row) -> [(repr, Row, hs, st, pan)]
            state = [FState() for _ in range(4)]
            for idx in order_seq:
                order, row, hs = path[idx]
                pan = None
                for ch in range(4):
                    c = s.pattern(ch, order)[row]
                    if c.effect == 8:
                        pan = c.param
                for ch in range(4):
                    k = (ch, order, row)
                    cell = s.pattern(ch, order)[row]
                    want, after = self.row(ch, cell, state[ch], hs[ch], explicit=k in explicit)
                    if pan is not None:
                        self.add_pan(ch, pan, want)
                    needs.setdefault(k, []).append((repr(want), want, hs[ch], state[ch], pan))
                    used = chosen.get(k, want)
                    state[ch] = after if repr(used) == repr(want) else self.apply(ch, used, state[ch], hs[ch])
            changed = False
            for k, lst in needs.items():
                reprs = {x[0] for x in lst}
                if len(reprs) == 1:
                    pick = lst[0][1]
                else:
                    if k not in explicit:
                        explicit.add(k)
                        changed = True
                    ch, order, row = k
                    pick, _ = self.row(ch, s.pattern(ch, order)[row], lst[0][3], lst[0][2], explicit=True)
                    if lst[0][4] is not None:
                        self.add_pan(ch, lst[0][4], pick)
                if repr(chosen.get(k)) != repr(pick):
                    chosen[k] = pick
                    changed = True
            if not changed:
                break
        else:
            self.rep.warn("the conversion didn't settle (some rows may depend on how they're reached)")

        self.fur.orders = [[] for _ in range(4)]
        for ch in range(4):
            seen = {}
            for order in range(len(s.orders[ch])):
                rows = []
                for row in range(64):
                    k = (ch, order, row)
                    if k in chosen:
                        rows.append(chosen[k])
                    else:   # never played
                        r, _ = self.row(ch, s.pattern(ch, order)[row], FState(), default_hs)
                        rows.append(r)
                key = repr(rows)
                if key not in seen:
                    seen[key] = len(seen)
                    self.fur.patterns[(ch, seen[key])] = rows
                self.fur.orders[ch].append(seen[key])
            if len(seen) > 256:
                raise SystemExit(f"channel {ch + 1} needs {len(seen)} patterns, Furnace has 256")
        cols = [1] * 4
        for (ch, _), rows in self.fur.patterns.items():
            for r in rows:
                cols[ch] = max(cols[ch], len(r.effects))
        if max(cols) > 8:
            raise SystemExit("a row needs more than 8 effect columns")
        self.fur.effect_cols = cols
        return self.fur


# -- checking a conversion --

def huge_states(song, ticks):
    drv = hugedriver.HugeDriver(song)
    stream = [drv.tick() for _ in range(ticks)]
    init = [(0x26, 0x80), (0x25, 0xFF), (0x24, 0x77)]
    return apumodel.play(stream, song.tick_rate(), init), drv


def fur_states(fur_path, hz, ticks):
    import furtrace
    trace = furtrace.run(Path(fur_path), 1)
    if sum(1 for t in trace.ticks if t.chip_ticked()) < ticks:
        trace = furtrace.run(Path(fur_path), 2 + ticks // max(1, len(trace.ticks)))
    stream = []
    for t in trace.ticks:
        if not t.chip_ticked():
            continue
        stream.append([(o[1], o[2]) for o in t.ops if o[0] == "W"])
        if len(stream) >= ticks:
            break
    init = [(o[1], o[2]) for o in trace.init if o[0] == "W"]
    return apumodel.play(stream, hz, init)


def describe(o):
    if o is None:
        return "silent"
    if o[0] == "sq":
        return f"vol {o[1]} period {o[2]} duty {o[3]} pan {o[4]}"
    if o[0] == "wave":
        return f"level {o[1]} period {o[2]} wave {bytes(o[3]).hex()} pan {o[4]}"
    return f"vol {o[1]} nr43 {o[2]:02X} pan {o[3]}"


def verify(song, fur_path, ticks=None, show=3):
    if ticks is None:
        ticks = 6000
    want, drv = huge_states(song, ticks)
    got = fur_states(fur_path, song.tick_rate(), ticks)
    n = min(len(want), len(got))
    # positions of each tick
    pos = []
    d2 = hugedriver.HugeDriver(song)
    for _ in range(n):
        pos.append(d2.position())
        d2.tick()
    bad = [0, 0, 0, 0]
    tiny = [0, 0, 0, 0]
    phase = [0, 0, 0, 0]
    first = []
    shown = [0, 0, 0, 0]
    for t in range(n):
        (wo, wv), (go, gv) = want[t], got[t]
        for ch in range(4):
            if wo[ch] != go[ch]:
                w, g = wo[ch], go[ch]
                if (w and g and w[0] == g[0] == "sq" and w[1] == g[1] and w[3:] == g[3:]
                        and abs(w[2] - g[2]) <= 2) or (w and g and w[0] == g[0] == "wave"
                        and w[1] == g[1] and w[3:] == g[3:] and abs(w[2] - g[2]) <= 2):
                    tiny[ch] += 1      # hUGEDriver's note table vs equal temperament: <= 3 cents
                    continue
                o, r, k = pos[t]
                cell = song.pattern(ch, o)[r]
                if (w and g and w[0] == g[0] and w[0] in ("sq", "wave") and w[1] == g[1]
                        and w[3:] == g[3:] and cell.effect in (0, 4) and cell.param):
                    phase[ch] += 1     # same arpeggio / vibrato, at another point of its cycle
                    continue
                bad[ch] += 1
                if shown[ch] < show:
                    shown[ch] += 1
                    o, r, k = pos[t]
                    first.append(f"tick {t} (order {o} row {r} tick {k}) CH{ch + 1}: "
                                 f"hUGE {describe(wo[ch])} / Furnace {describe(go[ch])}")
    return n, bad, first, tiny, phase


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("uge", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--ticks", type=int, default=6000, help="ticks to compare")
    a = ap.parse_args()
    song = uge.read(a.uge)
    conv = Converter(song)
    fur = conv.convert()
    out = a.out or a.uge.with_suffix(".fur")
    F.write(fur, out)
    print(f"{a.uge.name} -> {out}: {len(fur.instruments)} instruments, {len(fur.patterns)} patterns")
    for w in conv.rep.warnings:
        print(f"  warning: {w}")
    if not a.no_verify:
        n, bad, first, tiny, phase = verify(song, out, a.ticks)
        total = sum(bad)
        print(f"  check: {n} ticks, " + ", ".join(f"CH{i + 1} {n - b}/{n}" for i, b in enumerate(bad))
              + (" (identical)" if not total else ""))
        if sum(tiny):
            print(f"    + {sum(tiny)} channel-ticks 1-2 period units off (hUGEDriver's note table, <= 3 cents)")
        if sum(phase):
            print(f"    + {sum(phase)} channel-ticks with an arpeggio / vibrato at another point of its cycle")
        for f in first:
            print("   ", f)


if __name__ == "__main__":
    main()
