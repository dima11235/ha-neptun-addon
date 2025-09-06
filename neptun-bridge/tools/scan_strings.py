#!/usr/bin/env python3
import os, sys, glob, re

CAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'captures'))

KEYWORDS = [
    b'ssid', b'SSID', b'WiFi', b'Wi-Fi', b'wifi', b'wlan', b'WLAN',
    b'psk', b'pass', b'password', b'wpa', b'wep', b'network', b'AP', b'Access-Point',
    b'http', b'HTTP/1.1', b'POST ', b'GET ', b'Content-Type', b'Host:', b'User-Agent',
    b'mqtt', b'MQTT', b'MQIsdp'
]

def printable_runs(data: bytes, minlen=5):
    out = []
    cur = bytearray()
    for b in data:
        if 32 <= b <= 126:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                out.append(bytes(cur))
            cur.clear()
    if len(cur) >= minlen:
        out.append(bytes(cur))
    return out

def scan_file(path: str):
    with open(path, 'rb') as f:
        data = f.read()
    strings = printable_runs(data, minlen=5)
    hits = []
    for s in strings:
        ls = s.lower()
        if any(k.lower() in ls for k in KEYWORDS):
            hits.append(s)
    return strings, hits

def main():
    files = glob.glob(os.path.join(CAP_DIR, '*.pcapng')) + glob.glob(os.path.join(CAP_DIR, '*.pcap'))
    if not files:
        print('No capture files found in', CAP_DIR, file=sys.stderr)
        sys.exit(1)
    summary = []
    report_path = os.path.join(CAP_DIR, 'strings_scan_report.txt')
    with open(report_path, 'w', encoding='utf-8') as rpt:
        for p in files:
            strings, hits = scan_file(p)
            rpt.write(f'File: {os.path.basename(p)}\n')
            rpt.write(f'  strings_found: {len(strings)}\n')
            rpt.write(f'  keyword_hits: {len(hits)}\n')
            # Dedup and show up to 50 hits
            shown = set()
            rpt.write('  sample_hits:\n')
            cnt = 0
            for h in hits:
                t = h.decode('utf-8', errors='ignore')
                if t in shown:
                    continue
                shown.add(t)
                rpt.write('   - ' + t + '\n')
                cnt += 1
                if cnt >= 50:
                    break
            rpt.write('\n')
    print('Wrote', report_path)

if __name__ == '__main__':
    main()

