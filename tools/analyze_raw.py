#!/usr/bin/env python3
"""Analyse raw CSI_DATA lines: callback rate, valid_len, and whether the
I/Q subcarrier array actually contains varying data.

This is the check that matters. The 'LTF type %d has no data' warning
prints a config value and can lie; valid_len and the subcarrier bytes
cannot. On the C6 this is where the truth showed up as len 0 / all zeros.

Usage: analyze_raw.py SECONDS [label]
"""
import time, sys, os, re, statistics

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, 'csi.log')
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 10
LABEL = ' '.join(sys.argv[2:])

PAT = re.compile(r'CSI_DATA,(.*?),"\[(.*?)\]"')


def read():
    out = []
    with open(LOG, errors='replace') as fh:
        for line in fh:
            m = PAT.search(line)
            if not m:
                continue
            head = m.group(1).split(',')
            try:
                iq = [int(x) for x in m.group(2).split(',')]
            except ValueError:
                continue
            out.append((head, iq))
    return out


start = len(read())
print(f"### {LABEL or 'raw CSI'}: watching {SECS:.0f}s ###", flush=True)
time.sleep(SECS)
rows = read()[start:]

if len(rows) < 5:
    print(f"FAIL: only {len(rows)} raw CSI frames captured")
    sys.exit(1)

t0, t1 = int(rows[0][0][1]), int(rows[-1][0][1])
span = (t1 - t0) / 1000.0
print(f"frames {len(rows)}   span {span:.1f}s   CALLBACK RATE {len(rows)/span:.1f}/s")

rssi = [int(r[0][5]) for r in rows]
chan = {int(r[0][18]) for r in rows}
vlen = {len(r[1]) for r in rows}
declared = {int(r[0][26]) for r in rows}
print(f"rssi mean {statistics.mean(rssi):.1f} dBm   channel(s) {sorted(chan)}")
print(f"declared valid_len {sorted(declared)}   actual array len {sorted(vlen)}")
print()

allvals = [v for _, iq in rows for v in iq]
zero_frames = sum(1 for _, iq in rows if all(v == 0 for v in iq))
print(f"subcarrier values: min {min(allvals)}  max {max(allvals)}  "
      f"stdev {statistics.pstdev(allvals):.2f}")
print(f"frames that are entirely zero: {zero_frames}/{len(rows)}")

# Does a given subcarrier change over time? Static buffers were the C5/C61 bug.
n = min(len(iq) for _, iq in rows)
moving = 0
for k in range(n):
    col = [iq[k] for _, iq in rows]
    if len(set(col)) > 1:
        moving += 1
print(f"subcarriers that vary across frames: {moving}/{n}")
print()
if zero_frames == len(rows):
    print("VERDICT: ALL ZERO -- no usable CSI (the C6 failure mode)")
elif moving < n * 0.5:
    print("VERDICT: buffer looks STATIC -- suspicious")
else:
    print("VERDICT: real, varying CSI  OK")
print()
print("sample frame (first 24 values):")
print(" ", rows[-1][1][:24])
