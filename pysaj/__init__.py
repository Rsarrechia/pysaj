"""PySAJ interacts as a library to communicate with SAJ inverters"""

import asyncio
import csv
from datetime import date, datetime, timezone
from io import StringIO
import logging
import xml.etree.ElementTree as ET

import aiohttp

try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    __version__ = _pkg_version("pysaj")
except (ImportError, PackageNotFoundError):  # pragma: no cover
    __version__ = "0.0.0"

_LOGGER = logging.getLogger(__name__)

MAPPER_STATES = {
    "0": "Not connected",
    "1": "Waiting",
    "2": "Normal",
    "3": "Error",
    "4": "Upgrading",
}

STATE_NORMAL = "Normal"

URL_PATH_ETHERNET = "real_time_data.xml"
URL_PATH_ETHERNET_INFO = "equipment_data.xml"
URL_PATH_WIFI = "status/status.php"
URL_PATH_WIFI_INFO = "info.php"

# The inverter's own web UI (validv() in its script.js) renders 65535 as "N/A"
# for every field, so it is the module's not-available sentinel rather than a
# reading. Without this a missing channel decodes to e.g. 655.35 kWh.
NOT_AVAILABLE = 65535

# status.php returns exactly one record whose field count identifies the
# layout. The module's own status.html refuses anything that is not 35 fields
# ("if(35!=s.length){return;}"); the 23 field layout is the older firmware the
# csv_1_key indices were written for. Anything else is a malformed record and
# is thrown away instead of being indexed into.
WIFI_LAYOUTS = {35: "csv_2_key", 23: "csv_1_key"}

# A cumulative counter may not grow faster than max_rate units per hour. Never
# allow less than this much elapsed time, so that a fast poll cycle does not
# shrink the allowance to zero.
MIN_ELAPSED_HOURS = 1.0 / 60

# How many consecutive, mutually consistent readings are needed before an
# implausible jump in a cumulative counter is believed. This keeps a single
# corrupt high reading from being latched as the new baseline, which would
# otherwise reject every real reading that follows it.
CONFIRM_READINGS = 3

# Slack allowed when checking a daily counter against its lifetime counter.
CONSISTENCY_TOLERANCE = 1.0

# A rejected counter usually repeats on every poll, so only warn about it
# occasionally and report how many were suppressed in between.
WARN_INTERVAL_SECONDS = 300


def _apply_factor(num, factor):
    """Scale a raw reading. Only "/N" and "*N" factors are supported."""
    if not factor:
        return num

    operator, operand = factor[0], factor[1:]
    try:
        scale = float(operand)
    except ValueError as err:
        raise ValueError(f"Unsupported sensor factor {factor!r}") from err

    if operator == "/":
        return num / scale
    if operator == "*":
        return num * scale
    raise ValueError(f"Unsupported sensor factor {factor!r}")


def _coerce(sen, raw):
    """Turn one raw field into a validated reading, or None if unusable."""
    text = raw.strip()
    if not text:
        return None

    try:
        num = float(text)
    except ValueError:
        _LOGGER.debug("Sensor %s: discarding non-numeric value %r", sen.name, text)
        return None

    if num == NOT_AVAILABLE:
        # Channel the inverter does not have (e.g. a third PV string).
        return None

    try:
        num = _apply_factor(num, sen.factor)
    except ValueError:
        _LOGGER.error("Sensor %s has an invalid factor %r", sen.name, sen.factor)
        return None

    if sen.min_value is not None and num < sen.min_value:
        _LOGGER.warning(
            "Discarding %s reading %s%s: below the plausible minimum of %s",
            sen.name,
            num,
            sen.unit,
            sen.min_value,
        )
        return None
    if sen.max_value is not None and num > sen.max_value:
        _LOGGER.warning(
            "Discarding %s reading %s%s: above the plausible maximum of %s",
            sen.name,
            num,
            sen.unit,
            sen.max_value,
        )
        return None

    return num


def _warn_throttled(sen, now, message, *args):
    """Warn about a repeating rejection without flooding the log.

    The trailing "%s" in `message` receives a note about how many identical
    rejections were suppressed since the previous warning.
    """
    last = sen.last_warning
    if last is not None and (now - last).total_seconds() < WARN_INTERVAL_SECONDS:
        sen.suppressed_warnings += 1
        return

    if sen.suppressed_warnings:
        note = f" ({sen.suppressed_warnings} identical since the last message)"
    else:
        note = ""
    _LOGGER.warning(message, *args, note)
    sen.last_warning = now
    sen.suppressed_warnings = 0


def _accept_cumulative(sen, new, now):
    """Decide whether a lifetime counter may move to `new`.

    Lifetime counters must never go backwards: Home Assistant reads them as
    total_increasing, so a single low reading is taken as a meter reset and the
    whole counter is then booked as fresh production. Implausibly large forward
    jumps are held back until several readings agree, so that a corrupt high
    value cannot become the new baseline either.
    """
    old = sen.value
    if old is None:
        return True

    if new < old:
        _warn_throttled(
            sen,
            now,
            "Ignoring %s going backwards (%s -> %s%s). Keeping the previous value; "
            "a decrease would be read as a meter reset%s",
            sen.name,
            old,
            new,
            sen.unit,
        )
        return False

    if sen.max_rate is None:
        return True

    elapsed = MIN_ELAPSED_HOURS
    if sen.last_update is not None:
        elapsed = max(
            (now - sen.last_update).total_seconds() / 3600, MIN_ELAPSED_HOURS
        )

    if new - old <= sen.max_rate * elapsed:
        sen.pending_value = None
        sen.pending_count = 0
        return True

    if sen.pending_value is not None and new >= sen.pending_value:
        sen.pending_count += 1
    else:
        sen.pending_value = new
        sen.pending_count = 1

    if sen.pending_count < CONFIRM_READINGS:
        _warn_throttled(
            sen,
            now,
            "Holding back an implausible jump for %s (%s -> %s%s in %.1f min); "
            "%s of %s confirmations%s",
            sen.name,
            old,
            new,
            sen.unit,
            elapsed * 60,
            sen.pending_count,
            CONFIRM_READINGS,
        )
        return False

    _LOGGER.warning(
        "Accepting jump for %s (%s -> %s%s) after %s consistent readings",
        sen.name,
        old,
        new,
        sen.unit,
        sen.pending_count,
    )
    sen.pending_value = None
    sen.pending_count = 0
    return True


def _record_is_consistent(staged):
    """Reject a record whose daily counters exceed their lifetime counters."""
    for day_name, total_name in (
        ("today_yield", "total_yield"),
        ("today_time", "total_time"),
    ):
        day = staged.get(day_name)
        total = staged.get(total_name)
        if day is None or total is None:
            continue
        if day > total + CONSISTENCY_TOLERANCE:
            _LOGGER.warning(
                "Discarding record: %s (%s) exceeds %s (%s)",
                day_name,
                day,
                total_name,
                total,
            )
            return False
    return True


class Sensor(object):
    """Sensor definition"""

    def __init__(
        self,
        key,
        csv_1_key,
        csv_2_key,
        factor,
        name,
        unit="",
        per_day_basis=False,
        per_total_basis=False,
        min_value=None,
        max_value=None,
        max_rate=None,
    ):
        self.key = key
        self.csv_1_key = csv_1_key
        self.csv_2_key = csv_2_key
        self.factor = factor
        self.name = name
        self.unit = unit
        self.value = None
        self.per_day_basis = per_day_basis
        self.per_total_basis = per_total_basis
        self.date = date.today()
        self.enabled = False

        # Plausibility limits applied to the scaled reading.
        self.min_value = min_value
        self.max_value = max_value
        # Maximum growth per hour, for cumulative counters only.
        self.max_rate = max_rate

        # Bookkeeping for the guards.
        self.last_update = None
        self.pending_value = None
        self.pending_count = 0
        self.last_warning = None
        self.suppressed_warnings = 0

        # Decimals implied by the factor, for display purposes.
        self.precision = 0
        if factor.startswith("/"):
            try:
                self.precision = max(0, len(str(int(float(factor[1:])))) - 1)
            except ValueError:
                self.precision = 0


class Sensors(object):
    """SAJ sensors"""

    def __init__(self, wifi=False):
        self.__s = []
        self.add(
            (
                Sensor("p-ac", 11, 23, "", "current_power", "W", False, False, 0, 200000),
                Sensor(
                    "e-today", 3, 3, "/100", "today_yield", "kWh", True, False, 0, 2000
                ),
                Sensor(
                    "e-total",
                    1,
                    1,
                    "/100",
                    "total_yield",
                    "kWh",
                    False,
                    True,
                    0,
                    100000000,
                    200,
                ),
                Sensor("t-today", 4, 4, "/10", "today_time", "h", True, False, 0, 24),
                Sensor(
                    "t-total",
                    2,
                    2,
                    "/10",
                    "total_time",
                    "h",
                    False,
                    True,
                    0,
                    1000000,
                    1.2,
                ),
                Sensor(
                    "CO2",
                    21,
                    33,
                    "/10",
                    "total_co2_reduced",
                    "kg",
                    False,
                    True,
                    0,
                    100000000,
                    200,
                ),
                Sensor("temp", 20, 32, "/10", "temperature", "°C", False, False, -40, 120),
                Sensor("state", 22, 34, "", "state"),
            )
        )

        if wifi:
            # Channels the WiFi module reports that were previously dropped.
            # Field positions and scaling come from the module's own status.html
            # (its `cf` array) and /i18n/en/status.xml (its `u<N>` units). They
            # only exist in the 35 field layout, hence csv_1_key = -1.
            self.add(
                (
                    Sensor("PV1vol", -1, 5, "/10", "pv1_voltage", "V", False, False, 0, 1500),
                    Sensor("PV1cur", -1, 6, "/100", "pv1_current", "A", False, False, 0, 100),
                    Sensor("PV2vol", -1, 7, "/10", "pv2_voltage", "V", False, False, 0, 1500),
                    Sensor("PV2cur", -1, 8, "/100", "pv2_current", "A", False, False, 0, 100),
                    Sensor("PV3vol", -1, 9, "/10", "pv3_voltage", "V", False, False, 0, 1500),
                    Sensor("PV3cur", -1, 10, "/100", "pv3_current", "A", False, False, 0, 100),
                )
            )
            self.add(
                tuple(
                    Sensor(
                        f"PV{pv}StrCurr{string}",
                        -1,
                        11 + (pv - 1) * 4 + (string - 1),
                        "/100",
                        f"pv{pv}_string{string}_current",
                        "A",
                        False,
                        False,
                        0,
                        100,
                    )
                    for pv in (1, 2, 3)
                    for string in (1, 2, 3, 4)
                )
            )
            self.add(
                (
                    Sensor(
                        "gridConFreq",
                        -1,
                        24,
                        "/100",
                        "grid_frequency",
                        "Hz",
                        False,
                        False,
                        30,
                        90,
                    ),
                    Sensor("line1vol", -1, 25, "/10", "line1_voltage", "V", False, False, 0, 500),
                    Sensor("line1cur", -1, 26, "/100", "line1_current", "A", False, False, 0, 500),
                    Sensor("line2vol", -1, 27, "/10", "line2_voltage", "V", False, False, 0, 500),
                    Sensor("line2cur", -1, 28, "/100", "line2_current", "A", False, False, 0, 500),
                    Sensor("line3vol", -1, 29, "/10", "line3_voltage", "V", False, False, 0, 500),
                    Sensor("line3cur", -1, 30, "/100", "line3_current", "A", False, False, 0, 500),
                    Sensor("busvol", -1, 31, "/10", "bus_voltage", "V", False, False, 0, 1500),
                )
            )
        else:
            # Only reported over the ethernet XML interface.
            self.add(
                Sensor(
                    "maxPower",
                    -1,
                    -1,
                    "",
                    "today_max_current",
                    "W",
                    True,
                    False,
                    0,
                    200000,
                )
            )

    def __len__(self):
        """Length."""
        return len(self.__s)

    def __contains__(self, key):
        """Get a sensor using either the name or key."""
        try:
            if self[key]:
                return True
        except KeyError:
            return False

    def __getitem__(self, key):
        """Get a sensor using either the name or key."""
        for sen in self.__s:
            if sen.name == key or sen.key == key:
                return sen
        raise KeyError(key)

    def __iter__(self):
        """Iterator."""
        return self.__s.__iter__()

    def add(self, sensor):
        """Add a sensor, warning if it exists."""
        if isinstance(sensor, (list, tuple)):
            for sss in sensor:
                self.add(sss)
            return

        if not isinstance(sensor, Sensor):
            raise TypeError("pysaj.Sensor expected")

        if sensor.name in self:
            old = self[sensor.name]
            self.__s.remove(old)
            _LOGGER.warning("Replacing sensor %s with %s", old, sensor)

        if sensor.key in self:
            _LOGGER.warning("Duplicate SAJ sensor key %s", sensor.key)

        self.__s.append(sensor)


class SAJ(object):
    """Provides access to SAJ inverter data"""

    def __init__(self, host, wifi=False, username="admin", password="admin"):
        self.host = host
        self.wifi = wifi
        self.username = username
        self.password = password
        self.serialnumber = "XXXXXXXXXXXXXXXXX"

        self.url = "http://{0}/".format(self.host)
        if self.wifi:
            if len(self.username) > 0 and len(self.password) > 0:
                self.url = "http://{0}:{1}@{2}/".format(
                    self.username, self.password, self.host
                )
            self.url_info = self.url + URL_PATH_WIFI_INFO
            self.url += URL_PATH_WIFI
        else:
            self.url_info = self.url + URL_PATH_ETHERNET_INFO
            self.url += URL_PATH_ETHERNET

    async def read(self, sensors):
        """Returns necessary sensors from SAJ inverter"""

        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(
                timeout=timeout, raise_for_status=True
            ) as session:
                current_url = self.url_info
                async with session.get(current_url) as response:
                    data = await response.text(encoding="latin1")
                    _LOGGER.debug("Info data received: %s", data)
                    self._read_info(data)

                current_url = self.url
                async with session.get(current_url) as response:
                    data = await response.text(encoding="latin1")
                    _LOGGER.debug("Data received: %s", data)

                    if self.wifi:
                        return self._read_wifi(data, sensors)
                    return self._read_ethernet(data, sensors)
        except (
            aiohttp.client_exceptions.ClientConnectorError,
            asyncio.TimeoutError,
            TimeoutError,
        ):
            # Connection to inverter not possible.
            # This can be "normal" - so warning instead of error - as SAJ
            # inverters are powered by DC and thus have no power after the sun
            # has set.
            _LOGGER.warning(
                "Connection to SAJ inverter is not possible. The inverter may be "
                "offline due to darkness. Otherwise check host/ip address"
            )
            return False
        except aiohttp.client_exceptions.ClientResponseError as err:
            # 401 Unauthorized: wrong username/password
            if err.status == 401:
                raise UnauthorizedException(err)
            raise UnexpectedResponseException(err)
        except csv.Error:
            # CSV is not valid
            raise UnexpectedResponseException(
                str.format(
                    "No valid CSV received from {0} at {1}", self.host, current_url
                )
            )
        except ET.ParseError:
            # XML is not valid or even no XML at all
            raise UnexpectedResponseException(
                str.format(
                    "No valid XML received from {0} at {1}", self.host, current_url
                )
            )

    def _read_info(self, data):
        """Pick the serial number out of the device info response."""
        if self.wifi:
            for row in csv.reader(StringIO(data)):
                if row and row[0].strip():
                    self.serialnumber = row[0].strip()
        else:
            xml = ET.fromstring(data)
            find = xml.find("SN")
            if find is not None and find.text:
                self.serialnumber = find.text.strip()

        _LOGGER.debug("Inverter SN: %s", self.serialnumber)

    def _split_wifi_record(self, data):
        """Split status.php into its fields, or None if the record is malformed.

        The module emits a single comma separated line. Anything else - a short
        or truncated body, a wait marker, an extra line - is discarded rather
        than indexed into, because a partial record silently yields plausible
        looking numbers at the wrong field positions.
        """
        rows = [row for row in csv.reader(StringIO(data)) if any(f.strip() for f in row)]
        if not rows:
            raise csv.Error("empty response")

        if len(rows) > 1:
            _LOGGER.warning(
                "Discarding status record: expected a single line, got %s", len(rows)
            )
            return None

        values = [field.strip() for field in rows[0]]
        if len(values) not in WIFI_LAYOUTS:
            _LOGGER.warning(
                "Discarding status record with %s fields, expected one of %s: %r",
                len(values),
                sorted(WIFI_LAYOUTS),
                data,
            )
            return None

        return values

    def _read_wifi(self, data, sensors):
        """Validate and apply one status.php record."""
        values = self._split_wifi_record(data)
        if values is None:
            return False

        index_attr = WIFI_LAYOUTS[len(values)]

        staged = {}
        for sen in sensors:
            index = getattr(sen, index_attr)
            if index < 0 or index >= len(values):
                continue
            staged[sen.name] = self._stage(sen, values[index])

        if not _record_is_consistent(staged):
            return False

        return self._commit(sensors, staged)

    def _read_ethernet(self, data, sensors):
        """Validate and apply one real_time_data.xml record."""
        xml = ET.fromstring(data)

        staged = {}
        for sen in sensors:
            find = xml.find(sen.key)
            if find is None or find.text is None:
                continue
            staged[sen.name] = self._stage(sen, find.text)

        if not _record_is_consistent(staged):
            return False

        if not any(value is not None for value in staged.values()):
            # Parsed as XML but contains none of our fields: wrong endpoint.
            raise ET.ParseError("no known sensor fields in response")

        return self._commit(sensors, staged)

    @staticmethod
    def _stage(sen, raw):
        """Validate one raw field without applying it yet."""
        if sen.name == "state":
            text = raw.strip()
            if not text:
                return None
            return MAPPER_STATES.get(text, f"Unknown({text})")
        return _coerce(sen, raw)

    @staticmethod
    def _commit(sensors, staged):
        """Apply a validated record to the sensors."""
        now = datetime.now(timezone.utc)
        today = date.today()

        state = staged.get("state")
        running = state is None or state == STATE_NORMAL
        if not running:
            _LOGGER.warning(
                "Inverter state is %s, not %s. Live readings are cleared and "
                "counters keep their last known value until it runs again",
                state,
                STATE_NORMAL,
            )

        seen = 0
        for sen in sensors:
            if sen.name not in staged:
                continue
            value = staged[sen.name]
            if value is None:
                continue

            # The field exists on this inverter, so the sensor is worth
            # exposing even if this particular record is not trustworthy.
            sen.enabled = True
            seen += 1

            if sen.name != "state" and not running:
                # Registers are not meaningful while the inverter is starting
                # up, waiting or in error. Live readings become unknown rather
                # than stale; counters are left alone so they cannot regress.
                if not sen.per_day_basis and not sen.per_total_basis:
                    sen.value = None
                continue

            if sen.per_total_basis and not _accept_cumulative(sen, value, now):
                continue

            if sen.value != value:
                _LOGGER.debug("New value for sensor %s: %s", sen.name, value)
            sen.value = value
            sen.date = today
            sen.last_update = now

        if not seen:
            _LOGGER.warning("Discarding record: no usable sensor values")
            return False

        return True


class UnauthorizedException(Exception):
    """Exception for Unauthorized 401 status code"""

    def __init__(self, message):
        Exception.__init__(self, message)


class UnexpectedResponseException(Exception):
    """Exception for unexpected status code"""

    def __init__(self, message):
        Exception.__init__(self, message)
