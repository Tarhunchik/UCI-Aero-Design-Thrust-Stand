"""Matplotlib figures rendered inline by VS Code's Python/Jupyter viewer."""

from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from realtime_logger import CELL_COUNT, ThrustStandSession

CELL_COLORS = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728")


def _finite_max(values: Iterable[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    return max(finite) if finite else None


def _style(axis, title: str, ylabel: str) -> None:
    axis.set_title(title, fontweight="semibold")
    axis.set_ylabel(ylabel)
    axis.grid(True, alpha=0.35, linestyle="--")
    axis.axhline(0, color="#64748b", linewidth=0.8)


def render_dashboard(session: ThrustStandSession, recorded: bool = False) -> Figure:
    """Build a static figure for inline display or VS Code's Plot Viewer."""
    session.poll()
    row_count = CELL_COUNT + 1
    figure = plt.figure(figsize=(15, 2.8 * row_count), layout="constrained")
    grid = figure.add_gridspec(row_count, 2)
    figure.suptitle(
        "THRUST STAND — RECORDED TEST" if recorded else "THRUST STAND — LIVE",
        fontsize=16, fontweight="bold")

    source = session.sources[0] if session.sources else None
    for cell_index in range(CELL_COUNT):
        axis = figure.add_subplot(grid[cell_index, 0])
        _style(axis, f"Load cell {cell_index + 1}", "Thrust (lbf)")
        if source:
            times = (source.record_cell_times[cell_index] if recorded
                     else source.cell_times[cell_index])
            raw = (source.record_cells[cell_index] if recorded
                   else source.cells[cell_index])
            filtered = (source.record_filtered_cells[cell_index] if recorded
                        else source.filtered_cells[cell_index])
            axis.plot(times, raw, color=CELL_COLORS[cell_index], alpha=0.35,
                      linewidth=0.9, label="Measured (lbf)")
            axis.plot(times, filtered, color=CELL_COLORS[cell_index],
                      linewidth=2.0, label="Smoothed (lbf)")
            if times:
                axis.legend(loc="upper left")
            peak = _finite_max(filtered)
            if peak is not None:
                axis.text(0.99, 0.96, f"Max: {peak:.3f} lbf",
                          transform=axis.transAxes, ha="right", va="top")

    total_axis = figure.add_subplot(grid[CELL_COUNT, 0])
    total_times = session.record_total_times if recorded else session.total_times
    total_raw = session.record_total_values if recorded else session.total_values
    total_filtered = (
        session.record_total_filtered if recorded else session.total_filtered)
    _style(total_axis, "Total thrust — all fresh calibrated cells", "Thrust (lbf)")
    total_axis.set_xlabel("Computer time (s)")
    total_axis.plot(total_times, total_raw, color="#555555", alpha=0.35,
                    linewidth=0.9, label="Combined measured (lbf)")
    total_axis.plot(total_times, total_filtered, color="#111111",
                    linewidth=2.2, label="Combined smoothed (lbf)")
    if total_times:
        total_axis.legend(loc="upper left")
    peak = _finite_max(total_filtered)
    if peak is not None:
        total_axis.text(0.99, 0.96, f"Max: {peak:.3f} lbf",
                        transform=total_axis.transAxes, ha="right", va="top")

    electrical_titles = ("Battery voltage", "Current", "Electrical power")
    electrical_units = ("Voltage (V)", "Current (A)", "Power (W)")
    peak_units = ("V", "A", "W")
    electrical_rows = (0, 1, 2) if row_count == 3 else (0, 2, 4)
    for metric, plot_row in enumerate(electrical_rows):
        axis = figure.add_subplot(grid[plot_row, 1])
        _style(axis, electrical_titles[metric], electrical_units[metric])
        plotted: list[float] = []
        for source in session.sources:
            times = (source.record_electrical_times if recorded
                     else source.electrical_times)
            raw = (source.record_electrical_raw[metric] if recorded
                   else source.electrical_raw[metric])
            filtered = (source.record_electrical_filtered[metric] if recorded
                        else source.electrical_filtered[metric])
            axis.plot(times, raw, color="#9467bd", alpha=0.25,
                      linewidth=0.8)
            axis.plot(times, filtered, color="#9467bd",
                      linewidth=2.0, label="MAUCH")
            plotted.extend(filtered)
        if session.sources and any(
                (source.record_electrical_times if recorded
                 else source.electrical_times) for source in session.sources):
            axis.legend(loc="upper left")
        peak = _finite_max(plotted)
        if peak is not None:
            axis.text(0.99, 0.96, f"Max: {peak:.3f} {peak_units[metric]}",
                      transform=axis.transAxes, ha="right", va="top")
        if metric == 2:
            axis.set_xlabel("Computer time (s)")

    if session.status_messages:
        figure.text(0.01, 0.002, session.status_messages[-1], fontsize=8)
    return figure


def show_live(session: ThrustStandSession, seconds: float = 30.0,
              refresh_seconds: float = 0.5) -> Figure:
    """Refresh an inline VS Code/Jupyter output for a bounded interval."""
    if seconds <= 0 or refresh_seconds < 0.1:
        raise ValueError("seconds must be positive and refresh_seconds at least 0.1")
    from IPython.display import clear_output, display

    deadline = time.monotonic() + seconds
    last_figure: Figure | None = None
    while time.monotonic() < deadline:
        if last_figure is not None:
            plt.close(last_figure)
        last_figure = render_dashboard(session)
        clear_output(wait=True)
        display(last_figure)
        time.sleep(refresh_seconds)
    assert last_figure is not None
    return last_figure


def save_recorded_plot(session: ThrustStandSession,
                       output: Path | str | None = None) -> Path:
    """Stop the interval recording and save its complete plot beside the CSV."""
    session.stop_recording()
    has_data = bool(session.record_total_times) or any(
        source.record_electrical_times for source in session.sources)
    if not has_data:
        raise RuntimeError("No samples captured; call start_recording before the test")
    stamp = (session.record_started_at or datetime.now()).strftime("%Y%m%d_%H%M%S")
    path = Path(output) if output else session.output.parent / f"thrust_plot_{stamp}.png"
    figure = render_dashboard(session, recorded=True)
    figure.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(figure)
    return path
