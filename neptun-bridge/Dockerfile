ARG BUILD_ARCH
ARG BUILD_FROM
FROM ${BUILD_FROM}

# Dockerfile для HA add-on: Neptun Local Bridge
# - Базовый образ задаётся Supervisor через BUILD_FROM
# - Устанавливаем Python 3, paho-mqtt из пакетов Alpine, mosquitto и инструменты
# - Кладём rootfs/ (s6-overlay совместимый) и выставляем права

# Python + mosquitto
# Используем пакетный py3-paho-mqtt, чтобы избежать PEP 668 (externally managed env)
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
# EXPOSE носит информативный характер. Реальный порт слушателя Mosquitto
# задаётся опцией аддона (mqtt.listen_port) и подставляется скриптом
# cont-init.d/10-mosquitto.sh в mosquitto.conf. Так как используется
# host_network: true, публикация портов Docker не применяется; EXPOSE лишь
# документирует дефолт (2883) и помогает вспомогательным инструментам.
EXPOSE 2883

# s6 запускает всё сам
