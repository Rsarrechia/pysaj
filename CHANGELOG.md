# Changelog

All notable changes to this fork are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [0.1.1] - unreleased

### Changed
- Modern packaging: `pyproject.toml` (PEP 621) replaces `setup.py`; test
  dependencies moved to a `test` extra; added a GitHub Actions test workflow.
- `pysaj.__version__` is now derived from the installed package metadata.

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
