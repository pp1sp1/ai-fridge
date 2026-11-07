"""Per-item quantity sensors for Fridge Vision Inventory.

One sensor is created per inventory item and follows dynamic item
additions/removals via the ``SIGNAL_ITEMS_UPDATED`` dispatcher signal.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_AI_PROMPT,
    ATTR_CATEGORY,
    ATTR_DESCRIPTION,
    ATTR_EXPIRY_DATE,
    ATTR_ITEM_ID,
    ATTR_LAST_UPDATED,
    ATTR_LOCATION,
    ATTR_UNIT,
    DOMAIN,
    SIGNAL_ITEMS_UPDATED,
)
from .coordinator import (
    FridgeVisionCoordinator,
    InventoryItem,
    device_info,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one sensor per inventory item, following dynamic changes."""
    coordinator: FridgeVisionCoordinator = entry.runtime_data
    known: dict[str, FridgeItemSensor] = {}

    @callback
    def _async_sync_items() -> None:
        wanted = set(coordinator.items)

        new_ids = wanted - set(known)
        if new_ids:
            created = [
                (item_id, FridgeItemSensor(coordinator, entry, item_id))
                for item_id in sorted(new_ids)
            ]
            for item_id, sensor in created:
                known[item_id] = sensor
            async_add_entities([sensor for _, sensor in created])

        removed_ids = set(known) - wanted
        for item_id in removed_ids:
            sensor = known.pop(item_id)
            hass.async_create_task(sensor.async_remove())

    _async_sync_items()
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ITEMS_UPDATED, _async_sync_items)
    )


class FridgeItemSensor(SensorEntity):
    """Shows the quantity of a single inventory item."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:fridge-outline"

    def __init__(
        self,
        coordinator: FridgeVisionCoordinator,
        entry: ConfigEntry,
        item_id: str,
    ) -> None:
        self._coordinator = coordinator
        self._item_id = item_id
        self._attr_unique_id = f"{entry.entry_id}::{item_id}"
        self._attr_device_info = device_info(entry.entry_id)

    @property
    def item(self) -> InventoryItem | None:
        """The backing inventory item, or None once deleted."""
        return self._coordinator.items.get(self._item_id)

    @property
    def name(self) -> str | None:
        """Display name of the item (Polish)."""
        item = self.item
        return item.name if item else None

    @property
    def native_value(self) -> int | float | None:
        """Current quantity of the item (int when whole)."""
        item = self.item
        if item is None:
            return None
        value = float(item.quantity)
        return int(value) if value.is_integer() else value

    @property
    def available(self) -> bool:
        """Unavailable once the item was deleted."""
        return self.item is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose item metadata (simple_inventory-style fields)."""
        item = self.item
        if item is None:
            return {}
        return {
            ATTR_ITEM_ID: item.id,
            ATTR_AI_PROMPT: item.ai_prompt,
            ATTR_LOCATION: item.location,
            ATTR_UNIT: item.unit,
            ATTR_CATEGORY: item.category,
            ATTR_EXPIRY_DATE: item.expiry_date,
            ATTR_DESCRIPTION: item.description,
            ATTR_LAST_UPDATED: item.last_updated,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to inventory updates."""
        await super().async_added_to_hass()
        self.async_on_unload(
            async_dispatcher_connect(
                self.hass, SIGNAL_ITEMS_UPDATED, self._handle_items_updated
            )
        )

    @callback
    def _handle_items_updated(self) -> None:
        self.async_write_ha_state()
