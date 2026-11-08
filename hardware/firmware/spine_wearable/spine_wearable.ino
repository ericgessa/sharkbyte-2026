// Spine wearable (wireless): 3x MPU-6050 (AD0 select) + BNO085
// Same as spine_test, plus a paired ESP-NOW link to the base station
// (base_station.ino): it finds the base automatically, pairs with it, and
// sends its readings straight to it 50 times a second.
// Works on: XIAO ESP32S3, ESP32 DevKit
//
// The USB Serial Monitor still works exactly like spine_test when plugged in.
//
// Wiring: all four sensors share 3V3, GND, SDA, SCL
//   SDA -> XIAO D4 | DevKit GPIO21      SCL -> XIAO D5 | DevKit GPIO22
//   MPU A (mid back)   AD0 -> XIAO D0 | DevKit GPIO25
//   MPU B (lower back) AD0 -> XIAO D1 | DevKit GPIO26
//   MPU C (sacrum)     AD0 -> XIAO D3 | DevKit GPIO27
//   BNO085 (upper back): no extra wires, address 0x4A
//
// Library: "Adafruit BNO08x". Serial monitor: 115200 baud.
//
// Calibration (runs at startup, or send 'c' in the Serial Monitor, or type
// 'c' into the laptop receiver, which relays it through the base station):
//   1. Stand in neutral posture and hold still for 3 seconds.
//   2. Slowly bend forward as far as is comfortable and hold.
// The sketch learns which way is "forward" for each sensor from that bend,
// so the sensors don't need to be perfectly aligned.

#include <Wire.h>
#include <Adafruit_BNO08x.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include "esp_arduino_version.h"

// ---------- Radio protocol (must match base_station.ino) ----------
#define WIFI_CHANNEL 1
#define SPINE_MAGIC  0x5B17
#define PAIR_KEY     0x53484B42UL    // base and wearable only pair if these match
const uint32_t SEND_MS = 20;         // 50 readings per second
const uint32_t PAIR_TIMEOUT_MS = 1000;
const uint32_t LINK_TIMEOUT_MS = 3000;

enum PacketType : uint8_t {
  PKT_DATA     = 1,   // wearable -> base: readings
  PKT_CMD      = 2,   // base -> wearable: command (arg 'c' = recalibrate)
  PKT_BEACON   = 3,   // base -> everyone: "a base station is here"
  PKT_PAIR_REQ = 4,   // wearable -> base: "pair with me"
  PKT_PAIR_ACK = 5,   // base -> wearable: "paired" (also repeated as a heartbeat)
};

struct __attribute__((packed)) SpinePacket {
  uint16_t magic;
  uint8_t  type;      // PKT_DATA
  uint8_t  phase;     // 0 neutral, 1 bend_wait, 2 bend_hold, 3 live
  uint32_t seq;       // counts up, lets the base station spot lost packets
  uint32_t ms;        // wearable's millis()
  float    fwd[4];    // each sensor's forward angle: C, B, A, BNO (NaN if not live)
  float    torso, upper, mid, lumbar;
  uint8_t  okMask;    // bit i set = sensor i is working
};

struct __attribute__((packed)) CtrlPacket {
  uint16_t magic;
  uint8_t  type;      // PKT_CMD, PKT_BEACON, PKT_PAIR_REQ, PKT_PAIR_ACK
  uint8_t  arg;
  uint32_t key;       // PAIR_KEY
};

static_assert(sizeof(SpinePacket) <= 64, "packet too big for the receive buffer");

// ---------- Pins ----------
// Sensors are numbered bottom to top: 0 = MPU C, 1 = MPU B, 2 = MPU A, 3 = BNO085
#if defined(CONFIG_IDF_TARGET_ESP32S3)
// XIAO ESP32S3: D3 = GPIO4, D1 = GPIO2, D0 = GPIO1
const int MPU_AD0[3] = {4, 2, 1};
#else
const int MPU_AD0[3] = {27, 26, 25};
#endif

const int NS = 4;
const int BNO = 3;
const char* NAMES[NS] = {"MPU C (sacrum)", "MPU B (lower back)",
                         "MPU A (mid back)", "BNO085 (upper back)"};

const float ALPHA = 0.98f;           // filter: trust in gyro vs accelerometer
const uint32_t TICK_US = 10000;      // 100 Hz
const int NEUTRAL_TICKS = 300;       // 3 s
const int BEND_TICKS = 100;          // 1 s
const uint32_t BEND_WAIT_MS = 4000;

// ---------- Small 3D vector helpers ----------
struct V3 { float x, y, z; };
V3 v3(float x, float y, float z) { return {x, y, z}; }
V3 add(V3 a, V3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
V3 sub(V3 a, V3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
V3 mul(V3 a, float s) { return {a.x * s, a.y * s, a.z * s}; }
float dot(V3 a, V3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
V3 cross(V3 a, V3 b) {
  return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}
V3 normalize(V3 a) {
  float n = sqrtf(dot(a, a));
  return n > 1e-6f ? mul(a, 1.0f / n) : a;
}

// ---------- State ----------
Adafruit_BNO08x bno(-1);
sh2_SensorValue_t bnoVal;

bool ok[NS];
uint32_t errors[NS];
V3 upNow[NS];          // current "up" direction, in each sensor's own axes
V3 upNeutral[NS];      // "up" during neutral posture
V3 bendAxis[NS];       // each sensor's forward-bend rotation axis
V3 upSum[NS];
V3 gyroBias[3], gyroSum[3];

enum Phase { NEUTRAL, BEND_WAIT, BEND_HOLD, LIVE };
Phase phase;
int tickCount = 0;
uint32_t phaseStart = 0, lastTick = 0, lastPrint = 0;
bool debugView = false;
uint32_t bnoEvents = 0, bnoEventsLast = 0;

// ---------- MPU access (AD0 select) ----------
void selectMPU(int m) {
  for (int k = 0; k < 3; k++) digitalWrite(MPU_AD0[k], k == m ? LOW : HIGH);
  delayMicroseconds(20);
}
void deselectAll() { selectMPU(-1); }

bool present(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

void writeReg(uint8_t reg, uint8_t v) {
  Wire.beginTransmission(0x68);
  Wire.write(reg);
  Wire.write(v);
  Wire.endTransmission();
}

bool readRaw(int m, int16_t out[7]) {
  selectMPU(m);
  Wire.beginTransmission(0x68);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)0x68, (uint8_t)14) != 14) return false;
  for (int i = 0; i < 7; i++) {
    uint8_t hi = Wire.read();
    uint8_t lo = Wire.read();
    out[i] = (int16_t)((hi << 8) | lo);
  }
  return true;
}

// ---------- BNO085 ----------
void pollBNO() {
  if (!ok[BNO]) return;
  if (bno.wasReset()) {
    if (!bno.enableReport(SH2_GRAVITY, TICK_US)) Serial.println("! BNO085 reset and could not restart its gravity report");
  }
  while (bno.getSensorEvent(&bnoVal)) {
    if (bnoVal.sensorId == SH2_GRAVITY) {
      upNow[BNO] = normalize(v3(bnoVal.un.gravity.x, bnoVal.un.gravity.y, bnoVal.un.gravity.z));
      bnoEvents++;
    }
  }
}

// ---------- Calibration ----------
void startCalibration() {
  for (int i = 0; i < NS; i++) upSum[i] = v3(0, 0, 0);
  for (int m = 0; m < 3; m++) gyroSum[m] = v3(0, 0, 0);
  tickCount = 0;
  phase = NEUTRAL;
  Serial.println("\n[Calibration 1/2] Stand tall in your normal posture and hold STILL for 3 seconds...");
}

void finishCalibration() {
  Serial.println("  Bend captured. Forward tilt seen by each sensor:");
  for (int i = 0; i < NS; i++) {
    if (!ok[i]) continue;
    V3 bent = normalize(upSum[i]);
    float tilt = acosf(constrain(dot(upNeutral[i], bent), -1.0f, 1.0f)) * RAD_TO_DEG;
    bendAxis[i] = normalize(cross(upNeutral[i], bent));
    Serial.printf("    %-20s %5.1f deg%s\n", NAMES[i], tilt,
                  tilt < 8 ? "   <- barely moved, send 'c' and bend deeper" : "");
  }
  Serial.println("\nStand back up. Live readings in degrees (+ = bending forward).");
  Serial.println("torso = whole upper body lean | upper/mid/lumbar = bend within each section");
  Serial.println("Send 'c' to recalibrate, 'd' to toggle the per-sensor debug view.\n");
}

// Signed forward angle of sensor i relative to neutral, in degrees
float forwardAngle(int i) {
  V3 c = cross(upNeutral[i], upNow[i]);
  return atan2f(dot(c, bendAxis[i]), dot(upNeutral[i], upNow[i])) * RAD_TO_DEG;
}

// Signed sideways angle of sensor i (tilt that is NOT forward/back), in degrees
float sideAngle(int i) {
  V3 sideAxis = normalize(cross(upNeutral[i], bendAxis[i]));
  V3 c = cross(upNeutral[i], upNow[i]);
  return atan2f(dot(c, sideAxis), dot(upNeutral[i], upNow[i])) * RAD_TO_DEG;
}

// ---------- 100 Hz update ----------
void tick(float dt) {
  for (int m = 0; m < 3; m++) {
    if (!ok[m]) continue;
    int16_t r[7];
    if (!readRaw(m, r)) { errors[m]++; continue; }

    V3 acc = normalize(v3(r[0], r[1], r[2]));
    V3 gyroDeg = v3(r[4] / 131.0f, r[5] / 131.0f, r[6] / 131.0f);

    if (phase == NEUTRAL) {
      upNow[m] = acc;                               // still: accelerometer alone is fine
      gyroSum[m] = add(gyroSum[m], gyroDeg);        // learn gyro offset while still
    } else {
      // Rotate the "up" estimate by the gyro, then nudge it toward the accelerometer
      V3 w = mul(sub(gyroDeg, gyroBias[m]), DEG_TO_RAD);
      V3 g = sub(upNow[m], mul(cross(w, upNow[m]), dt));
      upNow[m] = normalize(add(mul(g, ALPHA), mul(acc, 1 - ALPHA)));
    }
  }
  deselectAll();

  if (phase == NEUTRAL) {
    for (int i = 0; i < NS; i++) if (ok[i]) upSum[i] = add(upSum[i], upNow[i]);
    if (++tickCount >= NEUTRAL_TICKS) {
      for (int i = 0; i < NS; i++) upNeutral[i] = normalize(upSum[i]);
      for (int m = 0; m < 3; m++) {
        gyroBias[m] = mul(gyroSum[m], 1.0f / tickCount);
        upNow[m] = upNeutral[m];
      }
      Serial.println("  Neutral captured.");
      Serial.println("[Calibration 2/2] Slowly bend FORWARD as far as is comfortable "
                     "(reach toward your knees) and hold. Capturing in 4 seconds...");
      phase = BEND_WAIT;
      phaseStart = millis();
    }
  } else if (phase == BEND_WAIT) {
    if (millis() - phaseStart >= BEND_WAIT_MS) {
      for (int i = 0; i < NS; i++) upSum[i] = v3(0, 0, 0);
      tickCount = 0;
      phase = BEND_HOLD;
      Serial.println("  Hold it...");
    }
  } else if (phase == BEND_HOLD) {
    for (int i = 0; i < NS; i++) if (ok[i]) upSum[i] = add(upSum[i], upNow[i]);
    if (++tickCount >= BEND_TICKS) {
      finishCalibration();
      phase = LIVE;
    }
  }
}

// ---------- Radio link ----------
// The radio callback runs on a different task, so it only copies packets into
// a queue; everything else happens in loop().
struct RxItem { uint8_t mac[6]; uint8_t len; uint8_t data[64]; };
QueueHandle_t rxQueue;

enum LinkState { SEARCHING, WAIT_ACK, PAIRED };
LinkState linkState = SEARCHING;
uint8_t baseMac[6];
bool radioOk = false, calRequested = false;
uint32_t linkTimer = 0, lastHeard = 0, seqNo = 0, lastSend = 0;

void queueRx(const uint8_t *mac, const uint8_t *data, int len) {
  if (len < 3 || len > 64) return;
  RxItem it;
  memcpy(it.mac, mac, 6);
  it.len = len;
  memcpy(it.data, data, len);
  xQueueSend(rxQueue, &it, 0);
}

#if ESP_ARDUINO_VERSION_MAJOR >= 3
void onRadioRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  queueRx(info->src_addr, data, len);
}
#else
void onRadioRecv(const uint8_t *mac, const uint8_t *data, int len) {
  queueRx(mac, data, len);
}
#endif

String macStr(const uint8_t *m) {
  char b[18];
  snprintf(b, sizeof(b), "%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
  return String(b);
}

bool addPeer(const uint8_t *mac) {
  if (esp_now_is_peer_exist(mac)) return true;
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, mac, 6);
  peer.channel = WIFI_CHANNEL;
  peer.encrypt = false;
  return esp_now_add_peer(&peer) == ESP_OK;
}

void sendCtrl(const uint8_t *mac, uint8_t type, uint8_t arg = 0) {
  CtrlPacket c = {SPINE_MAGIC, type, arg, PAIR_KEY};
  esp_now_send(mac, (const uint8_t *)&c, sizeof(c));
}

void dropLink(const char *why) {
  if (esp_now_is_peer_exist(baseMac)) esp_now_del_peer(baseMac);
  linkState = SEARCHING;
  Serial.printf("[radio] %s, searching for base station...\n", why);
}

void setupRadio() {
  rxQueue = xQueueCreate(16, sizeof(RxItem));
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) {
    Serial.println("! ESP-NOW failed to start (data will only go over USB)");
    return;
  }
  esp_now_register_recv_cb(onRadioRecv);
  radioOk = true;
  Serial.printf("  ESP-NOW ready on channel %d (this wearable: %s)\n",
                WIFI_CHANNEL, WiFi.macAddress().c_str());
  Serial.println("[radio] searching for base station...");
}

void handleRadio() {
  if (!radioOk) return;

  RxItem it;
  while (xQueueReceive(rxQueue, &it, 0) == pdTRUE) {
    if (it.len != sizeof(CtrlPacket)) continue;   // the wearable only expects control packets
    CtrlPacket c;
    memcpy(&c, it.data, sizeof(c));
    if (c.magic != SPINE_MAGIC || c.key != PAIR_KEY) continue;
    bool fromBase = (memcmp(it.mac, baseMac, 6) == 0);

    if (linkState == SEARCHING) {
      if (c.type == PKT_BEACON && addPeer(it.mac)) {
        memcpy(baseMac, it.mac, 6);
        sendCtrl(baseMac, PKT_PAIR_REQ);
        linkState = WAIT_ACK;
        linkTimer = millis();
      }
    } else if (linkState == WAIT_ACK) {
      if (c.type == PKT_PAIR_ACK && fromBase) {
        linkState = PAIRED;
        lastHeard = millis();
        seqNo = 0;
        Serial.printf("[radio] paired with base station %s\n", macStr(baseMac).c_str());
      }
    } else if (linkState == PAIRED && fromBase) {
      lastHeard = millis();
      if (c.type == PKT_CMD && (c.arg == 'c' || c.arg == 'C')) calRequested = true;
      if (c.type == PKT_BEACON) {                 // base restarted: pair again
        sendCtrl(baseMac, PKT_PAIR_REQ);
        linkState = WAIT_ACK;
        linkTimer = millis();
      }
    }
  }

  if (linkState == WAIT_ACK && millis() - linkTimer > PAIR_TIMEOUT_MS) dropLink("pairing timed out");
  if (linkState == PAIRED && millis() - lastHeard > LINK_TIMEOUT_MS) dropLink("lost the base station");
}

void sendPacket() {
  SpinePacket p;
  p.magic = SPINE_MAGIC;
  p.type = PKT_DATA;
  p.phase = (uint8_t)phase;
  p.seq = seqNo++;
  p.ms = millis();
  p.okMask = 0;
  bool live = (phase == LIVE);
  for (int i = 0; i < NS; i++) {
    if (ok[i]) p.okMask |= (1 << i);
    p.fwd[i] = (live && ok[i]) ? forwardAngle(i) : NAN;
  }
  // Bottom to top: C(0) -> B(1) -> A(2) -> BNO(3). NaN carries through if missing.
  p.torso  = p.fwd[3];
  p.upper  = p.fwd[3] - p.fwd[2];
  p.mid    = p.fwd[2] - p.fwd[1];
  p.lumbar = p.fwd[1] - p.fwd[0];
  esp_now_send(baseMac, (const uint8_t *)&p, sizeof(p));
}

// ---------- Setup / loop ----------
void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}
  delay(200);
  Serial.println("\n=== Spine wearable (wireless): 3x MPU-6050 + BNO085 ===");

  for (int m = 0; m < 3; m++) pinMode(MPU_AD0[m], OUTPUT);
  deselectAll();
  Wire.begin();
  Wire.setClock(400000);   // drop to 100000 if you see read errors

  if (present(0x68)) Serial.println("! Something answers at 0x68 with all MPUs deselected: check AD0 wires.");

  for (int m = 0; m < 3; m++) {
    selectMPU(m);
    ok[m] = present(0x68);
    if (!ok[m]) {
      Serial.printf("  %-20s NOT FOUND\n", NAMES[m]);
      continue;
    }
    writeReg(0x6B, 0x00);   // wake up
    delay(50);
    writeReg(0x1A, 0x03);   // low-pass filter ~44 Hz
    writeReg(0x1B, 0x00);   // gyro +/-250 deg/s
    writeReg(0x1C, 0x00);   // accel +/-2 g
    delay(20);

    // A working, awake MPU sitting still should measure ~1.00 g of gravity
    int16_t r[7];
    if (readRaw(m, r)) {
      float g = sqrtf((float)r[0] * r[0] + (float)r[1] * r[1] + (float)r[2] * r[2]) / 16384.0f;
      Serial.printf("  %-20s OK, gravity reads %.2f g%s\n", NAMES[m], g,
                    (g < 0.7f || g > 1.3f) ? "   <- should be ~1.00: not sampling or data garbled" : "");
    } else {
      Serial.printf("  %-20s found, but data read FAILED\n", NAMES[m]);
    }
  }
  deselectAll();

  ok[BNO] = bno.begin_I2C(0x4A, &Wire);
  if (ok[BNO] && !bno.enableReport(SH2_GRAVITY, TICK_US)) {
    Serial.println("! BNO085 found, but its gravity report could not be enabled");
  }
  Serial.printf("  %-20s %s\n", NAMES[BNO], ok[BNO] ? "OK" : "NOT FOUND");

  for (int i = 0; i < NS; i++) upNow[i] = v3(0, 0, 1);

  setupRadio();
  startCalibration();
  lastTick = micros();
}

void loop() {
  pollBNO();
  handleRadio();

  if (calRequested) {
    calRequested = false;
    Serial.println("\n[recalibration requested from base station]");
    startCalibration();
  }

  if (linkState == PAIRED && millis() - lastSend >= SEND_MS) {
    lastSend = millis();
    sendPacket();
  }

  if (Serial.available()) {
    char ch = Serial.read();
    if (ch == 'c' || ch == 'C') startCalibration();
    if (ch == 'd' || ch == 'D') {
      debugView = !debugView;
      Serial.println(debugView ? "\n[debug view: each sensor on its own]"
                               : "\n[normal view: spine sections]");
    }
  }

  uint32_t now = micros();
  if (now - lastTick >= TICK_US) {
    float dt = (now - lastTick) / 1e6f;
    lastTick = now;
    tick(dt);
  }

  if (phase == LIVE && millis() - lastPrint >= 100) {
    lastPrint = millis();
    float a[NS];
    for (int i = 0; i < NS; i++) a[i] = ok[i] ? forwardAngle(i) : NAN;

    // Bottom to top: C(0) -> B(1) -> A(2) -> BNO(3)
    float lumbar = a[1] - a[0];
    float mid    = a[2] - a[1];
    float upper  = a[3] - a[2];
    float torso  = a[3];

    if (debugView) {
      // Each sensor on its own: fwd = forward/back tilt, side = sideways tilt
      const char* tag[NS] = {"C", "B", "A", "BNO"};
      for (int i = 0; i < NS; i++) {
        Serial.printf("%s fwd %6.1f side %6.1f%s", tag[i], a[i],
                      ok[i] ? sideAngle(i) : NAN, i < NS - 1 ? " | " : "");
      }
      // Printed every 100 ms, so x10 = readings per second (should be ~100)
      Serial.printf(" | BNO %lu Hz", (unsigned long)((bnoEvents - bnoEventsLast) * 10));
      bnoEventsLast = bnoEvents;
    } else {
      Serial.printf("torso %6.1f | upper %6.1f | mid %6.1f | lumbar %6.1f",
                    torso, upper, mid, lumbar);
    }
    if (errors[0] || errors[1] || errors[2]) {
      Serial.printf(" | read errors C:%lu B:%lu A:%lu",
                    (unsigned long)errors[0], (unsigned long)errors[1], (unsigned long)errors[2]);
    }
    Serial.println();
  }
}
