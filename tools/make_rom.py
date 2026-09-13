#!/usr/bin/env python3
"""
make_rom.py - One-command pipeline from .fur song to standalone Game Boy .gb ROM.
Usage:
    python tools/make_rom.py test_songs/MenuLoop.fur -o bin/MenuLoop.gb
"""

import os
import sys
import argparse
import subprocess

def main():
    parser = argparse.ArgumentParser(description="Compile Furnace .fur song into a standalone Game Boy .gb ROM")
    parser.add_argument("input", help="Path to input .fur song")
    parser.add_argument("-o", "--output", default=None, help="Output .gb ROM path")
    parser.add_argument("--gbdk", default=r"C:\gbdk", help="GBDK installation path")
    parser.add_argument("--furnace", default=r"tools/bin/furnace.exe", help="Furnace CLI path")
    parser.add_argument("--solo", type=int, choices=[1, 2, 3, 4], default=None, help="Mute all channels except specified channel (1..4)")
    args = parser.parse_args()

    input_fur = os.path.abspath(args.input)
    if not os.path.exists(input_fur):
        print(f"Error: input file {input_fur} does not exist!", file=sys.stderr)
        sys.exit(1)

    base_name = os.path.splitext(os.path.basename(input_fur))[0]
    out_rom = os.path.abspath(args.output if args.output else f"bin/{base_name}.gb")
    os.makedirs(os.path.dirname(out_rom), exist_ok=True)

    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    fur2gb_py = os.path.join(root_dir, "tools", "fur2gb.py")
    furnace_exe = os.path.abspath(args.furnace)
    lcc_exe = os.path.join(args.gbdk, "bin", "lcc.exe")

    if not os.path.exists(lcc_exe):
        print(f"Error: lcc.exe not found at {lcc_exe}!", file=sys.stderr)
        sys.exit(1)

    # 1. Compile .fur to song_data.c
    temp_c = os.path.join(root_dir, "bin", f"{base_name}_data.c")
    print(f"[*] Converting {input_fur} -> {temp_c}...")
    cmd_convert = [
        sys.executable, fur2gb_py, input_fur,
        "-o", temp_c,
        "--furnace", furnace_exe
    ]
    res = subprocess.run(cmd_convert)
    if res.returncode != 0:
        print("[-] fur2gb conversion failed!", file=sys.stderr)
        sys.exit(res.returncode)

    # 2. Compile standalone ROM with lcc
    driver_c = os.path.join(root_dir, "driver", "fur_driver.c")
    player_c = os.path.join(root_dir, "driver", "rom_player.c")
    driver_inc = os.path.join(root_dir, "driver")

    print(f"[*] Compiling ROM: {out_rom}...")
    cmd_lcc = [
        lcc_exe,
        f"-I{driver_inc}",
        "-Wl-m"
    ]
    if args.solo is not None:
        cmd_lcc.append(f"-DSOLO_CHANNEL={args.solo - 1}")
    cmd_lcc.extend([
        "-o", out_rom,
        driver_c,
        player_c,
        temp_c
    ])
    res_lcc = subprocess.run(cmd_lcc)
    if res_lcc.returncode != 0 or not os.path.exists(out_rom):
        print("[-] GBDK compilation failed!", file=sys.stderr)
        sys.exit(res_lcc.returncode if res_lcc.returncode != 0 else 1)

    print(f"[+] Successfully built standalone ROM: {out_rom} ({os.path.getsize(out_rom)} bytes)")

if __name__ == "__main__":
    main()
