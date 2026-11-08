// BNO085 quick test
// Works on: XIAO ESP32S3, ESP32 DevKit (any ESP32 board)
//
// Wiring:
//   BNO085 VIN -> 3V3
//   BNO085 GND -> GND
//   BNO085 SDA -> XIAO D4   | ESP32 DevKit GPIO21
//   BNO085 SCL -> XIAO D5   | ESP32 DevKit GPIO22
//   Leave PS0, PS1, INT, RST unconnected.
//
// Library: "Adafruit BNO08x" (Arduino Library Manager)
// Serial monitor: 115200 baud

#include <Wire.h>
#include <Adafruit_BNO08x.h>

#define BNO_ADDR     0x4A    // 0x4B if the board's address jumper is bridged
#define REPORT_US    10000   // sensor report interval: 10 ms = 100 Hz
#define PRINT_EVERY  100     // ms between printed lines (10 lines/sec)

Adafruit_BNO08x bno(-1);     // -1 = no reset pin wired
sh2_SensorValue_t val;

float yaw = 0, pitch = 0, roll = 0;
uint8_t accuracy = 0;
uint32_t eventCount = 0, lastPrint = 0, lastRateCheck = 0;
float rateHz = 0;

// Lists every device on the I2C bus so you can see whether the BNO085 shows up
void scanI2C() {
  Serial.println("Scanning I2C bus...");
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("  found device at 0x%02X%s\n", a,
                    a == BNO_ADDR ? "  <- BNO085" : "");
      found++;
    }
  }
  if (found == 0) {
    Serial.println("  nothing found: check 3V3, GND, and whether SDA/SCL are swapped");
  }
}

void enableReports() {
  if (!bno.enableReport(SH2_ROTATION_VECTOR, REPORT_US)) {
    Serial.println("Could not enable rotation vector report");
  }
}

// Converts the sensor's quaternion output to yaw/pitch/roll in degrees
void toEuler(float r, float i, float j, float k) {
  float sr = r * r, si = i * i, sj = j * j, sk = k * k;
  yaw   = atan2(2.0f * (i * j + k * r), (si - sj - sk + sr)) * RAD_TO_DEG;
  pitch = asin(-2.0f * (i * k - j * r) / (si + sj + sk + sr)) * RAD_TO_DEG;
  roll  = atan2(2.0f * (j * k + i * r), (-si - sj + sk + sr)) * RAD_TO_DEG;
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}   // wait for USB serial (XIAO)
  delay(200);
  Serial.println("\n=== BNO085 quick test ===");

  Wire.begin();   // uses the board's default SDA/SCL pins
  scanI2C();

  if (!bno.begin_I2C(BNO_ADDR, &Wire)) {
    Serial.println("BNO085 not responding at that address. Halting.");
    while (true) delay(1000);
  }
  Serial.println("BNO085 OK");
  enableReports();
  Serial.println("Tilt forward/back = pitch, side to side = roll, spin flat = yaw.\n");
}

void loop() {
  // The sensor can reset itself; reports must be re-enabled when it does
  if (bno.wasReset()) {
    Serial.println("Sensor reset, re-enabling reports");
    enableReports();
  }

  if (bno.getSensorEvent(&val) && val.sensorId == SH2_ROTATION_VECTOR) {
    toEuler(val.un.rotationVector.real, val.un.rotationVector.i,
            val.un.rotationVector.j, val.un.rotationVector.k);
    accuracy = val.status & 0x03;   // 0 = unreliable ... 3 = high
    eventCount++;
  }

  uint32_t now = millis();

  // Measure how many readings per second actually arrive
  if (now - lastRateCheck >= 1000) {
    rateHz = eventCount * 1000.0f / (now - lastRateCheck);
    eventCount = 0;
    lastRateCheck = now;
  }

  if (now - lastPrint >= PRINT_EVERY) {
    lastPrint = now;
    Serial.printf("yaw %7.1f   pitch %6.1f   roll %6.1f   | cal %u/3 | %5.1f Hz\n",
                  yaw, pitch, roll, accuracy, rateHz);
  }
}
