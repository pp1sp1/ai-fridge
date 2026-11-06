/**
 * fridge-inventory-card.js
 *
 * Bundled Lovelace card for Fridge Vision Inventory.
 * Fork of simple-inventory-card: all barcode / scanner code (Quagga, ZXing,
 * camera stream) has been removed.
 *
 * Features:
 *   - Product list with Polish names, quantity and +/- counter buttons that
 *     call fridge_vision_inventory.update_quantity.
 *   - Add item dialog (Nazwa produktu (PL), Prompt AI (EN), Ilość) calling
 *     fridge_vision_inventory.add_item with a loading state while the model
 *     re-parametrizes.
 *   - Collapsible last-detection view showing image.fridge_last_detection.
 *
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
      padding: 16px;
      box-sizing: border-box;
      font-family: var(--primary-font-family, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif);
      color: var(--primary-text-color, #212121);
    }
    .card {
      background: var(--card-background-color, var(--ha-card-background, #ffffff));
      border-radius: 8px;
      padding: 0;
      box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.1));
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
    }
    .item-meta {
      flex: 0 0 150px;
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
      opacity: 0.45;
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
      padding-top: 16px;
    }
    .detection-panel summary {
      font-weight: 600;
      font-size: 1rem;
      cursor: pointer;
      padding: 8px 0;
      list-style: none;
    }
    .detection-panel summary::-webkit-details-marker {
      display: none;
    }
    .detection-panel summary::after {
      content: "▾";
      float: right;
      font-size: 0.8rem;
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
    }
    .hint {
      color: var(--secondary-text-color, #757575);
      font-size: 0.9rem;
    }
    /* Dialog */
    .dialog-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.45);
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
      background: var(--card-background-color, #ffffff);
      border-radius: 10px;
      padding: 24px;
      width: 100%;
      max-width: 420px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
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
      padding: 8px 10px;
      border: 1px solid var(--divider-color, #e0e0e0);
      border-radius: 6px;
      background: var(--secondary-background-color, #fafafa);
      color: var(--primary-text-color, #212121);
      font-size: 0.95rem;
    }
    .dialog-actions {
      display: flex;
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
    .button-primary:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .loading {
      margin-left: 10px;
      font-size: 0.9rem;
      color: var(--secondary-text-color, #757575);
    }
  `;

  customElements.define(
    "fridge-inventory-card",
    class FridgeInventoryCard extends HTMLElement {
      constructor() {
        super();
        const root = this.attachShadow({ mode: "open" });
        const style = document.createElement("style");
        style.textContent = CSS;
        root.appendChild(style);
        this._container = document.createElement("div");
        root.appendChild(this._container);

        this._hass = null;
        this._dialogOpen = false;
        this._saving = false;

        root.addEventListener("click", this._onClick.bind(this));
      }

      /* HA sets `hass` on custom card elements. */
      set hass(hass) {
        this._hass = hass;
        this._render();
      }

      /* HA calls setConfig(config) to pass card configuration. */
      setConfig(config) {
        this._config = config || {};
        this._render();
      }

      getCardSize() {
        return 3;
      }

      connectedCallback() {
        this._render();
      }

      /* ------------------------------------------------------------------ */
      /* Data                                                               */
      /* ------------------------------------------------------------------ */

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
        return list.sort(function (a, b) {
          const nameA = a.name || "";
          const nameB = b.name || "";
          return nameA.localeCompare(nameB);
        });
      }

      _lastDetectionEntityId() {
        return "image.fridge_last_detection";
      }

      _lastDetectionState() {
        const hass = this._hass;
        if (!hass || !hass.states) return null;
        return hass.states[this._lastDetectionEntityId()] || null;
      }

      _imageUrl(entityId) {
        const state = this._lastDetectionState();
        const token = state && state.attributes ? state.attributes.access_token : null;
        let url = "/api/image_proxy/" + encodeURIComponent(entityId);
        if (token) {
          url += "?token=" + encodeURIComponent(token);
        }
        return url;
      }

      /* ------------------------------------------------------------------ */
      /* Rendering                                                          */
      /* ------------------------------------------------------------------ */

      _render() {
        const items = this._items();
        const lastDetection = this._lastDetectionState();
        const imageUrl = lastDetection ? this._imageUrl(this._lastDetectionEntityId()) : null;

        const itemRows = items
          .map(
            (state) => `
              <div class="item" data-id="${escapeHtml(state.attributes.item_id || "")}">
                <span class="item-name">${escapeHtml(state.name)}</span>
                <span class="item-meta">${escapeHtml(state.attributes.location || "fridge")}</span>
                <button type="button" class="qty-button" data-action="dec" data-id="${escapeHtml(state.attributes.item_id || "")}" aria-label="Zmniejsz ${escapeHtml(state.name)}">−</button>
                <span class="item-qty">${Number(state.state) || 0}</span>
                <button type="button" class="qty-button" data-action="inc" data-id="${escapeHtml(state.attributes.item_id || "")}" aria-label="Zwiększ ${escapeHtml(state.name)}">+</button>
              </div>
            `
          )
          .join("");

        const detectionHtml = lastDetection
          ? `<img class="detection-image" src="${escapeHtml(imageUrl)}" alt="Ostatnia detekcja">`
          : `<p class="hint">Brak nagranych detekcji.</p>`;

        this._container.innerHTML = `
          <div class="card">
            <div class="card-content">
              <div class="card-header">Fridge Vision Inventory</div>
              <div class="items">
                ${items.length ? itemRows : '<div class="items-empty">Brak produktów w inwentarzu.</div>'}
              </div>
              <button type="button" class="button-add" data-action="open-add">+ Dodaj produkt</button>

              <div class="detection-panel">
                <summary>Podgląd ostatniej detekcji</summary>
                <div class="detection-content">
                  ${detectionHtml}
                </div>
              </div>
            </div>
          </div>

          <div class="dialog-backdrop${this._dialogOpen ? " open" : ""}" data-action="close-dialog">
            <div class="dialog-box">
              <h3>Dodaj produkt</h3>
              <div class="field">
                <label>Nazwa produktu (PL)</label>
                <input type="text" id="fi-name" placeholder="np. Mleko 3.2%">
              </div>
              <div class="field">
                <label>Prompt AI (EN)</label>
                <input type="text" id="fi-ai_prompt" placeholder="np. milk carton">
              </div>
              <div class="field">
                <label>Ilość</label>
                <input type="number" id="fi-quantity" value="1" min="0">
              </div>
              <div class="field">
                <label>Lokalizacja</label>
                <input type="text" id="fi-location" value="fridge">
              </div>
              <div class="dialog-actions">
                <button type="button" class="button-cancel" data-action="close-dialog">Anuluj</button>
                <button type="button" class="button-primary" data-action="save-add"${this._saving ? " disabled" : ""}>Zapisz</button>
                ${this._saving ? '<span class="loading">Przetwarzanie modelu AI…</span>' : ""}
              </div>
            </div>
          </div>
        `;
      }

      /* ------------------------------------------------------------------ */
      /* Events                                                             */
      /* ------------------------------------------------------------------ */

      _onClick(event) {
        const button = event.target.closest("[data-action]");
        if (!button) return;
        const action = button.getAttribute("data-action");
        const id = button.getAttribute("data-id");

        switch (action) {
          case "inc":
            this._changeQuantity(id, 1);
            break;
          case "dec":
            this._changeQuantity(id, -1);
            break;
          case "open-add":
            this._dialogOpen = true;
            this._render();
            break;
          case "close-dialog":
            this._dialogOpen = false;
            this._render();
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
        if (!hass) return;
        try {
          await hass.callService("fridge_vision_inventory", "update_quantity", {
            item_id: itemId,
            delta: delta,
          });
        } catch (err) {
          console.error("Failed to update quantity", err);
        }
        this._render();
      }

      _readDraft() {
        const root = this.shadowRoot;
        const quantityInput = root.querySelector("#fi-quantity");
        return {
          name: (root.querySelector("#fi-name") || {}).value || "",
          ai_prompt: (root.querySelector("#fi-ai_prompt") || {}).value || "",
          quantity: Math.max(0, Number(quantityInput ? quantityInput.value : 0) || 0),
          location: (root.querySelector("#fi-location") || {}).value || "fridge",
        };
      }

      async _saveAddItem() {
        const hass = this._hass;
        if (!hass) return;
        const draft = this._readDraft();
        if (!draft.name || !draft.ai_prompt) {
          return;
        }

        this._saving = true;
        this._render();
        try {
          await hass.callService("fridge_vision_inventory", "add_item", {
            name: draft.name,
            ai_prompt: draft.ai_prompt,
            quantity: draft.quantity,
            location: draft.location || "fridge",
          });
        } catch (err) {
          console.error("Failed to add item", err);
          alert("Nie udało się dodać produktu: " + err);
        } finally {
          this._saving = false;
          this._dialogOpen = false;
          this._render();
        }
      }
    }
  );
})();
