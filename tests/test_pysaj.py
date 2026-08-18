import pytest
from unittest.mock import MagicMock
from datetime import date, datetime, timedelta, timezone
import aiohttp

import pysaj
from pysaj import (
    WARN_INTERVAL_SECONDS,
    Sensor,
    Sensors,
    SAJ,
    MAPPER_STATES,
    UnauthorizedException,
    UnexpectedResponseException,
)


# --- minimal aiohttp mock ---------------------------------------------------
# aioresponses does not track aiohttp's ClientResponse signature (it breaks on
# aiohttp >= 3.14, which is what Home Assistant ships), so we mock the small
# slice of aiohttp that pysaj.read() actually uses. This keeps the suite green
# against whatever aiohttp is installed. Each route maps a URL to either
# (body, status) or an Exception instance to raise when the request is made.


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    async def text(self, encoding="utf-8"):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeGet:
    def __init__(self, entry, raise_for_status):
        self._entry = entry
        self._raise = raise_for_status

    async def __aenter__(self):
        entry = self._entry
        if isinstance(entry, Exception):
            raise entry
        body, status = entry
        if self._raise and status >= 400:
            raise aiohttp.ClientResponseError(MagicMock(), (), status=status)
        return _FakeResponse(body)

    async def __aexit__(self, *exc):
        return False


def install_fake_http(monkeypatch, routes):
    """Patch aiohttp.ClientSession so pysaj.read() serves from `routes`."""

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            self._raise = kwargs.get("raise_for_status", False)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url):
            return _FakeGet(routes[str(url)], self._raise)

    monkeypatch.setattr(pysaj.aiohttp, "ClientSession", _FakeSession)


# A real 35 field record as returned by status.php on a 3 phase inverter with
# two PV strings connected (the unconnected channels report 65535).
WIFI_RECORD = (
    "3,1758433,121794,1165,82,3099,39,1657,40,"
    "65535,65535,65535,65535,65535,65535,65535,65535,"
    "65535,65535,65535,65535,65535,65535,"
    "185,4998,2290,55,2290,56,2286,56,6205,397,138036,2"
)


def wifi_record(overrides):
    """Build a 35 field record with individual fields replaced by index."""
    fields = WIFI_RECORD.split(",")
    for index, value in overrides.items():
        fields[index] = str(value)
    return ",".join(fields)


class TestSensor:
    def test_init(self):
        s = Sensor("key", 1, 2, "/10", "name", "unit", True, False)
        assert s.key == "key"
        assert s.csv_1_key == 1
        assert s.csv_2_key == 2
        assert s.factor == "/10"
        assert s.name == "name"
        assert s.unit == "unit"
        assert s.per_day_basis is True
        assert s.per_total_basis is False
        assert s.value is None
        assert s.enabled is False
        assert s.date == date.today()

    def test_precision_from_factor(self):
        assert Sensor("k", 1, 1, "", "n").precision == 0
        assert Sensor("k", 1, 1, "/10", "n").precision == 1
        assert Sensor("k", 1, 1, "/100", "n").precision == 2


class TestSensors:
    def test_init_wifi_false(self):
        sensors = Sensors(wifi=False)
        assert len(sensors) == 9
        assert "current_power" in sensors
        assert sensors["p-ac"].key == "p-ac"
        # Only reported over the XML interface.
        assert "today_max_current" in sensors

    def test_init_wifi_true(self):
        sensors = Sensors(wifi=True)
        # The eight shared sensors plus the channels only the WiFi module has.
        assert len(sensors) == 34
        for name in (
            "pv1_voltage",
            "pv2_current",
            "pv3_string4_current",
            "grid_frequency",
            "line3_voltage",
            "bus_voltage",
        ):
            assert name in sensors
        # Never present in the CSV, so it must not be offered for WiFi.
        assert "today_max_current" not in sensors

    def test_getitem_by_name(self):
        sensors = Sensors()
        s = sensors["current_power"]
        assert s.name == "current_power"

    def test_getitem_by_key(self):
        sensors = Sensors()
        s = sensors["p-ac"]
        assert s.key == "p-ac"

    def test_getitem_keyerror(self):
        sensors = Sensors()
        with pytest.raises(KeyError):
            sensors["nonexistent"]

    def test_contains(self):
        sensors = Sensors()
        assert "current_power" in sensors
        assert "nonexistent" not in sensors

    def test_add_single(self):
        sensors = Sensors()
        initial_len = len(sensors)
        new_sensor = Sensor("new", 99, 99, "", "new_sensor")
        sensors.add(new_sensor)
        assert len(sensors) == initial_len + 1
        assert "new_sensor" in sensors

    def test_add_list(self):
        sensors = Sensors()
        initial_len = len(sensors)
        new_sensors = [
            Sensor("new1", 99, 99, "", "new1"),
            Sensor("new2", 100, 100, "", "new2"),
        ]
        sensors.add(new_sensors)
        assert len(sensors) == initial_len + 2

    def test_add_replace(self, caplog):
        sensors = Sensors()
        initial_len = len(sensors)
        new_sensor = Sensor("p-ac", 11, 23, "", "current_power", "kW")
        sensors.add(new_sensor)
        assert len(sensors) == initial_len
        assert sensors["current_power"].unit == "kW"
        assert "Replacing sensor" in caplog.text

    def test_add_duplicate_key_warning(self, caplog):
        sensors = Sensors()
        new_sensor = Sensor("p-ac", 11, 23, "", "different_name")
        sensors.add(new_sensor)
        assert "Duplicate SAJ sensor key" in caplog.text


class TestSAJ:
    def test_init_ethernet(self):
        saj = SAJ("192.168.1.100", wifi=False)
        assert saj.host == "192.168.1.100"
        assert saj.wifi is False
        assert saj.url == "http://192.168.1.100/real_time_data.xml"
        assert saj.url_info == "http://192.168.1.100/equipment_data.xml"
        assert saj.serialnumber == "XXXXXXXXXXXXXXXXX"

    def test_init_wifi(self):
        saj = SAJ("192.168.1.100", wifi=True, username="user", password="pass")
        assert saj.wifi is True
        assert saj.url == "http://user:pass@192.168.1.100/status/status.php"
        assert saj.url_info == "http://user:pass@192.168.1.100/info.php"

    def test_init_wifi_no_creds(self):
        saj = SAJ("192.168.1.100", wifi=True, username="", password="")
        assert saj.url == "http://192.168.1.100/status/status.php"
        assert saj.url_info == "http://192.168.1.100/info.php"

    @pytest.mark.asyncio
    async def test_read_ethernet_success(self, monkeypatch):
        saj = SAJ("192.168.1.100", wifi=False)
        sensors = Sensors(wifi=False)

        info_xml = """<?xml version="1.0"?>
<root>
    <SN>123456789</SN>
</root>"""
        data_xml = """<?xml version="1.0"?>
<root>
    <p-ac>1000</p-ac>
    <e-today>500</e-today>
    <temp>250</temp>
    <state>2</state>
</root>"""

        install_fake_http(monkeypatch, {
            saj.url_info: (info_xml, 200),
            saj.url: (data_xml, 200),
        })

        result = await saj.read(sensors)

        assert result is True
        assert saj.serialnumber == "123456789"
        assert sensors["current_power"].value == 1000.0
        assert sensors["today_yield"].value == 5.0
        assert sensors["temperature"].value == 25.0
        assert sensors["state"].value == "Normal"

    @pytest.mark.asyncio
    async def test_read_wifi_success(self, monkeypatch):
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)

        install_fake_http(monkeypatch, {
            saj.url_info: ("SN123456789,x,00,0000,2,7\n", 200),
            saj.url: (WIFI_RECORD + "\n", 200),
        })

        result = await saj.read(sensors)

        assert result is True
        assert saj.serialnumber == "SN123456789"
        assert sensors["total_yield"].value == 17584.33
        assert sensors["today_yield"].value == 11.65
        assert sensors["current_power"].value == 185.0
        assert sensors["temperature"].value == 39.7
        assert sensors["state"].value == "Normal"

    @pytest.mark.asyncio
    async def test_read_wifi_exposes_new_channels(self, monkeypatch):
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)

        install_fake_http(monkeypatch, {
            saj.url_info: ("SN1\n", 200),
            saj.url: (WIFI_RECORD + "\n", 200),
        })

        assert await saj.read(sensors) is True

        assert sensors["pv1_voltage"].value == 309.9
        assert sensors["pv1_current"].value == 0.39
        assert sensors["pv2_voltage"].value == 165.7
        assert sensors["grid_frequency"].value == 49.98
        assert sensors["line1_voltage"].value == 229.0
        assert sensors["line3_current"].value == 0.56
        assert sensors["bus_voltage"].value == 620.5

    @pytest.mark.asyncio
    async def test_read_connection_error(self, monkeypatch):
        saj = SAJ("192.168.1.100", wifi=False)
        sensors = Sensors()

        install_fake_http(monkeypatch, {
            saj.url_info: aiohttp.ClientConnectorError(MagicMock(), OSError()),
        })

        result = await saj.read(sensors)
        assert result is False

    @pytest.mark.asyncio
    async def test_read_unauthorized(self, monkeypatch):
        saj = SAJ("192.168.1.100", wifi=False)
        sensors = Sensors()

        info_xml = """<?xml version="1.0"?>
<root>
    <SN>123</SN>
</root>"""

        install_fake_http(monkeypatch, {
            saj.url_info: (info_xml, 200),
            saj.url: ("", 401),
        })

        with pytest.raises(UnauthorizedException):
            await saj.read(sensors)

    @pytest.mark.asyncio
    async def test_read_invalid_csv(self, monkeypatch):
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)

        install_fake_http(monkeypatch, {
            saj.url_info: ("SN123\n", 200),
            saj.url: ("", 200),
        })

        with pytest.raises(UnexpectedResponseException):
            await saj.read(sensors)

    @pytest.mark.asyncio
    async def test_read_invalid_xml(self, monkeypatch):
        saj = SAJ("192.168.1.100", wifi=False)
        sensors = Sensors()

        install_fake_http(monkeypatch, {
            saj.url_info: ("<invalid>", 200),
            saj.url: ("<also invalid>", 200),
        })

        with pytest.raises(UnexpectedResponseException):
            await saj.read(sensors)


class TestRecordGuards:
    """The record must be well formed before any field is trusted."""

    def _prime(self):
        """A sensor set holding one good record."""
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)
        assert saj._read_wifi(WIFI_RECORD, sensors) is True
        return saj, sensors

    @pytest.mark.parametrize(
        "body",
        [
            "3,17",  # truncated mid number
            "w",  # the module's wait marker
            "3,1758433,121794,1165,82",  # partial record
            WIFI_RECORD + ",99",  # unexpected extra field
        ],
    )
    def test_malformed_record_is_discarded(self, body):
        saj, sensors = self._prime()
        before = sensors["total_yield"].value

        assert saj._read_wifi(body, sensors) is False
        assert sensors["total_yield"].value == before

    def test_multiline_record_is_discarded(self):
        saj, sensors = self._prime()
        before = sensors["total_yield"].value

        assert saj._read_wifi("w\n" + WIFI_RECORD, sensors) is False
        assert sensors["total_yield"].value == before

    def test_old_23_field_layout_still_reads(self):
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)
        # Older firmware: current_power at 11, temp at 20, CO2 at 21, state at 22.
        old = "0,1758433,121794,1165,82,0,0,0,0,0,0,185,0,0,0,0,0,0,0,0,397,138036,2"
        assert len(old.split(",")) == 23

        assert saj._read_wifi(old, sensors) is True
        assert sensors["total_yield"].value == 17584.33
        assert sensors["current_power"].value == 185.0
        assert sensors["temperature"].value == 39.7
        assert sensors["state"].value == "Normal"
        # 35 field only channels must stay absent on the old layout.
        assert sensors["bus_voltage"].enabled is False

    def test_not_available_sentinel_is_not_a_reading(self):
        _, sensors = self._prime()
        # 65535 in the record means the channel does not exist.
        assert sensors["pv3_voltage"].value is None
        assert sensors["pv3_voltage"].enabled is False
        assert sensors["pv1_string1_current"].enabled is False

    def test_present_but_invalid_field_still_enables_the_sensor(self):
        """Entities are created from a field's presence, not its plausibility."""
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)

        # First read of the day, while the module still reports 0Hz: below
        # grid_frequency's plausible minimum, so there is no value to apply.
        assert saj._read_wifi(wifi_record({24: 0}), sensors) is True

        assert sensors["grid_frequency"].value is None
        assert sensors["grid_frequency"].enabled is True

    def test_a_record_of_only_invalid_fields_is_still_discarded(self):
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)

        assert saj._read_wifi("w", sensors) is False

    def test_daily_above_lifetime_is_discarded(self):
        saj, sensors = self._prime()
        before = sensors["today_yield"].value

        # today_yield 200.00 kWh against a lifetime total of 1.00 kWh
        bad = wifi_record({1: 100, 3: 20000})
        assert saj._read_wifi(bad, sensors) is False
        assert sensors["today_yield"].value == before

    def test_out_of_range_field_is_dropped(self):
        saj, sensors = self._prime()
        # 300.00 Hz is not a grid frequency.
        assert saj._read_wifi(wifi_record({24: 30000}), sensors) is True
        assert sensors["grid_frequency"].value == 49.98

    def test_negative_temperature_is_kept(self):
        saj, sensors = self._prime()
        # -3.5 degrees, which the old name/key mix up turned into None.
        assert saj._read_wifi(wifi_record({32: -35}), sensors) is True
        assert sensors["temperature"].value == -3.5


class TestCumulativeGuards:
    """Lifetime counters must never regress or jump implausibly."""

    def _prime(self):
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)
        assert saj._read_wifi(WIFI_RECORD, sensors) is True
        return saj, sensors

    def test_backwards_total_is_rejected(self, caplog):
        saj, sensors = self._prime()

        # The observed failure: the register dips by a few kWh.
        assert saj._read_wifi(wifi_record({1: 1756793}), sensors) is True
        assert sensors["total_yield"].value == 17584.33
        assert "going backwards" in caplog.text

    def test_total_dropping_to_zero_is_rejected(self):
        saj, sensors = self._prime()

        assert saj._read_wifi(wifi_record({1: 0, 3: 0}), sensors) is True
        assert sensors["total_yield"].value == 17584.33

    def test_normal_growth_is_accepted(self):
        saj, sensors = self._prime()

        assert saj._read_wifi(wifi_record({1: 1758440}), sensors) is True
        assert sensors["total_yield"].value == 17584.40

    def test_implausible_jump_is_held_then_confirmed(self, caplog):
        saj, sensors = self._prime()
        spike = wifi_record({1: 9999999})

        # Held back while it looks like a one off.
        assert saj._read_wifi(spike, sensors) is True
        assert sensors["total_yield"].value == 17584.33
        assert saj._read_wifi(spike, sensors) is True
        assert sensors["total_yield"].value == 17584.33
        assert "Holding back" in caplog.text

        # Consistently reported, so it is real and must not freeze the counter.
        assert saj._read_wifi(spike, sensors) is True
        assert sensors["total_yield"].value == 99999.99

    def test_single_resolution_step_is_never_a_jump(self, caplog):
        """A 0.1h step in total_time is normal, not an implausible jump."""
        saj, sensors = self._prime()
        total_time = sensors["total_time"]
        assert total_time.value == pytest.approx(12179.4)
        # The consumer polls every 5s after a successful read, far below the
        # 5 min that 1.2h/h would need to allow one 0.1h step on its own.
        total_time.last_update = datetime.now(timezone.utc) - timedelta(seconds=5)

        assert saj._read_wifi(wifi_record({2: 121795}), sensors) is True

        assert total_time.value == pytest.approx(12179.5)
        assert "Holding back" not in caplog.text

    def test_total_time_still_rejects_a_real_jump(self):
        saj, sensors = self._prime()

        # +320h of runtime in one poll cycle is not a step, it is a bad read.
        assert saj._read_wifi(wifi_record({2: 125000}), sensors) is True
        assert sensors["total_time"].value == pytest.approx(12179.4)

    def test_long_gap_allows_a_large_increase(self):
        saj, sensors = self._prime()
        total = sensors["total_yield"]
        # Home Assistant was down for two days.
        total.last_update = datetime.now(timezone.utc) - timedelta(days=2)

        assert saj._read_wifi(wifi_record({1: 1763433}), sensors) is True
        assert sensors["total_yield"].value == 17634.33


class TestWarningThrottle:
    """A condition that repeats on every poll must not flood the log.

    The consumer polls every 5s after a successful read, so an unthrottled
    warning about a sunrise or a stuck field means hundreds of lines an hour.
    """

    def _prime(self):
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)
        assert saj._read_wifi(WIFI_RECORD, sensors) is True
        return saj, sensors

    def test_repeated_state_warning_is_throttled(self, caplog):
        saj, sensors = self._prime()
        waking = wifi_record({34: 1})

        for _ in range(10):
            assert saj._read_wifi(waking, sensors) is True

        assert caplog.text.count("Inverter state is") == 1

    def test_repeated_out_of_range_warning_is_throttled(self, caplog):
        saj, sensors = self._prime()
        # The module reports 0Hz until the grid synchronises, for the whole
        # dawn window, and 0 is below grid_frequency's plausible minimum.
        for _ in range(10):
            assert saj._read_wifi(wifi_record({24: 0}), sensors) is True

        assert caplog.text.count("Discarding grid_frequency reading") == 1

    def test_repeated_malformed_record_warning_is_throttled(self, caplog):
        saj, _ = self._prime()

        for _ in range(10):
            assert saj._read_wifi("w", Sensors(wifi=True)) is False

        assert caplog.text.count("Discarding status record with") == 1

    def test_distinct_conditions_do_not_mask_each_other(self, caplog):
        """Throttling is per message, so one rejection cannot hide another."""
        saj, sensors = self._prime()

        assert saj._read_wifi(wifi_record({1: 1756793}), sensors) is True
        assert "going backwards" in caplog.text

        # Well inside the throttle interval, but a different condition, so it
        # still has to be reported.
        assert saj._read_wifi(wifi_record({1: 9999999}), sensors) is True
        assert "Holding back" in caplog.text

    def test_suppressed_count_is_reported_per_message(self, caplog):
        saj, sensors = self._prime()
        backwards = wifi_record({1: 1756793})

        for _ in range(4):
            assert saj._read_wifi(backwards, sensors) is True
        # Pretend the throttle interval has passed for this one message.
        total = sensors["total_yield"]
        message, (_, suppressed) = next(iter(total.warn_history.items()))
        assert suppressed == 3
        total.warn_history[message] = (
            datetime.now(timezone.utc) - timedelta(seconds=WARN_INTERVAL_SECONDS + 1),
            suppressed,
        )

        assert saj._read_wifi(backwards, sensors) is True
        assert "3 identical since the last message" in caplog.text


class TestRunStateGate:
    """Readings taken while the inverter is not running are not trusted."""

    def _prime(self):
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)
        assert saj._read_wifi(WIFI_RECORD, sensors) is True
        return saj, sensors

    @pytest.mark.parametrize("code", [0, 1, 3, 4])
    def test_non_normal_state_clears_live_and_keeps_counters(self, code):
        saj, sensors = self._prime()

        # A zeroed wake up record carrying a non normal run state.
        waking = wifi_record({1: 0, 3: 0, 23: 0, 34: code})
        assert saj._read_wifi(waking, sensors) is True

        # Counters survive untouched, so Home Assistant sees no meter reset.
        assert sensors["total_yield"].value == 17584.33
        assert sensors["today_yield"].value == 11.65
        # Live readings are unknown rather than a stale or bogus number.
        assert sensors["current_power"].value is None
        assert sensors["grid_frequency"].value is None
        assert sensors["state"].value == MAPPER_STATES[str(code)]

    def test_channels_stay_enabled_while_not_running(self):
        """Entities must still be created if setup lands in a bad window."""
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)

        assert saj._read_wifi(wifi_record({34: 1}), sensors) is True
        assert sensors["total_yield"].enabled is True
        assert sensors["bus_voltage"].enabled is True
        assert sensors["total_yield"].value is None

    def test_recovery_after_waking(self):
        saj, sensors = self._prime()

        assert saj._read_wifi(wifi_record({34: 1, 23: 0}), sensors) is True
        assert sensors["current_power"].value is None

        assert saj._read_wifi(WIFI_RECORD, sensors) is True
        assert sensors["current_power"].value == 185.0
        assert sensors["total_yield"].value == 17584.33


class TestExceptions:
    def test_unauthorized_exception(self):
        exc = UnauthorizedException("msg")
        assert str(exc) == "msg"

    def test_unexpected_response_exception(self):
        exc = UnexpectedResponseException("msg")
        assert str(exc) == "msg"


class TestMapperStates:
    def test_known_states(self):
        assert MAPPER_STATES["0"] == "Not connected"
        assert MAPPER_STATES["2"] == "Normal"

    def test_unknown_state(self):
        assert MAPPER_STATES.get("99", "Unknown(99)") == "Unknown(99)"
