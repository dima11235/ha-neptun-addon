ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.19
FROM ${BUILD_FROM}

# Python + mosquitto
RUN apk add --no-cache python3 py3-pip mosquitto mosquitto-clients runuser bash tzdata \
 && pip3 install --no-cache-dir paho-mqtt==1.6.1

# s6 overlay уже в base образе от HA
COPY rootfs/ /

# Права
RUN chmod +x /etc/cont-init.d/*.sh \
    && chmod +x /etc/services.d/*/run /etc/services.d/*/finish \
    && chmod +x /usr/local/bin/neptun_bridge.py

# Среда
ENV PYTHONUNBUFFERED=1

# Mosquitto порт
EXPOSE 2883

# s6 запускает всё сам
