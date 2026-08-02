#!/usr/bin/env python3
"""Analyse a window of RADAR_DADA lines from the daemon log.

Judges the stream by its numbers, not by log text.

Usage: analyze.py SECONDS [label]
"""
import time, sys, statistics, os, re

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, 'csi.log')
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 15
LABEL = ' '.join(sys.argv[2:])

COLS = ['wander', 'wander_avg', 'someone_thr', 'room_status',
        'jitter', 'jitter_med', 'move_thr', 'human_status']
PAT = re.compile(r'RADAR_DADA,([-\d]+),([-\d]+),(.*)')


def read_rows():
    rows = []
    with open(LOG, errors='replace') as fh:
        for line in fh:
            m = PAT.search(line)
            if not m:
                continue
            try:
                vals = [float(x) for x in m.group(3).split(',')]
            except ValueError:
                continue
            if len(vals) == 8:
                rows.append([int(m.group(1)), int(m.group(2))] + vals)
    return rows


start = len(read_rows())
print(f"### {LABEL or 'capture'}: watching {SECS:.0f}s ###", flush=True)
time.sleep(SECS)
rows = read_rows()[start:]

if len(rows) < 5:
    print(f"FAIL: only {len(rows)} samples in window -- CSI is not flowing")
    sys.exit(1)

span = (rows[-1][1] - rows[0][1]) / 1000.0
rate = len(rows) / span if span > 0 else 0
seqs = [r[0] for r in rows]
gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)

print(f"samples {len(rows)}   span {span:.1f}s   rate {rate:.1f}/s   seq-gaps {gaps}")
print()
print(f"{'field':<13}{'min':>10}{'max':>10}{'mean':>10}{'stdev':>10}{'zeros':>7}  verdict")
print('-' * 78)
summary = {}
for i, name in enumerate(COLS):
    v = [r[2 + i] for r in rows]
    summary[name] = v
    nz = sum(1 for x in v if x == 0.0)
    sd = statistics.pstdev(v)
    if name in ('room_status', 'human_status'):
        verdict = f"states: {sorted(set(int(x) for x in v))}"
    elif nz == len(v):
        verdict = "ALL ZERO"
    elif sd == 0:
        verdict = "CONSTANT (suspicious)"
    else:
        verdict = "non-zero, varying  OK"
    print(f"{name:<13}{min(v):>10.6f}{max(v):>10.6f}"
          f"{statistics.mean(v):>10.6f}{sd:>10.6f}{nz:>7}  {verdict}")

j = summary['jitter']
print()
print(f"jitter mean {statistics.mean(j):.6f}   "
      f"distinct values {len(set(j))}/{len(j)}")
