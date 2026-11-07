"""Status sensors for Fridge Vision Inventory."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import (
    FridgeVisionCoordinator,
    SIGNAL_DETECTION_UPDATED,
    device_info,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the 2 Fridge Vision status sensors."""
    coordinator: FridgeVisionCoordinator = (
        getattr(entry, "runtime_data", None)
        or hass.data.get(DOMAIN, {}).get(entry.entry_id)
    )

    async_add_entities(
        [
            FridgeLastDetectionSensor(coordinator, entry),
            FridgeDetectionFailedSensor(coordinator, entry),
        ]
    )


class FridgeLastDetectionSensor(SensorEntity):
    """Shows what was detected in the last door sequence."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Ostatnia detekcja"
    _attr_icon = "mdi:eye-outline"

    def __init__(
        self,
        coordinator: FridgeVisionCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}::last_detection"
        self._attr_device_info = device_info(entry.entry_id)

    @property
    def native_value(self) -> str:
        """Text summary of the last detection (e.g. 'Mleko (Włożono)')."""
        return self._coordinator.last_detection_summary

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Detailed payload of the last detection."""
        return self._coordinator.last_detection_details

    async def async_added_to_hass(self) -> None:
        """Subscribe to detection events."""
        await super().async_added_to_hass()
        self.async_on_unload(
            async_dispatcher_connect(
                self.hass, SIGNAL_DETECTION_UPDATED, self._handle_detection_updated
            )
        )

    @callback
    def _handle_detection_updated(self) -> None:
        self.async_write_ha_state()


class FridgeDetectionFailedSensor(SensorEntity):
    """Indicates whether the last detection failed (True / False)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Niepowodzenie detekcji"

    def __init__(
        self,
        coordinator: FridgeVisionCoordinator,
        entry: ConfigEntry,
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}::detection_failed"
        self._attr_device_info = device_info(entry.entry_id)

    @property
    def native_value(self) -> bool:
        """Return True if last detection failed, False otherwise."""
        return self._coordinator.last_detection_failed

    @property
    def icon(self) -> str:
        """Change icon based on success or failure."""
        if self._coordinator.last_detection_failed:
            return "mdi:alert-circle-outline"
        return "mdi:check-circle-outline"

    async def async_added_to_hass(self) -> None:
        """Subscribe to detection events."""
        await super().async_added_to_hass()
        self.async_on_unload(
            async_dispatcher_connect(
                self.hass, SIGNAL_DETECTION_UPDATED, self._handle_detection_updated
            )
        )

    @callback
    def _handle_detection_updated(self) -> None:
        self.async_write_ha_state()