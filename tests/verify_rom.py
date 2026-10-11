#!/usr/bin/env python3
"""End-to-end check: the GB ROM must make exactly the register writes Furnace makes.

For each song:
  1. the instrumented Furnace 0.6.8.2 plays it for N loops and records every write of its GB
     backend, tick by tick (the reference);
  2. fur2gb compiles it; its Python player model must reproduce the reference (checked inside);
  3. a test ROM with driver/furplayer is built with GBDK and run headless in Mesen; every write to
     $FF10-$FF3F made by each fur_tick() call is captured;
  4. the ROM's writes are compared with the reference for every traced tick, all loops included.

Only NR50's VIN bits (masked to 0x77, ignored by Furnace's emulated chip) are allowed to differ.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import fur2gb  # noqa: E402
import furtrace  # noqa: E402

GBDK = Path(r"C:\gbdk")
MESEN = Path(r"C:\Users\soter\OneDrive\Documents\Mesen.exe")
BUILD = ROOT / "build" / "verify"


def build_rom(name: str, song, out: Path) -> Path:
    gen = out / "gen"
    files = fur2gb.emit_c(gen, "fur_test_song", song)
    rom = out / "furtest.gb"
    srcs = [ROOT / "tests" / "rom" / "main.c", ROOT / "driver" / "furplayer.c",
            ROOT / "driver" / "furplayer_tick.s"] + [f for f in files if f.suffix == ".c"]
    cmd = [str(GBDK / "bin" / "lcc.exe"), f"-I{ROOT / 'driver'}", "-Wl-yt0x19", "-autobank", "-Wl-yoA",
           "-Wl-m", "-Wl-j", "-o", str(rom)] + [str(s) for s in srcs]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=out)
    if res.returncode != 0 or not rom.exists():
        raise SystemExit(f"{name}: GBDK build failed\n{res.stdout}\n{res.stderr}")
    return rom


def symbols(rom: Path):
    syms = {}
    for line in rom.with_suffix(".noi").read_text().splitlines():
        p = line.split()
        if len(p) == 3 and p[0] == "DEF":
            syms[p[1]] = int(p[2], 16)
    return syms


def run_mesen(rom: Path, ticks: int, model: str):
    syms = symbols(rom)
    lua = (ROOT / "tests" / "verify_rom.lua").read_text()
    lua = (lua.replace("@TICK_ADDR@", str(syms["_fur_tick"] & 0xFFFF))
              .replace("@MARK_ADDR@", str(syms["_fur_test_mark"] & 0xFFFF))
              .replace("@TICKS@", str(ticks)))
    script = rom.with_suffix(".lua")
    script.write_text(lua)
    cmd = [str(MESEN), "--testrunner", str(rom), str(script), f"--Gameboy.Model={model}"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    got, cost = [], []
    t0 = None
    for line in res.stdout.splitlines():
        p = line.split()
        if not p:
            continue
        if p[0] == "T":
            got.append([])
            t0 = int(p[1])
        elif p[0] == "E" and t0 is not None:
            cost.append(int(p[1]) - t0)
            t0 = None
        elif p[0] == "W":
            got[-1].append((int(p[1], 16), int(p[2], 16)))
    return got, cost


def compare(name, trace, exp, got):
    n = min(len(exp), len(got))
    for ti in range(n):
        if got[ti] != exp[ti]:
            e, g = exp[ti], got[ti]
            i = next(i for i in range(max(len(e), len(g))) if (e[i] if i < len(e) else None) != (g[i] if i < len(g) else None))
            t = trace.ticks[ti]
            return (f"FAIL at tick {ti} (order {t.order:02X} row {t.row}), write {i}: "
                    f"ROM {g[i] if i < len(g) else None}, Furnace {e[i] if i < len(e) else None}")
    if len(got) < len(exp):
        return f"FAIL: ROM produced only {len(got)} of {len(exp)} ticks"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("songs", nargs="+", type=Path)
    ap.add_argument("--loops", type=int, default=4)
    ap.add_argument("--model", default="Gameboy", choices=["Gameboy", "GameboyColor"])
    ap.add_argument("--furnace", type=Path, default=furtrace.DEFAULT_FURNACE, help="traced Furnace build")
    ap.add_argument("--approx-loop", action="store_true", help="see tools/fur2gb.py --approx-loop")
    a = ap.parse_args()
    results = []
    for fur in a.songs:
        name = fur2gb.ident(fur.stem)
        out = (BUILD / name).resolve()
        out.mkdir(parents=True, exist_ok=True)
        t = time.time()
        try:
            trace, info = fur2gb.compile_song(fur, a.loops, furnace=a.furnace, approx_loop=a.approx_loop)
        except SystemExit as e:
            results.append((name, f"CONVERT FAIL: {e}", None, None))
            print(f"{name}: CONVERT FAIL: {e}", flush=True)
            continue
        exp = fur2gb.expected_writes(trace)[:fur2gb.compared_ticks(trace, info["exact_ticks"])]
        rom = build_rom(name, info, out)
        got, cost = run_mesen(rom, len(exp), a.model)
        err = compare(name, trace, exp, got)
        nw = sum(len(x) for x in exp)
        status = err or (f"PASS {len(exp)} ticks / {nw} writes identical"
                         + (" (exact part of an --approx-loop song)" if info["exact_ticks"] else ""))
        cyc = f"max {max(cost)} avg {sum(cost) // len(cost)}" if cost else "-"
        results.append((name, status, info, cyc))
        print(f"{name}: {status}; {info['bytes'] + info['wave_bytes']} B; fur_tick cycles {cyc}; "
              f"{time.time() - t:.0f} s", flush=True)
    bad = [r for r in results if not r[1].startswith("PASS")]
    print(f"\n{len(results) - len(bad)}/{len(results)} songs identical to Furnace")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
