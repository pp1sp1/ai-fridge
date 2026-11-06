"""Fridge Vision Inventory integration for Home Assistant."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from homeassistant.helpers.storage import Store
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig

from .const import (
    ACTION_IGNORE,
    ACTION_INSERT,
    ACTION_REMOVE,
    CONF_AI_SERVICE_URL,
    CONF_CAPTURE_FPS,
    CONF_CONFIDENCE_THRESHOLD,
    CONF_CAMERA_ENTITY,
    CONF_DOOR_SENSOR_ENTITY,
    CONF_MOTION_VECTOR_INSERT_DIRECTION,
    CONF_NOTIFY_DEVICE,
    DEFAULT_CAPTURE_FPS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_LOCATION,
    DOMAIN,
    IMAGE_ENTITY_ID,
    SERVICE_ADD_ITEM,
    SERVICE_DELETE_ITEM,
    SERVICE_REPARAMETERIZE_MODEL,
    SERVICE_UPDATE_QUANTITY,
    SIGNAL_ITEM_UPDATED,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .coordinator import FridgeVisionCoordinator
from .image import FridgeLastDetectionImage
from .sensor import FridgeInventorySensor

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.IMAGE]

# Bundled Lovelace card: registered automatically so no manual resources setup.
CARD_URL_PATH = "/fridge_vision_inventory/fridge-inventory-card.js"
CARD_FILE_PATH = (
    "custom_components/fridge_vision_inventory/frontend/fridge-inventory-card.js"
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration and auto-register the Lovelace card."""
    hass.data.setdefault(DOMAIN, {})

    file_path = hass.config.path(CARD_FILE_PATH)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL_PATH, file_path, cache_headers=False)]
    )
    add_extra_js_url(hass, CARD_URL_PATH)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry."""
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored = await store.async_load() or {}
    items: list[dict[str, Any]] = stored.get("items", [])

    coordinator = FridgeVisionCoordinator(hass, entry, store, items)
    await coordinator.async_setup()

    domain_data: dict[str, Any] = {
        "coordinator": coordinator,
        "items": items,
        "add_entities": None,
        "image_entity": None,
        "sensors": {},
    }
    hass.data[DOMAIN][entry.entry_id] = domain_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def async_add_item(call: ServiceCall) -> None:
        name: str = call.data["name"]
        ai_prompt: str = call.data["ai_prompt"]
        quantity: int = max(0, int(call.data.get("quantity", 1)))
        location: str = call.data.get("location", DEFAULT_LOCATION)
        item: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "name": name,
            "ai_prompt": ai_prompt,
            "quantity": quantity,
            "location": location,
            "last_updated": dt_util.utcnow().isoformat(),
        }
        items.append(item)
        await coordinator.async_save()
        await coordinator.async_reparameterize_model()
        add_entities = domain_data["add_entities"]
        if add_entities is not None:
            sensor = FridgeInventorySensor(coordinator, item)
            add_entities([sensor])
            domain_data["sensors"][item["id"]] = sensor

    async def async_update_quantity(call: ServiceCall) -> None:
        item_id: str = call.data["item_id"]
        delta: int = int(call.data["delta"])
        item = coordinator.find_item(item_id)
        if item is None:
            return
        item["quantity"] = max(0, int(item.get("quantity", 0)) + delta)
        item["last_updated"] = dt_util.utcnow().isoformat()
        await coordinator.async_save()
        async_dispatcher_send(hass, SIGNAL_ITEM_UPDATED.format(item_id), item)

    async def async_delete_item(call: ServiceCall) -> None:
        item_id: str = call.data["item_id"]
        item = coordinator.find_item(item_id)
        if item is None:
            return
        items[:] = [existing for existing in items if existing.get("id") != item_id]
        sensor = domain_data["sensors"].pop(item_id, None)
        if sensor is not None:
            await sensor.async_remove(force_remove=True)
        await coordinator.async_save()
        await coordinator.async_reparameterize_model()

    async def async_reparameterize_model(call: ServiceCall) -> None:
        await coordinator.async_reparameterize_model()

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_ITEM,
        async_add_item,
        schema=vol.Schema(
            {
                vol.Required("name"): cv.string,
                vol.Required("ai_prompt"): cv.string,
                vol.Optional("quantity", default=1): vol.All(
                    vol.Coerce(int), vol.Range(min=0)
                ),
                vol.Optional("location", default=DEFAULT_LOCATION): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_QUANTITY,
        async_update_quantity,
        schema=vol.Schema(
            {
                vol.Required("item_id"): cv.string,
                vol.Required("delta"): vol.Coerce(int),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_ITEM,
        async_delete_item,
        schema=vol.Schema({vol.Required("item_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REPARAMETERIZE_MODEL,
        async_reparameterize_model,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    domain_data = hass.data[DOMAIN].get(entry.entry_id)
    if domain_data is not None:
        coordinator: FridgeVisionCoordinator = domain_data.get("coordinator")
        if coordinator is not None:
            await coordinator.async_unload()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
