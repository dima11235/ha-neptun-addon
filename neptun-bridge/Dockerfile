ARG BUILD_ARCH
ARG BUILD_FROM
FROM ${BUILD_FROM}

# Python + mosquitto
# Use Alpine package for paho-mqtt to avoid PEP 668 (externally managed env)
RUN apk add --no-cache python3 py3-paho-mqtt mosquitto mosquitto-clients runuser bash tzdata

# s6 overlay уже в base образе от HA
COPY rootfs/ /

# Права
RUN chmod +x /etc/cont-init.d/*.sh \
    && chmod +x /etc/services.d/*/run /etc/services.d/*/finish \
    && chmod +x /usr/local/bin/neptun_bridge.py

# Среда
ENV PYTHONUNBUFFERED=1

# Mosquitto порт
# EXPOSE is informational only. The actual Mosquitto listener port
# is set from add-on options (`mqtt.listen_port`) by
# `rootfs/etc/cont-init.d/10-mosquitto.sh` into `mosquitto.conf`.
# With `host_network: true`, Docker port publishing is unused by HA;
# EXPOSE documents the default (2883) and helps tooling only.
EXPOSE 2883

# s6 запускает всё сам
