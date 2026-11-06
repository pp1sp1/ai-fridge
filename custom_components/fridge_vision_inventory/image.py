"""Image entity holding the last annotated detection frame."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, IMAGE_ENTITY_ID, IMAGE_ENTITY_UNIQUE_ID


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the last-detection image entity."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    image_entity = FridgeLastDetectionImage(hass)
    domain_data["image_entity"] = image_entity
    async_add_entities([image_entity])


class FridgeLastDetectionImage(ImageEntity):
    """Image entity showing the last annotated detection frame."""

    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self._attr_unique_id = IMAGE_ENTITY_UNIQUE_ID
        self._attr_name = "Ostatnia detekcja lodówki"
        self._image: bytes | None = None
        self._attr_image_last_updated: datetime | None = None

    async def async_image(self) -> bytes | None:
        """Return the last annotated frame as JPEG bytes."""
        return self._image

    @callback
    def async_set_image(self, image: bytes | None) -> None:
        """Store a new annotated frame and refresh the entity state."""
        self._image = image
        self._attr_image_last_updated = datetime.utcnow()
        self.async_write_ha_state()
