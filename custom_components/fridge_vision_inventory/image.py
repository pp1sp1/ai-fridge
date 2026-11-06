"""Image entity showing the last annotated detection frame.

The coordinator hands each annotated JPEG returned by the Vision AI
add-on to this entity; the content is served by the ``image`` component
under ``/api/image_proxy/{entity_id}`` with an access token.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

try:  # HA >= 2025.x splits the image component into submodules
    from homeassistant.components.image import Image as ImageData, ImageEntity
except ImportError:  # pragma: no cover - fallback for alternate layouts
    from homeassistant.components.image.entity import (  # type: ignore[no-redef]
        Image as ImageData,
        ImageEntity,
    )

from .const import DOMAIN
from .coordinator import FridgeVisionCoordinator, device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the last-detection image entity."""
    coordinator: FridgeVisionCoordinator = entry.runtime_data
    async_add_entities([FridgeLastDetectionImage(hass, coordinator, entry)])


class FridgeLastDetectionImage(ImageEntity):
    """Holds the latest annotated JPEG returned by the Vision AI add-on."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_detection"
    _attr_content_type = "image/jpeg"
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: FridgeVisionCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(hass)
        self._attr_unique_id = f"{entry.entry_id}::last_detection"
        self._attr_device_info = device_info(entry.entry_id)
        self._coordinator = coordinator
        self._content: bytes | None = None

    @callback
    def async_set_detection_image(self, data: bytes) -> None:
        """Store a new annotated frame and refresh state."""
        self._content = data
        self._cached_image = ImageData(
            content_type=self._attr_content_type, content=data
        )
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register with the coordinator so it can push new frames."""
        await super().async_added_to_hass()
        self._coordinator.register_image_entity(self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from the coordinator."""
        self._coordinator.unregister_image_entity(self)
