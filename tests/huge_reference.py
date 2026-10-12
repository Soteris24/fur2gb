#!/usr/bin/env python3
"""Reference output of a .uge song: the real hUGEDriver playing it, in an emulator.

Exports the song with hUGETracker's uge2source (RGBDS .asm), assembles it with hUGEDriver.asm
and tests/huge_ref/ref.asm into a 32 KB ROM, runs it in Mesen's test runner and records every
APU register write of each hUGE_dosound call. Results are cached in build/huge_ref.

Needs hUGETracker (HUGETRACKER: its folder, with uge2source, rgbasm, rgblink, rgbfix and
hUGEDriver/) and Mesen 2 (MESEN). This is a test tool: uge2fur itself doesn't need either.

    python tests/huge_reference.py song.uge [--ticks N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUGETRACKER = Path(os.environ.get("HUGETRACKER", r"C:\Users\soter\Downloads\hUGETracker-1.0.11-windows"))
MESEN = Path(os.environ.get("MESEN", r"C:\Users\soter\OneDrive\Documents\Mesen.exe"))
CACHE = ROOT / "build" / "huge_ref"


class RefError(RuntimeError):
    pass


def _run(cmd, cwd=None):
    res = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, cwd=cwd)
    if res.returncode != 0:
        raise RefError(f"{Path(str(cmd[0])).name} failed: {res.stdout}\n{res.stderr}")
    return res


def build_rom(uge: Path, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    drv = HUGETRACKER / "hUGEDriver"
    song = work / "song.asm"
    _run([HUGETRACKER / "uge2source.exe", uge.resolve(), "ref_song", song])
    objs = []
    for src in (drv / "hUGEDriver.asm", ROOT / "tests" / "huge_ref" / "ref.asm", song):
        o = work / (src.stem + ".o")
        _run([HUGETRACKER / "rgbasm.exe", "-I", drv, "-I", drv / "include", "-o", o, src])
        objs.append(o)
    rom = work / "ref.gb"
    try:
        _run([HUGETRACKER / "rgblink.exe", "-t", "-o", rom] + objs)
    except RefError as e:
        raise RefError(f"{uge.name}: the song doesn't fit a 32 KB ROM ({e})") from None
    _run([HUGETRACKER / "rgbfix.exe", "-v", "-p", "0xFF", rom])
    return rom


def capture(uge: Path, ticks: int) -> list:
    """[[(addr, val), ...] per hUGE_dosound call] for the first `ticks` calls"""
    uge = Path(uge)
    key = hashlib.sha256(uge.read_bytes() + (HUGETRACKER / "hUGEDriver" / "hUGEDriver.asm").read_bytes()
                         + (ROOT / "tests" / "huge_ref" / "ref.asm").read_bytes()).hexdigest()[:24]
    cached = CACHE / f"{key}.json"
    if cached.exists():
        data = json.loads(cached.read_text())
        if len(data) >= ticks:
            return [[tuple(w) for w in t] for t in data[:ticks]]
    work = CACHE / key
    rom = build_rom(uge, work)
    lua = (ROOT / "tests" / "huge_ref" / "ref.lua").read_text().replace("@TICKS@", str(ticks))
    script = work / "ref.lua"
    script.write_text(lua)
    res = subprocess.run([str(MESEN), "--testrunner", str(rom), str(script), "--Gameboy.Model=Gameboy"],
                         capture_output=True, text=True, timeout=3600)
    out = []
    for line in res.stdout.splitlines():
        p = line.split()
        if not p:
            continue
        if p[0] == "T":
            out.append([])
        elif p[0] == "W" and out:
            out[-1].append((int(p[1], 16), int(p[2], 16)))
    if len(out) < ticks:
        raise RefError(f"{uge.name}: Mesen stopped after {len(out)} of {ticks} ticks\n{res.stderr[-2000:]}")
    out = out[:ticks]
    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(out))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("uge", type=Path)
    ap.add_argument("--ticks", type=int, default=2000)
    a = ap.parse_args()
    t = capture(a.uge, a.ticks)
    for i, w in enumerate(t[:20]):
        print(i, " ".join(f"{x:02x}={v:02x}" for x, v in w))
    print(f"{len(t)} ticks, {sum(len(w) for w in t)} writes")


if __name__ == "__main__":
    main()
