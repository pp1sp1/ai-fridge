/**
 * fridge-inventory-card.js
 *
 * Bundled Lovelace card for Fridge Vision Inventory.
 * Auto-registered by the integration via add_extra_js_url; add to a dashboard
 * as: type: custom:fridge-inventory-card
 */

(function () {
  "use strict";

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[c];
    });
  }

  const CSS = `
    :host {
      display: block;
      box-sizing: border-box;
      font-family: var(--primary-font-family, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif);
      color: var(--primary-text-color, #212121);
    }
    ha-card {
      overflow: hidden;
    }
    .card-content {
      padding: 20px 24px;
    }
    .card-header {
      font-size: 1.4rem;
      font-weight: 600;
      margin: 0 0 16px 0;
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--primary-text-color, #212121);
    }
    .items {
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-bottom: 20px;
    }
    .items-empty {
      color: var(--secondary-text-color, #757575);
      font-style: italic;
      padding: 16px 0;
    }
    .item {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 14px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 8px;
      background: var(--secondary-background-color, #fafafa);
    }
    .item-name {
      flex: 1;
      font-weight: 500;
      font-size: 1.05rem;
      word-break: break-word;
    }
    .item-meta {
      flex: 0 1 auto;
      max-width: 120px;
      font-size: 0.85rem;
      color: var(--secondary-text-color, #757575);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .item-qty {
      font-size: 1.2rem;
      font-variant-numeric: tabular-nums;
      min-width: 34px;
      text-align: center;
      font-weight: 600;
    }
    .qty-button {
      width: 36px;
      height: 36px;
      border: none;
      border-radius: 6px;
      font-size: 1.4rem;
      font-weight: 700;
      line-height: 1;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: opacity 0.2s;
    }
    .qty-button[data-action="inc"] {
      background: var(--success-color, #4caf50);
      color: #ffffff;
    }
    .qty-button[data-action="dec"] {
      background: var(--error-color, #f44336);
      color: #ffffff;
    }
    .qty-button:disabled {
      opacity: 0.35;
      cursor: not-allowed;
    }
    .button-add {
      width: 100%;
      padding: 12px;
      border: none;
      border-radius: 8px;
      background: var(--accent-color, var(--primary-color, #039be5));
      color: #ffffff;
      font-size: 1rem;
      font-weight: 600;
      cursor: pointer;
      margin-top: 4px;
    }
    .button-add:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .detection-panel {
      margin-top: 24px;
      border-top: 1px solid var(--divider-color, #e0e0e0);
      padding-top: 12px;
    }
    .detection-panel summary {
      font-weight: 600;
      font-size: 1rem;
      cursor: pointer;
      padding: 8px 0;
      list-style: none;
      user-select: none;
    }
    .detection-panel summary::-webkit-details-marker {
      display: none;
    }
    .detection-panel summary::after {
      content: "▾";
      float: right;
      font-size: 0.9rem;
    }
    .detection-panel[open] summary::after {
      content: "▴";
    }
    .detection-content {
      margin-top: 10px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 10px;
    }
    .detection-image {
      max-width: 100%;
      max-height: 320px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 8px;
      background: #000000;
      object-fit: contain;
    }
    .hint {
      color: var(--secondary-text-color, #757575);
      font-size: 0.9rem;
      margin: 8px 0;
    }
    /* Dialog */
    .dialog-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.55);
      backdrop-filter: blur(2px);
      z-index: 1000;
      align-items: center;
      justify-content: center;
      padding: 20px;
      box-sizing: border-box;
    }
    .dialog-backdrop.open {
      display: flex;
    }
    .dialog-box {
      background: var(--card-background-color, var(--ha-card-background, #ffffff));
      color: var(--primary-text-color, #212121);
      border-radius: 10px;
      padding: 24px;
      width: 100%;
      max-width: 420px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
    }
    .dialog-box h3 {
      margin: 0 0 16px 0;
      font-size: 1.25rem;
    }
    .field {
      margin-bottom: 14px;
    }
    .field label {
      display: block;
      margin-bottom: 6px;
      font-size: 0.9rem;
      font-weight: 500;
      color: var(--secondary-text-color, #757575);
    }
    .field input {
      width: 100%;
      box-sizing: border-box;
      padding: 10px 12px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 6px;
      background: var(--secondary-background-color, #fafafa);
      color: var(--primary-text-color, #212121);
      font-size: 0.95rem;
      outline: none;
    }
    .field input:focus {
      border-color: var(--primary-color, #039be5);
    }
    .error-msg {
      color: var(--error-color, #f44336);
      font-size: 0.85rem;
      margin-top: 8px;
      display: none;
    }
    .error-msg.visible {
      display: block;
    }
    .dialog-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      justify-content: flex-end;
      margin-top: 20px;
    }
    .button-cancel {
      padding: 8px 16px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 6px;
      background: transparent;
      color: var(--primary-text-color, #212121);
      cursor: pointer;
      font-size: 0.95rem;
    }
    .button-primary {
      padding: 8px 18px;
      border: none;
      border-radius: 6px;
      background: var(--accent-color, var(--primary-color, #039be5));
      color: #ffffff;
      font-size: 0.95rem;
      font-weight: 600;
      cursor: pointer;
    }
    .button-primary:disabled,
    .button-cancel:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .loading {
      font-size: 0.85rem;
      color: var(--secondary-text-color, #757575);
      margin-right: auto;
    }
  `;

  class FridgeInventoryCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });

      this._hass = null;
      this._config = {};
      this._dialogOpen = false;
      this._saving = false;
      this._lastItemsSignature = null;
      this._lastImageSignature = null;

      this._initDom();
      this.shadowRoot.addEventListener("click", this._onClick.bind(this));
    }

    /* Tworzy bazowy szkielet DOM tylko raz, aby nie niszczyć inputów podczas pisania */
    _initDom() {
      this.shadowRoot.innerHTML = `
        <style>${CSS}</style>
        <ha-card>
          <div class="card-content">
            <div class="card-header" id="card-title">Fridge Vision Inventory</div>
            <div class="items" id="items-container">
              <div class="items-empty">Ładowanie...</div>
            </div>
            <button type="button" class="button-add" data-action="open-add">+ Dodaj produkt</button>

            <details class="detection-panel">
              <summary>Podgląd ostatniej detekcji</summary>
              <div class="detection-content" id="detection-container">
                <p class="hint">Brak nagranych detekcji.</p>
              </div>
            </details>
          </div>
        </ha-card>

        <div class="dialog-backdrop" id="dialog-backdrop" data-action="backdrop-close">
          <div class="dialog-box">
            <h3>Dodaj produkt</h3>
            <div class="field">
              <label for="fi-name">Nazwa produktu (PL) *</label>
              <input type="text" id="fi-name" placeholder="np. Mleko 3.2%" autocomplete="off">
            </div>
            <div class="field">
              <label for="fi-ai_prompt">Prompt AI (EN) *</label>
              <input type="text" id="fi-ai_prompt" placeholder="np. milk carton" autocomplete="off">
            </div>
            <div class="field">
              <label for="fi-quantity">Ilość</label>
              <input type="number" id="fi-quantity" value="1" min="0">
            </div>
            <div class="field">
              <label for="fi-location">Lokalizacja</label>
              <input type="text" id="fi-location" value="fridge" autocomplete="off">
            </div>
            <div class="error-msg" id="fi-error"></div>
            <div class="dialog-actions">
              <span class="loading" id="fi-loading" style="display: none;">Przetwarzanie AI…</span>
              <button type="button" class="button-cancel" id="fi-cancel-btn" data-action="close-dialog">Anuluj</button>
              <button type="button" class="button-primary" id="fi-save-btn" data-action="save-add">Zapisz</button>
            </div>
          </div>
        </div>
      `;

      this._itemsContainer = this.shadowRoot.querySelector("#items-container");
      this._detectionContainer = this.shadowRoot.querySelector("#detection-container");
      this._dialogBackdrop = this.shadowRoot.querySelector("#dialog-backdrop");
      this._errorEl = this.shadowRoot.querySelector("#fi-error");
      this._loadingEl = this.shadowRoot.querySelector("#fi-loading");
      this._saveBtn = this.shadowRoot.querySelector("#fi-save-btn");
      this._cancelBtn = this.shadowRoot.querySelector("#fi-cancel-btn");
      this._titleEl = this.shadowRoot.querySelector("#card-title");
    }

    set hass(hass) {
      this._hass = hass;
      this._updateContent();
    }

    setConfig(config) {
      this._config = config || {};
      if (this._titleEl && this._config.title) {
        this._titleEl.textContent = this._config.title;
      }
      this._updateContent();
    }

    getCardSize() {
      return 3;
    }

    connectedCallback() {
      this._updateContent();
    }

    /* ------------------------------------------------------------------ */
    /* Data                                                               */
    /* ------------------------------------------------------------------ */

    _getFriendlyName(state) {
      return (
        (state.attributes && state.attributes.friendly_name) ||
        state.name ||
        state.entity_id
      );
    }

    _items() {
      const states = this._hass ? this._hass.states : null;
      if (!states) return [];
      const list = [];
      for (const entityId of Object.keys(states)) {
        const state = states[entityId];
        if (
          state &&
          state.entity_id &&
          state.entity_id.startsWith("sensor.") &&
          state.attributes &&
          state.attributes.ai_prompt
        ) {
          list.push(state);
        }
      }
      return list.sort((a, b) => {
        const nameA = this._getFriendlyName(a);
        const nameB = this._getFriendlyName(b);
        return nameA.localeCompare(nameB, "pl");
      });
    }

    _lastDetectionEntityId() {
      return (this._config && this._config.image_entity) || "image.fridge_last_detection";
    }

    _lastDetectionState() {
      const hass = this._hass;
      if (!hass || !hass.states) return null;
      return hass.states[this._lastDetectionEntityId()] || null;
    }

    _imageUrl(entityId, state) {
      const token = state && state.attributes ? state.attributes.access_token : null;
      const cacheBuster = state ? state.last_updated || state.state : "";
      let url = "/api/image_proxy/" + encodeURIComponent(entityId);
      const params = [];
      if (token) params.push("token=" + encodeURIComponent(token));
      if (cacheBuster) params.push("v=" + encodeURIComponent(cacheBuster));
      if (params.length) url += "?" + params.join("&");
      return url;
    }

    /* ------------------------------------------------------------------ */
    /* Rendering (Tylko zmienione elementy)                               */
    /* ------------------------------------------------------------------ */

    _updateContent() {
      if (!this._hass || !this._itemsContainer) return;

      // 1. Aktualizacja listy produktów (tylko jeśli coś się zmieniło)
      const items = this._items();
      const itemsSignature = JSON.stringify(
        items.map((s) => [
          s.entity_id,
          s.state,
          this._getFriendlyName(s),
          s.attributes.item_id,
          s.attributes.location,
        ])
      );

      if (itemsSignature !== this._lastItemsSignature) {
        this._lastItemsSignature = itemsSignature;
        if (items.length === 0) {
          this._itemsContainer.innerHTML =
            '<div class="items-empty">Brak produktów w inwentarzu.</div>';
        } else {
          this._itemsContainer.innerHTML = items
            .map((state) => {
              const name = this._getFriendlyName(state);
              const itemId = state.attributes.item_id || "";
              const location = state.attributes.location || "fridge";
              const qty = Math.max(0, Number(state.state) || 0);
              return `
                <div class="item" data-id="${escapeHtml(itemId)}">
                  <span class="item-name">${escapeHtml(name)}</span>
                  <span class="item-meta">${escapeHtml(location)}</span>
                  <button type="button" class="qty-button" data-action="dec" data-id="${escapeHtml(itemId)}" ${qty <= 0 ? "disabled" : ""} aria-label="Zmniejsz ${escapeHtml(name)}">−</button>
                  <span class="item-qty">${qty}</span>
                  <button type="button" class="qty-button" data-action="inc" data-id="${escapeHtml(itemId)}" aria-label="Zwiększ ${escapeHtml(name)}">+</button>
                </div>
              `;
            })
            .join("");
        }
      }

      // 2. Aktualizacja obrazu detekcji (tylko jeśli coś się zmieniło)
      const lastDetection = this._lastDetectionState();
      const imageSignature = lastDetection
        ? `${lastDetection.state}_${lastDetection.last_updated}_${lastDetection.attributes && lastDetection.attributes.access_token}`
        : "none";

      if (imageSignature !== this._lastImageSignature) {
        this._lastImageSignature = imageSignature;
        if (lastDetection && lastDetection.state !== "unavailable" && lastDetection.state !== "unknown") {
          const imageUrl = this._imageUrl(this._lastDetectionEntityId(), lastDetection);
          this._detectionContainer.innerHTML = `<img class="detection-image" src="${escapeHtml(imageUrl)}" alt="Ostatnia detekcja">`;
        } else {
          this._detectionContainer.innerHTML = `<p class="hint">Brak nagranych detekcji.</p>`;
        }
      }
    }

    /* ------------------------------------------------------------------ */
    /* Dialog Management                                                  */
    /* ------------------------------------------------------------------ */

    _openDialog() {
      this._dialogOpen = true;
      this._setError("");
      // Reset pól formularza przy otwieraniu
      const root = this.shadowRoot;
      root.querySelector("#fi-name").value = "";
      root.querySelector("#fi-ai_prompt").value = "";
      root.querySelector("#fi-quantity").value = "1";
      root.querySelector("#fi-location").value = "fridge";

      this._dialogBackdrop.classList.add("open");
      setTimeout(() => {
        const nameInput = root.querySelector("#fi-name");
        if (nameInput) nameInput.focus();
      }, 50);
    }

    _closeDialog() {
      if (this._saving) return; // Nie zamykaj w trakcie zapisywania
      this._dialogOpen = false;
      this._dialogBackdrop.classList.remove("open");
      this._setError("");
    }

    _setSavingState(isSaving) {
      this._saving = isSaving;
      this._saveBtn.disabled = isSaving;
      this._cancelBtn.disabled = isSaving;
      this._loadingEl.style.display = isSaving ? "inline" : "none";
    }

    _setError(msg) {
      if (!this._errorEl) return;
      this._errorEl.textContent = msg || "";
      this._errorEl.classList.toggle("visible", Boolean(msg));
    }

    /* ------------------------------------------------------------------ */
    /* Events                                                             */
    /* ------------------------------------------------------------------ */

    _onClick(event) {
      const actionEl = event.target.closest("[data-action]");
      if (!actionEl) return;

      const action = actionEl.getAttribute("data-action");
      const id = actionEl.getAttribute("data-id");

      // Kliknięcie w ciemne tło zamyka modal, ale kliknięcie wewnątrz okna już nie
      if (action === "backdrop-close") {
        if (event.target === this._dialogBackdrop) {
          this._closeDialog();
        }
        return;
      }

      switch (action) {
        case "inc":
          this._changeQuantity(id, 1);
          break;
        case "dec":
          this._changeQuantity(id, -1);
          break;
        case "open-add":
          this._openDialog();
          break;
        case "close-dialog":
          this._closeDialog();
          break;
        case "save-add":
          this._saveAddItem();
          break;
        default:
          break;
      }
    }

    /* ------------------------------------------------------------------ */
    /* Actions                                                            */
    /* ------------------------------------------------------------------ */

    async _changeQuantity(itemId, delta) {
      const hass = this._hass;
      if (!hass || !itemId) return;
      try {
        await hass.callService("fridge_vision_inventory", "update_quantity", {
          item_id: itemId,
          delta: delta,
        });
      } catch (err) {
        console.error("Failed to update quantity", err);
      }
    }

    _readDraft() {
      const root = this.shadowRoot;
      const quantityInput = root.querySelector("#fi-quantity");
      return {
        name: (root.querySelector("#fi-name").value || "").trim(),
        ai_prompt: (root.querySelector("#fi-ai_prompt").value || "").trim(),
        quantity: Math.max(0, Number(quantityInput ? quantityInput.value : 0) || 0),
        location: (root.querySelector("#fi-location").value || "").trim() || "fridge",
      };
    }

    async _saveAddItem() {
      const hass = this._hass;
      if (!hass || this._saving) return;

      const draft = this._readDraft();
      if (!draft.name || !draft.ai_prompt) {
        this._setError("Wypełnij nazwę produktu oraz prompt AI.");
        return;
      }

      this._setError("");
      this._setSavingState(true);

      try {
        await hass.callService("fridge_vision_inventory", "add_item", {
          name: draft.name,
          ai_prompt: draft.ai_prompt,
          quantity: draft.quantity,
          location: draft.location,
        });
        this._setSavingState(false);
        this._closeDialog();
      } catch (err) {
        console.error("Failed to add item", err);
        this._setSavingState(false);
        this._setError("Błąd podczas dodawania produktu: " + (err.message || err));
      }
    }
  }

  if (!customElements.get("fridge-inventory-card")) {
    customElements.define("fridge-inventory-card", FridgeInventoryCard);
  }
})();