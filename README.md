# Fridge Vision Inventory

End-to-end smart fridge inventory system for Home Assistant, packaged inside a **single Git repository** that serves both as:

* a **Home Assistant Add-on repository** (`fridge-vision-ai/` — Docker add-on with YOLO-World v2 + FastAPI), and
* a **HACS Integration repository** (`custom_components/fridge_vision_inventory/` — config flow, sensors, image entity, services, and a bundled Lovelace card).

The system watches a fridge camera while the door is open, sends the frame sequence to the on-premise AI add-on, classifies insert/remove motion along the Y-axis with YOLO-World v2 zero-shot detection, and updates a per-item inventory with Polish product names. Uncertain events trigger actionable mobile notifications (Włożyłem / Wyjąłem / Ignoruj).

## Repository layout

```text
├── repository.yaml              # HA Add-on repository metadata
├── README.md
│
├── fridge-vision-ai/            # LOCAL ADD-ON (Docker + FastAPI + YOLO-World)
│   ├── config.yaml              # Add-on configuration and options schema
│   ├── Dockerfile               # python:3.11-slim, Torch CPU-only wheels
│   ├── requirements.txt
│   └── app/
│       ├── main.py              # FastAPI: /api/set_classes, /api/analyze_sequence
│       ├── tracker.py           # Centroid Y-axis motion tracker
│       └── model.py             # YOLO-World v2 lifecycle & CLIP re-parametrization
│
└── custom_components/
    └── fridge_vision_inventory/ # HACS INTEGRATION
        ├── manifest.json
        ├── __init__.py          # Auto-registers the Lovelace card via add_extra_js_url
        ├── config_flow.py       # UI configuration flow
        ├── const.py
        ├── coordinator.py       # Door-open frame capture (4 FPS) and analysis
        ├── sensor.py            # Item count sensors
        ├── image.py             # image.fridge_last_detection (last annotated frame)
        ├── services.yaml        # CRUD services for inventory items
        ├── strings.json
        └── frontend/
            └── fridge-inventory-card.js # Bundled Lovelace card (fork of simple-inventory-card, scanner removed)
```

## Features

* **Add-on (`fridge-vision-ai`)**
  * `python:3.11-slim` Docker image with PyTorch CPU-only wheels (under ~600 MB RAM).
  * `yolov8s-worldv2.pt` (configurable to `yolov8n-worldv2.pt`) loaded once at startup.
  * `POST /api/set_classes` — zero-shot re-parametrization of the CLIP text encoder from the inventory's English prompts.
  * `POST /api/analyze_sequence` — runs inference over ~4 FPS frame sequences, matches bounding boxes across frames by centroid distance and IoU, classifies `INSERTED` / `REMOVED` / `AMBIGUOUS` by Y-axis centroid delta vs the configured insertion direction, and returns an annotated JPEG (boxes, labels, motion arrows).

* **Integration (`fridge_vision_inventory`)**
  * Config flow: AI service URL, camera entity, door contact sensor, capture FPS, insert direction, confidence threshold, notification target.
  * Persisted inventory (Store) with Polish display names, English AI prompts, quantities, locations, timestamps.
  * Automatic detection cycle: door open → capture frames at 4 FPS (max 30 s) → door close → analyze → update quantities.
  * Actionable notifications for ambiguous events (`mobile_app_notification_action`).
  * Services: `add_item`, `update_quantity`, `delete_item`, `reparameterize_model`.
  * Entities: one `sensor` per item plus `image.fridge_last_detection`.

* **Bundled Lovelace card (`fridge-inventory-card.js`)**
  * Auto-registered via `add_extra_js_url` — no manual Lovelace resource setup.
  * Product list with Polish names and `+` / `−` counter buttons.
  * Add-item dialog (Nazwa produktu (PL), Prompt AI (EN), Ilość) with loading state while the model re-parametrizes.
  * Collapsible last-detection view with the annotated frame.
  * Fork of `simple-inventory-card` — all scanner/barcode code (Quagga/ZXing, camera stream) removed.

## Installation

### Add-on (AI service)

1. Add this repository as a custom **Add-on repository** in Home Assistant (Supervisor → Add-on Stores → `⋮` → Repositories) using the URL of this Git repo.
2. Install the **Fridge Vision AI** add-on, start it, and note its URL (e.g. `http://local-fridge-vision-ai:8000`).
3. Optionally adjust the model (`yolov8s-worldv2.pt` default) and confidence threshold in add-on options.

### Integration (HACS)

1. Add this repository as a custom HACS repository (Integration category).
2. Install **Fridge Vision Inventory**, restart Home Assistant.
3. Go to Settings → Devices & Services → Add Integration → **Fridge Vision Inventory** and complete the config flow.
4. Add the Lovelace card manually to a dashboard:
   ```yaml
   type: custom:fridge-inventory-card
   ```
   The card's JavaScript URL is registered automatically; no `resources` entry is needed.

## Detection cycle

1. Door contact turns `on` (door opens).
2. The integration grabs camera snapshots every 250 ms (~4 FPS) for up to 30 s.
3. Door contact turns `off` (door closes) → frames are posted to `POST /api/analyze_sequence`.
4. For every track with `INSERTED` and confidence ≥ threshold: `item.quantity += 1`.
5. For every track with `REMOVED` and confidence ≥ threshold: `item.quantity = max(0, quantity − 1)`.
6. `AMBIGUOUS` tracks trigger an actionable notification in Polish:
   * Title: `Lodówka: Niepewny ruch`
   * Message: `Wykryto: {item.name}. Co zrobiłeś?`
   * Actions: `Włożyłem` (insert), `Wyjąłem` (remove), `Ignoruj` (ignore).

## Services

* `fridge_vision_inventory.add_item` — `name` (PL), `ai_prompt` (EN), `quantity` (default 1), `location` (default `fridge`). Triggers model re-parametrization.
* `fridge_vision_inventory.update_quantity` — `item_id`, `delta` (+1 / −1).
* `fridge_vision_inventory.delete_item` — `item_id`. Triggers model re-parametrization.
* `fridge_vision_inventory.reparameterize_model` — sends all unique `ai_prompt`s to the add-on.

## Requirements

* Home Assistant 2023.9+ (image entities, `add_extra_js_url`, config flow selectors).
* ESPHome (or any HA-compatible) camera providing snapshots via `camera.snapshot`.
* A binary door/contact sensor.
