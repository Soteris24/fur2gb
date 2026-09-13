#!/usr/bin/env python3
"""
fur2gb.py - Furnace (.fur) to Game Boy C/ASM converter.
Uses Furnace headless CLI to export text format, then compiles
into compact, byte-aligned Game Boy driver tables and bytecode.
"""

import sys
import os
import argparse
import subprocess
import re
import zlib
import struct

NOTE_MAP = {
    'C-': 0, 'C#': 1, 'D-': 2, 'D#': 3, 'E-': 4, 'F-': 5,
    'F#': 6, 'G-': 7, 'G#': 8, 'A-': 9, 'A#': 10, 'B-': 11
}

def parse_note(note_str):
    if not note_str or note_str == '...':
        return 0 # FUR_NOTE_NONE
    if note_str in ('OFF', '==='):
        return 254 # FUR_NOTE_OFF
    
    match = re.match(r'^([A-G][#-]?)(-?\d+)$', note_str)
    if match:
        name, oct_str = match.groups()
        octave = int(oct_str)
        if name in NOTE_MAP:
            semi = NOTE_MAP[name]
            # Note 60 is C-0 in Furnace
            return 60 + octave * 12 + semi
    return 0

def parse_fx(fx_str):
    if not fx_str or fx_str == '....' or len(fx_str) < 4:
        return (0, 0)
    fx_t_str = fx_str[:2]
    fx_v_str = fx_str[2:]
    if fx_t_str == '..':
        return (0, 0)
    try:
        fx_t = int(fx_t_str, 16)
        fx_v = int(fx_v_str, 16) if fx_v_str != '..' else 0
        return (fx_t, fx_v)
    except Exception:
        return (0, 0)

def expand_adsr(params, max_ticks=48):
    if len(params) < 9:
        return params
    low, high, ar, ht, dr, sl, st, sr, rr = params[:9]
    pos = (low << 8) | (0xFF if low > high else 0)
    top = (high << 8) | 0xFF
    bottom = low << 8
    sus = (sl << 8) | 0xFF
    top_inv = high << 8
    bottom_inv = (low << 8) | 0xFF
    sus_inv = sl << 8

    lastPos = 0
    delay = 0
    out = []

    for _ in range(max_ticks):
        if delay > 0:
            delay -= 1
        else:
            if low <= high:
                if lastPos == 0:
                    pos += ar
                    if pos >= top:
                        pos = top
                        lastPos = 1
                        delay = ht
                elif lastPos == 1:
                    pos -= dr
                    if pos <= sus:
                        pos = sus
                        lastPos = 2
                        delay = st
                elif lastPos == 2:
                    pos -= sr
                    if pos <= bottom:
                        pos = bottom
                        lastPos = 4
                elif lastPos == 4:
                    pos = bottom
            else:
                if lastPos == 0:
                    pos -= ar
                    if pos <= top_inv:
                        pos = top_inv
                        lastPos = 1
                        delay = ht
                elif lastPos == 1:
                    pos += dr
                    if pos >= sus_inv:
                        pos = sus_inv
                        lastPos = 2
                        delay = st
                elif lastPos == 2:
                    pos += sr
                    if pos >= bottom_inv:
                        pos = bottom_inv
                        lastPos = 4
                elif lastPos == 4:
                    pos = bottom_inv
        val = pos >> 8
        if low <= high:
            val = max(low, min(high, val))
        else:
            val = max(high, min(low, val))
        out.append(val)
        if lastPos == 4 and len(out) >= 4 and all(v == out[-1] for v in out[-4:]):
            break
    return out

def expand_lfo(params, max_ticks=32):
    if len(params) < 14:
        return params
    low, high = params[0], params[1]
    speed = params[11]
    wave = params[12] & 3
    phase = params[13]

    bottom = low << 8
    top = (high << 8) | 0xFF
    pos = bottom
    lfoDir = False
    out = []
    for _ in range(max_ticks):
        if wave == 0: # triangle
            if lfoDir:
                pos -= speed
                if pos < bottom:
                    lfoDir = False
                    pos += speed * 2
            else:
                pos += speed
                if pos > top:
                    lfoDir = True
                    pos -= speed * 2
            val = pos >> 8
        elif wave == 1: # saw
            pos += speed
            if pos > top:
                pos -= (top - bottom)
            val = pos >> 8
        else: # pulse
            pos = (pos + speed) & 0xFFFF
            val = high if (pos & 0x8000) else low
        out.append(val)
    return out

def parse_txt_export(content):
    lines = content.splitlines()
    i = 0
    num_lines = len(lines)

    instruments = []
    wavetables = []
    subsongs = []

    cur_ins = None
    cur_subsong = None

    in_subsongs = False

    while i < num_lines:
        line = lines[i].strip()
        
        if line.startswith('# Subsongs'):
            in_subsongs = True
            cur_ins = None

        # Parse Instruments
        elif not in_subsongs and line.startswith('## ') and ': ' in line and not line.startswith('## Patterns'):
            # Instrument header
            m = re.match(r'^##\s+([0-9A-Fa-f]+):\s*(.*)', line)
            if m:
                ins_id = int(m.group(1), 16)
                ins_name = m.group(2)
                cur_ins = {
                    'id': ins_id,
                    'name': ins_name,
                    'volume': 15,
                    'direction': 0,
                    'length': 2,
                    'sound_len': 64,
                    'soft_env': 0,
                    'vol_macro': None,
                    'arp_macro': None,
                    'duty_macro': None,
                    'wave_macro': None
                }
                instruments.append(cur_ins)
        
        elif cur_ins and line.startswith('- volume:'):
            cur_ins['volume'] = int(line.split(':')[1].strip())
        elif cur_ins and line.startswith('- direction:'):
            cur_ins['direction'] = 1 if 'up' in line.lower() else 0
        elif cur_ins and line.startswith('- length:'):
            cur_ins['length'] = int(line.split(':')[1].strip())
        elif cur_ins and line.startswith('- sound length:'):
            cur_ins['sound_len'] = int(line.split(':')[1].strip())
        elif cur_ins and line.startswith('- use software envelope:'):
            cur_ins['soft_env'] = 1 if 'yes' in line.lower() else 0
        
        # Macros
        elif cur_ins and re.match(r'^\s*-\s*(vol|arp|duty|wave):\s*(.*)', line):
            m = re.match(r'^\s*-\s*(vol|arp|duty|wave):\s*(.*)', line)
            m_type = m.group(1)
            raw_tokens = m.group(2).split()
            
            values = []
            loop = 0xFF
            for tok in raw_tokens:
                if tok == '|':
                    loop = len(values)
                elif tok.startswith('[') or tok == '/':
                    continue
                else:
                    try:
                        v = int(tok)
                        if v >= 0x40000000:
                            v = v & 0x3FFFFFFF
                        values.append(v)
                    except ValueError:
                        pass
            
            if '[ADSR]' in line:
                values = expand_adsr(values)
            elif '[LFO]' in line:
                values = expand_lfo(values)
            
            macro_obj = {'len': len(values), 'loop': loop if loop < len(values) else 0xFF, 'values': values}
            if m_type == 'vol': cur_ins['vol_macro'] = macro_obj
            elif m_type == 'arp': cur_ins['arp_macro'] = macro_obj
            elif m_type == 'duty': cur_ins['duty_macro'] = macro_obj
            elif m_type == 'wave': cur_ins['wave_macro'] = macro_obj

        # Parse Wavetables
        elif line.startswith('- ') and '(32x16):' in line:
            # e.g. - 0 (32x16): 7 8 10 11 ... 32
            parts = line.split(':')[1].strip().split()
            samples = [int(p) for p in parts[:32]]
            # Pack 32 nibbles into 16 bytes (high nibble first)
            # Furnace defaults to invertWave = true for Game Boy (nibbles ^= 15)
            packed = []
            for s_idx in range(0, 32, 2):
                n1 = (samples[s_idx] ^ 15) & 0x0F
                n2 = (samples[s_idx + 1] ^ 15) & 0x0F
                packed.append((n1 << 4) | n2)
            wavetables.append(packed)

        # Parse Subsong
        elif in_subsongs and line.startswith('## ') and not line.startswith('## Patterns'):
            cur_subsong = {
                'tick_rate': 60,
                'virtual_tempo_n': 150,
                'virtual_tempo_d': 150,
                'speeds': [6],
                'pattern_len': 64,
                'orders': [[], [], [], []],
                'patterns': {} # (order_idx, chan_idx) -> list of rows
            }
            subsongs.append(cur_subsong)
        elif cur_subsong and line.startswith('- tick rate:'):
            cur_subsong['tick_rate'] = int(float(line.split(':')[1].strip()))
        elif cur_subsong and line.startswith('- virtual tempo:'):
            m_vt = re.search(r'(\d+)/(\d+)', line)
            if m_vt:
                cur_subsong['virtual_tempo_n'] = int(m_vt.group(1))
                cur_subsong['virtual_tempo_d'] = int(m_vt.group(2))
        elif cur_subsong and line.startswith('- speeds:'):
            parts = line.split(':')[1].strip().split()
            cur_subsong['speeds'] = [int(p) for p in parts]
        elif cur_subsong and line.startswith('- pattern length:'):
            cur_subsong['pattern_len'] = int(line.split(':')[1].strip())
        
        # Orders table
        elif cur_subsong and line == 'orders:':
            i += 1 # skip '```'
            i += 1
            while i < num_lines and lines[i].strip() != '```':
                ord_line = lines[i].strip()
                if '|' in ord_line:
                    vals = ord_line.split('|')[1].strip().split()
                    for ch_idx in range(4):
                        if ch_idx < len(vals):
                            val = int(vals[ch_idx], 16) if vals[ch_idx] != '..' else 0xFF
                            cur_subsong['orders'][ch_idx].append(val)
                i += 1

        # Patterns
        elif cur_subsong and line.startswith('----- ORDER '):
            order_idx = int(line.split()[2], 16)
            i += 1
            # Read rows for this order
            order_rows = [[] for _ in range(4)]
            while i < num_lines and re.match(r'^[0-9A-Fa-f]{2,3}\s*\|', lines[i].strip()):
                row_line = lines[i].strip()
                ch_parts = row_line.split('|')[1:]
                for ch_idx in range(4):
                    if ch_idx < len(ch_parts):
                        tokens = ch_parts[ch_idx].strip().split()
                        note_str = tokens[0] if len(tokens) > 0 else '...'
                        ins_str  = tokens[1] if len(tokens) > 1 else '..'
                        vol_str  = tokens[2] if len(tokens) > 2 else '..'
                        fx1_str  = tokens[3] if len(tokens) > 3 else '....'
                        fx2_str  = tokens[4] if len(tokens) > 4 else '....'
                        order_rows[ch_idx].append({
                            'note': parse_note(note_str),
                            'ins': int(ins_str, 16) if ins_str != '..' else 0xFF,
                            'vol': int(vol_str, 16) if vol_str != '..' else 0xFF,
                            'fx1': parse_fx(fx1_str),
                            'fx2': parse_fx(fx2_str),
                        })
                i += 1
            for ch_idx in range(4):
                cur_subsong['patterns'][(order_idx, ch_idx)] = order_rows[ch_idx]
            continue

        i += 1

    return {
        'instruments': instruments,
        'wavetables': wavetables,
        'subsong': subsongs[0] if subsongs else None
    }

def parse_fur_binary(filepath):
    with open(filepath, 'rb') as f:
        raw = f.read()
    try:
        data = zlib.decompress(raw)
    except Exception:
        data = raw

    cur = 32
    instruments = []
    wavetables = []
    
    while cur < len(data) - 8:
        magic = data[cur:cur+4].decode('ascii', errors='ignore')
        blen = struct.unpack_from('<I', data, cur+4)[0]
        payload = data[cur+8:cur+8+blen]
        
        if magic == 'WAVE':
            null_pos = payload.find(b'\x00')
            if null_pos != -1:
                p = null_pos + 1
                wlen, wmin, wmax = struct.unpack_from('<iii', payload, p)
                p += 12
                samples = []
                for s in range(wlen):
                    val = struct.unpack_from('<i', payload, p + s * 4)[0]
                    samples.append(val)
                wavetables.append({'len': wlen, 'max': wmax, 'samples': samples})
            
        elif magic == 'INS2':
            ins_info = {'ws': None}
            ws_idx = payload.find(b'WS')
            if ws_idx != -1 and ws_idx + 4 + 17 <= len(payload):
                ws_len = struct.unpack_from('<H', payload, ws_idx+2)[0]
                if ws_len >= 17:
                    w1, w2 = struct.unpack_from('<ii', payload, ws_idx+4)
                    rdiv, fx, en, glob_f, spd, p1, p2, p3, p4 = struct.unpack_from('<BBBBBBBBB', payload, ws_idx+12)
                    ins_info['ws'] = {
                        'wave1': w1,
                        'wave2': w2,
                        'rateDivider': rdiv,
                        'effect': fx,
                        'enabled': bool(en),
                        'global': bool(glob_f),
                        'speed': spd,
                        'param1': p1,
                        'param2': p2,
                        'param3': p3,
                        'param4': p4
                    }
            instruments.append(ins_info)
        cur += 8 + blen

    return instruments, wavetables

class DivWaveSynthSim:
    def __init__(self, w1_samples, w2_samples, ws_dict):
        self.width = 32
        self.height = 15
        self.wave1 = [int(x * self.height / 15) for x in w1_samples[:32]]
        self.wave2 = [int(x * self.height / 15) for x in w2_samples[:32]]
        self.output = list(self.wave1)
        self.state = ws_dict
        self.pos = 0
        self.stage = 0
        self.stage_dir = False
        self.div_counter = 0

    def tick(self):
        self.div_counter -= 1
        if self.div_counter <= 0:
            fx = self.state['effect']
            spd = self.state['speed']
            p1 = self.state['param1']
            p2 = self.state['param2']
            
            for i in range(spd + 1):
                if fx == 130: # DIV_WS_FADE
                    self.output[self.pos] = self.wave1[self.pos] + (((self.wave2[self.pos] - self.wave1[self.pos]) * self.stage) >> 9)
                    self.pos += 1
                    if self.pos >= self.width:
                        self.pos = 0
                        self.stage += 1 + p1
                        if self.stage > 512:
                            self.stage = 512
                elif fx == 131: # DIV_WS_PING_PONG
                    self.output[self.pos] = self.wave1[self.pos] + (((self.wave2[self.pos] - self.wave1[self.pos]) * self.stage) >> 8)
                    self.pos += 1
                    if self.pos >= self.width:
                        self.pos = 0
                        if self.stage_dir:
                            self.stage -= 1 + p1
                            if self.stage <= 0:
                                self.stage_dir = False
                                self.stage = 0
                        else:
                            self.stage += 1 + p1
                            if self.stage >= 256:
                                self.stage_dir = True
                                self.stage = 256
                elif fx == 132: # DIV_WS_OVERLAY
                    self.output[self.pos] += self.wave2[self.pos]
                    if self.output[self.pos] >= self.height:
                        self.output[self.pos] -= self.height
                    self.pos += 1
                    if self.pos >= self.width:
                        self.pos = 0
                elif fx == 133: # DIV_WS_NEGATIVE_OVERLAY
                    self.output[self.pos] -= self.wave2[self.pos]
                    if self.output[self.pos] < 0:
                        self.output[self.pos] += self.height
                    self.pos += 1
                    if self.pos >= self.width:
                        self.pos = 0
                elif fx == 135: # DIV_WS_MIX
                    self.output[self.pos] = (self.wave1[self.pos] + self.wave2[(self.pos + self.stage) % self.width]) >> 1
                    self.pos += 1
                    if self.pos >= self.width:
                        self.pos = 0
                        self.stage += p1
                        while self.stage >= self.width:
                            self.stage -= self.width

            self.div_counter = max(1, self.state['rateDivider'])

        # Invert wave (nibble ^= 15) for Game Boy
        packed = []
        for s in range(0, 32, 2):
            n1 = (max(0, min(15, self.output[s])) ^ 15) & 0x0F
            n2 = (max(0, min(15, self.output[s+1])) ^ 15) & 0x0F
            packed.append((n1 << 4) | n2)
        return packed

def bake_wavesynths(song_info, fur_file):
    bin_ins, bin_waves = parse_fur_binary(fur_file)
    waves = song_info['wavetables']
    sub = song_info.get('subsong')
    used_ins = set()
    if sub:
        for ch_idx in range(4):
            for pat_idx in sub['orders'][ch_idx]:
                for r in sub['patterns'].get((pat_idx, ch_idx), []):
                    if r.get('ins') is not None and r['ins'] != 255:
                        used_ins.add(r['ins'])
    
    for ins_idx, ins in enumerate(song_info['instruments']):
        if ins_idx >= len(bin_ins):
            break
        if ins_idx not in used_ins:
            continue
        ws = bin_ins[ins_idx]['ws']
        if not ws or not ws['enabled'] or ws['effect'] == 0:
            continue
        if ws['wave1'] >= len(bin_waves) or ws['wave2'] >= len(bin_waves):
            continue
            
        print(f"[*] Baking WaveSynth for Ins {ins_idx} ('{ins['name']}'): effect={ws['effect']}, spd={ws['speed']}")
        w1 = bin_waves[ws['wave1']]['samples']
        w2 = bin_waves[ws['wave2']]['samples']
        sim = DivWaveSynthSim(w1, w2, ws)
        
        frame_indices = []
        seen = {}
        loop_start = None
        max_ticks = 48
        
        for t in range(max_ticks):
            packed_frame = sim.tick()
            state_key = (sim.pos, sim.stage, sim.stage_dir, tuple(sim.output))
            if state_key in seen:
                loop_start = seen[state_key]
                break
            seen[state_key] = t
            
            # Find in waves or append
            if packed_frame in waves:
                w_idx = waves.index(packed_frame)
            else:
                w_idx = len(waves)
                waves.append(packed_frame)
            frame_indices.append(w_idx)
            
        if loop_start is None:
            loop_start = len(frame_indices) - 1
            
        ins['wave_macro'] = {
            'len': len(frame_indices),
            'loop': loop_start,
            'values': frame_indices
        }
        print(f"    -> Generated {len(frame_indices)} frames (loop={loop_start}), master wavetables now: {len(waves)}")

def encode_pattern(rows):
    """
    Encode rows into bytecode with RLE empty rows:
    - 0x80 | count: skip count empty rows (1..127)
    - mask byte:
      bit 0: note present (1 byte follows)
      bit 1: ins present (1 byte follows)
      bit 2: vol present (1 byte follows)
      bit 3: fx1 present (2 bytes follow: type, val)
      bit 4: fx2 present (2 bytes follow: type, val)
    - 0xFF: FUR_PATTERN_END
    """
    bytes_out = []
    rle_empty = 0

    def flush_rle():
        nonlocal rle_empty
        while rle_empty > 0:
            chunk = min(rle_empty, 127)
            bytes_out.append(0x80 | chunk)
            rle_empty -= chunk

    for r in rows:
        is_empty = (r['note'] == 0 and r['ins'] == 0xFF and r['vol'] == 0xFF and r['fx1'] == (0, 0) and r['fx2'] == (0, 0))
        if is_empty:
            rle_empty += 1
            if rle_empty == 127:
                flush_rle()
        else:
            flush_rle()
            mask = 0
            row_data = []
            if r['note'] != 0:
                mask |= 0x01
                row_data.append(r['note'])
            if r['ins'] != 0xFF:
                mask |= 0x02
                row_data.append(r['ins'])
            if r['vol'] != 0xFF:
                mask |= 0x04
                row_data.append(r['vol'])
            if r['fx1'] != (0, 0):
                mask |= 0x08
                row_data.append(r['fx1'][0])
                row_data.append(r['fx1'][1])
            if r['fx2'] != (0, 0):
                mask |= 0x10
                row_data.append(r['fx2'][0])
                row_data.append(r['fx2'][1])
            
            bytes_out.append(mask)
            bytes_out.extend(row_data)

    flush_rle()
    bytes_out.append(0xFF) # FUR_PATTERN_END
    return bytes_out

def generate_c_code(song_info, title="Song"):
    ins_list = song_info['instruments']
    waves = song_info['wavetables']
    sub = song_info['subsong']
    order_len = len(sub['orders'][0])

    lines = []
    lines.append('/* Generated by fur2gb */')
    lines.append('#include "fur_driver.h"')
    lines.append('')

    # Wavetables
    lines.append(f'/* Wavetables: {len(waves)} */')
    if waves:
        lines.append(f'static const uint8_t test_song_waves[{len(waves)}][16] = {{')
        for w_idx, w in enumerate(waves):
            hex_bytes = ', '.join(f'0x{b:02X}' for b in w)
            lines.append(f'    /* Wave {w_idx:2d} */ {{ {hex_bytes} }},')
        lines.append('};')
    else:
        lines.append('static const uint8_t test_song_waves[1][16] = {{ {0} }};')
    lines.append('')

    # Instrument Macros
    lines.append('/* Instrument Macros */')
    macro_id = 0
    ins_macro_refs = []
    for ins_idx, ins in enumerate(ins_list):
        refs = {'vol': 'NULL', 'arp': 'NULL', 'duty': 'NULL', 'wave': 'NULL'}
        for m_name in ('vol', 'arp', 'duty', 'wave'):
            m = ins[f'{m_name}_macro']
            if m and m['len'] > 0:
                var_vals = f'macro_{macro_id}_vals'
                var_mac  = f'macro_{macro_id}'
                vals_str = ', '.join(str(v if v <= 127 else v - 256) for v in m['values'])
                lines.append(f'static const int8_t {var_vals}[{m["len"]}] = {{ {vals_str} }};')
                lines.append(f'static const fur_macro_t {var_mac} = {{ {m["len"]}, 0x{m["loop"]:02X}, {var_vals} }};')
                refs[m_name] = f'&{var_mac}'
                macro_id += 1
        ins_macro_refs.append(refs)
    lines.append('')

    # Instruments Table
    lines.append(f'/* Instruments: {len(ins_list)} */')
    if ins_list:
        lines.append(f'static const fur_instrument_t test_song_instruments[{len(ins_list)}] = {{')
        for idx, ins in enumerate(ins_list):
            r = ins_macro_refs[idx]
            lines.append(f'    /* {idx:2d}: {ins["name"]} */ {{ {ins["volume"]}, {ins["direction"]}, {ins["length"]}, {ins["sound_len"]}, {ins["soft_env"]}, {r["vol"]}, {r["arp"]}, {r["duty"]}, {r["wave"]} }},')
        lines.append('};')
    else:
        lines.append('static const fur_instrument_t test_song_instruments[1] = { { 15, 0, 2, 64, 0, NULL, NULL, NULL, NULL } };')
    lines.append('')

    # Patterns
    lines.append('/* Channel Patterns */')
    # Unique patterns map: bytes tuple -> pat_id
    pat_map = [{}, {}, {}, {}]
    pat_order_lists = [[], [], [], []]

    for ch in range(4):
        for ord_idx in range(order_len):
            rows = sub['patterns'].get((ord_idx, ch), [])
            encoded = tuple(encode_pattern(rows))
            if encoded not in pat_map[ch]:
                p_id = len(pat_map[ch])
                pat_map[ch][encoded] = p_id
                # Output array
                c_bytes = ', '.join(f'0x{b:02X}' for b in encoded)
                lines.append(f'static const uint8_t pat_ch{ch}_{p_id}[] = {{ {c_bytes} }};')
            else:
                p_id = pat_map[ch][encoded]
            pat_order_lists[ch].append(p_id)
        lines.append('')

    # Pattern Pointers Table
    for ch in range(4):
        total_pats = len(pat_map[ch])
        lines.append(f'static const uint8_t * const patterns_ch{ch+1}[{total_pats}] = {{')
        for p_id in range(total_pats):
            lines.append(f'    pat_ch{ch}_{p_id},')
        lines.append('};')
        lines.append('')

    # Order Lists
    for ch in range(4):
        ords = ', '.join(str(p) for p in pat_order_lists[ch])
        lines.append(f'static const uint8_t order_ch{ch+1}[{order_len}] = {{ {ords} }};')
    lines.append('')

    # Speeds Table
    speeds_str = ', '.join(str(s) for s in sub['speeds'])
    lines.append(f'static const uint8_t song_speeds[{len(sub["speeds"])}] = {{ {speeds_str} }};')
    lines.append('')

    # Master Song Struct
    lines.append('const fur_song_t test_song = {')
    lines.append(f'    "{title}",')
    lines.append(f'    {sub["tick_rate"]},')
    lines.append(f'    {sub.get("virtual_tempo_n", 150)},')
    lines.append(f'    {sub.get("virtual_tempo_d", 150)},')
    lines.append(f'    {len(sub["speeds"])},')
    lines.append('    song_speeds,')
    lines.append(f'    {sub["pattern_len"]},')
    lines.append(f'    {order_len},')
    lines.append('    0, /* loop order */')
    lines.append('    order_ch1,')
    lines.append('    order_ch2,')
    lines.append('    order_ch3,')
    lines.append('    order_ch4,')
    lines.append('    patterns_ch1,')
    lines.append('    patterns_ch2,')
    lines.append('    patterns_ch3,')
    lines.append('    patterns_ch4,')
    lines.append(f'    {len(waves)},')
    lines.append('    test_song_waves,')
    lines.append(f'    {len(ins_list)},')
    lines.append('    test_song_instruments')
    lines.append('};')
    lines.append('')

    return '\n'.join(lines)

def main():
    parser = argparse.ArgumentParser(description="Convert Furnace .fur to Game Boy C tables")
    parser.add_argument("input", help="Path to input .fur song")
    parser.add_argument("-o", "--output", default="bin/song_data.c", help="Output .c file path")
    parser.add_argument("--furnace", default=r"tools/bin/furnace.exe", help="Path to furnace.exe")
    args = parser.parse_args()

    furnace_exe = os.path.abspath(args.furnace)
    if not os.path.exists(furnace_exe):
        # Fallback check
        alt_exe = os.path.join(os.path.dirname(__file__), "bin", "furnace.exe")
        if os.path.exists(alt_exe):
            furnace_exe = alt_exe
        else:
            print(f"Error: furnace.exe not found at {furnace_exe}", file=sys.stderr)
            sys.exit(1)

    fur_file = os.path.abspath(args.input)
    temp_txt = os.path.abspath("temp_export.txt")

    print(f"[*] Exporting text from {fur_file} via {furnace_exe}...")
    cmd = [furnace_exe, "-txtout", temp_txt, fur_file]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if not os.path.exists(temp_txt):
        print("Furnace error:", res.stderr, file=sys.stderr)
        sys.exit(1)

    with open(temp_txt, "r", encoding="utf-8", errors="ignore") as f:
        txt_content = f.read()
    if os.path.exists(temp_txt):
        os.remove(temp_txt)

    print("[*] Parsing exported song data...")
    song_info = parse_txt_export(txt_content)
    base_name = os.path.splitext(os.path.basename(fur_file))[0]

    print("[*] Baking dynamic WaveSynth tables if present...")
    bake_wavesynths(song_info, fur_file)

    print("[*] Generating Game Boy C tables...")
    c_code = generate_c_code(song_info, title=base_name)

    out_path = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(c_code)

    print(f"[+] Successfully wrote song data to {out_path}")

if __name__ == "__main__":
    main()
