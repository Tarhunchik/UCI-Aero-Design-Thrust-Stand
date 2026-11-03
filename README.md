# ESP32 thrust stand with four HX711 load cells and MAUCH PL-100

This project reads four 20 kg load cells through four HX711 modules plus voltage and current from a MAUCH PL-100. One ESP32 streams all thrust and electrical measurements over USB or Wi-Fi, saves everything to CSV, and plots it in real time. Electrical power is calculated as `voltage × current` in watts. `1 lbf = 4.448221615 N`.

## Confirmed hardware parameters

- Connected ESP32 USB/UART: Silicon Labs CP210x on **COM7**
- Four HX711 modules: 24-bit ADC, channel A gain 128
- HX711 supply range: 2.6–5.5 V; this setup uses **3.3 V** for ESP32-safe logic
- HX711 output: normally 10 samples/s; 80 samples/s only if the board's RATE jumper/pin is configured for it
- Load-cell capacity: 20 kg (44.09 lbf) per cell; no cell may exceed its individual rating
- Serial link: 115200 baud
- Wireless link: ESP32 access point `ThrustStand`, TCP port `3333`
- MAUCH PL-100: 0–100 A current output mapped to 0–3.3 V
- MAUCH voltage range: solder bridge in = divider 10 / up to 33 V; bridge out = up to 60 V and requires the divider printed on the board's final-test result

Do not exceed the load cell's rated load. Rocket/motor thrust can produce shock and off-axis loads well above the steady reading, so use a rigid mount, a physical shield, remote operation, and an appropriate safety factor.

## Wiring

### Each load cell to its own HX711

Use the same wiring for each common WishioT four-wire cell:

| Load-cell wire | HX711 |
|---|---|
| Red | E+ |
| Black | E- |
| Green | A+ |
| White | A- |

If applied force reads negative, that is not dangerous or a calibration failure: the firmware learns the sign during calibration. Wire colors can vary, so use the markings supplied with your exact cell if they differ.

### All four HX711 modules to the ESP32

| Module | HX711 pin | ESP32 |
|---|---|---|
| HX711 1 | VCC | 3V3 |
| HX711 1 | GND | GND |
| HX711 1 | DT / DOUT | GPIO 4 |
| HX711 1 | SCK / CLK | GPIO 5 |
| HX711 2 | VCC | 3V3 |
| HX711 2 | GND | GND |
| HX711 2 | DT / DOUT | GPIO 18 |
| HX711 2 | SCK / CLK | GPIO 19 |
| HX711 3 | VCC | 3V3 |
| HX711 3 | GND | GND |
| HX711 3 | DT / DOUT | GPIO 21 |
| HX711 3 | SCK / CLK | GPIO 22 |
| HX711 4 | VCC | 3V3 |
| HX711 4 | GND | GND |
| HX711 4 | DT / DOUT | GPIO 25 |
| HX711 4 | SCK / CLK | GPIO 26 |

All four HX711 boards share the ESP32's 3V3 and GND. All grounds must be common. To use different pins, edit the four entries in the `cells` array near the top of the sketch.

### MAUCH PL-100 to the ESP32

The same ESP32 also reads the PL-100.

| MAUCH sensor connection | ESP32 / power connection |
|---|---|
| Current analog output | GPIO 34 |
| Voltage analog output | GPIO 35 |
| Sensor ground | ESP32 GND and 5 V supply ground |
| Sensor supply | Regulated 5.0 V from the MAUCH PL/PC BEC or equivalent |

Use the signal names/markings on the MAUCH cable or board; do not assume a wire color or connector-pin order. The PL-100 uses a 4-pin Molex Clik-Mate 1.25 mm connector, and the sensor cable is not included with the board. Do **not** connect the sensor's 5 V supply pin to ESP32 3V3. The conditioned current and voltage signal outputs themselves are limited to 3.3 V and can connect directly to GPIO34/35.

Install the PL-100 only in the battery's **positive** main lead, in series between the battery and ESC, following the arrow/current direction on the board. The battery negative lead does not pass through the sensor. Work with the battery disconnected, insulate every high-current joint, use connectors and wire rated for the expected current, and put a suitable fuse/current-limiting device in the test setup. Never alter high-current wiring while the battery is connected.

The supplied code assumes the voltage-range solder bridge is **closed** and therefore uses `MAUCH_VOLTAGE_DIVIDER = 10.0`. If the bridge is open for a pack above 33 V, replace that constant with the divider written on your board's final-test result before testing. Fine calibration is performed with the VS Code interactive command cells and saved on the ESP32.

## 1. Upload the ESP32 firmware

Open `firmware/thrust_stand_esp32/thrust_stand_esp32.ino` in Arduino IDE.

1. Install Espressif's **esp32** board package in Boards Manager.
2. Select your exact ESP32 board (for a common DevKit, use **ESP32 Dev Module**).
3. Select the ESP32's port and upload the sketch. The ESP32 creates the Wi-Fi network at `192.168.4.1`.

The firmware contains its own HX711 reader, so no HX711 Arduino library is required.

The ESP32 provides the instrument network:

| Setting | Value |
|---|---|
| Wi-Fi name | `ThrustStand` |
| Password | Set `WIFI_PASSWORD` locally before flashing |
| ESP32 address | `192.168.4.1` |
| TCP port | `3333` |

Before flashing, replace the public `CHANGE_ME_BEFORE_FLASHING` placeholder in
`WIFI_PASSWORD` near the top of the sketch with a private password containing at
least eight characters.

## 2. Install and run the VS Code logger

In PowerShell, from this project folder:

```powershell
.\run_logger.ps1
```

The first run creates `.venv`, installs the logger and Jupyter dependencies,
registers a **Python (UCI Thrust Stand)** kernel, and opens
`logger/vscode_live_plot.py` in VS Code when the `code` launcher is available.
Install the recommended Microsoft Python and Jupyter extensions when VS Code
offers them. To prepare the environment without opening VS Code:

```powershell
.\run_logger.ps1 -InstallOnly
```

Open `logger/vscode_live_plot.py`, select the project kernel, and run cells with
**Shift+Enter**. Set `CONNECTION` to `"serial"`, `"wifi"`, or `"offline"`
and edit `SERIAL_PORT` or `WIFI_HOST` before running the setup cell. Each
figure is an inline Jupyter output; use the plot toolbar or double-click the
output to inspect it in VS Code's built-in Plot Viewer. No standalone plotting
window is created.

Alternatively, prepare the environment manually:

```powershell
python -m pip install -r .\logger\requirements.txt
python -m ipykernel install --user --name uci-thrust-stand --display-name "Python (UCI Thrust Stand)"
```

CSV files are written into `logs` with timestamps as soon as the setup cell
runs. Acquisition and CSV writing occur on background threads at full rate;
plot refresh frequency does not discard saved samples. Always run the final
`session.close()` cell before disconnecting hardware or closing VS Code so the
CSV is flushed cleanly.

### Run wirelessly

1. Power the ESP32 from USB or another stable 5 V USB supply.
2. On the plotting computer, connect to **ThrustStand** using the private password you configured in `WIFI_PASSWORD`. Windows may report “No internet”; remain connected because this is a private instrument network.
3. In the setup cell, set `CONNECTION = "wifi"` and run it. The ESP32 defaults
   to `192.168.4.1:3333`.

### Open the ESP32 web dashboard

After connecting the computer or phone to **ThrustStand**, open
`http://192.168.4.1` in a browser. The ESP32 serves a responsive dashboard
showing all four filtered load-cell readings, combined thrust, voltage,
current, and electrical power. It includes a multi-line thrust plot plus
separate voltage, current, and power plots covering the latest 30 seconds.
The page receives four updates per second through one persistent connection,
then reconnects automatically if the link drops. It marks missing or
uncalibrated cells without blocking sensor acquisition.

The browser page is display-only. Use the VS Code cells for tare, calibration,
full-rate CSV recording, and saved plots. The web dashboard and the TCP logger
can run at the same time.

The interactive workflow uses one ESP32. Four load-cell plots and combined
thrust plots are on the left; battery voltage, current, and electrical power
are on the right. Run the live-plot cell for a bounded refresh interval and
re-run it whenever another interval is wanted. Hardware acquisition continues
between plot cells. The ESP32 accepts only one network logger connection,
and USB serial remains available for diagnostics.

### Record and save a complete plot image

1. Run the `session.start_recording()` cell immediately before a test. This
   clears only the previous PNG interval; CSV recording remains continuous.
2. Run the test for as long as needed. The inline live-plot cell can be run
   during the interval or omitted without affecting acquisition.
3. Run the `save_recorded_plot(session)` cell. A timestamped
   `thrust_plot_YYYYmmdd_HHMMSS.png` containing the entire marked thrust and
   electrical interval is saved beside the CSV, not only the visible window.

## 3. Tare and calibrate

Each connected load cell must be calibrated independently before it can contribute accurate thrust data.

1. Leave the motor and all mounting hardware installed. Turn the motor off and make sure nothing is pushing or pulling on the rig.
2. Run `session.tare()` and keep the complete rig still. This zeros every responding cell and skips a disconnected cell.
3. Apply a precisely known force directly over cell 1, then run `session.calibrate_cell(1, force_lbf)`.
4. Repeat separately for cells 2, 3, and 4 with `session.calibrate_cell(2, force_lbf)`, `session.calibrate_cell(3, force_lbf)`, and `session.calibrate_cell(4, force_lbf)`.
5. Remove the force. The total should return close to zero. If applied thrust is displayed negative, run `session.invert()` once; every calibrated cell is reversed.

The ESP32 stores separate zero and calibration values for all four sensors in nonvolatile memory. Before later tests, leave the complete rig installed and run the tare command after warm-up. Recalibrate a sensor when its mounting, wiring, excitation, or load cell changes.

## 4. Calibrate the MAUCH PL-100

Remove the propeller and mechanically disable the motor before electrical calibration. Use a properly rated DC clamp meter for current; never put an ordinary handheld multimeter current input in series with propulsion wiring.

1. With the MAUCH electronics powered but no propulsion current flowing, run `session.zero_current()`.
2. Disconnect the propulsion battery while leaving the sensor electronics powered, then run `session.zero_voltage()`.
3. Reconnect the battery. Measure its voltage with a DVM and run `session.calibrate_voltage(reference_volts)`.
4. Run a safe, steady load well below the sensor and wiring limits, measure it simultaneously with a DC clamp meter, and run `session.calibrate_current(reference_amps)`.
5. Stop the load and verify that current returns near zero and displayed voltage agrees with the DVM. `session.power_info()` reports saved offsets and slopes; `session.reset_power_calibration()` restores factory approximations.

Current and voltage use independent two-point fits: the zero commands save ADC offsets, while the calibration commands calculate units per ADC count from the entered reference. The values survive power cycles. Calibrate near the middle or upper-middle of the range you normally use, without approaching the 100 A PL-100 limit.

The original ESP32 ADC becomes unreliable at the ends of its range. The session status warns when either electrical channel approaches saturation. If the warning appears, calibration cannot recover the clipped peak; reduce the signal with a suitable divider or use a 3.3 V-compatible external ADC before continuing.

## Measurement notes

- Each of the four load cells has a separate lbf plot. The bottom plot sums every fresh, calibrated load cell. Faint lines are measured force in lbf and solid lines are smoothed force in lbf.
- Every generated plot shows the maximum filtered value in its displayed interval: pounds-force for each calibrated load cell and total thrust, volts, amps, and watts. Saved PNGs calculate the corresponding maxima for the complete marked interval.
- A missing, unpowered, or uncalibrated load cell is reported as `NaN`, bypassed automatically, and excluded from the combined total. The other load cells continue sampling normally.
- If a cell is responding but has no calibration, its lbf trace remains empty and the status reports **calibration required; no calibrated lbf data**. Raw ADC counts remain available in the CSV for diagnostics but are never graphed.
- Upgrading from the original single-cell firmware automatically migrates its saved `zero` and `scale` values to load cell 1.
- The CSV retains all four raw ADC counts, all four calibrated cell forces, and the available-cell total.
- This plot-only behavior does not tare, reset, or modify the ESP32's nonvolatile load-cell calibration or zero offsets.
- The CSV uses `record_type=thrust` or `record_type=electrical`. Electrical rows include raw ADC counts, current, voltage, instantaneous power, and their filtered values.
- GPIO34 and GPIO35 are ADC1 inputs, so they continue working while ESP32 Wi-Fi is active. The firmware averages 16 ADC readings and publishes electrical data at 20 samples/s.
- The default 10 SPS mode is good for static measurements but may miss fast thrust peaks. If the HX711 module exposes a RATE pad/jumper, set RATE high for 80 SPS and restart. Many boards require a solder-jumper modification—verify the board markings first.
- Keep load-cell leads short, route them away from motor/igniter wiring, and use a rigid axial mount. USB power noise, cable strain, vibration, and temperature drift all affect readings.
- Run a test with a known load before any live motor test, and compare the recorded peak and steady value against that known load.
