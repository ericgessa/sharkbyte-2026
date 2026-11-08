// MPU select check: proves whether you're reading two different chips
// Same wiring as mpu_dual_test. Serial monitor: 115200 baud.
//
// How it works:
//   1. Writes a different "tag" value into each MPU (register 0x19, harmless).
//      If the selection works, each chip keeps its own tag.
//      If both chips hear every write, both end up with the LAST tag.
//   2. Prints raw readings from each side by side. Two different chips
//      never give identical raw numbers, even lying in the same position.

#include <Wire.h>

#if defined(ARDUINO_XIAO_ESP32S3)
const int AD0_PINS[] = {D0, D1};
#else
const int AD0_PINS[] = {25, 26};
#endif

const char* NAMES[] = {"MPU A", "MPU B"};
const uint8_t TAGS[] = {0x11, 0x22};

void selectMPU(int idx) {
  for (int k = 0; k < 2; k++) digitalWrite(AD0_PINS[k], k == idx ? LOW : HIGH);
  delayMicroseconds(50);
}

void writeReg(uint8_t reg, uint8_t v) {
  Wire.beginTransmission(0x68);
  Wire.write(reg);
  Wire.write(v);
  Wire.endTransmission();
}

int readReg(uint8_t reg) {
  Wire.beginTransmission(0x68);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return -1;
  if (Wire.requestFrom((uint8_t)0x68, (uint8_t)1) != 1) return -1;
  return Wire.read();
}

int16_t readWord(uint8_t reg) {
  return (int16_t)((readReg(reg) << 8) | readReg(reg + 1));
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}
  delay(200);
  Serial.println("\n=== MPU select check ===");

  for (int k = 0; k < 2; k++) pinMode(AD0_PINS[k], OUTPUT);
  selectMPU(-1);                       // both HIGH = both at 0x69
  Wire.begin();
  Wire.setClock(100000);               // slow, to rule out wiring speed issues

  // Wake each chip and write its own tag
  for (int i = 0; i < 2; i++) {
    selectMPU(i);
    writeReg(0x6B, 0x00);              // wake up
    delay(50);
    writeReg(0x19, TAGS[i]);           // write tag
  }

  // Read the tags back
  Serial.println("\nTag test:");
  bool pass = true;
  for (int i = 0; i < 2; i++) {
    selectMPU(i);
    int tag = readReg(0x19);
    bool good = (tag == TAGS[i]);
    pass &= good;
    Serial.printf("  %s tag = 0x%02X (expected 0x%02X) %s\n",
                  NAMES[i], tag, TAGS[i], good ? "OK" : "WRONG");
  }
  selectMPU(-1);
  Serial.println(pass
    ? "  PASS: two separate chips, selection works.\n"
    : "  FAIL: both chips are receiving the same commands.\n");

  Serial.println("Raw readings (accel X, Y, Z, temperature):");
}

void loop() {
  int16_t v[2][4];
  for (int i = 0; i < 2; i++) {
    selectMPU(i);
    v[i][0] = readWord(0x3B);          // accel X
    v[i][1] = readWord(0x3D);          // accel Y
    v[i][2] = readWord(0x3F);          // accel Z
    v[i][3] = readWord(0x41);          // temperature
  }
  selectMPU(-1);

  bool identical = true;
  for (int a = 0; a < 4; a++) if (v[0][a] != v[1][a]) identical = false;

  Serial.printf("A: %6d %6d %6d %6d  |  B: %6d %6d %6d %6d  %s\n",
                v[0][0], v[0][1], v[0][2], v[0][3],
                v[1][0], v[1][1], v[1][2], v[1][3],
                identical ? "<- IDENTICAL (same chip!)" : "");
  delay(500);
}
