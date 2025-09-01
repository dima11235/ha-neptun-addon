# Neptun Local Bridge (HA add-on)

Поднимает локальный Mosquitto и Python-мост для Neptun:
- подписка на `14cb98a541/<MAC>/from`, парсинг бинарных пакетов;
- публикация в `neptun/<MAC>/...` читаемых топиков;
- MQTT Discovery для Home Assistant (клапан, датчики, счётчики);
- команды открытия/закрытия через `neptun/<MAC>/cmd/valve/set`.

## Установка
1. В HA → Settings → Add-ons → Add-on Store → меню (⋮) → Repositories → добавьте URL репозитория.
2. Установите **Neptun Local Bridge** → Configure:
   - `listen_port`: 1883
   - при необходимости `user/password`
   - `cloud_prefix`: `14cb98a541`
3. Запустите аддон, убедитесь что Neptun подключается к этому брокеру.
4. В HA → Settings → Devices & Services → MQTT:
   - укажите подключение к `mqtt://<IP_HA>:1883` и логин/пароль из аддона.
5. Устройства появятся автоматически через MQTT Discovery.

## Примечания
- Для перехвата облака может потребоваться DNS-override на Neptun (например, прописать в роутере, чтобы `mqtt.sstcloud...` резолвился на IP HA), если устройство жёстко ходит на имя хоста.
- Топики `neptun/<MAC>/state/valve_open` дают состояние клапана (1/0).
- Командный топик: `neptun/<MAC>/cmd/valve/set` с payload `1/0` (или `ON/OFF`, `OPEN/CLOSE`).
