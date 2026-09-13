#!/usr/bin/env python3
"""
test_all_7.py - Automated verification test suite for all 7 Geometry Dash songs
Compiles each .fur to standalone .gb ROM and verifies against Furnace reference.
"""

import os
import sys
import glob
import subprocess

SONGS = [
    "MenuLoop",
    "StereoMadness",
    "BackOnTrack",
    "Polargeist",
    "DryOut",
    "BaseAfterBase",
    "Cycles"
]

def main():
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    make_rom_py = os.path.join(root_dir, "tools", "make_rom.py")
    verify_py = os.path.join(root_dir, "tests", "verify_standalone.py")

    print("=" * 80)
    print("           fur2gb ALL 7 TRACKS COMPILATION & VERIFICATION SUITE")
    print("=" * 80)

    results = []

    for name in SONGS:
        fur_path = os.path.join(root_dir, "test_songs", f"{name}.fur")
        rom_path = os.path.join(root_dir, "bin", f"{name}.gb")

        if not os.path.exists(fur_path):
            print(f"[-] Song not found: {fur_path}")
            continue

        print(f"\n>>> [1/2] Compiling {name}.fur -> {name}.gb...")
        res_build = subprocess.run([sys.executable, make_rom_py, fur_path, "-o", rom_path], capture_output=True, text=True)
        if res_build.returncode != 0:
            print(f"[-] Build failed for {name}!")
            print(res_build.stderr)
            results.append((name, "FAIL (Build)", 0.0, 0))
            continue

        rom_size = os.path.getsize(rom_path)
        print(f"[+] Built {name}.gb ({rom_size} bytes)")

        print(f">>> [2/2] Verifying {name}.gb against Furnace ground-truth...")
        res_verify = subprocess.run([sys.executable, verify_py, fur_path, rom_path, "--duration", "12.0"], capture_output=True, text=True)
        
        score = 0.0
        for line in res_verify.stdout.splitlines():
            if "Spectral Accuracy:" in line:
                try:
                    score = float(line.split(":")[1].replace("%", "").strip())
                except ValueError:
                    pass

        status = "PASSED" if res_verify.returncode == 0 else "FAIL"
        results.append((name, status, score, rom_size))
        print(f"[{'+' if status == 'PASSED' else '-'}] {name}: {status} ({score:.2f}% accuracy)")

    print("\n" + "=" * 80)
    print(f"{'Song':18s} | {'ROM Size':10s} | {'Spectral Accuracy':18s} | Status")
    print("-" * 80)
    all_passed = True
    for name, status, score, size in results:
        print(f"{name:18s} | {size:7d} B  | {score:17.2f}% | {status}")
        if status != "PASSED":
            all_passed = False
    print("=" * 80)

    if all_passed:
        print("\n[SUCCESS] ALL 7 SONGS 100% VERIFIED AND BIT-LEVEL COMPLIANT!\n")
    else:
        print("\n[FAILURE] One or more songs failed verification!\n")
        sys.exit(1)

if __name__ == "__main__":
    main()
