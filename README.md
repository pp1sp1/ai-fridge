# Fridge Vision Inventory

All-in-one monorepo for a smart fridge inventory system: a local Home
Assistant add-on running a YOLO-World zero-shot detector, a HACS
integration that buffers camera frames while the fridge door is open and
classifies motions into insertions/removals, and an auto-injected
Lovelace card for the Polish UI.

Everything runs locally — frames never leave the machine, no cloud
services are involved.

```
├── repository.yaml                  # HA Add-on repository metadata
├── hacs.json                        # HACS metadata for the integration
├── README.md
│
├── fridge-vision-ai/                # LOCAL ADD-ON (Docker + FastAPI + YOLO-World)
│   ├── config.yaml                  # Add-on configuration and schema
│   ├── Dockerfile                   # python:3.11-slim, Torch CPU wheels
│   ├── requirements.txt
│   └── app/
│       ├── main.py                  # FastAPI endpoints
│       ├── tracker.py               # Centroid Y-axis motion tracker + annotation
│       └── model.py                 # YOLO-World v2 lifecycle & CLIP re-param
│
└── custom_components/
    └── fridge_vision_inventory/     # HACS INTEGRATION
        ├── manifest.json
        ├── __init__.py              # Auto-registers the Lovelace card
        ├── config_flow.py           # UI config (camera, door sensor, vector, ...)
        ├── const.py
        ├── coordinator.py           # Frame grabber (4 FPS), inventory Store, services logic
        ├── sensor.py                # Per-item quantity sensors
        ├── image.py                 # image.fridge_last_detection entity
        ├── services.yaml
        ├── strings.json
        ├── translations/            # en.json / pl.json
        └── frontend/
            └── fridge-inventory-card.js
```

## How it works

1. The door contact opens (`on`) → the integration grabs frames from the
   fridge camera every 250 ms (4 FPS, max 30 s buffer, ~120 frames).
2. The door closes (`off`) → the buffered sequence is posted to the
   add-on's `POST /api/analyze_sequence`.
3. The add-on runs YOLO-World v2 on every frame with the prompts from
   your inventory (zero-shot, re-parameterized via CLIP), matches boxes
   across frames with a centroid + IoU tracker and classifies each track:
   - movement aligned with the configured motion vector → `INSERTED`
   - movement opposed to it → `REMOVED`
   - shorter than 2 frames or |ΔY| below the motion threshold → `AMBIGUOUS`
4. The integration draws the annotated frame (boxes, labels, motion
   arrows) into `image.fridge_last_detection`, increments/decrements item
   quantities, and asks you via an actionable notification whenever a
   motion is ambiguous: *„Wykryto: Mleko 3.2%. Co zrobiłeś?"* with buttons
   **Włożyłem / Wyjąłem / Ignoruj**.
5. The bundled card shows the product list with `+`/`−` counters, an
   add-item dialog, and the last annotated detection image.

## Installation

### 1. Add-on (Fridge Vision AI)

1. Settings → Add-ons → Add-on Store → **⋮ → Repositories** and add this
   repository URL (or place the folder in `/addons/local/`).
2. Install **Fridge Vision AI**, then start it.
3. First start downloads the YOLO-World weights into the persistent
   `/data` volume (~25 MB for `yolov8n-worldv2.pt`, ~90 MB for the `s`
   model) — internet access is required once.

Options (add-on page → Configuration):

| Option | Default | Description |
| --- | --- | --- |
| `model` | `yolov8n-worldv2.pt` | Alternative: `yolov8s-worldv2.pt` (more accurate, ~600 MB RAM budget) |
| `image_size` | `640` | Inference resolution (320–1280). Lower = faster, less RAM |
| `log_level` | `info` | Log verbosity |

The API is exposed on port 8000 and health-checked by the Supervisor
watchdog (`/api/health`).

### 2. Integration (HACS)

1. HACS → **⋮ → Custom repositories** → add this repo with category
   **Integration**.
2. Install **Fridge Vision Inventory**, restart Home Assistant.
3. Settings → Devices & Services → **Add Integration** → *Fridge Vision
   Inventory* and fill in:

| Field | Default | Description |
| --- | --- | --- |
| Vision AI service URL | `http://local_fridge-vision-ai:8000` | Hostname is the add-on slug; verify it on the add-on page (e.g. `a0d7b954_fridge-vision-ai` for community repos). Alternative: use the mapped port via the host address. |
| Fridge camera entity | – | e.g. `camera.esphome_fridge`; must support snapshots |
| Fridge door sensor | – | Binary sensor, `on` = open |
| Capture rate (FPS) | `4` | 1–10 |
| Motion vector for insertions | `top_to_bottom` | Hand enters from top → bottom, or `bottom_to_top` |
| Confidence threshold | `0.25` | Minimum confidence for inventory changes |
| Notification target | – | e.g. `notify.mobile_app_phone`; empty = no notifications |

### 3. Lovelace card

Nothing to configure manually — the card JS is served by the integration
and auto-injected with `add_extra_js_url`. Add the card to a dashboard:

```yaml
type: custom:fridge-inventory-card
title: Zapasy w lodówce
image_entity: image.fridge_last_detection
```

## Usage

### Card

- **+ / −** buttons on each row call
  `fridge_vision_inventory.update_quantity` (delta ±1).
- **+ Dodaj produkt** opens the dialog with *Nazwa produktu (PL)*,
  *Prompt AI (EN)* and *Ilość*; saving calls
  `fridge_vision_inventory.add_item` and shows a loading state while the
  model re-parameterizes.
- **Ostatnie wykrycie** is a collapsible panel showing the annotated
  image from `image.fridge_last_detection` (boxes + motion arrows).

### Services

```yaml
# Add a product (also re-parameterizes the model)
service: fridge_vision_inventory.add_item
data:
  name: "Mleko 3.2%"
  ai_prompt: "milk carton"
  quantity: 1
  location: "fridge"

# Manual correction
service: fridge_vision_inventory.update_quantity
data:
  item_id: "<id from the item sensor attributes>"
  delta: -1

# Delete (also re-parameterizes)
service: fridge_vision_inventory.delete_item
data:
  item_id: "<id>"

# Push all unique prompts to the add-on
service: fridge_vision_inventory.reparameterize_model
```

Each item also exposes a quantity sensor (`sensor.<nazwa>`) with the
`item_id`, `ai_prompt`, `location` and `last_updated` attributes.

### Actionable notifications (AMBIGUOUS motions)

When a movement is ambiguous and the detection matches an inventory item,
a notification is sent to the configured target:

- Title: `Lodówka: Niepewny ruch`
- Message: `Wykryto: {item.name}. Co zrobiłeś?`
- Actions: `Włożyłem` (`ACTION_INSERT`), `Wyjąłem` (`ACTION_REMOVE`),
  `Ignoruj` (`ACTION_IGNORE`)

Tapping the buttons fires `mobile_app_notification_action`; the
integration adjusts the quantity accordingly (works with the HA
companion app; `item_id`/`tag` are carried in the action payload and
correlated server-side as fallback).

## AI prompt guide

Prompts are English text hints for the zero-shot detector — describe the
product visually, not by brand:

| Product (PL) | Prompt (EN) |
| --- | --- |
| Mleko 3.2% | `milk carton` |
| Masło | `butter package` |
| Ser żółty | `block of cheese` |
| Jogurt | `yogurt cup` |
| Ketchup | `ketchup bottle` |

Tips: keep prompts distinct from each other, use concrete object nouns,
and run `reparameterize_model` after editing items.

## Add-on REST API

- `GET /api/health` → `{"status": "ok", "model": "...", "model_ready": true, "classes": [...], "device": "cpu"}`
- `POST /api/set_classes` → `{"classes": ["milk carton", "butter"]}`
- `POST /api/analyze_sequence`:
  ```json
  {
    "frames": ["<base64 JPEG>", "..."],
    "vector_direction": "top_to_bottom",
    "confidence_threshold": 0.25,
    "motion_threshold": 0.05
  }
  ```
  Response:
  ```json
  {
    "detections": [
      {
        "ai_prompt": "milk carton",
        "action": "INSERTED",
        "confidence": 0.81,
        "track_length_frames": 4,
        "delta_y_px": 182.4
      }
    ],
    "annotated_image_base64": "<base64 JPEG>"
  }
  ```

`motion_threshold` is the minimum |ΔY| as a fraction of frame height
(default `0.05`); tracks shorter than 2 frames or below it are reported
as `AMBIGUOUS`.

Quick test from the host:

```bash
curl http://homeassistant.local:8000/api/health
```

## Tuning & troubleshooting

- **`cannot_connect` during setup** — the add-on is not started or the
  slug differs from `local_fridge-vision-ai`. Check the add-on page for
  the hostname (the integration probes `/api/health` before saving).
- **Wrong INSERT/REMOVE directions** — flip the *motion vector* setting,
  or verify the camera is not rotated 180° (a horizontal mirror does not
  affect the Y axis, a rotation does).
- **Too many AMBIGUOUS notifications** — raise `motion_threshold` in API
  calls, lower the camera FPS jitter, or use a more distinctive prompt.
- **Door sensor semantics** — the integration assumes `on` = open. If
  your contact sensor is inverted, wrap it in a template binary sensor.
- **Memory** — the `n` model at 640 px stays well under the ~600 MB
  budget; the `s` model or larger `image_size` increases RAM use.
- **First start is slow** — weights are downloaded once into `/data`.
- **Frames dropped** — snapshot failures are logged at debug level and
  skipped; the analysis simply uses the frames it got (min. 2 required).

## Requirements

- Home Assistant 2024.6+ (uses `StaticPathConfig`, `entry.runtime_data`)
- Home Assistant OS / Supervised (for the add-on; the integration alone
  also works with a manually run `fridge-vision-ai` container)
- CPU only — no GPU required
