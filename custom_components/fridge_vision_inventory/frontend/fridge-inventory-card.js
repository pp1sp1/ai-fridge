/**
 * Fridge Inventory Card (v3)
 * Bundled Lovelace card for the Fridge Vision Inventory integration.
 *
 * v3 follows blaineventurine/simple-inventory-card: items with quantities,
 * units, categories, expiry dates and descriptions, plus/minus buttons on
 * every row, and a dedicated change-history tab ("Historia") fed by the
 * fridge_vision_inventory/history WebSocket command - every camera or
 * manual change is listed with before/after quantities and its source.
 *
 * Scanner-free: no barcode scanning and no Open Food Facts lookups -
 * products are identified by the YOLO-World "AI prompt" that is OPTIONAL
 * (empty prompt = manual-only item that is never auto-detected).
 *
 * Other features: header with total / out-of-stock badges, add & edit
 * modals (Polish UI), delete with confirm, live search, location &
 * quantity filters, five sort modes, real-time updates via
 * fridge_vision_inventory/subscribe_items (polling fallback), overflow
 * menu with manual AI re-parameterization, toasts, and the collapsible
 * "last detection" panel with the annotated frame.
 *
 * The card is auto-injected by the integration (no Lovelace resource
 * setup needed) and ships with its own visual editor, so it can be added
 * and configured entirely from the Home Assistant UI - no YAML editing
 * required.
 */
const CARD_VERSION = "3.0.0";
const SERVICE_DOMAIN = "fridge_vision_inventory";
const WS_ITEMS_TYPE = "fridge_vision_inventory/items";
const WS_SUBSCRIBE_TYPE = "fridge_vision_inventory/subscribe_items";
const WS_HISTORY_TYPE = "fridge_vision_inventory/history";

const MAX_PROMPT_CHARS = 60; // mirrors the add-on's _MAX_PROMPT_CHARS
const MAX_QUANTITY = 9999;
const POLL_INTERVAL_MS = 60000;
const SEARCH_DEBOUNCE_MS = 300;

const SORTS = ["name", "qty_desc", "qty_asc", "location", "zero_last"];
const QTY_FILTERS = ["all", "zero", "available"];

const QTY_FILTER_LABELS = {
  zero: "tylko zerowe",
  available: "tylko dostępne",
};

const esc = (value) =>
  String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);

const fmtQty = (value) => {
  const num = Number(value);
  if (!Number.isFinite(num)) return "0";
  return String(Number.isInteger(num) ? num : Math.round(num * 100) / 100);
};

const CARD_DEFAULT_TITLE = "Zapasy w lodówce";
const CARD_DEFAULT_IMAGE_ENTITY = "image.fridge_last_detection";
const CARD_TYPE = "fridge-inventory-card";

// Toggles exposed in the visual editor (all default to true).
const CARD_TOGGLE_FIELDS = [
  ["show_header", "Pokaż nagłówek z licznikami"],
  ["show_search", "Pokaż wyszukiwanie"],
  ["show_sort", "Pokaż sortowanie"],
  ["show_filters", "Pokaż filtry"],
  ["show_add_button", "Pokaż przycisk „+ Dodaj”"],
  ["show_detection", "Pokaż panel „Ostatnie wykrycie”"],
];

function normalizeCardConfig(config) {
  const source = config && typeof config === "object" ? config : {};
  const normalized = {
    type:
      typeof source.type === "string" && source.type
        ? source.type
        : `custom:${CARD_TYPE}`,
    title:
      typeof source.title === "string" && source.title.length
        ? source.title
        : CARD_DEFAULT_TITLE,
    image_entity:
      typeof source.image_entity === "string" && source.image_entity
        ? source.image_entity
        : CARD_DEFAULT_IMAGE_ENTITY,
  };
  for (const [field] of CARD_TOGGLE_FIELDS) {
    normalized[field] = source[field] !== false;
  }
  return normalized;
}

const ICONS = {
  search:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15.5 14h-.79l-.28-.27a6.5 6.5 0 1 0-.7.7l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0A4.5 4.5 0 1 1 14 9.5 4.5 4.5 0 0 1 9.5 14z"/></svg>',
  filter:
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 5h18v2H3zm4 6h10v2H7zm3 6h4v2h-4z"/></svg>',
  dots: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8a2 2 0 1 0-2-2 2 2 0 0 0 2 2zm0 2a2 2 0 1 0 2 2 2 2 0 0 0-2-2zm0 6a2 2 0 1 0 2 2 2 2 0 0 0-2-2z"/></svg>',
};

function relativeTime(iso) {
  const time = Date.parse(iso || "");
  if (!Number.isFinite(time)) return "";
  const diffMs = Date.now() - time;
  if (diffMs < 45000) return "teraz";
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 60) return `${minutes} min temu`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} godz. temu`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days} dni temu`;
  return new Date(time).toLocaleDateString("pl-PL");
}

class FridgeInventoryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._items = [];
    this._busy = false;
    this._initialized = false;
    this._pollTimer = null;
    this._unsubEvents = null;
    this._subscribed = false;
    this._lastError = null;
    this._toastTimer = null;
    this._searchTimer = null;
    this._search = "";
    this._sort = "name";
    this._filterQty = "all";
    this._filterLocations = new Set();
    this._filtersOpen = false;
    this._menuOpen = false;
    this._deleteArmed = false;
    this._editing = null;
    this._tab = "items";
    this._history = [];
    this._documentClick = (event) => {
      if (!this._menuOpen) return;
      const path = event.composedPath ? event.composedPath() : [];
      const menu = this.shadowRoot && this.shadowRoot.getElementById("menu");
      const menuBtn = this.shadowRoot && this.shadowRoot.getElementById("menuBtn");
      if ((menu && path.includes(menu)) || (menuBtn && path.includes(menuBtn))) return;
      this._closeMenu();
    };
  }

  // ---------- Lifecycle ----------

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("Invalid configuration for fridge-inventory-card");
    }
    this._config = normalizeCardConfig(config);
    if (this._initialized) {
      this._initialized = false;
      this.shadowRoot.innerHTML = "";
    }
    if (this._hass) this._build();
  }

  set hass(hass) {
    const changed = this._hass !== hass;
    this._hass = hass;
    if (!this._initialized && this._config) {
      this._build();
    } else {
      this._updateImagePanel();
      if (changed) {
        // The WebSocket connection may have been re-established.
        this._subscribeRealtime();
      }
    }
  }

  connectedCallback() {
    document.addEventListener("click", this._documentClick, true);
    this._startPolling();
    if (this._initialized) {
      this._refreshItems();
      this._subscribeRealtime();
    }
  }

  disconnectedCallback() {
    document.removeEventListener("click", this._documentClick, true);
    this._stopPolling();
    this._unsubscribeRealtime();
  }

  getCardSize() {
    return 6;
  }

  static getStubConfig() {
    return {
      title: CARD_DEFAULT_TITLE,
      image_entity: CARD_DEFAULT_IMAGE_ENTITY,
    };
  }

  // Lets Home Assistant open the built-in visual editor (pure GUI, no YAML).
  static async getConfigElement() {
    return document.createElement("fridge-inventory-card-editor");
  }

  // ---------- UI state persistence ----------

  get _storageKey() {
    return `fvi-card:${this._config.title}`;
  }

  _saveUiState() {
    try {
      localStorage.setItem(
        this._storageKey,
        JSON.stringify({
          sort: this._sort,
          filterQty: this._filterQty,
          filterLocations: [...this._filterLocations],
        })
      );
    } catch (err) {
      /* storage unavailable — filters simply do not persist */
    }
  }

  _loadUiState() {
    try {
      const raw = localStorage.getItem(this._storageKey);
      if (!raw) return;
      const state = JSON.parse(raw);
      if (SORTS.includes(state.sort)) this._sort = state.sort;
      if (QTY_FILTERS.includes(state.filterQty)) this._filterQty = state.filterQty;
      if (Array.isArray(state.filterLocations)) {
        this._filterLocations = new Set(state.filterLocations);
      }
    } catch (err) {
      /* ignore malformed persisted state */
    }
  }

  // ---------- Data ----------

  async _refreshItems() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: WS_ITEMS_TYPE });
      this._items = Array.isArray(result && result.items) ? result.items : [];
      this._lastError = null;
    } catch (err) {
      this._lastError = "Integracja Fridge Vision Inventory nie odpowiada.";
    }
    this._renderItems();
  }

  async _subscribeRealtime() {
    this._unsubscribeRealtime();
    const connection = this._hass && this._hass.connection;
    if (!connection || typeof connection.subscribeMessage !== "function") return;
    try {
      this._unsubEvents = await connection.subscribeMessage(
        (message) => {
          const payload = message && message.event ? message.event : message;
          if (payload && Array.isArray(payload.items)) {
            this._items = payload.items;
            this._lastError = null;
            this._renderItems();
            if (this._tab === "history") this._refreshHistory();
          }
        },
        { type: WS_SUBSCRIBE_TYPE }
      );
      this._subscribed = true;
    } catch (err) {
      this._subscribed = false;
    }
  }

  _unsubscribeRealtime() {
    if (this._unsubEvents) {
      try {
        this._unsubEvents();
      } catch (err) {
        /* subscription already gone */
      }
      this._unsubEvents = null;
    }
    this._subscribed = false;
  }

  _startPolling() {
    this._stopPolling();
    this._pollTimer = setInterval(() => {
      if (this._hass && this._initialized && !this._subscribed) {
        this._refreshItems();
      }
    }, POLL_INTERVAL_MS);
  }

  _stopPolling() {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  async _onDelta(item, delta) {
    if (this._busy) return;
    if (delta < 0 && !(item.quantity > 0)) return;
    this._busy = true;
    item.quantity = Math.max(0, (Number(item.quantity) || 0) + delta);
    this._renderItems();
    try {
      await this._hass.callService(SERVICE_DOMAIN, "update_quantity", {
        item_id: item.id,
        delta,
      });
      this._lastError = null;
    } catch (err) {
      this._lastError = "Nie udało się zaktualizować ilości produktu.";
      this._toast(this._lastError, true);
    }
    this._busy = false;
    if (!this._subscribed) await this._refreshItems();
    else this._renderItems();
  }

  // ---------- DOM skeleton ----------

  _build() {
    const style = `
      :host { --fvi-danger: var(--error-color, #db4437); }
      ha-card {
        padding: 14px 16px 12px;
        background: var(--card-background-color, var(--ha-card-background, #fff));
        border-radius: var(--ha-card-border-radius, 12px);
        color: var(--primary-text-color, #212121);
      }
      .header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
      .titles { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
      .title { font-size: 1.25rem; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .actions { display: flex; align-items: center; gap: 6px; position: relative; }
      .badge {
        font-size: 0.72rem; font-weight: 600; border-radius: 999px;
        padding: 2px 8px; background: var(--secondary-background-color, #f2f4f7);
        color: var(--secondary-text-color, #727272); white-space: nowrap;
      }
      .badge.danger { background: var(--fvi-danger); color: #fff; }
      .btn {
        border: none; cursor: pointer; font-family: inherit;
        border-radius: 50%; width: 32px; height: 32px;
        font-size: 1.1rem; line-height: 1; font-weight: 700;
        background: var(--secondary-background-color, #f2f4f7);
        color: var(--primary-text-color, #212121);
        display: inline-flex; align-items: center; justify-content: center;
      }
      .btn:disabled { opacity: 0.35; cursor: default; }
      .btn.primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); border-radius: 16px; width: auto; height: auto; padding: 8px 14px; font-size: 0.9rem; font-weight: 600; }
      .btn.secondary { background: var(--secondary-background-color, #f2f4f7); border-radius: 16px; width: auto; height: auto; padding: 8px 14px; font-size: 0.9rem; }
      .btn.danger { background: var(--fvi-danger); color: #fff; border-radius: 16px; width: auto; height: auto; padding: 8px 14px; font-size: 0.9rem; font-weight: 600; }
      .btn.danger.armed { outline: 2px solid var(--fvi-danger); outline-offset: 2px; }
      .icon-btn {
        border: none; background: transparent; color: var(--secondary-text-color, #727272);
        width: 34px; height: 34px; border-radius: 50%; cursor: pointer;
        display: inline-flex; align-items: center; justify-content: center;
      }
      .icon-btn:hover { background: var(--secondary-background-color, #f2f4f7); }
      .icon-btn svg { width: 20px; height: 20px; fill: currentColor; }
      .icon-btn.active { color: var(--primary-color, #03a9f4); background: var(--secondary-background-color, #f2f4f7); }
      .icon-btn.dec, .icon-btn.inc {
        background: var(--secondary-background-color, #f2f4f7);
        color: var(--primary-text-color, #212121);
        font-size: 1.15rem; font-weight: 700;
      }
      .menu {
        position: absolute; top: 40px; right: 0; z-index: 8;
        background: var(--card-background-color, #fff);
        border: 1px solid var(--divider-color, #e0e0e0);
        border-radius: 10px; box-shadow: 0 4px 16px rgba(0,0,0,0.18);
        min-width: 210px; overflow: hidden;
      }
      .menu button {
        display: block; width: 100%; text-align: left; border: none; cursor: pointer;
        background: transparent; color: var(--primary-text-color, #212121);
        padding: 10px 14px; font-size: 0.9rem; font-family: inherit;
      }
      .menu button:hover { background: var(--secondary-background-color, #f2f4f7); }
      .toolbar { display: flex; align-items: center; gap: 6px; margin: 6px 0 4px; }
      .search-wrap { position: relative; flex: 1; min-width: 0; }
      .search-wrap svg {
        position: absolute; left: 9px; top: 50%; transform: translateY(-50%);
        width: 17px; height: 17px; fill: var(--secondary-text-color, #727272);
        pointer-events: none;
      }
      .search {
        width: 100%; box-sizing: border-box; padding: 8px 10px 8px 32px;
        border-radius: 10px; border: 1px solid var(--divider-color, #e0e0e0);
        font-size: 0.9rem; font-family: inherit;
        background: var(--secondary-background-color, #f2f4f7);
        color: var(--primary-text-color, #212121);
      }
      .select {
        padding: 7px 8px; border-radius: 10px; font-size: 0.85rem; font-family: inherit;
        border: 1px solid var(--divider-color, #e0e0e0);
        background: var(--secondary-background-color, #f2f4f7);
        color: var(--primary-text-color, #212121); max-width: 42%;
      }
      .filters {
        background: var(--secondary-background-color, #f2f4f7);
        border-radius: 10px; padding: 10px 12px; margin-top: 6px;
      }
      .filters-grid { display: flex; gap: 18px; flex-wrap: wrap; }
      .filter-label { font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--secondary-text-color, #727272); margin-bottom: 4px; }
      .check { display: flex; align-items: center; gap: 6px; font-size: 0.85rem; padding: 2px 0; cursor: pointer; }
      .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 2px; }
      .fchip {
        display: inline-flex; align-items: center; gap: 5px;
        font-size: 0.75rem; font-weight: 600; border-radius: 999px;
        background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff);
        padding: 3px 9px;
      }
      .fchip button {
        border: none; background: transparent; color: inherit; cursor: pointer;
        font-size: 0.85rem; line-height: 1; padding: 0; font-weight: 700;
      }
      .fchip.clear { background: var(--secondary-background-color, #f2f4f7); color: var(--secondary-text-color, #727272); }
      .items { list-style: none; margin: 4px 0 0; padding: 0; }
      .items li {
        display: flex; align-items: center; gap: 10px;
        padding: 8px 4px; border-bottom: 1px solid var(--divider-color, #e0e0e0);
        cursor: pointer;
      }
      .items li:hover { background: var(--secondary-background-color, rgba(242,244,247,0.5)); }
      .items li:last-child { border-bottom: none; }
      .items li.empty-stock .name { opacity: 0.45; }
      .group-header {
        cursor: default; font-size: 0.78rem; font-weight: 700; text-transform: uppercase;
        letter-spacing: 0.05em; color: var(--secondary-text-color, #727272);
        padding: 10px 4px 4px;
      }
      .group-header:hover { background: transparent; }
      .row-text { flex: 1; min-width: 0; }
      .name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .meta { display: flex; align-items: center; gap: 6px; margin-top: 3px; overflow: hidden; }
      .chip {
        font-size: 0.7rem; border-radius: 6px; padding: 1px 7px; white-space: nowrap;
        background: var(--secondary-background-color, #f2f4f7);
        color: var(--secondary-text-color, #727272);
      }
      .chip.prompt { font-family: var(--code-font-family, monospace); color: var(--primary-color, #03a9f4); }
      .chip.noprompt {
        border: 1px dashed var(--divider-color, #e0e0e0);
        background: transparent; font-style: italic;
      }
      .chip-time { margin-left: auto; }
      .meta .chip { overflow: hidden; text-overflow: ellipsis; max-width: 45%; }
      .qty-wrap { display: flex; align-items: center; gap: 6px; }
      .qty { min-width: 2em; text-align: center; font-weight: 700; font-variant-numeric: tabular-nums; }
      .unit { font-size: 0.72rem; color: var(--secondary-text-color, #727272); margin-left: 2px; }
      .tabs { display: flex; gap: 4px; margin: 2px 0 8px; border-bottom: 1px solid var(--divider-color, #e0e0e0); }
      .tab {
        flex: 1; border: none; background: transparent; font-family: inherit;
        font-size: 0.92rem; font-weight: 600; color: var(--secondary-text-color, #727272);
        padding: 8px 4px; cursor: pointer; border-bottom: 2px solid transparent;
      }
      .tab.active { color: var(--primary-color, #03a9f4); border-bottom-color: var(--primary-color, #03a9f4); }
      .hist-actions { display: flex; gap: 8px; justify-content: flex-end; margin: 4px 0 8px; }
      .hist-icon {
        width: 30px; height: 30px; border-radius: 50%; flex: none;
        display: inline-flex; align-items: center; justify-content: center;
        font-weight: 700; font-size: 1rem;
      }
      .hist-icon.inc { background: rgba(76, 175, 80, 0.18); color: var(--success-color, #4caf50); }
      .hist-icon.dec { background: rgba(219, 68, 55, 0.15); color: var(--fvi-danger); }
      .hist-icon.upd { background: var(--secondary-background-color, #f2f4f7); color: var(--secondary-text-color, #727272); }
      .hist-head { display: flex; align-items: baseline; gap: 8px; }
      .hist-what { font-weight: 500; }
      .hist-delta { font-size: 0.8rem; font-weight: 700; font-variant-numeric: tabular-nums; }
      .hist-delta.inc { color: var(--success-color, #4caf50); }
      .hist-delta.dec { color: var(--fvi-danger); }
      .src-badge {
        font-size: 0.66rem; font-weight: 700; border-radius: 999px; padding: 1px 7px;
        background: var(--secondary-background-color, #f2f4f7);
        color: var(--secondary-text-color, #727272); text-transform: uppercase; letter-spacing: 0.03em;
      }
      .src-badge.vision { background: rgba(3, 169, 244, 0.14); color: var(--primary-color, #03a9f4); }
      .hist-sub { margin-top: 2px; }
      .hist-detail { font-size: 0.74rem; color: var(--secondary-text-color, #727272); }
      .dlg textarea {
        padding: 9px 10px; border-radius: 8px; border: 1px solid var(--divider-color, #e0e0e0);
        font-size: 0.95rem; font-family: inherit; resize: vertical; min-height: 54px;
        background: var(--secondary-background-color, #f2f4f7);
        color: var(--primary-text-color, #212121);
      }
      .error {
        background: var(--fvi-danger); color: #fff; border-radius: 8px;
        padding: 8px 10px; margin: 8px 0 2px; font-size: 0.85rem;
      }
      .empty { color: var(--secondary-text-color, #727272); text-align: center; padding: 14px 0 8px; font-size: 0.92rem; }
      .empty .btn { margin-top: 10px; }
      details.detection { margin-top: 12px; border-top: 1px solid var(--divider-color, #e0e0e0); padding-top: 10px; }
      details.detection summary { cursor: pointer; font-weight: 600; color: var(--primary-color, #03a9f4); }
      .det-body { margin-top: 10px; text-align: center; }
      .det-body img { max-width: 100%; border-radius: 8px; display: block; margin: 0 auto; }
      .muted { color: var(--secondary-text-color, #727272); font-size: 0.85rem; }
      dialog.dlg {
        border: none; border-radius: 14px; padding: 0;
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #212121);
        max-width: 92vw; width: 380px;
        box-shadow: 0 10px 34px rgba(0,0,0,0.28);
      }
      dialog.dlg::backdrop { background: rgba(0, 0, 0, 0.45); }
      .dlg form { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
      .dlg h3 { margin: 0; font-size: 1.15rem; }
      .dlg label { display: flex; flex-direction: column; gap: 4px; font-size: 0.82rem; color: var(--secondary-text-color, #727272); }
      .dlg label b { color: var(--primary-text-color, #212121); font-weight: 600; }
      .req { color: var(--fvi-danger); font-weight: 700; }
      .dlg input {
        padding: 9px 10px; border-radius: 8px; border: 1px solid var(--divider-color, #e0e0e0);
        font-size: 1rem; font-family: inherit;
        background: var(--secondary-background-color, #f2f4f7);
        color: var(--primary-text-color, #212121);
      }
      .dlg input.invalid { border-color: var(--fvi-danger); }
      .dlg .helper { font-size: 0.72rem; line-height: 1.35; }
      .dlg .counter { font-size: 0.7rem; text-align: right; }
      .dlg-error { color: var(--fvi-danger); font-size: 0.82rem; min-height: 1em; }
      .dlg-meta { font-size: 0.72rem; color: var(--secondary-text-color, #727272); }
      .dlg-meta code { font-size: 0.68rem; }
      .dlg-actions { display: flex; align-items: center; gap: 8px; }
      .dlg-actions .spacer { flex: 1; }
      .toast {
        position: fixed; left: 50%; bottom: 22px; transform: translate(-50%, 16px);
        background: var(--primary-text-color, #212121); color: var(--card-background-color, #fff);
        padding: 9px 16px; border-radius: 999px; font-size: 0.85rem;
        opacity: 0; pointer-events: none; transition: opacity 0.22s, transform 0.22s;
        z-index: 99; max-width: 84vw;
      }
      .toast.show { opacity: 1; transform: translate(-50%, 0); }
      .toast.error { background: var(--fvi-danger); }
    `;
    this.shadowRoot.innerHTML = `
      <style>${style}</style>
      <ha-card>
        ${this._config.show_header ? this._headerHtml() : ""}
        <div class="tabs" id="tabs">
          <button class="tab active" data-tab="items" type="button">Produkty</button>
          <button class="tab" data-tab="history" type="button">Historia</button>
        </div>
        <div id="itemsView">
          <div id="error" class="error" hidden></div>
          ${this._toolbarHtml()}
          <div class="filters" id="filterPanel" hidden></div>
          <div class="chips" id="chips"></div>
          <ul class="items" id="list"></ul>
          <div class="empty" id="empty" hidden></div>
        </div>
        <div id="historyView" hidden>
          <div class="hist-actions">
            <button class="btn secondary" id="histRefresh" type="button">Odśwież</button>
            <button class="btn secondary" id="histClear" type="button">Wyczyść historię</button>
          </div>
          <ul class="items" id="historyList"></ul>
          <div class="empty" id="historyEmpty" hidden>Brak zmian — historia zapisuje każde zdjęcie/odłożenie produktu (kamera i ręczne zmiany).</div>
        </div>
        <details class="detection" id="detection" ${this._config.show_detection ? "" : "hidden"}>
          <summary>Ostatnie wykrycie</summary>
          <div class="det-body">
            <img id="detImg" alt="Ostatnie wykrycie" hidden />
            <div class="muted" id="detMsg">Brak wykryć — otwórz lodówkę, aby wykonać pierwszą analizę.</div>
          </div>
        </details>
      </ha-card>
      ${this._addDialogHtml()}
      ${this._editDialogHtml()}
      <div class="toast" id="toast"></div>
`;
    this._bindEvents();
    this._initialized = true;
    this._loadUiState();
    const sortSel = this.shadowRoot.getElementById("sortSel");
    if (sortSel) sortSel.value = this._sort;
    this._updateImagePanel();
    this._refreshItems();
    this._subscribeRealtime();
  }

  _headerHtml() {
    return `
      <div class="header">
        <div class="titles">
          <span class="title">${esc(this._config.title)}</span>
          <span class="badge" id="totalBadge" hidden></span>
          <span class="badge danger" id="emptyBadge" hidden></span>
        </div>
        <div class="actions">
          ${this._config.show_add_button ? `<button class="btn primary" id="addBtn" type="button">+ Dodaj</button>` : ""}
          <button class="icon-btn" id="menuBtn" type="button" aria-label="Menu karty">${ICONS.dots}</button>
          <div class="menu" id="menu" hidden>
            <button type="button" data-menu="refresh">Odśwież listę</button>
            <button type="button" data-menu="reparam">Re-parametryzacja AI</button>
          </div>
        </div>
      </div>
    `;
  }

  _toolbarHtml() {
    const showToolbar = this._config.show_search || this._config.show_sort || this._config.show_filters;
    if (!showToolbar) return "";
    return `
      <div class="toolbar">
        ${this._config.show_search ? `<div class="search-wrap">${ICONS.search}<input class="search" id="searchInput" type="search" placeholder="Szukaj produktu…" autocomplete="off" /></div>` : ""}
        ${this._config.show_filters ? `<button class="icon-btn" id="filterBtn" type="button" aria-label="Filtry" title="Filtry">${ICONS.filter}</button>` : ""}
        ${this._config.show_sort ? `
          <select class="select" id="sortSel" aria-label="Sortowanie">
            <option value="name">Nazwa A–Z</option>
            <option value="qty_desc">Ilość malejąco</option>
            <option value="qty_asc">Ilość rosnąco</option>
            <option value="location">Lokalizacja</option>
            <option value="zero_last">Puste na końcu</option>
          </select>` : ""}
      </div>
    `;
  }

  _addDialogHtml() {
    return `
      <dialog class="dlg" id="addDialog">
        <form id="addForm" novalidate>
          <h3>Nowy produkt</h3>
          <label><b>Nazwa produktu (PL) <span class="req">*</span></b>
            <input id="addName" type="text" required placeholder="Mleko 3.2%" />
          </label>
          <label><b>Prompt AI (EN)</b>
            <input id="addPrompt" type="text" placeholder="milk carton" maxlength="${MAX_PROMPT_CHARS}" />
            <span class="helper">Opcjonalny — po angielsku, np. <i>milk carton, butter, cheese</i>. Bez promptu produkt jest tylko ręczny (kamera go nie rozpozna).</span>
            <span class="counter" id="addPromptCount">0/${MAX_PROMPT_CHARS}</span>
          </label>
          <label><b>Ilość</b>
            <input id="addQty" type="number" min="0" max="${MAX_QUANTITY}" step="0.5" value="1" />
          </label>
          <label><b>Jednostka</b>
            <input id="addUnit" type="text" placeholder="szt, L, kg…" />
          </label>
          <label><b>Kategoria</b>
            <input id="addCategory" type="text" list="catList" placeholder="Nabiał" />
            <datalist id="catList"></datalist>
          </label>
          <label><b>Lokalizacja</b>
            <input id="addLocation" type="text" list="locList" placeholder="fridge" />
            <datalist id="locList"></datalist>
          </label>
          <label><b>Data ważności</b>
            <input id="addExpiry" type="date" />
          </label>
          <label><b>Opis</b>
            <textarea id="addDescription" placeholder="Opcjonalny opis…"></textarea>
          </label>
          <div class="dlg-error" id="addError"></div>
          <div class="dlg-actions">
            <button class="btn secondary" id="addCancel" type="button">Anuluj</button>
            <button class="btn primary" id="addSave" type="submit">Dodaj produkt</button>
          </div>
        </form>
      </dialog>
    `;
  }

  _editDialogHtml() {
    return `
      <dialog class="dlg" id="editDialog">
        <form id="editForm" novalidate>
          <h3>Edytuj produkt</h3>
          <label><b>Nazwa produktu (PL) <span class="req">*</span></b>
            <input id="editName" type="text" required />
          </label>
          <label><b>Prompt AI (EN)</b>
            <input id="editPrompt" type="text" maxlength="${MAX_PROMPT_CHARS}" />
            <span class="helper">Opcjonalny — zmiana promptu wywoła re-parametryzację modelu AI. Pusty prompt: produkt zostaje na liście, ale przestaje być automatycznie rozpoznawany.</span>
            <span class="counter" id="editPromptCount">0/${MAX_PROMPT_CHARS}</span>
          </label>
          <label><b>Ilość</b>
            <input id="editQty" type="number" min="0" max="${MAX_QUANTITY}" step="0.5" />
          </label>
          <label><b>Jednostka</b>
            <input id="editUnit" type="text" placeholder="szt, L, kg…" />
          </label>
          <label><b>Kategoria</b>
            <input id="editCategory" type="text" list="catList" />
          </label>
          <label><b>Lokalizacja</b>
            <input id="editLocation" type="text" list="locList" />
          </label>
          <label><b>Data ważności</b>
            <input id="editExpiry" type="date" />
          </label>
          <label><b>Opis</b>
            <textarea id="editDescription"></textarea>
          </label>
          <div class="dlg-meta" id="editMeta"></div>
          <div class="dlg-error" id="editError"></div>
          <div class="dlg-actions">
            <button class="btn danger" id="editDelete" type="button">Usuń</button>
            <span class="spacer"></span>
            <button class="btn secondary" id="editCancel" type="button">Anuluj</button>
            <button class="btn primary" id="editSave" type="submit">Zapisz</button>
          </div>
        </form>
      </dialog>
    `;
  }

  _bindEvents() {
    const byId = (id) => this.shadowRoot.getElementById(id);

    const tabs = byId("tabs");
    if (tabs) {
      tabs.addEventListener("click", (event) => {
        const btn = event.target.closest("[data-tab]");
        if (btn) this._switchTab(btn.dataset.tab);
      });
    }
    const histRefresh = byId("histRefresh");
    if (histRefresh) histRefresh.addEventListener("click", () => this._refreshHistory());
    const histClear = byId("histClear");
    if (histClear) histClear.addEventListener("click", () => this._clearHistory());

    const addBtn = byId("addBtn");
    if (addBtn) addBtn.addEventListener("click", () => this._openAddDialog());

    const menuBtn = byId("menuBtn");
    if (menuBtn) menuBtn.addEventListener("click", () => this._toggleMenu());
    const menu = byId("menu");
    if (menu) {
      menu.addEventListener("click", (event) => {
        const action = event.target.closest("[data-menu]");
        if (!action) return;
        this._closeMenu();
        if (action.dataset.menu === "refresh") {
          this._refreshItems();
          this._toast("Lista odświeżona");
        } else if (action.dataset.menu === "reparam") {
          this._manualReparameterize();
        }
      });
    }

    const searchInput = byId("searchInput");
    if (searchInput) {
      searchInput.addEventListener("input", () => {
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => {
          this._search = searchInput.value;
          this._renderItems();
        }, SEARCH_DEBOUNCE_MS);
      });
    }

    const filterBtn = byId("filterBtn");
    if (filterBtn) {
      filterBtn.addEventListener("click", () => {
        this._filtersOpen = !this._filtersOpen;
        filterBtn.classList.toggle("active", this._filtersOpen);
        this._renderFilterPanel();
      });
    }

    const sortSel = byId("sortSel");
    if (sortSel) {
      sortSel.addEventListener("change", () => {
        this._sort = SORTS.includes(sortSel.value) ? sortSel.value : "name";
        this._saveUiState();
        this._renderItems();
      });
    }

    const filterPanel = byId("filterPanel");
    if (filterPanel) {
      filterPanel.addEventListener("change", (event) => {
        const input = event.target;
        if (!input) return;
        if (input.name === "fqty") {
          this._filterQty = QTY_FILTERS.includes(input.value) ? input.value : "all";
        } else if (input.type === "checkbox" && input.dataset.loc !== undefined) {
          if (input.checked) this._filterLocations.add(input.dataset.loc);
          else this._filterLocations.delete(input.dataset.loc);
        } else {
          return;
        }
        this._saveUiState();
        this._renderItems();
      });
    }

    const chips = byId("chips");
    if (chips) {
      chips.addEventListener("click", (event) => {
        const clearAll = event.target.closest("[data-clear-all]");
        if (clearAll) {
          this._search = "";
          if (searchInput) searchInput.value = "";
          this._filterQty = "all";
          this._filterLocations.clear();
          this._saveUiState();
          this._renderItems();
          return;
        }
        const chip = event.target.closest("[data-chip]");
        if (!chip) return;
        const kind = chip.dataset.chip;
        if (kind === "search") {
          this._search = "";
          if (searchInput) searchInput.value = "";
        } else if (kind === "qty") {
          this._filterQty = "all";
        } else if (kind === "loc") {
          this._filterLocations.delete(chip.dataset.value || "");
        }
        this._saveUiState();
        this._renderItems();
      });
    }

    const list = byId("list");
    if (list) {
      list.addEventListener("click", (event) => {
        if (event.target.closest("button")) return;
        const row = event.target.closest("li[data-id]");
        if (!row) return;
        const item = this._items.find((entry) => entry.id === row.dataset.id);
        if (item) this._openEditDialog(item);
      });
    }

    const empty = byId("empty");
    if (empty) {
      empty.addEventListener("click", (event) => {
        if (event.target.closest("[data-action='clear-filters']")) {
          this._search = "";
          if (searchInput) searchInput.value = "";
          this._filterQty = "all";
          this._filterLocations.clear();
          this._saveUiState();
          this._renderItems();
        } else if (event.target.closest("[data-action='add']")) {
          this._openAddDialog();
        }
      });
    }

    byId("addCancel").addEventListener("click", () => byId("addDialog").close());
    byId("addForm").addEventListener("submit", (event) => this._submitAdd(event));
    byId("addPrompt").addEventListener("input", () =>
      this._updatePromptCounter("addPrompt", "addPromptCount")
    );

    byId("editCancel").addEventListener("click", () => byId("editDialog").close());
    byId("editForm").addEventListener("submit", (event) => this._submitEdit(event));
    byId("editPrompt").addEventListener("input", () =>
      this._updatePromptCounter("editPrompt", "editPromptCount")
    );
    byId("editDelete").addEventListener("click", () => this._deleteFromEdit());

    for (const dialogId of ["addDialog", "editDialog"]) {
      const dialog = byId(dialogId);
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
      });
      dialog.addEventListener("close", () => this._resetDeleteArm());
    }
  }

  // ---------- Menu / toast ----------

  _toggleMenu() {
    this._menuOpen = !this._menuOpen;
    const menu = this.shadowRoot.getElementById("menu");
    if (menu) menu.hidden = !this._menuOpen;
  }

  _closeMenu() {
    this._menuOpen = false;
    const menu = this.shadowRoot.getElementById("menu");
    if (menu) menu.hidden = true;
  }

  _toast(message, isError) {
    const el = this.shadowRoot.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("error", Boolean(isError));
    el.classList.add("show");
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
  }

  async _manualReparameterize() {
    try {
      await this._hass.callService(SERVICE_DOMAIN, "reparameterize_model", {});
      this._toast("Model AI zre-parametryzowany");
    } catch (err) {
      this._toast("Re-parametryzacja nie powiodła się", true);
    }
  }

  // ---------- Rendering ----------

  _visibleItems() {
    const query = this._search.trim().toLowerCase();
    const list = this._items.filter((item) => {
      if (query) {
        const haystack = `${item.name} ${item.ai_prompt} ${item.location}`.toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      if (this._filterLocations.size && !this._filterLocations.has(item.location || "")) {
        return false;
      }
      const quantity = Number(item.quantity) || 0;
      if (this._filterQty === "zero" && quantity !== 0) return false;
      if (this._filterQty === "available" && quantity <= 0) return false;
      return true;
    });
    const byName = (a, b) => String(a.name).localeCompare(String(b.name), "pl");
    switch (this._sort) {
      case "qty_desc":
        list.sort((a, b) => (Number(b.quantity) || 0) - (Number(a.quantity) || 0) || byName(a, b));
        break;
      case "qty_asc":
        list.sort((a, b) => (Number(a.quantity) || 0) - (Number(b.quantity) || 0) || byName(a, b));
        break;
      case "location":
        list.sort(
          (a, b) =>
            String(a.location || "").localeCompare(String(b.location || ""), "pl") ||
            byName(a, b)
        );
        break;
      case "zero_last":
        list.sort(
          (a, b) =>
            ((Number(a.quantity) > 0 ? 0 : 1) - (Number(b.quantity) > 0 ? 0 : 1)) ||
            byName(a, b)
        );
        break;
      default:
        list.sort(byName);
    }
    return list;
  }

  _renderItems() {
    const list = this.shadowRoot.getElementById("list");
    const empty = this.shadowRoot.getElementById("empty");
    if (!list || !empty) return;

    const errorEl = this.shadowRoot.getElementById("error");
    if (errorEl) {
      if (this._lastError) {
        errorEl.textContent = this._lastError;
        errorEl.hidden = false;
      } else {
        errorEl.hidden = true;
      }
    }
    this._renderBadges();
    this._renderChips();
    this._renderFilterPanel();

    list.innerHTML = "";
    if (!this._items.length) {
      empty.hidden = false;
      empty.innerHTML =
        "Brak produktów — dodaj pierwszy produkt, a AI nauczy się go rozpoznawać." +
        (this._config.show_add_button
          ? `<br /><button class="btn primary" type="button" data-action="add">+ Dodaj produkt</button>`
          : "");
      return;
    }

    const visible = this._visibleItems();
    if (!visible.length) {
      empty.hidden = false;
      empty.innerHTML =
        "Brak wyników dla wybranych filtrów." +
        `<br /><button class="btn secondary" type="button" data-action="clear-filters">Wyczyść filtry</button>`;
      return;
    }
    empty.hidden = true;

    if (this._sort === "location") {
      let currentLocation = null;
      for (const item of visible) {
        const location = item.location || "";
        if (location !== currentLocation) {
          currentLocation = location;
          const header = document.createElement("li");
          header.className = "group-header";
          header.textContent = location || "Bez lokalizacji";
          list.appendChild(header);
        }
        list.appendChild(this._buildRow(item));
      }
    } else {
      for (const item of visible) list.appendChild(this._buildRow(item));
    }
  }

  _buildRow(item) {
    const li = document.createElement("li");
    li.dataset.id = item.id || "";
    if (!(Number(item.quantity) > 0)) li.classList.add("empty-stock");
    const prompt = item.ai_prompt || "";
    const location = item.location || "";
    const category = item.category || "";
    const expiry = item.expiry_date || "";
    const updated = relativeTime(item.last_updated);
    li.innerHTML = `
      <div class="row-text">
        <div class="name" title="${esc(item.name)}">${esc(item.name)}</div>
        <div class="meta">
          ${prompt ? `<span class="chip prompt" title="Prompt AI: ${esc(prompt)}">${esc(prompt)}</span>` : `<span class="chip noprompt" title="Brak promptu AI — produkt nie jest automatycznie rozpoznawany">bez promptu AI</span>`}
          ${category ? `<span class="chip" title="Kategoria: ${esc(category)}">${esc(category)}</span>` : ""}
          ${location ? `<span class="chip" title="Lokalizacja: ${esc(location)}">${esc(location)}</span>` : ""}
          ${expiry ? `<span class="chip" title="Data ważności: ${esc(expiry)}">⏳ ${esc(expiry)}</span>` : ""}
          ${updated ? `<span class="chip chip-time">${esc(updated)}</span>` : ""}
        </div>
      </div>
      <div class="qty-wrap">
        <button class="btn dec" type="button" aria-label="Zmniejsz ilość: ${esc(item.name)}" ${this._busy || !(Number(item.quantity) > 0) ? "disabled" : ""}>−</button>
        <span class="qty">${esc(fmtQty(item.quantity))}${item.unit ? `<span class="unit">${esc(item.unit)}</span>` : ""}</span>
        <button class="btn inc" type="button" aria-label="Zwiększ ilość: ${esc(item.name)}" ${this._busy ? "disabled" : ""}>+</button>
      </div>
    `;
    li.querySelector(".dec").addEventListener("click", () => this._onDelta(item, -1));
    li.querySelector(".inc").addEventListener("click", () => this._onDelta(item, 1));
    return li;
  }

  _renderBadges() {
    const totalBadge = this.shadowRoot.getElementById("totalBadge");
    const emptyBadge = this.shadowRoot.getElementById("emptyBadge");
    if (!totalBadge || !emptyBadge) return;
    const total = this._items.reduce((sum, item) => sum + (Number(item.quantity) || 0), 0);
    const missing = this._items.filter((item) => !(Number(item.quantity) > 0)).length;
    if (this._items.length) {
      totalBadge.textContent = `Razem: ${total}`;
      totalBadge.hidden = false;
    } else {
      totalBadge.hidden = true;
    }
    if (missing > 0) {
      emptyBadge.textContent = `Brakuje: ${missing}`;
      emptyBadge.hidden = false;
    } else {
      emptyBadge.hidden = true;
    }
  }

  _renderChips() {
    const chips = this.shadowRoot.getElementById("chips");
    if (!chips) return;
    const parts = [];
    const query = this._search.trim();
    if (query) {
      parts.push(
        `<span class="fchip" data-chip="search" title="Kliknij, aby usunąć">Szukaj: „${esc(query)}” <button type="button" aria-label="Usuń filtr wyszukiwania">×</button></span>`
      );
    }
    for (const location of [...this._filterLocations].sort()) {
      parts.push(
        `<span class="fchip" data-chip="loc" data-value="${esc(location)}" title="Kliknij, aby usunąć">Lokalizacja: ${esc(location)} <button type="button" aria-label="Usuń filtr lokalizacji">×</button></span>`
      );
    }
    if (this._filterQty !== "all") {
      parts.push(
        `<span class="fchip" data-chip="qty" title="Kliknij, aby usunąć">Ilość: ${esc(QTY_FILTER_LABELS[this._filterQty] || this._filterQty)} <button type="button" aria-label="Usuń filtr ilości">×</button></span>`
      );
    }
    if (parts.length > 1) {
      parts.push(
        `<span class="fchip clear" data-clear-all="1" title="Usuń wszystkie filtry">Wyczyść wszystko</span>`
      );
    }
    chips.innerHTML = parts.join("");
    chips.hidden = parts.length === 0;
  }

  _renderFilterPanel() {
    const panel = this.shadowRoot.getElementById("filterPanel");
    const filterBtn = this.shadowRoot.getElementById("filterBtn");
    if (!panel) return;
    if (filterBtn) filterBtn.classList.toggle("active", this._filtersOpen);
    if (!this._filtersOpen) {
      panel.hidden = true;
      return;
    }
    const locations = [...new Set(this._items.map((item) => item.location || ""))].sort();
    const locChecks = locations
      .map((location) => {
        const checked = this._filterLocations.has(location) ? "checked" : "";
        return `<label class="check"><input type="checkbox" data-loc="${esc(location)}" ${checked} /> ${esc(location || "Bez lokalizacji")}</label>`;
      })
      .join("");
    const qtyOptions = [
      ["all", "Wszystkie"],
      ["zero", "Tylko zerowe"],
      ["available", "Tylko dostępne"],
    ]
      .map(
        ([value, label]) =>
          `<label class="check"><input type="radio" name="fqty" value="${value}" ${this._filterQty === value ? "checked" : ""} /> ${label}</label>`
      )
      .join("");
    panel.innerHTML = `
      <div class="filters-grid">
        <div>
          <div class="filter-label">Lokalizacja</div>
          ${locChecks || `<div class="muted">Brak lokalizacji</div>`}
        </div>
        <div>
          <div class="filter-label">Ilość</div>
          ${qtyOptions}
        </div>
      </div>
    `;
    panel.hidden = false;
  }

  // ---------- Tabs / history ----------

  _switchTab(tab) {
    if (tab !== "items" && tab !== "history") return;
    this._tab = tab;
    const byId = (id) => this.shadowRoot.getElementById(id);
    for (const btn of this.shadowRoot.querySelectorAll(".tab")) {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    }
    const itemsView = byId("itemsView");
    const historyView = byId("historyView");
    if (itemsView) itemsView.hidden = tab !== "items";
    if (historyView) historyView.hidden = tab !== "history";
    const detection = byId("detection");
    if (detection) {
      detection.hidden = tab !== "items" || !this._config.show_detection;
    }
    if (tab === "history") this._refreshHistory();
  }

  async _refreshHistory() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({ type: WS_HISTORY_TYPE, limit: 100 });
      this._history = Array.isArray(result && result.events) ? result.events : [];
      this._lastError = null;
    } catch (err) {
      this._history = [];
    }
    this._renderHistory();
  }

  async _clearHistory() {
    const btn = this.shadowRoot.getElementById("histClear");
    if (btn && btn.dataset.armed !== "1") {
      btn.dataset.armed = "1";
      btn.textContent = "Na pewno wyczyścić?";
      setTimeout(() => {
        if (btn.isConnected) {
          btn.dataset.armed = "";
          btn.textContent = "Wyczyść historię";
        }
      }, 4000);
      return;
    }
    if (btn) {
      btn.dataset.armed = "";
      btn.textContent = "Wyczyść historię";
    }
    try {
      await this._hass.callService(SERVICE_DOMAIN, "clear_history", {});
      this._toast("Historia wyczyszczona");
    } catch (err) {
      this._toast("Nie udało się wyczyścić historii", true);
    }
    this._refreshHistory();
  }

  _histRow(event) {
    const li = document.createElement("li");
    const type = String(event.event_type || "");
    const isInc = type === "increment" || type === "add";
    const isDec = type === "decrement" || type === "delete";
    const iconClass = isInc ? "inc" : isDec ? "dec" : "upd";
    const icon = type === "add" ? "+" : type === "delete" ? "×" : type === "update" ? "✎" : isInc ? "+" : "−";
    const what =
      type === "add"
        ? "Dodano"
        : type === "delete"
          ? "Usunięto z listy"
          : type === "update"
            ? "Edytowano"
            : type === "increment"
              ? "Zwiększono"
              : "Zmniejszono";
    const deltaClass = isInc ? "inc" : isDec ? "dec" : "";
    let deltaText = "";
    if (type === "increment") deltaText = `+${fmtQty(event.amount)}`;
    else if (type === "decrement") deltaText = `−${fmtQty(event.amount)}`;
    else if (type === "add") deltaText = `${fmtQty(event.quantity_after ?? event.amount)}`;
    else if (type === "delete") deltaText = `było ${fmtQty(event.quantity_before ?? 0)}`;
    const source = String(event.source || "manual") === "vision" ? "Kamera" : "Ręcznie";
    const before = event.quantity_before;
    const after = event.quantity_after;
    const transition =
      before != null && after != null ? `${fmtQty(before)} → ${fmtQty(after)}` : "";
    const detail = String(event.detail || "");
    li.innerHTML = `
      <span class="hist-icon ${iconClass}" aria-hidden="true">${icon}</span>
      <div class="row-text">
        <div class="hist-head">
          <span class="hist-what">${esc(what)}: ${esc(event.item_name || "")}</span>
          ${deltaText ? `<span class="hist-delta ${deltaClass}">${esc(deltaText)}</span>` : ""}
        </div>
        <div class="meta hist-sub">
          <span class="src-badge ${String(event.source) === "vision" ? "vision" : ""}">${esc(source)}</span>
          ${transition ? `<span class="chip">${esc(transition)}</span>` : ""}
          ${relativeTime(event.timestamp) ? `<span class="chip chip-time">${esc(relativeTime(event.timestamp))}</span>` : ""}
        </div>
        ${detail ? `<div class="hist-detail">${esc(detail)}</div>` : ""}
      </div>
    `;
    return li;
  }

  _renderHistory() {
    const list = this.shadowRoot.getElementById("historyList");
    const empty = this.shadowRoot.getElementById("historyEmpty");
    if (!list || !empty) return;
    list.innerHTML = "";
    if (!this._history.length) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    for (const event of this._history) list.appendChild(this._histRow(event));
  }

  // ---------- Add dialog ----------

  _openAddDialog() {
    const dialog = this.shadowRoot.getElementById("addDialog");
    this.shadowRoot.getElementById("addName").value = "";
    this.shadowRoot.getElementById("addPrompt").value = "";
    this.shadowRoot.getElementById("addQty").value = "1";
    this.shadowRoot.getElementById("addUnit").value = "";
    this.shadowRoot.getElementById("addCategory").value = "";
    this.shadowRoot.getElementById("addLocation").value = "";
    this.shadowRoot.getElementById("addExpiry").value = "";
    this.shadowRoot.getElementById("addDescription").value = "";
    this.shadowRoot.getElementById("addError").textContent = "";
    this._updatePromptCounter("addPrompt", "addPromptCount");
    this._fillDatalists();
    this._clearInvalid("addForm");
    dialog.showModal();
    this.shadowRoot.getElementById("addName").focus();
  }

  async _submitAdd(event) {
    event.preventDefault();
    const dialog = this.shadowRoot.getElementById("addDialog");
    const nameInput = this.shadowRoot.getElementById("addName");
    const promptInput = this.shadowRoot.getElementById("addPrompt");
    const qtyInput = this.shadowRoot.getElementById("addQty");
    const unitInput = this.shadowRoot.getElementById("addUnit");
    const categoryInput = this.shadowRoot.getElementById("addCategory");
    const locationInput = this.shadowRoot.getElementById("addLocation");
    const expiryInput = this.shadowRoot.getElementById("addExpiry");
    const descriptionInput = this.shadowRoot.getElementById("addDescription");
    const dlgError = this.shadowRoot.getElementById("addError");
    const saveBtn = this.shadowRoot.getElementById("addSave");

    const name = nameInput.value.trim();
    const aiPrompt = promptInput.value.trim();
    const quantity = this._parseQuantity(qtyInput.value);
    const unit = unitInput.value.trim();
    const category = categoryInput.value.trim();
    const location = locationInput.value.trim();
    const expiryDate = expiryInput.value || "";
    const description = descriptionInput.value.trim();

    this._clearInvalid("addForm");
    if (!name) {
      dlgError.textContent = "Podaj nazwę produktu (PL).";
      nameInput.classList.add("invalid");
      return;
    }
    if (aiPrompt.length > MAX_PROMPT_CHARS) {
      dlgError.textContent = `Prompt AI może mieć maksymalnie ${MAX_PROMPT_CHARS} znaków.`;
      promptInput.classList.add("invalid");
      return;
    }

    saveBtn.disabled = true;
    const originalLabel = saveBtn.textContent;
    saveBtn.textContent = aiPrompt ? "Zapisuję… re-parametryzacja AI" : "Zapisuję…";
    try {
      await this._hass.callService(SERVICE_DOMAIN, "add_item", {
        name,
        ai_prompt: aiPrompt,
        quantity,
        location: location || undefined,
        unit: unit || undefined,
        category: category || undefined,
        expiry_date: expiryDate || undefined,
        description: description || undefined,
      });
      this._lastError = null;
      dialog.close();
      this._toast(`Dodano: ${name}`);
      if (!this._subscribed) await this._refreshItems();
    } catch (err) {
      dlgError.textContent = "Nie udało się dodać produktu. Spróbuj ponownie.";
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = originalLabel;
    }
  }

  // ---------- Edit dialog ----------

  _openEditDialog(item) {
    const dialog = this.shadowRoot.getElementById("editDialog");
    this._editing = item;
    this._deleteArmed = false;
    const deleteBtn = this.shadowRoot.getElementById("editDelete");
    deleteBtn.textContent = "Usuń";
    deleteBtn.classList.remove("armed");
    this.shadowRoot.getElementById("editName").value = item.name || "";
    this.shadowRoot.getElementById("editPrompt").value = item.ai_prompt || "";
    this.shadowRoot.getElementById("editQty").value = String(Number(item.quantity) || 0);
    this.shadowRoot.getElementById("editUnit").value = item.unit || "";
    this.shadowRoot.getElementById("editCategory").value = item.category || "";
    this.shadowRoot.getElementById("editLocation").value = item.location || "";
    this.shadowRoot.getElementById("editExpiry").value = item.expiry_date || "";
    this.shadowRoot.getElementById("editDescription").value = item.description || "";
    this.shadowRoot.getElementById("editError").textContent = "";
    const shortId = String(item.id || "").slice(0, 8);
    this.shadowRoot.getElementById("editMeta").innerHTML =
      `ID: <code>${esc(shortId)}…</code>` +
      (item.last_updated
        ? ` · Zaktualizowano: ${esc(relativeTime(item.last_updated))}`
        : "");
    this._updatePromptCounter("editPrompt", "editPromptCount");
    this._fillDatalists();
    this._clearInvalid("editForm");
    dialog.showModal();
  }

  async _submitEdit(event) {
    event.preventDefault();
    const item = this._editing;
    if (!item) return;
    const nameInput = this.shadowRoot.getElementById("editName");
    const promptInput = this.shadowRoot.getElementById("editPrompt");
    const qtyInput = this.shadowRoot.getElementById("editQty");
    const unitInput = this.shadowRoot.getElementById("editUnit");
    const categoryInput = this.shadowRoot.getElementById("editCategory");
    const locationInput = this.shadowRoot.getElementById("editLocation");
    const expiryInput = this.shadowRoot.getElementById("editExpiry");
    const descriptionInput = this.shadowRoot.getElementById("editDescription");
    const dlgError = this.shadowRoot.getElementById("editError");
    const saveBtn = this.shadowRoot.getElementById("editSave");

    const name = nameInput.value.trim();
    const aiPrompt = promptInput.value.trim();
    const quantity = this._parseQuantity(qtyInput.value);
    const unit = unitInput.value.trim();
    const category = categoryInput.value.trim();
    const location = locationInput.value.trim();
    const expiryDate = expiryInput.value || "";
    const description = descriptionInput.value.trim();

    this._clearInvalid("editForm");
    if (!name) {
      dlgError.textContent = "Podaj nazwę produktu (PL).";
      nameInput.classList.add("invalid");
      return;
    }
    if (aiPrompt.length > MAX_PROMPT_CHARS) {
      dlgError.textContent = `Prompt AI może mieć maksymalnie ${MAX_PROMPT_CHARS} znaków.`;
      promptInput.classList.add("invalid");
      return;
    }
    const promptCleared = !aiPrompt && Boolean(String(item.ai_prompt || "").trim());
    const promptChanged =
      aiPrompt.toLowerCase() !== String(item.ai_prompt || "").toLowerCase();
    saveBtn.disabled = true;
    const originalLabel = saveBtn.textContent;
    saveBtn.textContent = promptChanged
      ? "Zapisuję… re-parametryzacja AI"
      : "Zapisuję…";
    try {
      await this._hass.callService(SERVICE_DOMAIN, "update_item", {
        item_id: item.id,
        name,
        ai_prompt: aiPrompt,
        quantity,
        location,
        unit,
        category,
        expiry_date: expiryDate,
        description,
      });
      this._lastError = null;
      this.shadowRoot.getElementById("editDialog").close();
      this._toast(
        promptCleared
          ? "Zapisano — produkt bez promptu AI nie będzie rozpoznawany"
          : "Zapisano zmiany"
      );
      if (!this._subscribed) await this._refreshItems();
    } catch (err) {
      dlgError.textContent = "Nie udało się zapisać zmian. Spróbuj ponownie.";
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = originalLabel;
    }
  }

  async _deleteFromEdit() {
    const item = this._editing;
    if (!item) return;
    const deleteBtn = this.shadowRoot.getElementById("editDelete");
    if (!this._deleteArmed) {
      this._deleteArmed = true;
      deleteBtn.textContent = "Na pewno usunąć?";
      deleteBtn.classList.add("armed");
      return;
    }
    deleteBtn.disabled = true;
    try {
      await this._hass.callService(SERVICE_DOMAIN, "delete_item", {
        item_id: item.id,
      });
      this._lastError = null;
      this.shadowRoot.getElementById("editDialog").close();
      this._toast(`Usunięto: ${item.name}`);
      if (!this._subscribed) await this._refreshItems();
    } catch (err) {
      this.shadowRoot.getElementById("editError").textContent =
        "Nie udało się usunąć produktu. Spróbuj ponownie.";
    } finally {
      deleteBtn.disabled = false;
      this._resetDeleteArm();
    }
  }

  _resetDeleteArm() {
    this._deleteArmed = false;
    const deleteBtn = this.shadowRoot.getElementById("editDelete");
    if (deleteBtn) {
      deleteBtn.textContent = "Usuń";
      deleteBtn.classList.remove("armed");
    }
  }

  // ---------- Helpers ----------

  _parseQuantity(raw) {
    const parsed = parseFloat(String(raw).replace(",", "."));
    if (!Number.isFinite(parsed) || parsed < 0) return 0;
    return Math.min(parsed, MAX_QUANTITY);
  }

  _updatePromptCounter(inputId, counterId) {
    const input = this.shadowRoot.getElementById(inputId);
    const counter = this.shadowRoot.getElementById(counterId);
    if (input && counter) {
      counter.textContent = `${input.value.length}/${MAX_PROMPT_CHARS}`;
    }
  }

  _fillDatalists() {
    const locList = this.shadowRoot.getElementById("locList");
    if (locList) {
      const locations = [...new Set(this._items.map((item) => item.location || ""))]
        .filter(Boolean)
        .sort();
      locList.innerHTML = locations
        .map((location) => `<option value="${esc(location)}"></option>`)
        .join("");
    }
    const catList = this.shadowRoot.getElementById("catList");
    if (catList) {
      const categories = [...new Set(this._items.map((item) => item.category || ""))]
        .filter(Boolean)
        .sort();
      catList.innerHTML = categories
        .map((category) => `<option value="${esc(category)}"></option>`)
        .join("");
    }
  }

  _clearInvalid(formId) {
    const form = this.shadowRoot.getElementById(formId);
    if (form) form.querySelectorAll(".invalid").forEach((el) => el.classList.remove("invalid"));
  }

  // ---------- Last detection panel ----------

  _updateImagePanel() {
    if (!this._initialized || !this._hass) return;
    const img = this.shadowRoot.getElementById("detImg");
    const msg = this.shadowRoot.getElementById("detMsg");
    if (!img || !msg) return;
    const state = this._hass.states ? this._hass.states[this._config.image_entity] : null;
    const url = state && state.attributes ? state.attributes.entity_picture : null;
    if (url) {
      if (img.getAttribute("src") !== url) img.setAttribute("src", url);
      img.hidden = false;
      msg.hidden = true;
      msg.textContent = "";
    } else {
      img.hidden = true;
      img.removeAttribute("src");
      msg.hidden = false;
      msg.textContent = state
        ? "Brak wykryć — otwórz lodówkę, aby wykonać pierwszą analizę."
        : `Encja ${this._config.image_entity} nie została znaleziona.`;
    }
  }
}

if (!window.customCards) {
  window.customCards = [];
}
if (!window.customCards.some((card) => card.type === "fridge-inventory-card")) {
  window.customCards.push({
    type: "fridge-inventory-card",
    name: "Fridge Inventory Card",
    description:
      "Inwentarz lodówki (YOLO-World): dodawanie i edycja produktów, filtry, sortowanie, podglad wykryc.",
  });
}

if (!customElements.get("fridge-inventory-card")) {
  customElements.define("fridge-inventory-card", FridgeInventoryCard);
}

// ---------- Visual editor (full GUI configuration, no YAML needed) ----------

class FridgeInventoryCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._ready = false;
  }

  setConfig(config) {
    this._config = normalizeCardConfig(config);
    if (this._ready) this._syncInputs();
    else this._render();
  }

  _emitConfig() {
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: { ...this._config } },
        bubbles: true,
        composed: true,
      })
    );
  }

  _imageEntityOptions() {
    try {
      const homeAssistant = document.querySelector("home-assistant");
      const hass = homeAssistant && homeAssistant.hass;
      if (!hass || !hass.states) return [];
      return Object.keys(hass.states)
        .filter((entityId) => entityId.startsWith("image."))
        .sort();
    } catch (err) {
      return [];
    }
  }

  _render() {
    const entities = this._imageEntityOptions();
    const style = `
      :host { display: block; }
      .editor { display: flex; flex-direction: column; gap: 14px; }
      .field { display: flex; flex-direction: column; gap: 5px; }
      .field-title, .field > span { font-size: 0.8rem; font-weight: 600; color: var(--primary-text-color, #212121); }
      .field-title { display: block; margin-bottom: 7px; }
      .field > small { font-size: 0.72rem; line-height: 1.4; color: var(--secondary-text-color, #727272); }
      input[type="text"] {
        padding: 8px 10px; border-radius: 8px; font-size: 0.95rem; font-family: inherit;
        border: 1px solid var(--divider-color, #e0e0e0);
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #212121);
      }
      .toggles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px 16px; }
      .check { display: flex; align-items: center; gap: 8px; font-size: 0.85rem; cursor: pointer; color: var(--primary-text-color, #212121); }
      .check input { accent-color: var(--primary-color, #03a9f4); }
    `;
    const toggles = CARD_TOGGLE_FIELDS.map(
      ([field, label]) =>
        `<label class="check"><input type="checkbox" id="${field}" /> ${label}</label>`
    ).join("");
    this.shadowRoot.innerHTML = `
      <style>${style}</style>
      <div class="editor">
        <div class="field">
          <span>Tytuł karty</span>
          <input id="title" type="text" />
          <small>Np. „Zapasy w lodówce” — widoczny w nagłówku karty.</small>
        </div>
        <div class="field">
          <span>Encja obrazu z ostatnim wykryciem</span>
          <input id="image_entity" type="text" list="fviEntityList" />
          <small>Domyślnie image.fridge_last_detection — encje image.* są podpowiadane.</small>
          <datalist id="fviEntityList">
            ${entities
              .map((entityId) => `<option value="${esc(entityId)}"></option>`)
              .join("")}
          </datalist>
        </div>
        <div>
          <span class="field-title">Sekcje karty</span>
          <div class="toggles">${toggles}</div>
        </div>
      </div>
    `;
    const byId = (id) => this.shadowRoot.getElementById(id);
    byId("title").addEventListener("input", () => {
      this._config.title = byId("title").value;
      this._emitConfig();
    });
    byId("image_entity").addEventListener("input", () => {
      this._config.image_entity = byId("image_entity").value.trim();
      this._emitConfig();
    });
    for (const [field] of CARD_TOGGLE_FIELDS) {
      byId(field).addEventListener("change", () => {
        this._config[field] = byId(field).checked;
        this._emitConfig();
      });
    }
    this._ready = true;
    this._syncInputs();
  }

  _syncInputs() {
    const byId = (id) => this.shadowRoot.getElementById(id);
    const title = byId("title");
    if (title && title.value !== this._config.title) title.value = this._config.title;
    const imageEntity = byId("image_entity");
    if (imageEntity && imageEntity.value !== this._config.image_entity) {
      imageEntity.value = this._config.image_entity;
    }
    const datalist = byId("fviEntityList");
    if (datalist) {
      const entities = this._imageEntityOptions();
      datalist.innerHTML = entities
        .map((entityId) => `<option value="${esc(entityId)}"></option>`)
        .join("");
    }
    for (const [field] of CARD_TOGGLE_FIELDS) {
      const box = byId(field);
      if (box) box.checked = Boolean(this._config[field]);
    }
  }
}

if (!customElements.get("fridge-inventory-card-editor")) {
  customElements.define("fridge-inventory-card-editor", FridgeInventoryCardEditor);
}

console.info(
  `%c FRIDGE-INVENTORY-CARD %c ${CARD_VERSION} `,
  "background: #2196f3; color: #fff; border-radius: 3px 0 0 3px; padding: 2px 4px;",
  "background: #e3f2fd; color: #0d47a1; border-radius: 0 3px 3px 0; padding: 2px 4px;"
);
