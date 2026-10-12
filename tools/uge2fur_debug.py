"""Debug view of a uge2fur conversion at one tick: hUGE cells, Furnace rows, both streams' writes.

    python tools/uge2fur_debug.py song.uge TICK [--ch N] [--span K]
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import uge, uge2fur, hugedriver, furtrace, furfile as F

ap = argparse.ArgumentParser()
ap.add_argument("uge", type=Path); ap.add_argument("tick", type=int)
ap.add_argument("--ch", type=int, default=0); ap.add_argument("--span", type=int, default=3)
a = ap.parse_args()
song = uge.read(a.uge)
conv = uge2fur.Converter(song); fur = conv.convert()
out = Path("build/uge2fur") / (a.uge.stem + ".fur"); F.write(fur, out)
trace = furtrace.run(out, 2)
fticks = [t for t in trace.ticks if t.chip_ticked()]
d = hugedriver.HugeDriver(song)
want, _ = uge2fur.huge_states(song, a.tick + a.span + 1)
got = uge2fur.fur_states(out, song.tick_rate(), a.tick + a.span + 1)
for t in range(a.tick + a.span + 1):
    o, r, k = d.position(); w = d.tick()
    if t < a.tick - a.span: continue
    chs = [a.ch - 1] if a.ch else range(4)
    if k == 0 or t == a.tick - a.span:
        for ch in chs:
            no = fur.orders[ch][o]
            print(f"  -- order {o} row {r} CH{ch+1}: hUGE {song.pattern(ch, o)[r]}  |  fur {fur.patterns[(ch, no)][r]}")
    f = lambda ws: " ".join(f"{x:02x}={v:02x}" for x, v in ws)
    fw = [(op[1], op[2]) for op in fticks[t].ops if op[0] == "W"]
    sel = lambda ws: [x for x in ws if not a.ch or (0x10 + 5*(a.ch-1) <= x[0] <= 0x14 + 5*(a.ch-1)) or x[0] in (0x24, 0x25)]
    print(f"{t:5} o{o} r{r} t{k}  hUGE: {f(sel(w))}")
    print(f"{'':16}  fur:  {f(sel(fw))}")
    for ch in chs:
        if want[t][0][ch] != got[t][0][ch]:
            print(f"{'':16}  CH{ch+1} hUGE {uge2fur.describe(want[t][0][ch])} / fur {uge2fur.describe(got[t][0][ch])}")
