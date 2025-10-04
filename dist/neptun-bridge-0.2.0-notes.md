# Neptun ProW+WiFi Local Bridge 0.2.0

## Что нового
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
4. Настройте NAT/redirect, чтобы модуль Neptun подключался к `IP_HA:2883` вместо SST Cloud.
5. Проверьте лог и убедитесь, что сущности Neptun появились в Home Assistant.

### Переадресация облака → аддон (NAT)
- Облачный адрес: `185.76.147.189:1883`
- Локальный адрес: `IP_HA:<listen_port>` (по умолчанию `2883`)

Пример для Keenetic (CLI), где `192.168.1.200` — IP Home Assistant:
```
ip static tcp 185.76.147.189/32 1883 192.168.1.200 2883
system configuration save
```

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
- `NB_DEBUG` — подробный лог, `NB_RETAIN` — поведение retain по умолчанию.

### Пример секретов (`/config/secrets.yaml`)
```yaml
mqtt_username: myuser
mqtt_password: mypass
```

## Диагностика
- Включите `debug: true`, чтобы видеть подробный лог подключения и разбор MQTT-кадров.
- Используйте топики `neptun/<MAC>/raw/*` для анализа необработанных данных.
- Если сущности не обновляются, проверьте правила NAT и доступ к `IP_HA:listen_port`.

## Обратная связь
Сообщайте об ошибках и предлагайте улучшения через Issues GitHub или присылайте Pull Request.
