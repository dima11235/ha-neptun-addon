#!/usr/bin/with-contenv bash
set -e
OPTIONS="/data/options.json"

export NB_CLOUD_PREFIX=$(jq -r '.bridge.cloud_prefix' "$OPTIONS")
export NB_TOPIC_PREFIX=$(jq -r '.bridge.topic_prefix' "$OPTIONS")
export NB_DISCOVERY_PREFIX=$(jq -r '.bridge.discovery_prefix' "$OPTIONS")
export NB_RETAIN=$(jq -r '.bridge.retain' "$OPTIONS")
export NB_DEBUG=$(jq -r '.bridge.debug' "$OPTIONS")

# Local MQTT auth for python (mirror mosquitto anon setting)
if grep -q "false" /etc/mosquitto/mosquitto.conf | grep -q allow_anonymous; then
  export NB_MQTT_USER=$(jq -r '.mqtt.user' "$OPTIONS")
  export NB_MQTT_PASS=$(jq -r '.mqtt.password' "$OPTIONS")
else
  export NB_MQTT_USER=""
  export NB_MQTT_PASS=""
fi

# HA MQTT connection (publish discovery + states, subscribe commands)
export NB_HA_MQTT_HOST=$(jq -r '.ha_mqtt.host' "$OPTIONS")
export NB_HA_MQTT_PORT=$(jq -r '.ha_mqtt.port' "$OPTIONS")
export NB_HA_MQTT_USER=$(jq -r '.ha_mqtt.user' "$OPTIONS")
export NB_HA_MQTT_PASS=$(jq -r '.ha_mqtt.password' "$OPTIONS")

# Resolve !secret for HA/user/pass and local user/pass
resolve_secret() {
  local val="$1"
  if [[ "$val" =~ ^!secret[[:space:]]+([A-Za-z0-9_]+)$ ]]; then
    local key="${BASH_REMATCH[1]}"
    if [ -f /config/secrets.yaml ]; then
      local line
      line=$(grep -E "^[[:space:]]*$key:[[:space:]]*" /config/secrets.yaml | head -n1 || true)
      if [ -n "$line" ]; then
        echo "$line" | sed -E "s/^[^:]+:[[:space:]]*//" | sed -E "s/^['\"]?(.*)['\"]?$/\1/"
        return 0
      fi
    fi
    echo ""; return 0
  fi
  echo "$val"
}

if [ -n "$NB_MQTT_USER" ]; then export NB_MQTT_USER=$(resolve_secret "$NB_MQTT_USER"); fi
if [ -n "$NB_MQTT_PASS" ]; then export NB_MQTT_PASS=$(resolve_secret "$NB_MQTT_PASS"); fi
if [ -n "$NB_HA_MQTT_USER" ]; then export NB_HA_MQTT_USER=$(resolve_secret "$NB_HA_MQTT_USER"); fi
if [ -n "$NB_HA_MQTT_PASS" ]; then export NB_HA_MQTT_PASS=$(resolve_secret "$NB_HA_MQTT_PASS"); fi
