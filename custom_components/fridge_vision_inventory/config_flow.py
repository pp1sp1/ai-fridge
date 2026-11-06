"""Config flow for the Fridge Vision Inventory integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

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
                    ): selector.TextSelector(),
                    vol.Required(CONF_CAMERA_ENTITY): selector.EntitySelector(),
                    vol.Required(CONF_DOOR_SENSOR_ENTITY): selector.EntitySelector(),
                    vol.Optional(
                        CONF_CAPTURE_FPS, default=DEFAULT_CAPTURE_FPS
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1, max=30, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(
                        CONF_MOTION_VECTOR_INSERT_DIRECTION,
                        default=DIRECTION_TOP_TO_BOTTOM,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=DIRECTION_TOP_TO_BOTTOM,
                                    label="Wkładam od góry do dołu (top_to_bottom)",
                                ),
                                selector.SelectOptionDict(
                                    value=DIRECTION_BOTTOM_TO_TOP,
                                    label="Wkładam od dołu do góry (bottom_to_top)",
                                ),
                            ]
                        )
                    ),
                    vol.Optional(
                        CONF_CONFIDENCE_THRESHOLD, default=DEFAULT_CONFIDENCE_THRESHOLD
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.0, max=1.0, step=0.05, mode=selector.NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(CONF_NOTIFY_DEVICE): selector.EntitySelector(),
                }
            ),
        )
