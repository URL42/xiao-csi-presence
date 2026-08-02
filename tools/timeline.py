#!/usr/bin/env python3
"""Bucket the whole RADAR_DADA history so activity shows up as a shape.

Usage: timeline.py [bucket_seconds] [last_n_minutes]
"""
import sys, os, re, statistics

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, 'csi.log')
BUCKET = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
LASTMIN = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0

PAT = re.compile(r'RADAR_DADA,(\d+),(\d+),(.*)')
rows = []
with open(LOG, errors='replace') as fh:
    for line in fh:
        m = PAT.search(line)
        if not m:
            continue
        try:
            v = [float(x) for x in m.group(3).split(',')]
        except ValueError:
            continue
        if len(v) == 8 and int(m.group(2)) > 0:
            rows.append((int(m.group(2)) / 1000.0, v))

if not rows:
    sys.exit("no data")

tend = rows[-1][0]
rows = [r for r in rows if r[0] >= tend - LASTMIN * 60]
t0 = rows[0][0]

buckets = {}
for t, v in rows:
    buckets.setdefault(int((t - t0) // BUCKET), []).append(v)

print(f"bucket = {BUCKET:.0f}s, {len(rows)} samples, "
      f"span {(rows[-1][0]-t0)/60:.1f} min\n")
print(f"{'t+':>6} {'n':>4} {'wander':>9} {'jitter':>9} {'room%':>6} {'human%':>7}  wander bar")
print('-' * 84)

allw = [v[0] for _, v in rows]
lo, hi = min(allw), max(allw)
rng = (hi - lo) or 1.0

for k in sorted(buckets):
    b = buckets[k]
    w = statistics.mean(x[0] for x in b)
    j = statistics.mean(x[4] for x in b)
    room = 100.0 * sum(x[3] for x in b) / len(b)
    hum = 100.0 * sum(x[7] for x in b) / len(b)
    bar = '#' * int(38 * (w - lo) / rng)
    print(f"{k*BUCKET:>5.0f}s {len(b):>4} {w:>9.5f} {j:>9.5f} "
          f"{room:>5.0f}% {hum:>6.0f}%  {bar}")

print()
print(f"wander over window: min {lo:.5f}  max {hi:.5f}  ratio {hi/lo if lo else 0:.1f}x")
