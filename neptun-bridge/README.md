
## Neptun Local Bridge (дополнение для Home Assistant)

Локальный MQTT‑мост и парсер бинарных кадров Neptun AquaControl. Аддон работает рядом с Home Assistant: принимает кадры от устройства Neptun, проверяет целостность, раскладывает данные по понятным MQTT‑топикам и автоматически объявляет сущности в HA (MQTT Discovery). Команды из HA отправляются обратно на устройство через MQTT‑канал.

## Возможности
- Приём и разбор кадров Neptun: 0x52 (system_state), 0x53 (sensor_state), 0x57 (settings), 0x43 (counters) с проверкой CRC16‑CCITT.
- Публикация состояния в `neptun/<MAC>/**` и авто‑объявление сущностей в `homeassistant/**`.
- Управление из HA: клапан, «сухой режим» (Floor Wash), «закрывать при оффлайне», типы линий (sensor/counter), запись счётчиков и шага.
- Диагностика: «raw» кадры по типам/именам; подробные логи при `debug: true` или `NB_DEBUG=true`.

## Требования
- Home Assistant с брокером MQTT (обычно `core-mosquitto`) для приёма Discovery и состояний.
- Возможность направить исходящее MQTT‑соединение устройства Neptun на локальный брокер аддона (порт по умолчанию 2883) либо настроить бридж/форвардинг на роутере.

## Установка
1. Добавьте репозиторий аддонов и установите «Neptun Local Bridge».
2. Откройте настройки аддона и укажите параметры (см. раздел «Параметры» ниже). В большинстве случаев достаточно задать учётные данные HA MQTT (`ha_mqtt.user/password`).
3. Запустите аддон. Сущности появятся автоматически (MQTT Discovery должно быть включено в HA).
4. Перенаправьте исходящий MQTT‑трафик устройства Neptun с облака на локальный брокер аддона:
   - Облако: `185.76.147.189:1883`
   - Локально: `IP_HA:<listen_port>` (по умолчанию `2883`)

Пример для Keenetic (CLI), где `192.168.1.200` — IP Home Assistant:
```
ip static tcp 185.76.147.189/32 1883 192.168.1.200 2883
system configuration save
```
Убедитесь, что:
- адрес `IP_HA` доступен из подсети устройства Neptun;
- порт совпадает с `mqtt.listen_port` (если меняли дефолт `2883` — используйте его);
- при необходимости включены hairpin/loopback NAT или настроен policy‑based NAT.

## Параметры (options)
```
mqtt:
  listen_port: 2883          # Порт локального брокера аддона
  allow_anonymous: true      # Разрешить анонимные подключения (по умолчанию true)
  user: ""                   # (опционально) пользователь локального брокера
  password: ""               # (опционально) пароль локального брокера
ha_mqtt:
  host: "core-mosquitto"     # Брокер HA (по умолчанию core-mosquitto)
  port: 1883                 # Порт брокера HA
  user: "!secret mqtt_username"      # Пользователь HA MQTT (!secret поддерживается)
  password: "!secret mqtt_password"  # Пароль HA MQTT (!secret поддерживается)
bridge:
  cloud_prefix: ""           # Пусто — автообнаружение; если задан — используется принудительно
  topic_prefix: "neptun"     # Базовый префикс локальных топиков состояния/команд
  discovery_prefix: "homeassistant"  # Префикс MQTT Discovery
  retain: true               # Retain по умолчанию для публикаций
  debug: false               # Дополнительный лог в stderr
```
Поддерживаются секреты HA (`/config/secrets.yaml`):
```
mqtt_username: myuser
mqtt_password: mypass
mqtt_server: mqtt://core-mosquitto:1883
```

## Переменные окружения (в контейнере)
- `NB_TOPIC_PREFIX` (по умолчанию `neptun`)
- `NB_DISCOVERY_PREFIX` (по умолчанию `homeassistant`)
- `NB_CLOUD_PREFIX` (если известен заранее; иначе мост выучит по входящим кадрам)
- `NB_RETAIN` (`true`/`false`), `NB_DEBUG` (`true`/`false`)

## Как это работает (топики)
- Вход устройства: `CLOUD_PREFIX/<MAC>/from` — сырые бинарные кадры Neptun.
- Публикации состояния: `neptun/<MAC>/**` — сенсоры, счётчики, статусы.
- Команды из HA: `neptun/<MAC>/cmd/**` — настройки, клапан, линии, счётчики.
- Авто‑объявления HA: `homeassistant/**` (или ваш `discovery_prefix`).

Если `cloud_prefix: ""`, аддон подписывается на `+/+/from` и автоматически определяет префикс и MAC по первому входящему сообщению. Команды на устройство отправляются на `<prefix>/<MAC>/to`. Пока префикс не определён, команды откладываются до первого кадра.

## Сущности Home Assistant
Управление (switch/select/number):
- Switch `Valve` — открыть/закрыть кран
  - cmd: `neptun/<mac>/cmd/valve/set` (`1`/`0`), state: `neptun/<mac>/state/valve_open`
- Switch `Floor Wash` (бывш. Dry Flag) — включает/выключает «сухой режим»
  - cmd: `neptun/<mac>/cmd/dry_flag/set` (`on`/`off`), state: `neptun/<mac>/settings/dry_flag`
- Switch `Close On Offline` — закрывать кран при потере датчиков
  - cmd: `neptun/<mac>/cmd/close_on_offline/set` (`close`/`open`), state: `neptun/<mac>/settings/close_valve_flag`
- Select `Line i Type` (i=1..4) — тип входа линии (sensor/counter)
  - cmd: `neptun/<mac>/cmd/line_i_type/set`, state: `neptun/<mac>/settings/lines_in/line_i`
- Number (mode=box) `Line i Counter (set)` — установка счётчика в литрах
  - cmd: `neptun/<mac>/cmd/counters/line_i/value/set`, state: `neptun/<mac>/counters/line_i/value`
- Number (mode=box) `Line i Step (set)` — шаг в L/импульс
  - cmd: `neptun/<mac>/cmd/counters/line_i/step/set`, state: `neptun/<mac>/counters/line_i/step`

Сенсоры (read‑only):
- Проводные утечки `Line i Leak` (i=1..4): `neptun/<mac>/lines_status/line_i`
- Беспроводные датчики (динамически): Battery, RSSI, Leak
- Счётчики воды: литры `neptun/<mac>/counters/line_i/value` + производный sensor м³ (через value_template)
- `Module Status` (текст): `neptun/<mac>/state/status_name`
- `Module Alert` (binary, problem): `neptun/<mac>/settings/status/module_alert` (yes/no — любой проблемный бит)
- `Module Lost` (binary, problem): `neptun/<mac>/settings/status/module_lost` — yes, если нет данных > 120 секунд
- `Device Time` (timestamp): `neptun/<mac>/device_time` (TLV 0x44, локальное время устройства, конвертируется в ISO UTC)
- `Device Time Drift` (diagnostic): `neptun/<mac>/device_time_drift_seconds` — разница «локальные часы хоста − время устройства», в секундах

## Команды на устройство (MQTT)
- Клапан: `neptun/<mac>/cmd/valve/set` → `1`/`0`
- Сухой режим: `neptun/<mac>/cmd/dry_flag/set` → `on`/`off`
- Закрыть при оффлайне: `neptun/<mac>/cmd/close_on_offline/set` → `close`/`open`
- Тип линии: `neptun/<mac>/cmd/line_<i>_type/set` → `sensor`/`counter`
- Счётчик (литры): `neptun/<mac>/cmd/counters/line_<i>/value/set` → целое значение L
- Шаг счётчика: `neptun/<mac>/cmd/counters/line_<i>/step/set` → 1..255 (L/импульс)

Для записи счётчиков мост формирует кадр 0x57/0x43 на `<cloud_prefix>/<MAC>/to`. Для неизменяемых линий подставляются последние известные «сырые» значения/шаги, чтобы ничего не обнулить.

## Протокол и обработка
- Поддерживаемые кадры: 0x52 (system_state), 0x53 (sensor_state), 0x57 (settings), 0x43 (counters).
- TLV 0x44 — «время устройства» приходит в ЛОКАЛЬНОМ времени устройства. Мост конвертирует его в ISO UTC (с учётом локального часового пояса хоста) и публикует также epoch.
- Module Lost: если присутствует TLV 0x44 — сравнение делается с локальным временем; если 0x44 нет — по времени получения кадра. Порог — 120 секунд.
- Батарея беспроводных датчиков: значения > 200 (похоже на «вольтовую шкалу») отбрасываются и не публикуются; валидные проценты 0..100 — публикуются.

## Диагностика
- Включите `debug: true` (или `NB_DEBUG=true`) — мост начнёт печатать отладочные сообщения и шире подписываться на топики.
- «Raw» данные публикуются в:
  - `neptun/<mac>/raw/*` (hex/base64/len)
  - группировки по типу/имени: `neptun/<mac>/raw/by_type/*`, `neptun/<mac>/raw/by_name/*`

## Ограничения и примечания
- Настройка Wi‑Fi самого Neptun НЕ выполняется через MQTT. Используйте штатное приложение/режим точки доступа.
- Если в HA видите старые сущности после обновления — перезапустите интеграцию MQTT или дождитесь обновления Discovery.

## Частые вопросы
- «Module Lost» срабатывает слишком рано?
  — Порог увеличен до 120 секунд. Если 0x44 отсутствует или часы устройства «уехали» в будущее > 5 минут, используется время получения кадра.
- Батарея «прыгает» между ~24% и ~240%?
  — Значения > 200 считаются «вольтовой шкалой» и не публикуются; остаются стабильные проценты 0..100.
- Не вижу switch для клапана, а только две кнопки?
  — В новых версиях используется один switch `Valve`. Старые кнопки удаляются Discovery‑публикацией с пустым payload.

## История версий (кратко)
- 0.1.48 — новая иконка Module Lost; уточнения в документации.
- 0.1.47 — порог Module Lost 120 секунд; обновление README.
- 0.1.45–0.1.46 — корректная обработка локального `device_time` (TLV 0x44); фиксы устаревших API datetime.
- 0.1.44 — `Module Status` (текст) и `Module Alert` (binary).
- 0.1.41–0.1.43 — `Module Lost`, `Device Time Drift`, улучшения диагностики.
- 0.1.36–0.1.39 — switch `Valve`, Floor Wash (бывш. Dry Flag), number (mode=box) для счётчика/шага, типы линий (select).

- 0.1.62 - Add Set Device Time button (HA) and support for neptun/<MAC>/cmd/time/set; treat naive datetimes as local time.
 - 0.1.63 - Fix device_time publishing and avoid retained time-set command; extrapolate when TLV 0x44 is absent.
 - 0.1.64 - Remove device_time extrapolation and optimistic publish; only publish time when provided by device.
 - 0.1.65 - Send time set as local ASCII "DD/MM/YYYY,HH:MM:SS" (TLV 0x44) per device expectation.
 - 0.1.66 - Subscribe to both `<prefix>/+/from` and `<prefix>/+/to` to handle device frames published under `/to`.
 - 0.1.67 - Make time-set command retained (device clears `/to` after processing) to ensure delivery after reconnect.
 - 0.1.68 - Fix duplicate `device_time` publishes (local tz vs UTC) causing +offset jumps; publish once (UTC ISO) only when device provides TLV 0x44.
