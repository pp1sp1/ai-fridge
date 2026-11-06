"""The Fridge Vision Inventory integration.

Auto-registers the bundled Lovelace card as an extra module URL, exposes
CRUD services and a WebSocket API for the card, and creates one data
coordinator per config entry.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    DEFAULT_LOCATION,
    DOMAIN,
    SERVICE_ADD_ITEM,
    SERVICE_DELETE_ITEM,
    SERVICE_REPARAMETERIZE_MODEL,
    SERVICE_UPDATE_QUANTITY,
    WS_TYPE_ITEMS,
)
from .coordinator import FridgeVisionCoordinator, VisionApiError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.IMAGE]

FRONTEND_URL_PATH = f"/{DOMAIN}/fridge-inventory-card.js"
FRONTEND_FILE_PATH = f"custom_components/{DOMAIN}/frontend/fridge-inventory-card.js"

_DATA_FRONTEND_REGISTERED = f"{DOMAIN}_frontend_registered"
_DATA_SERVICES_REGISTERED = f"{DOMAIN}_services_registered"
_DATA_WS_REGISTERED = f"{DOMAIN}_ws_registered"

SERVICE_SCHEMAS: dict[str, vol.Schema] = {
    SERVICE_ADD_ITEM: vol.Schema(
        {
            vol.Required("name"): cv.string,
            vol.Required("ai_prompt"): cv.string,
            vol.Optional("quantity", default=1): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=9999)
            ),
            vol.Optional("location", default=DEFAULT_LOCATION): cv.string,
        }
    ),
    SERVICE_UPDATE_QUANTITY: vol.Schema(
        {
            vol.Required("item_id"): cv.string,
            vol.Required("delta"): vol.All(
                vol.Coerce(int), vol.Range(min=-999, max=999)
            ),
        }
    ),
    SERVICE_DELETE_ITEM: vol.Schema({vol.Required("item_id"): cv.string}),
    SERVICE_REPARAMETERIZE_MODEL: vol.Schema({}),
}


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration (config-flow only; nothing to do here)."""
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the bundled card and inject it into every Lovelace dashboard."""
    if hass.data.get(_DATA_FRONTEND_REGISTERED):
        return
    file_path = Path(hass.config.path(FRONTEND_FILE_PATH))
    if not file_path.is_file():
        _LOGGER.error(
            "Bundled card not found at %s; frontend registration skipped",
            file_path,
        )
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL_PATH, str(file_path), cache_headers=False)]
    )
    add_extra_js_url(hass, FRONTEND_URL_PATH)
    hass.data[_DATA_FRONTEND_REGISTERED] = True
    _LOGGER.debug("Lovelace card registered at %s", FRONTEND_URL_PATH)


def _get_coordinator(hass: HomeAssistant) -> FridgeVisionCoordinator | None:
    """Return the (single) coordinator, if any entry is set up."""
    for value in hass.data.get(DOMAIN, {}).values():
        if isinstance(value, FridgeVisionCoordinator):
            return value
    return None


def _require_coordinator(hass: HomeAssistant) -> FridgeVisionCoordinator:
    coordinator = _get_coordinator(hass)
    if coordinator is None:
        raise HomeAssistantError("Fridge Vision Inventory is not set up")
    return coordinator


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain services once."""
    if hass.data.get(_DATA_SERVICES_REGISTERED):
        return
    hass.data[_DATA_SERVICES_REGISTERED] = True

    async def _async_handle_add_item(call: ServiceCall) -> None:
        await _require_coordinator(hass).async_add_item(
            name=call.data["name"],
            ai_prompt=call.data["ai_prompt"],
            quantity=call.data["quantity"],
            location=call.data["location"],
        )

    async def _async_handle_update_quantity(call: ServiceCall) -> None:
        await _require_coordinator(hass).async_update_quantity(
            call.data["item_id"], call.data["delta"]
        )

    async def _async_handle_delete_item(call: ServiceCall) -> None:
        await _require_coordinator(hass).async_delete_item(call.data["item_id"])

    async def _async_handle_reparameterize(call: ServiceCall) -> None:
        coordinator = _require_coordinator(hass)
        try:
            ok = await coordinator.async_reparameterize_model()
        except VisionApiError as err:
            raise HomeAssistantError(
                f"Re-parameterization failed: {err}"
            ) from err
        if not ok:
            raise HomeAssistantError(
                "No AI prompts configured; add an inventory item first"
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_ITEM,
        _async_handle_add_item,
        schema=SERVICE_SCHEMAS[SERVICE_ADD_ITEM],
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_QUANTITY,
        _async_handle_update_quantity,
        schema=SERVICE_SCHEMAS[SERVICE_UPDATE_QUANTITY],
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_ITEM,
        _async_handle_delete_item,
        schema=SERVICE_SCHEMAS[SERVICE_DELETE_ITEM],
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REPARAMETERIZE_MODEL,
        _async_handle_reparameterize,
        schema=SERVICE_SCHEMAS[SERVICE_REPARAMETERIZE_MODEL],
    )
    _LOGGER.debug("Domain services registered")


@callback
def _async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register the WebSocket command used by the bundled card."""
    if hass.data.get(_DATA_WS_REGISTERED):
        return
    hass.data[_DATA_WS_REGISTERED] = True

    @websocket_api.websocket_command({vol.Required("type"): WS_TYPE_ITEMS})
    @callback
    def ws_get_items(
        hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
    ) -> None:
        coordinator = _get_coordinator(hass)
        if coordinator is None:
            connection.send_error(
                msg["id"], "not_ready", "Fridge Vision Inventory is not set up"
            )
            return
        connection.send_result(
            msg["id"],
            {"items": [item.to_dict() for item in coordinator.sorted_items()]},
        )

    websocket_api.async_register_command(hass, ws_get_items)
    _LOGGER.debug("WebSocket API registered (%s)", WS_TYPE_ITEMS)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Fridge Vision Inventory from a config entry."""
    await _async_register_frontend(hass)

    coordinator = FridgeVisionCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    _async_register_services(hass)
    _async_register_websocket_api(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and shut its coordinator down."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if isinstance(coordinator, FridgeVisionCoordinator):
        await coordinator.async_shutdown()

    if not hass.data.get(DOMAIN):
        for service in (
            SERVICE_ADD_ITEM,
            SERVICE_UPDATE_QUANTITY,
            SERVICE_DELETE_ITEM,
            SERVICE_REPARAMETERIZE_MODEL,
        ):
            hass.services.async_remove(DOMAIN, service)
        hass.data.pop(_DATA_SERVICES_REGISTERED, None)
    return True
