/**
 * Fridge Inventory Card
 * Bundled Lovelace card for the Fridge Vision Inventory integration.
 *
 * Scanner-free fork concept of simple-inventory-card: the product list is
 * driven by the YOLO-World motion analysis of the Fridge Vision AI add-on.
 * Features: product list with +/- counters, add-item dialog (Polish UI),
 * collapsible "last detection" panel with the annotated frame.
 *
 * Card is auto-injected by the integration (no Lovelace resource setup).
 */
const CARD_VERSION = "1.0.0";
const SERVICE_DOMAIN = "fridge_vision_inventory";
const WS_ITEMS_TYPE = "fridge_vision_inventory/items";

const esc = (value) =>
  String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));

class FridgeInventoryCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._items = [];
    this._busy = false;
    this._initialized = false;
    this._refreshTimer = null;
    this._lastError = null;
  }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("Invalid configuration for fridge-inventory-card");
    }
    this._config = {
      title:
        typeof config.title === "string" && config.title.length
          ? config.title
          : "Zapasy w lodówce",
      image_entity:
        typeof config.image_entity === "string" && config.image_entity
          ? config.image_entity
          : "image.fridge_last_detection",
    };
    if (this._initialized) {
      this._initialized = false;
      this.shadowRoot.innerHTML = "";
    }
    if (this._hass) this._build();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized && this._config) {
      this._build();
    } else {
      this._updateImagePanel();
    }
  }

  connectedCallback() {
    this._refreshTimer = setInterval(() => {
      if (this._hass && this._initialized) this._refreshItems();
    }, 60000);
  }

  disconnectedCallback() {
    if (this._refreshTimer) {
      clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  getCardSize() {
    return 5;
  }

  static getStubConfig() {
    return {
      title: "Zapasy w lodówce",
      image_entity: "image.fridge_last_detection",
    };
  }

  // ---------- DOM ----------

  _build() {
    const style = `
      :host { --fvi-danger: var(--error-color, #db4437); }
      ha-card {
        padding: 14px 16px;
        background: var(--card-background-color, var(--ha-card-background, #fff));
        border-radius: var(--ha-card-border-radius, 12px);
        color: var(--primary-text-color, #212121);
      }
      .header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 10px; }
      .title { font-size: 1.25rem; font-weight: 600; }
      .total { font-size: 0.9rem; color: var(--secondary-text-color, #727272); }
      .items { list-style: none; margin: 0; padding: 0; }
      .items li {
        display: flex; align-items: center; gap: 10px;
        padding: 8px 4px; border-bottom: 1px solid var(--divider-color, #e0e0e0);
      }
      .items li:last-child { border-bottom: none; }
      .items li.empty-stock .name { opacity: 0.45; }
      .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .qty {
        min-width: 2.2em; text-align: center; font-weight: 600;
        font-variant-numeric: tabular-nums;
      }
      .btn {
        border: none; border-radius: 50%; width: 32px; height: 32px;
        font-size: 1.1rem; line-height: 1; cursor: pointer; font-weight: 700;
        background: var(--secondary-background-color, #f2f4f7);
        color: var(--primary-text-color, #212121);
        display: inline-flex; align-items: center; justify-content: center;
      }
      .btn:disabled { opacity: 0.35; cursor: default; }
      .btn.primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); border-radius: 16px; width: auto; height: auto; padding: 8px 14px; font-size: 0.9rem; font-weight: 600; }
      .btn.secondary { background: var(--secondary-background-color, #f2f4f7); border-radius: 16px; width: auto; height: auto; padding: 8px 14px; font-size: 0.9rem; }
      .add-row { margin-top: 12px; display: flex; justify-content: center; }
      .error {
        background: var(--fvi-danger); color: #fff; border-radius: 8px;
        padding: 8px 10px; margin-bottom: 8px; font-size: 0.85rem;
      }
      .empty { color: var(--secondary-text-color, #727272); text-align: center; padding: 12px 0; }
      details.detection { margin-top: 14px; border-top: 1px solid var(--divider-color, #e0e0e0); padding-top: 10px; }
      details.detection summary { cursor: pointer; font-weight: 600; color: var(--primary-color, #03a9f4); }
      .det-body { margin-top: 10px; text-align: center; }
      .det-body img { max-width: 100%; border-radius: 8px; display: block; margin: 0 auto; }
      .muted { color: var(--secondary-text-color, #727272); font-size: 0.85rem; }
      dialog.dlg {
        border: none; border-radius: 12px; padding: 0;
        background: var(--card-background-color, #fff);
        color: var(--primary-text-color, #212121);
        max-width: 92vw; width: 360px;
      }
      dialog.dlg::backdrop { background: rgba(0, 0, 0, 0.4); }
      .dlg form { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
      .dlg h3 { margin: 0; font-size: 1.1rem; }
      .dlg label { display: flex; flex-direction: column; gap: 4px; font-size: 0.85rem; color: var(--secondary-text-color, #727272); }
      .dlg input {
        padding: 8px 10px; border-radius: 8px; border: 1px solid var(--divider-color, #e0e0e0);
        font-size: 1rem; background: var(--secondary-background-color, #f2f4f7);
        color: var(--primary-text-color, #212121);
      }
      .dlg-error { color: var(--fvi-danger); font-size: 0.82rem; min-height: 1em; }
      .dlg-actions { display: flex; justify-content: flex-end; gap: 8px; }
    `;
    this.shadowRoot.innerHTML = `
      <style>${style}</style>
      <ha-card>
        <div class="header">
          <span class="title">${esc(this._config.title)}</span>
          <span class="total" id="total"></span>
        </div>
        <div id="error" class="error" hidden></div>
        <ul class="items" id="list"></ul>
        <div class="empty" id="empty" hidden>Brak produktów — dodaj pierwszy produkt.</div>
        <div class="add-row">
          <button class="btn primary" id="addBtn" type="button">+ Dodaj produkt</button>
        </div>
        <details class="detection" id="detection">
          <summary>Ostatnie wykrycie</summary>
          <div class="det-body">
            <img id="detImg" alt="Ostatnie wykrycie" hidden />
            <div class="muted" id="detMsg">Brak wykryć — otwórz lodówkę, aby wykonać pierwszą analizę.</div>
          </div>
        </details>
      </ha-card>
      <dialog class="dlg" id="addDialog">
        <form id="addForm">
          <h3>Nowy produkt</h3>
          <label>Nazwa produktu (PL)
            <input id="fName" type="text" required placeholder="Mleko 3.2%" />
          </label>
          <label>Prompt AI (EN)
            <input id="fPrompt" type="text" required placeholder="milk carton" />
          </label>
          <label>Ilość
            <input id="fQty" type="number" min="0" step="1" value="1" />
          </label>
          <div class="dlg-error" id="dlgError"></div>
          <div class="dlg-actions">
            <button class="btn secondary" id="cancelBtn" type="button">Anuluj</button>
            <button class="btn primary" id="saveBtn" type="submit">Zapisz</button>
          </div>
        </form>
      </dialog>
    `;

    this.shadowRoot.getElementById("addBtn").addEventListener("click", () => this._openDialog());
    this.shadowRoot.getElementById("cancelBtn").addEventListener("click", () => this._closeDialog());
    this.shadowRoot.getElementById("addForm").addEventListener("submit", (ev) => this._submitAdd(ev));

    this._initialized = true;
    this._updateImagePanel();
    this._refreshItems();
  }

  // ---------- Data ----------

  async _refreshItems() {
    if (!this._hass) return;
    let items = [];
    try {
      const result = await this._hass.callWS({ type: WS_ITEMS_TYPE });
      items = Array.isArray(result && result.items) ? result.items : [];
      this._lastError = null;
    } catch (err) {
      this._lastError = "Integracja Fridge Vision Inventory nie odpowiada.";
    }
    this._items = items;
    this._renderItems();
  }

  _renderItems() {
    const list = this.shadowRoot.getElementById("list");
    const empty = this.shadowRoot.getElementById("empty");
    const totalEl = this.shadowRoot.getElementById("total");
    const errorEl = this.shadowRoot.getElementById("error");
    if (!list) return;

    if (this._lastError) {
      errorEl.textContent = this._lastError;
      errorEl.hidden = false;
    } else {
      errorEl.hidden = true;
    }

    list.innerHTML = "";
    if (!this._items.length) {
      empty.hidden = false;
      totalEl.textContent = "";
      return;
    }
    empty.hidden = true;
    const total = this._items.reduce((sum, item) => sum + (Number(item.quantity) || 0), 0);
    totalEl.textContent = `Razem: ${total}`;

    for (const item of this._items) {
      const li = document.createElement("li");
      if (!item.quantity) li.classList.add("empty-stock");
      li.innerHTML = `
        <span class="name" title="${esc(item.name)}">${esc(item.name)}</span>
        <span class="qty">${esc(item.quantity)}</span>
      `;
      const minus = document.createElement("button");
      minus.className = "btn";
      minus.type = "button";
      minus.textContent = "−";
      minus.disabled = this._busy || !item.quantity;
      minus.setAttribute("aria-label", `Zmniejsz ilość: ${item.name}`);
      minus.addEventListener("click", () => this._onDelta(item, -1));
      const plus = document.createElement("button");
      plus.className = "btn";
      plus.type = "button";
      plus.textContent = "+";
      plus.disabled = this._busy;
      plus.setAttribute("aria-label", `Zwiększ ilość: ${item.name}`);
      plus.addEventListener("click", () => this._onDelta(item, 1));
      li.appendChild(minus);
      li.appendChild(plus);
      list.appendChild(li);
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
    }
    await this._refreshItems();
    this._busy = false;
    this._renderItems();
  }

  // ---------- Add item dialog ----------

  _openDialog() {
    const dialog = this.shadowRoot.getElementById("addDialog");
    this.shadowRoot.getElementById("fName").value = "";
    this.shadowRoot.getElementById("fPrompt").value = "";
    this.shadowRoot.getElementById("fQty").value = "1";
    this.shadowRoot.getElementById("dlgError").textContent = "";
    dialog.showModal();
  }

  _closeDialog() {
    this.shadowRoot.getElementById("addDialog").close();
  }

  async _submitAdd(event) {
    event.preventDefault();
    const nameInput = this.shadowRoot.getElementById("fName");
    const promptInput = this.shadowRoot.getElementById("fPrompt");
    const qtyInput = this.shadowRoot.getElementById("fQty");
    const dlgError = this.shadowRoot.getElementById("dlgError");
    const saveBtn = this.shadowRoot.getElementById("saveBtn");

    const name = nameInput.value.trim();
    const aiPrompt = promptInput.value.trim();
    const quantity = Math.max(0, parseInt(qtyInput.value, 10) || 0);

    if (!name) {
      dlgError.textContent = "Podaj nazwę produktu (PL).";
      return;
    }
    if (!aiPrompt) {
      dlgError.textContent = "Podaj prompt AI (EN), np. \"milk carton\".";
      return;
    }

    saveBtn.disabled = true;
    const originalLabel = saveBtn.textContent;
    saveBtn.textContent = "Zapisuję… re-parametryzacja AI";

    try {
      await this._hass.callService(SERVICE_DOMAIN, "add_item", {
        name,
        ai_prompt: aiPrompt,
        quantity,
      });
      this._lastError = null;
      this._closeDialog();
      await this._refreshItems();
    } catch (err) {
      dlgError.textContent = "Nie udało się dodać produktu. Spróbuj ponownie.";
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = originalLabel;
    }
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
    description: "Inwentarz lodówki sterowany analizą ruchu Fridge Vision AI.",
  });
}

if (!customElements.get("fridge-inventory-card")) {
  customElements.define("fridge-inventory-card", FridgeInventoryCard);
}

console.info(
  `%c FRIDGE-INVENTORY-CARD %c ${CARD_VERSION} `,
  "background: #2196f3; color: #fff; border-radius: 3px 0 0 3px; padding: 2px 4px;",
  "background: #e3f2fd; color: #0d47a1; border-radius: 0 3px 3px 0; padding: 2px 4px;"
);
