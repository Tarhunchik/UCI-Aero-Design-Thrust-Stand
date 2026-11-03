import csv
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "logger"))

from realtime_logger import SourceState, ThrustStandSession, parse_power, parse_sample
from vscode_plotting import render_dashboard, save_recorded_plot


class FakeTransport:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def close(self):
        pass


class ProtocolTests(unittest.TestCase):
    def test_four_cell_sample_and_nan_bypass(self):
        sample = parse_sample(
            "DATA,100,11,22,33,0,1.0,2.0,3.0,nan,6.0,58.8399,50.0".split(","))
        self.assertIsNotNone(sample)
        self.assertEqual(sample["raw3"], 33)
        self.assertTrue(math.isnan(sample["cell4_lbf"]))
        self.assertAlmostEqual(sample["cell1_lbf"], 2.20462262, places=6)

    def test_legacy_single_cell_sample_is_supported(self):
        sample = parse_sample("DATA,100,123,1.0,9.80665,9.0".split(","))
        self.assertEqual(sample["raw2"], 0)
        self.assertTrue(math.isnan(sample["kgf2"]))

    def test_legacy_dual_cell_sample_is_supported(self):
        sample = parse_sample(
            "DATA,100,123,456,1.0,2.0,3.0,29.41995,25.0".split(","))
        self.assertEqual(sample["raw2"], 456)
        self.assertTrue(math.isnan(sample["kgf3"]))

    def test_power_record(self):
        power = parse_power("POWER,100,10,20,1,12,12,0.9,11.9,10.71".split(","))
        self.assertEqual(power["current_raw"], 10)
        self.assertEqual(power["filtered_power_w"], 10.71)


class FirmwareWebDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.firmware = (
            Path(__file__).resolve().parents[1] /
            "firmware" / "thrust_stand_esp32" / "thrust_stand_esp32.ino"
        ).read_text(encoding="utf-8")

    def test_dashboard_and_json_routes_are_present(self):
        self.assertIn("WiFiServer webServer(80);", self.firmware)
        self.assertIn('webRequest.startsWith("GET /data ")', self.firmware)
        self.assertIn('webRequest.startsWith("GET /events ")', self.firmware)
        self.assertIn('webRequest.startsWith("GET / ")', self.firmware)
        self.assertIn('new EventSource("/events")', self.firmware)
        self.assertNotIn('fetch("/data"', self.firmware)
        self.assertNotIn("setInterval(update,250)", self.firmware)

    def test_dashboard_preserves_four_cell_contract(self):
        self.assertIn("constexpr uint8_t CELL_COUNT = 4;", self.firmware)
        for cell_number in range(1, 5):
            self.assertIn(f'Load cell {cell_number}', self.firmware)

    def test_dashboard_contains_self_hosted_history_plots(self):
        for canvas_id in (
                "thrustChart", "voltageChart", "currentChart", "powerChart"):
            self.assertIn(f'id="{canvas_id}"', self.firmware)
        self.assertIn("const historyLimit=120;", self.firmware)
        self.assertIn(
            'window.addEventListener("resize",drawCharts)', self.firmware)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / "test.csv"
        self.session = ThrustStandSession(self.output)
        self.transport = FakeTransport()
        self.source = SourceState(1, "ESP", "test", self.transport)
        self.session.sources.append(self.source)

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def test_missing_cell_does_not_remove_available_cell_from_total(self):
        sample = parse_sample(
            "DATA,100,123,0,0,0,1.0,nan,nan,nan,1.0,9.80665,9.0".split(","))
        self.session._process_sample(self.source, time.monotonic(), sample, [])
        self.assertTrue(math.isfinite(self.session.total_values[-1]))
        self.assertAlmostEqual(self.source.cells[0][-1], 2.20462262, places=6)
        self.assertFalse(self.source.cells[1])

    def test_uncalibrated_counts_never_enter_force_total(self):
        sample = parse_sample(
            "DATA,100,123,456,789,321,nan,nan,nan,nan,nan,nan,nan".split(","))
        statuses = []
        self.session._process_sample(
            self.source, time.monotonic(), sample, statuses)
        self.assertTrue(all(not values for values in self.source.cells))
        self.assertTrue(all(not values for values in self.source.filtered_cells))
        self.assertEqual(len(statuses), 4)
        self.assertTrue(all(
            "calibration required; no calibrated lbf data" in status
            for status in statuses))
        self.assertTrue(math.isnan(self.session.total_values[-1]))

    def test_commands_preserve_firmware_protocol(self):
        self.session.tare()
        self.session.calibrate_cell(4, 2.20462)
        self.session.calibrate_current(20)
        self.session.calibrate_voltage(24)
        self.assertEqual(self.transport.writes, [
            b"TARE\n", b"CALLBF4 2.204620\n",
            b"CALAMPS 20.000000\n", b"CALVOLTS 24.000000\n"])

    def test_csv_schema_and_record_types(self):
        sample = parse_sample(
            "DATA,100,123,0,0,0,1.0,nan,nan,nan,1.0,9.80665,9.0".split(","))
        power = parse_power(
            "POWER,100,10,20,1,12,12,0.9,11.9,10.71".split(","))
        self.session._write_csv(self.source, sample, None)
        self.session._write_csv(self.source, None, power)
        self.session.close()
        with self.output.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertNotIn(None, rows[0])
        self.assertEqual([row["record_type"] for row in rows], [
            "thrust", "electrical"])
        self.assertEqual(rows[0]["raw4"], "0")
        self.assertEqual(rows[1]["power_W"], "12.0")

    def test_native_figure_and_recorded_png_smoke(self):
        self.session.start_recording()
        sample = parse_sample(
            "DATA,100,123,0,0,0,1.0,nan,nan,nan,1.0,9.80665,9.0".split(","))
        power = parse_power(
            "POWER,100,10,20,1,12,12,0.9,11.9,10.71".split(","))
        now = time.monotonic()
        self.session._process_sample(self.source, now, sample, [])
        self.session._process_power(self.source, now, power, [])
        figure = render_dashboard(self.session)
        self.assertGreaterEqual(len(figure.axes), 8)
        output = save_recorded_plot(
            self.session, Path(self.temp.name) / "recorded.png")
        self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
