#!/usr/bin/env python3
"""Run the instrumented Furnace (FUR2GB_GB_TRACE) on a .fur and parse its GB trace.

The trace is written by Furnace's own engine (patch in tools/furnace_trace.patch):

    W <addr> <val>        register write queued by DivPlatformGB (hex, addr = 0x10..0x3f)
    T <hz> <skipping>     start of an engine tick (DivEngine::nextTick)
    R <order> <row> <t> <hz>  order/row/getCurHz() right before the chip tick() runs
    V <pos> <cnt> <freq>  updateWave(): anti-click wave position, period count, ch3 freq
    Z                     anti-click state reset (phase reset macro on ch3)
    L                     the tick that just ran made the song loop

Writes before the first T are the backend's reset() writes.
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FURNACE = Path(os.environ.get("FUR2GB_FURNACE", ROOT / ".build" / "furnace-0682-build" / "furnace.exe"))


@dataclass
class Tick:
    hz: float
    order: int = -1
    row: int = -1
    # ops in queue order: ("W", addr, val), ("V", pos, cnt, freq), ("Z",), ("P",) = start of tick()
    ops: list = field(default_factory=list)
    loop: bool = False  # this tick made the song loop

    def chip_ticked(self):
        """False for the tick on which the song stops: nextTick() returns before the chip's tick()."""
        return ("P",) in self.ops

    def writes(self):
        return [(o[1], o[2]) for o in self.ops if o[0] == "W"]


@dataclass
class Trace:
    init: list            # ops queued before the first tick (reset(), forceIns())
    ticks: list           # [Tick]

    def loop_ticks(self):
        """Indices of ticks flagged L (the tick in which the song wrapped)."""
        return [i for i, t in enumerate(self.ticks) if t.loop]


def parse(path: Path) -> Trace:
    init = []
    ticks = []
    cur = None
    with open(path, "r", encoding="ascii") as f:
        for line in f:
            p = line.split()
            if not p:
                continue
            k = p[0]
            if k == "W":
                (init if cur is None else cur.ops).append(("W", int(p[1], 16), int(p[2], 16)))
            elif k == "T":
                if p[2] != "0":
                    raise ValueError(f"{path}: tick while seeking (skipping={p[2]})")
                cur = Tick(hz=struct.unpack("<f", struct.pack("<f", float(p[1])))[0])  # as getCurHz()
                ticks.append(cur)
            elif k == "R":
                cur.order, cur.row = int(p[1]), int(p[2])
                cur.hz = struct.unpack("<f", struct.pack("<f", float(p[4])))[0]  # getCurHz() in tick()
                cur.ops.append(("P",))  # the chip's tick() starts here; earlier ops come from row commands
            elif k == "V":
                (init if cur is None else cur.ops).append(("V", int(p[1]), int(p[2]), int(p[3])))
            elif k == "Z":
                cur.ops.append(("Z",))
            elif k == "L":
                cur.loop = True
            else:
                raise ValueError(f"{path}: unknown trace line {line!r}")
    return Trace(init, ticks)


def song_chips(fur: Path, furnace: Path = DEFAULT_FURNACE):
    """The song's sound chips as [(name, id, {flag: value})], from Furnace's text export."""
    with tempfile.TemporaryDirectory(prefix="fur2gb_") as tmp:
        txt = Path(tmp) / "song.txt"
        res = subprocess.run([str(furnace), "-noreport", "-loglevel", "error", "-txtout", str(txt),
                              str(Path(fur).resolve())], capture_output=True, text=True)
        if not txt.exists():
            raise RuntimeError(f"furnace could not read {fur}: {res.stdout} {res.stderr}")
        lines = txt.read_text(encoding="utf-8", errors="replace").splitlines()
    chips = []
    section = False
    in_flags = False
    for line in lines:
        if line.startswith("# "):
            section = line.strip() == "# Sound Chips"
            continue
        if not section:
            continue
        if line.startswith("- "):
            chips.append([line[2:].strip(), None, {}])
        elif line.strip().startswith("- id:") and chips:
            chips[-1][1] = int(line.split(":")[1], 16)
        elif line.strip() == "```":
            in_flags = not in_flags
        elif in_flags and "=" in line and chips:
            k, v = line.strip().split("=", 1)
            chips[-1][2][k] = v
    return [tuple(c) for c in chips]


def check_gb_song(fur: Path, furnace: Path = DEFAULT_FURNACE):
    """Raise ValueError unless the song uses exactly one Game Boy chip the player supports."""
    chips = song_chips(fur, furnace)
    names = ", ".join(c[0] for c in chips) or "none"
    if len(chips) != 1 or chips[0][1] != 0x06:
        raise ValueError(f"{Path(fur).name} is not a Game Boy song (sound chips: {names})")
    if chips[0][2].get("chipType") == "3":
        raise ValueError(f"{Path(fur).name} uses the Game Boy Advance (native) model, which a Game Boy "
                         f"can't play (64-sample waves); pick a DMG or CGB model in the chip settings")
    return chips[0]


def run(fur: Path, loops: int, furnace: Path = DEFAULT_FURNACE, subsong: int | None = None,
        keep: Path | None = None) -> Trace:
    """Render `fur` for `loops` loops with the traced Furnace and return the parsed trace."""
    fur = Path(fur).resolve()
    with tempfile.TemporaryDirectory(prefix="fur2gb_") as tmp:
        trace_path = Path(keep) if keep else Path(tmp) / "trace.txt"
        wav = Path(tmp) / "discard.wav"
        env = os.environ.copy()
        env["FUR2GB_GB_TRACE"] = str(trace_path)
        cmd = [str(furnace), "-noreport", "-loglevel", "error", "-output", str(wav), "-loops", str(loops)]
        if subsong is not None:
            cmd += ["-subsong", str(subsong)]
        cmd.append(str(fur))
        res = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if res.returncode != 0 or not trace_path.exists():
            raise RuntimeError(f"furnace failed ({res.returncode}): {res.stdout}\n{res.stderr}")
        return parse(trace_path)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fur", type=Path)
    ap.add_argument("--loops", type=int, default=2)
    ap.add_argument("--subsong", type=int)
    ap.add_argument("--keep", type=Path, help="keep the raw trace at this path")
    ap.add_argument("--furnace", type=Path, default=DEFAULT_FURNACE)
    a = ap.parse_args()
    t = run(a.fur, a.loops, furnace=a.furnace, subsong=a.subsong, keep=a.keep)
    nw = sum(len(x.writes()) for x in t.ticks)
    print(f"init ops {len(t.init)}, ticks {len(t.ticks)}, writes {nw}, loops at {t.loop_ticks()}")
