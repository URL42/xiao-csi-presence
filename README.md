# WiFi CSI presence detection on a Seeed XIAO ESP32S3

Human presence and motion detection from WiFi Channel State Information, using
a single ESP32-S3 and an ordinary router — no second board, no PIR, no camera.

Built on Espressif's [esp-csi](https://github.com/espressif/esp-csi)
`esp-radar/console_test` example, with the board-support and bug fixes needed to
make it actually work on a XIAO ESP32S3 under ESP-IDF v5.4. Includes a
serial→MQTT bridge with Home Assistant discovery.

**Status: working.** Presence flips correctly on entry and exit, and holds while
the occupant is motionless — which is the hard case.

```
    t+    n    wander    jitter  room%  human%
    0s   28   0.00192   0.00138     0%      0%   <- empty room
   10s   23   0.00239   0.00262     0%      0%   <- empty room
   20s   27   0.27172   0.39244    22%     74%   <- someone enters
   30s    6   0.43505   0.59343   100%    100%   <- detected
  170s    7   0.85640   0.00570   100%      0%   <- sitting still, presence HOLDS
  230s    8   0.68326   0.48750   100%    100%   <- moving again
```

Empty-vs-occupied separation on `wander` is roughly **400×**.

---

## Why this repo exists

The stock example does not work on this board, and the reasons are not obvious:

1. **The console goes to a pin that isn't connected.** Upstream puts it on UART0
   (GPIO43/44). The XIAO has no UART bridge chip — only the ESP32-S3's built-in
   USB-Serial/JTAG. Flash stock and you get a completely silent terminal, which
   reads exactly like broken firmware.
2. **`wifi_config -b <bssid>` was silently ignored** — it set `sta.bssid` but
   never `sta.bssid_set = true`, which esp_wifi requires. On a mesh the board
   free-roams between nodes, so the CSI reference path changes underneath your
   calibration.
3. **`wifi_scan` could only ever print zeros** — the loop zeroed each record and
   printed it without calling `esp_wifi_scan_get_ap_record()`.
4. **Presence detection does not work with automatic gain.** See below.

Items 2 and 3 are plain upstream bugs, not IDF-version issues.

## The AGC finding

Presence-while-motionless initially failed: `room_status` was stuck at 1 in an
empty room, and the `wander` baseline drifted (0.11 → 0.028 → 0.18) with no
stable separation between empty and occupied.

Measured from the raw CSI dump:

```
agc_gain   distinct=18  range 29..49
rssi       distinct=8   range -50..-42  stdev 0.68    <- signal is STABLE

mean frame amplitude per agc_gain:
  agc_gain=40  n=196   mean_amp=11.00
  agc_gain=45  n=1409  mean_amp=5.88
```

Received power was essentially constant, but raw CSI amplitude swung **~2×
purely from receiver AGC changes**. That artefact lands directly in the metric
used for presence. `esp_radar`'s `csi_compensate_en` is already `true` on S3 and
is not sufficient on its own.

The fix is to pin the gain rather than compensate it:

| | gain auto | gain pinned |
|---|---|---|
| `agc_gain` distinct values | 18 | **1** |
| amplitude coefficient of variation | 20.4% | **10.6%** |
| wander separation, empty vs occupied | ~1.5× (overlapping) | **~400×** |
| `room_status` in an empty room | stuck at 1 ❌ | **0** ✅ |
| raw callback rate | 101/s | 22.3/s |

Pinning costs about 4× the frames — the receiver drops frames whose level does
not suit the fixed gain — but 22/s is ample and correctness beats frame count.

---

## Hardware

| | |
|---|---|
| Board | Seeed Studio XIAO ESP32S3 (ESP32-S3R8, 8MB flash, 8MB PSRAM) |
| USB | native USB-Serial/JTAG only (VID `0x303A` / PID `0x1001`) — no UART bridge |
| User LED | GPIO21, **active low** (not a WS2812; GPIO38 is not broken out) |
| Toolchain | ESP-IDF v5.4, macOS (Apple Silicon) |
| Stimulus | ICMP ping to the router; no second ESP32 needed |

Other ESP32-S3 boards should work; adjust `BOARD_XIAO_ESP32S3` in `app_main.c`.

> **The ESP32-C6 is a dead end for this.** It never returns usable CSI —
> `LTF type 0 has no data`, all metrics exactly `0.000000`
> ([esp-idf#14271](https://github.com/espressif/esp-idf/issues/14271)). The same
> pattern affects the C61 ([#18118](https://github.com/espressif/esp-idf/issues/18118))
> and C5 ([#18493](https://github.com/espressif/esp-idf/issues/18493)). Use an
> S3 — it is the part Espressif's own examples are developed against.

---

## Layout

```
firmware/
  esp-csi-xiao-s3.patch        apply to esp-csi's console_test example
  sdkconfig.defaults.esp32s3   board config (IDF auto-applies this)
bridge/
  csi_bridge.py                serial -> MQTT, Home Assistant discovery
  config.example.toml          copy to config.toml and edit
tools/
  csi_daemon.py                holds the serial port so board state survives
  analyze.py                   stats over the aggregated RADAR_DADA stream
  analyze_raw.py               validates raw CSI: rate, valid_len, zero frames
  timeline.py                  buckets history so activity shows up as a shape
NOTES.md                       full working notes, gotchas, calibration procedure
```

---

## Quick start

### 1. Firmware

```bash
git clone https://github.com/espressif/esp-csi.git ~/esp/esp-csi
cd ~/esp/esp-csi/examples/esp-radar/console_test
git apply /path/to/xiao-csi-presence/firmware/esp-csi-xiao-s3.patch
cp /path/to/xiao-csi-presence/firmware/sdkconfig.defaults.esp32s3 .

. ~/esp/esp-idf/export.sh
idf.py set-target esp32s3
idf.py build
idf.py -p /dev/cu.usbmodem101 flash
```

Confirm the console actually went to USB before blaming anything else:

```bash
grep -q "^CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y" sdkconfig && echo YES || echo NO
```

> **If the board currently runs MicroPython or CircuitPython**, esptool cannot
> reset it and will fail with `No serial data received` or
> `Invalid head of packet (0x08)` (that is the REPL echoing esptool's SYNC).
> Check with `ioreg -p IOUSB -w0 -l | grep idProduct` — PID `0x4001` means an
> app owns USB, not the ROM. Back up the filesystem, then from the REPL run
> `import machine; machine.bootloader()`. See NOTES.md.

### 2. Pin everything, then calibrate

Order matters. Calibrating before pinning produces a baseline that is invalid
the moment the BSSID or gain changes.

```
wifi_scan -s YOUR_SSID                                   # note BSSID + channel
wifi_config -s YOUR_SSID -p YOUR_PASS -b aa:bb:cc:dd:ee:ff
csi_gain --status                                        # wait ~25s for READY
csi_gain --force_agc=<n> --force_fft=<n>                 # pin to that baseline
```

Now **leave the room**, then:

```
radar --train_start
   ... wait 90 seconds ...
radar --train_stop
```

Verify while still outside: `room_status` must read 0.

### 3. MQTT bridge

```bash
cd bridge
cp config.example.toml config.toml     # edit; config.toml is gitignored
uv run csi_bridge.py --config config.toml
uv run csi_bridge.py --config config.toml --calibrate   # room must be EMPTY
```

Publishes `binary_sensor` occupancy + motion and `sensor` wander/jitter/rate,
with MQTT LWT availability. It holds the serial port for its whole lifetime and
reapplies WiFi + gain + thresholds after any board reset — none of that survives
a reset, and merely opening the port causes one.

---

## Reading the output

```
RADAR_DADA, count, timestamp,
  wander, wander_avg, someone_threshold/sensitivity, room_status,
  jitter, jitter_median, jitter_median/move_sensitivity, human_status
```

**Healthy, empty room:**
```
RADAR_DADA,42,15832,0.001923,0.001744,0.006655,0,0.001381,0.002415,0.012074,0
```

**Broken (the C6 failure):**
```
RADAR_DADA,17,3204,0.000000,0.000000,0.000000,0,0.000000,0.000000,0.000000,0
```

Clean zeros in **both** wander and jitter. Real CSI is never clean — a still
room gives small messy numbers like `0.000431`, never `0.000000`.

`wander` legitimately reads `0.000000` *before* calibration: with no training
set there is no baseline to measure drift against. **jitter should still be
non-zero at that stage** — if jitter is zero too, that is the real failure.

### Yes/no checks

```bash
grep -c "has no data" csi.log                  # the C6 failure; want 0
grep -c RADAR_DADA csi.log                     # is anything arriving?
grep CSI_DATA csi.log | grep -c '"\[0\(,0\)*\]"'   # all-zero frames; want 0
grep -q "Pinning to BSSID" csi.log && echo YES || echo NO
grep "GAIN: status" csi.log | tail -1          # want status=FORCE
```

---

## Known limitations

- **Nothing persists across reboot.** WiFi, gain and thresholds are RAM only.
  The bridge reapplies them; a bare console session must too.
- **Update rate drops when occupied** (~2.8/s empty vs 0.1–0.8/s occupied). The
  pinned gain is learned empty, so a body in the path costs frames. Detection
  stays correct; only refresh rate degrades. Hence the bridge's occupancy hold
  timer.
- **Calibration is per-AP, per-position, per-gain.** Moving the board, changing
  BSSID, or re-pinning gain all invalidate it.
- **`--csi_stop` stops the whole radar**, not just the raw dump. To stop only
  the flood use `radar --csi_output_type=NULL`.
- **Runtime LTF switching is C5/C61 only.** On S3, `--csi_output_type` only
  selects which slice of the buffer is printed. Not a problem — LLTF is the S3
  default and works (`valid_len=104`, 52 subcarriers).

See [NOTES.md](NOTES.md) for the full detail.

## Licence

The patch in `firmware/` applies to Espressif's esp-csi, which is Apache-2.0.
Original work here is Apache-2.0 to match.
