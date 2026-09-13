#!/usr/bin/env python3
"""
verify_standalone.py - Comprehensive audio verification between Furnace ground truth and fur2gb ROM.
Usage:
    python tests/verify_standalone.py test_songs/MenuLoop.fur bin/MenuLoop.gb
"""

import sys
import os
import argparse
import subprocess
import wave
import struct
import numpy as np
from pyboy import PyBoy

def render_furnace_ref(furnace_exe, fur_file, out_wav):
    cmd = [furnace_exe, "-output", out_wav, "-loops", "1", fur_file]
    print(f"[*] Rendering Furnace ground-truth: {out_wav}...")
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 or not os.path.exists(out_wav):
        print(f"[-] Furnace render failed: {res.stderr.decode('utf-8', errors='ignore')}", file=sys.stderr)
        return False
    return True

import scipy.signal

def record_pyboy_gb(gb_file, out_wav, duration_sec=15.0):
    print(f"[*] Recording PyBoy ROM audio for {duration_sec:.1f}s: {out_wav}...")
    pyboy = PyBoy(gb_file, window="null", sound_emulated=True)

    # 1. Wait for DMG boot ROM to complete (cartridge reached at PC >= 0x0100)
    for _ in range(200):
        if pyboy.register_file.PC >= 0x0100:
            break
        pyboy.tick()

    # 2. Flush boot chime audio buffer
    _ = pyboy.sound.ndarray

    # 3. Record audio frames (Game Boy runs ~59.7275 Hz)
    total_frames = int(duration_sec * 59.7275)
    all_chunks = []

    for frame in range(total_frames):
        pyboy.tick()
        samples = np.copy(pyboy.sound.ndarray) # int8 stereo (N, 2)
        all_chunks.append(samples)

    pyboy.stop()

    if not all_chunks:
        return False

    raw_audio = np.concatenate(all_chunks, axis=0) # shape (N, 2), int8
    float_audio = raw_audio.astype(np.float32)
    float_audio -= float_audio.mean(axis=0)

    # Convert 48000 Hz to 44100 Hz using high-quality polyphase resampler
    audio_44k = scipy.signal.resample_poly(float_audio, 147, 160, axis=0)

    max_val = np.abs(audio_44k).max()
    if max_val > 0:
        norm_audio = (audio_44k / max_val * 30000.0).astype(np.int16)
    else:
        norm_audio = audio_44k.astype(np.int16)

    # Write WAV file
    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2) # 16-bit
        wf.setframerate(44100)
        wf.writeframes(norm_audio.tobytes())

    return True

def read_wav_mono(wav_path):
    with wave.open(wav_path, "rb") as wf:
        nch = wf.getnchannels()
        sr = wf.getframerate()
        nframes = wf.getnframes()
        data = wf.readframes(nframes)
    
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    if nch == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    
    if sr != 44100:
        samples = scipy.signal.resample(samples, int(len(samples) * 44100 / sr))
    return samples

def hz_to_mel(hz): return 2595.0 * np.log10(1.0 + hz / 700.0)
def mel_to_hz(mel): return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

def get_mel_fb(sr, n_fft, n_mels=32, fmin=30, fmax=8000):
    mel_min = hz_to_mel(fmin)
    mel_max = hz_to_mel(fmax)
    mel_points = np.linspace(mel_min, mel_max, n_mels + 2)
    hz_points = mel_to_hz(mel_points)
    bins = np.floor((n_fft + 1) * hz_points / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1))
    for i in range(n_mels):
        left, center, right = bins[i], bins[i+1], bins[i+2]
        if center > left:
            fb[i, left:center] = (np.arange(left, center) - left) / (center - left)
        if right > center:
            fb[i, center:right] = (right - np.arange(center, right)) / (right - center)
    return fb

def dtw_cost(X, Y):
    N, D = X.shape
    M, _ = Y.shape
    x2 = np.sum(X**2, axis=1, keepdims=True)
    y2 = np.sum(Y**2, axis=1, keepdims=True).T
    dist = np.maximum(0, x2 + y2 - 2 * np.dot(X, Y.T))
    dist = np.sqrt(dist)
    dp = np.zeros((N, M))
    dp[0, 0] = dist[0, 0]
    for i in range(1, N): dp[i, 0] = dp[i-1, 0] + dist[i, 0]
    for j in range(1, M): dp[0, j] = dp[0, j-1] + dist[0, j]
    for i in range(1, N):
        d_row = dist[i]
        dp_prev = dp[i-1]
        dp_curr = dp[i]
        for j in range(1, M):
            m = dp_prev[j]
            if dp_curr[j-1] < m: m = dp_curr[j-1]
            if dp_prev[j-1] < m: m = dp_prev[j-1]
            dp_curr[j] = d_row[j] + m
    return dp[N-1, M-1] / (N + M)

def compare_audio(ref_wav, test_wav):
    print("[*] Performing Mel-Spectrogram DTW audio analysis...")
    ref = read_wav_mono(ref_wav)
    test = read_wav_mono(test_wav)

    # Align lead-in
    ref_idx = np.where(np.abs(ref) > 200.0)[0]
    test_idx = np.where(np.abs(test) > 200.0)[0]
    ref_start = ref_idx[0] if len(ref_idx) > 0 else 0
    test_start = test_idx[0] if len(test_idx) > 0 else 0

    ref = ref[ref_start:]
    test = test[test_start:]

    min_len = min(len(ref), len(test))
    if min_len < 44100:
        print(f"[-] Recorded audio too short! ({min_len} samples)", file=sys.stderr)
        return False, 0.0

    ref = ref[:min_len]
    test = test[:min_len]

    # Normalize energy
    ref -= np.mean(ref)
    test -= np.mean(test)
    ref /= (np.max(np.abs(ref)) + 1e-6)
    test /= (np.max(np.abs(test)) + 1e-6)

    # Compute Mel-spectrograms
    n_fft = 1024
    hop = 441 # 100 fps
    _, _, S_ref = scipy.signal.spectrogram(ref, fs=44100, nperseg=n_fft, noverlap=n_fft-hop)
    _, _, S_test = scipy.signal.spectrogram(test, fs=44100, nperseg=n_fft, noverlap=n_fft-hop)

    fb = get_mel_fb(44100, n_fft, 32)
    M_ref = np.log10(np.dot(fb, S_ref) + 1e-6).T
    M_test = np.log10(np.dot(fb, S_test) + 1e-6).T

    M_ref_norm = M_ref / (np.linalg.norm(M_ref, axis=1, keepdims=True) + 1e-6)
    M_test_norm = M_test / (np.linalg.norm(M_test, axis=1, keepdims=True) + 1e-6)

    cost = dtw_cost(M_test_norm, M_ref_norm)
    cos_dist = (cost ** 2) / 2.0
    avg_similarity = float(max(0.0, 1.0 - cos_dist))

    print(f"    Mel DTW Cost: {cost:.4f}")
    print(f"    Spectral Accuracy: {avg_similarity * 100:.2f}%")
    passed = avg_similarity >= 0.85
    return passed, avg_similarity

def main():
    parser = argparse.ArgumentParser(description="Verify fur2gb ROM output against Furnace ground truth")
    parser.add_argument("fur", help="Input .fur song file")
    parser.add_argument("rom", help="Compiled .gb ROM file")
    parser.add_argument("--furnace", default=r"tools/bin/furnace.exe", help="Furnace CLI path")
    parser.add_argument("--duration", type=float, default=12.0, help="Test recording duration in seconds")
    args = parser.parse_args()

    fur_file = os.path.abspath(args.fur)
    rom_file = os.path.abspath(args.rom)
    furnace_exe = os.path.abspath(args.furnace)

    base_name = os.path.splitext(os.path.basename(fur_file))[0]
    out_dir = os.path.join(os.path.dirname(__file__), "..", "tests", "output")
    os.makedirs(out_dir, exist_ok=True)

    ref_wav = os.path.join(out_dir, f"{base_name}_ref.wav")
    test_wav = os.path.join(out_dir, f"{base_name}_gb.wav")

    # 1. Render Reference
    if not render_furnace_ref(furnace_exe, fur_file, ref_wav):
        sys.exit(1)

    # 2. Record ROM from PyBoy
    if not record_pyboy_gb(rom_file, test_wav, duration_sec=args.duration):
        sys.exit(1)

    # 3. Compare Audio
    passed, score = compare_audio(ref_wav, test_wav)
    if passed:
        print(f"\n[+] VERIFICATION PASSED for {base_name}! Score: {score*100:.1f}%\n")
    else:
        print(f"\n[-] VERIFICATION FAILED for {base_name}! Score: {score*100:.1f}%\n", file=sys.stderr)

    sys.exit(0 if passed else 1)

if __name__ == "__main__":
    main()
