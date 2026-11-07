"""The Fridge Vision Inventory integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, SERVICE_REPARAMETERIZE_MODEL
from .coordinator import FridgeVisionCoordinator, VisionApiError

_LOGGER = logging.getLogger(__name__)

# Platformy: IMAGE (ostatnie zdjecie) oraz SENSOR (co wykryto + czy blad true/false)
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.IMAGE]

_DATA_SERVICES_REGISTERED = f"{DOMAIN}_services_registered"


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration via config-flow."""
    return True


def _get_coordinator(hass: HomeAssistant) -> FridgeVisionCoordinator | None:
    """Return the coordinator instance."""
    for value in hass.data.get(DOMAIN, {}).values():
        if isinstance(value, FridgeVisionCoordinator):
            return value
    return None


def _require_coordinator(hass: HomeAssistant) -> FridgeVisionCoordinator:
    coordinator = _get_coordinator(hass)
    if coordinator is None:
        raise HomeAssistantError("Fridge Vision Inventory is not set up")
    return coordinator


def _async_register_services(hass: HomeAssistant) -> None:
    """Register domain services (only reparameterization remains)."""
    if hass.data.get(_DATA_SERVICES_REGISTERED):
        return
    hass.data[_DATA_SERVICES_REGISTERED] = True

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
                "No AI prompts found in simple_inventory (aliases starting with 'ai:')"
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REPARAMETERIZE_MODEL,
        _async_handle_reparameterize,
        schema=vol.Schema({}),
    )
    _LOGGER.debug("Registered service: %s", SERVICE_REPARAMETERIZE_MODEL)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Fridge Vision from a config entry."""
    coordinator = FridgeVisionCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    coordinator = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if isinstance(coordinator, FridgeVisionCoordinator):
        await coordinator.async_shutdown()

    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_REPARAMETERIZE_MODEL)
        hass.data.pop(_DATA_SERVICES_REGISTERED, None)

    return True