"""Constants for the Fridge Vision Inventory integration."""

DOMAIN = "fridge_vision_inventory"

# Config entry keys.
CONF_INVENTORY_ENTITY = "inventory_entity"
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

# Actionable notifications.
EVENT_NOTIFICATION_ACTION = "mobile_app_notification_action"
ACTION_INSERT = "ACTION_INSERT"
ACTION_REMOVE = "ACTION_REMOVE"
ACTION_IGNORE = "ACTION_IGNORE"

NOTIFICATION_TITLE_AMBIGUOUS = "Lodówka: Niepewny ruch"
NOTIFICATION_TITLE_UNKNOWN_ITEM = "Lodówka: Nieznany produkt"
PENDING_LIFETIME_MINUTES = 30

NOTIFY_DOMAIN = "notify"

# Services.
SERVICE_REPARAMETERIZE_MODEL = "reparameterize_model"