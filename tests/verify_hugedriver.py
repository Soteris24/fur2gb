#!/usr/bin/env python3
"""Check tools/hugedriver.py against the real hUGEDriver (tests/huge_reference.py): every APU
write of every tick, in order.

    python tests/verify_hugedriver.py song.uge [more.uge ...] [--ticks N]
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import uge  # noqa: E402
import hugedriver  # noqa: E402
import huge_reference  # noqa: E402


def check(path: Path, ticks: int):
    song = uge.read(path)
    try:
        ref = huge_reference.capture(path, ticks)
    except huge_reference.RefError as e:
        return None, str(e)
    drv = hugedriver.HugeDriver(song)
    for t in range(ticks):
        pos = drv.position()
        got = drv.tick()
        if [tuple(w) for w in got] != [tuple(w) for w in ref[t]]:
            und = [m for tt, m in drv.undefined if tt == t]
            f = lambda ws: " ".join(f"{a:02x}={v:02x}" for a, v in ws)
            return ("undef" if und else False), (f"tick {t} (order {pos[0]} row {pos[1]} tick {pos[2]})"
                           + (f" [undefined: {und[0]}]" if und else "")
                           + f"\n    driver: {f(ref[t])}\n    model:  {f(got)}")
    und = sorted({m for _, m in drv.undefined})
    return True, ("; ".join(und[:3]) if und else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uge", type=Path, nargs="+")
    ap.add_argument("--ticks", type=int, default=20000)
    a = ap.parse_args()
    bad = 0
    for p in a.uge:
        ok, msg = check(p, a.ticks)
        tag = {True: "OK  ", False: "FAIL", None: "SKIP", "undef": "UNDF"}[ok]
        print(f"{tag} {p.name}" + (f": {msg}" if msg else ""), flush=True)
        bad += ok is False
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
