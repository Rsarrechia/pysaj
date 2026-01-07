import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date
import xml.etree.ElementTree as ET
from io import StringIO
import csv
import aiohttp

from pysaj import (
    Sensor,
    Sensors,
    SAJ,
    MAPPER_STATES,
    UnauthorizedException,
    UnexpectedResponseException,
)
from aioresponses import aioresponses


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


class TestSensors:
    def test_init_wifi_false(self):
        sensors = Sensors(wifi=False)
        assert len(sensors) == 9
        assert "current_power" in sensors
        assert sensors["p-ac"].key == "p-ac"

    def test_init_wifi_true(self):
        sensors = Sensors(wifi=True)
        assert len(sensors) == 9

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
        original = sensors["current_power"]
        new_sensor = Sensor("p-ac", 11, 23, "", "current_power", "kW")  # different unit
        sensors.add(new_sensor)
        assert len(sensors) == 9  # same length
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
    async def test_read_ethernet_success(self):
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

        with aioresponses() as rs:
            rs.get(saj.url_info, body=info_xml)
            rs.get(saj.url, body=data_xml)

            result = await saj.read(sensors)

            assert result is True
            assert saj.serialnumber == "123456789"
            assert sensors["current_power"].value == 1000.0
            assert sensors["today_yield"].value == 5.0
            assert sensors["temperature"].value == 25.0
            assert sensors["state"].value == "Normal"

    @pytest.mark.asyncio
    async def test_read_wifi_success(self):
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)

        # Mock CSV responses
        info_csv = "SN123456789\n"
        data_csv = "1000,0,500,500,60,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2,1000\n"

        with aioresponses() as rs:
            rs.get(saj.url_info, body=info_csv)
            rs.get(saj.url, body=data_csv)

            result = await saj.read(sensors)

            assert result is True
            assert saj.serialnumber == "SN123456789"
            assert sensors["current_power"].value == 1000.0
            assert sensors["today_yield"].value == 5.0
            assert sensors["state"].value == "Normal"

    @pytest.mark.asyncio
    async def test_read_connection_error(self):
        saj = SAJ("192.168.1.100", wifi=False)
        sensors = Sensors()

        with aioresponses() as rs:
            rs.get(
                saj.url_info,
                exception=aiohttp.ClientConnectorError(MagicMock(), OSError()),
            )

            result = await saj.read(sensors)
            assert result is False

    @pytest.mark.asyncio
    async def test_read_unauthorized(self):
        saj = SAJ("192.168.1.100", wifi=False)
        sensors = Sensors()

        info_xml = """<?xml version="1.0"?>
<root>
    <SN>123</SN>
</root>"""

        with aioresponses() as rs:
            rs.get(saj.url_info, body=info_xml)
            rs.get(saj.url, status=401)

            with pytest.raises(UnauthorizedException):
                await saj.read(sensors)

    @pytest.mark.asyncio
    async def test_read_invalid_csv(self):
        saj = SAJ("192.168.1.100", wifi=True)
        sensors = Sensors(wifi=True)

        info_csv = "SN123\n"
        data_csv = ""  # Empty CSV

        with aioresponses() as rs:
            rs.get(saj.url_info, body=info_csv)
            rs.get(saj.url, body=data_csv)

            with pytest.raises(UnexpectedResponseException):
                await saj.read(sensors)

    @pytest.mark.asyncio
    async def test_read_invalid_xml(self):
        saj = SAJ("192.168.1.100", wifi=False)
        sensors = Sensors()

        info_xml = "<invalid>"
        data_xml = "<also invalid>"

        with aioresponses() as rs:
            rs.get(saj.url_info, body=info_xml)
            rs.get(saj.url, body=data_xml)

            with pytest.raises(UnexpectedResponseException):
                await saj.read(sensors)


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
        # In code, uses .get(v, f"Unknown({v})")
        assert MAPPER_STATES.get("99", "Unknown(99)") == "Unknown(99)"
