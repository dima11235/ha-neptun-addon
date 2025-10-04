# Neptun ProW+WiFi Local Bridge

Локальный мост для системы Neptun ProW+WiFi в Home Assistant.
Аддон запускает встроенный Mosquitto, принимает MQTT-кадры устройства и публикует структурированные топики
`neptun/<MAC>/...`, одновременно создавая discovery-сущности в Home Assistant.

## Основные функции
- Локальная работа без облака SST Cloud: NAT перенаправляет `185.76.147.189:1883` → `IP_HA:2883`.
- Динамические иконки, цвета и атрибуты для клапана, датчиков протечки, RSSI и батарей.
- Повторные отправки команд и защита от фликера для Floor Wash, Valve и Close On Offline.
- Публикация счётчиков, статусов линий, диагностических атрибутов и raw-кадров.

## Что нового в 0.2.0
- Обновлённые discovery-пакеты с динамическими иконками/цветами.
- Командные ретраи с окном ожидания 60 секунд.
- Переработанная телеметрия беспроводных датчиков и записи Line Type/счётчиков.

## Требования
- Home Assistant OS / Supervisor.
- MQTT-брокер `core-mosquitto` (или другой, указанный в конфигурации).
- Маршрутизатор с NAT/redirect для подключения модуля Neptun к `IP_HA:2883`.

## Конфигурация (пример)
```yaml
mqtt:
  listen_port: 2883
  allow_anonymous: true
ha_mqtt:
  host: core-mosquitto
  port: 1883
  user: "!secret mqtt_username"
  password: "!secret mqtt_password"
bridge:
  topic_prefix: neptun
  discovery_prefix: homeassistant
  retain: true
  debug: false
```

## Полезные переменные окружения
- `NB_PENDING_HOLD_SEC` — окно ожидания подтверждения команд (60 по умолчанию).
- `NB_MODULE_LOST_TIMEOUT` — таймаут потери связи (300 сек).
- `NB_WATCHDOG_PERIOD` — период фонового контроля (30 сек).
- `NB_DEBUG`, `NB_RETAIN` — управление логом и retain по умолчанию.

## Диагностика
- Включите `debug: true`, чтобы видеть подробный лог.
- Для анализа протокола используйте `neptun/<MAC>/raw/*`.
- Если сущности не обновляются, проверьте правила NAT и доступ к `IP_HA:listen_port`.

## Поддержка
Вопросы и предложения — через Issues и Pull Request в GitHub.
