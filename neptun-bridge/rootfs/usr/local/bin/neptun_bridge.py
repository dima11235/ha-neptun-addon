#!/usr/bin/env python3
""" BRIDGE DOC: Neptun AquaControl MQTT bridge

High-level overview:
- Listens to cloud MQTT frames at `<prefix>/<MAC>/from` (binary).
- Validates frames (CRC16-CCITT), decodes content and publishes structured topics under `neptun/<MAC>/...`.
- Publishes Home Assistant discovery under `homeassistant/...` for switches and sensors.

Environment variables:
- NB_TOPIC_PREFIX: base for local topics (default: neptun).
- NB_DISCOVERY_PREFIX: HA discovery (default: homeassistant).
- NB_CLOUD_PREFIX: upstream prefix for sending commands back.
- NB_MQTT_USER/NB_MQTT_PASS: optional broker auth.
- NB_RETAIN/NB_DEBUG: behavior toggles.
- NB_WATCHDOG_PERIOD: watchdog check period in seconds (default: 30).

Protocol notes:
- Frame signature: 0x02 0x54 ... type at byte[3], big-endian length at [4..5], CRC16-CCITT trailing 2 bytes.
- Types: 0x52 system_state, 0x53 sensor_state, 0x43 counter_state, etc.
"""
import os, sys, json, time, struct, threading
from datetime import datetime, timezone
import paho.mqtt.client as mqtt

CLOUD_PREFIX   = os.getenv("NB_CLOUD_PREFIX", "")
TOPIC_PREFIX   = os.getenv("NB_TOPIC_PREFIX", "neptun")
DISCOVERY_PRE  = os.getenv("NB_DISCOVERY_PREFIX", "homeassistant")
RETAIN_DEFAULT = os.getenv("NB_RETAIN", "true").lower() == "true"
DEBUG          = os.getenv("NB_DEBUG", "false").lower() == "true"
MODULE_LOST_DEFAULT = int(os.getenv("NB_MODULE_LOST_TIMEOUT", "300"))
WATCHDOG_PERIOD = int(os.getenv("NB_WATCHDOG_PERIOD", "30"))
if WATCHDOG_PERIOD < 5:
    WATCHDOG_PERIOD = 5

MQTT_HOST = "127.0.0.1"
# Prefer NB_MQTT_PORT (from options), fallback to MQTT_LISTEN_PORT
MQTT_PORT = int(os.getenv("NB_MQTT_PORT") or os.getenv("MQTT_LISTEN_PORT", "2883"))
MQTT_USER = os.getenv("NB_MQTT_USER", None) or None
MQTT_PASS = os.getenv("NB_MQTT_PASS", None) or None

# Print startup parameters unconditionally (helps diagnostics even without DEBUG)
try:
    print(
        "[BRIDGE] startup:",
        f"host={MQTT_HOST}",
        f"port={MQTT_PORT}",
        f"user={'set' if (MQTT_USER and MQTT_PASS) else 'none'}",
        f"cloud_prefix={'<auto>' if not CLOUD_PREFIX else CLOUD_PREFIX}",
        f"topic_prefix={TOPIC_PREFIX}",
        f"discovery_prefix={DISCOVERY_PRE}",
        f"retain={RETAIN_DEFAULT}",
        f"debug={DEBUG}",
        sep=" ", file=sys.stderr, flush=True
    )
except Exception:
    pass

# ===== CRC16-CCITT =====
# [BRIDGE DOC] Compute CRC16-CCITT over bytes, polynomial 0x1021, initial 0xFFFF.
def crc16_ccitt(data: bytes) -> int:
    c = 0xFFFF
    for b in data:
        c ^= (b & 0xFF) << 8
        for _ in range(8):
            if c & 0x8000:
                c = ((c << 1) ^ 0x1021) & 0xFFFF
            else:
                c = (c << 1) & 0xFFFF
    return c & 0xFFFF

# [BRIDGE DOC] Validate frame signature (0x02 0x54), length, and CRC.
def frame_ok(buf: bytes) -> bool:
    if not buf or len(buf) < 8: return False
    if buf[0] != 0x02 or buf[1] != 0x54: return False
    L = (buf[4] << 8) | buf[5]
    T = 6 + L + 2
    if len(buf) != T: return False
    return struct.unpack(">H", buf[-2:])[0] == crc16_ccitt(buf[:-2])

# [BRIDGE DOC] Map numeric frame type to a human-readable label.
def type_name(t: int) -> str:
    return {
        0x52: "system_state",
        0x53: "sensor_state",
        0x43: "counter_state",
        0x4E: "sensor_name",
        0x63: "counter_name",
        0xFB: "ack",
        0xFE: "busy",
        0x57: "reconnect",
    }.get(t, "unknown")

# [BRIDGE DOC] Split NUL-separated CP-1251 bytes into decoded strings.
def split_cp1251_strings(b: bytes):
    parts = b.split(b"\x00")
    if parts and parts[-1] == b"": parts = parts[:-1]
    return [p.decode("cp1251", errors="ignore") for p in parts]

# [BRIDGE DOC] Convert status bitmask to a comma-separated summary (ALARM/BATTERY/etc).
def decode_status_name(s: int) -> str:
    if s == 0: return "NORMAL"
    arr = []
    if s & 0x01: arr.append("ALARM")
    if s & 0x02: arr.append("MAIN BATTERY")
    if s & 0x04: arr.append("SENSOR BATTERY")
    if s & 0x08: arr.append("SENSOR OFFLINE")
    return ",".join(arr)

# [BRIDGE DOC] Decode 4-bit line input config into sensor/counter labels per line.
def map_lines_in(mask: int):
    a = []
    for i in range(4):
        a.append("counter" if ((mask >> i) & 1) else "sensor")
    return {"line_1":a[0],"line_2":a[1],"line_3":a[2],"line_4":a[3]}

# Battery handling across frames (0x52 vs 0x53)
# If the value looks like a voltage-scale (>200), we drop it and do not publish.
def normalize_battery(v):
    try:
        x = int(v)
    except Exception:
        return None
    if x > 200:
        return None
    if x < 0: x = 0
    if x > 100: x = 100
    return x

# Convert 0..4 RSSI bars to percent 0..100
def rssi_bars_to_percent(v):
    try:
        x = int(v)
    except Exception:
        return None
    if x < 0: x = 0
    if x > 4: x = 4
    return x * 25

# Unified builder for Home Assistant device descriptor
def make_device(mac: str):
    """Return (device_dict, safe_mac, dev_id) for consistent HA discovery.

    - safe_mac: MAC sanitized for IDs (':' and '-' replaced with '_', lowercased)
    - dev_id:   primary device identifier used in discovery: 'neptun_<safe_mac>'
    - device:   dict with identifiers and basic metadata used in discovery payloads
    """
    safe_mac = mac.replace(":", "_").replace("-", "_").lower()
    dev_id = f"neptun_{safe_mac}"
    device = {
        "identifiers": [dev_id],
        "manufacturer": "Neptun",
        "model": "AquaControl",
        "name": f"Neptun {safe_mac}"
    }
    return device, safe_mac, dev_id

# ===== PARSERS =====
# [BRIDGE DOC] Parse 0x52 payload: flags, wireless sensors, wired lines, counters, totals.
def parse_system_state(buf: bytes) -> dict:
    p = buf[6:-2]
    out = {}
    i = 0
    while i + 3 <= len(p):
        tag = p[i]; i += 1
        ln = (p[i] << 8) | p[i+1]; i += 2
        v = p[i:i+ln]; i += ln

        if tag == 0x49: # 'I' device type/version or id 
            try: out["device_id"] = v.decode("ascii", "ignore")
            except: pass
        elif tag == 0x4D: # MAC
            out["mac"] = v.decode("ascii", "ignore")
        elif tag == 0x41: # access flag
            out["access"] = (len(v)>0 and v[0]>0)
        elif tag == 0x53: # flags
            if len(v) >= 7:
                out["valve_open"]   = (v[0] == 1)
                out["sensors_count"]= v[1]
                out["relay_count"]  = v[2]
                out["dry_flag"]     = (v[3] == 1)
                out["flag_cl_valve"]= (v[4] == 1)
                out["line_in_cfg"]  = v[5]
                out["status"]       = v[6]
                out["status_name"]  = decode_status_name(v[6])
        elif tag == 0x73: # wireless sensors: [signal,id,batt,leak]*
            arr = []
            for j in range(0, len(v)//4*4, 4):
                signal, sid, batt, leak = v[j], v[j+1], v[j+2], v[j+3]
                arr.append({
                    "line": sid,
                    "sensor_id": sid,
                    "signal_level": signal,
                    "battery_percent": batt,
                    "leak": leak != 0,
                    "leak_state": leak
                })
            out["wireless_sensors"] = arr
        elif tag == 0x4C: # 'L' -> wired leak lines
            # Accept either 4 bytes (one per line) or 1-byte bitmask (low 4 bits)
            try:
                if len(v) >= 4:
                    out["wired_states"] = [bool(v[i]) for i in range(4)]
                elif len(v) >= 1:
                    m = v[0]
                    out["wired_states"] = [bool((m >> i) & 1) for i in range(4)]
            except Exception:
                pass
        elif tag == 0x43: # counters per line: 4B BE + 1B step
            cs = []
            for j in range(0, len(v)//5*5, 5):
                val = (v[j]<<24)|(v[j+1]<<16)|(v[j+2]<<8)|v[j+3]
                cs.append({"value": val & 0xFFFFFFFF, "step": v[j+4]})
            out["counters"] = cs
        elif tag == 0x57: # module RSSI/level
            if len(v): out["W"] = v[0]
        elif tag == 0x44: # device time as ASCII epoch seconds (observed)
            try:
                s = v.decode("ascii", "ignore").strip()
                if s.isdigit():
                    out["device_time_epoch"] = int(s)
            except Exception:
                pass
        else:
            pass
    return out

# [BRIDGE DOC] Parse 0x53 payload into list of wireless sensor states.
def parse_sensor_state(buf: bytes):
    p = buf[6:-2]
    i = 2
    res = []
    while i + 4 <= len(p):
        signal, sid, batt, leak = p[i], p[i+1], p[i+2], p[i+3]
        res.append({
            "line": sid, "sensor_id": sid, "signal_level": signal,
            "battery_percent": batt, "leak": leak != 0, "leak_state": leak
        })
        i += 4
    return res

# ===== MQTT =====
client = mqtt.Client(client_id="neptun-bridge", clean_session=True, userdata=None, protocol=mqtt.MQTTv311)
if MQTT_USER and MQTT_PASS:
    client.username_pw_set(MQTT_USER, MQTT_PASS)

state_cache = {}  # per-MAC: keep last flags for command composing
announced = set() # discovery announced MACs
mac_to_prefix = {}
last_seen = {}        # per-MAC last host timestamp of incoming device frame
last_dev_epoch = {}   # per-MAC last device epoch seen in TLV 0x44
module_lost_timeout = {}  # per-MAC timeout seconds for module_lost (fallback MODULE_LOST_DEFAULT)

# Lightweight retry control for applying settings when device is busy or lags
MAX_RETRIES = 3
RETRY_DELAY_SEC = 2

def log(*a):
    if DEBUG: print("[BRIDGE]", *a, file=sys.stderr)

# Compute a simple icon color name for UI cards that read attributes.
# Note: Home Assistant core does not use this attribute, but custom cards
# (e.g., button-card, some Mushroom templates) can consume it.
def icon_color(kind: str, value) -> str:
    try:
        k = (kind or "").lower()
        if k in ("leak", "problem", "module_lost", "sensors_lost"):
            # value may be on/off, yes/no, 1/0, True/False
            v = str(value).strip().lower()
            is_on = v in ("on", "yes", "1", "true", "problem", "closed")
            return "var(--red-color)" if is_on else "var(--green-color)"
        if k in ("module_alert"):
            # value may be on/off, yes/no, 1/0, True/False
            v = str(value).strip().lower()
            is_on = v in ("on", "yes", "1", "true", "problem", "closed")
            return "var(--orange-color)" if is_on else "var(--green-color)"
        if k == "valve_closed":
            # Here value is valve_open ("1" open/"0" closed). Closed -> red
            v = str(value).strip()
            return "var(--red-color)" if v == "0" else "var(--green-color)"
        if k == "valve_switch":
            # Switch entity for valve: ON/open -> green, OFF/closed -> orange
            v = str(value).strip().lower()
            is_on = v in ("1", "on", "open", "true")
            return "var(--green-color)" if is_on else "var(--orange-color)"
        if k in ("dry_mode", "floor_wash"):
            # Floor Wash switch: ON -> orange, OFF -> green
            v = str(value).strip().lower()
            is_on = v in ("1", "on", "true", "yes")
            return "var(--orange-color)" if is_on else "var(--green-color)"
        if k == "battery_percent":
            x = int(float(value))
            if x < 15: return "var(--red-color)"
            if x < 35: return "var(--orange-color)"
            if x < 60: return "var(--yellow-color)"
            return "var(--green-color)"
        if k == "battery_flag":
            v = str(value).strip().lower()
            return "var(--red-color)" if v in ("yes", "on", "1", "true") else "var(--green-color)"
        if k == "signal":
            x = int(float(value))
            if x <= 25: return "var(--red-color)"
            if x <= 50: return "var(--orange-color)"
            return "var(--green-color)"
        if k == "status_text":
            return "var(--green-color)" if str(value).strip().upper() == "NORMAL" else "var(--orange-color)"
        if k == "counter":
            return "var(--blue-color)"
    except Exception:
        pass
    return "var(--grey-color)"

# Compute a matching MDI icon name for the same kinds used in icon_color().
def icon_name(kind: str, value) -> str:
    try:
        k = (kind or "").lower()
        if k in ("leak",):
            v = str(value).strip().lower()
            is_on = v in ("on", "yes", "1", "true", "problem", "closed") or v == "1"
            return "mdi:water-alert" if is_on else "mdi:water-off"
        if k in ("sensors_lost",):
            v = str(value).strip().lower()
            return "mdi:signal-off" if v in ("yes", "on", "1", "true") else "mdi:signal"
        if k in ("module_lost",):
            v = str(value).strip().lower()
            return "mdi:server-off" if v in ("yes", "on", "1", "true") else "mdi:server"
        if k in ("module_alert",):
            v = str(value).strip().lower()
            return "mdi:alert-circle-outline" if v in ("yes", "on", "1", "true") else "mdi:check-circle-outline"
        if k == "valve_closed":
            # value is valve_open string ("1" open/"0" closed)
            v = str(value).strip()
            return "mdi:water-pump-off" if v == "0" else "mdi:water-pump"
        if k == "battery_flag":
            v = str(value).strip().lower()
            return "mdi:battery-alert" if v in ("yes", "on", "1", "true") else "mdi:battery"
        if k == "signal":
            x = int(float(value))
            if x == 0: return "mdi:signal-cellular-0"
            if x <= 25: return "mdi:signal-cellular-1"
            if x <= 50: return "mdi:signal-cellular-2"
            return "mdi:signal-cellular-3"
        if k == "status_text":
            return "mdi:check-circle-outline" if str(value).strip().upper() == "NORMAL" else "mdi:alert-circle-outline"
    except Exception:
        pass
    return "mdi:help-circle-outline"

# Map signal percent to bucket string off/1/2/3/4
def signal_bucket(pct: int) -> str:
    try:
        x = int(float(pct))
    except Exception:
        return "off"
    if x <= 0:
        return "off"
    if x <= 25:
        return "1"
    if x <= 50:
        return "2"
    if x <= 75:
        return "3"
    return "4"

# [BRIDGE DOC] Publish wrapper: JSON-encode dicts, apply retain default, debug logging.
def pub(topic, payload, retain=None, qos=0):
    if retain is None: retain = RETAIN_DEFAULT
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, ensure_ascii=False)
    try:
        if isinstance(payload, str) and topic.startswith(f"{DISCOVERY_PRE}/sensor/"):
            # quick check: infer by keywords in payload
            if '"unit_of_measurement": "pulse"' in payload:
                return
            if '"unit_of_measurement": "L"' in payload and 'value_template' not in payload:
                return
    except Exception:
        pass
    if DEBUG:
        try:
            plen = (len(payload) if isinstance(payload, (bytes, bytearray)) else len(str(payload)))
        except Exception:
            plen = 0
        print("[BRIDGE]","PUB", topic, f"len={plen}", f"retain={retain}", file=sys.stderr, flush=True)
    client.publish(topic, payload, qos=qos, retain=retain)

# [BRIDGE DOC] Publish raw frames (hex and bytes) to diagnostic topics, grouped by type/name.
def publish_raw(mac, buf: bytes):
    base = f"{TOPIC_PREFIX}/{mac}/raw"
    hexstr = buf.hex()
    b64    = buf.hex() 
    t = buf[3]
    th = f"0x{t:02x}"
    name = type_name(t)
    ts = int(time.time()*1000)

    
    pub(f"{base}/hex", hexstr, retain=False)
    pub(f"{base}/base64",  buf.decode("latin1","ignore"), retain=False)  
    pub(f"{base}/type", th, retain=False)
    pub(f"{base}/len", len(buf), retain=False)

    
    byT = f"{TOPIC_PREFIX}/{mac}/raw/by_type/{th}"
    pub(f"{byT}/hex", hexstr, retain=True)
    pub(f"{byT}/base64", buf.hex(), retain=True)
    pub(f"{byT}/len", len(buf), retain=True)
    pub(f"{byT}/ts", ts, retain=True)
    pub(f"{byT}/name", name, retain=True)

    byN = f"{TOPIC_PREFIX}/{mac}/raw/by_name/{name}"
    for sfx in ("hex","base64","len","ts","type"):
        src = {"hex":hexstr,"base64":buf.hex(),"len":len(buf),"ts":ts,"type":th}[sfx]
        pub(f"{byN}/{sfx}", src, retain=True)

# [BRIDGE DOC] Build 0x57 write-counters frame (TLV 0x43 with 4x [U32 BE value, U8 step]).
def compose_counters_set_frame(lines_vals_steps):
    """Compose a settings frame to write counters for lines 1..4.

    lines_vals_steps: dict { index:int(1..4) -> (value_liters:int, step:int or None) }
    For unspecified lines, use last known raw value/step if available, else zeros.
    """
    # Seed with last known raw from cache if available
    lines = []  # list of tuples (val, step)
    for i in range(1,5):
        lines.append((0, 0))
    # Try to fill from cache
    try:
        # We pass mac via closure by reading the most recent from state_cache within caller; here keep zeros.
        pass
    except Exception:
        pass
    # Caller will replace with proper defaults before calling this if needed.

    # Override with provided updates
    for idx, tup in lines_vals_steps.items():
        if not (1 <= idx <= 4):
            continue
        val = int(max(0, int(tup[0])))
        stp = int(tup[1]) if (len(tup) > 1 and tup[1] is not None) else 0
        lines[idx-1] = (val, stp if 0 <= stp <= 255 else 0)

    # Build TLV payload 0x43 of 20 bytes (4 lines * 5 bytes)
    tlv = bytearray()
    for (val, stp) in lines:
        tlv += bytes([(val >> 24) & 0xFF, (val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF, stp & 0xFF])

    # Frame: 02 54 51 57 00 17 43 00 14 <tlv> CRC
    body = bytearray([0x02,0x54,0x51,0x57, 0x00,0x17, 0x43,0x00,0x14])
    body += tlv
    crc = crc16_ccitt(body)
    return bytes(body + struct.pack(">H", crc))

# [BRIDGE DOC] Build 0x57 set-time frame (TLV 0x44 with ASCII local datetime "DD/MM/YYYY,HH:MM:SS").
def compose_time_set_frame(epoch_seconds: int):
    """Compose a settings frame to set device time.

    Format per observed write frame: TLV 0x44 carrying ASCII local datetime
    string "DD/MM/YYYY,HH:MM:SS" (e.g. 08/09/2025,23:31:18).

    Returns bytes ready to publish to <cloud_prefix>/<MAC>/to.
    """
    try:
        if epoch_seconds < 0:
            epoch_seconds = 0
    except Exception:
        epoch_seconds = 0
    try:
        lt = time.localtime(int(epoch_seconds))
        s = f"{lt.tm_mday:02d}/{lt.tm_mon:02d}/{lt.tm_year:04d},{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
    except Exception:
        # Fallback to current local time if formatting fails
        lt = time.localtime()
        s = f"{lt.tm_mday:02d}/{lt.tm_mon:02d}/{lt.tm_year:04d},{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
    b = s.encode("ascii", errors="ignore")
    L = 3 + len(b)
    body = bytearray([0x02,0x54,0x51,0x57, (L >> 8) & 0xFF, L & 0xFF, 0x44, (len(b) >> 8) & 0xFF, len(b) & 0xFF])
    body += b
    crc = crc16_ccitt(body)
    return bytes(body + struct.pack(">H", crc))

# [BRIDGE DOC] Emit Home Assistant discovery for one device MAC (id-sanitized).
def ensure_discovery(mac):
    """
    Publish Home Assistant discovery configs for a single Neptun device.

    Purpose:
    - Define entities (buttons, switches, selects, sensors) so HA can create
      and link them to one device before state starts flowing.
    - Use a stable device identifier `neptun_<safe_mac>` to avoid splitting
      entities across multiple HA devices.
    - Idempotent: runs only once per MAC and returns early if already done.

    Notes:
    - Discovery topics go under `<DISCOVERY_PRE>/*/neptun_<safe_mac>_.../config`.
    - This function does NOT publish live state values; see publish_system() and
      publish_sensor_state() for runtime updates.
    """
    if mac in announced:
        return
    
    device, safe_mac, dev_id = make_device(mac)
    
    # Switch entity for valve control (replaces two stateless buttons)
    sw_valve_id = f"neptun_{safe_mac}_valve"
    sw_valve_conf = {
        "name": f"Valve",
        "unique_id": sw_valve_id,
        "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/valve/set",
        "state_topic": f"{TOPIC_PREFIX}/{mac}/state/valve_open",
        "payload_on": "1",
        "payload_off": "0",
        "icon": "mdi:valve",
        "qos": 0,
        "retain": True,
        "json_attributes_topic": f"{TOPIC_PREFIX}/{mac}/attributes/valve_switch",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/switch/{sw_valve_id}/config", sw_valve_conf, retain=True)
    # Publish empty payloads to old button discovery topics to remove them (cleanup)
    try:
        btn_open_id = f"neptun_{safe_mac}_valve_open"
        btn_close_id = f"neptun_{safe_mac}_valve_close"
        client.publish(f"{DISCOVERY_PRE}/button/{btn_open_id}/config", b"", retain=True)
        client.publish(f"{DISCOVERY_PRE}/button/{btn_close_id}/config", b"", retain=True)
    except Exception:
        pass

    # Floor Wash entities removed: use Dry Flag switch under Controls

    # Add MQTT switch for Floor Wash (dry mode control)
    obj_id2 = f"neptun_{safe_mac}_dry_flag"
    conf2 = {
        "name": f"Floor Wash",
        "unique_id": obj_id2,
        "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/dry_flag/set",
        "state_topic": f"{TOPIC_PREFIX}/{mac}/settings/dry_flag",
        "payload_on": "on",
        "payload_off": "off",
        "qos": 0,
        "retain": True,
        "icon": "mdi:water-circle",
        "json_attributes_topic": f"{TOPIC_PREFIX}/{mac}/attributes/dry_flag",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/switch/{obj_id2}/config", conf2, retain=True)

    # Add MQTT switch for Close On Offline
    obj_id3 = f"neptun_{safe_mac}_close_on_offline"
    conf3 = {
        "name": f"Close On Offline",
        "unique_id": obj_id3,
        "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/close_on_offline/set",
        "state_topic": f"{TOPIC_PREFIX}/{mac}/settings/close_valve_flag",
        "payload_on": "close",
        "payload_off": "open",
        "qos": 0,
        "retain": True,
        "icon": "mdi:water-alert-outline",
        "entity_category": "config",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/switch/{obj_id3}/config", conf3, retain=True)

    # Add MQTT selects for wired line types (sensor/counter)
    for i in range(1,5):
        sel_id = f"neptun_{safe_mac}_line_{i}_type"
        sel_conf = {
            "name": f"Line {i} Type",
            "unique_id": sel_id,
            "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/line_{i}_type/set",
            "state_topic": f"{TOPIC_PREFIX}/{mac}/settings/lines_in/line_{i}",
            "options": ["sensor", "counter"],
            "icon": "mdi:tune",
            "entity_category": "config",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/select/{sel_id}/config", sel_conf, retain=True)

    for i in range(1,5):
        # Derived: cubic meters from liters via value_template
        sidM = f"neptun_{safe_mac}_line_{i}_counter"
        confM = {
            "name": f"Line {i} Counter",
            "unique_id": sidM,
            "state_topic": f"{TOPIC_PREFIX}/{mac}/counters/line_{i}/value",
            "device_class": "water",
            "unit_of_measurement": "m\u00B3",
            "state_class": "total",
            "value_template": "{{ value | float / 1000 }}",
            "icon": "mdi:counter",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/sensor/{sidM}/config", confM, retain=True)

        # Step in liters per pulse (L/pulse)
        sidS = f"neptun_{safe_mac}_line_{i}_step"
        confS = {
            "name": f"Line {i} Counter Step",
            "unique_id": sidS,
            "state_topic": f"{TOPIC_PREFIX}/{mac}/counters/line_{i}/step",
            "unit_of_measurement": "L/pulse",
            "icon": "mdi:counter",
            "entity_category": "diagnostic",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/sensor/{sidS}/config", confS, retain=True)

        # Number entity to SET counter value in liters
        numV_id = f"neptun_{safe_mac}_line_{i}_counter_set"
        numV_conf = {
            "name": f"Line {i} Counter (set)",
            "unique_id": numV_id,
            "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/counters/line_{i}/value/set",
            "state_topic": f"{TOPIC_PREFIX}/{mac}/counters/line_{i}/value",
            "unit_of_measurement": "L",
            "icon": "mdi:counter",
            "min": 0,
            "max": 1000000000,
            "step": 1,
            "mode": "box",
            "entity_category": "config",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/number/{numV_id}/config", numV_conf, retain=True)

        # Number entity to SET counter step in L/pulse
        numS_id = f"neptun_{safe_mac}_line_{i}_step_set"
        numS_conf = {
            "name": f"Line {i} Step (set)",
            "unique_id": numS_id,
            "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/counters/line_{i}/step/set",
            "state_topic": f"{TOPIC_PREFIX}/{mac}/counters/line_{i}/step",
            "unit_of_measurement": "L/pulse",
            "icon": "mdi:counter",
            "min": 1,
            "max": 255,
            "step": 1,
            "mode": "box",
            "entity_category": "config",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/number/{numS_id}/config", numS_conf, retain=True)

    # Device time (timestamp sensor)
    dt_id = f"neptun_{safe_mac}_last_seen"
    dt_conf = {
        "name": f"Last Seen",
        "unique_id": dt_id,
        "state_topic": f"{TOPIC_PREFIX}/{mac}/device_time",
        "device_class": "timestamp",
        "entity_category": "diagnostic",
        "entity_registry_enabled_default": False,
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/sensor/{dt_id}/config", dt_conf, retain=True)

    # Frame interval sensor (seconds)
    drift_id = f"neptun_{safe_mac}_frame_interval"
    drift_conf = {
        "name": f"Frame Interval",
        "unique_id": drift_id,
        "state_topic": f"{TOPIC_PREFIX}/{mac}/frame_interval_seconds",
        "unit_of_measurement": "s",
        "icon": "mdi:timer-outline",
        "entity_category": "diagnostic",
        "entity_registry_enabled_default": False,
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/sensor/{drift_id}/config", drift_conf, retain=True)

    # Module RSSI (0..4 bars) as reported by device (TLV 0x57 -> st["W"]) 
    rssi_id = f"neptun_{safe_mac}_module_rssi"
    rssi_conf = {
        "name": f"Module RSSI",
        "unique_id": rssi_id,
        "state_topic": f"{TOPIC_PREFIX}/{mac}/signal_level",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        #"icon": "mdi:wifi",
        "entity_category": "diagnostic",
        "json_attributes_topic": f"{TOPIC_PREFIX}/{mac}/attributes/module_rssi",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/sensor/{rssi_id}/config", rssi_conf, retain=True)

    # Button to set device time to current host time
    btn_id = f"neptun_{safe_mac}_set_time_now"
    btn_conf = {
        "name": f"Set Device Time",
        "unique_id": btn_id,
        "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/time/set",
        "payload_press": "now",
        "icon": "mdi:clock-check",
        "entity_category": "config",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/button/{btn_id}/config", btn_conf, retain=True)

    # Wired leak sensors (lines 1..4)
    for i in range(1,5):
        wired_id = f"neptun_{safe_mac}_line_{i}_leak"
        wired_conf = {
            "name": f"Line {i} Leak",
            "unique_id": wired_id,
            "state_topic": f"{TOPIC_PREFIX}/{mac}/lines_status/line_{i}",
            "payload_on": "on",
            "payload_off": "off",
            "device_class": "moisture",
            "json_attributes_topic": f"{TOPIC_PREFIX}/{mac}/lines_status/line_{i}/attributes",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/binary_sensor/{wired_id}/config", wired_conf, retain=True)

    # Line input type sensors (sensor/counter)
    for i in range(1,5):
        t_id = f"neptun_{safe_mac}_line_{i}_type"
        t_conf = {
            "name": f"Line {i} Type",
            "unique_id": t_id,
            "state_topic": f"{TOPIC_PREFIX}/{mac}/settings/lines_in/line_{i}",
            "icon": "mdi:label",
            "entity_category": "diagnostic",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/sensor/{t_id}/config", t_conf, retain=True)

    base_topic = f"{TOPIC_PREFIX}/{mac}"

    # Overall leak detected
    leak_id = f"neptun_{safe_mac}_leak_detected"
    leak_conf = {
        "name": f"Leak Detected",
        "unique_id": leak_id,
        "state_topic": f"{base_topic}/settings/status/alert",
        "payload_on": "on",
        "payload_off": "off",
        "device_class": "moisture",
        "json_attributes_topic": f"{base_topic}/attributes/leak_detected",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{leak_id}/config", leak_conf, retain=True)

    # Module Status (text)
    mod_status_id = f"neptun_{safe_mac}_module_status"
    mod_status_conf = {
        "name": f"Module Status",
        "unique_id": mod_status_id,
        "state_topic": f"{base_topic}/state/status_name",
        "icon": "mdi:alert-circle-outline",
        "json_attributes_topic": f"{base_topic}/attributes/module_status",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/sensor/{mod_status_id}/config", mod_status_conf, retain=True)

    # Module Alert (any problem bit)
    mod_alert_id = f"neptun_{safe_mac}_module_alert"
    mod_alert_conf = {
        "name": f"Module Alert",
        "unique_id": mod_alert_id,
        "state_topic": f"{base_topic}/settings/status/module_alert",
        "payload_on": "yes",
        "payload_off": "no",
        "device_class": "problem",
        "json_attributes_topic": f"{base_topic}/attributes/module_alert",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{mod_alert_id}/config", mod_alert_conf, retain=True)

    # Valve State
    valve_closed_id = f"neptun_{safe_mac}_valve_closed"
    valve_closed_conf = {
        "name": f"Valve Closed",
        "unique_id": valve_closed_id,
        "state_topic": f"{base_topic}/state/valve_open",
        "payload_on": "0",
        "payload_off": "1",
        "device_class": "problem",
        "icon": "mdi:water-pump-off",
        "json_attributes_topic": f"{base_topic}/attributes/valve_closed",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{valve_closed_id}/config", valve_closed_conf, retain=True)

    # Module Battery 
    mod_batt_id = f"neptun_{safe_mac}_module_battery"
    mod_batt_conf = {
        "name": f"Module Battery",
        "unique_id": mod_batt_id,
        "state_topic": f"{base_topic}/settings/status/battery_discharge_in_module",
        "payload_on": "yes",
        "payload_off": "no",
        "device_class": "battery",
        "icon": "mdi:car-battery",
        "json_attributes_topic": f"{base_topic}/attributes/module_battery",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{mod_batt_id}/config", mod_batt_conf, retain=True)

    # Sensors Battery 
    sens_batt_id = f"neptun_{safe_mac}_sensors_battery"
    sens_batt_conf = {
        "name": f"Sensors Battery",
        "unique_id": sens_batt_id,
        "state_topic": f"{base_topic}/settings/status/battery_discharge_in_sensor",
        "payload_on": "yes",
        "payload_off": "no",
        "device_class": "battery",
        "json_attributes_topic": f"{base_topic}/attributes/sensors_battery",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{sens_batt_id}/config", sens_batt_conf, retain=True)

    # Sensors Lost
    sens_lost_id = f"neptun_{safe_mac}_sensors_lost"
    sens_lost_conf = {
        "name": f"Sensors Lost",
        "unique_id": sens_lost_id,
        "state_topic": f"{base_topic}/settings/status/sensors_lost",
        "payload_on": "yes",
        "payload_off": "no",
        "device_class": "problem",
        "icon": "mdi:signal-off",
        "json_attributes_topic": f"{base_topic}/attributes/sensors_lost",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{sens_lost_id}/config", sens_lost_conf, retain=True)

    # Module Lost (no data received > timeout), locally computed
    mod_lost_id = f"neptun_{safe_mac}_module_lost"
    mod_lost_conf = {
        "name": f"Module Lost",
        "unique_id": mod_lost_id,
        "state_topic": f"{base_topic}/settings/status/module_lost",
        "payload_on": "yes",
        "payload_off": "no",
        "device_class": "problem",
        "icon": "mdi:server-off",
        "json_attributes_topic": f"{base_topic}/attributes/module_lost",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{mod_lost_id}/config", mod_lost_conf, retain=True)

    # Number entity to configure Module Lost timeout (seconds)
    mlt_id = f"neptun_{safe_mac}_module_lost_timeout"
    mlt_conf = {
        "name": f"Module Lost Timeout",
        "unique_id": mlt_id,
        "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/module_lost_timeout/set",
        "state_topic": f"{TOPIC_PREFIX}/{mac}/settings/module_lost_timeout",
        "unit_of_measurement": "s",
        "icon": "mdi:timer-alert-outline",
        "min": 10,
        "max": 3600,
        "step": 1,
        "mode": "box",
        "entity_category": "config",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/number/{mlt_id}/config", mlt_conf, retain=True)

    # Publish current timeout value
    try:
        cur_to = int(module_lost_timeout.get(mac, MODULE_LOST_DEFAULT))
    except Exception:
        cur_to = MODULE_LOST_DEFAULT
    pub(f"{TOPIC_PREFIX}/{mac}/settings/module_lost_timeout", cur_to, retain=True)

    announced.add(mac)

# [BRIDGE DOC] Handle 0x52: update caches, publish states/settings/counters and discovery.
def publish_system(mac_from_topic, buf: bytes):
    """
    Handle a 0x52 "system_state" frame and publish runtime data.

    Responsibilities:
    - Parse the binary frame (flags, counters, wireless/wired sensors, lines).
    - Ensure HA discovery has been published once (calls ensure_discovery()).
    - Update `state_cache` to preserve flags used when composing commands.
    - Publish structured MQTT topics under `neptun/<mac>/*` for HA to consume.

    This function focuses on live state (non-discovery). It is called for every
    valid 0x52 frame received from the Neptun cloud.
    """
    st = parse_system_state(buf)
    mac = st.get("mac", mac_from_topic)
    base = f"{TOPIC_PREFIX}/{mac}"
    device, safe_mac, dev_id = make_device(mac)

    publish_raw(mac, buf)
    ensure_discovery(mac)
    try:
        last_seen[mac] = time.time()
    except Exception:
        pass
    # Track device-provided time if present (no extrapolation)
    try:
        if "device_time_epoch" in st:
            last_dev_epoch[mac] = int(st.get("device_time_epoch"))
    except Exception:
        pass
    
    prev = state_cache.get(mac, {})
    prev.update({
        "valve_open": bool(st.get("valve_open", False)),
        "dry_flag": bool(st.get("dry_flag", False)),
        "flag_cl_valve": bool(st.get("flag_cl_valve", False)),
        "line_in_cfg": int(st.get("line_in_cfg", 0))
    })
    state_cache[mac] = prev
    
    sensors_status = []
    for s in st.get("wireless_sensors", []):
        nb = normalize_battery(s.get("battery_percent", 0))
        entry = {
            "line": s["sensor_id"],
            "attention": 1 if s["leak"] else 0,
            "signal_level": rssi_bars_to_percent(s.get("signal_level", 0)) or 0
        }
        if nb is not None:
            entry["battery"] = nb
            pub(f"{base}/sensors_status/{s['sensor_id']}/battery", nb, retain=False)
            try:
                pub(
                    f"{base}/sensors_status/{s['sensor_id']}/attributes/battery",
                    {"icon_color": icon_color("battery_percent", nb)},
                    retain=False,
                )
            except Exception:
                pass
        sensors_status.append(entry)
        sigp = rssi_bars_to_percent(s.get("signal_level", 0)) or 0
        pub(f"{base}/sensors_status/{s['sensor_id']}/signal_level", sigp, retain=False)
        try:
            pub(
                f"{base}/sensors_status/{s['sensor_id']}/attributes/signal",
                {"icon_color": icon_color("signal", sigp), "icon": icon_name("signal", sigp)},
                retain=False,
            )
        except Exception:
            pass
        att = 1 if s["leak"] else 0
        pub(f"{base}/sensors_status/{s['sensor_id']}/attention", att, retain=False)
        try:
            pub(
                f"{base}/sensors_status/{s['sensor_id']}/attributes/leak",
                {"icon_color": icon_color("leak", att), "icon": icon_name("leak", att)},
                retain=False,
            )
        except Exception:
            pass
             
        obj_id = f"neptun_{safe_mac}_sensor_{s['sensor_id']}_leak"
        conf = {
            "name": f"Sensor {s['sensor_id']} Leak",
            "unique_id": obj_id,
            "state_topic": f"{TOPIC_PREFIX}/{mac}/sensors_status/{s['sensor_id']}/attention",
            "payload_on": "1", "payload_off": "0",
            "device_class": "moisture",
            "json_attributes_topic": f"{TOPIC_PREFIX}/{mac}/sensors_status/{s['sensor_id']}/attributes/leak",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/binary_sensor/{obj_id}/config", conf, retain=True)

        obj_id = f"neptun_{safe_mac}_sensor_{s['sensor_id']}_battery"
        conf = {
            "name": f"Sensor {s['sensor_id']} Battery",
            "unique_id": obj_id,
            "state_topic": f"{TOPIC_PREFIX}/{mac}/sensors_status/{s['sensor_id']}/battery",
            "unit_of_measurement": "%",
            "device_class": "battery",
            "json_attributes_topic": f"{TOPIC_PREFIX}/{mac}/sensors_status/{s['sensor_id']}/attributes/battery",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/sensor/{obj_id}/config", conf, retain=True)

        obj_id = f"neptun_{safe_mac}_sensor_{s['sensor_id']}_signal_level"
        # Republish discovery only when RSSI bucket changes
        try:
            sb = signal_bucket(sigp)
        except Exception:
            sb = None
        try:
            prev_cache = state_cache.get(mac, {})
            by_id = prev_cache.get("sensor_rssi_bucket") or {}
            last_sb = by_id.get(int(s['sensor_id']))
            if sb != last_sb:
                conf = {
                    "name": f"Sensor {s['sensor_id']} RSSI",
                    "unique_id": obj_id,
                    "state_topic": f"{TOPIC_PREFIX}/{mac}/sensors_status/{s['sensor_id']}/signal_level",
                    "unit_of_measurement": "%",
                    "icon": icon_name("signal", sigp),
                    "entity_category": "diagnostic",
                    "json_attributes_topic": f"{TOPIC_PREFIX}/{mac}/sensors_status/{s['sensor_id']}/attributes/signal",
                    "device": device
                }
                pub(f"{DISCOVERY_PRE}/sensor/{obj_id}/config", conf, retain=True)
                by_id[int(s['sensor_id'])] = sb
                prev_cache["sensor_rssi_bucket"] = by_id
                state_cache[mac] = prev_cache
        except Exception:
            pass

    if sensors_status:
        pub(f"{base}/sensors_status/json", sensors_status, retain=False)

    # settings (retained)
    settings = {
        "status": {
            "alert": "on" if (st.get("status",0)&0x01) else "off",
            "dry_flag": "yes" if st.get("dry_flag") else "no",
            "sensors_lost": "yes" if (st.get("status",0)&0x08) else "no",
            "battery_discharge_in_module": "yes" if (st.get("status",0)&0x02) else "no",
            "battery_discharge_in_sensor": "yes" if (st.get("status",0)&0x04) else "no",
            "module_alert": "yes" if (st.get("status",0) != 0) else "no",
        },
        "dry_flag": "on" if st.get("dry_flag") else "off",
        "lines_in": map_lines_in(st.get("line_in_cfg",0)),
        "relay_count": st.get("relay_count",0),
        "sensors_count": st.get("sensors_count",0),
        "valve_settings": "opened" if st.get("valve_open") else "closed",
        "close_valve_flag": "close" if st.get("flag_cl_valve") else "open",
    }
    
    pub(f"{base}/settings/status/alert", settings["status"]["alert"], retain=True)
    try:
        pub(
            f"{base}/attributes/leak_detected",
            {"icon_color": icon_color("leak", settings["status"]["alert"]), "icon": icon_name("leak", settings["status"]["alert"])},
            retain=False,
        )
    except Exception:
        pass
    pub(f"{base}/settings/status/dry_flag", settings["status"]["dry_flag"], retain=True)
    pub(f"{base}/settings/status/sensors_lost", settings["status"]["sensors_lost"], retain=True)
    try:
        pub(
            f"{base}/attributes/sensors_lost",
            {"icon_color": icon_color("sensors_lost", settings["status"]["sensors_lost"]), "icon": icon_name("sensors_lost", settings["status"]["sensors_lost"])},
            retain=False,
        )
    except Exception:
        pass
    # Update discovery icon when sensors_lost bucket changes
    try:
        bucket = str(settings["status"]["sensors_lost"]).strip().lower()
        prev_cache = state_cache.get(mac, {})
        last_b = prev_cache.get("sensors_lost_bucket")
        if bucket != last_b:
            device, safe_mac, dev_id = make_device(mac)
            sens_lost_id = f"neptun_{safe_mac}_sensors_lost"
            sens_lost_conf = {
                "name": f"Sensors Lost",
                "unique_id": sens_lost_id,
                "state_topic": f"{base}/settings/status/sensors_lost",
                "payload_on": "yes",
                "payload_off": "no",
                "device_class": "problem",
                "icon": icon_name("sensors_lost", bucket),
                "json_attributes_topic": f"{base}/attributes/sensors_lost",
                "device": device
            }
            pub(f"{DISCOVERY_PRE}/binary_sensor/{sens_lost_id}/config", sens_lost_conf, retain=True)
            prev_cache["sensors_lost_bucket"] = bucket
            state_cache[mac] = prev_cache
    except Exception:
        pass
    pub(f"{base}/settings/status/battery_discharge_in_module", settings["status"]["battery_discharge_in_module"], retain=True)
    try:
        pub(
            f"{base}/attributes/module_battery",
            {"icon_color": icon_color("battery_flag", settings["status"]["battery_discharge_in_module"]), "icon": icon_name("battery_flag", settings["status"]["battery_discharge_in_module"])},
            retain=False,
        )
    except Exception:
        pass
    pub(f"{base}/settings/status/battery_discharge_in_sensor", settings["status"]["battery_discharge_in_sensor"], retain=True)
    try:
        pub(
            f"{base}/attributes/sensors_battery",
            {"icon_color": icon_color("battery_flag", settings["status"]["battery_discharge_in_sensor"]), "icon": icon_name("battery_flag", settings["status"]["battery_discharge_in_sensor"])},
            retain=False,
        )
    except Exception:
        pass
    pub(f"{base}/settings/status/module_alert", settings["status"]["module_alert"], retain=True)
    try:
        pub(
            f"{base}/attributes/module_alert",
            {"icon_color": icon_color("module_alert", settings["status"]["module_alert"]), "icon": icon_name("module_alert", settings["status"]["module_alert"])},
            retain=False,
        )
    except Exception:
        pass
    pub(f"{base}/settings/dry_flag", settings["dry_flag"], retain=True)
    try:
        pub(
            f"{base}/attributes/dry_flag",
            {"icon_color": icon_color("floor_wash", settings["dry_flag"])},
            retain=False,
        )
    except Exception:
        pass
    pub(f"{base}/settings/relay_count", settings["relay_count"], retain=True)
    pub(f"{base}/settings/sensors_count", settings["sensors_count"], retain=True)
    pub(f"{base}/settings/valve_settings", settings["valve_settings"], retain=True)
    pub(f"{base}/settings/close_valve_flag", settings["close_valve_flag"], retain=True)

    li = settings["lines_in"]
    for k in ("line_1","line_2","line_3","line_4"):
        pub(f"{base}/settings/lines_in/{k}", li[k], retain=True)
    
    if "device_id" in st: pub(f"{base}/device_id", st["device_id"], retain=True)
    if "W" in st:
        try:
            w = rssi_bars_to_percent(st.get("W", 0)) or 0
        except Exception:
            w = 0
        pub(f"{base}/signal_level", w, retain=False)
        try:
            pub(
                f"{base}/attributes/module_rssi",
                {"icon_color": icon_color("signal", w), "icon": icon_name("signal", w)},
                retain=False,
            )
        except Exception:
            pass
        # Only update discovery icon if the RSSI bucket changed (off/1/2/3/4)
        try:
            bucket = signal_bucket(w)
            prev_cache = state_cache.get(mac, {})
            last_b = prev_cache.get("module_rssi_bucket")
            if bucket != last_b:
                device, safe_mac, dev_id = make_device(mac)
                rssi_id = f"neptun_{safe_mac}_module_rssi"
                rssi_conf = {
                    "name": f"Module RSSI",
                    "unique_id": rssi_id,
                    "state_topic": f"{TOPIC_PREFIX}/{mac}/signal_level",
                    "unit_of_measurement": "%",
                    "state_class": "measurement",
                    "icon": icon_name("signal", w),
                    "entity_category": "diagnostic",
                    "json_attributes_topic": f"{TOPIC_PREFIX}/{mac}/attributes/module_rssi",
                    "device": device
                }
                pub(f"{DISCOVERY_PRE}/sensor/{rssi_id}/config", rssi_conf, retain=True)
                prev_cache["module_rssi_bucket"] = bucket
                state_cache[mac] = prev_cache
        except Exception:
            pass
    # Device time (if provided): publish epoch and local ISO8601 with offset
    try:
        if "device_time_epoch" in st:
            ts = int(st["device_time_epoch"])
            # Device reports epoch-like SECONDS IN LOCAL CLOCK (not UTC).
            # To get correct local wall time, subtract current local UTC offset once,
            # then format as local ISO (to avoid applying offset twice).
            try:
                off = datetime.now().astimezone().utcoffset()
                off_s = int(off.total_seconds()) if off else 0
            except Exception:
                off_s = 0
            try:
                iso = datetime.fromtimestamp(ts - off_s).astimezone().isoformat()
            except Exception:
                iso = str(ts)
            pub(f"{base}/device_time_epoch", ts, retain=True)
            pub(f"{base}/device_time", iso, retain=True)
    except Exception:
        pass

    # Publish current Module Lost timeout setting (retained)
    try:
        cur_to = int(module_lost_timeout.get(mac, MODULE_LOST_DEFAULT))
    except Exception:
        cur_to = MODULE_LOST_DEFAULT
    pub(f"{base}/settings/module_lost_timeout", cur_to, retain=True)

    pub(f"{base}/mac_address", mac, retain=True)
    
    parsedCfg = {
        "settings": settings,
        "device_id": st.get("device_id",""),
        "mac_address": mac,
        "signal_level": st.get("W"),
        "access_status": "available" if st.get("access") else "restricted"
    }
    if sensors_status: parsedCfg["sensors_status"] = sensors_status
    pub(f"{base}/config/json", parsedCfg, retain=True)

    # Cache last raw counters for potential write frames and publish
    last_raw = []
    for idx, c in enumerate(st.get("counters", []), start=1):
        val = int(c.get("value",0)); step = int(c.get("step",1)) or 1
        last_raw.append((val, step))
        pub(f"{base}/counters/line_{idx}/value", val, retain=False)
        pub(f"{base}/counters/line_{idx}/step", step, retain=False)
    try:
        prev_cache = state_cache.get(mac, {})
        prev_cache["counters_last"] = last_raw
        state_cache[mac] = prev_cache
    except Exception:
        pass

    # Publish wired-only lines leak status (on/off)
    # Conditions to publish 'on' for line i:
    #  - line i is configured as sensor (not counter) in line_in_cfg
    #  - wired zone state for line i is active in TLV 0x4C
    #  - no wireless sensor with id == i+1 reports leak in TLV 0x73
    wired = st.get("wired_states", [])
    line_cfg = int(st.get("line_in_cfg", 0)) & 0x0F
    wireless_leaks = set()
    try:
        for ws in st.get("wireless_sensors", []) or []:
            try:
                if ws.get("leak"):
                    wireless_leaks.add(int(ws.get("sensor_id", 0)))
            except Exception:
                continue
    except Exception:
        pass
    if wired:
        for i in range(4):
            is_sensor_line = ((line_cfg >> i) & 1) == 0
            wired_active = (i < len(wired) and bool(wired[i]))
            wireless_same_zone = ((i+1) in wireless_leaks)
            stv = "on" if (is_sensor_line and wired_active and not wireless_same_zone) else "off"
            pub(f"{base}/lines_status/line_{i+1}", stv, retain=False)
            try:
                pub(
                    f"{base}/lines_status/line_{i+1}/attributes",
                    {"icon_color": icon_color("leak", stv), "icon": icon_name("leak", stv)},
                    retain=False,
                )
            except Exception:
                pass

    # Do not publish duplicate line input types under base/lines_in/*.
    # Kept only settings/lines_in/{k} publishes above for a single source of truth.

    # state/*
    pub(f"{base}/state/json", st, retain=False)
    if "valve_open" in st:
        v = "1" if st["valve_open"] else "0"
        pub(f"{base}/state/valve_open", v, retain=False)
        try:
            pub(
                f"{base}/attributes/valve_closed",
                {"icon_color": icon_color("valve_closed", v), "icon": icon_name("valve_closed", v)},
                retain=False,
            )
        except Exception:
            pass
        try:
            pub(
                f"{base}/attributes/valve_switch",
                {"icon_color": icon_color("valve_switch", v)},
                retain=False,
            )
        except Exception:
            pass
        # Republish discovery for valve_closed only when state bucket changes (open/closed)
        try:
            bucket = ("open" if v == "1" else "closed")
            prev_cache = state_cache.get(mac, {})
            last_b = prev_cache.get("valve_state_bucket")
            if bucket != last_b:
                valve_closed_id = f"neptun_{safe_mac}_valve_closed"
                valve_closed_conf = {
                    "name": f"Valve Closed",
                    "unique_id": valve_closed_id,
                    "state_topic": f"{base}/state/valve_open",
                    "payload_on": "0",
                    "payload_off": "1",
                    "device_class": "problem",
                    "icon": icon_name("valve_closed", v),
                    "json_attributes_topic": f"{base}/attributes/valve_closed",
                    "device": device
                }
                pub(f"{DISCOVERY_PRE}/binary_sensor/{valve_closed_id}/config", valve_closed_conf, retain=True)
                prev_cache["valve_state_bucket"] = bucket
                state_cache[mac] = prev_cache
        except Exception:
            pass
    if "status" in st: pub(f"{base}/state/status", st["status"], retain=False)
    if "status_name" in st and st["status_name"]:
        pub(f"{base}/state/status_name", st["status_name"], retain=False)
        try:
            pub(
                f"{base}/attributes/module_status",
                {"icon_color": icon_color("status_text", st["status_name"]), "icon": icon_name("status_text", st["status_name"])},
                retain=False,
            )
        except Exception:
            pass

# [BRIDGE DOC] Handle 0x53: publish per-sensor battery, signal and attention flag.
def publish_sensor_state(mac_from_topic, buf: bytes):
    """
    Handle a 0x53 "sensor_state" frame and publish per-sensor telemetry.

    Publishes for each wireless sensor:
    - Battery percentage, signal level (LQI), and attention (leak) flags
    under `neptun/<mac>/sensors_status/<id>/*`.

    Discovery is not re-sent here; entity definitions are created in
    ensure_discovery(). This function emits only current values.
    """
    sensors = parse_sensor_state(buf)
    mac = mac_from_topic
    base = f"{TOPIC_PREFIX}/{mac}"
    publish_raw(mac, buf)
    try:
        last_seen[mac] = time.time()
    except Exception:
        pass
    if sensors:
        slim = []
        for s in sensors:
            nb = normalize_battery(s.get("battery_percent", 0))
            e = {"line": s["sensor_id"], "attention": 1 if s["leak"] else 0, "signal_level": rssi_bars_to_percent(s.get("signal_level", 0)) or 0}
            if nb is not None:
                e["battery"] = nb
                pub(f"{base}/sensors_status/{s['sensor_id']}/battery", nb, retain=False)
                try:
                    pub(
                        f"{base}/sensors_status/{s['sensor_id']}/attributes/battery",
                        {"icon_color": icon_color("battery_percent", nb)},
                        retain=False,
                    )
                except Exception:
                    pass
            sigp = rssi_bars_to_percent(s.get("signal_level", 0)) or 0
            pub(f"{base}/sensors_status/{s['sensor_id']}/signal_level", sigp, retain=False)
            try:
                pub(
                    f"{base}/sensors_status/{s['sensor_id']}/attributes/signal",
                    {"icon_color": icon_color("signal", sigp), "icon": icon_name("signal", sigp)},
                    retain=False,
                )
            except Exception:
                pass
            att = 1 if s["leak"] else 0
            pub(f"{base}/sensors_status/{s['sensor_id']}/attention", att, retain=False)
            try:
                pub(
                    f"{base}/sensors_status/{s['sensor_id']}/attributes/leak",
                    {"icon_color": icon_color("leak", att), "icon": icon_name("leak", att)},
                    retain=False,
                )
            except Exception:
                pass
            slim.append(e)
        pub(f"{base}/sensors_status/json", slim, retain=False)

# [BRIDGE DOC] Build 0x57 settings frame (valve/dry/close_on_offline/line_cfg) with CRC.
def compose_settings_frame(open_valve: bool, dry=False, close_on_offline=False, line_cfg=0):
    """Build a Neptun settings command frame (type 0x57).

    Parameters:
      - open_valve: True to open, False to close the valve.
      - dry: Dry mode flag (mirrors settings/dry_flag).
      - close_on_offline: Close valve when sensors/offline (flag_cl_valve).
      - line_cfg: 4-bit mask for line inputs (bit0..bit3 -> lines 1..4).
                  1 = counter, 0 = sensor; value range 0..15.

    Returns:
      - bytes ready to publish to <cloud_prefix>/<MAC>/to.

    Frame layout:
      02 54 51 57 00 07 53 00 04 VV DF CF LC CRC  (CRC16-CCITT, big-endian).
    """
    # 02 54 51 57 00 07 53 00 04 VV DF CF LC CRC
    body = bytearray([0x02,0x54,0x51,0x57,0x00,0x07, 0x53,0x00,0x04, 0,0,0,0])
    body[9]  = 1 if open_valve else 0
    body[10] = 1 if dry else 0
    body[11] = 1 if close_on_offline else 0
    body[12] = line_cfg & 0xFF
    crc = crc16_ccitt(body)
    return bytes(body + struct.pack(">H", crc))

# Publish a settings frame to cloud for a given MAC using current cache defaults.
def publish_settings(mac: str, *, open_valve=None, dry=None, close_on_offline=None) -> bool:
    st = state_cache.get(mac, {})
    try:
        ov = bool(st.get("valve_open", False)) if open_valve is None else bool(open_valve)
        df = bool(st.get("dry_flag", False)) if dry is None else bool(dry)
        cf = bool(st.get("flag_cl_valve", False)) if close_on_offline is None else bool(close_on_offline)
        lc = int(st.get("line_in_cfg", 0))
    except Exception:
        ov = bool(open_valve) if open_valve is not None else False
        df = bool(dry) if dry is not None else False
        cf = bool(close_on_offline) if close_on_offline is not None else False
        lc = 0

    frame = compose_settings_frame(open_valve=ov, dry=df, close_on_offline=cf, line_cfg=lc)
    pref = CLOUD_PREFIX or mac_to_prefix.get(mac, "")
    if not pref:
        log("No cloud prefix known for", mac, ", waiting for incoming frame to learn it")
        return False
    try:
        client.publish(f"{pref}/{mac}/to", frame, qos=0, retain=True)
        return True
    except Exception:
        return False

def _retry_apply_dry_flag(mac: str, want_on: bool):
    # Retry a few times if device does not reflect dry_flag state yet
    for i in range(MAX_RETRIES):
        try:
            cur = bool(state_cache.get(mac, {}).get("dry_flag", False))
        except Exception:
            cur = None
        if cur == want_on:
            return
        time.sleep(RETRY_DELAY_SEC)
        ok = publish_settings(mac, dry=want_on)
        if not ok:
            # If we still don't know prefix, break early
            break

def _retry_apply_valve(mac: str, want_open: bool):
    # Retry if valve state did not reflect desired value
    for i in range(MAX_RETRIES):
        try:
            cur = bool(state_cache.get(mac, {}).get("valve_open", False))
        except Exception:
            cur = None
        if cur == want_open:
            return
        time.sleep(RETRY_DELAY_SEC)
        ok = publish_settings(mac, open_valve=want_open)
        if not ok:
            break

def _retry_apply_close_on_offline(mac: str, want_on: bool):
    # Retry if close_on_offline flag did not reflect desired value
    for i in range(MAX_RETRIES):
        try:
            cur = bool(state_cache.get(mac, {}).get("flag_cl_valve", False))
        except Exception:
            cur = None
        if cur == want_on:
            return
        time.sleep(RETRY_DELAY_SEC)
        ok = publish_settings(mac, close_on_offline=want_on)
        if not ok:
            break

# [BRIDGE DOC] Subscribe to upstream `<prefix>/+/from` and local `neptun/+/cmd/#`.
def on_connect(c, userdata, flags, rc):
    log("MQTT connected", rc)
    
    if CLOUD_PREFIX:
        c.subscribe(f"{CLOUD_PREFIX}/+/from", qos=0)
        c.subscribe(f"{CLOUD_PREFIX}/+/to", qos=0)
    else:
        c.subscribe("+/+/from", qos=0)
        c.subscribe("+/+/to", qos=0)
        if DEBUG:
            c.subscribe("#", qos=0)
    
    c.subscribe(f"{TOPIC_PREFIX}/+/cmd/#", qos=0)
    # Subscribe to retained settings to restore on restart
    c.subscribe(f"{TOPIC_PREFIX}/+/settings/module_lost_timeout", qos=0)
    try:
        print(
            "[BRIDGE] subscribed:",
            f"{CLOUD_PREFIX or '+/+'}/from",
            f"and {TOPIC_PREFIX}/+/cmd/#",
            file=sys.stderr, flush=True
        )
    except Exception:
        pass
    if DEBUG:
        print("[BRIDGE]","Subscribed:", f"{CLOUD_PREFIX or '+/+'}/from, {CLOUD_PREFIX or '+/+'}/to and {TOPIC_PREFIX}/+/cmd/#", file=sys.stderr, flush=True)

# [BRIDGE DOC] Route frames to publishers; handle valve command and forward to cloud.
def on_message(c, userdata, msg):
    """
    MQTT message router for both cloud frames and HA commands.

    - Cloud frames: `<prefix>/<MAC>/from` (binary). Validates and dispatches
      by frame type (0x52 -> publish_system, 0x53 -> publish_sensor_state).
    - HA commands: `neptun/<MAC>/cmd/...` (valve, dry_flag, close_on_offline,
      line_i_type). Composes a settings frame and publishes it back to the
      learned cloud prefix `<prefix>/<MAC>/to`.

    Uses `state_cache` to retain flags so changing one setting (e.g. valve)
    does not unintentionally reset others.
    """
    try:
        t = msg.topic
        if DEBUG:
            log("RX", t)
        if (t.endswith("/from") or t.endswith("/to")) and t.count("/") >= 2:
            
            mac = t.split("/")[1]
            try:
                pref = t.split("/")[0]
                mac_to_prefix[mac] = pref
            except Exception:
                pass
            buf = msg.payload if isinstance(msg.payload, (bytes, bytearray)) else bytes(msg.payload)
            if not buf or len(buf) < 8:
                return
            
            publish_raw(mac, buf)
            if not frame_ok(buf):
                log("Bad frame", t, buf.hex())
                return
            # On any valid device frame: compute inter-frame gap and update last_seen
            try:
                now_ts = time.time()
                prev_ts = float(last_seen.get(mac, 0) or 0)
                gap = int(now_ts - prev_ts) if prev_ts > 0 else 0
                base = f"{TOPIC_PREFIX}/{mac}"
                pub(f"{base}/frame_interval_seconds", gap, retain=True)
                last_seen[mac] = now_ts
            except Exception:
                pass
            typ = buf[3]
            if typ == 0x52:
                publish_system(mac, buf)
            elif typ == 0x53:
                publish_sensor_state(mac, buf)
            elif typ in (0x4E, 0x63, 0x43):   
                pass
            else:
                pass

        elif t.startswith(f"{TOPIC_PREFIX}/") and "/cmd/" in t:
            
            parts = t.split("/")
            # [neptun, <mac>, cmd, ...]
            mac = parts[1]
            cmd = parts[3:]
            if cmd[:1] == ["valve"]:
                
                pl = (msg.payload.decode("utf-8","ignore") if msg.payload else "").strip().upper()
                want_open = pl in ("1","ON","OPEN","TRUE")
                st = state_cache.get(mac, {})
                frame = compose_settings_frame(
                    open_valve=want_open,
                    dry=bool(st.get("dry_flag", False)),
                    close_on_offline=bool(st.get("flag_cl_valve", False)),
                    line_cfg=int(st.get("line_in_cfg", 0))
                )
                
                if CLOUD_PREFIX:
                    pref = CLOUD_PREFIX
                else:
                    try:
                        pref = mac_to_prefix.get(mac, "")
                    except Exception:
                        pref = ""
                if not pref:
                    log("No cloud prefix known for", mac, ", waiting for incoming frame to learn it")
                    return
                c.publish(f"{pref}/{mac}/to", frame, qos=0, retain=True)
                log("CMD valve ->", mac, "open" if want_open else "close")
                # Optimistic local state update for HA switch UX
                st["valve_open"] = want_open
                state_cache[mac] = st
                base = f"{TOPIC_PREFIX}/{mac}"
                pub(f"{base}/state/valve_open", "1" if want_open else "0", retain=False)
                try:
                    pub(
                        f"{base}/attributes/valve_switch",
                        {"icon_color": icon_color("valve_switch", ("1" if want_open else "0"))},
                        retain=False,
                    )
                except Exception:
                    pass
                try:
                    threading.Thread(target=_retry_apply_valve, args=(mac, want_open), daemon=True).start()
                except Exception:
                    pass
            elif cmd[:1] == ["dry_flag"]:
                pl = (msg.payload.decode("utf-8","ignore") if msg.payload else "").strip()
                up = pl.upper()
                want_on = up in ("1","ON","OPEN","TRUE","YES") or pl.lower() == "on"
                # Publish settings with desired dry flag
                ok = publish_settings(mac, dry=want_on)
                if not ok:
                    return
                log("CMD dry_flag ->", mac, "on" if want_on else "off")

                # Optimistic local state update to avoid HA UI reverting
                st["dry_flag"] = want_on
                state_cache[mac] = st
                base = f"{TOPIC_PREFIX}/{mac}"
                pub(f"{base}/settings/dry_flag", "on" if want_on else "off", retain=True)
                # Mirror also status/dry_flag for consistency
                pub(f"{base}/settings/status/dry_flag", "yes" if want_on else "no", retain=True)
                try:
                    pub(
                        f"{base}/attributes/dry_flag",
                        {"icon_color": icon_color("floor_wash", ("on" if want_on else "off"))},
                        retain=False,
                    )
                except Exception:
                    pass
                # Kick off a background retry if device state doesn't change
                try:
                    threading.Thread(target=_retry_apply_dry_flag, args=(mac, want_on), daemon=True).start()
                except Exception:
                    pass

            elif cmd[:1] == ["close_on_offline"]:
                pl = (msg.payload.decode("utf-8","ignore") if msg.payload else "").strip()
                up = pl.upper()
                want_on = up in ("1","ON","CLOSE","TRUE","YES") or pl.lower() in ("on","close")
                st = state_cache.get(mac, {})
                frame = compose_settings_frame(
                    open_valve=bool(st.get("valve_open", False)),
                    dry=bool(st.get("dry_flag", False)),
                    close_on_offline=want_on,
                    line_cfg=int(st.get("line_in_cfg", 0))
                )
                pref = CLOUD_PREFIX or mac_to_prefix.get(mac, "")
                if not pref:
                    log("No cloud prefix known for", mac, "‚ waiting for incoming frame to learn it")
                    return
                c.publish(f"{pref}/{mac}/to", frame, qos=0, retain=True)
                log("CMD close_on_offline ->", mac, "on" if want_on else "off")

                # Optimistic local state update
                st["flag_cl_valve"] = want_on
                state_cache[mac] = st
                base = f"{TOPIC_PREFIX}/{mac}"
                pub(f"{base}/settings/close_valve_flag", "close" if want_on else "open", retain=True)
                try:
                    threading.Thread(target=_retry_apply_close_on_offline, args=(mac, want_on), daemon=True).start()
                except Exception:
                    pass

            elif cmd[:2] == ["time", "set"]:
                # Accepts either numeric epoch seconds, ISO string, or 'now'
                pl_raw = (msg.payload.decode("utf-8","ignore") if msg.payload else "").strip()
                epoch = None
                if not pl_raw or pl_raw.lower() == "now" or pl_raw.lower() == "press":
                    epoch = int(time.time())
                else:
                    try:
                        # Try integer epoch first
                        epoch = int(float(pl_raw))
                    except Exception:
                        # Try ISO 8601 (python 3.11 fromisoformat handles many forms)
                        try:
                            dt = datetime.fromisoformat(pl_raw)
                            if dt.tzinfo is None:
                                # Treat naive as LOCAL time (Neptun reports local time)
                                epoch = int(time.mktime(dt.timetuple()))
                            else:
                                epoch = int(dt.timestamp())
                        except Exception:
                            epoch = int(time.time())

                frame = compose_time_set_frame(epoch)
                pref = CLOUD_PREFIX or mac_to_prefix.get(mac, "")
                if not pref:
                    log("No cloud prefix known for", mac, ", waiting for incoming frame to learn it")
                    return
                # Retain time-set so device receives it even if reconnects; device clears topic after processing
                c.publish(f"{pref}/{mac}/to", frame, qos=0, retain=True)
                log("CMD time set (retained) ->", mac, epoch)

            elif len(cmd) >= 1 and cmd[0].startswith("line_") and cmd[0].endswith("_type"):
                try:
                    part = cmd[0]  # e.g. line_1_type
                    idx = int(part.split("_")[1])
                except Exception:
                    idx = None
                if idx is None or not (1 <= idx <= 4):
                    return
                pl = (msg.payload.decode("utf-8","ignore") if msg.payload else "").strip().lower()
                want_counter = pl in ("1","on","true","yes","counter") or pl == "counter"
                st = state_cache.get(mac, {})
                base_cfg = int(st.get("line_in_cfg", 0)) & 0x0F
                mask = 1 << (idx - 1)
                if want_counter:
                    new_cfg = base_cfg | mask
                else:
                    new_cfg = base_cfg & (~mask & 0x0F)
                frame = compose_settings_frame(
                    open_valve=bool(st.get("valve_open", False)),
                    dry=bool(st.get("dry_flag", False)),
                    close_on_offline=bool(st.get("flag_cl_valve", False)),
                    line_cfg=new_cfg
                )
                pref = CLOUD_PREFIX or mac_to_prefix.get(mac, "")
                if not pref:
                    log("No cloud prefix known for", mac, "‚ waiting for incoming frame to learn it")
                    return
                c.publish(f"{pref}/{mac}/to", frame, qos=0, retain=True)
                log(f"CMD line_{idx}_type ->", mac, "counter" if want_counter else "sensor")

                # Optimistic local state update for select entity
                st["line_in_cfg"] = new_cfg
                state_cache[mac] = st
                base = f"{TOPIC_PREFIX}/{mac}"
                pub(f"{base}/settings/lines_in/line_{idx}", "counter" if want_counter else "sensor", retain=True)

            elif cmd[:1] == ["module_lost_timeout"]:
                # Set per-device Module Lost timeout in seconds via MQTT Number
                pl = (msg.payload.decode("utf-8","ignore") if msg.payload else "").strip()
                try:
                    val = int(float(pl))
                except Exception:
                    return
                if val < 10: val = 10
                if val > 3600: val = 3600
                module_lost_timeout[mac] = val
                base = f"{TOPIC_PREFIX}/{mac}"
                pub(f"{base}/settings/module_lost_timeout", val, retain=True)
                log("CMD module_lost_timeout ->", mac, val)

            elif len(cmd) >= 3 and cmd[0] == "counters" and cmd[1].startswith("line_"):
                # Handle counters write commands:
                # neptun/<mac>/cmd/counters/line_<i>/value/set  (payload=int liters)
                # neptun/<mac>/cmd/counters/line_<i>/step/set   (payload=int L/pulse)
                try:
                    idx = int(cmd[1].split("_")[1])
                except Exception:
                    idx = None
                if idx is None or not (1 <= idx <= 4):
                    return
                action = cmd[2]
                payload = (msg.payload.decode("utf-8","ignore") if msg.payload else "").strip()

                # Prepare defaults from last seen raw values to avoid zeroing other lines
                last = state_cache.get(mac, {}).get("counters_last", [])
                defaults = {}
                for i in range(1,5):
                    try:
                        rv, rs = last[i-1]
                    except Exception:
                        rv, rs = 0, 0
                    defaults[i] = (rv, rs)

                updates = {}
                if action == "value" and len(cmd) >= 4 and cmd[3] == "set":
                    try:
                        desired_l = int(float(payload))
                    except Exception:
                        return
                    # Keep current step for the line if known
                    cur_step = defaults.get(idx, (0,0))[1]
                    updates[idx] = (desired_l, cur_step if cur_step else 0)
                elif action == "step" and len(cmd) >= 4 and cmd[3] == "set":
                    try:
                        step = int(float(payload))
                        if not (0 < step <= 255):
                            return
                    except Exception:
                        return
                    cur_val = defaults.get(idx, (0,0))[0]
                    updates[idx] = (cur_val, step)
                else:
                    return

                # Build full lines payload combining defaults and updates
                lines = {}
                for i in range(1,5):
                    val, stp = defaults[i]
                    if i in updates:
                        uval, ustp = updates[i]
                        val = uval
                        if ustp:
                            stp = ustp
                    lines[i] = (val, stp)

                frame = compose_counters_set_frame(lines)
                pref = CLOUD_PREFIX or mac_to_prefix.get(mac, "")
                if not pref:
                    log("No cloud prefix known for", mac, ", waiting for incoming frame to learn it")
                    return
                c.publish(f"{pref}/{mac}/to", frame, qos=0, retain=True)
                log(f"CMD counters line_{idx} {action} ->", mac, payload)
            

    except Exception as e:
        log("on_message error:", e)
    
    # Restore retained settings (non-cmd topics)
    try:
        t = msg.topic
        if t.startswith(f"{TOPIC_PREFIX}/") and "/settings/" in t and t.endswith("/module_lost_timeout"):
            mac = t.split("/")[1]
            pl = (msg.payload.decode("utf-8","ignore") if msg.payload else "").strip()
            try:
                val = int(float(pl))
            except Exception:
                return
            if val < 10: val = 10
            if val > 3600: val = 3600
            module_lost_timeout[mac] = val
            if DEBUG:
                log("RESTORE module_lost_timeout <-", mac, val)
    except Exception:
        pass

client.on_connect = on_connect
client.on_message = on_message

# [BRIDGE DOC] MQTT connect loop with exponential backoff on errors.
def main():
    # Background watchdog to publish Module Lost state every 10s
    def watchdog_loop():
        while True:
            try:
                now = time.time()
                macs = set(list(last_seen.keys()) + list(announced))
                for mac in macs:
                    try:
                        # Determine module_lost purely by host-observed gap between frames
                        # Ignore device-reported time to avoid false positives from clock drift
                        last = float(last_seen.get(mac, 0) or 0)
                        try:
                            timeout = int(module_lost_timeout.get(mac, MODULE_LOST_DEFAULT))
                        except Exception:
                            timeout = MODULE_LOST_DEFAULT
                        lost = (now - last) > timeout
                        base = f"{TOPIC_PREFIX}/{mac}"
                        val = "yes" if lost else "no"
                        pub(f"{base}/settings/status/module_lost", val, retain=True)
                        try:
                            pub(
                                f"{base}/attributes/module_lost",
                                {"icon_color": icon_color("module_lost", val), "icon": icon_name("module_lost", val)},
                                retain=False,
                            )
                        except Exception:
                            pass
                        # Update discovery icon when module_lost bucket changes
                        try:
                            prev_cache = state_cache.get(mac, {})
                            last_b = prev_cache.get("module_lost_bucket")
                            if val != last_b:
                                device, safe_mac, dev_id = make_device(mac)
                                mod_lost_id = f"neptun_{safe_mac}_module_lost"
                                mod_lost_conf = {
                                    "name": f"Module Lost",
                                    "unique_id": mod_lost_id,
                                    "state_topic": f"{base}/settings/status/module_lost",
                                    "payload_on": "yes",
                                    "payload_off": "no",
                                    "device_class": "problem",
                                    "icon": icon_name("module_lost", val),
                                    "json_attributes_topic": f"{base}/attributes/module_lost",
                                    "device": device
                                }
                                pub(f"{DISCOVERY_PRE}/binary_sensor/{mod_lost_id}/config", mod_lost_conf, retain=True)
                                prev_cache["module_lost_bucket"] = val
                                state_cache[mac] = prev_cache
                        except Exception:
                            pass
                    except Exception:
                        continue
            except Exception:
                pass
            time.sleep(WATCHDOG_PERIOD)

    threading.Thread(target=watchdog_loop, name="neptun-watchdog", daemon=True).start()
    backoff = 1
    while True:
        try:
            if DEBUG:
                print("[BRIDGE]", "Connecting to", MQTT_HOST, MQTT_PORT, "user=", bool(MQTT_USER), file=sys.stderr, flush=True)
            try:
                print("[BRIDGE] connecting:", MQTT_HOST, MQTT_PORT, "auth=", bool(MQTT_USER and MQTT_PASS), file=sys.stderr, flush=True)
            except Exception:
                pass
            if MQTT_USER and MQTT_PASS:
                client.username_pw_set(MQTT_USER, MQTT_PASS)
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever(retry_first_connection=True)
        except Exception as e:
            log("MQTT reconnect in", backoff, "sec:", e)
            time.sleep(backoff)
            backoff = min(30, backoff*2)

if __name__ == "__main__":
    main()
