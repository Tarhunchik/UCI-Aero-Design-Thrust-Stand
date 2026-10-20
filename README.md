# ESP32 thrust stand with dual HX711 load cells and MAUCH PL-100

This project reads two 20 kg load cells through two HX711 modules plus voltage and current from a MAUCH PL-100. It streams thrust and electrical measurements from an ESP32 over USB or Wi-Fi, saves everything to CSV, and plots it in real time. Electrical power is calculated as `voltage × current` in watts. `1 lbf = 4.448221615 N`.

## Confirmed hardware parameters

- Connected ESP32 USB/UART: Silicon Labs CP210x on **COM7**
- Two HX711 modules: 24-bit ADC, channel A gain 128
- HX711 supply range: 2.6–5.5 V; this setup uses **3.3 V** for ESP32-safe logic
- HX711 output: normally 10 samples/s; 80 samples/s only if the board's RATE jumper/pin is configured for it
- Load-cell capacity: 20 kg (44.09 lbf) per cell; neither cell may exceed its individual rating
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

### Both HX711 modules to the ESP32

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

All grounds must be common. To use different pins, edit the two entries in the `cells` array near the top of the sketch.

### MAUCH PL-100 to ESP 1

The firmware enables the PL-100 on ESP 1 only. ESP 2 does not sample its unconnected analog pins, so it cannot create false electrical readings.

| MAUCH sensor connection | ESP32 / power connection |
|---|---|
| Current analog output | GPIO 34 |
| Voltage analog output | GPIO 35 |
| Sensor ground | ESP32 GND and 5 V supply ground |
| Sensor supply | Regulated 5.0 V from the MAUCH PL/PC BEC or equivalent |

Use the signal names/markings on the MAUCH cable or board; do not assume a wire color or connector-pin order. The PL-100 uses a 4-pin Molex Clik-Mate 1.25 mm connector, and the sensor cable is not included with the board. Do **not** connect the sensor's 5 V supply pin to ESP32 3V3. The conditioned current and voltage signal outputs themselves are limited to 3.3 V and can connect directly to GPIO34/35.

Install the PL-100 only in the battery's **positive** main lead, in series between the battery and ESC, following the arrow/current direction on the board. The battery negative lead does not pass through the sensor. Work with the battery disconnected, insulate every high-current joint, use connectors and wire rated for the expected current, and put a suitable fuse/current-limiting device in the test setup. Never alter high-current wiring while the battery is connected.

The supplied code assumes the voltage-range solder bridge is **closed** and therefore uses `MAUCH_VOLTAGE_DIVIDER = 10.0`. If the bridge is open for a pack above 33 V, replace that constant with the divider written on your board's final-test result before testing. Fine calibration is performed from the separate dashboard setup window and saved on the ESP32.

## 1. Upload the ESP32 firmware

Open `firmware/thrust_stand_esp32/thrust_stand_esp32.ino` in Arduino IDE.

1. Install Espressif's **esp32** board package in Boards Manager.
2. Select your exact ESP32 board (for a common DevKit, use **ESP32 Dev Module**).
3. Leave `DEVICE_NUMBER = 1`, select the first ESP32's port, and upload it. This ESP creates the Wi-Fi network at `192.168.4.1`.
4. For an optional second ESP32, change the constant to `DEVICE_NUMBER = 2`, select that board's port, and upload again. ESP 2 joins ESP 1's network at `192.168.4.2`.

The firmware contains its own HX711 reader, so no HX711 Arduino library is required.

ESP 1 provides the shared network:

| Setting | Value |
|---|---|
| Wi-Fi name | `ThrustStand` |
| Password | Set `WIFI_PASSWORD` locally before flashing |
| ESP 1 address | `192.168.4.1` |
| ESP 2 address | `192.168.4.2` |
| TCP port | `3333` |

Before flashing, replace the public `CHANGE_ME_BEFORE_FLASHING` placeholder in
`WIFI_PASSWORD` near the top of the sketch with a private password containing at
least eight characters. Use the same SSID and password on both ESP32 boards.

## 2. Install and run the PC logger

In PowerShell, from this project folder:

```powershell
.\run_logger.ps1
```

The first run creates a Python virtual environment and installs `pyserial` and `matplotlib`. If your ESP32 later appears on another port:

```powershell
.\run_logger.ps1 -Port COM8
```

Alternatively, install the requirements and launch the Python file directly:

```powershell
python -m pip install -r .\logger\requirements.txt
python .\logger\realtime_logger.py --port COM7
```

CSV files are written into `logs` with timestamps. Closing the graph flushes and closes the file cleanly.

### Run wirelessly

1. Power ESP 1 first, then ESP 2 if used. Each can use a USB power bank or another stable 5 V USB supply.
2. On the plotting computer, connect to **ThrustStand** using the private password you configured in `WIFI_PASSWORD`. Windows may report “No internet”; remain connected because this is a private instrument network.
3. Start the wireless logger:

```powershell
.\run_logger.ps1 -Wifi
```

Or launch it directly:

```powershell
python .\logger\realtime_logger.py --wifi
```

The logger initially connects to ESP 1. The dashboard is split vertically: load-cell and total-thrust plots are on the left; battery voltage, current, and electrical power are on the right. The main window contains only **Calibration & setup**, **Clear plot**, and recording controls so the graphs have more room.

The dashboard renders at 5 frames per second by default and limits each visible line to 450 points. Sensor acquisition and CSV recording remain at full rate. This keeps the six-plot dashboard responsive on laptops and high-resolution displays. If needed, reduce rendering load further without losing saved data:

```powershell
.\run_logger.ps1 -Wifi -RefreshMs 300 -MaxPlotPoints 300
```

To open the dashboard without a connected COM device, use:

```powershell
.\run_logger.ps1 -Offline
```

The dashboard also falls back to offline mode instead of closing when its initial USB or Wi-Fi connection fails. Open **Calibration & setup**, enter an ESP Wi-Fi address, and click **Connect ESP** to attach a board later.

Click **Calibration & setup** to open a separate window. That window contains ESP targeting, **+ Add ESP**, all load-cell calibration controls, and all MAUCH electrical calibration controls. To add ESP 2, leave **Add host** set to `192.168.4.2` and click **+ Add ESP**. Use **Target ESP** to choose which board receives commands. Recording and PNG export preserve the split graph layout.

The plotter supports one or two ESPs. With only ESP 1 connected, all six visible plots use an equal three-row layout. Adding ESP 2 automatically changes to an equal-height five-row layout for its four load cells plus total thrust; the three equal-size electrical plots are distributed down the right side. Each ESP still accepts only one network logger connection, and USB serial remains available for diagnostics.

### Record and save a complete plot image

1. Click **Start recording** immediately before a test. This clears the previous image capture and begins recording every plotted sample.
2. Run the test for as long as needed. The live graph can continue scrolling normally.
3. Click **Stop & save plot**. A timestamped `thrust_plot_YYYYmmdd_HHMMSS.png` containing the entire recorded thrust and electrical interval is saved in the `logs` folder beside the CSV—not only the last visible plot window.

## 3. Tare and calibrate

Each connected load cell must be calibrated independently before it can contribute accurate thrust data.

1. Leave the motor and all mounting hardware installed. Turn the motor off and make sure nothing is pushing or pulling on the rig.
2. Select the board in **Target ESP**, click **Zero target ESP**, and keep its complete rig still. This zeros every responding cell on that ESP and skips a disconnected cell.
3. Apply a precisely known force directly over cell 1 so the calibration load is carried by that sensor. Enter the force in pounds-force and click **Calibrate cell 1**.
4. Move the same known force directly over cell 2 and click **Calibrate cell 2**.
5. Remove the force. The total should return close to zero. If applied thrust is displayed negative, click **Invert target** once; every calibrated cell on that ESP is reversed.
6. If a second ESP is connected, select target `2` and repeat the calibration for its two sensors.

The ESP32 stores separate zero and calibration values for both sensors in nonvolatile memory. Before later tests, leave the complete rig installed and click **Zero assembled rig** after warm-up. Recalibrate a sensor when its mounting, wiring, excitation, or load cell changes.

## 4. Calibrate the MAUCH PL-100

Electrical calibration is enabled on ESP 1. Remove the propeller and mechanically disable the motor before calibration. Use a properly rated DC clamp meter for current; never put an ordinary handheld multimeter current input in series with propulsion wiring.

1. Open **Calibration & setup** and set **Target ESP** to `1`.
2. With the MAUCH electronics powered but no propulsion current flowing, click **Zero current**.
3. Disconnect the propulsion battery while leaving the sensor electronics powered, then click **Zero voltage**.
4. Reconnect the battery. Measure its voltage with a DVM, enter that value under **Reference volts**, and click **Calibrate voltage**.
5. Run a safe, steady load well below the sensor and wiring limits. Enter the simultaneous DC clamp-meter reading under **Reference amps**, then click **Calibrate current**.
6. Stop the load and verify that current returns near zero and displayed voltage agrees with the DVM. **Read calibration info** prints the saved offsets and slopes. **Reset electrical calibration** restores factory approximations.

Current and voltage use independent two-point fits: the zero buttons save ADC offsets, while the calibration buttons calculate units per ADC count from the entered reference. The values survive power cycles. Calibrate near the middle or upper-middle of the range you normally use, without approaching the 100 A PL-100 limit.

The original ESP32 ADC becomes unreliable at the ends of its range. The dashboard warns when either electrical channel approaches saturation. If the warning appears, calibration cannot recover the clipped peak; reduce the signal with a suitable divider or use a 3.3 V-compatible external ADC before continuing.

## Measurement notes

- Each ESP has a separate plot for load cell 1 and load cell 2. The bottom plot sums every fresh, calibrated load cell across the connected ESPs. Faint lines are raw data and solid lines are filtered data.
- Every live plot shows its maximum filtered value since **Clear plot**: pounds-force for each calibrated load cell and total thrust, volts, amps, and watts. Saved PNGs calculate and display the corresponding maxima for the complete recorded interval.
- A missing, unpowered, or uncalibrated load cell is reported as `NaN`, bypassed automatically, and excluded from the combined total. The other load cell continues sampling normally.
- If a cell is responding but has no calibration, its individual panel switches to **ADC counts (uncalibrated)** so wiring and sensor activity remain visible. ADC counts never contribute to the pounds-force total.
- Upgrading from the original single-cell firmware automatically migrates its saved `zero` and `scale` values to load cell 1.
- The CSV retains the ESP identity, both raw ADC counts, both calibrated cell forces, and each ESP's available-cell total.
- The CSV uses `record_type=thrust` or `record_type=electrical`. Electrical rows include raw ADC counts, current, voltage, instantaneous power, and their filtered values.
- GPIO34 and GPIO35 are ADC1 inputs, so they continue working while ESP32 Wi-Fi is active. The firmware averages 16 ADC readings and publishes electrical data at 20 samples/s.
- The default 10 SPS mode is good for static measurements but may miss fast thrust peaks. If the HX711 module exposes a RATE pad/jumper, set RATE high for 80 SPS and restart. Many boards require a solder-jumper modification—verify the board markings first.
- Keep load-cell leads short, route them away from motor/igniter wiring, and use a rigid axial mount. USB power noise, cable strain, vibration, and temperature drift all affect readings.
- Run a test with a known load before any live motor test, and compare the recorded peak and steady value against that known load.
