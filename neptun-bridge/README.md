## Neptun ProW+WiFi Local Bridge Add-on

Local bridge for the Neptun ProW+WiFi system in Home Assistant.
The add-on starts an embedded Mosquitto broker, intercepts the device connection, and forwards data to Home Assistant MQTT (`core-mosquitto`), so you can control Neptun entirely locally without the SST Cloud.

## Key Features
- Receives Neptun binary frames and publishes structured MQTT topics under `neptun/<MAC>/...`.
- Automatically creates and updates Home Assistant entities via MQTT Discovery.
- Controls the valve, Floor Wash, and Close On Offline with retries and flicker protection.
- Publishes counters, line states, signal diagnostics, and other attributes with `retain` support.
- Writes a detailed log and publishes raw frames to `neptun/<MAC>/raw/*` for debugging.

## What's New in 0.2.0
- Refreshed discovery payloads with dynamic icons and colors (module, leak sensors, RSSI, batteries, valve).
- Command retries and a waiting window (60 seconds by default) for Floor Wash, the valve, and Close On Offline.
- Reworked telemetry handling for wireless sensors with unified Line Type/counter entries.
- Improved `retain` settings to keep states after restarting Home Assistant.

## Requirements
- Home Assistant OS or Supervisor with access to the add-on store.
- Home Assistant MQTT broker (`core-mosquitto`) or another accessible broker.
- Ability to redirect the Neptun cloud endpoint `185.76.147.189:1883` to the local `IP_HA:2883` (NAT/redirect).
- MQTT credentials in `/config/secrets.yaml` if the broker requires authentication.

## Installation
1. Add the repository `https://github.com/dima11235/ha-neptun-addon` in the add-on store.
2. Install and start **Neptun ProW+WiFi Local Bridge**.
3. Fill in the **Configuration** section and add secrets to `/config/secrets.yaml`.
4. Configure NAT/redirect on your router so the Neptun device connects to `IP_HA:2883` instead of the SST Cloud.
5. Review the log and make sure Neptun entities appear in Home Assistant.

Cloud to add-on redirection (NAT)
- Cloud endpoint: `185.76.147.189:1883`
- Local endpoint: `IP_HA:<listen_port>` (default `2883`)

Keenetic (CLI), where `192.168.1.200` is the HA IP:
```
ip static tcp 185.76.147.189/32 1883 192.168.1.200 2883
system configuration save
```

## Configuration
```yaml
mqtt:
  listen_port: 2883
  allow_anonymous: true
  user: ""
  password: ""
ha_mqtt:
  host: "core-mosquitto"
  port: 1883
  user: "!secret mqtt_username"
  password: "!secret mqtt_password"
bridge:
  cloud_prefix: ""
  topic_prefix: "neptun"
  discovery_prefix: "homeassistant"
  retain: true
  debug: false
```

### Additional Environment Variables
- `NB_PENDING_HOLD_SEC` (default 60) - command confirmation wait window.
- `NB_MODULE_LOST_TIMEOUT` (default 300) - timeout for marking a module as lost.
- `NB_WATCHDOG_PERIOD` (default 30) - background monitoring interval.
- `NB_DEBUG` enables verbose logging; `NB_RETAIN` toggles the default retain behavior.

### Secrets Example (`/config/secrets.yaml`)
```yaml
mqtt_username: myuser
mqtt_password: mypass
```

## Troubleshooting
- Enable `debug: true` to capture detailed connection logs and MQTT frame parsing.
- Use the `neptun/<MAC>/raw/*` topics to inspect raw data.
- If entities do not update, verify your NAT rules and that the device connects to `IP_HA:listen_port`.

## Feedback
Report bugs and suggest improvements through GitHub Issues or submit a Pull Request.
