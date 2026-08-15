# pysaj

This library was created to communicate with SAJ solar inverters within Home Assistant.
It is based on the pysma component written by @kellerza

## Sensors

`Sensors(wifi=True)` reads the WiFi module's `status/status.php`; `Sensors(wifi=False)`
reads the ethernet interface's `real_time_data.xml`. A sensor is only reported as
`enabled` once the inverter has actually returned a value for it, so channels the
hardware does not have (a third PV string, per-string currents) never show up.

| Sensor | Unit | Interface |
| --- | --- | --- |
| `current_power` | W | both |
| `today_yield`, `total_yield` | kWh | both |
| `today_time`, `total_time` | h | both |
| `total_co2_reduced` | kg | both |
| `temperature` | °C | both |
| `state` | | both |
| `today_max_current` | W | ethernet |
| `pv1_voltage` … `pv3_voltage` | V | wifi |
| `pv1_current` … `pv3_current` | A | wifi |
| `pv1_string1_current` … `pv3_string4_current` | A | wifi |
| `grid_frequency` | Hz | wifi |
| `line1_voltage` … `line3_voltage` | V | wifi |
| `line1_current` … `line3_current` | A | wifi |
| `bus_voltage` | V | wifi |

Field positions, scaling and units for the WiFi channels come from the module's own
`status.html` (its `cf` array) and `/i18n/en/status.xml` (its `u<N>` entries).

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
