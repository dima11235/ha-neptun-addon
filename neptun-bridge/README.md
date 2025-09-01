# Neptun Local Bridge (Home Assistant add-on)

Мини‑аддон, который поднимает локальный Mosquitto без аутентификации и парсит пакеты Neptun, а затем бриджит нужные топики в стандартный MQTT Home Assistant с аутентификацией. Сущности в HA создаются через MQTT Discovery.

Что делает аддон
- Поднимает локальный MQTT на настраиваемом порту (по умолчанию 2883), без аутентификации.
- Принимает кадры от Neptun в `CLOUD_PREFIX/<MAC>/from` и публикует разобранные данные в `TOPIC_PREFIX/<MAC>/...`.
- Через Mosquitto bridge пересылает в HA брокер Discovery и состояния; команды из HA возвращает на локальный брокер.
- Создает в HA: переключатель клапана, датчики протечки и показания счетчиков (в т.ч. литры) через MQTT Discovery.

Установка
- Добавьте репозиторий аддонов и установите “Neptun Local Bridge”.
- В конфигурации аддона укажите параметры (см. ниже). Обычно достаточно задать учетные данные HA MQTT.
- Запустите аддон. Сущности появятся автоматически (MQTT Discovery должно быть включено в HA).

Параметры (options)
```
mqtt:
  listen_port: 2883          # Порт локального брокера внутри аддона
  allow_anonymous: true      # Разрешить анонимные подключения (по умолчанию true)
  user: ""                   # Опционально: пользователь локального брокера
  password: ""               # Опционально: пароль локального брокера
ha_mqtt:
  host: core-mosquitto       # Брокер HA (по умолчанию core-mosquitto)
  port: 1883                 # Порт брокера HA
  user: "!secret mqtt_user"  # Пользователь HA MQTT (поддерживается !secret)
  password: "!secret mqtt_password"  # Пароль HA MQTT (поддерживается !secret)
bridge:
  cloud_prefix: "14cb98a541" # Префикс «облачных» топиков устройства
  topic_prefix: "neptun"     # Префикс публикаций состояния/команд
  discovery_prefix: "homeassistant"  # Префикс MQTT Discovery
  retain: true               # Retain по умолчанию для публикаций
  debug: false               # Дополнительный лог в stderr
```

Secrets
- В полях `mqtt.user/password` и `ha_mqtt.user/password` можно использовать `!secret KEY`.
- Значения читаются из файла `/config/secrets.yaml` (развертывание через Supervisor; у аддона есть доступ к `/config`).
- Пример `secrets.yaml`:
```
mqtt_user: myuser
mqtt_password: mypass
```

Сетевое взаимодействие
- Локальный Mosquitto слушает `listen_port` (по умолчанию 2883) внутри контейнера аддона.
- Бридж в HA настраивается автоматически при наличии `ha_mqtt.user/password` и публикует:
  - Исходящие в HA: `<topic_prefix>/#` и `<discovery_prefix>/#`.
  - Входящие из HA в локальный: `<topic_prefix>/+/cmd/#` (команды для устройства).
- Рекомендуемый хост HA MQTT: `core-mosquitto`; порт: `1883`.

Основные топики
- Вход устройства: `CLOUD_PREFIX/<MAC>/from` (сырые кадры Neptun).
- Публикации состояния: `TOPIC_PREFIX/<MAC>/**` (сенсоры, счетчики, состояния).
- Команды из HA: `TOPIC_PREFIX/<MAC>/cmd/valve/set` со значениями `1/0` (`ON/OFF`, `OPEN/CLOSE`).
- MQTT Discovery публикуется под `DISCOVERY_PREFIX/**`.

Примечания
- Для работы без стороннего брокера устройство Neptun должно уметь подключаться к локальному брокеру аддона.
- Если локальный брокер нужно защитить, задайте `mqtt.user/password` и `allow_anonymous: false`.
- Отладка: включите `bridge.debug: true` – аддон начнет печатать подробные сообщения в лог.

