"""Inventory data model for Fridge Vision Inventory.

A simple_inventory-style data layer: items with quantity, unit, category,
location, expiry date and description, plus an append-only change history
that records every add / update / delete / increment / decrement with
before-and-after quantities.

This module is intentionally free of Home Assistant imports so the whole
model can be unit-tested standalone; the coordinator wires it to HA
storage, service calls, events and the WebSocket API.

Quantities are floats internally (decimals like 0.5 are valid, mirroring
simple_inventory); they serialize as ints when whole to keep the stored
JSON readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

# History event types (mirrors simple_inventory's event vocabulary).
EVENT_ADD = "add"
EVENT_REMOVE = "remove"
EVENT_INCREMENT = "increment"
EVENT_DECREMENT = "decrement"
EVENT_UPDATE = "update"
EVENT_DELETE = "delete"

# Who caused a change.
SOURCE_VISION = "vision"
SOURCE_MANUAL = "manual"

# The history is capped so the Store payload stays small even after
# months of door openings; the newest events are always kept.
MAX_HISTORY_EVENTS = 500

# Largest quantity accepted from any source.
MAX_QUANTITY = 9999.0

_ITEM_STRING_FIELDS = (
    "unit",
    "category",
    "location",
    "expiry_date",
    "description",
)


def clamp_quantity(value: float) -> float:
    """Clamp a quantity into ``[0, MAX_QUANTITY]``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(MAX_QUANTITY, number))


def clean_qty(value: float) -> int | float:
    """Return an int when the quantity is whole, else the float."""
    number = float(value)
    if number.is_integer():
        return int(number)
    return round(number, 3)


@dataclass(slots=True)
class InventoryItem:
    """A single inventory entry persisted in the Store."""

    id: str
    name: str
    ai_prompt: str
    quantity: float
    unit: str
    category: str
    location: str
    expiry_date: str
    description: str
    last_updated: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage, service responses and WebSocket payloads."""
        return {
            "id": self.id,
            "name": self.name,
            "ai_prompt": self.ai_prompt,
            "quantity": clean_qty(self.quantity),
            "unit": self.unit,
            "category": self.category,
            "location": self.location,
            "expiry_date": self.expiry_date,
            "description": self.description,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InventoryItem | None:
        """Deserialize one stored entry; returns None when malformed.

        Tolerant of the historic v1 layout (no unit/category/... fields,
        integer quantities) so the storage migration is automatic.
        """
        try:
            item_id = str(payload["id"]).strip()
            name = str(payload["name"]).strip()
            # The AI prompt is optional: an item without a prompt stays in
            # the inventory but is not auto-detected by the vision model.
            ai_prompt = str(payload.get("ai_prompt", "")).strip()
            if not item_id or not name:
                return None
            quantity = clamp_quantity(payload.get("quantity", 0))
            fields = {
                key: str(payload.get(key) or "").strip()
                for key in _ITEM_STRING_FIELDS
            }
            last_updated = str(
                payload.get("last_updated") or ""
            )
        except (KeyError, TypeError, ValueError):
            return None
        return cls(
            id=item_id,
            name=name,
            ai_prompt=ai_prompt,
            quantity=quantity,
            last_updated=last_updated,
            **fields,
        )


@dataclass(slots=True)
class HistoryEvent:
    """One recorded inventory change (simple_inventory-style)."""

    event_type: str
    item_name: str
    amount: float
    quantity_before: float | None
    quantity_after: float | None
    timestamp: str
    source: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage and WebSocket payloads."""
        return {
            "event_type": self.event_type,
            "item_name": self.item_name,
            "amount": clean_qty(self.amount),
            "quantity_before": (
                None
                if self.quantity_before is None
                else clean_qty(self.quantity_before)
            ),
            "quantity_after": (
                None
                if self.quantity_after is None
                else clean_qty(self.quantity_after)
            ),
            "timestamp": self.timestamp,
            "source": self.source,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HistoryEvent | None:
        """Deserialize one history entry; returns None when malformed."""
        try:
            event_type = str(payload["event_type"]).strip()
            item_name = str(payload["item_name"]).strip()
            if not event_type or not item_name:
                return None
            before = payload.get("quantity_before")
            after = payload.get("quantity_after")
            return cls(
                event_type=event_type,
                item_name=item_name,
                amount=clamp_quantity(payload.get("amount", 0)),
                quantity_before=None if before is None else float(before),
                quantity_after=None if after is None else float(after),
                timestamp=str(payload.get("timestamp") or ""),
                source=str(payload.get("source") or SOURCE_MANUAL),
                detail=str(payload.get("detail") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


class InventoryState:
    """In-memory inventory: items keyed by id plus a change history.

    Mutating methods return the created ``HistoryEvent`` (or ``None``
    when nothing changed) so the caller can fire HA events and persist.
    Timestamps are passed in by the caller (``now``) to keep this class
    deterministic and testable.
    """

    __slots__ = ("items", "history")

    def __init__(self) -> None:
        self.items: dict[str, InventoryItem] = {}
        self.history: list[HistoryEvent] = []  # oldest first

    # ---------- lookups ----------

    def find_by_prompt(self, prompt: str) -> InventoryItem | None:
        """First item whose AI prompt matches (case-insensitive)."""
        wanted = str(prompt).strip().casefold()
        if not wanted:
            return None
        for item in self.items.values():
            if item.ai_prompt.strip().casefold() == wanted:
                return item
        return None

    def find_by_name(self, name: str) -> InventoryItem | None:
        """First item whose display name matches (case-insensitive)."""
        wanted = str(name).strip().casefold()
        if not wanted:
            return None
        for item in self.items.values():
            if item.name.strip().casefold() == wanted:
                return item
        return None

    # ---------- mutations ----------

    def add_item(
        self,
        *,
        name: str,
        ai_prompt: str = "",
        quantity: float = 1.0,
        unit: str = "",
        category: str = "",
        location: str = "",
        expiry_date: str = "",
        description: str = "",
        item_id: str | None = None,
        now: str,
    ) -> tuple[InventoryItem, HistoryEvent]:
        """Create an item (quantity >= 0) and record an ``add`` event.

        Unlike simple_inventory, ``ai_prompt`` is optional: without it the
        item is stored but never auto-detected by the vision model.
        """
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("Item name must not be empty")
        clean_id = str(item_id).strip() if item_id is not None else ""
        if not clean_id:
            clean_id = uuid.uuid4().hex
        item = InventoryItem(
            id=clean_id,
            name=clean_name,
            ai_prompt=str(ai_prompt).strip(),
            quantity=clamp_quantity(quantity),
            unit=str(unit).strip(),
            category=str(category).strip(),
            location=str(location).strip(),
            expiry_date=str(expiry_date).strip(),
            description=str(description).strip(),
            last_updated=now,
        )
        self.items[item.id] = item
        event = HistoryEvent(
            event_type=EVENT_ADD,
            item_name=item.name,
            amount=clean_qty(item.quantity),
            quantity_before=None,
            quantity_after=item.quantity,
            timestamp=now,
            source=SOURCE_MANUAL,
            detail=f"prompt: {item.ai_prompt}" if item.ai_prompt else "",
        )
        self._record(event)
        return item, event

    def update_item(
        self,
        item_id: str,
        *,
        now: str,
        name: str | None = None,
        ai_prompt: str | None = None,
        quantity: float | None = None,
        unit: str | None = None,
        category: str | None = None,
        location: str | None = None,
        expiry_date: str | None = None,
        description: str | None = None,
    ) -> tuple[InventoryItem, HistoryEvent]:
        """Update any subset of fields and record an ``update`` event."""
        item = self.items.get(item_id)
        if item is None:
            raise KeyError(f"Unknown item id: {item_id}")
        changed: list[str] = []
        if name is not None:
            clean_name = str(name).strip()
            if not clean_name:
                raise ValueError("Item name must not be empty")
            if clean_name != item.name:
                item.name = clean_name
                changed.append("name")
        if ai_prompt is not None:
            clean_prompt = str(ai_prompt).strip()
            if clean_prompt != item.ai_prompt:
                item.ai_prompt = clean_prompt
                changed.append("ai_prompt")
        if quantity is not None:
            new_quantity = clamp_quantity(quantity)
            if new_quantity != item.quantity:
                item.quantity = new_quantity
                changed.append("quantity")
        for field_name, value in (
            ("unit", unit),
            ("category", category),
            ("location", location),
            ("expiry_date", expiry_date),
            ("description", description),
        ):
            if value is None:
                continue
            clean_value = str(value).strip()
            if clean_value != getattr(item, field_name):
                setattr(item, field_name, clean_value)
                changed.append(field_name)
        if not changed:
            return item, None  # type: ignore[return-value]
        item.last_updated = now
        event = HistoryEvent(
            event_type=EVENT_UPDATE,
            item_name=item.name,
            amount=0,
            quantity_before=None,
            quantity_after=item.quantity,
            timestamp=now,
            source=SOURCE_MANUAL,
            detail=f"pola: {', '.join(changed)}",
        )
        self._record(event)
        return item, event

    def delete_item(
        self, item_id: str, *, now: str
    ) -> tuple[InventoryItem, HistoryEvent]:
        """Remove an item and record a ``delete`` event."""
        item = self.items.pop(item_id, None)
        if item is None:
            raise KeyError(f"Unknown item id: {item_id}")
        event = HistoryEvent(
            event_type=EVENT_DELETE,
            item_name=item.name,
            amount=0,
            quantity_before=item.quantity,
            quantity_after=None,
            timestamp=now,
            source=SOURCE_MANUAL,
            detail="produkt usunięty z inwentarza",
        )
        self._record(event)
        return item, event

    def change_quantity(
        self,
        item_id: str,
        delta: float,
        *,
        source: str,
        now: str,
        detail: str = "",
    ) -> tuple[InventoryItem, HistoryEvent | None]:
        """Apply ``delta`` (clamped at 0) and record increment/decrement.

        Returns ``(item, None)`` when the effective amount is zero
        (nothing to record, nothing to persist).
        """
        item = self.items.get(item_id)
        if item is None:
            raise KeyError(f"Unknown item id: {item_id}")
        before = item.quantity
        after = clamp_quantity(before + float(delta))
        amount = after - before
        if amount == 0:
            item.last_updated = now
            return item, None
        item.quantity = after
        item.last_updated = now
        event = HistoryEvent(
            event_type=EVENT_INCREMENT if amount > 0 else EVENT_DECREMENT,
            item_name=item.name,
            amount=clean_qty(abs(amount)),
            quantity_before=before,
            quantity_after=after,
            timestamp=now,
            source=source,
            detail=detail,
        )
        self._record(event)
        return item, event

    def clear_history(self) -> int:
        """Drop all history events; returns how many were removed."""
        removed = len(self.history)
        self.history.clear()
        return removed

    # ---------- history ----------

    def recent_history(self, limit: int | None = None) -> list[HistoryEvent]:
        """History newest-first, optionally limited."""
        events = list(reversed(self.history))
        if limit is not None and limit >= 0:
            events = events[: max(0, int(limit))]
        return events

    def _record(self, event: HistoryEvent) -> None:
        self.history.append(event)
        if len(self.history) > MAX_HISTORY_EVENTS:
            # Keep the newest MAX_HISTORY_EVENTS events.
            del self.history[: len(self.history) - MAX_HISTORY_EVENTS]

    # ---------- persistence ----------

    def to_storage(self) -> dict[str, Any]:
        """Storage layout for Store version 2 (items + history)."""
        return {
            "items": {item_id: item.to_dict() for item_id, item in self.items.items()},
            "history": [event.to_dict() for event in self.history],
        }

    @classmethod
    def from_storage(cls, raw: Any) -> InventoryState:
        """Rebuild state from storage; tolerant of the v1 layout.

        v1 stored only ``{"items": {...}}`` with a subset of fields; the
        new fields default to empty and the history starts empty.
        Malformed entries are skipped with the counters reported back via
        the ``load_stats`` attribute (kept simple: just logged upstream).
        """
        state = cls()
        if not isinstance(raw, dict):
            return state
        raw_items = raw.get("items")
        if isinstance(raw_items, dict):
            for key, payload in raw_items.items():
                item = InventoryItem.from_dict(payload)
                if item is None:
                    continue
                state.items[item.id] = item
        raw_history = raw.get("history")
        if isinstance(raw_history, list):
            for payload in raw_history:
                event = HistoryEvent.from_dict(payload)
                if event is None:
                    continue
                state.history.append(event)
        # Enforce the cap even if an oversized history was imported.
        if len(state.history) > MAX_HISTORY_EVENTS:
            del state.history[: len(state.history) - MAX_HISTORY_EVENTS]
        return state
