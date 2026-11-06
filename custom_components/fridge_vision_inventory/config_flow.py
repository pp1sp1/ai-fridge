"""Config flow for the Fridge Vision Inventory integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_AI_SERVICE_URL,
    CONF_CAPTURE_FPS,
    CONF_CONFIDENCE_THRESHOLD,
    CONF_CAMERA_ENTITY,
    CONF_DOOR_SENSOR_ENTITY,
    CONF_MOTION_VECTOR_INSERT_DIRECTION,
    CONF_NOTIFY_DEVICE,
    DEFAULT_CAPTURE_FPS,
    DEFAULT_CONFIDENCE_THRESHOLD,
    DIRECTION_BOTTOM_TO_TOP,
    DIRECTION_TOP_TO_BOTTOM,
    DOMAIN,
)


class FridgeVisionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Fridge Vision Inventory."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initiated by the user."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(
                title="Fridge Vision Inventory", data=user_input
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AI_SERVICE_URL, default="http://local-fridge-vision-ai:8000"
                    ): str,
                    vol.Required(CONF_CAMERA_ENTITY): cv.entity_id,
                    vol.Required(CONF_DOOR_SENSOR_ENTITY): cv.entity_id,
                    vol.Optional(
                        CONF_CAPTURE_FPS, default=DEFAULT_CAPTURE_FPS
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
                    vol.Optional(
                        CONF_MOTION_VECTOR_INSERT_DIRECTION,
                        default=DIRECTION_TOP_TO_BOTTOM,
                    ): vol.In(
                        {
                            DIRECTION_TOP_TO_BOTTOM: "Wkładam od góry do dołu (top_to_bottom)",
                            DIRECTION_BOTTOM_TO_TOP: "Wkładam od dołu do góry (bottom_to_top)",
                        }
                    ),
                    vol.Optional(
                        CONF_CONFIDENCE_THRESHOLD, default=DEFAULT_CONFIDENCE_THRESHOLD
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
                    vol.Optional(CONF_NOTIFY_DEVICE): cv.entity_id,
                }
            ),
        )
