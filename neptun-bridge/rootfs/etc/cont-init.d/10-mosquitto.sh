#!/usr/bin/with-contenv bash
set -e

# Читаем опции из options.json, которые HA кладёт в /data/options.json
OPTIONS="/data/options.json"

MQTT_PORT=$(jq -r '.mqtt.listen_port' "$OPTIONS")
MQTT_USER=$(jq -r '.mqtt.user' "$OPTIONS")
MQTT_PASS=$(jq -r '.mqtt.password' "$OPTIONS")
MQTT_ANON=$(jq -r '.mqtt.allow_anonymous' "$OPTIONS")

# Ensure persistence directory exists for mosquitto db
mkdir -p /data/mosquitto || true
# Align ownership when mosquitto runs as service user (best-effort)
chown -R mosquitto:mosquitto /data/mosquitto 2>/dev/null || true

# Align container timezone to host if TZ is provided
if [ -n "$TZ" ] && [ -f "/usr/share/zoneinfo/$TZ" ]; then
  ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime || true
  echo "$TZ" > /etc/timezone || true
fi

# HA MQTT bridge settings
HA_HOST=$(jq -r '.ha_mqtt.host' "$OPTIONS")
HA_PORT=$(jq -r '.ha_mqtt.port' "$OPTIONS")
HA_USER=$(jq -r '.ha_mqtt.user' "$OPTIONS")
HA_PASS=$(jq -r '.ha_mqtt.password' "$OPTIONS")
BR_TOPIC_PREFIX=$(jq -r '.bridge.topic_prefix' "$OPTIONS")
BR_DISC_PREFIX=$(jq -r '.bridge.discovery_prefix' "$OPTIONS")

# Defaults for HA broker if unset
if [ -z "$HA_HOST" ] || [ "$HA_HOST" = "null" ]; then HA_HOST="core-mosquitto"; fi
if [ -z "$HA_PORT" ] || [ "$HA_PORT" = "null" ]; then HA_PORT="1883"; fi

# Resolve !secret values from /config/secrets.yaml
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

# Apply secret resolution
MQTT_USER=$(resolve_secret "$MQTT_USER")
MQTT_PASS=$(resolve_secret "$MQTT_PASS")
HA_USER=$(resolve_secret "$HA_USER")
HA_PASS=$(resolve_secret "$HA_PASS")

# Optional: read direct secrets keys if present (fallbacks)
get_secret() {
  local key="$1"
  if [ -f /config/secrets.yaml ]; then
    local line
    line=$(grep -E "^[[:space:]]*$key:[[:space:]]*" /config/secrets.yaml | head -n1 || true)
    if [ -n "$line" ]; then
      echo "$line" | sed -E "s/^[^:]+:[[:space:]]*//" | sed -E "s/^['\"]?(.*)['\"]?$/\1/"
      return 0
    fi
  fi
  echo ""
}

# If mqtt_server secret exists, parse and override HA_HOST/HA_PORT
SECRET_MQTT_SERVER=$(get_secret mqtt_server)
if [ -n "$SECRET_MQTT_SERVER" ]; then
  # expected like mqtt://host:port
  srv=${SECRET_MQTT_SERVER#mqtt://}
  HA_HOST=${srv%%:*}
  HA_PORT=${srv##*:}
fi

# Username/password fallbacks from secrets if not set via options
if [ -z "$HA_USER" ] || [ "$HA_USER" = "null" ]; then HA_USER=$(get_secret mqtt_username); fi
if [ -z "$HA_PASS" ] || [ "$HA_PASS" = "null" ]; then HA_PASS=$(get_secret mqtt_password); fi

# Экспортируем для mosquitto.conf
export MQTT_LISTEN_PORT="${MQTT_PORT:-2883}"

if [ -n "$MQTT_USER" ] && [ "$MQTT_USER" != "null" ] && [ -n "$MQTT_PASS" ] && [ "$MQTT_PASS" != "null" ]; then
  export MQTT_ALLOW_ANON="false"
  mosquitto_passwd -c -b /data/mosquitto/passwd "$MQTT_USER" "$MQTT_PASS"
  if ! grep -q "password_file" /etc/mosquitto/mosquitto.conf; then
    echo "password_file /data/mosquitto/passwd" >> /etc/mosquitto/mosquitto.conf
    echo "acl_file /etc/mosquitto/acl" >> /etc/mosquitto/mosquitto.conf
  fi
else
  export MQTT_ALLOW_ANON="${MQTT_ANON:-true}"
fi

# Render placeholders in mosquitto.conf (listener / allow_anonymous)
CONF="/etc/mosquitto/mosquitto.conf"
# Use basic sed (BusyBox compatible)
sed -i "s|\${MQTT_LISTEN_PORT}|${MQTT_LISTEN_PORT}|g" "$CONF"
sed -i "s|\${MQTT_ALLOW_ANON}|${MQTT_ALLOW_ANON}|g" "$CONF"

# Configure bridge to HA broker if credentials provided
if [ -n "$HA_HOST" ] && [ "$HA_HOST" != "null" ] \
   && [ -n "$HA_PORT" ] && [ "$HA_PORT" != "null" ] \
   && [ -n "$HA_USER" ] && [ "$HA_USER" != "null" ] \
   && [ -n "$HA_PASS" ] && [ "$HA_PASS" != "null" ]; then
  # remove previous block if present
  sed -i '/^# BEGIN ha_bridge/,/^# END ha_bridge/d' /etc/mosquitto/mosquitto.conf || true
  cat >> /etc/mosquitto/mosquitto.conf <<EOF
# BEGIN ha_bridge
connection ha_bridge
address ${HA_HOST}:${HA_PORT}
remote_username ${HA_USER}
remote_password ${HA_PASS}
cleansession true
start_type automatic
notifications false
try_private false
# publish device topics and discovery to HA
topic ${BR_TOPIC_PREFIX}/# out 0
topic ${BR_DISC_PREFIX}/# out 0
# receive commands from HA
topic ${BR_TOPIC_PREFIX}/+/cmd/# in 0
# END ha_bridge
EOF
fi
