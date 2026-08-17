# pysaj

This library was created to communicate with SAJ solar inverters within Home Assistant.
It is based on the pysma component written by @kellerza

## Sensors

`Sensors(wifi=True)` reads the WiFi module's `status/status.php`; `Sensors(wifi=False)`
reads the ethernet interface's `real_time_data.xml`. A sensor is only reported as
`enabled` once the inverter has actually returned a value for it, so channels a
particular inverter does not have (a third PV string, unused string currents) never
show up.

| Sensor | Unit | Interface |
| --- | --- | --- |
| `current_power` | W | both |
| `today_yield`, `total_yield` | kWh | both |
| `today_time`, `total_time` | h | both |
| `total_co2_reduced` | kg | both |
| `temperature` | °C | both |
| `state` | | both |
| `today_max_current` | W | ethernet |
| `pv1_voltage` … `pv3_voltage` | V | WiFi ✓ · LAN pending¹ |
| `pv1_current` … `pv3_current` | A | WiFi ✓ · LAN pending¹ |
| `pv1_string1_current` … `pv3_string4_current` | A | WiFi ✓ · LAN pending¹ |
| `grid_frequency` | Hz | WiFi ✓ · LAN pending¹ |
| `line1_voltage` … `line3_voltage` | V | WiFi ✓ · LAN pending¹ |
| `line1_current` … `line3_current` | A | WiFi ✓ · LAN pending¹ |
| `bus_voltage` | V | WiFi ✓ · LAN pending¹ |

¹ These values are **not WiFi-exclusive** — the inverter measures them regardless of
how you connect. They are currently wired up and verified only for the WiFi interface,
where field positions, scaling and units come from the module's own `status.html`
(its `cf` array) and `/i18n/en/status.xml` (its `u<N>` entries). The ethernet/LAN
interface exposes the same values in `real_time_data.xml`, but under different XML tag
names that have not been confirmed against a device (for example, PV1 appears to be
`<pv1-v>` / `<pv1-c>`). Adding LAN support is a matter of filling in each sensor's
`key` with the correct XML tag — contributions from anyone with a LAN-connected SAJ
inverter are welcome. Until then these sensors simply stay disabled on the LAN
interface rather than reporting wrong values.

## Validation

The WiFi module returns malformed records - truncated bodies, wait markers, zeroed
registers while it is starting up. Its own web UI throws these away
(`if(35!=s.length){return;}`); this library does the same, because a partial record
otherwise yields plausible looking numbers read from the wrong field positions.

A record is only applied when:

* it is a single line whose field count matches a known layout (35, or 23 for older
  firmware),
* the daily counters do not exceed their lifetime counters,
* each reading falls inside that sensor's plausible range.

`65535` is the module's not-available sentinel and never becomes a reading.

Readings taken while the inverter's run state is not `Normal` (waking up, waiting,
error) are not trusted: live values become `None` and the counters keep their last
known value.

Lifetime counters (`total_yield`, `total_time`, `total_co2_reduced`) may never
decrease. Home Assistant reads them as `total_increasing`, so a single low sample is
taken as a meter reset and the whole counter is then booked as fresh production.
Implausibly large forward jumps are held until several consecutive readings agree, so
a corrupt high value cannot become the new baseline either.
