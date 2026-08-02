# WiFi CSI presence detection — Seeed XIAO ESP32S3

Working notes for `esp-csi/examples/esp-radar/console_test` on a Seeed
Studio XIAO ESP32S3, ESP-IDF v5.4, macOS.

Status: **working.** All five acceptance criteria met — see
[Definition of done](#definition-of-done-evidence).

---

## Board

```
Chip type:   ESP32-S3 (QFN56) revision v0.2
Features:    Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB
Flash:       8MB, GigaDevice, QIO
USB mode:    USB-Serial/JTAG  (VID 0x303A / PID 0x1001)
MAC:         xx:xx:xx:xx:xx:xx
```

Identify with:

```bash
esptool -p /dev/cu.usbmodem101 --no-stub chip-id
ioreg -p IOUSB -w0 -l | grep -E '"(USB Product Name|idProduct)"'
```

The `ioreg` check matters more than it looks — see
[MicroPython](#gotcha-0-a-board-running-micropython-cannot-be-auto-reset).

---

## Which example, and why

`console_test`, not `wifi_sensing_demo`.

| | `wifi_sensing_demo` | `console_test` |
|---|---|---|
| esp-radar | prebuilt `.a`, no `src/` | **compiles from source** |
| Tuning | unreachable from app layer | runtime console commands |
| Verification | trust the state machine | raw CSI dump + PyQt waterfall |

The build log confirms the important part:

```
-- ESP_RADAR: 0.3.4
-- Using local esp-radar library
```

`wifi_sensing_demo` is what blocks debugging on a misbehaving chip: when the
detection is wrong you cannot see or change anything inside it.

### ESP32-S3 vs the ESP32-C6 dead end

The C6 never returned usable CSI (`LTF type 0 has no data`, all metrics
`0.000000`), traced to espressif/esp-idf#14271. That failure is confined to the
newer RISC-V Wi-Fi 6 parts — C6 (#14271), C61 (#18118), C5 (#18493).

Searched both trackers for the S3: **no open issue reports zero or absent CSI on
ESP32-S3.** Open S3 issues are tooling only — esp-csi #206 (GUI error), #228
(`esp_radar_get_gain_compensation` symbol), #244 (parser vs IDF 5.5.1).

esp-csi's README pins IDF v5.0.2 as its reference. **v5.4 builds clean** with no
API breakage. Two upstream bugs were hit, both unrelated to IDF version — see
[Upstream bugs fixed](#upstream-bugs-fixed-here).

---

## Config changes

### `sdkconfig.defaults.esp32s3` (new file)

IDF applies `sdkconfig.defaults` then `sdkconfig.defaults.<target>`, so upstream
stays pristine and every deviation lives in one short file.

| Setting | Why |
|---|---|
| `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` | **Critical.** Upstream puts the console on UART0 (GPIO43/44). The XIAO has no UART bridge — flashing stock gives a totally silent terminal. |
| `CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y` | Upstream declares 4MB; XIAO is 8MB. |
| `CONFIG_ESP_WIFI_CSI_ENABLED=y` | Upstream uses the pre-5.0 name `CONFIG_ESP32_WIFI_CSI_ENABLED`; set the current name explicitly rather than relying on the rename table. |

Verify it took **in the generated sdkconfig**, not the defaults file:

```bash
grep -q "^CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y" sdkconfig && echo YES || echo NO
```

`CONFIG_ESP_CONSOLE_UART_NUM=-1` in the generated file confirms UART is fully out
of the path.

### `main/app_main.c`

1. **REPL over USB Serial/JTAG** — `esp_console_new_repl_usb_serial_jtag()`
   instead of `..._uart()`, guarded by `#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG`
   so other boards still build. The `uart_ll` baud hack below it is already
   `#if`-guarded to ESP32/ESP32-S2, so it is inert on S3.
2. **New `csi_gain` console command** — exposes
   `esp_csi_gain_ctrl_set_rx_force_gain()`. This is what makes presence
   detection work; see [The AGC problem](#the-agc-problem-the-key-finding).
3. **Board LED abstraction** — upstream drives a WS2812 on GPIO38
   (ESP32-S3-DevKitC-1 layout). The XIAO has no addressable RGB; it has one
   plain user LED on **GPIO21, active low**, and does not break out GPIO38 at
   all, so stock gives zero feedback. `board_led_set()` / `board_led_refresh()`
   / `board_led_init()` now sit behind `BOARD_XIAO_ESP32S3`; colour collapses to
   on/off. Set `BOARD_XIAO_ESP32S3 0` to restore stock WS2812 behaviour.

### Runtime settings (RAM only — reapply every boot)

```
wifi_config -s <SSID> -p <PASS> -b aa:bb:cc:dd:ee:ff
csi_gain --force_agc=51 --force_fft=-44
radar --predict_someone_threshold=<from calibration>
radar --predict_move_threshold=<from calibration>
```

**Nothing persists across reset.** And opening the serial port on macOS asserts
DTR/RTS, which resets the board — so a naive script that opens the port, sends a
command, and closes loses all state. Use a daemon that holds the port open for
the whole session.

---

## The AGC problem (the key finding)

Presence-while-motionless did not work at first. `room_status` was stuck at 1 in
an empty room, and the `wander` baseline drifted across a session: 0.11 → 0.028
→ 0.18, with no stable separation between empty and occupied.

Root cause, measured from the raw CSI dump:

```
agc_gain   distinct=18  range 29..49
rssi       distinct=8   range -50..-42  stdev 0.68   <- signal is STABLE

mean frame amplitude per agc_gain:
  agc_gain=40  n=196   mean_amp=11.00
  agc_gain=43  n=2406  mean_amp=6.42
  agc_gain=45  n=1409  mean_amp=5.88
```

Received power was constant, but **raw CSI amplitude swung ~2× purely from
receiver AGC gain changes**. That artefact lands directly in `waveform_wander`.
`esp_radar` has `csi_compensate_en = true` by default on S3, and it was on — the
compensation is not sufficient on its own.

Fix: pin the gain instead of compensating it.

```
csi_gain --status                          # let it settle, read the baseline
csi_gain --force_agc=51 --force_fft=-44    # pin to that baseline
```

| | gain auto | gain forced |
|---|---|---|
| `agc_gain` distinct values | 18 | **1** |
| amplitude coefficient of variation | 20.4% | **10.6%** |
| wander separation empty vs occupied | ~1.5× (overlapping) | **~400×** |
| `room_status` in empty room | stuck at 1 ❌ | **0** ✅ |
| raw callback rate | 101/s | 22.3/s |

**The gain baseline is per-AP and per-position.** It read 44 on one mesh node and
51 on the other. Re-read `csi_gain --status` after any move, then re-force.

---

## Calibration procedure

1. Pin everything first — BSSID, then gain. Calibrating before pinning produces
   a baseline that is invalid the moment either changes.
   ```
   wifi_config -s <SSID> -p <PASS> -b <BSSID>
   csi_gain --status          # wait ~25s for status=READY, note the baseline
   csi_gain --force_agc=<n> --force_fft=<n>
   ```
2. **Leave the room.** Shut the door. If you are present during calibration your
   body becomes part of "empty" and detection is permanently degraded.
3. ```
   radar --train_start
   ```
   Wait **90 seconds** (longer than default; fewer false triggers).
   ```
   radar --train_stop
   ```
4. Read the thresholds off the train_stop line:
   ```
   esp_radar_train_stop (peer=aa:bb:cc:dd:ee:ff), data_num=30,
     wander_th=0.000998, jitter_th=0.052672
   ```
5. Verify while still out of the room — `room_status` must read 0.

### Threshold tuning

Only the **ratio** `threshold / sensitivity` matters. From `app_main.c:439`:

```c
if (wander_average * someone_sensitivity > someone_threshold)  someone_count++;
if (jitter * move_sensitivity > move_threshold || ...)         move_count++;
```

That ratio is printed live as the `someone_thr` and `move_thr` columns, so you
can set a target cutoff directly: `threshold = cutoff * sensitivity`
(defaults: someone 0.15, move 0.2).

The trained `jitter_th` came out slightly too high in practice — measured walking
peaked at 0.145 against a threshold of 0.161, so motion only registered ~15% of
the time. Set motion cutoff from measured data, roughly 3–5× the still-room
jitter mean.

---

## What a healthy log looks like

### `RADAR_DADA` field order (`app_main.c:490`)

```
RADAR_DADA, count, timestamp,
  waveform_wander, wander_average, someone_threshold/sensitivity, room_status,
  waveform_jitter, jitter_median,  jitter_median/move_sensitivity, human_status
```

### Healthy — empty room, calibrated, gain pinned

```
RADAR_DADA,42,15832,0.001923,0.001744,0.006655,0,0.001381,0.002415,0.012074,0
                    ^wander            ^cutoff  ^0  ^jitter               ^0
```
Small, messy, non-zero; both statuses 0.

### Healthy — occupied

```
RADAR_DADA,88,25104,0.435052,0.398211,0.006655,1,0.593433,0.284120,1.420600,1
```
wander two-plus orders of magnitude up; both statuses 1.

### Broken — the C6 failure

```
RADAR_DADA,17,3204,0.000000,0.000000,0.000000,0,0.000000,0.000000,0.000000,0
W (3204) esp_radar: LTF type 0 has no data
```
Clean zeros in **both** wander and jitter. Note `0.000000` exactly, never
`0.000431` — real CSI is never clean.

### `wander` legitimately reads 0.000000 before calibration

Not a fault. `esp_radar.c:1274`:

```c
if (!peer || !peer->cal || peer->cal->data_num == 0) {
    radar_info->waveform_wander = 0.0f;
    return;
}
```

No training set, no baseline to measure drift against. **jitter should still be
non-zero at this stage** — if jitter is zero too, that is the real failure.

---

## Yes/no checks

```bash
# console routed correctly (run in the project dir)
grep -q "^CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y" sdkconfig && echo YES || echo NO

# the LTF failure that killed the C6 — want 0
grep -c "has no data" csi.log

# is any CSI arriving at all?
grep -c RADAR_DADA csi.log

# jitter stuck at zero == broken; want a high count of non-zero
grep RADAR_DADA csi.log | awk -F, '$8 != "0.000000"' | wc -l

# raw frames that are entirely zero — want 0
grep CSI_DATA csi.log | grep -c '"\[0\(,0\)*\]"'

# did the BSSID pin actually apply?
grep -q "Pinning to BSSID" csi.log && echo YES || echo NO

# is gain pinned?
grep "GAIN: status" csi.log | tail -1     # want status=FORCE
```

---

## Upstream bugs fixed here

Both in `components/commands/src/wifi_cmd.c` — a top-level component, so the
component manager cannot revert the edits. Neither is IDF-5.4-specific; both are
plain upstream bugs.

**1. `wifi_scan` printed only zeros.** The loop zeroed the record and printed it
without ever fetching one — `esp_wifi_scan_get_ap_record()` was never called. It
reported "26 APs found" and then 26 rows of empty SSID / `00:00:00:00:00:00` /
channel 0 / rssi 0.

**2. `wifi_config -b <bssid>` was silently ignored.** It set
`wifi_config.sta.bssid` but never `wifi_config.sta.bssid_set = true`, and
esp_wifi ignores the BSSID field without that flag. On a mesh this means the STA
roams freely and the CSI reference path can change underneath a calibration —
exactly the instability this project needed to eliminate. Confirmed: before the
fix the board associated with `...:d1` despite `-b ...:81`; after, it pins
correctly.

---

## Known limitations

- **Update rate drops when the room is occupied.** ~2.8/s empty vs 0.1–0.8/s
  occupied, because the pinned gain was learned empty and a body in the path
  causes more frames to be dropped. Detection stays correct; only the refresh
  rate degrades. Remedies: force a slightly lower `agc_gain` as a compromise
  between both states, or re-run `csi_gain --status` with someone in the room and
  pick a midpoint.
- **Forcing gain costs frames**: 101/s → 22.3/s. Worth it — correctness beats
  frame count — but do not force gain if you need maximum raw CSI throughput for
  offline analysis.
- **Calibration is per-AP, per-position, per-gain.** Moving the board, changing
  BSSID, or re-forcing gain all invalidate it.
- **Nothing persists across reboot.** WiFi, gain, and thresholds are RAM only.
- **`--csi_stop` stops the whole radar**, not just the raw dump — it calls
  `esp_radar_stop()`. To stop only the flood use `radar --csi_output_type=NULL`.
- **Runtime LTF switching is C5/C61 only.** The LTF block in `app_main.c:219` is
  `#if CONFIG_IDF_TARGET_ESP32C5 || CONFIG_IDF_TARGET_ESP32C61`. On S3,
  `--csi_output_type` only selects which slice of the buffer is printed. Not a
  problem — LLTF is the S3 default and works (`valid_len=104`, 52 subcarriers).
- **Update rate is variable**, not fixed — `RADAR_DADA` is esp-radar's
  aggregated output (`csi_handle_time: 200`), not the raw callback rate.
  `radar --send_data_interval` changes the ping TX rate, not this.

---

## Definition of done: evidence

| # | Criterion | Result |
|---|---|---|
| 1 | Callbacks at tens/sec | **22.3/s** raw (101/s with gain auto), 0 sequence gaps |
| 2 | jitter/wander non-zero and noisy | jitter 0.0004–0.0497, 69/69 distinct; wander 0.0008–0.0079 empty |
| 3 | Rise when walking, settle when leaving | jitter 0.0014 → 0.593 → 0.005 |
| 4 | Calibration → non-zero thresholds | `wander_th=0.000998`, `jitter_th=0.052672`, `data_num=30` |
| 5 | Presence flips on entry/exit | empty 0% → occupied 100%, **holds while motionless** |

Raw CSI sanity, gain pinned:

```
frames 265   span 11.9s   CALLBACK RATE 22.3/s
declared valid_len [104]   actual array len [104]
subcarrier values: min -63  max 64  stdev 17.62
frames that are entirely zero: 0/265
subcarriers that vary across frames: 104/104
```

Entry transition:

```
    t+    n    wander    jitter  room%  human%
    0s   28   0.00192   0.00138     0%      0%   <- empty
   10s   23   0.00239   0.00262     0%      0%   <- empty
   20s   27   0.27172   0.39244    22%     74%   <- entering
   30s    6   0.43505   0.59343   100%    100%   <- detected
  170s    7   0.85640   0.00570   100%      0%   <- sitting still, presence holds
  230s    8   0.68326   0.48750   100%    100%   <- moving again
```

---

## MQTT bridge (Home Assistant)

`~/esp/csi-mqtt-bridge/` — serial → MQTT with HA discovery.

```bash
cd ~/esp/csi-mqtt-bridge
cp config.example.toml config.toml     # then edit; config.toml is gitignored
uv run csi_bridge.py --config config.toml
uv run csi_bridge.py --config config.toml --calibrate   # room must be EMPTY
```

The bridge holds the serial port for its whole lifetime by design, and
reapplies WiFi + forced gain + thresholds after every board reset — because
none of that survives a reset and merely opening the port causes one.

Entities published: `binary_sensor` occupancy + motion (with device classes),
`sensor` wander / jitter / sample-rate. Availability uses an MQTT LWT so HA
marks the device unavailable if the bridge dies.

Two behaviours worth knowing:

- **Occupancy has a hold timer** (`occupancy_hold`, default 60s). The board's
  update rate drops when the room is occupied, so without a hold the entity
  flaps. Motion also counts as occupancy — if you are moving you are in the
  room regardless of what `wander` currently says.
- **Stale-stream recovery**: 30s with no samples triggers a full setup
  reapply, which covers a board reset or a WiFi drop.

---

## Gotcha 0: a board running MicroPython cannot be auto-reset

This board arrived running MicroPython v1.28.0, which cost an hour before it was
spotted. Symptoms:

- `esptool` fails with `No serial data received`, or `Invalid head of packet
  (0x08)` — that is the MicroPython REPL **echoing** esptool's SYNC bytes.
- Reading the idle port returns **0 bytes** (a REPL only speaks when spoken to).
- `ioreg` shows PID **`0x4001` / "Espressif Device"** (MicroPython's TinyUSB
  stack) rather than `0x1001` / "USB JTAG_serial debug unit" (the ROM's
  USB-Serial/JTAG).
- BOOT+RESET appears not to work, because the board just reboots into
  MicroPython.

Diagnose:

```bash
ioreg -p IOUSB -w0 -l | grep -E '"(USB Product Name|idProduct)"'
```

`0x4001` means an application owns USB, not the ROM. MicroPython's TinyUSB does
not implement the DTR/RTS→GPIO0/EN reset gesture, so esptool cannot reset it.

Fix — cleaner and more reliable than the button dance:

```python
import machine
machine.bootloader()      # hands control to the ROM loader
```

**Back up the filesystem first** — flashing erases it:

```python
import os
for e in os.ilistdir(''):
    print(e[0], e[3])
```

Once esp-csi is flashed the board uses the hardware USB-Serial/JTAG, and plain
`idf.py flash` works normally with no button presses.
