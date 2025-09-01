#!/usr/bin/with-contenv bash
set -e

# Читаем опции из options.json, которые HA кладёт в /data/options.json
OPTIONS="/data/options.json"

MQTT_PORT=$(jq -r '.mqtt.listen_port' "$OPTIONS")
MQTT_USER=$(jq -r '.mqtt.user' "$OPTIONS")
MQTT_PASS=$(jq -r '.mqtt.password' "$OPTIONS")
MQTT_ANON=$(jq -r '.mqtt.allow_anonymous' "$OPTIONS")

# Экспортируем для mosquitto.conf
export MQTT_LISTEN_PORT="${MQTT_PORT:-1883}"

if [ -n "$MQTT_USER" ] && [ "$MQTT_USER" != "null" ] && [ -n "$MQTT_PASS" ] && [ "$MQTT_PASS" != "null" ]; then
  export MQTT_ALLOW_ANON="false"
  mkdir -p /data/mosquitto
  mosquitto_passwd -c -b /data/mosquitto/passwd "$MQTT_USER" "$MQTT_PASS"
  if ! grep -q "password_file" /etc/mosquitto/mosquitto.conf; then
    echo "password_file /data/mosquitto/passwd" >> /etc/mosquitto/mosquitto.conf
    echo "acl_file /etc/mosquitto/acl" >> /etc/mosquitto/mosquitto.conf
  fi
else
  export MQTT_ALLOW_ANON="${MQTT_ANON:-true}"
fi
