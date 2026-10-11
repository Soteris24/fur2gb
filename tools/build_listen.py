#!/usr/bin/env python3
"""Build one listening ROM per song: build/listen/<song>.gb (plays at the song's tick rate)."""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import fur2gb  # noqa: E402

GBDK = Path(r"C:\gbdk")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("songs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "listen")
    ap.add_argument("--approx-loop", action="store_true")
    a = ap.parse_args()
    for fur in a.songs:
        name = fur2gb.ident(fur.stem)
        try:
            _, song = fur2gb.compile_song(fur, 4, approx_loop=a.approx_loop)
        except SystemExit as e:
            print(f"{fur.name}: skipped: {e}", flush=True)
            continue
        work = (a.out / "work" / name).resolve()
        files = fur2gb.emit_c(work, "fur_test_song", song)
        rom = (work / "song.gb").resolve()  # lcc can't take spaces in the output path
        cmd = [str(GBDK / "bin" / "lcc.exe"), f"-I{ROOT / 'driver'}", "-Wl-yt0x19", "-autobank", "-Wl-yoA",
               f"-Wm-yn{name[:15].upper()}", "-o", str(rom), str(ROOT / "tests" / "rom" / "listen.c"),
               str(ROOT / "driver" / "furplayer.c"), str(ROOT / "driver" / "furplayer_tick.s")]
        cmd += [str(f) for f in files if f.suffix == ".c"]
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=work)
        if res.returncode != 0 or not rom.exists():
            print(f"{fur.name}: build failed: {res.stdout[-300:]} {res.stderr[-300:]}", flush=True)
            continue
        final = (a.out / f"{fur.stem}.gb").resolve()
        rom.replace(final)
        print(f"{fur.name}: {final}", flush=True)


if __name__ == "__main__":
    main()
