// AD0 phase check: find out why both MPUs answer at once
// Same wiring as before. Serial monitor: 115200 baud.
//
// Cycles through three states, holding each for 5 seconds:
//   A selected   -> MPU A AD0 LOW,  MPU B AD0 HIGH
//   B selected   -> MPU A AD0 HIGH, MPU B AD0 LOW
//   none         -> both HIGH
// In each state it scans the bus and says what it SHOULD find.
// While a state is held, measure each GY-521's AD0 pin to GND with a multimeter.

#include <Wire.h>

#if defined(ARDUINO_XIAO_ESP32S3)
const int AD0_A = D0;
const int AD0_B = D1;
#else
const int AD0_A = 25;
const int AD0_B = 26;
#endif

bool present(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

void runState(const char* name, int levelA, int levelB) {
  digitalWrite(AD0_A, levelA);
  digitalWrite(AD0_B, levelB);
  delay(10);

  bool at68 = present(0x68);
  bool at69 = present(0x69);

  Serial.printf("\n--- %s ---\n", name);
  Serial.printf("  Pins set: MPU A AD0 = %s, MPU B AD0 = %s\n",
                levelA ? "HIGH" : "LOW", levelB ? "HIGH" : "LOW");
  Serial.printf("  Multimeter should read: A AD0 = %s, B AD0 = %s\n",
                levelA ? "~3.3V" : "~0V", levelB ? "~3.3V" : "~0V");

  bool expect68 = (levelA == LOW) || (levelB == LOW);
  bool expect69 = (levelA == HIGH) || (levelB == HIGH);
  Serial.printf("  0x68: %-6s (expected %s)\n", at68 ? "FOUND" : "empty", expect68 ? "FOUND" : "empty");
  Serial.printf("  0x69: %-6s (expected %s)\n", at69 ? "FOUND" : "empty", expect69 ? "FOUND" : "empty");

  if (at68 == expect68 && at69 == expect69) {
    Serial.println("  OK");
  } else if (expect68 && expect69 && !at69) {
    Serial.println("  PROBLEM: the deselected MPU did not move to 0x69.");
    Serial.println("           Its AD0 isn't getting 3.3V (wires touching, wrong pin, or on INT).");
  } else if (!expect68 && at68) {
    Serial.println("  PROBLEM: something is still at 0x68 with both deselected.");
  } else {
    Serial.println("  PROBLEM: unexpected result, check SDA/SCL/power.");
  }

  delay(5000);   // hold this state so you can measure with a multimeter
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}
  delay(200);
  Serial.println("\n=== AD0 phase check ===");
  Serial.printf("Using GPIO%d for MPU A AD0, GPIO%d for MPU B AD0\n", AD0_A, AD0_B);

  pinMode(AD0_A, OUTPUT);
  pinMode(AD0_B, OUTPUT);
  digitalWrite(AD0_A, HIGH);
  digitalWrite(AD0_B, HIGH);
  Wire.begin();
  Wire.setClock(100000);
}

void loop() {
  runState("A selected", LOW, HIGH);
  runState("B selected", HIGH, LOW);
  runState("none selected", HIGH, HIGH);
}
