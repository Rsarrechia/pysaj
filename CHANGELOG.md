# Changelog

All notable changes to this fork are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- A cumulative counter's growth allowance is never smaller than one step of its
  own resolution. `total_time` is `/10` scaled with a 1.2h/h limit, so at any
  poll interval below five minutes every normal 0.1h increment was held back as
  an implausible jump and logged as one.
- Warnings raised per record are throttled: the inverter run state, out of range
  fields, accepted jumps, malformed records and daily-above-lifetime rejections.
  Throttle state is keyed per message, so one recurring rejection no longer
  suppresses a different one or inflates its suppressed count.
- A sensor whose field is present but whose value is unusable (`grid_frequency`
  reads 0Hz until the module synchronises) is exposed again, so the consumer
  still creates its entity when the first read lands in such a window. Channels
  the inverter reports as not available (`65535`) stay hidden.

### Note
- Since 0.1.0 the Ethernet/XML path applies each sensor's `factor`, where it
  previously stored the raw text. Values from `real_time_data.xml` are now
  scaled the same way as the WiFi ones (e.g. temperature `250` reads as 25.0).

## [0.1.1] - 2026-08-17

### Changed
- Modern packaging: `pyproject.toml` (PEP 621) replaces `setup.py`; test
  dependencies moved to a `test` extra; added a GitHub Actions test workflow.
- `pysaj.__version__` is now derived from the installed package metadata.
- Tests no longer depend on `aioresponses` (which does not support current
  `aiohttp`); the HTTP layer is mocked directly, so the suite runs green
  against the `aiohttp` Home Assistant ships.

## [0.1.0]

### Fixed
- Lifetime counters (`total_yield`, `total_time`, `total_co2_reduced`) can no
  longer decrease, and implausible jumps are held until confirmed. This fixes a
  Home Assistant meter-reset spike where a single bad reading was booked as a
  full counter's worth of production in one hour.
- Malformed status records (wrong field count, wait markers, multi-line,
  partial) are discarded instead of being indexed into and misparsed.
- Negative temperatures are read correctly (previously discarded due to a
  `name`/`key` mix-up).

### Added
- `65535` is honoured as the module's not-available sentinel; per-sensor range
  checks; readings are ignored while the inverter run state is not `Normal`.
- 26 previously dropped WiFi channels: PV string voltages/currents, grid
  frequency, per-phase AC voltage/current, and DC bus voltage.
- Throttling of repeated counter-rejection warnings (at most once per five
  minutes) so a persistent cause cannot flood the log.
- Test suite (`tests/test_pysaj.py`).

This fork is based on [fredericvl/pysaj](https://github.com/fredericvl/pysaj)
(last released as 0.0.16), itself derived from work by kellerza. Licensed MIT.
