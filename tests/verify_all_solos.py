
import os, sys, wave, subprocess
import numpy as np
import scipy.signal
from pyboy import PyBoy

sys.path.append('tests')
from verify_standalone import record_pyboy_gb, compare_audio

SONGS = [
    ('MenuLoop', 30.02),
    ('StereoMadness', 86.25),
    ('BackOnTrack', 83.52),
    ('Polargeist', 91.02),
    ('DryOut', 83.39),
    ('BaseAfterBase', 85.12),
    ('Cycles', 87.55)
]

def main():
    os.makedirs('bin', exist_ok=True)
    all_scores = {}

    for song_name, duration_sec in SONGS:
        fur_path = os.path.join('test_songs', song_name + '.fur')
        ref_prefix = 'temp_' + song_name
        if song_name == 'MenuLoop': ref_prefix = 'temp_test_menu'
        elif song_name == 'BackOnTrack': ref_prefix = 'temp_bot'
        elif song_name == 'DryOut': ref_prefix = 'temp_dry_out'
        
        print('\n==================================================')
        print('VERIFYING ' + song_name.upper() + ' (FULL SONG: ' + str(round(duration_sec, 1)) + 's)')
        print('==================================================')
        all_scores[song_name] = {}

        for ch in [1, 2, 3, 4]:
            rom_path = os.path.join('bin', song_name + '_solo' + str(ch) + '.gb')
            ref_wav = ref_prefix + '_c0' + str(ch) + '.wav'
            test_wav = 'temp_' + song_name + '_gb_solo' + str(ch) + '.wav'

            if not os.path.exists(rom_path):
                print('[*] Building ' + rom_path + '...')
                cmd = ['python', 'tools/make_rom.py', fur_path, '-o', rom_path, '--solo', str(ch)]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    print('[-] Failed to build ' + rom_path + ': ' + res.stderr)
                    all_scores[song_name]['CH' + str(ch)] = 0.0
                    continue

            print('[*] Testing CH' + str(ch) + ' solo (' + str(round(duration_sec, 1)) + 's)...')
            ok = record_pyboy_gb(rom_path, test_wav, duration_sec=duration_sec)
            if not ok:
                print('[-] Failed to record CH' + str(ch))
                all_scores[song_name]['CH' + str(ch)] = 0.0
                continue

            passed, score = compare_audio(ref_wav, test_wav)
            all_scores[song_name]['CH' + str(ch)] = score
            print('[+] ' + song_name + ' CH' + str(ch) + ': ' + str(round(score*100, 2)) + '%')

    print('\n\n' + '='*65)
    print('       COMPREHENSIVE ALL-7 SONGS SOLO CHANNEL REPORT')
    print('='*65)
    print('%-16s | %-10s | %-10s | %-10s | %-10s' % ('Song', 'CH1 (Pulse1)', 'CH2 (Pulse2)', 'CH3 (Wave)', 'CH4 (Noise)'))
    print('-'*65)
    for s_name, _ in SONGS:
        sc = all_scores[s_name]
        c1 = str(round(sc.get('CH1', 0.0)*100, 2)) + '%'
        c2 = str(round(sc.get('CH2', 0.0)*100, 2)) + '%'
        c3 = str(round(sc.get('CH3', 0.0)*100, 2)) + '%'
        c4 = str(round(sc.get('CH4', 0.0)*100, 2)) + '%'
        print('%-16s | %-12s | %-12s | %-12s | %-12s' % (s_name, c1, c2, c3, c4))
    print('='*65)

if __name__ == '__main__':
    main()
