// Base station: pairs with the spine wearable over ESP-NOW, receives its
// readings, and prints one JSON object per line over USB for the laptop.
// Works on: any ESP32 (spare XIAO ESP32S3 or ESP32 DevKit). Nothing to wire,
// just plug it into the laptop.
//
// Pairing is automatic: the base announces itself until a wearable with the
// same PAIR_KEY asks to pair. After that, data goes only between those two,
// and either side re-pairs on its own if the link drops for 3 seconds.
//
// Serial: 115200 baud. Sending 'c' over serial tells the wearable to recalibrate.
//
// Output, one line each:
//   {"type":"data","seq":12,"t":4567,"phase":"live","torso":12.3,"upper":1.2,
//    "mid":-0.4,"lumbar":5.6,"sensors":{"C":1.0,"B":6.6,"A":6.2,"BNO":7.4},"ok":15,"lost":0}
//   {"type":"status","msg":"..."}
// Angles are degrees, + = bending forward. null = not available (calibrating,
// or that sensor isn't working).

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include "esp_arduino_version.h"

// ---------- Radio protocol (must match spine_wearable.ino) ----------
#define WIFI_CHANNEL 1
#define SPINE_MAGIC  0x5B17
#define PAIR_KEY     0x53484B42UL
const uint32_t BEACON_MS = 500;        // "I'm here" while unpaired
const uint32_t HEARTBEAT_MS = 500;     // "still here" while paired
const uint32_t LINK_TIMEOUT_MS = 3000;

enum PacketType : uint8_t {
  PKT_DATA     = 1,
  PKT_CMD      = 2,
  PKT_BEACON   = 3,
  PKT_PAIR_REQ = 4,
  PKT_PAIR_ACK = 5,
};

struct __attribute__((packed)) SpinePacket {
  uint16_t magic;
  uint8_t  type;
  uint8_t  phase;
  uint32_t seq;
  uint32_t ms;
  float    fwd[4];    // C, B, A, BNO
  float    torso, upper, mid, lumbar;
  uint8_t  okMask;
};

struct __attribute__((packed)) CtrlPacket {
  uint16_t magic;
  uint8_t  type;
  uint8_t  arg;
  uint32_t key;
};

static_assert(sizeof(SpinePacket) <= 64, "packet too big for the receive buffer");

const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
const char* PHASES[] = {"neutral", "bend_wait", "bend_hold", "live"};

// ---------- State ----------
struct RxItem { uint8_t mac[6]; uint8_t len; uint8_t data[64]; };
QueueHandle_t rxQueue;

enum LinkState { UNPAIRED, PAIRED };
LinkState linkState = UNPAIRED;
uint8_t wearMac[6];
uint32_t lastBeacon = 0, lastHeartbeat = 0, lastRxMs = 0;
uint32_t lastSeq = 0, lost = 0;
bool gotAny = false;

// ---------- Helpers ----------
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

void printStatus(const String &msg) {
  Serial.printf("{\"type\":\"status\",\"msg\":\"%s\"}\n", msg.c_str());
}

void printNum(float v) {
  if (isnan(v)) Serial.print("null");
  else Serial.print(v, 1);
}

void printPacket(const SpinePacket &p) {
  Serial.printf("{\"type\":\"data\",\"seq\":%lu,\"t\":%lu,\"phase\":\"%s\"",
                (unsigned long)p.seq, (unsigned long)p.ms,
                p.phase < 4 ? PHASES[p.phase] : "unknown");
  Serial.print(",\"torso\":");  printNum(p.torso);
  Serial.print(",\"upper\":");  printNum(p.upper);
  Serial.print(",\"mid\":");    printNum(p.mid);
  Serial.print(",\"lumbar\":"); printNum(p.lumbar);
  Serial.print(",\"sensors\":{\"C\":"); printNum(p.fwd[0]);
  Serial.print(",\"B\":");   printNum(p.fwd[1]);
  Serial.print(",\"A\":");   printNum(p.fwd[2]);
  Serial.print(",\"BNO\":"); printNum(p.fwd[3]);
  Serial.printf("},\"ok\":%u,\"lost\":%lu}\n", p.okMask, (unsigned long)lost);
}

// ---------- Radio handling ----------
void handleRadio() {
  RxItem it;
  while (xQueueReceive(rxQueue, &it, 0) == pdTRUE) {
    bool fromWearable = (linkState == PAIRED && memcmp(it.mac, wearMac, 6) == 0);

    // Pairing requests
    if (it.len == sizeof(CtrlPacket)) {
      CtrlPacket c;
      memcpy(&c, it.data, sizeof(c));
      if (c.magic != SPINE_MAGIC || c.key != PAIR_KEY || c.type != PKT_PAIR_REQ) continue;
      if (linkState == PAIRED && !fromWearable) continue;   // already taken

      if (!addPeer(it.mac)) continue;
      bool again = fromWearable;
      memcpy(wearMac, it.mac, 6);
      linkState = PAIRED;
      lastRxMs = millis();
      gotAny = false;
      lost = 0;
      sendCtrl(wearMac, PKT_PAIR_ACK);
      printStatus(String(again ? "wearable re-paired " : "paired with wearable ") + macStr(wearMac));
      continue;
    }

    // Readings, only from our paired wearable
    if (it.len == sizeof(SpinePacket) && fromWearable) {
      SpinePacket p;
      memcpy(&p, it.data, sizeof(p));
      if (p.magic != SPINE_MAGIC || p.type != PKT_DATA) continue;

      if (gotAny && p.seq > lastSeq + 1) lost += p.seq - lastSeq - 1;
      gotAny = true;
      lastSeq = p.seq;
      lastRxMs = millis();
      printPacket(p);
    }
  }

  uint32_t now = millis();
  if (linkState == UNPAIRED && now - lastBeacon >= BEACON_MS) {
    lastBeacon = now;
    sendCtrl(BROADCAST, PKT_BEACON);
  }
  if (linkState == PAIRED && now - lastHeartbeat >= HEARTBEAT_MS) {
    lastHeartbeat = now;
    sendCtrl(wearMac, PKT_PAIR_ACK);
  }
  if (linkState == PAIRED && now - lastRxMs > LINK_TIMEOUT_MS) {
    esp_now_del_peer(wearMac);
    linkState = UNPAIRED;
    printStatus("lost the wearable, waiting for it to reconnect");
  }
}

// ---------- Setup / loop ----------
void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {}
  delay(200);

  rxQueue = xQueueCreate(32, sizeof(RxItem));

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) {
    printStatus("ESP-NOW failed to start");
    while (true) delay(1000);
  }
  esp_now_register_recv_cb(onRadioRecv);
  addPeer(BROADCAST);   // needed to send the pairing beacon

  printStatus(String("base station ready (") + WiFi.macAddress() + "), waiting for wearable");
}

void loop() {
  handleRadio();

  // Commands from the laptop
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == 'c' || ch == 'C') {
      if (linkState == PAIRED) {
        sendCtrl(wearMac, PKT_CMD, 'c');
        printStatus("recalibration sent to wearable");
      } else {
        printStatus("no wearable paired yet, can't recalibrate");
      }
    }
  }
}
