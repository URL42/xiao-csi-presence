#!/usr/bin/env python3
"""Serial -> MQTT bridge for esp-csi console_test, with Home Assistant discovery.

Reads the RADAR_DADA stream from a Seeed XIAO ESP32S3 running the esp-csi
console_test firmware and republishes presence/motion to MQTT.

The bridge owns the serial port for its whole lifetime, which is deliberate:

  * Opening the port on macOS asserts DTR/RTS and resets the ESP32-S3.
  * console_test keeps the WiFi association, the forced receiver gain and the
    calibration thresholds in RAM only.

So anything that opens the port drops all of that on the floor. The bridge
therefore reapplies the full setup after every (re)connect, and can optionally
run calibration itself.

Usage:
    uv run csi_bridge.py --config config.toml
    uv run csi_bridge.py --config config.toml --calibrate   # room must be EMPTY

See config.example.toml.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import paho.mqtt.client as mqtt
import serial

LOG = logging.getLogger("csi_bridge")

# RADAR_DADA, count, timestamp,
#   wander, wander_avg, someone_threshold/sensitivity, room_status,
#   jitter, jitter_median, jitter_median/move_sensitivity, human_status
RADAR_RE = re.compile(r"RADAR_DADA,(\d+),(\d+),([-\d.eE+,]+)")
TRAIN_RE = re.compile(
    r"esp_radar_train_stop.*?data_num=(\d+).*?wander_th=([\d.eE+-]+), jitter_th=([\d.eE+-]+)"
)


@dataclass
class Sample:
    count: int
    timestamp: int
    wander: float
    wander_avg: float
    someone_cutoff: float
    room_status: int
    jitter: float
    jitter_median: float
    move_cutoff: float
    human_status: int


@dataclass
class Config:
    port: str = "/dev/cu.usbmodem101"
    baud: int = 115200

    wifi_ssid: str = ""
    wifi_password: str = ""
    wifi_bssid: str = ""

    force_agc: int | None = None
    force_fft: int | None = None

    someone_threshold: float | None = None
    move_threshold: float | None = None

    calibrate_seconds: int = 90

    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    base_topic: str = "sensors"
    discovery_prefix: str = "homeassistant"

    device_id: str = "csi_radar"
    device_name: str = "CSI Presence Radar"

    publish_interval: float = 1.0
    occupancy_hold: float = 60.0
    motion_hold: float = 5.0

    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = tomllib.loads(path.read_text())
        flat = {}
        for section in data.values():
            if isinstance(section, dict):
                flat.update(section)
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        cfg = cls(**{k: v for k, v in flat.items() if k in known})
        cfg.extra = {k: v for k, v in flat.items() if k not in known}
        cfg.validate()
        return cfg

    # Values carried over unedited from config.example.toml, each with advice
    # specific to that field. A placeholder BSSID in particular looks entirely
    # plausible in a log line while the board silently never associates.
    PLACEHOLDERS = {
        "wifi_ssid": (
            {"YOUR_SSID"},
            "Set this to your network name.",
        ),
        "wifi_password": (
            {"CHANGE_ME"},
            "Set this to your network password.",
        ),
        "wifi_bssid": (
            {"aa:bb:cc:dd:ee:ff", "xx:xx:xx:xx:xx:xx"},
            "Get the real BSSID from the board's console:\n"
            "        wifi_scan -s <your ssid>",
        ),
        "mqtt_host": (
            {"YOUR_BROKER_HOST"},
            "Set this to your MQTT broker's hostname or IP (for Home Assistant\n"
            "    this is usually the machine running Mosquitto).",
        ),
    }

    def validate(self) -> None:
        problems = []
        for field, (dummies, hint) in self.PLACEHOLDERS.items():
            if getattr(self, field) in dummies:
                problems.append(f"  {field} = {getattr(self, field)!r}\n    {hint}")
        if problems:
            raise SystemExit(
                "config.toml still contains placeholder values from the "
                "example file:\n\n" + "\n\n".join(problems) + "\n"
            )
        if self.wifi_bssid and not re.fullmatch(
                r"([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", self.wifi_bssid):
            raise SystemExit(f"wifi_bssid {self.wifi_bssid!r} is not a MAC address")


def find_port(configured: str) -> str:
    """Resolve the serial port, tolerating macOS renumbering.

    The XIAO's USB-Serial/JTAG node changes name when it re-enumerates on a
    different USB port (cu.usbmodem101 -> cu.usbmodem1101), so a hardcoded
    path goes stale. Set port = "auto" to pick the single Espressif device.
    """
    from serial.tools import list_ports

    if configured and configured != "auto" and Path(configured).exists():
        return configured

    # VID 0x303A is Espressif; PID 0x1001 is the USB-Serial/JTAG peripheral.
    candidates = [p.device for p in list_ports.comports() if p.vid == 0x303A]
    if not candidates:
        candidates = [p.device for p in list_ports.comports()
                      if "usbmodem" in p.device]
    if not candidates:
        raise SystemExit(f"no Espressif serial device found (configured: {configured!r})")
    if len(candidates) > 1:
        LOG.warning("multiple candidates %s, using the first", candidates)
    LOG.info("using serial port %s", candidates[0])
    return candidates[0]


class Board:
    """Owns the serial port and the board's volatile configuration."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ser = serial.Serial(find_port(cfg.port), cfg.baud, timeout=0.1)
        time.sleep(0.3)
        self.ser.setDTR(False)     # never fall into the ROM bootloader
        self._buf = b""
        self._wait_boot()

    def _wait_boot(self, quiet: float = 1.5, limit: float = 25.0) -> None:
        LOG.info("waiting for board to finish booting")
        last = t0 = time.time()
        while time.time() - t0 < limit:
            if self.ser.read(4096):
                last = time.time()
            elif time.time() - last > quiet:
                LOG.info("board ready")
                return
        LOG.warning("boot did not settle within %.0fs, continuing anyway", limit)

    def send(self, cmd: str, settle: float = 1.0) -> list[str]:
        LOG.info("-> %s", cmd if "password" not in cmd and " -p " not in cmd
                 else re.sub(r"(-p|--password)\s+\S+", r"\1 <redacted>", cmd))
        self.ser.write(cmd.encode() + b"\r\n")
        self.ser.flush()
        # drain while we wait, so the reply is available and the OS buffer
        # does not overflow under a heavy CSI stream
        out: list[str] = []
        t0 = time.time()
        while time.time() - t0 < settle:
            out.extend(self.lines())
            time.sleep(0.02)
        return out

    def wait_for(self, pattern: str, timeout: float, what: str) -> str | None:
        """Watch the stream for a pattern, returning the matching line."""
        rx = re.compile(pattern)
        t0 = time.time()
        while time.time() - t0 < timeout:
            for line in self.lines():
                if rx.search(line):
                    return line
            time.sleep(0.05)
        LOG.error("timed out after %.0fs waiting for %s", timeout, what)
        return None

    def count_samples(self, seconds: float) -> int:
        n = 0
        t0 = time.time()
        while time.time() - t0 < seconds:
            for line in self.lines():
                if parse_sample(line) is not None:
                    n += 1
            time.sleep(0.02)
        return n

    def lines(self):
        """Yield complete decoded lines as they arrive."""
        chunk = self.ser.read(8192)
        if chunk:
            self._buf += chunk
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            yield raw.decode("utf-8", "replace").strip()

    # ---- setup that must be reapplied after every reset ----

    def apply_setup(self) -> bool:
        """Reapply the volatile config. Returns True if the board associated."""
        c = self.cfg
        connected = True
        if c.wifi_ssid:
            cmd = f"wifi_config -s {c.wifi_ssid} -p {c.wifi_password}"
            if c.wifi_bssid:
                cmd += f" -b {c.wifi_bssid}"
            self.send(cmd, settle=1.0)
            line = self.wait_for(r"connected with|sta ip:", 20.0,
                                 "the board to associate")
            if line is None:
                LOG.error("not associated. Check ssid/password, and that "
                          "wifi_bssid is a real BSSID from `wifi_scan -s %s`",
                          c.wifi_ssid)
                connected = False
            else:
                LOG.info("associated: %s", line.split(":", 1)[-1].strip()[:90])
                self.wait_for(r"sta ip:", 15.0, "a DHCP lease")

        if c.force_agc is not None and c.force_fft is not None:
            # let the gain baseline settle before pinning it
            self.send("csi_gain --status", settle=2.0)
            self.send(f"csi_gain --force_agc={c.force_agc} --force_fft={c.force_fft}",
                      settle=2.0)

        if c.someone_threshold is not None:
            self.send(f"radar --predict_someone_threshold={c.someone_threshold}")
        if c.move_threshold is not None:
            self.send(f"radar --predict_move_threshold={c.move_threshold}")

        return connected

    def preflight(self) -> bool:
        """Confirm CSI is actually flowing before committing to a long capture.

        Calibration takes 90s with the operator stood outside the room. Finding
        out afterwards that nothing was ever received is the worst possible
        outcome, so spend three seconds proving the stream first.
        """
        LOG.info("preflight: checking CSI is flowing")
        n = self.count_samples(3.0)
        rate = n / 3.0
        if n == 0:
            LOG.error("preflight FAILED: no CSI samples in 3s. The board is "
                      "not associated, or the radar is stopped "
                      "(`radar --csi_start`).")
            return False
        LOG.info("preflight OK: %.1f samples/s", rate)
        return True

    def calibrate(self) -> tuple[float, float] | None:
        """Run training. The room MUST be empty."""
        secs = self.cfg.calibrate_seconds
        LOG.warning("CALIBRATING for %ds - the room must be EMPTY", secs)
        self.send("radar --train_start", settle=1.0)
        t0 = time.time()
        while time.time() - t0 < secs:
            for _ in self.lines():
                pass
            time.sleep(0.05)
        self.send("radar --train_stop", settle=3.0)

        deadline = time.time() + 5
        while time.time() < deadline:
            for line in self.lines():
                m = TRAIN_RE.search(line)
                if m:
                    n, wth, jth = int(m.group(1)), float(m.group(2)), float(m.group(3))
                    LOG.info("calibration done: data_num=%d wander_th=%g jitter_th=%g",
                             n, wth, jth)
                    if n == 0:
                        LOG.error("calibration collected 0 samples - is CSI flowing?")
                        return None
                    return wth, jth
            time.sleep(0.05)
        LOG.error("no calibration result seen. esp_radar only emits a result "
                  "when it has a CSI peer, so this almost always means no CSI "
                  "arrived during the capture.")
        return None


class Publisher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = f"{cfg.base_topic}/{cfg.device_id}"
        self.avail = f"{self.base}/availability"
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"{cfg.device_id}_bridge"
        )
        if cfg.mqtt_username:
            self.client.username_pw_set(cfg.mqtt_username, cfg.mqtt_password)
        self.client.will_set(self.avail, "offline", retain=True)
        self.client.on_connect = self._on_connect
        LOG.info("connecting to mqtt %s:%d", cfg.mqtt_host, cfg.mqtt_port)
        self.client.connect(cfg.mqtt_host, cfg.mqtt_port, keepalive=60)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        LOG.info("mqtt connected (rc=%s)", rc)
        client.publish(self.avail, "online", retain=True)
        self.publish_discovery()

    # ---- Home Assistant MQTT discovery ----

    def _device(self) -> dict:
        return {
            "identifiers": [self.cfg.device_id],
            "name": self.cfg.device_name,
            "manufacturer": "Seeed Studio",
            "model": "XIAO ESP32S3 (esp-csi console_test)",
        }

    def publish_discovery(self) -> None:
        p = self.cfg.discovery_prefix
        did = self.cfg.device_id
        common = {
            "availability_topic": self.avail,
            "device": self._device(),
        }
        entities = [
            (f"{p}/binary_sensor/{did}/occupancy/config", {
                "name": "Occupancy",
                "unique_id": f"{did}_occupancy",
                "state_topic": f"{self.base}/occupancy",
                "device_class": "occupancy",
                "payload_on": "ON", "payload_off": "OFF",
            }),
            (f"{p}/binary_sensor/{did}/motion/config", {
                "name": "Motion",
                "unique_id": f"{did}_motion",
                "state_topic": f"{self.base}/motion",
                "device_class": "motion",
                "payload_on": "ON", "payload_off": "OFF",
            }),
            (f"{p}/sensor/{did}/wander/config", {
                "name": "CSI Wander",
                "unique_id": f"{did}_wander",
                "state_topic": f"{self.base}/state",
                "value_template": "{{ value_json.wander }}",
                "state_class": "measurement",
                "suggested_display_precision": 4,
                "icon": "mdi:waveform",
            }),
            (f"{p}/sensor/{did}/jitter/config", {
                "name": "CSI Jitter",
                "unique_id": f"{did}_jitter",
                "state_topic": f"{self.base}/state",
                "value_template": "{{ value_json.jitter }}",
                "state_class": "measurement",
                "suggested_display_precision": 4,
                "icon": "mdi:pulse",
            }),
            (f"{p}/sensor/{did}/rate/config", {
                "name": "CSI Sample Rate",
                "unique_id": f"{did}_rate",
                "state_topic": f"{self.base}/state",
                "value_template": "{{ value_json.rate }}",
                "unit_of_measurement": "/s",
                "state_class": "measurement",
                "icon": "mdi:speedometer",
            }),
        ]
        for topic, payload in entities:
            payload.update(common)
            self.client.publish(topic, json.dumps(payload), retain=True)
        LOG.info("published HA discovery for %d entities", len(entities))

    def publish_state(self, s: Sample, occupancy: bool, motion: bool, rate: float) -> None:
        self.client.publish(f"{self.base}/occupancy", "ON" if occupancy else "OFF", retain=True)
        self.client.publish(f"{self.base}/motion", "ON" if motion else "OFF", retain=True)
        self.client.publish(f"{self.base}/state", json.dumps({
            "wander": round(s.wander, 6),
            "wander_avg": round(s.wander_avg, 6),
            "jitter": round(s.jitter, 6),
            "jitter_median": round(s.jitter_median, 6),
            "someone_cutoff": round(s.someone_cutoff, 6),
            "move_cutoff": round(s.move_cutoff, 6),
            "room_status": s.room_status,
            "human_status": s.human_status,
            "rate": round(rate, 1),
        }), retain=False)

    def close(self) -> None:
        self.client.publish(self.avail, "offline", retain=True)
        time.sleep(0.2)
        self.client.loop_stop()
        self.client.disconnect()


def parse_sample(line: str) -> Sample | None:
    m = RADAR_RE.search(line)
    if not m:
        return None
    parts = m.group(3).split(",")
    if len(parts) != 8:
        return None                      # train_stop emits a short 6-field line
    try:
        v = [float(x) for x in parts]
    except ValueError:
        return None
    return Sample(int(m.group(1)), int(m.group(2)),
                  v[0], v[1], v[2], int(v[3]), v[4], v[5], v[6], int(v[7]))


def run(cfg: Config, do_calibrate: bool) -> int:
    board = Board(cfg)
    associated = board.apply_setup()

    if do_calibrate:
        # Fail in seconds rather than after a 90s capture with the operator
        # stood outside the room.
        if not associated:
            LOG.error("refusing to calibrate: the board is not on the network")
            return 1
        if not board.preflight():
            LOG.error("refusing to calibrate: no CSI stream")
            return 1

        result = board.calibrate()
        if result is None:
            LOG.error("calibration failed, not publishing")
            return 1
        # esp-radar's trained thresholds are a starting point; config overrides win
        if cfg.someone_threshold is None:
            board.send(f"radar --predict_someone_threshold={result[0]}")
        if cfg.move_threshold is None:
            board.send(f"radar --predict_move_threshold={result[1]}")

    pub = Publisher(cfg)

    running = True

    def stop(_sig, _frm):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    last_pub = 0.0
    last_occupied = 0.0
    last_motion = 0.0
    recent: list[float] = []
    stale_warned = False
    last_sample_at = time.time()

    LOG.info("bridging; occupancy_hold=%.0fs motion_hold=%.0fs",
             cfg.occupancy_hold, cfg.motion_hold)

    while running:
        got = False
        for line in board.lines():
            s = parse_sample(line)
            if s is None:
                continue
            got = True
            now = time.time()
            last_sample_at = now
            recent.append(now)
            recent[:] = [t for t in recent if now - t <= 10.0]

            if s.room_status:
                last_occupied = now
            if s.human_status:
                last_motion = now

            # Hold timers stop the entity flickering on single-sample dropouts.
            # Motion alone also counts as occupancy: if you are moving you are
            # in the room, whatever the wander metric currently says.
            occupancy = (now - last_occupied <= cfg.occupancy_hold
                         or now - last_motion <= cfg.occupancy_hold)
            motion = now - last_motion <= cfg.motion_hold

            if now - last_pub >= cfg.publish_interval:
                rate = len(recent) / 10.0
                pub.publish_state(s, occupancy, motion, rate)
                last_pub = now
                stale_warned = False

        if not got:
            if time.time() - last_sample_at > 30 and not stale_warned:
                LOG.warning("no CSI samples for 30s - board reset, or WiFi dropped. "
                            "Reapplying setup.")
                board.apply_setup()
                last_sample_at = time.time()
                stale_warned = True
            time.sleep(0.05)

    LOG.info("shutting down")
    pub.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--calibrate", action="store_true",
                    help="run calibration on startup; the room must be EMPTY")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return run(Config.load(args.config), args.calibrate)


if __name__ == "__main__":
    sys.exit(main())
