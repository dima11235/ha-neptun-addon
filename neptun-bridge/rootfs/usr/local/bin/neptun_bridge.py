#!/usr/bin/env python3
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

def frame_ok(buf: bytes) -> bool:
    if not buf or len(buf) < 8: return False
    if buf[0] != 0x02 or buf[1] != 0x54: return False
    L = (buf[4] << 8) | buf[5]
    T = 6 + L + 2
    if len(buf) != T: return False
    return struct.unpack(">H", buf[-2:])[0] == crc16_ccitt(buf[:-2])

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

def split_cp1251_strings(b: bytes):
    parts = b.split(b"\x00")
    if parts and parts[-1] == b"": parts = parts[:-1]
    return [p.decode("cp1251", errors="ignore") for p in parts]

def decode_status_name(s: int) -> str:
    if s == 0: return "NORMAL"
    arr = []
    if s & 0x01: arr.append("ALARM")
    if s & 0x02: arr.append("MAIN BATTERY")
    if s & 0x04: arr.append("SENSOR BATTERY")
    if s & 0x08: arr.append("SENSOR OFFLINE")
    return ",".join(arr)

def map_lines_in(mask: int):
    a = []
    for i in range(4):
        a.append("water_counter" if ((mask >> i) & 1) else "wired_sensor")
    return {"line_1":a[0],"line_2":a[1],"line_3":a[2],"line_4":a[3]}

# ===== PARSERS =====
def parse_system_state(buf: bytes) -> dict:
    p = buf[6:-2]
    out = {}
    i = 0
    while i + 3 <= len(p):
        tag = p[i]; i += 1
        ln = (p[i] << 8) | p[i+1]; i += 2
        v = p[i:i+ln]; i += ln

        if tag == 0x49: # 'I' device type/version or id in некоторых прошивках
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
        elif tag == 0x43: # counters per line: 4B BE + 1B step
            cs = []
            for j in range(0, len(v)//5*5, 5):
                val = (v[j]<<24)|(v[j+1]<<16)|(v[j+2]<<8)|v[j+3]
                cs.append({"value": val & 0xFFFFFFFF, "step": v[j+4]})
            out["counters"] = cs
        elif tag == 0x44: # total water ASCII — мы не публикуем (по просьбе)
            try:
                out["water_total_ascii"] = v.decode("ascii","ignore")
                out["water_total"] = int(out["water_total_ascii"])
            except: pass
        elif tag == 0x57: # module RSSI/level
            if len(v): out["W"] = v[0]
        else:
            pass
    return out

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

def pub(topic, payload, retain=None, qos=0):
    if retain is None: retain = RETAIN_DEFAULT
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, ensure_ascii=False)
    if DEBUG:
        try:
            plen = (len(payload) if isinstance(payload, (bytes, bytearray)) else len(str(payload)))
        except Exception:
            plen = 0
        print("[BRIDGE]","PUB", topic, f"len={plen}", f"retain={retain}", file=sys.stderr, flush=True)
    client.publish(topic, payload, qos=qos, retain=retain)

def publish_raw(mac, buf: bytes):
    base = f"{TOPIC_PREFIX}/{mac}/raw"
    hexstr = buf.hex()
    b64    = buf.hex() # оставим hex в обоих для простоты чтения
    t = buf[3]
    th = f"0x{t:02x}"
    name = type_name(t)
    ts = int(time.time()*1000)

    # Универсальные
    pub(f"{base}/hex", hexstr, retain=False)
    pub(f"{base}/base64",  buf.decode("latin1","ignore"), retain=False)  # если хочется сырец
    pub(f"{base}/type", th, retain=False)
    pub(f"{base}/len", len(buf), retain=False)

    # По типу (retained)
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

def ensure_discovery(mac):
    if mac in announced:
        return
    device = {
        "identifiers": [f"neptun_{mac}"],
        "manufacturer": "Neptun",
        "model": "AquaControl",
        "name": f"Neptun {mac}"
    }
    # Клапан как MQTT switch (простая совместимость)
    obj_id = f"neptun_{mac}_valve"
    conf = {
        "name": "Neptun Valve",
        "uniq_id": obj_id,
        "cmd_t": f"~cmd/valve/set",
        "stat_t": f"~state/valve_open",
        "pl_on": "1",
        "pl_off": "0",
        "qos": 0,
        "retain": False,
        "dev": device,
        "~": f"{TOPIC_PREFIX}/{mac}/"
    }
    pub(f"{DISCOVERY_PRE}/switch/{obj_id}/config", conf, retain=True)

    # Счетчики (литры как value/step)
    for i in range(1,5):
        sid = f"neptun_{mac}_counter_{i}"
        conf = {
            "name": f"Counter line {i} (raw)",
            "uniq_id": sid,
            "stat_t": f"~counters/line_{i}/value",
            "dev_cla": "measurement",
            "unit_of_meas": "pulse",
            "dev": device,
            "~": f"{TOPIC_PREFIX}/{mac}/"
        }
        pub(f"{DISCOVERY_PRE}/sensor/{sid}/config", conf, retain=True)
        # Литры (derived)
        sidL = f"neptun_{mac}_liters_{i}"
        confL = {
            "name": f"Counter line {i} (liters)",
            "uniq_id": sidL,
            "stat_t": f"~counters/line_{i}/liters",
            "dev_cla": "water",
            "unit_of_meas": "L",
            "dev": device,
            "~": f"{TOPIC_PREFIX}/{mac}/"
        }
        pub(f"{DISCOVERY_PRE}/sensor/{sidL}/config", confL, retain=True)

    announced.add(mac)

def publish_system(mac_from_topic, buf: bytes):
    st = parse_system_state(buf)
    mac = st.get("mac", mac_from_topic)
    base = f"{TOPIC_PREFIX}/{mac}"

    publish_raw(mac, buf)
    ensure_discovery(mac)

    # кэш для команд
    prev = state_cache.get(mac, {})
    prev.update({
        "dry_flag": bool(st.get("dry_flag", False)),
        "flag_cl_valve": bool(st.get("flag_cl_valve", False)),
        "line_in_cfg": int(st.get("line_in_cfg", 0))
    })
    state_cache[mac] = prev

    # wireless sensors -> простые топики и сводка
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
        # discovery для leak как binary_sensor
        obj_id = f"neptun_{mac}_leak_{s['sensor_id']}"
        conf = {
            "name": f"Leak {s['sensor_id']}",
            "uniq_id": obj_id,
            "stat_t": f"~sensors_status/{s['sensor_id']}/attention",
            "pl_on": "1", "pl_off": "0",
            "dev_cla": "moisture",
            "dev": {
                "identifiers":[f"neptun_{mac}"],
                "manufacturer":"Neptun","model":"AquaControl","name":f"Neptun {mac}"
            },
            "~": f"{TOPIC_PREFIX}/{mac}/"
        }
        pub(f"{DISCOVERY_PRE}/binary_sensor/{obj_id}/config", conf, retain=True)

        # батарея/сигнал (обычные sensors)
        for what, name, unit in (("battery","Battery","%"), ("signal_level","Signal","")):
            sid = f"neptun_{mac}_{what}_{s['sensor_id']}"
            conf2 = {
                "name": f"{name} {s['sensor_id']}",
                "uniq_id": sid,
                "stat_t": f"~sensors_status/{s['sensor_id']}/{what}",
                "dev": {"identifiers":[f"neptun_{mac}"]},
                "~": f"{TOPIC_PREFIX}/{mac}/"
            }
            if unit: conf2["unit_of_meas"] = unit
            pub(f"{DISCOVERY_PRE}/sensor/{sid}/config", conf2, retain=True)

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
    # Публикуем как раньше, но без lines_status и без water_total
    pub(f"{base}/settings/status/alert", settings["status"]["alert"], retain=True)
    pub(f"{base}/settings/status/dry_flag", settings["status"]["dry_flag"], retain=True)
    pub(f"{base}/settings/status/sensors_lost", settings["status"]["sensors_lost"], retain=True)
    pub(f"{base}/settings/status/battery_discharge_in_module", settings["status"]["battery_discharge_in_module"], retain=True)
    pub(f"{base}/settings/status/battery_discharge_in_sensor", settings["status"]["battery_discharge_in_sensor"], retain=True)
    pub(f"{base}/settings/dry_flag", settings["dry_flag"], retain=True)
    li = settings["lines_in"]
    for k in ("line_1","line_2","line_3","line_4"):
        pub(f"{base}/settings/lines_in/{k}", li[k], retain=True)
    pub(f"{base}/settings/relay_count", settings["relay_count"], retain=True)
    pub(f"{base}/settings/sensors_count", settings["sensors_count"], retain=True)
    pub(f"{base}/settings/valve_settings", settings["valve_settings"], retain=True)
    pub(f"{base}/settings/close_valve_flag", settings["close_valve_flag"], retain=True)

    # top-level идентификаторы + signal_level (телеметрия)
    if "device_id" in st: pub(f"{base}/device_id", st["device_id"], retain=True)
    pub(f"{base}/mac_address", mac, retain=True)
    if "W" in st: pub(f"{base}/signal_level", st["W"], retain=False)

    # summary config/json (retained)
    parsedCfg = {
        "settings": settings,
        "device_id": st.get("device_id",""),
        "mac_address": mac,
        "signal_level": st.get("W"),
        "access_status": "available" if st.get("access") else "restricted"
    }
    if sensors_status: parsedCfg["sensors_status"] = sensors_status
    pub(f"{base}/config/json", parsedCfg, retain=True)

    # counters: value/step + liters
    for idx, c in enumerate(st.get("counters", []), start=1):
        val = int(c.get("value",0)); step = int(c.get("step",1)) or 1
        pub(f"{base}/counters/line_{idx}/value", val, retain=False)
        pub(f"{base}/counters/line_{idx}/step", step, retain=False)
        liters = round(val/step, 3)
        pub(f"{base}/counters/line_{idx}/liters", liters, retain=False)

    # state/*
    pub(f"{base}/state/json", st, retain=False)
    if "valve_open" in st: pub(f"{base}/state/valve_open", "1" if st["valve_open"] else "0", retain=False)
    if "status" in st: pub(f"{base}/state/status", st["status"], retain=False)
    if "status_name" in st and st["status_name"]: pub(f"{base}/state/status_name", st["status_name"], retain=False)

def publish_sensor_state(mac_from_topic, buf: bytes):
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

def compose_settings_frame(open_valve: bool, dry=False, close_on_offline=False, line_cfg=0):
    # 02 54 51 57 00 07 53 00 04 VV DF CF LC CRC
    body = bytearray([0x02,0x54,0x51,0x57,0x00,0x07, 0x53,0x00,0x04, 0,0,0,0])
    body[9]  = 1 if open_valve else 0
    body[10] = 1 if dry else 0
    body[11] = 1 if close_on_offline else 0
    body[12] = line_cfg & 0xFF
    crc = crc16_ccitt(body)
    return bytes(body + struct.pack(">H", crc))

def on_connect(c, userdata, flags, rc):
    log("MQTT connected", rc)
    # Входящие бинарные кадры от Neptun (локализованный «облако»-префикс)
    if CLOUD_PREFIX:
        c.subscribe(f"{CLOUD_PREFIX}/+/from", qos=0)
    else:
        c.subscribe("+/+/from", qos=0)
        if DEBUG:
            c.subscribe("#", qos=0)
    # Команды от HA
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

def on_message(c, userdata, msg):
    try:
        t = msg.topic
        if DEBUG:
            log("RX", t)
        if t.endswith("/from") and t.count("/") >= 2:
            # mac из топика 14cb.../<MAC>/from
            mac = t.split("/")[1]
            try:
                pref = t.split("/")[0]
                mac_to_prefix[mac] = pref
            except Exception:
                pass
            buf = msg.payload if isinstance(msg.payload, (bytes, bytearray)) else bytes(msg.payload)
            if not buf or len(buf) < 8:
                return
            # сохранить для отладки сырец
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
                # Можно добавить обработку имён при необходимости
                pass
            else:
                pass

        elif t.startswith(f"{TOPIC_PREFIX}/") and "/cmd/" in t:
            # команды из HA
            parts = t.split("/")
            # [neptun, <mac>, cmd, ...]
            mac = parts[1]
            cmd = parts[3:]
            if cmd[:1] == ["valve"]:
                # ожидаем payload "1"/"0" или "ON"/"OFF"|"OPEN"/"CLOSE"
                pl = (msg.payload.decode("utf-8","ignore") if msg.payload else "").strip().upper()
                want_open = pl in ("1","ON","OPEN","TRUE")
                st = state_cache.get(mac, {})
                frame = compose_settings_frame(
                    open_valve=want_open,
                    dry=bool(st.get("dry_flag", False)),
                    close_on_offline=bool(st.get("flag_cl_valve", False)),
                    line_cfg=int(st.get("line_in_cfg", 0))
                )
                # Публикуем в "облачный" to, RETAIN=TRUE — устройство само чистит
                if CLOUD_PREFIX:
                    pref = CLOUD_PREFIX
                else:
                    try:
                        pref = mac_to_prefix.get(mac, "")
                    except Exception:
                        pref = ""
                if not pref:
                    log("No cloud prefix known for", mac, "— waiting for incoming frame to learn it")
                    return
                c.publish(f"{pref}/{mac}/to", frame, qos=0, retain=True)
                log("CMD valve ->", mac, "open" if want_open else "close")
            # можно добавить: cmd/get_names -> 0x4E и 0x63, cmd/get_state -> 0x52

    except Exception as e:
        log("on_message error:", e)

client.on_connect = on_connect
client.on_message = on_message

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
