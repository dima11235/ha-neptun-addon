## Neptun Local Bridge (Home Assistant add-on)

Локальный мост MQTT для модулей Neptun AquaControl (ProW+WiFi). Репозиторий содержит готовый аддон для Home Assistant и сопутствующие скрипты/конфигурацию.

Кратко
- Перехватывает MQTT‑трафик модуля Neptun локально (через встроенный Mosquitto).
- Декодирует бинарные кадры (CRC16‑CCITT, TLV) и публикует структурированные топики `neptun/<MAC>/**`.
- Публикует MQTT Discovery в `homeassistant/**` и принимает команды из HA.
- Полностью локальная интеграция, без доступа к облаку Neptun.

Архитектура
- Внутри аддона запущен Mosquitto (локальный брокер). Модуль Neptun «думает», что подключается к облаку, но правило NAT вашего роутера перенаправляет его на этот локальный брокер.
- Python‑скрипт (`neptun_bridge.py`) подписывается на бинарные топики устройства, валидирует и парсит кадры, публикует состояния и Discovery.
- Mosquitto внутри аддона настроен «мостом» к брокеру Home Assistant (обычно `core-mosquitto`): Discovery/состояния уходят в HA, команды приходят обратно в аддон.

Установка как дополнения HA
1) Настройки → Дополнения → Магазин дополнений → Репозитории → добавить `https://github.com/dima11235/ha-neptun-addon`.
2) Установите “Neptun ProW+WiFi Local Bridge (Home Assistant add-on)”.
3) Вкладка Конфигурация — заполните параметры (см. ниже). Для HA‑MQTT удобно использовать `!secret`.
4) Запустите аддон и проверьте логи.
5) Настройте переадресацию с облака Neptun на аддон (пример для Keenetic ниже). После первого кадра сущности появятся в HA автоматически.

Переадресация облака → аддон (NAT)
- Облачный адрес: `185.76.147.189:1883`
- Локальный: `IP_HA:<listen_port>` (по умолчанию `2883`)

Keenetic (CLI), где `192.168.1.200` — IP HA:
```
ip static tcp 185.76.147.189/32 1883 192.168.1.200 2883
system configuration save
```
Нюансы:
- Совпадение порта с `mqtt.listen_port` (2883 по умолчанию).
- Возможен hairpin/loopback NAT для клиентов в одной подсети.

Параметры аддона (options)
```
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
Пример `secrets.yaml`:
```
mqtt_username: myuser
mqtt_password: mypass
mqtt_server: mqtt://core-mosquitto:1883
```

Сущности в Home Assistant (основное)
- Switch: Valve — управление клапаном.
- Switch: Floor Wash — «сухая уборка» (dry_flag).
- Switch: Close On Offline — закрывать клапан при оффлайне.
- Select: Line N Type (1..4) — sensor/counter.
- Sensor: Line N Counter (м³) и Line N Counter Step (L/pulse).
- Number: Line N Counter (set) и Line N Step (set).
- Sensors: Module Status (текст), Module RSSI (%), Frame Interval (s), Device Time.
- Button: Set Device Time.
- Binary sensors: Leak Detected; Line N Leak (1..4); Module/Sensors Battery; Sensors Online; Module Online; Valve Open.
- Number: Module Lost Timeout (сек).

Темы MQTT (схема)
- База: `neptun/<MAC>/...`
- Состояния: `state/*`, `settings/*`, `counters/*`, `lines_status/*`.
- Команды: `neptun/<MAC>/cmd/**/set` (клапан, настройки линий, счетчики, время и т.д.).
- Диагностика raw: `neptun/<MAC>/raw/*` (hex, base64, by_type).

Переменные окружения (скрипт)
- `NB_TOPIC_PREFIX` (по умолчанию `neptun`), `NB_DISCOVERY_PREFIX` (по умолчанию `homeassistant`), `NB_CLOUD_PREFIX` (обычно пусто).
- `NB_MQTT_PORT`, `NB_MQTT_USER`, `NB_MQTT_PASS` — подключение к локальному брокеру аддона.
- `NB_RETAIN`, `NB_DEBUG`, `NB_WATCHDOG_PERIOD`, `NB_MODULE_LOST_TIMEOUT`.

Разработка
- Структура:
  - `neptun-bridge/rootfs/...` — s6‑сервисы, mosquitto, скрипт моста.
  - `neptun-bridge/config.yaml` — манифест аддона.
  - `neptun-bridge/Dockerfile` — образ аддона на базе HA Supervisor.
- Запуск вне HA не поддерживается «из коробки». Аддон рассчитан на среду Supervisor (s6, `/data/options.json`, секреты из `/config/secrets.yaml`).
- Для теста парсера можно запустить `neptun_bridge.py` c заданными `NB_*`, имея рядом брокер MQTT; однако полный функционал (мост в HA, автоконфиг) проще проверить через HA‑аддон.

Поддержка
- Вопросы и проблемы — через Issues в репозитории.
- Пожалуйста, приложите лог аддона и описание конфигурации (порты, NAT, настройки MQTT).

