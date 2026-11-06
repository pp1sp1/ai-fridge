"""Item count sensors for the Fridge Vision Inventory integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_LOCATION, DOMAIN, SIGNAL_ITEM_UPDATED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up item count sensors from the stored inventory."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = domain_data["coordinator"]
    items: list[dict[str, Any]] = domain_data["items"]

    entities = [
        FridgeInventorySensor(coordinator, item) for item in items
    ]
    async_add_entities(entities)
    domain_data["add_entities"] = async_add_entities
    domain_data["sensors"] = {entity.item_id: entity for entity in entities}


class FridgeInventorySensor(SensorEntity):
    """A sensor reporting the quantity of a single inventory item."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "szt."
    _attr_should_poll = False

    def __init__(
        self, coordinator: Any, item: dict[str, Any]
    ) -> None:
        self.coordinator = coordinator
        self.item_id: str = item["id"]
        self._item = item
        self._attr_unique_id = f"{DOMAIN}_{item['id']}"
        self._attr_name = item["name"]

    @callback
    def async_added_to_hass(self) -> None:
        """Subscribe to per-item update signals."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ITEM_UPDATED.format(self.item_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self, item: dict[str, Any]) -> None:
        self._item = item
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        return int(self._item.get("quantity", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "ai_prompt": self._item.get("ai_prompt"),
            "location": self._item.get("location", DEFAULT_LOCATION),
            "item_id": self.item_id,
            "last_updated": self._item.get("last_updated"),
        }

    @property
    def available(self) -> bool:
        return True
