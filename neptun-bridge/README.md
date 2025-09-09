## Neptun ProW+WiFi Local Bridge (дополнение для Home Assistant)

Локальный мост MQTT для модулей Neptun AquaControl (ProW+WiFi). Аддон перехватывает MQTT‑трафик устройства, валидирует и декодирует бинарные кадры, публикует человекочитаемые топики и автоматически создает устройства/сущности в Home Assistant через MQTT Discovery. Работает полностью локально — без подключения к облаку Neptun.

Что делает аддон
- Принимает поток бинарных кадров от модуля Neptun через локальный брокер Mosquitto внутри аддона (порт настраивается).
- Проверяет CRC16‑CCITT, разбирает TLV и публикует структурированные состояния в `neptun/<MAC>/**`.
- Публикует Discovery в `homeassistant/**` (переключатели, сенсоры, селекты, number, button).
- Мостит данные в брокер Home Assistant (`core-mosquitto`) и принимает команды обратно.

Требования
- Home Assistant с установленным брокером MQTT (обычно `core-mosquitto`).
- Роутер с возможностью статической переадресации (NAT/redirect) для перехвата облачного адреса Neptun.
- Модуль Neptun ProW+WiFi, подключенный к вашему Wi‑Fi/роутеру.

Установка
1) В HA: Настройки → Дополнения → Магазин дополнений → Репозитории → добавить `https://github.com/dima11235/ha-neptun-addon`.
2) Установите “Neptun ProW+WiFi Local Bridge (Home Assistant add-on)”.
3) Откройте вкладку Конфигурация и задайте параметры (см. ниже). Для подключения к HA‑MQTT удобно использовать `!secret`.
4) Запустите аддон. В логах увидите обнаружение порта и параметры моста.
5) Настройте переадресацию MQTT с облака Neptun на аддон (пример ниже). После первого входящего кадра сущности появятся в HA автоматически (MQTT Discovery).

Переадресация (NAT/redirect)
- Облачный адрес: `185.76.147.189:1883`
- Локальный адрес: `IP_HA:<listen_port>` (по умолчанию `2883`)

Пример для Keenetic (CLI), где `192.168.1.200` — IP Home Assistant:
```
ip static tcp 185.76.147.189/32 1883 192.168.1.200 2883
system configuration save
```
Важно:
- `IP_HA` должен быть доступен модулю Neptun из вашей сети.
- Порт должен совпадать с `mqtt.listen_port` (по умолчанию 2883).
- На маршрутизаторе может понадобиться hairpin/loopback NAT или policy‑based NAT, если модуль и HA в одной подсети.

Параметры аддона (options)
```
mqtt:
  listen_port: 2883          # Порт локального брокера внутри аддона
  allow_anonymous: true      # Разрешить анонимные подключения (по умолчанию true)
  user: ""                   # (опционально) логин локального брокера
  password: ""               # (опционально) пароль локального брокера
ha_mqtt:
  host: "core-mosquitto"     # Брокер HA (по умолчанию core-mosquitto)
  port: 1883                 # Порт брокера HA
  user: "!secret mqtt_username"      # Логин HA MQTT (!secret из secrets.yaml)
  password: "!secret mqtt_password"  # Пароль HA MQTT (!secret из secrets.yaml)
bridge:
  cloud_prefix: ""           # Префикс «облака» (обычно пусто: autodetect)
  topic_prefix: "neptun"     # Базовый префикс локальных топиков
  discovery_prefix: "homeassistant"  # Префикс MQTT Discovery
  retain: true               # Retain по умолчанию для публикуемых значений
  debug: false               # Подробный лог в stderr
```
Пример secrets (`/config/secrets.yaml`):
```
mqtt_username: myuser
mqtt_password: mypass
mqtt_server: mqtt://core-mosquitto:1883
```

Создаваемые сущности в Home Assistant
- Переключатель: Valve — управление клапаном (on=open, off=close).
- Переключатель: Floor Wash — режим «сухой уборки» (dry_flag).
- Переключатель: Close On Offline — закрывать клапан при оффлайне.
- Селекты: Line N Type (1..4) — тип линий (sensor/counter).
- Сенсоры счетчиков: Line N Counter (м³), Line N Counter Step (L/pulse).
- Number: Line N Counter (set) — установка значения счетчика в литрах.
- Number: Line N Step (set) — установка шага счетчика (L/pulse).
- Сенсоры состояния: Module Status (текст), Module RSSI (%), Frame Interval (s), Device Time.
- Кнопка: Set Device Time — выставить время устройства сейчас.
- Бинарные сенсоры: Leak Detected; Line N Leak (1..4); Module/Sensors Battery; Sensors Online; Module Online; Valve Open.
- Number: Module Lost Timeout (сек) — настройка таймаута оффлайна модуля.

Схема топиков MQTT (основное)
- База устройства: `neptun/<MAC>/...`
- Состояния: `state/*`, `settings/*`, `counters/*`, `lines_status/*`.
- Команды из HA: `neptun/<MAC>/cmd/**/set` (клапан, dry_flag, close_on_offline, счетчики, время и т.д.).
- Diagnostic raw: `neptun/<MAC>/raw/*` (hex, base64, by_type) — для отладки.

Переменные окружения (для справки)
- `NB_TOPIC_PREFIX` (по умолчанию `neptun`)
- `NB_DISCOVERY_PREFIX` (по умолчанию `homeassistant`)
- `NB_CLOUD_PREFIX` (обычно пусто: autodetect)
- `NB_MQTT_PORT`, `NB_MQTT_USER`, `NB_MQTT_PASS` — локальный брокер аддона
- `NB_RETAIN`, `NB_DEBUG`, `NB_WATCHDOG_PERIOD`, `NB_MODULE_LOST_TIMEOUT`

Отладка и советы
- Загляните в Логи аддона: видно параметры старта и публикации Discovery.
- Проверьте, что локальный порт слушает (по умолчанию 2883) и правило NAT активно.
- Если используете аутентификацию локального брокера — задайте `mqtt.user/password` и прокиньте их в модуль (если требуется).
- При проблемах с Discovery убедитесь, что аддон видит первый входящий кадр (иначе сущности не публикуются).

