"""SAJ solar inverter interface."""

from datetime import date
import logging
from typing import override

import pysaj
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_TYPE,
    CONF_USERNAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfMass,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    AddEntitiesCallback,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType, StateType
from homeassistant.util import dt as dt_util

from . import SAJConfigEntry, SAJRuntimeData
from .const import CONNECTION_TYPES, DOMAIN, INTEGRATION_TITLE

_LOGGER = logging.getLogger(__name__)

SAJ_UNIT_MAPPINGS = {
    "": None,
    "h": UnitOfTime.HOURS,
    "kg": UnitOfMass.KILOGRAMS,
    "kWh": UnitOfEnergy.KILO_WATT_HOUR,
    "W": UnitOfPower.WATT,
    "°C": UnitOfTemperature.CELSIUS,
    "V": UnitOfElectricPotential.VOLT,
    "A": UnitOfElectricCurrent.AMPERE,
    "Hz": UnitOfFrequency.HERTZ,
}

SAJ_DEVICE_CLASSES = {
    UnitOfPower.WATT: SensorDeviceClass.POWER,
    UnitOfEnergy.KILO_WATT_HOUR: SensorDeviceClass.ENERGY,
    UnitOfTemperature.CELSIUS: SensorDeviceClass.TEMPERATURE,
    UnitOfElectricPotential.VOLT: SensorDeviceClass.VOLTAGE,
    UnitOfElectricCurrent.AMPERE: SensorDeviceClass.CURRENT,
    UnitOfFrequency.HERTZ: SensorDeviceClass.FREQUENCY,
}

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_TYPE, default=CONNECTION_TYPES[0]): vol.In(CONNECTION_TYPES),
        vol.Inclusive(CONF_USERNAME, "credentials"): cv.string,
        vol.Inclusive(CONF_PASSWORD, "credentials"): cv.string,
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SAJConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the SAJ sensors from a config entry."""
    runtime = entry.runtime_data
    sensor_def = runtime.sensor_def

    hass_sensors = [
        SAJsensor(runtime, entry.unique_id, sensor, inverter_name=None)
        for sensor in sensor_def
        if sensor.enabled
    ]

    async_add_entities(hass_sensors)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Migrate YAML sensor platform configuration to a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data=dict(config),
    )
    if (
        result.get("type") is FlowResultType.ABORT
        and result.get("reason") != "already_configured"
    ):
        reason = result.get("reason", "unknown")
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"deprecated_yaml_import_issue_{reason}",
            is_fixable=False,
            issue_domain=DOMAIN,
            severity=ir.IssueSeverity.WARNING,
            translation_key=f"deprecated_yaml_import_issue_{reason}",
            translation_placeholders={
                "domain": DOMAIN,
                "integration_title": INTEGRATION_TITLE,
            },
        )
        return

    ir.async_create_issue(
        hass,
        HOMEASSISTANT_DOMAIN,
        f"deprecated_yaml_{DOMAIN}",
        is_fixable=False,
        issue_domain=DOMAIN,
        severity=ir.IssueSeverity.WARNING,
        translation_key="deprecated_yaml",
        translation_placeholders={
            "domain": DOMAIN,
            "integration_title": INTEGRATION_TITLE,
        },
    )


class SAJsensor(SensorEntity, RestoreEntity):
    """Representation of a SAJ sensor."""

    _attr_should_poll = False
    _state: StateType

    def __init__(
        self,
        runtime: SAJRuntimeData,
        serialnumber: str | None,
        pysaj_sensor: pysaj.Sensor,
        inverter_name: str | None = None,
    ) -> None:
        """Initialize the SAJ sensor."""
        self._runtime = runtime
        self._sensor = pysaj_sensor
        self._inverter_name = inverter_name
        self._serialnumber = serialnumber
        self._state = self._sensor.value

        self._attr_unique_id = f"{serialnumber}_{pysaj_sensor.name}"
        native_uom = SAJ_UNIT_MAPPINGS[pysaj_sensor.unit]
        self._attr_native_unit_of_measurement = native_uom
        if self._inverter_name:
            self._attr_name = f"saj_{self._inverter_name}_{pysaj_sensor.name}"
        else:
            self._attr_name = f"saj_{pysaj_sensor.name}"

        self._attr_device_class = SAJ_DEVICE_CLASSES.get(native_uom)

        if pysaj_sensor.per_total_basis:
            # Lifetime counters. pysaj guarantees these never regress, which is
            # what keeps the statistics engine from reading a bad sample as a
            # meter reset and booking the whole counter as fresh production.
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        elif native_uom is not None and not pysaj_sensor.per_day_basis:
            self._attr_state_class = SensorStateClass.MEASUREMENT

        # Only present from pysaj 0.1.0 onwards; tolerate an older library
        # rather than failing setup outright.
        precision = getattr(pysaj_sensor, "precision", None)
        if native_uom is not None and precision is not None:
            self._attr_suggested_display_precision = precision

    @override
    async def async_added_to_hass(self) -> None:
        """Register for inverter poll updates."""
        await super().async_added_to_hass()
        await self._async_restore_counter_baseline()
        self.async_on_remove(
            self._runtime.polling.async_add_poll_listener(self._on_poll_success)
        )

    async def _async_restore_counter_baseline(self) -> None:
        """Carry the last known lifetime value across a restart.

        Setup performs its first read before any entity exists, so the library
        starts with no baseline and accepts whatever that first record said -
        including a low value taken while the inverter was still waking up.
        Restoring here re-establishes the floor before the value is published.
        """
        if not self._sensor.per_total_basis:
            return

        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            return

        try:
            restored = float(last_state.state)
        except ValueError:
            return

        current = self._sensor.value
        if current is not None and current >= restored:
            return

        if current is not None:
            _LOGGER.warning(
                "Restoring %s to %s: the first reading after startup was %s, "
                "which would be read as a meter reset",
                self._sensor.name,
                restored,
                current,
            )

        self._sensor.value = restored
        self._sensor.last_update = last_state.last_updated
        self._state = restored

    @property
    @override
    def native_value(self) -> StateType:
        """Return the state of the sensor."""
        return self._state

    @property
    def per_day_basis(self) -> bool:
        """Return if the sensors value is on daily basis or not."""
        return self._sensor.per_day_basis

    @property
    def per_total_basis(self) -> bool:
        """Return if the sensors value is cumulative or not."""
        return self._sensor.per_total_basis

    @property
    def date_updated(self) -> date:
        """Return the date when the sensor was last updated."""
        return self._sensor.date

    @callback
    def _on_poll_success(self, success: bool) -> None:
        """Update state from the inverter after a poll."""
        state_unknown = False
        if not success and (
            (self.per_day_basis and dt_util.now().date() > self.date_updated)
            or (not self.per_day_basis and not self.per_total_basis)
        ):
            state_unknown = True

        update = False
        if self._sensor.value != self._state:
            update = True
            self._state = self._sensor.value

        if state_unknown and self._state is not None:
            update = True
            self._state = None

        if update:
            self.async_write_ha_state()
