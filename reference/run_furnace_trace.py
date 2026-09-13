#!/usr/bin/env python3
"""Run an instrumented Furnace build and collect its native GB backend trace."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("furnace", type=Path, help="instrumented Furnace executable")
    parser.add_argument("fur", type=Path)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--duration", type=int, default=1)
    args = parser.parse_args()

    args.trace.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["FUR2GB_GB_TRACE"] = str(args.trace.resolve())
    # Audio is deliberately discarded; the trace comes from DivPlatformGB itself.
    audio_path = args.trace.with_suffix(".discard.wav")
    try:
        result = subprocess.run(
            [str(args.furnace), "-q", "-noreport", "-output", str(audio_path),
             "-loops", str(args.duration), str(args.fur)],
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit(result.returncode)
    finally:
        if audio_path.exists():
            audio_path.unlink()


if __name__ == "__main__":
    main()
