"""Constants for the Fridge Vision Inventory integration."""

DOMAIN = "fridge_vision_inventory"
STORAGE_KEY = "fridge_vision_inventory_data"
STORAGE_VERSION = 1

# Configuration entry keys
CONF_AI_SERVICE_URL = "ai_service_url"
CONF_CAMERA_ENTITY = "camera_entity"
CONF_DOOR_SENSOR_ENTITY = "door_sensor_entity"
CONF_CAPTURE_FPS = "capture_fps"
CONF_MOTION_VECTOR_INSERT_DIRECTION = "motion_vector_insert_direction"
CONF_CONFIDENCE_THRESHOLD = "confidence_threshold"
CONF_NOTIFY_DEVICE = "notify_device"

# Defaults
DEFAULT_CAPTURE_FPS = 4
DEFAULT_CONFIDENCE_THRESHOLD = 0.25
DEFAULT_LOCATION = "fridge"

# Motion vector directions
DIRECTION_TOP_TO_BOTTOM = "top_to_bottom"
DIRECTION_BOTTOM_TO_TOP = "bottom_to_top"

# Services
SERVICE_ADD_ITEM = "add_item"
SERVICE_UPDATE_QUANTITY = "update_quantity"
SERVICE_DELETE_ITEM = "delete_item"
SERVICE_REPARAMETERIZE_MODEL = "reparameterize_model"

# Entities
IMAGE_ENTITY_ID = "image.fridge_last_detection"
IMAGE_ENTITY_UNIQUE_ID = "fridge_last_detection"

# Notification actions (mobile_app_notification_action)
ACTION_INSERT = "fridge_vision_insert"
ACTION_REMOVE = "fridge_vision_remove"
ACTION_IGNORE = "fridge_vision_ignore"

# Notifications (Polish UI)
NOTIFICATION_TITLE = "Lodówka: Niepewny ruch"
NOTIFICATION_MESSAGE = "Wykryto: {item_name}. Co zrobiłeś?"

# Dispatcher signal for item updates (per item id)
SIGNAL_ITEM_UPDATED = f"{DOMAIN}_item_updated_{{}}"

# Capture / analysis limits
MAX_CAPTURE_SECONDS = 30.0
ANALYSIS_TIMEOUT_SECONDS = 90.0
REPARAMETERIZE_TIMEOUT_SECONDS = 30.0

# Motion classification thresholds
MOTION_THRESHOLD_PX = 15.0
