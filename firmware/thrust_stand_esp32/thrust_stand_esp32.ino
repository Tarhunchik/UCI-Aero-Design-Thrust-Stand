/*
  ESP32 + HX711 thrust stand firmware

  Default wiring:
    ESP32 3V3 -> HX711 VCC
    ESP32 GND -> HX711 GND
    ESP32 GPIO4 <- HX711 DT/DOUT
    ESP32 GPIO5 -> HX711 SCK/CLK

  Serial commands (newline terminated):
    TARE          zero the complete stationary rig (motor may stay installed)
    CAL <kg>      calibrate with a known mass-equivalent load applied
    CALN <N>      calibrate with a known force in the thrust direction
    CALLBF <lbf>  calibrate with a known force in pounds-force
    INVERT        reverse the displayed thrust direction
    INFO          print configuration
    RESETCAL      erase calibration
*/

#include <Arduino.h>
#include <Preferences.h>
#include <math.h>

constexpr uint8_t HX_DOUT_PIN = 4;
constexpr uint8_t HX_SCK_PIN = 5;
constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint32_t HX_TIMEOUT_MS = 1500;
constexpr size_t ZERO_SAMPLES = 20;
constexpr size_t CAL_SAMPLES = 20;
constexpr size_t STARTUP_DISCARD_SAMPLES = 12;
constexpr double MIN_COUNTS_PER_KG = 1000.0;
constexpr float STANDARD_GRAVITY = 9.80665f;
constexpr double NEWTONS_PER_LBF = 4.4482216152605;
constexpr float EMA_ALPHA = 0.30f;  // display smoothing; raw data is also transmitted

Preferences prefs;
int64_t zeroOffset = 0;
double countsPerKg = NAN;
double filteredKgf = NAN;

bool hxReady(uint32_t timeoutMs) {
  const uint32_t started = millis();
  while (digitalRead(HX_DOUT_PIN) == HIGH) {
    if (millis() - started >= timeoutMs) return false;
    delay(1);
  }
  return true;
}

// Read channel A, gain 128. Clocking is protected so SCK never remains HIGH
// long enough to accidentally put the HX711 into power-down mode.
bool hxRead(int32_t &value, uint32_t timeoutMs = HX_TIMEOUT_MS) {
  if (!hxReady(timeoutMs)) return false;

  uint32_t data = 0;
  noInterrupts();
  for (uint8_t i = 0; i < 24; ++i) {
    digitalWrite(HX_SCK_PIN, HIGH);
    delayMicroseconds(1);
    data = (data << 1) | (digitalRead(HX_DOUT_PIN) ? 1U : 0U);
    digitalWrite(HX_SCK_PIN, LOW);
    delayMicroseconds(1);
  }
  // 25th pulse selects channel A, gain 128 for the next conversion.
  digitalWrite(HX_SCK_PIN, HIGH);
  delayMicroseconds(1);
  digitalWrite(HX_SCK_PIN, LOW);
  interrupts();

  if (data & 0x800000UL) data |= 0xFF000000UL;
  value = static_cast<int32_t>(data);
  return true;
}

bool averageRaw(size_t sampleCount, int64_t &average) {
  int64_t sum = 0;
  for (size_t i = 0; i < sampleCount; ++i) {
    int32_t raw;
    if (!hxRead(raw)) return false;
    sum += raw;
  }
  average = sum / static_cast<int64_t>(sampleCount);
  return true;
}

void printInfo() {
  Serial.printf("# INFO,dout=%u,sck=%u,baud=%lu,zero=%lld,counts_per_kg=",
                HX_DOUT_PIN, HX_SCK_PIN, SERIAL_BAUD, zeroOffset);
  if (isfinite(countsPerKg)) Serial.println(countsPerKg, 6);
  else Serial.println("not_calibrated");
}

void tare() {
  Serial.println("# STATUS,Zeroing complete assembled rig: motor off, no applied thrust, keep still");
  int64_t average;
  if (!averageRaw(ZERO_SAMPLES, average)) {
    Serial.println("# ERROR,HX711 timeout during tare; check VCC/GND/DT/SCK");
    return;
  }
  zeroOffset = average;
  filteredKgf = NAN;
  prefs.putLong64("zero", zeroOffset);
  Serial.printf("# STATUS,Tare complete,zero=%lld\n", zeroOffset);
}

void calibrate(double knownKg) {
  if (!isfinite(knownKg) || knownKg <= 0.0 || knownKg > 20.0) {
    Serial.println("# ERROR,CAL mass must be greater than 0 and at most 20 kg");
    return;
  }
  Serial.printf("# STATUS,Calibrating with %.6f kg; keep the load still\n", knownKg);
  int64_t loadedAverage;
  if (!averageRaw(CAL_SAMPLES, loadedAverage)) {
    Serial.println("# ERROR,HX711 timeout during calibration");
    return;
  }
  const double factor = static_cast<double>(loadedAverage - zeroOffset) / knownKg;
  if (!isfinite(factor) || fabs(factor) < MIN_COUNTS_PER_KG) {
    Serial.println("# ERROR,Calibration signal is too small; use a heavier known mass and check mounting/wiring");
    return;
  }
  countsPerKg = factor; // sign follows the actual load-cell orientation
  filteredKgf = NAN;
  prefs.putDouble("scale", countsPerKg);
  Serial.printf("# STATUS,Calibration saved,counts_per_kg=%.6f\n", countsPerKg);
}

void calibrateNewtons(double knownNewtons) {
  if (!isfinite(knownNewtons) || knownNewtons <= 0.0 || knownNewtons > 196.133) {
    Serial.println("# ERROR,CALN force must be greater than 0 and at most 196.133 N");
    return;
  }
  Serial.printf("# STATUS,Calibrating with %.6f N; hold that force steady in the thrust direction\n",
                knownNewtons);
  int64_t loadedAverage;
  if (!averageRaw(CAL_SAMPLES, loadedAverage)) {
    Serial.println("# ERROR,HX711 timeout during force calibration");
    return;
  }
  const double knownKgEquivalent = knownNewtons / STANDARD_GRAVITY;
  const double factor = static_cast<double>(loadedAverage - zeroOffset) / knownKgEquivalent;
  if (!isfinite(factor) || fabs(factor) < MIN_COUNTS_PER_KG) {
    Serial.println("# ERROR,Calibration signal is too small; apply a larger known force and check mounting/wiring");
    return;
  }
  countsPerKg = factor; // makes the calibration direction positive automatically
  filteredKgf = NAN;
  prefs.putDouble("scale", countsPerKg);
  Serial.printf("# STATUS,Force calibration saved,counts_per_kg=%.6f\n", countsPerKg);
}

void calibratePoundsForce(double knownLbf) {
  if (!isfinite(knownLbf) || knownLbf <= 0.0 || knownLbf > 44.0925) {
    Serial.println("# ERROR,CALLBF force must be greater than 0 and at most 44.0925 lbf");
    return;
  }
  calibrateNewtons(knownLbf * NEWTONS_PER_LBF);
}

void invertDirection() {
  if (!isfinite(countsPerKg)) {
    Serial.println("# ERROR,Calibrate before changing direction");
    return;
  }
  countsPerKg = -countsPerKg;
  filteredKgf = NAN;
  prefs.putDouble("scale", countsPerKg);
  Serial.println("# STATUS,Displayed thrust direction inverted and saved");
}

void handleCommand(String command) {
  command.trim();
  if (command.length() == 0) return;
  String upper = command;
  upper.toUpperCase();

  if (upper == "T" || upper == "TARE") {
    tare();
  } else if (upper.startsWith("CALLBF ")) {
    calibratePoundsForce(command.substring(7).toDouble());
  } else if (upper.startsWith("CALN ")) {
    calibrateNewtons(command.substring(5).toDouble());
  } else if (upper.startsWith("CAL ")) {
    calibrate(command.substring(4).toDouble());
  } else if (upper == "INVERT") {
    invertDirection();
  } else if (upper == "I" || upper == "INFO") {
    printInfo();
  } else if (upper == "RESETCAL") {
    countsPerKg = NAN;
    filteredKgf = NAN;
    prefs.remove("scale");
    Serial.println("# STATUS,Calibration erased");
  } else {
    Serial.println("# ERROR,Use TARE, CALLBF <lbf>, CALN <N>, CAL <kg>, INVERT, INFO, or RESETCAL");
  }
}

void setup() {
  pinMode(HX_DOUT_PIN, INPUT_PULLUP);
  pinMode(HX_SCK_PIN, OUTPUT);
  digitalWrite(HX_SCK_PIN, LOW);
  Serial.begin(SERIAL_BAUD);
  delay(700);

  prefs.begin("thrust-stand", false);
  zeroOffset = prefs.getLong64("zero", 0);
  countsPerKg = prefs.getDouble("scale", NAN);

  // Reject a stored factor that is too close to the converter's normal noise.
  if (isfinite(countsPerKg) && fabs(countsPerKg) < MIN_COUNTS_PER_KG) {
    countsPerKg = NAN;
    prefs.remove("scale");
  }

  Serial.println("# THRUST_STAND_V1");
  Serial.println("# COLUMNS,time_ms,raw_counts,kgf,force_N,filtered_force_N");
  printInfo();
  if (!hxReady(HX_TIMEOUT_MS)) {
    Serial.println("# ERROR,HX711 not ready; check wiring and power");
  } else {
    // HX711 output can be far from its settled value immediately after reset.
    // Do not let those samples seed the display filter or reach the plotter.
    int32_t discarded;
    for (size_t i = 0; i < STARTUP_DISCARD_SAMPLES; ++i) {
      if (!hxRead(discarded)) break;
    }
    filteredKgf = NAN;
    if (zeroOffset == 0) {
      Serial.println("# STATUS,No saved zero. Send TARE with the stand unloaded");
    }
    if (!isfinite(countsPerKg)) {
      Serial.println("# STATUS,Calibration required: TARE stationary assembled rig, then CALLBF <known_force_lbf>");
    }
  }
}

void loop() {
  while (Serial.available()) {
    const String command = Serial.readStringUntil('\n');
    handleCommand(command);
  }

  if (digitalRead(HX_DOUT_PIN) == LOW) {
    int32_t raw;
    if (hxRead(raw, 20)) {
      double kgf = NAN;
      double forceN = NAN;
      double filteredN = NAN;
      if (isfinite(countsPerKg)) {
        kgf = static_cast<double>(static_cast<int64_t>(raw) - zeroOffset) / countsPerKg;
        forceN = kgf * STANDARD_GRAVITY;
        if (!isfinite(filteredKgf)) filteredKgf = kgf;
        else filteredKgf += EMA_ALPHA * (kgf - filteredKgf);
        filteredN = filteredKgf * STANDARD_GRAVITY;
      }
      Serial.printf("DATA,%lu,%ld,%.6f,%.6f,%.6f\n",
                    millis(), raw, kgf, forceN, filteredN);
    }
  }
}
