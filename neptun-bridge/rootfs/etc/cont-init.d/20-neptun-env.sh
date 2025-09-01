#!/usr/bin/with-contenv bash
set -e
OPTIONS="/data/options.json"

export NB_CLOUD_PREFIX=$(jq -r '.bridge.cloud_prefix' "$OPTIONS")
export NB_TOPIC_PREFIX=$(jq -r '.bridge.topic_prefix' "$OPTIONS")
export NB_DISCOVERY_PREFIX=$(jq -r '.bridge.discovery_prefix' "$OPTIONS")
export NB_RETAIN=$(jq -r '.bridge.retain' "$OPTIONS")
export NB_DEBUG=$(jq -r '.bridge.debug' "$OPTIONS")

# Для python-клиента MQTT
if grep -q "false" /etc/mosquitto/mosquitto.conf | grep -q allow_anonymous; then
  export NB_MQTT_USER=$(jq -r '.mqtt.user' "$OPTIONS")
  export NB_MQTT_PASS=$(jq -r '.mqtt.password' "$OPTIONS")
else
  export NB_MQTT_USER=""
  export NB_MQTT_PASS=""
fi
