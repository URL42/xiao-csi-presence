#!/usr/bin/env python3
"""Hold the serial port open so board state survives between commands.

Opening the port on macOS asserts DTR/RTS, which resets the ESP32-S3.
That wipes the WiFi association and any calibration thresholds, since
console_test keeps both in RAM only. So one process owns the port for the
whole session; commands arrive through a file and all output is appended
to a log the analysis scripts read.

  start:  python csi_daemon.py &
  send:   echo "radar --train_start" >> $CMD
  read:   tail -f $LOG
"""
import serial, time, os, sys

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, 'csi.log')
CMD = os.path.join(BASE, 'csi.cmd')
PORT = '/dev/cu.usbmodem101'

open(LOG, 'w').close()
open(CMD, 'w').close()

s = serial.Serial(PORT, 115200, timeout=0.1)
time.sleep(0.3)
s.setDTR(False)

log = open(LOG, 'a', buffering=1, errors='replace')


def note(msg):
    log.write(f"\n### {msg} ###\n")


note("daemon attached, waiting for boot to settle")
last, t0 = time.time(), time.time()
while time.time() - t0 < 25:
    c = s.read(4096)
    if c:
        log.write(c.decode('utf-8', 'replace'))
        last = time.time()
    elif time.time() - last > 1.5:
        break
note("board ready")

sent = 0
try:
    while True:
        c = s.read(8192)
        if c:
            log.write(c.decode('utf-8', 'replace'))

        try:
            with open(CMD) as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            lines = []

        while sent < len(lines):
            cmd = lines[sent].strip()
            sent += 1
            if not cmd:
                continue
            if cmd == '__QUIT__':
                note("daemon exiting")
                s.close()
                sys.exit(0)
            note(f"CMD: {cmd}")
            s.write(cmd.encode() + b'\r\n')
            s.flush()

        time.sleep(0.05)
except KeyboardInterrupt:
    s.close()
