# ESP32 + WishioT 20 kg HX711 thrust stand

This project reads a 20 kg load cell through an HX711, streams every sample from an ESP32, saves it to CSV on the PC, and plots thrust in pounds-force (`lbf`) in real time. The CSV also retains kilogram-force and newton values for engineering reference. `1 lbf = 4.448221615 N`.

## Confirmed hardware parameters

- Connected ESP32 USB/UART: Silicon Labs CP210x on **COM7**
- HX711: 24-bit ADC, channel A gain 128
- HX711 supply range: 2.6–5.5 V; this setup uses **3.3 V** for ESP32-safe logic
- HX711 output: normally 10 samples/s; 80 samples/s only if the board's RATE jumper/pin is configured for it
- Load-cell capacity: 20 kg, approximately 44.09 lbf maximum static force
- Serial link: 115200 baud

Do not exceed the load cell's rated load. Rocket/motor thrust can produce shock and off-axis loads well above the steady reading, so use a rigid mount, a physical shield, remote operation, and an appropriate safety factor.

## Wiring

### Load cell to HX711

For the common WishioT four-wire cell:

| Load-cell wire | HX711 |
|---|---|
| Red | E+ |
| Black | E- |
| Green | A+ |
| White | A- |

If applied force reads negative, that is not dangerous or a calibration failure: the firmware learns the sign during calibration. Wire colors can vary, so use the markings supplied with your exact cell if they differ.

### HX711 to ESP32

| HX711 | ESP32 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| DT / DOUT | GPIO 4 |
| SCK / CLK | GPIO 5 |

To use different pins, edit `HX_DOUT_PIN` and `HX_SCK_PIN` near the top of the sketch.

## 1. Upload the ESP32 firmware

Open `firmware/thrust_stand_esp32/thrust_stand_esp32.ino` in Arduino IDE.

1. Install Espressif's **esp32** board package in Boards Manager.
2. Select your exact ESP32 board (for a common DevKit, use **ESP32 Dev Module**).
3. Select **COM7**.
4. Upload the sketch.

The firmware contains its own HX711 reader, so no HX711 Arduino library is required.

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

### Record and save a complete plot image

1. Click **Start recording** immediately before a test. This clears the previous image capture and begins recording every plotted sample.
2. Run the test for as long as needed. The live graph can continue scrolling normally.
3. Click **Stop & save plot**. A timestamped `thrust_plot_YYYYmmdd_HHMMSS.png` containing the entire recorded interval is saved in the `logs` folder beside the CSV—not only the last visible plot window.

## 3. Tare and calibrate

Calibration is required before the graph can show pounds-force.

1. Leave the motor and all mounting hardware installed. Turn the motor off and make sure nothing is pushing or pulling on the rig.
2. Click **Zero assembled rig** and keep it still for about 2 seconds at 10 SPS. This removes motor weight and mounting preload without disassembly.
3. Apply a precisely known force in the same direction as thrust. A luggage/fish scale can be used to pull the rig inline.
4. Enter that applied force in pounds-force (for example, a 2 lb pull is `2.000 lbf`) and click **Calibrate force** while holding the force steady.
5. Release the force. The reading should return close to zero. If real thrust is displayed negative, click **Invert direction** once; the choice is saved.

The ESP32 stores zero, calibration, and direction in nonvolatile memory. Before later tests, leave the complete rig installed and click **Zero assembled rig** after warm-up. Recalibration is only needed when mounting geometry, wiring, excitation voltage, or the load cell changes.

## Measurement notes

- The pale trace is each converted reading; the red trace is an exponential moving average for easier viewing. The CSV retains both.
- The default 10 SPS mode is good for static measurements but may miss fast thrust peaks. If the HX711 module exposes a RATE pad/jumper, set RATE high for 80 SPS and restart. Many boards require a solder-jumper modification—verify the board markings first.
- Keep load-cell leads short, route them away from motor/igniter wiring, and use a rigid axial mount. USB power noise, cable strain, vibration, and temperature drift all affect readings.
- Run a test with a known load before any live motor test, and compare the recorded peak and steady value against that known load.
