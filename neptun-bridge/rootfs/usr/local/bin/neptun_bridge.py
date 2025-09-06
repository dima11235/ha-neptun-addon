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

Protocol notes:
- Frame signature: 0x02 0x54 ... type at byte[3], big-endian length at [4..5], CRC16-CCITT trailing 2 bytes.
- Types: 0x52 system_state, 0x53 sensor_state, 0x43 counter_state, etc.
"""
import os, sys, json, time, struct
from datetime import datetime
import paho.mqtt.client as mqtt

CLOUD_PREFIX   = os.getenv("NB_CLOUD_PREFIX", "")
TOPIC_PREFIX   = os.getenv("NB_TOPIC_PREFIX", "neptun")
DISCOVERY_PRE  = os.getenv("NB_DISCOVERY_PREFIX", "homeassistant")
RETAIN_DEFAULT = os.getenv("NB_RETAIN", "true").lower() == "true"
DEBUG          = os.getenv("NB_DEBUG", "false").lower() == "true"

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

def log(*a):
    if DEBUG: print("[BRIDGE]", *a, file=sys.stderr)

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
    
    # Two stateless buttons for valve control
    btn_open_id = f"neptun_{safe_mac}_valve_open"
    btn_open_conf = {
        "name": f"Valve Open",
        "unique_id": btn_open_id,
        "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/valve/set",
        "payload_press": "1",
        "icon": "mdi:water-pump",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/button/{btn_open_id}/config", btn_open_conf, retain=True)

    btn_close_id = f"neptun_{safe_mac}_valve_close"
    btn_close_conf = {
        "name": f"Valve Close",
        "unique_id": btn_close_id,
        "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/valve/set",
        "payload_press": "0",
        "icon": "mdi:water-pump-off",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/button/{btn_close_id}/config", btn_close_conf, retain=True)

    # Add MQTT switch for Dry Flag
    obj_id2 = f"neptun_{safe_mac}_dry_flag"
    conf2 = {
        "name": f"Dry Flag",
        "unique_id": obj_id2,
        "command_topic": f"{TOPIC_PREFIX}/{mac}/cmd/dry_flag/set",
        "state_topic": f"{TOPIC_PREFIX}/{mac}/settings/dry_flag",
        "payload_on": "on",
        "payload_off": "off",
        "qos": 0,
        "retain": False,
        "icon": "mdi:water-off",
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
        "retain": False,
        "icon": "mdi:lan-disconnect",
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
            "icon": "mdi:water",
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
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/sensor/{sidS}/config", confS, retain=True)

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
            "icon": "mdi:water-alert",
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
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/sensor/{t_id}/config", t_conf, retain=True)

    # Additional binary sensors matching Node-RED flow
    base_topic = f"{TOPIC_PREFIX}/{mac}"
    # Overall leak detected
    leak_id = f"neptun_{safe_mac}_leak_detected"
    leak_conf = {
        "name": f"Leak Detected",
        "unique_id": leak_id,
        "state_topic": f"{base_topic}/settings/status/alert",
        "payload_on": "on",
        "payload_off": "off",
        "device_class": "problem",
        "icon": "mdi:water-alert",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{leak_id}/config", leak_conf, retain=True)

    # Valve Closed (inverse of valve_open)
    valve_closed_id = f"neptun_{safe_mac}_valve_closed"
    valve_closed_conf = {
        "name": f"Valve Closed",
        "unique_id": valve_closed_id,
        "state_topic": f"{base_topic}/state/valve_open",
        "payload_on": "0",
        "payload_off": "1",
        "device_class": "problem",
        "icon": "mdi:valve",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{valve_closed_id}/config", valve_closed_conf, retain=True)

    # Module Battery Discharged
    mod_batt_id = f"neptun_{safe_mac}_module_battery"
    mod_batt_conf = {
        "name": f"Module Battery",
        "unique_id": mod_batt_id,
        "state_topic": f"{base_topic}/settings/status/battery_discharge_in_module",
        "payload_on": "yes",
        "payload_off": "no",
        "device_class": "battery",
        "icon": "mdi:battery-alert",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{mod_batt_id}/config", mod_batt_conf, retain=True)

    # Sensors Battery Discharged
    sens_batt_id = f"neptun_{safe_mac}_sensors_battery"
    sens_batt_conf = {
        "name": f"Sensors Battery",
        "unique_id": sens_batt_id,
        "state_topic": f"{base_topic}/settings/status/battery_discharge_in_sensor",
        "payload_on": "yes",
        "payload_off": "no",
        "device_class": "problem",
        "icon": "mdi:battery-alert",
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
        "icon": "mdi:lan-disconnect",
        "device": device
    }
    pub(f"{DISCOVERY_PRE}/binary_sensor/{sens_lost_id}/config", sens_lost_conf, retain=True)

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
        sensors_status.append({
            "line": s["sensor_id"],
            "battery": s["battery_percent"],
            "attention": 1 if s["leak"] else 0,
            "signal_level": s["signal_level"]
        })
        pub(f"{base}/sensors_status/{s['sensor_id']}/battery", s["battery_percent"], retain=False)
        pub(f"{base}/sensors_status/{s['sensor_id']}/signal_level", s["signal_level"], retain=False)
        pub(f"{base}/sensors_status/{s['sensor_id']}/attention", 1 if s["leak"] else 0, retain=False)
             
        obj_id = f"neptun_{safe_mac}_sensor_{s['sensor_id']}_leak"
        conf = {
            "name": f"Sensor {s['sensor_id']} Leak",
            "unique_id": obj_id,
            "state_topic": f"{TOPIC_PREFIX}/{mac}/sensors_status/{s['sensor_id']}/attention",
            "payload_on": "1", "payload_off": "0",
            "device_class": "moisture",
            "icon": "mdi:water-alert",
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
            "icon": "mdi:battery",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/sensor/{obj_id}/config", conf, retain=True)

        obj_id = f"neptun_{safe_mac}_sensor_{s['sensor_id']}_signal_level"
        conf = {
            "name": f"Sensor {s['sensor_id']} RSSI",
            "unique_id": obj_id,
            "state_topic": f"{TOPIC_PREFIX}/{mac}/sensors_status/{s['sensor_id']}/signal_level",
            "unit_of_measurement": "lqi",
            "icon": "mdi:signal",
            "device": device
        }
        pub(f"{DISCOVERY_PRE}/sensor/{obj_id}/config", conf, retain=True)

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
        },
        "dry_flag": "on" if st.get("dry_flag") else "off",
        "lines_in": map_lines_in(st.get("line_in_cfg",0)),
        "relay_count": st.get("relay_count",0),
        "sensors_count": st.get("sensors_count",0),
        "valve_settings": "opened" if st.get("valve_open") else "closed",
        "close_valve_flag": "close" if st.get("flag_cl_valve") else "open",
    }
    
    pub(f"{base}/settings/status/alert", settings["status"]["alert"], retain=True)
    pub(f"{base}/settings/status/dry_flag", settings["status"]["dry_flag"], retain=True)
    pub(f"{base}/settings/status/sensors_lost", settings["status"]["sensors_lost"], retain=True)
    pub(f"{base}/settings/status/battery_discharge_in_module", settings["status"]["battery_discharge_in_module"], retain=True)
    pub(f"{base}/settings/status/battery_discharge_in_sensor", settings["status"]["battery_discharge_in_sensor"], retain=True)
    pub(f"{base}/settings/dry_flag", settings["dry_flag"], retain=True)
    pub(f"{base}/settings/relay_count", settings["relay_count"], retain=True)
    pub(f"{base}/settings/sensors_count", settings["sensors_count"], retain=True)
    pub(f"{base}/settings/valve_settings", settings["valve_settings"], retain=True)
    pub(f"{base}/settings/close_valve_flag", settings["close_valve_flag"], retain=True)

    li = settings["lines_in"]
    for k in ("line_1","line_2","line_3","line_4"):
        pub(f"{base}/settings/lines_in/{k}", li[k], retain=True)
    
    if "device_id" in st: pub(f"{base}/device_id", st["device_id"], retain=True)
    if "W" in st: pub(f"{base}/signal_level", st["W"], retain=False)

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

    for idx, c in enumerate(st.get("counters", []), start=1):
        val = int(c.get("value",0)); step = int(c.get("step",1)) or 1
        pub(f"{base}/counters/line_{idx}/value", val, retain=False)
        pub(f"{base}/counters/line_{idx}/step", step, retain=False)

    # Publish wired lines leak status (on/off)
    wired = st.get("wired_states", [])
    if wired:
        for i in range(4):
            stv = "on" if (i < len(wired) and wired[i]) else "off"
            pub(f"{base}/lines_status/line_{i+1}", stv, retain=False)

    # Do not publish duplicate line input types under base/lines_in/*.
    # Kept only settings/lines_in/{k} publishes above for a single source of truth.

    # state/*
    pub(f"{base}/state/json", st, retain=False)
    if "valve_open" in st: pub(f"{base}/state/valve_open", "1" if st["valve_open"] else "0", retain=False)
    if "status" in st: pub(f"{base}/state/status", st["status"], retain=False)
    if "status_name" in st and st["status_name"]: pub(f"{base}/state/status_name", st["status_name"], retain=False)

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
    if sensors:
        slim = []
        for s in sensors:
            slim.append({"line": s["sensor_id"], "battery": s["battery_percent"], "attention": 1 if s["leak"] else 0, "signal_level": s["signal_level"]})
            pub(f"{base}/sensors_status/{s['sensor_id']}/battery", s["battery_percent"], retain=False)
            pub(f"{base}/sensors_status/{s['sensor_id']}/signal_level", s["signal_level"], retain=False)
            pub(f"{base}/sensors_status/{s['sensor_id']}/attention", 1 if s["leak"] else 0, retain=False)
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

# [BRIDGE DOC] Subscribe to upstream `<prefix>/+/from` and local `neptun/+/cmd/#`.
def on_connect(c, userdata, flags, rc):
    log("MQTT connected", rc)
    
    if CLOUD_PREFIX:
        c.subscribe(f"{CLOUD_PREFIX}/+/from", qos=0)
    else:
        c.subscribe("+/+/from", qos=0)
        if DEBUG:
            c.subscribe("#", qos=0)
    
    c.subscribe(f"{TOPIC_PREFIX}/+/cmd/#", qos=0)
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
        print("[BRIDGE]","Subscribed:", f"{CLOUD_PREFIX or '+/+'}/from and {TOPIC_PREFIX}/+/cmd/#", file=sys.stderr, flush=True)

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
        if t.endswith("/from") and t.count("/") >= 2:
            
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
            elif cmd[:1] == ["dry_flag"]:
                pl = (msg.payload.decode("utf-8","ignore") if msg.payload else "").strip()
                up = pl.upper()
                want_on = up in ("1","ON","OPEN","TRUE","YES") or pl.lower() == "on"
                st = state_cache.get(mac, {})
                frame = compose_settings_frame(
                    open_valve=bool(st.get("valve_open", False)),
                    dry=want_on,
                    close_on_offline=bool(st.get("flag_cl_valve", False)),
                    line_cfg=int(st.get("line_in_cfg", 0))
                )
                pref = CLOUD_PREFIX or mac_to_prefix.get(mac, "")
                if not pref:
                    log("No cloud prefix known for", mac, ", waiting for incoming frame to learn it")
                    return
                c.publish(f"{pref}/{mac}/to", frame, qos=0, retain=True)
                log("CMD dry_flag ->", mac, "on" if want_on else "off")

                # Optimistic local state update to avoid HA UI reverting
                st["dry_flag"] = want_on
                state_cache[mac] = st
                base = f"{TOPIC_PREFIX}/{mac}"
                pub(f"{base}/settings/dry_flag", "on" if want_on else "off", retain=True)
                # Mirror also status/dry_flag for consistency
                pub(f"{base}/settings/status/dry_flag", "yes" if want_on else "no", retain=True)

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
                    log("No cloud prefix known for", mac, "РІР‚вЂќ waiting for incoming frame to learn it")
                    return
                c.publish(f"{pref}/{mac}/to", frame, qos=0, retain=True)
                log("CMD close_on_offline ->", mac, "on" if want_on else "off")

                # Optimistic local state update
                st["flag_cl_valve"] = want_on
                state_cache[mac] = st
                base = f"{TOPIC_PREFIX}/{mac}"
                pub(f"{base}/settings/close_valve_flag", "close" if want_on else "open", retain=True)

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
                    log("No cloud prefix known for", mac, "РІР‚вЂќ waiting for incoming frame to learn it")
                    return
                c.publish(f"{pref}/{mac}/to", frame, qos=0, retain=True)
                log(f"CMD line_{idx}_type ->", mac, "counter" if want_counter else "sensor")

                # Optimistic local state update for select entity
                st["line_in_cfg"] = new_cfg
                state_cache[mac] = st
                base = f"{TOPIC_PREFIX}/{mac}"
                pub(f"{base}/settings/lines_in/line_{idx}", "counter" if want_counter else "sensor", retain=True)
            

    except Exception as e:
        log("on_message error:", e)

client.on_connect = on_connect
client.on_message = on_message

# [BRIDGE DOC] MQTT connect loop with exponential backoff on errors.
def main():
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
