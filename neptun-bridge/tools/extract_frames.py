#!/usr/bin/env python3
import sys, os, glob, json

SIG0, SIG1, SIG2 = 0x02, 0x54, 0x51

def crc16_ccitt(data: bytes) -> int:
    c = 0xFFFF
    for b in data:
        c ^= (b & 0xFF) << 8
        for _ in range(8):
            if c & 0x8000:
                c = ((c << 1) ^ 0x1021) & 0xFFFF
            else:
                c = (c << 1) & 0xFFFF
    return c & 0xFFFF

def tname(t: int) -> str:
    return {
        0x52: "system_state",
        0x53: "sensor_state",
        0x43: "counter_state",
        0x4E: "sensor_name",
        0x63: "counter_name",
        0xFB: "ack",
        0xFE: "busy",
        0x57: "settings",
    }.get(t, f"0x{t:02X}")

def parse_tlv_settings(p: bytes):
    # Return dict of tlv summaries
    out = {}
    i = 0
    while i + 3 <= len(p):
        tag = p[i]; ln = (p[i+1] << 8) | p[i+2]; i += 3
        v = p[i:i+ln]; i += ln
        if tag == 0x53 and ln >= 4:
            out['tlv_53'] = {
                'valve_open': 1 if v[0] else 0,
                'dry_flag': 1 if v[1] else 0,
                'close_on_offline': 1 if v[2] else 0,
                'line_cfg': v[3]
            }
        elif tag == 0x43 and ln == 0x14:
            ls = []
            for j in range(0, 20, 5):
                val = (v[j]<<24)|(v[j+1]<<16)|(v[j+2]<<8)|v[j+3]
                step = v[j+4]
                ls.append({'value_l': val, 'step': step})
            out['tlv_43'] = ls
        else:
            out[f'tlv_{tag:02X}'] = v.hex()
    return out

def extract_from_file(path: str):
    with open(path, 'rb') as f:
        data = f.read()
    found = []
    i = 0
    n = len(data)
    while True:
        j = data.find(bytes([SIG0, SIG1, SIG2]), i)
        if j < 0:
            break
        if j+6 <= n:
            t = data[j+3]
            L = (data[j+4] << 8) | data[j+5]
            T = 6 + L + 2
            if j+T <= n:
                frag = data[j:j+T]
                # CRC check
                crc_ok = ((frag[-2] << 8) | frag[-1]) == crc16_ccitt(frag[:-2])
                if crc_ok:
                    item = {
                        'file': os.path.basename(path),
                        'offset': j,
                        'type': t,
                        'type_name': tname(t),
                        'len': L,
                        'hex': frag.hex(),
                    }
                    if t == 0x57:
                        item['tlv'] = parse_tlv_settings(frag[6:-2])
                    found.append(item)
                i = j + T
            else:
                break
        else:
            break
    return found

def main():
    cap_dir = os.path.join(os.path.dirname(__file__), '..', 'captures')
    cap_dir = os.path.abspath(cap_dir)
    files = glob.glob(os.path.join(cap_dir, '*.pcapng')) + glob.glob(os.path.join(cap_dir, '*.pcap'))
    if not files:
        print('No capture files found under', cap_dir, file=sys.stderr)
        sys.exit(1)
    out_jsonl = os.path.join(cap_dir, 'extracted_frames.jsonl')
    out_sum = os.path.join(cap_dir, 'extracted_summary.txt')
    total = []
    for p in files:
        total += extract_from_file(p)
    # Write JSONL
    with open(out_jsonl, 'w', encoding='utf-8') as jf:
        for it in total:
            jf.write(json.dumps(it, ensure_ascii=False) + '\n')
    # Build summary
    by_type = {}
    for it in total:
        by_type.setdefault(it['type_name'], 0)
        by_type[it['type_name']] += 1
    with open(out_sum, 'w', encoding='utf-8') as sf:
        sf.write('Frames found: %d\n' % len(total))
        for k, v in sorted(by_type.items(), key=lambda kv: kv[0]):
            sf.write(f"{k}: {v}\n")
        sf.write('\nFirst 10 frames with details:\n')
        for it in total[:10]:
            sf.write(json.dumps(it, ensure_ascii=False) + '\n')
    print('Wrote', out_jsonl, 'and', out_sum)

if __name__ == '__main__':
    main()

