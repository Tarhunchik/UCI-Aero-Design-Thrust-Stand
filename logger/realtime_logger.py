#!/usr/bin/env python3
"""Hardware acquisition and recording for the VS Code/Jupyter workflow.

This module intentionally contains no GUI or plotting backend.
"""

from __future__ import annotations

import csv
import math
import queue
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import serial
from serial.tools import list_ports

BAUD = 115200
DEFAULT_WIFI_HOST = "192.168.4.1"
DEFAULT_TCP_PORT = 3333
CELL_COUNT = 4
STANDARD_GRAVITY = 9.80665
NEWTONS_PER_LBF = 4.4482216152605
EMA_ALPHA = 0.30
TOTAL_FRESHNESS_SECONDS = 1.0
CSV_HEADER = [
    "host_time_iso", "record_type", "source", "endpoint", "esp_time_ms",
    "raw1", "raw2", "raw3", "raw4",
    "kgf1", "kgf2", "kgf3", "kgf4", "total_kgf", "total_force_N",
    "filtered_total_force_N", "cell1_lbf", "cell2_lbf", "cell3_lbf",
    "cell4_lbf", "total_force_lbf",
    "filtered_total_force_lbf", "current_adc_raw", "voltage_adc_raw",
    "current_A", "voltage_V", "power_W", "filtered_current_A",
    "filtered_voltage_V", "filtered_power_W",
]


class SocketTransport:
    """Serial-like wrapper around the ESP32 TCP stream."""

    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.create_connection((host, port), timeout=8.0)
        self.sock.settimeout(0.2)
        self.buffer = bytearray()

    def readline(self) -> bytes:
        while b"\n" not in self.buffer:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return b""
            if not chunk:
                raise ConnectionError("ESP32 closed the wireless connection")
            self.buffer.extend(chunk)
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        return line + b"\n"

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def close(self) -> None:
        self.sock.close()


def choose_port(requested: str | None = None) -> str:
    if requested:
        return requested
    ports = list(list_ports.comports())
    likely = [
        port for port in ports
        if any(tag in (port.description or "").lower()
               for tag in ("cp210", "ch340", "usb serial", "usb jtag", "uart"))
    ]
    if len(likely) == 1:
        return likely[0].device
    if not ports:
        raise RuntimeError("No serial ports found. Connect the ESP32 and try again.")
    details = "\n".join(f"  {port.device}: {port.description}" for port in ports)
    raise RuntimeError(f"Could not choose one ESP32 port. Pass port='COMx'.\n{details}")


def parse_sample(parts: list[str]) -> dict[str, float | int] | None:
    """Parse current four-cell and legacy one/two-cell DATA records."""
    if not parts or parts[0] != "DATA":
        return None
    if len(parts) == 13:
        esp_ms = int(parts[1])
        raw = list(map(int, parts[2:6]))
        kgf = list(map(float, parts[6:10]))
        total_kgf, force_n, filtered_n = map(float, parts[10:13])
    elif len(parts) == 9:
        esp_ms = int(parts[1])
        raw = [int(parts[2]), int(parts[3]), 0, 0]
        kgf = [*map(float, parts[4:6]), math.nan, math.nan]
        total_kgf, force_n, filtered_n = map(float, parts[6:9])
    elif len(parts) == 6:
        esp_ms, raw1 = int(parts[1]), int(parts[2])
        kgf1, force_n, filtered_n = map(float, parts[3:6])
        raw = [raw1, 0, 0, 0]
        kgf = [kgf1, math.nan, math.nan, math.nan]
        total_kgf = kgf1
    else:
        return None
    result: dict[str, float | int] = {
        "esp_ms": esp_ms, "total_kgf": total_kgf,
        "force_n": force_n, "filtered_n": filtered_n,
        "total_lbf": force_n / NEWTONS_PER_LBF,
        "filtered_lbf": filtered_n / NEWTONS_PER_LBF,
    }
    for index in range(CELL_COUNT):
        result[f"raw{index + 1}"] = raw[index]
        result[f"kgf{index + 1}"] = kgf[index]
        result[f"cell{index + 1}_lbf"] = (
            kgf[index] * STANDARD_GRAVITY / NEWTONS_PER_LBF)
    return result


def parse_power(parts: list[str]) -> dict[str, float | int] | None:
    """Parse a MAUCH POWER protocol record."""
    if not parts or parts[0] != "POWER" or len(parts) != 10:
        return None
    values = list(map(float, parts[4:10]))
    return {
        "esp_ms": int(parts[1]), "current_raw": int(parts[2]),
        "voltage_raw": int(parts[3]), "current_a": values[0],
        "voltage_v": values[1], "power_w": values[2],
        "filtered_current_a": values[3], "filtered_voltage_v": values[4],
        "filtered_power_w": values[5],
    }


@dataclass
class SourceState:
    number: int
    label: str
    endpoint: str
    transport: Any
    cell_times: list[deque[float]] = field(
        default_factory=lambda: [deque() for _ in range(CELL_COUNT)])
    cells: list[deque[float]] = field(
        default_factory=lambda: [deque() for _ in range(CELL_COUNT)])
    filtered_cells: list[deque[float]] = field(
        default_factory=lambda: [deque() for _ in range(CELL_COUNT)])
    filter_state: list[float] = field(
        default_factory=lambda: [math.nan] * CELL_COUNT)
    latest_cells: list[float] = field(
        default_factory=lambda: [math.nan] * CELL_COUNT)
    latest_filtered_cells: list[float] = field(
        default_factory=lambda: [math.nan] * CELL_COUNT)
    latest_cell_times: list[float] = field(
        default_factory=lambda: [0.0] * CELL_COUNT)
    electrical_times: deque[float] = field(default_factory=deque)
    electrical_raw: list[deque[float]] = field(default_factory=lambda: [deque(), deque(), deque()])
    electrical_filtered: list[deque[float]] = field(default_factory=lambda: [deque(), deque(), deque()])
    record_cell_times: list[list[float]] = field(
        default_factory=lambda: [[] for _ in range(CELL_COUNT)])
    record_cells: list[list[float]] = field(
        default_factory=lambda: [[] for _ in range(CELL_COUNT)])
    record_filtered_cells: list[list[float]] = field(
        default_factory=lambda: [[] for _ in range(CELL_COUNT)])
    record_electrical_times: list[float] = field(default_factory=list)
    record_electrical_raw: list[list[float]] = field(default_factory=lambda: [[], [], []])
    record_electrical_filtered: list[list[float]] = field(default_factory=lambda: [[], [], []])
    saturation_warned: bool = False


class ThrustStandSession:
    """Own transports, full-rate CSV recording, and plot-ready histories."""

    def __init__(self, output: Path | str | None = None, window: float = 30.0) -> None:
        if window <= 0:
            raise ValueError("window must be greater than zero")
        self.window = float(window)
        self.output = Path(output) if output else (
            Path(__file__).resolve().parent.parent / "logs" /
            f"thrust_{datetime.now():%Y%m%d_%H%M%S}.csv")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self._csv_file = self.output.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._csv_file)
        self._writer.writerow(CSV_HEADER)
        self._csv_file.flush()
        self._csv_lock = threading.Lock()
        self._incoming: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started = time.monotonic()
        self._closed = False
        self.rows_written = 0
        self.sources: list[SourceState] = []
        self.status_messages: deque[str] = deque(maxlen=100)
        self.total_times: deque[float] = deque()
        self.total_values: deque[float] = deque()
        self.total_filtered: deque[float] = deque()
        self.recording = False
        self.record_started_mono = 0.0
        self.record_started_at: datetime | None = None
        self.record_total_times: list[float] = []
        self.record_total_values: list[float] = []
        self.record_total_filtered: list[float] = []

    def connect_serial(self, port: str | None = None, baud: int = BAUD) -> SourceState:
        selected = choose_port(port)
        transport = serial.Serial(selected, baud, timeout=0.2)
        time.sleep(1.5)
        transport.reset_input_buffer()
        return self._register_source(transport, f"USB {selected}")

    def connect_wifi(self, host: str = DEFAULT_WIFI_HOST,
                     port: int = DEFAULT_TCP_PORT) -> SourceState:
        return self._register_source(SocketTransport(host, port), f"Wi-Fi {host}:{port}")

    def _register_source(self, transport: Any, endpoint: str) -> SourceState:
        if self._closed:
            transport.close()
            raise RuntimeError("session is closed")
        if self.sources:
            transport.close()
            raise RuntimeError("The one-ESP workflow already has a connected source")
        source = SourceState(1, "ESP", endpoint, transport)
        self.sources.append(source)
        thread = threading.Thread(target=self._read_source, args=(source,), daemon=True)
        self._threads.append(thread)
        thread.start()
        self.status_messages.append(f"{source.label} connected: {endpoint}")
        return source

    def _read_source(self, source: SourceState) -> None:
        while not self._stop.is_set():
            try:
                text = source.transport.readline().decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if text.startswith("#"):
                    self._incoming.put(("status", f"{source.label}: {text[1:].strip()}"))
                    continue
                parts = text.split(",")
                sample, power = parse_sample(parts), parse_power(parts)
                if sample is None and power is None:
                    continue
                host_mono = time.monotonic()
                self._write_csv(source, sample, power)
                self._incoming.put(("data" if sample is not None else "power",
                                    source, host_mono, sample or power))
            except ValueError as exc:
                self._incoming.put(("status", f"{source.label}: skipped invalid data ({exc})"))
            except (serial.SerialException, OSError, ConnectionError) as exc:
                if not self._stop.is_set():
                    self._incoming.put(("status", f"{source.label}: connection lost ({exc})"))
                break

    def _write_csv(self, source: SourceState, sample: dict[str, Any] | None,
                   power: dict[str, Any] | None) -> None:
        host_iso = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with self._csv_lock:
            if sample is not None:
                self._writer.writerow([
                    host_iso, "thrust", source.label, source.endpoint,
                    sample["esp_ms"],
                    *[sample[f"raw{index + 1}"] for index in range(CELL_COUNT)],
                    *[sample[f"kgf{index + 1}"] for index in range(CELL_COUNT)],
                    sample["total_kgf"],
                    sample["force_n"], sample["filtered_n"], sample["cell1_lbf"],
                    sample["cell2_lbf"], sample["cell3_lbf"], sample["cell4_lbf"],
                    sample["total_lbf"], sample["filtered_lbf"],
                    "", "", "", "", "", "", "", "",
                ])
            else:
                assert power is not None
                self._writer.writerow([
                    host_iso, "electrical", source.label, source.endpoint,
                    power["esp_ms"],
                    *[""] * 4, *[""] * 4, "", "", "",
                    *[""] * 4, "", "",
                    power["current_raw"], power["voltage_raw"], power["current_a"],
                    power["voltage_v"], power["power_w"], power["filtered_current_a"],
                    power["filtered_voltage_v"], power["filtered_power_w"],
                ])
            self.rows_written += 1
            if self.rows_written % 10 == 0:
                self._csv_file.flush()

    def poll(self) -> list[str]:
        """Move queued samples into histories; call before rendering a plot."""
        new_status: list[str] = []
        while True:
            try:
                item = self._incoming.get_nowait()
            except queue.Empty:
                break
            if item[0] == "status":
                message = str(item[1])
                self.status_messages.append(message)
                new_status.append(message)
            elif item[0] == "power":
                self._process_power(item[1], float(item[2]), item[3], new_status)
            else:
                self._process_sample(item[1], float(item[2]), item[3], new_status)
        return new_status

    def _process_power(self, source: SourceState, host_mono: float,
                       power: dict[str, Any], statuses: list[str]) -> None:
        elapsed = host_mono - self._started
        saturated = int(power["current_raw"]) >= 4000 or int(power["voltage_raw"]) >= 4000
        if saturated and not source.saturation_warned:
            source.saturation_warned = True
            message = f"{source.label}: electrical ADC near saturation; reduce signal voltage"
            self.status_messages.append(message)
            statuses.append(message)
        elif (source.saturation_warned and int(power["current_raw"]) < 3800 and
              int(power["voltage_raw"]) < 3800):
            source.saturation_warned = False
        raw = [float(power["voltage_v"]), float(power["current_a"]), float(power["power_w"])]
        filtered = [float(power["filtered_voltage_v"]),
                    float(power["filtered_current_a"]), float(power["filtered_power_w"])]
        source.electrical_times.append(elapsed)
        for index in range(3):
            source.electrical_raw[index].append(raw[index])
            source.electrical_filtered[index].append(filtered[index])
        if self.recording:
            source.record_electrical_times.append(host_mono - self.record_started_mono)
            for index in range(3):
                source.record_electrical_raw[index].append(raw[index])
                source.record_electrical_filtered[index].append(filtered[index])
        while source.electrical_times and elapsed - source.electrical_times[0] > self.window:
            source.electrical_times.popleft()
            for series in source.electrical_raw + source.electrical_filtered:
                series.popleft()

    def _process_sample(self, source: SourceState, host_mono: float,
                        sample: dict[str, Any], statuses: list[str]) -> None:
        elapsed = host_mono - self._started
        forces = [
            float(sample[f"cell{index + 1}_lbf"]) for index in range(CELL_COUNT)]
        counts = [
            float(sample[f"raw{index + 1}"]) for index in range(CELL_COUNT)]
        filtered_displayed = [math.nan] * CELL_COUNT
        for index, force in enumerate(forces):
            value = force if math.isfinite(force) else math.nan
            if not math.isfinite(value) and counts[index] != 0.0:
                message = (f"{source.label} cell {index + 1}: "
                           "calibration required; no calibrated lbf data")
                self.status_messages.append(message)
                statuses.append(message)
            if math.isfinite(value):
                previous = source.filter_state[index]
                filtered_displayed[index] = (
                    value if not math.isfinite(previous)
                    else previous + EMA_ALPHA * (value - previous))
                source.filter_state[index] = filtered_displayed[index]
                source.cell_times[index].append(elapsed)
                source.cells[index].append(value)
                source.filtered_cells[index].append(filtered_displayed[index])
                source.latest_cells[index] = value
                source.latest_filtered_cells[index] = filtered_displayed[index]
                source.latest_cell_times[index] = host_mono
            if self.recording and math.isfinite(value):
                source.record_cell_times[index].append(host_mono - self.record_started_mono)
                source.record_cells[index].append(value)
                source.record_filtered_cells[index].append(filtered_displayed[index])
            while source.cell_times[index] and elapsed - source.cell_times[index][0] > self.window:
                source.cell_times[index].popleft()
                source.cells[index].popleft()
                source.filtered_cells[index].popleft()

        active_raw: list[float] = []
        active_filtered: list[float] = []
        for active_source in self.sources:
            for index in range(CELL_COUNT):
                fresh = host_mono - active_source.latest_cell_times[index] <= TOTAL_FRESHNESS_SECONDS
                if fresh and math.isfinite(active_source.latest_cells[index]):
                    active_raw.append(active_source.latest_cells[index])
                if fresh and math.isfinite(active_source.latest_filtered_cells[index]):
                    active_filtered.append(active_source.latest_filtered_cells[index])
        combined = sum(active_raw) if active_raw else math.nan
        combined_filtered = sum(active_filtered) if active_filtered else math.nan
        self.total_times.append(elapsed)
        self.total_values.append(combined)
        self.total_filtered.append(combined_filtered)
        if self.recording:
            self.record_total_times.append(host_mono - self.record_started_mono)
            self.record_total_values.append(combined)
            self.record_total_filtered.append(combined_filtered)
        while self.total_times and elapsed - self.total_times[0] > self.window:
            self.total_times.popleft()
            self.total_values.popleft()
            self.total_filtered.popleft()

    def send(self, command: str) -> None:
        if not self.sources:
            raise RuntimeError("The ESP is not connected")
        self.sources[0].transport.write((command.strip() + "\n").encode("ascii"))

    def tare(self) -> None:
        self.send("TARE")

    def calibrate_cell(self, cell: int, force_lbf: float) -> None:
        if cell not in range(1, CELL_COUNT + 1):
            raise ValueError("cell must be 1, 2, 3, or 4")
        if not 0 < force_lbf <= 44.0925:
            raise ValueError("force_lbf must be > 0 and <= 44.0925")
        self.send(f"CALLBF{cell} {force_lbf:.6f}")

    def invert(self) -> None:
        self.send("INVERT")

    def reset_load_calibration(self) -> None:
        self.send("RESETCAL")

    def zero_current(self) -> None:
        self.send("ZEROAMPS")

    def calibrate_current(self, amps: float) -> None:
        if not 0 < amps <= 100:
            raise ValueError("amps must be > 0 and <= 100")
        self.send(f"CALAMPS {amps:.6f}")

    def zero_voltage(self) -> None:
        self.send("ZEROVOLTS")

    def calibrate_voltage(self, volts: float) -> None:
        if not 0 < volts <= 60:
            raise ValueError("volts must be > 0 and <= 60")
        self.send(f"CALVOLTS {volts:.6f}")

    def power_info(self) -> None:
        self.send("POWERINFO")

    def reset_power_calibration(self) -> None:
        self.send("RESETPOWER")

    def start_recording(self) -> None:
        self.poll()
        self.record_total_times.clear()
        self.record_total_values.clear()
        self.record_total_filtered.clear()
        for source in self.sources:
            groups = (source.record_cell_times + source.record_cells +
                      source.record_filtered_cells + [source.record_electrical_times] +
                      source.record_electrical_raw + source.record_electrical_filtered)
            for series in groups:
                series.clear()
        self.record_started_mono = time.monotonic()
        self.record_started_at = datetime.now()
        self.recording = True

    def stop_recording(self) -> None:
        self.poll()
        self.recording = False

    def close(self) -> None:
        if self._closed:
            return
        self.poll()
        self._stop.set()
        for source in self.sources:
            try:
                source.transport.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=1.0)
        with self._csv_lock:
            self._csv_file.flush()
            self._csv_file.close()
        self._closed = True

    def __enter__(self) -> "ThrustStandSession":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
