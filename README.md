# Neptun ProW+WiFi Local Bridge

Локальный мост для системы Neptun ProW+WiFi в Home Assistant.
Дополнение поднимает встроенный брокер Mosquitto, перехватывает соединение устройства и перенаправляет данные
в Home Assistant MQTT (`core-mosquitto`), так что управлять Neptun можно полностью локально без облака SST Cloud.

## Основные возможности
- Приём бинарных кадров Neptun и публикация структурированных MQTT-топиков `neptun/<MAC>/...`.
- Автоматическое создание и актуализация сущностей Home Assistant через MQTT Discovery.
- Управление клапаном, Floor Wash и Close On Offline с повторными отправками и защитой от «фликера».
- Публикация счётчиков, состояний линий, диагностики сигнала и других атрибутов с поддержкой `retain`.
- Вывод подробного лога и публикация необработанных кадров в `neptun/<MAC>/raw/*` для отладки.

## Что нового в 0.2.0
- Обновлённые discovery-пакеты с динамическими иконками и цветами (модуль, датчики протечки, RSSI, батареи, клапан).
- Повторные отправки команд и окно ожидания (по умолчанию 60 секунд) для Floor Wash, клапана и Close On Offline.
- Переработанная обработка телеметрии беспроводных датчиков и унифицированная запись Line Type/счётчиков.
- Улучшенные retain-настройки, чтобы состояния не терялись после перезапуска Home Assistant.

## Требования
- Home Assistant OS или Supervisor с доступом к магазину дополнений.
- MQTT-брокер Home Assistant (`core-mosquitto`) или другой доступный брокер.
- Возможность перенаправить облачный адрес Neptun `185.76.147.189:1883` на локальный `IP_HA:2883` (NAT/redirect).
- Учётные данные MQTT в `/config/secrets.yaml`, если брокер требует авторизацию.

## Установка
1. Добавьте репозиторий `https://github.com/dima11235/ha-neptun-addon` в магазине дополнений.
2. Установите и запустите **Neptun ProW+WiFi Local Bridge**.
3. Заполните раздел «Конфигурация» и добавьте секреты в `/config/secrets.yaml`.
4. Настройте NAT/redirect на маршрутизаторе, чтобы устройство Neptun подключалось к `IP_HA:2883` вместо SST Cloud.
5. Проверьте лог и убедитесь, что сущности Neptun появились в Home Assistant.

## Конфигурация
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

### Дополнительные переменные окружения
- `NB_PENDING_HOLD_SEC` (по умолчанию 60) — окно ожидания подтверждения команд.
- `NB_MODULE_LOST_TIMEOUT` (по умолчанию 300) — таймаут признания модуля «потерянным».
- `NB_WATCHDOG_PERIOD` (по умолчанию 30) — период фонового мониторинга.
- `NB_DEBUG` — подробный лог, `NB_RETAIN` — управление retain по умолчанию.

### Пример секретов (`/config/secrets.yaml`)
```yaml
mqtt_username: myuser
mqtt_password: mypass
```

## Диагностика
- Включите `debug: true`, чтобы видеть подробный лог подключения и разбор MQTT-кадров.
- Используйте топики `neptun/<MAC>/raw/*` для анализа исходных данных.
- Если сущности не обновляются, проверьте правила NAT и подключение устройства к `IP_HA:listen_port`.

## Обратная связь
Сообщайте об ошибках и предлагайте улучшения через Issues GitHub или присылайте Pull Request.
