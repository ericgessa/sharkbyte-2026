// Two MPU-6050 test using the AD0 select trick
// Works on: XIAO ESP32S3, ESP32 DevKit (any ESP32 board)
//
// Wiring (both MPUs share the first four):
//   VCC -> 3V3          GND -> GND
//   SDA -> XIAO D4  | DevKit GPIO21
//   SCL -> XIAO D5  | DevKit GPIO22
//   MPU A AD0 -> XIAO D0 | DevKit GPIO25
//   MPU B AD0 -> XIAO D1 | DevKit GPIO26
//   Leave XDA, XCL, INT unconnected.
//
// No extra libraries needed. Serial monitor: 115200 baud.
// Keep both sensors STILL for the first ~2 seconds (gyro calibration).

#include <Wire.h>

#if defined(ARDUINO_XIAO_ESP32S3)
const int AD0_PINS[] = {D0, D1};
#else
const int AD0_PINS[] = {25, 26};      // ESP32 DevKit: change here if wired differently
#endif

const int N = 2;
const char* NAMES[N] = {"MPU A", "MPU B"};

const uint8_t ADDR_SELECTED = 0x68;   // the MPU whose AD0 is LOW answers here
const uint8_t ADDR_IDLE     = 0x69;   // all the others (AD0 HIGH) sit here, ignored

const float ALPHA       = 0.98f;      // complementary filter: trust in gyro vs accel
const uint32_t LOOP_US  = 10000;      // 100 Hz update
const uint32_t PRINT_MS = 100;        // 10 lines per second

bool ok[N];
float pitch[N], roll[N];
float gyroBias[N][3];
uint32_t readErrors[N];
uint32_t lastLoop = 0, lastPrint = 0;

// ---------- AD0 select ----------

void selectMPU(int idx) {
  for (int k = 0; k < N; k++) digitalWrite(AD0_PINS[k], k == idx ? LOW : HIGH);
  delayMicroseconds(20);
}

void deselectAll() {
  for (int k = 0; k < N; k++) digitalWrite(AD0_PINS[k], HIGH);
  delayMicroseconds(20);
}

// ---------- I2C helpers (always talk to 0x68 = the selected MPU) ----------

bool present(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

bool writeReg(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(ADDR_SELECTED);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

int readReg(uint8_t reg) {
  Wire.beginTransmission(ADDR_SELECTED);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return -1;
  if (Wire.requestFrom(ADDR_SELECTED, (uint8_t)1) != 1) return -1;
  return Wire.read();
}

// Reads accel X/Y/Z, temperature, gyro X/Y/Z (7 values) from MPU idx
bool readRaw(int idx, int16_t out[7]) {
  selectMPU(idx);
  Wire.beginTransmission(ADDR_SELECTED);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(ADDR_SELECTED, (uint8_t)14) != 14) return false;
  for (int i = 0; i < 7; i++) {
    uint8_t hi = Wire.read();          // read high byte first, then low byte
    uint8_t lo = Wire.read();
    out[i] = (int16_t)((hi << 8) | lo);
  }
  return true;
}

const char* chipName(int who) {
  switch (who) {
    case 0x68: return "MPU-6050 (genuine)";
    case 0x70: return "MPU-6500 (clone/variant, works the same here)";
    case 0x71: return "MPU-9250 (works the same here)";
    case 0x72: case 0x73: return "MPU-6500 family clone (works the same here)";
    default:   return "unknown chip";
  }
}

// ---------- Setup ----------

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}
  delay(200);
  Serial.println("\n=== Dual MPU-6050 test (AD0 trick) ===");
  Serial.printf("AD0 pins: MPU A = GPIO%d, MPU B = GPIO%d\n", AD0_PINS[0], AD0_PINS[1]);

  // Drive every AD0 HIGH *before* starting I2C, so no two MPUs share 0x68
  for (int k = 0; k < N; k++) pinMode(AD0_PINS[k], OUTPUT);
  deselectAll();

  Wire.begin();
  Wire.setClock(400000);

  // Check 1: with everyone deselected, nothing should answer at 0x68
  Serial.println("\n[1] All deselected:");
  bool at68 = present(ADDR_SELECTED), at69 = present(ADDR_IDLE);
  Serial.printf("  0x68 %s   0x69 %s\n", at68 ? "FOUND" : "empty", at69 ? "FOUND" : "empty");
  if (at68) Serial.println("  ! Something is stuck at 0x68: an AD0 wire is probably loose.");
  if (!at69) Serial.println("  ! Nothing at 0x69: check 3V3, GND, SDA, SCL.");

  // Check 2: select each MPU, confirm it answers alone, identify it, configure it
  Serial.println("\n[2] Selecting each MPU:");
  for (int i = 0; i < N; i++) {
    selectMPU(i);
    ok[i] = present(ADDR_SELECTED);
    if (!ok[i]) {
      Serial.printf("  %s: no answer at 0x68. Check its AD0 wire and its SDA/SCL.\n", NAMES[i]);
      continue;
    }
    int who = readReg(0x75);           // WHO_AM_I register
    Serial.printf("  %s: OK, WHO_AM_I = 0x%02X -> %s\n", NAMES[i], who, chipName(who));

    writeReg(0x6B, 0x00);              // wake up (chips boot in sleep mode)
    delay(50);
    writeReg(0x1A, 0x03);              // low-pass filter ~44 Hz (smooths vibration)
    writeReg(0x1B, 0x00);              // gyro range +/-250 deg/s
    writeReg(0x1C, 0x00);              // accel range +/-2 g
  }
  deselectAll();

  // Check 3: gyro calibration, sensors must be still
  Serial.println("\n[3] Calibrating gyros, keep both sensors STILL...");
  int16_t raw[7];
  const int SAMPLES = 200;
  for (int i = 0; i < N; i++) gyroBias[i][0] = gyroBias[i][1] = gyroBias[i][2] = 0;
  for (int s = 0; s < SAMPLES; s++) {
    for (int i = 0; i < N; i++) {
      if (ok[i] && readRaw(i, raw)) {
        for (int a = 0; a < 3; a++) gyroBias[i][a] += raw[4 + a] / 131.0f;
      }
    }
    delay(5);
  }
  for (int i = 0; i < N; i++) {
    for (int a = 0; a < 3; a++) gyroBias[i][a] /= SAMPLES;
    if (ok[i] && readRaw(i, raw)) {    // start angles from the accelerometer
      float ax = raw[0] / 16384.0f, ay = raw[1] / 16384.0f, az = raw[2] / 16384.0f;
      roll[i]  = atan2(ay, az) * RAD_TO_DEG;
      pitch[i] = atan2(-ax, sqrt(ay * ay + az * az)) * RAD_TO_DEG;
    }
  }
  deselectAll();

  Serial.println("Done.\n");
  Serial.println("Wiggle ONE sensor at a time: only its numbers should change.");
  Serial.println("bend = MPU B pitch - MPU A pitch (this becomes the spine angle).\n");
  lastLoop = micros();
}

// ---------- Loop ----------

void loop() {
  uint32_t now = micros();
  if (now - lastLoop < LOOP_US) return;
  float dt = (now - lastLoop) / 1e6f;
  lastLoop = now;

  int16_t raw[7];
  for (int i = 0; i < N; i++) {
    if (!ok[i]) continue;
    if (!readRaw(i, raw)) { readErrors[i]++; continue; }

    float ax = raw[0] / 16384.0f, ay = raw[1] / 16384.0f, az = raw[2] / 16384.0f;
    float gx = raw[4] / 131.0f - gyroBias[i][0];
    float gy = raw[5] / 131.0f - gyroBias[i][1];

    float accRoll  = atan2(ay, az) * RAD_TO_DEG;
    float accPitch = atan2(-ax, sqrt(ay * ay + az * az)) * RAD_TO_DEG;

    // Gyro is smooth but drifts; accel is noisy but doesn't drift. Blend them.
    roll[i]  = ALPHA * (roll[i]  + gx * dt) + (1 - ALPHA) * accRoll;
    pitch[i] = ALPHA * (pitch[i] + gy * dt) + (1 - ALPHA) * accPitch;
  }
  deselectAll();

  if (millis() - lastPrint >= PRINT_MS) {
    lastPrint = millis();
    Serial.printf("A pitch %6.1f roll %6.1f | B pitch %6.1f roll %6.1f | bend %6.1f | errors A:%lu B:%lu\n",
                  pitch[0], roll[0], pitch[1], roll[1], pitch[1] - pitch[0],
                  (unsigned long)readErrors[0], (unsigned long)readErrors[1]);
  }
}
