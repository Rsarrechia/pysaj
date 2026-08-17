# SAJ Solar Inverter — Home Assistant integration

A Home Assistant custom integration for **SAJ solar inverters**, over either the
WiFi module (`status.php`) or the ethernet/LAN interface (`real_time_data.xml`).
It is a maintained fork of the built-in `saj` integration and the
[`pysaj`](https://github.com/Rsarrechia/pysaj) library, with fixes and extra
sensors.

> This branch contains the **Home Assistant integration**. The `pysaj` **library**
> it depends on lives on the [`master`](https://github.com/Rsarrechia/pysaj/tree/master)
> branch and is pulled in automatically via the manifest.

## What's fixed / added

- **No more `total_yield` spike.** Lifetime counters can't go backwards, so a bad
  reading is no longer booked by Home Assistant as a full counter's worth of
  production in one hour.
- **Start-up parsing hardened** — malformed/partial records are discarded instead
  of misparsed.
- **Extra sensors** the inverter already reports: PV string voltages/currents,
  per-phase AC voltage/current, grid frequency, and DC bus voltage. (Currently
  verified over WiFi; see the library README for the LAN status.)

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories**.
2. Add `https://github.com/Rsarrechia/pysaj` with category **Integration**.
3. Install **SAJ Solar Inverter**, then restart Home Assistant.

### Manual

Copy `custom_components/saj/` into your Home Assistant `config/custom_components/`
directory and restart.

## Configuration

Settings → **Devices & Services** → **Add Integration** → **SAJ Solar Inverter**.

- **Host** — the inverter's IP address or hostname.
- **Connection type** — `wifi` or `ethernet`.
- **Username / password** — only for WiFi modules that require a login (optional).

Sensors are created automatically for whatever the inverter reports.

## Note on the `saj` domain

This integration uses the domain `saj`, which is the same as the built-in Home
Assistant `saj` integration. A custom integration takes precedence over the
built-in one, so installing this **replaces** the built-in `saj` on your system
(that is the point — it's the maintained version). Home Assistant will log the
usual "custom integration which has not been tested" notice; that is expected.

## Credits & license

Based on [fredericvl/pysaj](https://github.com/fredericvl/pysaj) and the original
Home Assistant `saj` integration, itself derived from work by kellerza.
Licensed under the MIT License (see `LICENSE`).
