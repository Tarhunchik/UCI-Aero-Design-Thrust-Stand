/*
  ESP32 dual-load-cell thrust stand

  HX711/load cell 1: DT GPIO4,  SCK GPIO5
  HX711/load cell 2: DT GPIO18, SCK GPIO19
  MAUCH PL-100: current signal GPIO34, voltage signal GPIO35

  Wireless access point:
    SSID: ThrustStand
    Password: set WIFI_PASSWORD locally before flashing
    TCP: 192.168.4.1:3333

  Set DEVICE_NUMBER to 1 before uploading ESP 1. Set it to 2 before
  uploading ESP 2. ESP 1 creates the network; ESP 2 joins it at 192.168.4.2.

  Commands (USB serial or Wi-Fi TCP, newline terminated):
    TARE           zero both cells with the complete stationary rig installed
    CALLBF1 <lbf>  calibrate cell 1 with a known force applied to cell 1
    CALLBF2 <lbf>  calibrate cell 2 with a known force applied to cell 2
    INVERT         reverse both displayed force directions
    INFO           print configuration
    RESETCAL       erase both calibration factors
    POWERINFO      print MAUCH electrical-sensor configuration
    ZEROAMPS       capture zero-current ADC offset with motor off
    ZEROVOLTS      capture zero-voltage ADC offset with battery disconnected
    CALAMPS <A>    calibrate current against a DC clamp meter
    CALVOLTS <V>   calibrate voltage against a DVM
    RESETPOWER     erase electrical calibration
*/

#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <math.h>
#include <stdarg.h>

constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint8_t DEVICE_NUMBER = 1; // valid values: 1 (primary) or 2 (secondary)
constexpr uint16_t TCP_PORT = 3333;
constexpr char WIFI_SSID[] = "ThrustStand";
// Public-repository placeholder. Replace this locally with a private password
// of at least eight characters before flashing either ESP32.
constexpr char WIFI_PASSWORD[] = "CHANGE_ME_BEFORE_FLASHING";
constexpr uint32_t HX_TIMEOUT_MS = 1500;
constexpr size_t ZERO_SAMPLES = 20;
constexpr size_t CAL_SAMPLES = 20;
constexpr size_t STARTUP_DISCARD_SAMPLES = 12;
constexpr double MIN_COUNTS_PER_KG = 1000.0;
constexpr double STANDARD_GRAVITY = 9.80665;
constexpr double NEWTONS_PER_LBF = 4.4482216152605;
constexpr double EMA_ALPHA = 0.30;

// MAUCH PL-100 integration. The sensor is connected to ESP 1 by default.
// Its conditioned current output is 0.0-3.3 V for 0-100 A. The voltage
// output is battery voltage divided by the board's configured divider.
constexpr bool ENABLE_MAUCH_SENSOR = DEVICE_NUMBER == 1;
constexpr uint8_t MAUCH_CURRENT_PIN = 34; // ADC1; safe while Wi-Fi is active
constexpr uint8_t MAUCH_VOLTAGE_PIN = 35; // ADC1; safe while Wi-Fi is active
constexpr double MAUCH_VOLTAGE_DIVIDER = 10.0; // solder bridge IN, max 33 V
constexpr double DEFAULT_CURRENT_A_PER_COUNT = 100.0 / 4095.0;
constexpr double DEFAULT_VOLTAGE_V_PER_COUNT =
    (3.3 * MAUCH_VOLTAGE_DIVIDER) / 4095.0;
constexpr double POWER_EMA_ALPHA = 0.20;
constexpr uint32_t POWER_INTERVAL_MS = 50;      // 20 electrical samples/s
constexpr uint8_t ADC_AVERAGE_SAMPLES = 16;

struct LoadCell {
  uint8_t doutPin;
  uint8_t sckPin;
  const char *zeroKey;
  const char *scaleKey;
  int64_t zeroOffset;
  double countsPerKg;
  double filteredKgf;
};

LoadCell cells[2] = {
  {4, 5, "zero1", "scale1", 0, NAN, NAN},
  {18, 19, "zero2", "scale2", 0, NAN, NAN}
};

Preferences prefs;
WiFiServer tcpServer(TCP_PORT);
WiFiClient tcpClient;
String serialCommand;
String wifiCommand;
uint32_t lastPowerSampleMs = 0;
double filteredCurrentA = NAN;
double filteredVoltageV = NAN;
uint32_t currentZeroRaw = 0;
uint32_t voltageZeroRaw = 0;
double currentAperCount = DEFAULT_CURRENT_A_PER_COUNT;
double voltageVperCount = DEFAULT_VOLTAGE_V_PER_COUNT;

IPAddress networkIp() {
  return DEVICE_NUMBER == 1 ? WiFi.softAPIP() : WiFi.localIP();
}

void emitLine(const String &line) {
  Serial.println(line);
  if (tcpClient && tcpClient.connected()) tcpClient.println(line);
}

void emitf(const char *format, ...) {
  char buffer[320];
  va_list args;
  va_start(args, format);
  vsnprintf(buffer, sizeof(buffer), format, args);
  va_end(args);
  emitLine(buffer);
}

bool hxReady(const LoadCell &cell, uint32_t timeoutMs) {
  const uint32_t started = millis();
  while (digitalRead(cell.doutPin) == HIGH) {
    if (millis() - started >= timeoutMs) return false;
    delay(1);
  }
  return true;
}

bool hxRead(LoadCell &cell, int32_t &value, uint32_t timeoutMs = HX_TIMEOUT_MS) {
  if (!hxReady(cell, timeoutMs)) return false;

  uint32_t data = 0;
  noInterrupts();
  for (uint8_t i = 0; i < 24; ++i) {
    digitalWrite(cell.sckPin, HIGH);
    delayMicroseconds(1);
    data = (data << 1) | (digitalRead(cell.doutPin) ? 1U : 0U);
    digitalWrite(cell.sckPin, LOW);
    delayMicroseconds(1);
  }
  digitalWrite(cell.sckPin, HIGH);
  delayMicroseconds(1);
  digitalWrite(cell.sckPin, LOW);
  interrupts();

  if (data & 0x800000UL) data |= 0xFF000000UL;
  value = static_cast<int32_t>(data);
  return true;
}

bool averageRaw(LoadCell &cell, size_t sampleCount, int64_t &average) {
  int64_t sum = 0;
  for (size_t i = 0; i < sampleCount; ++i) {
    int32_t raw;
    if (!hxRead(cell, raw)) return false;
    sum += raw;
  }
  average = sum / static_cast<int64_t>(sampleCount);
  return true;
}

void printInfo() {
  const String scale1 = isfinite(cells[0].countsPerKg) ? String(cells[0].countsPerKg, 6) : "not_calibrated";
  const String scale2 = isfinite(cells[1].countsPerKg) ? String(cells[1].countsPerKg, 6) : "not_calibrated";
  emitf("# INFO,cell1_dout=%u,cell1_sck=%u,cell1_zero=%lld,cell1_counts_per_kg=%s",
        cells[0].doutPin, cells[0].sckPin, cells[0].zeroOffset, scale1.c_str());
  emitf("# INFO,cell2_dout=%u,cell2_sck=%u,cell2_zero=%lld,cell2_counts_per_kg=%s",
        cells[1].doutPin, cells[1].sckPin, cells[1].zeroOffset, scale2.c_str());
  emitf("# INFO,device=%u,wifi_ssid=%s,wifi_ip=%s,tcp_port=%u",
        DEVICE_NUMBER, WIFI_SSID, networkIp().toString().c_str(), TCP_PORT);
  emitf("# POWERINFO,enabled=%u,current_pin=%u,voltage_pin=%u,voltage_divider=%.6f,current_zero_raw=%lu,voltage_zero_raw=%lu,current_A_per_count=%.9f,voltage_V_per_count=%.9f",
        ENABLE_MAUCH_SENSOR ? 1 : 0, MAUCH_CURRENT_PIN, MAUCH_VOLTAGE_PIN,
        MAUCH_VOLTAGE_DIVIDER, currentZeroRaw, voltageZeroRaw,
        currentAperCount, voltageVperCount);
}

uint32_t averageAdcRaw(uint8_t pin, uint16_t samples = ADC_AVERAGE_SAMPLES) {
  uint32_t total = 0;
  for (uint16_t i = 0; i < samples; ++i) total += analogRead(pin);
  return total / samples;
}

void resetPowerFilters() {
  filteredCurrentA = NAN;
  filteredVoltageV = NAN;
}

void zeroPowerChannel(bool currentChannel) {
  if (!ENABLE_MAUCH_SENSOR) {
    emitLine("# ERROR,Electrical sensor is disabled on this ESP");
    return;
  }
  const uint8_t pin = currentChannel ? MAUCH_CURRENT_PIN : MAUCH_VOLTAGE_PIN;
  const uint32_t raw = averageAdcRaw(pin, 128);
  if (currentChannel) {
    currentZeroRaw = raw;
    prefs.putUInt("izero", currentZeroRaw);
    emitf("# STATUS,Current zero saved,current_zero_raw=%lu", currentZeroRaw);
  } else {
    voltageZeroRaw = raw;
    prefs.putUInt("vzero", voltageZeroRaw);
    emitf("# STATUS,Voltage zero saved,voltage_zero_raw=%lu", voltageZeroRaw);
  }
  resetPowerFilters();
}

void calibratePowerChannel(bool currentChannel, double referenceValue) {
  const double maximum = currentChannel ? 100.0 : 60.0;
  if (!ENABLE_MAUCH_SENSOR) {
    emitLine("# ERROR,Electrical sensor is disabled on this ESP");
    return;
  }
  if (!isfinite(referenceValue) || referenceValue <= 0.0 || referenceValue > maximum) {
    emitf("# ERROR,%s reference must be greater than 0 and at most %.1f",
          currentChannel ? "Current" : "Voltage", maximum);
    return;
  }
  const uint8_t pin = currentChannel ? MAUCH_CURRENT_PIN : MAUCH_VOLTAGE_PIN;
  const uint32_t zero = currentChannel ? currentZeroRaw : voltageZeroRaw;
  const uint32_t raw = averageAdcRaw(pin, 128);
  if (raw <= zero + 8) {
    emitf("# ERROR,%s calibration signal is too close to zero; check wiring and reference load",
          currentChannel ? "Current" : "Voltage");
    return;
  }
  const double scale = referenceValue / static_cast<double>(raw - zero);
  if (currentChannel) {
    currentAperCount = scale;
    prefs.putDouble("iscale", currentAperCount);
    emitf("# STATUS,Current calibration saved,reference_A=%.5f,raw=%lu,A_per_count=%.9f",
          referenceValue, raw, currentAperCount);
  } else {
    voltageVperCount = scale;
    prefs.putDouble("vscale", voltageVperCount);
    emitf("# STATUS,Voltage calibration saved,reference_V=%.5f,raw=%lu,V_per_count=%.9f",
          referenceValue, raw, voltageVperCount);
  }
  resetPowerFilters();
}

void sampleElectricalPower() {
  if (!ENABLE_MAUCH_SENSOR) return;
  const uint32_t now = millis();
  if (now - lastPowerSampleMs < POWER_INTERVAL_MS) return;
  lastPowerSampleMs = now;

  const uint32_t currentRaw = averageAdcRaw(MAUCH_CURRENT_PIN);
  const uint32_t voltageRaw = averageAdcRaw(MAUCH_VOLTAGE_PIN);
  const uint32_t correctedCurrentRaw = currentRaw > currentZeroRaw ? currentRaw - currentZeroRaw : 0;
  const uint32_t correctedVoltageRaw = voltageRaw > voltageZeroRaw ? voltageRaw - voltageZeroRaw : 0;
  double currentA = correctedCurrentRaw * currentAperCount;
  double voltageV = correctedVoltageRaw * voltageVperCount;
  if (currentA < 0.05) currentA = 0.0;
  if (voltageV < 0.02) voltageV = 0.0;

  if (!isfinite(filteredCurrentA)) filteredCurrentA = currentA;
  else filteredCurrentA += POWER_EMA_ALPHA * (currentA - filteredCurrentA);
  if (!isfinite(filteredVoltageV)) filteredVoltageV = voltageV;
  else filteredVoltageV += POWER_EMA_ALPHA * (voltageV - filteredVoltageV);

  const double powerW = voltageV * currentA;
  const double filteredPowerW = filteredVoltageV * filteredCurrentA;
  emitf("POWER,%lu,%lu,%lu,%.5f,%.5f,%.5f,%.5f,%.5f,%.5f",
        now, currentRaw, voltageRaw, currentA, voltageV, powerW,
        filteredCurrentA, filteredVoltageV, filteredPowerW);
}

void tareAll() {
  emitLine("# STATUS,Zeroing available cells: motor off, no applied thrust, keep the rig still");
  uint8_t zeroed = 0;
  for (uint8_t i = 0; i < 2; ++i) {
    int64_t average;
    if (!averageRaw(cells[i], ZERO_SAMPLES, average)) {
      emitf("# STATUS,HX711 cell %u unavailable; skipped during tare", i + 1);
      continue;
    }
    cells[i].zeroOffset = average;
    cells[i].filteredKgf = NAN;
    prefs.putLong64(cells[i].zeroKey, cells[i].zeroOffset);
    ++zeroed;
  }
  if (zeroed) {
    emitf("# STATUS,Tare complete,zeroed_cells=%u,cell1_zero=%lld,cell2_zero=%lld",
          zeroed, cells[0].zeroOffset, cells[1].zeroOffset);
  } else {
    emitLine("# ERROR,No HX711 cells responded during tare");
  }
}

void calibrateCell(uint8_t index, double knownLbf) {
  if (index > 1) return;
  if (!isfinite(knownLbf) || knownLbf <= 0.0 || knownLbf > 44.0925) {
    emitLine("# ERROR,Calibration force must be greater than 0 and at most 44.0925 lbf per cell");
    return;
  }

  emitf("# STATUS,Calibrating cell %u with %.6f lbf; hold the load steady over that cell",
        index + 1, knownLbf);
  int64_t loadedAverage;
  if (!averageRaw(cells[index], CAL_SAMPLES, loadedAverage)) {
    emitf("# ERROR,HX711 cell %u timeout during calibration", index + 1);
    return;
  }

  const double knownKgEquivalent = knownLbf * NEWTONS_PER_LBF / STANDARD_GRAVITY;
  const double factor = static_cast<double>(loadedAverage - cells[index].zeroOffset) /
                        knownKgEquivalent;
  if (!isfinite(factor) || fabs(factor) < MIN_COUNTS_PER_KG) {
    emitLine("# ERROR,Calibration signal is too small; use a larger known force and check mounting/wiring");
    return;
  }

  cells[index].countsPerKg = factor;
  cells[index].filteredKgf = NAN;
  prefs.putDouble(cells[index].scaleKey, cells[index].countsPerKg);
  emitf("# STATUS,Cell %u calibration saved,counts_per_kg=%.6f",
        index + 1, cells[index].countsPerKg);
}

void invertDirection() {
  uint8_t inverted = 0;
  for (uint8_t i = 0; i < 2; ++i) {
    if (!isfinite(cells[i].countsPerKg)) continue;
    cells[i].countsPerKg = -cells[i].countsPerKg;
    cells[i].filteredKgf = NAN;
    prefs.putDouble(cells[i].scaleKey, cells[i].countsPerKg);
    ++inverted;
  }
  if (inverted) emitf("# STATUS,Direction inverted for %u calibrated cell(s)", inverted);
  else emitLine("# ERROR,Calibrate at least one available cell before changing direction");
}

void handleCommand(String command) {
  command.trim();
  if (!command.length()) return;
  String upper = command;
  upper.toUpperCase();

  if (upper == "T" || upper == "TARE") {
    tareAll();
  } else if (upper.startsWith("CALLBF1 ")) {
    calibrateCell(0, command.substring(8).toDouble());
  } else if (upper.startsWith("CALLBF2 ")) {
    calibrateCell(1, command.substring(8).toDouble());
  } else if (upper.startsWith("CALLBF ")) {
    emitLine("# ERROR,Two-cell mode requires CALLBF1 <lbf> and CALLBF2 <lbf>");
  } else if (upper == "INVERT") {
    invertDirection();
  } else if (upper == "I" || upper == "INFO") {
    printInfo();
  } else if (upper == "POWERINFO") {
    printInfo();
  } else if (upper == "ZEROAMPS") {
    zeroPowerChannel(true);
  } else if (upper == "ZEROVOLTS") {
    zeroPowerChannel(false);
  } else if (upper.startsWith("CALAMPS ")) {
    calibratePowerChannel(true, command.substring(8).toDouble());
  } else if (upper.startsWith("CALVOLTS ")) {
    calibratePowerChannel(false, command.substring(9).toDouble());
  } else if (upper == "RESETPOWER") {
    currentZeroRaw = 0;
    voltageZeroRaw = 0;
    currentAperCount = DEFAULT_CURRENT_A_PER_COUNT;
    voltageVperCount = DEFAULT_VOLTAGE_V_PER_COUNT;
    prefs.remove("izero");
    prefs.remove("vzero");
    prefs.remove("iscale");
    prefs.remove("vscale");
    resetPowerFilters();
    emitLine("# STATUS,Electrical calibration reset to defaults");
  } else if (upper == "RESETCAL") {
    for (uint8_t i = 0; i < 2; ++i) {
      cells[i].countsPerKg = NAN;
      cells[i].filteredKgf = NAN;
      prefs.remove(cells[i].scaleKey);
    }
    emitLine("# STATUS,Both calibration factors erased");
  } else {
    emitLine("# ERROR,Use TARE, CALLBF1/2 <lbf>, INVERT, INFO, ZEROAMPS, ZEROVOLTS, CALAMPS <A>, CALVOLTS <V>, RESETPOWER, or RESETCAL");
  }
}

void readCommands(Stream &stream, String &buffer) {
  while (stream.available()) {
    const char c = static_cast<char>(stream.read());
    if (c == '\n' || c == '\r') {
      if (buffer.length()) {
        handleCommand(buffer);
        buffer = "";
      }
    } else if (buffer.length() < 80) {
      buffer += c;
    } else {
      buffer = "";
      emitLine("# ERROR,Command too long");
    }
  }
}

void serviceWifiClient() {
  if (tcpClient && !tcpClient.connected()) {
    tcpClient.stop();
    wifiCommand = "";
  }
  WiFiClient candidate = tcpServer.available();
  if (candidate) {
    if (tcpClient && tcpClient.connected()) {
      candidate.println("# ERROR,Another logger is already connected");
      candidate.stop();
    } else {
      tcpClient = candidate;
      tcpClient.setNoDelay(true);
      wifiCommand = "";
      emitLine("# STATUS,Wireless logger connected");
      printInfo();
    }
  }
  if (tcpClient && tcpClient.connected()) readCommands(tcpClient, wifiCommand);
}

void setup() {
  for (uint8_t i = 0; i < 2; ++i) {
    pinMode(cells[i].doutPin, INPUT_PULLUP);
    pinMode(cells[i].sckPin, OUTPUT);
    digitalWrite(cells[i].sckPin, LOW);
  }
  Serial.begin(SERIAL_BAUD);
  delay(700);

  if (ENABLE_MAUCH_SENSOR) {
    pinMode(MAUCH_CURRENT_PIN, INPUT);
    pinMode(MAUCH_VOLTAGE_PIN, INPUT);
    analogReadResolution(12);
    analogSetPinAttenuation(MAUCH_CURRENT_PIN, ADC_11db);
    analogSetPinAttenuation(MAUCH_VOLTAGE_PIN, ADC_11db);
  }

  bool wifiReady = false;
  if (DEVICE_NUMBER == 1) {
    WiFi.mode(WIFI_AP);
    wifiReady = WiFi.softAP(WIFI_SSID, WIFI_PASSWORD);
  } else {
    WiFi.mode(WIFI_STA);
    const IPAddress localIp(192, 168, 4, DEVICE_NUMBER);
    const IPAddress gateway(192, 168, 4, 1);
    const IPAddress subnet(255, 255, 255, 0);
    WiFi.config(localIp, gateway, subnet);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    const uint32_t connectStarted = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - connectStarted < 15000) delay(100);
    wifiReady = WiFi.status() == WL_CONNECTED;
  }

  if (wifiReady) {
    tcpServer.begin();
    tcpServer.setNoDelay(true);
    Serial.printf("# STATUS,ESP %u WiFi ready: %s:%u\n",
                  DEVICE_NUMBER, networkIp().toString().c_str(), TCP_PORT);
  } else {
    Serial.println("# ERROR,Could not establish WiFi; USB serial still works");
  }

  prefs.begin("thrust-stand", false);
  currentZeroRaw = prefs.getUInt("izero", 0);
  voltageZeroRaw = prefs.getUInt("vzero", 0);
  currentAperCount = prefs.getDouble("iscale", DEFAULT_CURRENT_A_PER_COUNT);
  voltageVperCount = prefs.getDouble("vscale", DEFAULT_VOLTAGE_V_PER_COUNT);
  if (!isfinite(currentAperCount) || currentAperCount <= 0.0) {
    currentAperCount = DEFAULT_CURRENT_A_PER_COUNT;
  }
  if (!isfinite(voltageVperCount) || voltageVperCount <= 0.0) {
    voltageVperCount = DEFAULT_VOLTAGE_V_PER_COUNT;
  }
  // Migrate calibration made by the original single-cell firmware to cell 1.
  if (!prefs.isKey(cells[0].zeroKey) && prefs.isKey("zero")) {
    prefs.putLong64(cells[0].zeroKey, prefs.getLong64("zero", 0));
    emitLine("# STATUS,Migrated legacy zero offset to cell 1");
  }
  if (!prefs.isKey(cells[0].scaleKey) && prefs.isKey("scale")) {
    const double legacyScale = prefs.getDouble("scale", NAN);
    if (isfinite(legacyScale) && fabs(legacyScale) >= MIN_COUNTS_PER_KG) {
      prefs.putDouble(cells[0].scaleKey, legacyScale);
      emitLine("# STATUS,Migrated legacy calibration factor to cell 1");
    }
  }
  for (uint8_t i = 0; i < 2; ++i) {
    cells[i].zeroOffset = prefs.getLong64(cells[i].zeroKey, 0);
    cells[i].countsPerKg = prefs.getDouble(cells[i].scaleKey, NAN);
    if (isfinite(cells[i].countsPerKg) && fabs(cells[i].countsPerKg) < MIN_COUNTS_PER_KG) {
      cells[i].countsPerKg = NAN;
      prefs.remove(cells[i].scaleKey);
    }
  }

  emitf("# THRUST_STAND_V7_POWER_CAL,device=%u", DEVICE_NUMBER);
  emitLine("# COLUMNS,time_ms,raw1,raw2,kgf1,kgf2,total_kgf,total_force_N,filtered_total_force_N");
  if (ENABLE_MAUCH_SENSOR) {
    emitLine("# POWER_COLUMNS,time_ms,current_adc_raw,voltage_adc_raw,current_A,voltage_V,power_W,filtered_current_A,filtered_voltage_V,filtered_power_W");
  }
  printInfo();

  for (uint8_t i = 0; i < 2; ++i) {
    if (!hxReady(cells[i], HX_TIMEOUT_MS)) {
      emitf("# ERROR,HX711 cell %u not ready; check its wiring and power", i + 1);
    }
  }

  for (size_t sample = 0; sample < STARTUP_DISCARD_SAMPLES; ++sample) {
    for (uint8_t i = 0; i < 2; ++i) {
      int32_t discarded;
      if (digitalRead(cells[i].doutPin) == LOW) hxRead(cells[i], discarded, 20);
    }
    delay(10);
  }

  if (!cells[0].zeroOffset || !cells[1].zeroOffset) {
    emitLine("# STATUS,Send TARE with the complete stand unloaded");
  }
  if (!isfinite(cells[0].countsPerKg) || !isfinite(cells[1].countsPerKg)) {
    emitLine("# STATUS,Calibrate each sensor separately with CALLBF1 and CALLBF2");
  }
}

void loop() {
  readCommands(Serial, serialCommand);
  serviceWifiClient();
  sampleElectricalPower();

  int32_t raw[2] = {0, 0};
  bool received[2] = {false, false};
  for (uint8_t i = 0; i < 2; ++i) {
    if (digitalRead(cells[i].doutPin) == LOW) received[i] = hxRead(cells[i], raw[i], 20);
  }
  if (!received[0] && !received[1]) return;

  double kgf[2] = {NAN, NAN};
  double filteredKgf[2] = {NAN, NAN};
  double totalKgf = 0.0;
  double filteredTotalKgf = 0.0;
  uint8_t validCells = 0;
  for (uint8_t i = 0; i < 2; ++i) {
    if (!received[i] || !isfinite(cells[i].countsPerKg)) continue;
    kgf[i] = static_cast<double>(static_cast<int64_t>(raw[i]) - cells[i].zeroOffset) /
             cells[i].countsPerKg;
    if (!isfinite(cells[i].filteredKgf)) cells[i].filteredKgf = kgf[i];
    else cells[i].filteredKgf += EMA_ALPHA * (kgf[i] - cells[i].filteredKgf);
    filteredKgf[i] = cells[i].filteredKgf;
    totalKgf += kgf[i];
    filteredTotalKgf += filteredKgf[i];
    ++validCells;
  }

  const double reportedTotalKgf = validCells ? totalKgf : NAN;
  const double totalForceN = validCells ? totalKgf * STANDARD_GRAVITY : NAN;
  const double filteredTotalN = validCells ? filteredTotalKgf * STANDARD_GRAVITY : NAN;
  emitf("DATA,%lu,%ld,%ld,%.6f,%.6f,%.6f,%.6f,%.6f",
        millis(), raw[0], raw[1], kgf[0], kgf[1], reportedTotalKgf,
        totalForceN, filteredTotalN);
}
