#!/usr/bin/env python3
"""Plot and record thrust plus MAUCH electrical data over USB or Wi-Fi."""

from __future__ import annotations

import argparse
import csv
import math
import queue
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import FancyBboxPatch
from matplotlib.widgets import Button, TextBox
import serial
from serial.tools import list_ports


BAUD = 115200
DEFAULT_WIFI_HOST = "192.168.4.1"
DEFAULT_SECOND_HOST = "192.168.4.2"
DEFAULT_TCP_PORT = 3333
STANDARD_GRAVITY = 9.80665
NEWTONS_PER_LBF = 4.4482216152605
SOURCE_COLORS = ("#1f77b4", "#ff7f0e")
EMA_ALPHA = 0.30
TOTAL_FRESHNESS_SECONDS = 1.0
DEFAULT_REFRESH_MS = 200
DEFAULT_MAX_PLOT_POINTS = 450

plt.rcParams["path.simplify"] = True
plt.rcParams["path.simplify_threshold"] = 0.7
plt.rcParams["agg.path.chunksize"] = 10000


def set_rounded_buttons(figure: Any, buttons: list[Button]) -> None:
    """Replace square widget backgrounds with lightweight rounded patches."""
    for button in buttons:
        button.ax.patch.set_visible(False)
        for spine in button.ax.spines.values():
            spine.set_visible(False)
        patch = FancyBboxPatch(
            (0.01, 0.06), 0.98, 0.88,
            boxstyle="round,pad=0.02,rounding_size=0.10",
            transform=button.ax.transAxes,
            facecolor=button.color, edgecolor="#94a3b8", linewidth=1.0,
            clip_on=False, zorder=0)
        button.ax.add_patch(patch)
        button._rounded_patch = patch  # keep the artist alive with the widget
        button._rounded_hovered = False

    def update_hover(event: Any) -> None:
        changed = False
        for button in buttons:
            hovered = event.inaxes is button.ax
            if button._rounded_hovered != hovered:
                button._rounded_patch.set_facecolor(
                    button.hovercolor if hovered else button.color)
                button._rounded_hovered = hovered
                changed = True
        if changed:
            figure.canvas.draw_idle()

    figure.canvas.mpl_connect("motion_notify_event", update_hover)


def limited_plot_data(times: Any, values: Any, maximum: int) -> tuple[Any, Any]:
    """Limit rendered points while preserving full-rate acquisition and CSV data."""
    length = min(len(times), len(values))
    if length <= maximum:
        return times, values
    step = math.ceil(length / maximum)
    return list(times)[::step], list(values)[::step]


class SocketTransport:
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


@dataclass
class SourceState:
    number: int
    label: str
    endpoint: str
    transport: Any
    color: str
    raw_lines: tuple[Any, Any]
    filtered_lines: tuple[Any, Any]
    cell_times: list[deque[float]] = field(default_factory=lambda: [deque(), deque()])
    cells: list[deque[float]] = field(default_factory=lambda: [deque(), deque()])
    filtered_cells: list[deque[float]] = field(default_factory=lambda: [deque(), deque()])
    filter_state: list[float] = field(default_factory=lambda: [math.nan, math.nan])
    force_filter_state: list[float] = field(default_factory=lambda: [math.nan, math.nan])
    cell_units: list[str | None] = field(default_factory=lambda: [None, None])
    latest_cells: list[float] = field(default_factory=lambda: [math.nan, math.nan])
    latest_filtered_cells: list[float] = field(default_factory=lambda: [math.nan, math.nan])
    latest_cell_times: list[float] = field(default_factory=lambda: [0.0, 0.0])
    record_cell_times: list[list[float]] = field(default_factory=lambda: [[], []])
    record_cells: list[list[float]] = field(default_factory=lambda: [[], []])
    record_filtered_cells: list[list[float]] = field(default_factory=lambda: [[], []])
    power_lines: tuple[Any, Any, Any] | None = None
    electrical_times: deque[float] = field(default_factory=deque)
    electrical_raw: list[deque[float]] = field(
        default_factory=lambda: [deque(), deque(), deque()])
    electrical_filtered: list[deque[float]] = field(
        default_factory=lambda: [deque(), deque(), deque()])
    record_electrical_times: list[float] = field(default_factory=list)
    record_electrical_raw: list[list[float]] = field(
        default_factory=lambda: [[], [], []])
    record_electrical_filtered: list[list[float]] = field(
        default_factory=lambda: [[], [], []])
    saturation_warned: bool = False
    cell_peaks: list[float] = field(default_factory=lambda: [-math.inf, -math.inf])


def choose_port(requested: str | None) -> str:
    if requested:
        return requested
    ports = list(list_ports.comports())
    likely = [
        p for p in ports
        if any(tag in (p.description or "").lower()
               for tag in ("cp210", "ch340", "usb serial", "usb jtag", "uart"))
    ]
    if len(likely) == 1:
        return likely[0].device
    if not ports:
        raise RuntimeError("No serial ports found. Connect the ESP32 and try again.")
    details = "\n".join(f"  {p.device}: {p.description}" for p in ports)
    raise RuntimeError(f"Could not choose one ESP32 port. Use --port.\n{details}")


def parse_sample(parts: list[str]) -> dict[str, float | int] | None:
    if not parts or parts[0] != "DATA":
        return None
    if len(parts) == 9:  # dual-cell V3/V4
        esp_ms = int(parts[1])
        raw1, raw2 = int(parts[2]), int(parts[3])
        kgf1, kgf2, total_kgf, force_n, filtered_n = map(float, parts[4:9])
    elif len(parts) == 6:  # backward-compatible single-cell firmware
        esp_ms, raw1 = int(parts[1]), int(parts[2])
        raw2 = 0
        kgf1, force_n, filtered_n = map(float, parts[3:6])
        kgf2 = 0.0
        total_kgf = kgf1
    else:
        return None

    return {
        "esp_ms": esp_ms,
        "raw1": raw1,
        "raw2": raw2,
        "kgf1": kgf1,
        "kgf2": kgf2,
        "total_kgf": total_kgf,
        "force_n": force_n,
        "filtered_n": filtered_n,
        "cell1_lbf": kgf1 * STANDARD_GRAVITY / NEWTONS_PER_LBF,
        "cell2_lbf": kgf2 * STANDARD_GRAVITY / NEWTONS_PER_LBF,
        "total_lbf": force_n / NEWTONS_PER_LBF,
        "filtered_lbf": filtered_n / NEWTONS_PER_LBF,
    }


def parse_power(parts: list[str]) -> dict[str, float | int] | None:
    if not parts or parts[0] != "POWER" or len(parts) != 10:
        return None
    values = list(map(float, parts[4:10]))
    return {
        "esp_ms": int(parts[1]),
        "current_raw": int(parts[2]),
        "voltage_raw": int(parts[3]),
        "current_a": values[0],
        "voltage_v": values[1],
        "power_w": values[2],
        "filtered_current_a": values[3],
        "filtered_voltage_v": values[4],
        "filtered_power_w": values[5],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="USB serial port (auto-detected when unambiguous)")
    parser.add_argument("--baud", type=int, default=BAUD)
    parser.add_argument("--wifi", action="store_true",
                        help="Connect ESP 1 over Wi-Fi instead of USB")
    parser.add_argument("--offline", action="store_true",
                        help="Open the dashboard without connecting to an ESP")
    parser.add_argument("--host", default=DEFAULT_WIFI_HOST,
                        help=f"ESP 1 Wi-Fi address (default: {DEFAULT_WIFI_HOST})")
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT)
    parser.add_argument("--window", type=float, default=30.0,
                        help="Visible plot window in seconds (default: 30)")
    parser.add_argument("--output", type=Path,
                        help="CSV path (default: logs/thrust_YYYYmmdd_HHMMSS.csv)")
    parser.add_argument("--refresh-ms", type=int, default=DEFAULT_REFRESH_MS,
                        help="Dashboard refresh period in milliseconds (default: 200)")
    parser.add_argument("--max-plot-points", type=int,
                        default=DEFAULT_MAX_PLOT_POINTS,
                        help="Maximum rendered points per line (default: 450; CSV remains full rate)")
    args = parser.parse_args()
    if args.refresh_ms < 50:
        parser.error("--refresh-ms must be at least 50")
    if args.max_plot_points < 50:
        parser.error("--max-plot-points must be at least 50")

    output = args.output or (Path(__file__).resolve().parent.parent / "logs" /
                             f"thrust_{datetime.now():%Y%m%d_%H%M%S}.csv")
    output.parent.mkdir(parents=True, exist_ok=True)

    initial_transport: Any | None = None
    initial_endpoint = ""
    startup_status = ""
    if args.offline:
        startup_status = (
            "Offline mode — open Calibration & setup to connect an ESP over Wi-Fi")
    else:
        try:
            if args.wifi:
                initial_transport = SocketTransport(args.host, args.tcp_port)
                initial_endpoint = f"Wi-Fi {args.host}:{args.tcp_port}"
            else:
                port = choose_port(args.port)
                initial_transport = serial.Serial(port, args.baud, timeout=0.2)
                initial_endpoint = f"USB {port}"
                time.sleep(1.5)
                initial_transport.reset_input_buffer()
        except Exception as exc:
            startup_status = (
                f"Offline — initial connection failed: {exc}. "
                "Use Calibration & setup to connect over Wi-Fi")
            print(startup_status, file=sys.stderr)

    csv_file = output.open("w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow(["host_time_iso", "record_type", "source", "endpoint", "esp_time_ms",
                     "raw1", "raw2", "kgf1", "kgf2", "total_kgf",
                     "total_force_N", "filtered_total_force_N", "cell1_lbf",
                     "cell2_lbf", "total_force_lbf", "filtered_total_force_lbf",
                     "current_adc_raw", "voltage_adc_raw", "current_A", "voltage_V",
                     "power_W", "filtered_current_A", "filtered_voltage_V",
                     "filtered_power_W"])
    csv_file.flush()
    csv_lock = threading.Lock()
    rows_written = 0

    incoming: queue.Queue[tuple] = queue.Queue()
    stop = threading.Event()
    reader_threads: list[threading.Thread] = []
    sources: list[SourceState] = []
    connecting = {"active": False}
    plot_started = time.monotonic()
    recording = False
    record_started_mono = 0.0
    record_started_at: datetime | None = None
    peak = -math.inf
    electrical_peaks = [-math.inf, -math.inf, -math.inf]
    last_axes_refresh = 0.0

    total_times: deque[float] = deque()
    total_values: deque[float] = deque()
    total_filtered: deque[float] = deque()
    record_total_times: list[float] = []
    record_total_values: list[float] = []
    record_total_filtered: list[float] = []

    fig = plt.figure(figsize=(15, 9), facecolor="#eef2f6")
    fig.canvas.manager.set_window_title("Thrust Stand — Live Dashboard")
    fig.suptitle("THRUST STAND  •  LIVE DASHBOARD", fontsize=15,
                 fontweight="bold", color="#1f2937", y=0.985)
    grid = fig.add_gridspec(3, 2, left=0.06, right=0.98, top=0.93,
                            bottom=0.19, hspace=0.62, wspace=0.22)
    cell_axes = [
        [fig.add_subplot(grid[0, 0]), fig.add_subplot(grid[1, 0])],
        [fig.add_subplot(grid[0, 0]), fig.add_subplot(grid[1, 0])],
    ]
    total_ax = fig.add_subplot(grid[2, 0])
    electrical_axes = [
        fig.add_subplot(grid[0, 1]),
        fig.add_subplot(grid[1, 1]),
        fig.add_subplot(grid[2, 1]),
    ]

    def style_axis(axis: Any) -> None:
        axis.set_facecolor("#ffffff")
        axis.grid(True, color="#cbd5e1", alpha=0.55, linewidth=0.75,
                  linestyle="--")
        axis.tick_params(colors="#475569", labelsize=9)
        for spine in axis.spines.values():
            spine.set_color("#cbd5e1")
        axis.title.set_color("#0f172a")
        axis.title.set_fontweight("semibold")

    for esp_index, axes in enumerate(cell_axes):
        for cell_index, cell_ax in enumerate(axes):
            style_axis(cell_ax)
            cell_ax.axhline(0, color="#64748b", linewidth=0.8)
            cell_ax.set_title(f"ESP {esp_index + 1} — Load cell {cell_index + 1}")
            cell_ax.set_ylabel("Thrust (lbf)")
            if esp_index == 1:
                cell_ax.set_visible(False)
    cell_peak_texts = [
        [axis.text(0.985, 0.95, "Max: -- lbf", transform=axis.transAxes,
                   ha="right", va="top", fontsize=9.5, color="#0f172a",
                   bbox={"boxstyle": "round,pad=0.25", "facecolor": "#f8fafc",
                         "edgecolor": "#cbd5e1", "alpha": 0.9})
         for axis in axes]
        for axes in cell_axes
    ]
    style_axis(total_ax)
    total_ax.axhline(0, color="#64748b", linewidth=0.8)
    total_ax.set_title("Total thrust — all available load cells")
    total_ax.set_xlabel("Computer time (s)")
    total_ax.set_ylabel("Total thrust (lbf)")
    total_raw_line, = total_ax.plot([], [], color="#555555", linewidth=0.9,
                                    alpha=0.40, label="Combined raw")
    total_filtered_line, = total_ax.plot([], [], color="#111111", linewidth=2.2,
                                         label="Combined filtered")
    total_ax.legend(loc="upper left")
    peak_text = total_ax.text(
        0.985, 0.95, "Max: -- lbf", transform=total_ax.transAxes,
        ha="right", va="top", fontsize=9.5, color="#0f172a",
        bbox={"boxstyle": "round,pad=0.25", "facecolor": "#f8fafc",
              "edgecolor": "#cbd5e1", "alpha": 0.9})
    status_text = fig.text(
        0.01, 0.012, startup_status or f"CSV: {output}",
        fontsize=8.5, color="#334155",
        bbox={"boxstyle": "round,pad=0.3", "facecolor": "#ffffff",
              "edgecolor": "#cbd5e1", "alpha": 0.95})
    electrical_titles = ("Battery voltage", "Current", "Electrical power")
    electrical_units = ("Voltage (V)", "Current (A)", "Power (W)")
    for index, electrical_ax in enumerate(electrical_axes):
        style_axis(electrical_ax)
        electrical_ax.axhline(0, color="#64748b", linewidth=0.8)
        electrical_ax.set_title(electrical_titles[index])
        electrical_ax.set_ylabel(electrical_units[index])
    electrical_axes[-1].set_xlabel("Computer time (s)")
    electrical_peak_texts = [
        axis.text(0.985, 0.95, f"Max: -- {unit}", transform=axis.transAxes,
                  ha="right", va="top", fontsize=9.5, color="#0f172a",
                  bbox={"boxstyle": "round,pad=0.25", "facecolor": "#f8fafc",
                        "edgecolor": "#cbd5e1", "alpha": 0.9})
        for axis, unit in zip(electrical_axes, ("V", "A", "W"))
    ]

    def read_source(source: SourceState) -> None:
        nonlocal rows_written
        while not stop.is_set():
            try:
                line = source.transport.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("#"):
                    incoming.put(("status", f"{source.label}: {line[1:].strip()}"))
                    continue
                parts = line.split(",")
                sample = parse_sample(parts)
                power = parse_power(parts)
                if sample is None and power is None:
                    continue
                host_iso = datetime.now().astimezone().isoformat(timespec="milliseconds")
                with csv_lock:
                    if sample is not None:
                        writer.writerow([
                            host_iso, "thrust", source.label, source.endpoint,
                            sample["esp_ms"], sample["raw1"], sample["raw2"],
                            sample["kgf1"], sample["kgf2"], sample["total_kgf"],
                            sample["force_n"], sample["filtered_n"],
                            sample["cell1_lbf"], sample["cell2_lbf"],
                            sample["total_lbf"], sample["filtered_lbf"],
                            "", "", "", "", "", "", "", "",
                        ])
                    else:
                        writer.writerow([
                            host_iso, "electrical", source.label, source.endpoint,
                            power["esp_ms"], "", "", "", "", "", "", "",
                            "", "", "", "", power["current_raw"],
                            power["voltage_raw"], power["current_a"],
                            power["voltage_v"], power["power_w"],
                            power["filtered_current_a"], power["filtered_voltage_v"],
                            power["filtered_power_w"],
                        ])
                    rows_written += 1
                    if rows_written % 10 == 0:
                        csv_file.flush()
                if sample is not None:
                    incoming.put(("data", source, time.monotonic(), sample))
                else:
                    incoming.put(("power", source, time.monotonic(), power))
            except ValueError as exc:
                incoming.put(("status", f"{source.label}: skipped invalid data ({exc})"))
            except (serial.SerialException, OSError) as exc:
                if not stop.is_set():
                    incoming.put(("status", f"{source.label}: connection lost ({exc})"))
                break

    def register_source(transport: Any, endpoint: str) -> SourceState:
        nonlocal grid
        number = len(sources) + 1
        label = f"ESP {number}"
        color = SOURCE_COLORS[number - 1]
        if number == 2:
            grid = fig.add_gridspec(5, 2, left=0.06, right=0.98, top=0.93,
                                    bottom=0.19, hspace=0.78, wspace=0.22)
            cell_axes[0][0].set_subplotspec(grid[0, 0])
            cell_axes[0][1].set_subplotspec(grid[1, 0])
            cell_axes[1][0].set_subplotspec(grid[2, 0])
            cell_axes[1][1].set_subplotspec(grid[3, 0])
            total_ax.set_subplotspec(grid[4, 0])
            for axis, row in zip(electrical_axes, (0, 2, 4)):
                axis.set_subplotspec(grid[row, 1])
            fig.canvas.draw_idle()
        raw_lines = []
        filtered_lines = []
        for cell_index, cell_ax in enumerate(cell_axes[number - 1]):
            cell_ax.set_visible(True)
            raw_line, = cell_ax.plot([], [], color=color, linewidth=0.8, alpha=0.35,
                                     label="Raw")
            filtered_line, = cell_ax.plot([], [], color=color, linewidth=2.0,
                                          label="Filtered")
            cell_ax.legend(loc="upper left", frameon=True, fancybox=True,
                           framealpha=0.9, edgecolor="#cbd5e1", fontsize=8)
            raw_lines.append(raw_line)
            filtered_lines.append(filtered_line)
        power_lines = []
        for electrical_ax in electrical_axes:
            power_line, = electrical_ax.plot([], [], color=color, linewidth=1.8,
                                              label=label)
            power_lines.append(power_line)
            electrical_ax.legend(loc="upper left", frameon=True, fancybox=True,
                                 framealpha=0.9, edgecolor="#cbd5e1", fontsize=8)
        source = SourceState(number, label, endpoint, transport, color,
                             tuple(raw_lines), tuple(filtered_lines),
                             power_lines=tuple(power_lines))
        sources.append(source)
        thread = threading.Thread(target=read_source, args=(source,), daemon=True)
        reader_threads.append(thread)
        thread.start()
        return source

    if initial_transport is not None:
        register_source(initial_transport, initial_endpoint)

    settings_ax = fig.add_axes([0.05, 0.055, 0.20, 0.055])
    clear_ax = fig.add_axes([0.29, 0.055, 0.17, 0.055])
    record_ax = fig.add_axes([0.51, 0.055, 0.20, 0.055])
    stop_record_ax = fig.add_axes([0.75, 0.055, 0.20, 0.055])

    settings_button = Button(settings_ax, "Calibration & setup",
                             color="#2563eb", hovercolor="#1d4ed8")
    clear_button = Button(clear_ax, "Clear plot",
                          color="#e2e8f0", hovercolor="#cbd5e1")
    record_button = Button(record_ax, "Start recording",
                           color="#16a34a", hovercolor="#15803d")
    stop_record_button = Button(stop_record_ax, "Stop & save plot",
                                color="#f59e0b", hovercolor="#d97706")
    for button in (settings_button, record_button, stop_record_button):
        button.label.set_color("white")
        button.label.set_fontweight("semibold")
    clear_button.label.set_color("#1e293b")
    clear_button.label.set_fontweight("semibold")
    set_rounded_buttons(
        fig, [settings_button, clear_button, record_button, stop_record_button])
    settings_controls: dict[str, Any] = {"figure": None}

    def selected_source() -> SourceState | None:
        target_box = settings_controls.get("target_box")
        try:
            number = int(target_box.text) if target_box is not None else 1
        except ValueError:
            status_text.set_text("Target ESP must be 1 or 2")
            return None
        if number < 1 or number > len(sources):
            status_text.set_text(f"ESP {number} is not connected")
            return None
        return sources[number - 1]

    def send_target(command: str) -> None:
        source = selected_source()
        if source is None:
            return
        try:
            source.transport.write((command + "\n").encode("ascii"))
        except (serial.SerialException, OSError) as exc:
            status_text.set_text(f"{source.label}: write failed ({exc})")

    def calibrate(cell_number: int) -> None:
        force_box = settings_controls.get("force_box")
        try:
            force_lbf = float(force_box.text) if force_box is not None else math.nan
            if not 0 < force_lbf <= 44.0925:
                raise ValueError
        except ValueError:
            status_text.set_text("Calibration force must be > 0 and <= 44.0925 lbf")
            return
        send_target(f"CALLBF{cell_number} {force_lbf:.6f}")

    def calibrate_electrical(kind: str) -> None:
        is_current = kind == "current"
        box = settings_controls.get("current_box" if is_current else "voltage_box")
        maximum = 100.0 if is_current else 60.0
        unit = "A" if is_current else "V"
        try:
            reference = float(box.text) if box is not None else math.nan
            if not 0 < reference <= maximum:
                raise ValueError
        except ValueError:
            status_text.set_text(
                f"Electrical reference must be > 0 and <= {maximum:g} {unit}")
            return
        send_target(f"{'CALAMPS' if is_current else 'CALVOLTS'} {reference:.6f}")

    def clear_plot(_event=None) -> None:
        nonlocal plot_started, peak
        plot_started = time.monotonic()
        peak = -math.inf
        total_times.clear()
        total_values.clear()
        total_filtered.clear()
        for metric_index in range(3):
            electrical_peaks[metric_index] = -math.inf
        for source in sources:
            for cell_index in range(2):
                source.cell_times[cell_index].clear()
                source.cells[cell_index].clear()
                source.filtered_cells[cell_index].clear()
                source.latest_cells[cell_index] = math.nan
                source.latest_filtered_cells[cell_index] = math.nan
                source.latest_cell_times[cell_index] = 0.0
                source.filter_state[cell_index] = math.nan
                source.force_filter_state[cell_index] = math.nan
                source.cell_units[cell_index] = None
                source.cell_peaks[cell_index] = -math.inf
                cell_axes[source.number - 1][cell_index].set_ylabel("Thrust (lbf)")
            source.electrical_times.clear()
            for series in source.electrical_raw + source.electrical_filtered:
                series.clear()

    clear_button.on_clicked(clear_plot)

    def add_esp(_event=None) -> None:
        if len(sources) >= 2:
            status_text.set_text("Two ESPs are already connected")
            return
        if connecting["active"]:
            status_text.set_text("ESP connection is already in progress")
            return
        host_box = settings_controls.get("host_box")
        default_host = args.host if not sources else DEFAULT_SECOND_HOST
        host = host_box.text.strip() if host_box is not None else default_host
        if not host:
            status_text.set_text("Enter the second ESP address")
            return
        connecting["active"] = True
        status_text.set_text(f"Connecting to second ESP at {host}:{args.tcp_port}...")

        def connect() -> None:
            try:
                transport = SocketTransport(host, args.tcp_port)
                incoming.put(("new_source", transport,
                              f"Wi-Fi {host}:{args.tcp_port}"))
            except OSError as exc:
                incoming.put(("connect_error", host, str(exc)))

        threading.Thread(target=connect, daemon=True).start()

    def open_settings(_event=None) -> None:
        existing = settings_controls.get("figure")
        if existing is not None and plt.fignum_exists(existing.number):
            existing.canvas.manager.show()
            return

        settings_fig = plt.figure(figsize=(10.5, 8.0), facecolor="#eef2f6")
        settings_fig.canvas.manager.set_window_title("Thrust stand — calibration & setup")
        settings_fig.suptitle("CALIBRATION & SETUP", fontsize=16,
                              fontweight="bold", color="#1f2937")
        settings_fig.text(
            0.05, 0.91,
            "Select the ESP that receives commands. Electrical calibration is enabled on ESP 1.",
            fontsize=10)

        def labeled_box(bounds: list[float], label: str, initial: str) -> TextBox:
            x, y, _width, height = bounds
            settings_fig.text(x, y + height + 0.010, label, fontsize=9.5,
                              color="#334155", fontweight="semibold")
            return TextBox(settings_fig.add_axes(bounds), "", initial=initial,
                           color="#ffffff", hovercolor="#f8fafc")

        target_box = labeled_box([0.05, 0.81, 0.14, 0.060], "Target ESP", "1")
        host_box = labeled_box(
            [0.27, 0.81, 0.29, 0.060], "ESP Wi-Fi address",
            DEFAULT_SECOND_HOST if sources else args.host)
        add_button = Button(settings_fig.add_axes([0.64, 0.81, 0.29, 0.060]),
                            "+ Add ESP" if sources else "Connect ESP")

        settings_fig.text(0.05, 0.71, "Load-cell calibration", fontsize=13,
                          fontweight="bold")
        tare_button = Button(settings_fig.add_axes([0.05, 0.59, 0.21, 0.065]),
                             "Zero assembled rig")
        force_box = labeled_box([0.31, 0.59, 0.17, 0.065],
                                "Known force (lbf)", "2.20462")
        cal1_button = Button(settings_fig.add_axes([0.53, 0.59, 0.18, 0.065]),
                             "Calibrate cell 1")
        cal2_button = Button(settings_fig.add_axes([0.75, 0.59, 0.18, 0.065]),
                             "Calibrate cell 2")
        invert_button = Button(settings_fig.add_axes([0.05, 0.49, 0.21, 0.060]),
                               "Invert thrust direction")
        reset_load_button = Button(
            settings_fig.add_axes([0.31, 0.49, 0.22, 0.060]),
            "Reset load calibration")

        settings_fig.text(0.05, 0.42, "MAUCH electrical calibration", fontsize=13,
                          fontweight="bold")
        settings_fig.text(
            0.05, 0.365,
            "Zero current: motor off/no current. Zero voltage: propulsion battery disconnected.\n"
            "For span calibration, enter the simultaneous clamp-meter or DVM reading.",
            fontsize=9.5)
        zero_current_button = Button(
            settings_fig.add_axes([0.05, 0.245, 0.21, 0.065]), "Zero current")
        current_box = labeled_box([0.31, 0.245, 0.17, 0.065],
                                  "Reference current (A)", "20.0")
        cal_current_button = Button(
            settings_fig.add_axes([0.53, 0.245, 0.30, 0.065]), "Calibrate current")
        zero_voltage_button = Button(
            settings_fig.add_axes([0.05, 0.135, 0.21, 0.065]), "Zero voltage")
        voltage_box = labeled_box([0.31, 0.135, 0.17, 0.065],
                                  "Reference voltage (V)", "24.0")
        cal_voltage_button = Button(
            settings_fig.add_axes([0.53, 0.135, 0.30, 0.065]), "Calibrate voltage")
        info_button = Button(settings_fig.add_axes([0.05, 0.035, 0.21, 0.060]),
                             "Read calibration info")
        reset_power_button = Button(
            settings_fig.add_axes([0.53, 0.035, 0.30, 0.060]),
            "Reset electrical calibration")

        all_settings_buttons = [
            add_button, tare_button, cal1_button, cal2_button, invert_button,
            reset_load_button, zero_current_button, cal_current_button,
            zero_voltage_button, cal_voltage_button, info_button,
            reset_power_button,
        ]
        for button in all_settings_buttons:
            button.color = "#e2e8f0"
            button.hovercolor = "#cbd5e1"
            button.ax.set_facecolor(button.color)
            button.label.set_color("#1e293b")
            button.label.set_fontweight("semibold")
        for button in (cal1_button, cal2_button, cal_current_button,
                       cal_voltage_button, add_button):
            button.color = "#bfdbfe"
            button.hovercolor = "#93c5fd"
            button.ax.set_facecolor(button.color)
        for button in (reset_load_button, reset_power_button):
            button.color = "#fee2e2"
            button.hovercolor = "#fecaca"
            button.ax.set_facecolor(button.color)
        set_rounded_buttons(settings_fig, all_settings_buttons)

        settings_controls.update({
            "figure": settings_fig, "target_box": target_box,
            "host_box": host_box, "add_button": add_button,
            "force_box": force_box,
            "current_box": current_box, "voltage_box": voltage_box,
            "widgets": [add_button, tare_button, cal1_button, cal2_button,
                        invert_button, reset_load_button, zero_current_button, cal_current_button,
                        zero_voltage_button, cal_voltage_button, info_button,
                        reset_power_button],
        })
        add_button.on_clicked(add_esp)
        tare_button.on_clicked(lambda _click: send_target("TARE"))
        cal1_button.on_clicked(lambda _click: calibrate(1))
        cal2_button.on_clicked(lambda _click: calibrate(2))
        invert_button.on_clicked(lambda _click: send_target("INVERT"))
        reset_load_button.on_clicked(lambda _click: send_target("RESETCAL"))
        zero_current_button.on_clicked(lambda _click: send_target("ZEROAMPS"))
        cal_current_button.on_clicked(
            lambda _click: calibrate_electrical("current"))
        zero_voltage_button.on_clicked(lambda _click: send_target("ZEROVOLTS"))
        cal_voltage_button.on_clicked(
            lambda _click: calibrate_electrical("voltage"))
        info_button.on_clicked(lambda _click: send_target("POWERINFO"))
        reset_power_button.on_clicked(lambda _click: send_target("RESETPOWER"))
        settings_fig.show()

    settings_button.on_clicked(open_settings)

    def start_recording(_event=None) -> None:
        nonlocal recording, record_started_mono, record_started_at
        record_total_times.clear()
        record_total_values.clear()
        record_total_filtered.clear()
        for source in sources:
            for cell_index in range(2):
                source.record_cell_times[cell_index].clear()
                source.record_cells[cell_index].clear()
                source.record_filtered_cells[cell_index].clear()
            source.record_electrical_times.clear()
            for series in (source.record_electrical_raw +
                           source.record_electrical_filtered):
                series.clear()
        record_started_mono = time.monotonic()
        record_started_at = datetime.now()
        recording = True
        record_button.color = "#14532d"
        record_ax.set_facecolor("#14532d")
        status_text.set_text("Recording all connected ESPs...")

    def stop_and_save_plot(_event=None) -> None:
        nonlocal recording
        recording = False
        record_button.color = "#16a34a"
        record_ax.set_facecolor("#16a34a")
        if (not record_total_times and
                not any(source.record_electrical_times for source in sources)):
            status_text.set_text("No samples were captured; start recording first")
            return

        stamp = (record_started_at or datetime.now()).strftime("%Y%m%d_%H%M%S")
        image_path = output.parent / f"thrust_plot_{stamp}.png"
        thrust_plot_count = len(sources) * 2 + 1
        saved_fig = Figure(figsize=(16, 3.0 * max(thrust_plot_count, 3)), dpi=150,
                           facecolor="#eef2f6")
        FigureCanvasAgg(saved_fig)
        saved_fig.suptitle("THRUST STAND  •  RECORDED TEST", fontsize=16,
                           fontweight="bold", color="#1f2937")
        saved_grid = saved_fig.add_gridspec(max(thrust_plot_count, 3), 2,
                                             hspace=0.62, wspace=0.25)
        saved_axes = [saved_fig.add_subplot(saved_grid[i, 0])
                      for i in range(thrust_plot_count)]
        axis_index = 0
        for source in sources:
            for cell_index in range(2):
                saved_ax = saved_axes[axis_index]
                axis_index += 1
                saved_ax.plot(source.record_cell_times[cell_index],
                              source.record_cells[cell_index],
                              color=source.color, linewidth=0.8, alpha=0.35,
                              label="Raw")
                saved_ax.plot(source.record_cell_times[cell_index],
                              source.record_filtered_cells[cell_index],
                              color=source.color, linewidth=2.0, label="Filtered")
                saved_ax.axhline(0, color="black", linewidth=0.7)
                saved_ax.set_title(f"{source.label} — Load cell {cell_index + 1}")
                saved_ax.set_ylabel(source.cell_units[cell_index] or "Thrust (lbf)")
                style_axis(saved_ax)
                saved_ax.legend(loc="upper left", framealpha=0.9,
                                edgecolor="#cbd5e1", fontsize=8)
                recorded_cell = [
                    value for value in source.record_filtered_cells[cell_index]
                    if math.isfinite(value)
                ]
                peak_label = "Max: -- lbf"
                if source.cell_units[cell_index] == "Thrust (lbf)" and recorded_cell:
                    peak_label = f"Max: {max(recorded_cell):.3f} lbf"
                saved_ax.text(
                    0.985, 0.95, peak_label, transform=saved_ax.transAxes,
                    ha="right", va="top", fontsize=9.5,
                    bbox={"boxstyle": "round,pad=0.25", "facecolor": "#f8fafc",
                          "edgecolor": "#cbd5e1", "alpha": 0.9})
        saved_total_ax = saved_axes[-1]
        saved_total_ax.plot(record_total_times, record_total_values,
                            color="#555555", linewidth=0.9, alpha=0.40,
                            label="Combined raw")
        saved_total_ax.plot(record_total_times, record_total_filtered,
                            color="#111111", linewidth=2.2,
                            label="Combined filtered")
        saved_total_ax.axhline(0, color="black", linewidth=0.7)
        saved_total_ax.set_title("Total thrust — all available load cells")
        saved_total_ax.set_xlabel("Computer time (s)")
        saved_total_ax.set_ylabel("Total thrust (lbf)")
        style_axis(saved_total_ax)
        saved_total_ax.legend(loc="upper left", framealpha=0.9,
                              edgecolor="#cbd5e1", fontsize=8)
        recorded_values = [v for v in record_total_filtered if math.isfinite(v)]
        if recorded_values:
            saved_total_ax.text(0.985, 0.95, f"Max: {max(recorded_values):.3f} lbf",
                                transform=saved_total_ax.transAxes,
                                ha="right", va="top", fontsize=9.5,
                                bbox={"boxstyle": "round,pad=0.25",
                                      "facecolor": "#f8fafc",
                                      "edgecolor": "#cbd5e1", "alpha": 0.9})
        electrical_spans = []
        row_count = max(thrust_plot_count, 3)
        electrical_rows = (0, 1, 2) if row_count == 3 else (0, 2, 4)
        for row in electrical_rows:
            electrical_spans.append(saved_fig.add_subplot(saved_grid[row, 1]))
        for metric_index, saved_ax in enumerate(electrical_spans):
            for source in sources:
                saved_ax.plot(source.record_electrical_times,
                              source.record_electrical_raw[metric_index],
                              color=source.color, linewidth=0.8, alpha=0.35)
                saved_ax.plot(source.record_electrical_times,
                              source.record_electrical_filtered[metric_index],
                              color=source.color, linewidth=2.0,
                              label=source.label)
            saved_ax.axhline(0, color="black", linewidth=0.7)
            saved_ax.set_title(electrical_titles[metric_index])
            saved_ax.set_ylabel(electrical_units[metric_index])
            style_axis(saved_ax)
            if any(source.record_electrical_times for source in sources):
                saved_ax.legend(loc="upper left", framealpha=0.9,
                                edgecolor="#cbd5e1", fontsize=8)
            recorded_electrical = [
                value
                for source in sources
                for value in source.record_electrical_filtered[metric_index]
                if math.isfinite(value)
            ]
            unit = ("V", "A", "W")[metric_index]
            label = (f"Max: {max(recorded_electrical):.3f} {unit}"
                     if recorded_electrical else f"Max: -- {unit}")
            saved_ax.text(
                0.985, 0.95, label, transform=saved_ax.transAxes,
                ha="right", va="top", fontsize=9.5,
                bbox={"boxstyle": "round,pad=0.25", "facecolor": "#f8fafc",
                      "edgecolor": "#cbd5e1", "alpha": 0.9})
        electrical_spans[-1].set_xlabel("Computer time (s)")
        saved_fig.tight_layout(rect=(0, 0, 1, 0.97))
        saved_fig.savefig(image_path, bbox_inches="tight")
        status_text.set_text(f"Saved plot for {len(sources)} ESP(s): {image_path}")

    record_button.on_clicked(start_recording)
    stop_record_button.on_clicked(stop_and_save_plot)

    def update(_frame):
        nonlocal peak, last_axes_refresh
        newest_status = None
        while True:
            try:
                item = incoming.get_nowait()
            except queue.Empty:
                break

            if item[0] == "status":
                newest_status = item[1]
                continue
            if item[0] == "connect_error":
                connecting["active"] = False
                newest_status = f"Could not connect to {item[1]}: {item[2]}"
                continue
            if item[0] == "new_source":
                connecting["active"] = False
                register_source(item[1], item[2])
                newest_status = f"ESP {len(sources)} connected: {item[2]}"
                host_box = settings_controls.get("host_box")
                add_button = settings_controls.get("add_button")
                if len(sources) == 1:
                    if host_box is not None:
                        host_box.set_val(DEFAULT_SECOND_HOST)
                    if add_button is not None:
                        add_button.label.set_text("+ Add ESP")
                continue

            if item[0] == "power":
                _, source, host_mono, power = item
                t = host_mono - plot_started
                saturated = (int(power["current_raw"]) >= 4000 or
                             int(power["voltage_raw"]) >= 4000)
                if saturated and not source.saturation_warned:
                    source.saturation_warned = True
                    newest_status = (
                        f"{source.label}: electrical ADC near saturation; "
                        "reduce the signal voltage before trusting peaks")
                elif (source.saturation_warned and
                      int(power["current_raw"]) < 3800 and
                      int(power["voltage_raw"]) < 3800):
                    source.saturation_warned = False
                raw_values = [float(power["voltage_v"]),
                              float(power["current_a"]),
                              float(power["power_w"])]
                filtered_values = [float(power["filtered_voltage_v"]),
                                   float(power["filtered_current_a"]),
                                   float(power["filtered_power_w"])]
                source.electrical_times.append(t)
                for metric_index in range(3):
                    source.electrical_raw[metric_index].append(
                        raw_values[metric_index])
                    source.electrical_filtered[metric_index].append(
                        filtered_values[metric_index])
                    if math.isfinite(filtered_values[metric_index]):
                        electrical_peaks[metric_index] = max(
                            electrical_peaks[metric_index],
                            filtered_values[metric_index])
                if recording:
                    source.record_electrical_times.append(
                        host_mono - record_started_mono)
                    for metric_index in range(3):
                        source.record_electrical_raw[metric_index].append(
                            raw_values[metric_index])
                        source.record_electrical_filtered[metric_index].append(
                            filtered_values[metric_index])
                while (source.electrical_times and
                       t - source.electrical_times[0] > args.window):
                    source.electrical_times.popleft()
                    for series in (source.electrical_raw +
                                   source.electrical_filtered):
                        series.popleft()
                continue

            _, source, host_mono, sample = item
            t = host_mono - plot_started
            force_values = [float(sample["cell1_lbf"]),
                            float(sample["cell2_lbf"])]
            raw_counts = [float(sample["raw1"]), float(sample["raw2"])]
            cell_values = [math.nan, math.nan]
            filtered_values = [math.nan, math.nan]
            for cell_index, force_value in enumerate(force_values):
                if source.cell_units[cell_index] is None:
                    if math.isfinite(force_value):
                        source.cell_units[cell_index] = "Thrust (lbf)"
                    elif raw_counts[cell_index] != 0.0:
                        source.cell_units[cell_index] = "ADC counts (uncalibrated)"
                        newest_status = (f"{source.label} cell {cell_index + 1}: "
                                         "ADC data received; calibration required for lbf")
                    if source.cell_units[cell_index]:
                        cell_axes[source.number - 1][cell_index].set_ylabel(
                            source.cell_units[cell_index])

                if source.cell_units[cell_index] == "Thrust (lbf)":
                    display_value = force_value
                elif source.cell_units[cell_index] == "ADC counts (uncalibrated)":
                    display_value = (raw_counts[cell_index]
                                     if raw_counts[cell_index] != 0.0 else math.nan)
                else:
                    display_value = math.nan
                cell_values[cell_index] = display_value

                if math.isfinite(display_value):
                    previous = source.filter_state[cell_index]
                    if math.isfinite(previous):
                        filtered_value = previous + EMA_ALPHA * (display_value - previous)
                    else:
                        filtered_value = display_value
                    source.filter_state[cell_index] = filtered_value
                    filtered_values[cell_index] = filtered_value

                if math.isfinite(force_value):
                    previous_force = source.force_filter_state[cell_index]
                    if math.isfinite(previous_force):
                        filtered_force = (previous_force +
                                          EMA_ALPHA * (force_value - previous_force))
                    else:
                        filtered_force = force_value
                    source.force_filter_state[cell_index] = filtered_force
                    source.latest_cells[cell_index] = force_value
                    source.latest_filtered_cells[cell_index] = filtered_force
                    source.latest_cell_times[cell_index] = host_mono
                    source.cell_peaks[cell_index] = max(
                        source.cell_peaks[cell_index], filtered_force)
                if math.isfinite(display_value):
                    source.cell_times[cell_index].append(t)
                    source.cells[cell_index].append(display_value)
                    source.filtered_cells[cell_index].append(
                        filtered_values[cell_index])

            if recording:
                for cell_index in range(2):
                    if math.isfinite(cell_values[cell_index]):
                        source.record_cell_times[cell_index].append(
                            host_mono - record_started_mono)
                        source.record_cells[cell_index].append(
                            cell_values[cell_index])
                        source.record_filtered_cells[cell_index].append(
                            filtered_values[cell_index])

            active_raw: list[float] = []
            active_filtered: list[float] = []
            for active_source in sources:
                for cell_index in range(2):
                    fresh = (host_mono - active_source.latest_cell_times[cell_index]
                             <= TOTAL_FRESHNESS_SECONDS)
                    raw_value = active_source.latest_cells[cell_index]
                    filtered_value = active_source.latest_filtered_cells[cell_index]
                    if fresh and math.isfinite(raw_value):
                        active_raw.append(raw_value)
                    if fresh and math.isfinite(filtered_value):
                        active_filtered.append(filtered_value)
            combined_total = sum(active_raw) if active_raw else math.nan
            combined_filtered = sum(active_filtered) if active_filtered else math.nan
            total_times.append(t)
            total_values.append(combined_total)
            total_filtered.append(combined_filtered)
            if recording:
                record_total_times.append(host_mono - record_started_mono)
                record_total_values.append(combined_total)
                record_total_filtered.append(combined_filtered)
            if math.isfinite(combined_filtered):
                peak = max(peak, combined_filtered)

            for cell_index in range(2):
                while (source.cell_times[cell_index] and
                       t - source.cell_times[cell_index][0] > args.window):
                    source.cell_times[cell_index].popleft()
                    source.cells[cell_index].popleft()
                    source.filtered_cells[cell_index].popleft()
            while total_times and t - total_times[0] > args.window:
                total_times.popleft()
                total_values.popleft()
                total_filtered.popleft()

        if newest_status:
            status_text.set_text(newest_status)

        latest = 0.0
        refresh_limits = time.monotonic() - last_axes_refresh >= 0.75
        if refresh_limits:
            last_axes_refresh = time.monotonic()
        artists: list[Any] = [peak_text, status_text,
                              total_raw_line, total_filtered_line]
        for source in sources:
            for cell_index in range(2):
                raw_line = source.raw_lines[cell_index]
                filtered_line = source.filtered_lines[cell_index]
                raw_line.set_data(*limited_plot_data(
                    source.cell_times[cell_index], source.cells[cell_index],
                    args.max_plot_points))
                filtered_line.set_data(*limited_plot_data(
                    source.cell_times[cell_index],
                    source.filtered_cells[cell_index], args.max_plot_points))
                artists.extend((raw_line, filtered_line))
                cell_peak_text = cell_peak_texts[source.number - 1][cell_index]
                cell_peak = source.cell_peaks[cell_index]
                cell_peak_text.set_text(
                    f"Max: {cell_peak:.3f} lbf" if math.isfinite(cell_peak)
                    else "Max: -- lbf")
                artists.append(cell_peak_text)
                finite_cell = []
                if refresh_limits:
                    finite_cell = [
                        value
                        for series in (source.cells[cell_index],
                                       source.filtered_cells[cell_index])
                        for value in series if math.isfinite(value)
                    ]
                if refresh_limits and finite_cell:
                    low, high = min(finite_cell), max(finite_cell)
                    pad = max(0.5, (high - low) * 0.12)
                    cell_axes[source.number - 1][cell_index].set_ylim(
                        low - pad, high + pad)
                if source.cell_times[cell_index]:
                    latest = max(latest, source.cell_times[cell_index][-1])
            if source.power_lines is not None:
                for metric_index, power_line in enumerate(source.power_lines):
                    power_line.set_data(*limited_plot_data(
                        source.electrical_times,
                        source.electrical_filtered[metric_index],
                        args.max_plot_points))
                    artists.append(power_line)
                if source.electrical_times:
                    latest = max(latest, source.electrical_times[-1])

        total_raw_line.set_data(*limited_plot_data(
            total_times, total_values, args.max_plot_points))
        total_filtered_line.set_data(*limited_plot_data(
            total_times, total_filtered, args.max_plot_points))
        if total_times:
            latest = max(latest, total_times[-1])
        finite_total = []
        if refresh_limits:
            finite_total = [value for series in (total_values, total_filtered)
                            for value in series if math.isfinite(value)]

        if refresh_limits and latest:
            right = max(args.window, latest)
            left = max(0, right - args.window)
            total_ax.set_xlim(left, right)
            for axes in cell_axes:
                for cell_ax in axes:
                    if cell_ax.get_visible():
                        cell_ax.set_xlim(left, right)
            for electrical_ax in electrical_axes:
                electrical_ax.set_xlim(left, right)
        if refresh_limits and finite_total:
            low, high = min(finite_total), max(finite_total)
            pad = max(0.5, (high - low) * 0.12)
            total_ax.set_ylim(low - pad, high + pad)
        for metric_index, electrical_ax in enumerate(electrical_axes):
            finite_electrical = []
            if refresh_limits:
                finite_electrical = [
                    value
                    for source in sources
                    for value in source.electrical_filtered[metric_index]
                    if math.isfinite(value)
                ]
            if refresh_limits and finite_electrical:
                low, high = min(finite_electrical), max(finite_electrical)
                minimum_pad = (0.5, 1.0, 5.0)[metric_index]
                pad = max(minimum_pad, (high - low) * 0.12)
                electrical_ax.set_ylim(min(0.0, low - pad), high + pad)
            unit = ("V", "A", "W")[metric_index]
            electrical_peak = electrical_peaks[metric_index]
            electrical_peak_texts[metric_index].set_text(
                f"Max: {electrical_peak:.3f} {unit}"
                if math.isfinite(electrical_peak) else f"Max: -- {unit}")
            artists.append(electrical_peak_texts[metric_index])
        peak_text.set_text(f"Max: {peak:.3f} lbf" if math.isfinite(peak)
                           else "Max: -- lbf")
        return tuple(artists)

    animation = FuncAnimation(fig, update, interval=args.refresh_ms, blit=False,
                              cache_frame_data=False)
    try:
        plt.show()
    finally:
        stop.set()
        for source in sources:
            try:
                source.transport.close()
            except OSError:
                pass
        for thread in reader_threads:
            thread.join(timeout=1.0)
        with csv_lock:
            csv_file.flush()
            csv_file.close()
        print(f"Saved {rows_written} samples from {len(sources)} ESP(s) to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
