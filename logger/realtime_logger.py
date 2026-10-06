#!/usr/bin/env python3
"""Save ESP32 thrust-stand serial data to CSV and plot force in real time."""

from __future__ import annotations

import argparse
import csv
import math
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.widgets import Button, TextBox
import serial
from serial.tools import list_ports


BAUD = 115200
NEWTONS_PER_LBF = 4.4482216152605


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Serial port (auto-detected when unambiguous)")
    parser.add_argument("--baud", type=int, default=BAUD)
    parser.add_argument("--window", type=float, default=30.0,
                        help="Visible plot window in seconds (default: 30)")
    parser.add_argument("--output", type=Path,
                        help="CSV path (default: logs/thrust_YYYYmmdd_HHMMSS.csv)")
    args = parser.parse_args()

    try:
        port = choose_port(args.port)
        output = args.output or (Path(__file__).resolve().parent.parent / "logs" /
                                 f"thrust_{datetime.now():%Y%m%d_%H%M%S}.csv")
        output.parent.mkdir(parents=True, exist_ok=True)
        ser = serial.Serial(port, args.baud, timeout=0.2)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    # Opening a serial port resets most ESP32 dev boards.
    time.sleep(1.5)
    ser.reset_input_buffer()
    incoming: queue.Queue[tuple] = queue.Queue()
    stop = threading.Event()
    rows_written = 0

    csv_file = output.open("w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow(["host_time_iso", "esp_time_ms", "raw_counts", "kgf",
                     "force_N", "filtered_force_N", "force_lbf",
                     "filtered_force_lbf"])
    csv_file.flush()

    def read_serial() -> None:
        nonlocal rows_written
        while not stop.is_set():
            try:
                line = ser.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("#"):
                    incoming.put(("status", line[1:].strip()))
                    continue
                parts = line.split(",")
                if len(parts) != 6 or parts[0] != "DATA":
                    continue
                esp_ms, raw = int(parts[1]), int(parts[2])
                kgf, force_n, filtered_n = map(float, parts[3:6])
                host_time = datetime.now().astimezone().isoformat(timespec="milliseconds")
                force_lbf = force_n / NEWTONS_PER_LBF
                filtered_lbf = filtered_n / NEWTONS_PER_LBF
                writer.writerow([host_time, esp_ms, raw, kgf, force_n, filtered_n,
                                 force_lbf, filtered_lbf])
                rows_written += 1
                if rows_written % 10 == 0:
                    csv_file.flush()
                incoming.put(("data", esp_ms, force_lbf, filtered_lbf))
            except (ValueError, serial.SerialException) as exc:
                incoming.put(("status", f"Reader error: {exc}"))
                if isinstance(exc, serial.SerialException):
                    stop.set()

    reader = threading.Thread(target=read_serial, daemon=True)
    reader.start()

    times: deque[float] = deque()
    forces: deque[float] = deque()
    filtered: deque[float] = deque()
    run_start_ms: int | None = None
    peak = -math.inf
    recording = False
    record_start_ms: int | None = None
    record_times: list[float] = []
    record_forces: list[float] = []
    record_filtered: list[float] = []
    record_started_at: datetime | None = None

    fig, ax = plt.subplots(figsize=(11, 6))
    plt.subplots_adjust(bottom=0.30)
    raw_line, = ax.plot([], [], color="#9bb8d3", linewidth=0.8, alpha=0.65,
                        label="Force (raw)")
    filtered_line, = ax.plot([], [], color="#d62728", linewidth=1.8,
                             label="Force (EMA)")
    ax.axhline(0, color="black", linewidth=0.7)
    ax.set_title("ESP32 Thrust Stand")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Thrust (lbf)")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left")
    status_text = fig.text(0.01, 0.01, f"{port} • CSV: {output}", fontsize=9)
    peak_text = ax.text(0.99, 0.98, "Peak: -- lbf", transform=ax.transAxes,
                        ha="right", va="top", fontsize=11)

    tare_ax = fig.add_axes([0.04, 0.15, 0.15, 0.06])
    cal_box_ax = fig.add_axes([0.28, 0.15, 0.13, 0.06])
    cal_ax = fig.add_axes([0.42, 0.15, 0.16, 0.06])
    invert_ax = fig.add_axes([0.63, 0.15, 0.14, 0.06])
    clear_ax = fig.add_axes([0.81, 0.15, 0.14, 0.06])
    record_ax = fig.add_axes([0.28, 0.065, 0.18, 0.06])
    stop_record_ax = fig.add_axes([0.53, 0.065, 0.20, 0.06])
    tare_button = Button(tare_ax, "Zero assembled rig")
    cal_box = TextBox(cal_box_ax, "Force lbf ", initial="2.20462")
    cal_button = Button(cal_ax, "Calibrate force")
    invert_button = Button(invert_ax, "Invert direction")
    clear_button = Button(clear_ax, "Clear plot")
    record_button = Button(record_ax, "Start recording")
    stop_record_button = Button(stop_record_ax, "Stop & save plot")

    def send(command: str) -> None:
        try:
            ser.write((command + "\n").encode("ascii"))
        except serial.SerialException as exc:
            status_text.set_text(f"Serial write error: {exc}")

    tare_button.on_clicked(lambda _event: send("TARE"))

    def calibrate(_event) -> None:
        try:
            force_lbf = float(cal_box.text)
            if not 0 < force_lbf <= 44.0925:
                raise ValueError
            send(f"CALLBF {force_lbf:.6f}")
        except ValueError:
            status_text.set_text("Calibration force must be > 0 and <= 44.0925 lbf")

    cal_button.on_clicked(calibrate)
    invert_button.on_clicked(lambda _event: send("INVERT"))

    def clear_plot(_event=None) -> None:
        nonlocal run_start_ms, peak
        times.clear(); forces.clear(); filtered.clear()
        run_start_ms = None
        peak = -math.inf

    clear_button.on_clicked(clear_plot)

    def start_recording(_event=None) -> None:
        nonlocal recording, record_start_ms, record_started_at
        record_times.clear()
        record_forces.clear()
        record_filtered.clear()
        record_start_ms = None
        record_started_at = datetime.now()
        recording = True
        record_button.color = "#b7e4c7"
        record_ax.set_facecolor("#b7e4c7")
        status_text.set_text("Recording plot... click Stop & save plot when finished")

    def stop_and_save_plot(_event=None) -> None:
        nonlocal recording
        recording = False
        record_button.color = "0.85"
        record_ax.set_facecolor("0.85")
        if not record_times:
            status_text.set_text("No samples were captured; start recording first")
            return

        stamp = (record_started_at or datetime.now()).strftime("%Y%m%d_%H%M%S")
        image_path = output.parent / f"thrust_plot_{stamp}.png"
        saved_fig = Figure(figsize=(12, 7), dpi=150)
        FigureCanvasAgg(saved_fig)
        saved_ax = saved_fig.add_subplot(1, 1, 1)
        saved_ax.plot(record_times, record_forces, color="#9bb8d3", linewidth=0.8,
                      alpha=0.7, label="Force (raw)")
        saved_ax.plot(record_times, record_filtered, color="#d62728", linewidth=1.8,
                      label="Force (EMA)")
        saved_ax.axhline(0, color="black", linewidth=0.7)
        saved_ax.set_title("ESP32 Thrust Stand Recorded Run")
        saved_ax.set_xlabel("Time (s)")
        saved_ax.set_ylabel("Thrust (lbf)")
        saved_ax.grid(True, alpha=0.25)
        saved_ax.legend(loc="upper left")
        finite_recorded = [v for v in record_forces if math.isfinite(v)]
        if finite_recorded:
            saved_peak = max(finite_recorded)
            saved_ax.text(0.99, 0.98, f"Peak: {saved_peak:.3f} lbf",
                          transform=saved_ax.transAxes, ha="right", va="top")
        saved_fig.tight_layout()
        saved_fig.savefig(image_path, bbox_inches="tight")
        status_text.set_text(f"Saved complete plot: {image_path}")

    record_button.on_clicked(start_recording)
    stop_record_button.on_clicked(stop_and_save_plot)

    def update(_frame):
        nonlocal run_start_ms, peak, record_start_ms
        newest_status = None
        while True:
            try:
                item = incoming.get_nowait()
            except queue.Empty:
                break
            if item[0] == "status":
                newest_status = item[1]
                continue
            _, esp_ms, force_n, filtered_n = item
            if run_start_ms is None:
                run_start_ms = esp_ms
            t = (esp_ms - run_start_ms) / 1000.0
            times.append(t); forces.append(force_n); filtered.append(filtered_n)
            if recording:
                if record_start_ms is None:
                    record_start_ms = esp_ms
                record_times.append((esp_ms - record_start_ms) / 1000.0)
                record_forces.append(force_n)
                record_filtered.append(filtered_n)
            if math.isfinite(force_n):
                peak = max(peak, force_n)
            while times and t - times[0] > args.window:
                times.popleft(); forces.popleft(); filtered.popleft()

        if newest_status:
            status_text.set_text(newest_status)
        if times:
            raw_line.set_data(times, forces)
            filtered_line.set_data(times, filtered)
            right = max(args.window, times[-1])
            ax.set_xlim(max(0, right - args.window), right)
            finite_y = [v for v in list(forces) + list(filtered) if math.isfinite(v)]
            if finite_y:
                low, high = min(finite_y), max(finite_y)
                pad = max(0.5, (high - low) * 0.12)
                ax.set_ylim(low - pad, high + pad)
        peak_text.set_text(f"Peak: {peak:.3f} lbf" if math.isfinite(peak) else "Peak: -- lbf")
        return raw_line, filtered_line, status_text, peak_text

    animation = FuncAnimation(fig, update, interval=100, blit=False,
                              cache_frame_data=False)
    try:
        plt.show()
    finally:
        stop.set()
        reader.join(timeout=1.0)
        csv_file.flush()
        csv_file.close()
        ser.close()
        print(f"Saved {rows_written} samples to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
