"""Constants for the Fridge Vision Inventory integration."""

DOMAIN = "fridge_vision_inventory"

# Storage (homeassistant.helpers.storage.Store).
STORAGE_KEY = "fridge_vision_inventory_data"
STORAGE_VERSION = 1
SAVE_DELAY = 5

# Config entry keys.
CONF_AI_SERVICE_URL = "ai_service_url"
CONF_CAMERA_ENTITY = "camera_entity"
CONF_DOOR_SENSOR_ENTITY = "door_sensor_entity"
CONF_CAPTURE_FPS = "capture_fps"
CONF_MOTION_VECTOR_INSERT_DIRECTION = "motion_vector_insert_direction"
CONF_CONFIDENCE_THRESHOLD = "confidence_threshold"
CONF_NOTIFY_DEVICE = "notify_device"

DEFAULT_AI_SERVICE_URL = "http://local_fridge-vision-ai:8000"
DEFAULT_CAPTURE_FPS = 4
DEFAULT_MOTION_VECTOR_INSERT_DIRECTION = "top_to_bottom"
DEFAULT_CONFIDENCE_THRESHOLD = 0.25
DEFAULT_LOCATION = "fridge"

VECTOR_TOP_TO_BOTTOM = "top_to_bottom"
VECTOR_BOTTOM_TO_TOP = "bottom_to_top"

# Frame buffering while the door is open.
BUFFER_SECONDS = 30
MAX_BUFFERED_FRAMES = 120
MIN_FRAMES_FOR_ANALYSIS = 2
CAPTURE_SNAPSHOT_TIMEOUT = 6.0

# Vision AI API call limits (seconds).
HEALTH_TIMEOUT = 10.0
SET_CLASSES_TIMEOUT = 60.0
ANALYZE_TIMEOUT = 300.0

# Detection actions returned by the add-on.
DETECTION_INSERTED = "INSERTED"
DETECTION_REMOVED = "REMOVED"
DETECTION_AMBIGUOUS = "AMBIGUOUS"

# Dispatcher signals.
SIGNAL_ITEMS_UPDATED = f"{DOMAIN}_items_updated"

# Actionable notifications.
EVENT_NOTIFICATION_ACTION = "mobile_app_notification_action"
ACTION_INSERT = "ACTION_INSERT"
ACTION_REMOVE = "ACTION_REMOVE"
ACTION_IGNORE = "ACTION_IGNORE"

NOTIFICATION_TITLE_AMBIGUOUS = "Lodówka: Niepewny ruch"
NOTIFICATION_TITLE_UNKNOWN_ITEM = "Lodówka: Nieznany produkt"
PENDING_LIFETIME_MINUTES = 30

NOTIFY_DOMAIN = "notify"

# WebSocket API.
WS_TYPE_ITEMS = f"{DOMAIN}/items"
WS_TYPE_SUBSCRIBE_ITEMS = f"{DOMAIN}/subscribe_items"

# Services.
SERVICE_ADD_ITEM = "add_item"
SERVICE_UPDATE_ITEM = "update_item"
SERVICE_UPDATE_QUANTITY = "update_quantity"
SERVICE_DELETE_ITEM = "delete_item"
SERVICE_REPARAMETERIZE_MODEL = "reparameterize_model"

# Item / attribute names.
ATTR_ITEM_ID = "item_id"
ATTR_AI_PROMPT = "ai_prompt"
ATTR_LOCATION = "location"
ATTR_LAST_UPDATED = "last_updated"
