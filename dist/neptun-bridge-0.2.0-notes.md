# Neptun ProW+WiFi Local Bridge 0.2.0

## Highlights
- Home Assistant discovery now updates icons and colors dynamically for module status, leak sensors, valve state, RSSI buckets, and per-sensor batteries.
- Reliability upgrades: Floor Wash, valve, and Close On Offline switches use retries with cached state handling and retain flags to avoid flicker and lost commands.
- Wireless telemetry was refactored for cleaner parsing and publishing, keeping sensor topics consistent.
- Line type and counter writes now reuse helpers to ensure consistent Home Assistant state updates.
- Default pending-hold window for command retries increased to 60 seconds; tune with NB_PENDING_HOLD_SEC if required.

## Detailed Changes
- Added icon_color/icon attributes for module, leak, valve, sensor RSSI, and per-sensor battery entities, plus runtime discovery refresh when values change.
- Changed RSSI and signal icons to bucket-based variants that reflect strength accurately.
- Published discovery for valve_closed, module_lost, and sensors_lost with dynamic icons and icon_color attributes.
- Enabled retain flag on key switches so state survives Home Assistant restarts.
- Introduced retry logic and helper consolidation for valve, Floor Wash, Close On Offline, line types, and counter updates to reduce state drift.
- Added anti-flicker protection for valve and Floor Wash topics when devices echo data slowly.
- Tweaked module alert coloring for better visibility.

## Upgrade Notes
- Restart the add-on after updating to reload the new discovery payloads.
- If you override NB_PENDING_HOLD_SEC, check whether the new 60 second default suits your setup.
- Allow a few minutes after upgrade for Home Assistant to refresh entity icons via discovery.
