"""Safe, dependency-free Playerok deal ledger.

Only non-secret metadata is persisted. Trade URLs, message bodies, session tokens,
cookies, and buyer credentials must never be written to this ledger.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from threading import RLock
from typing import Any


ACTIVE_INPUT_STATUSES = {"awaiting_trade_url", "manual_review", "needs_review"}
FINAL_PLATFORM_STATUSES = {"confirmed", "rolled_back"}
CLOSED_FOR_INPUT_STATUSES = {"confirmed", "rolled_back", "problem", "problem_resolved"}
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_SECRET_RE = re.compile(r"\b(?:token|access_token|session|cookie|jwt)\s*[=:]\s*[^\s&<>]+", re.IGNORECASE)
_LOCK = RLock()


def _safe_text(value: Any, limit: int = 180) -> str | None:
    if value is None:
        return None
    text = str(value)
    # Item titles and status text are untrusted. Never persist URLs or token-like
    # fragments that may have been pasted into a marketplace listing name.
    text = _URL_RE.sub("[URL скрыта]", text)
    text = _SECRET_RE.sub("[секрет скрыт]", text)
    text = " ".join(text.split()).strip()
    if not text:
        return None
    return text[:limit]


def _safe_id(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    # Identifiers should never contain URLs, cookie strings or credential fields.
    if _URL_RE.search(raw) or _SECRET_RE.search(raw):
        return None
    text = _safe_text(raw, 128)
    return text if text and text.lower() not in {"none", "null"} else None


def normalize_platform_status(status: Any) -> str:
    """Normalize SDK enums/event names without depending on SDK imports."""
    if status is None:
        return "unknown"
    raw = getattr(status, "name", status)
    value = str(raw).strip().upper().split(".")[-1]
    value = value.replace(" ", "_")
    if value in {"PAID", "ITEM_PAID", "NEW_DEAL"}:
        return "paid"
    if value in {"PENDING", "AWAITING_SEND"}:
        return "pending"
    if value in {"SENT", "ITEM_SENT"}:
        return "sent"
    if value in {"CONFIRMED", "CONFIRMED_AUTOMATICALLY", "DEAL_CONFIRMED", "DEAL_CONFIRMED_AUTOMATICALLY"}:
        return "confirmed"
    if value in {"ROLLED_BACK", "DEAL_ROLLED_BACK"}:
        return "rolled_back"
    if value in {"HAS_PROBLEM", "DEAL_HAS_PROBLEM", "PROBLEM"}:
        return "problem"
    if value in {"PROBLEM_RESOLVED", "DEAL_PROBLEM_RESOLVED"}:
        return "problem_resolved"
    return "unknown"


def map_item_to_sets(item_id: Any, one_set_item_id: Any, five_set_item_id: Any) -> int | None:
    """Map a listing only by exact, unique IDs; never infer from a title."""
    listing_id = _safe_id(item_id)
    one_id = _safe_id(one_set_item_id)
    five_id = _safe_id(five_set_item_id)
    if not listing_id or (one_id and five_id and one_id == five_id):
        return None
    if one_id and listing_id == one_id:
        return 1
    if five_id and listing_id == five_id:
        return 5
    return None


def _initial_fulfillment(status: str, sets: int | None) -> str:
    # Platform terminal/problem states remain visible even if the listing ID
    # was not mapped; unknown quantity still requires review, but not re-delivery.
    if status == "confirmed":
        return "confirmed"
    if status == "rolled_back":
        return "rolled_back"
    if status == "problem":
        return "problem"
    if sets not in {1, 5}:
        return "needs_review"
    if status in {"paid", "pending"}:
        return "awaiting_trade_url"
    if status == "sent":
        return "sent"
    return "awaiting_payment"


def upsert_deal(
    orders: dict,
    *,
    deal_id: Any,
    status: Any,
    item_id: Any = None,
    item_name: Any = None,
    chat_id: Any = None,
    sets: int | None = None,
    event_type: str = "",
) -> tuple[dict, bool, bool]:
    """Upsert one marketplace deal and report (record, created, changed).

    Repeated events are idempotent. A stale PAID event cannot regress SENT or
    CONFIRMED. Conflicting listing IDs on the same deal force manual review.
    """
    safe_deal_id = _safe_id(deal_id)
    if not safe_deal_id:
        raise ValueError("Playerok deal ID is required")

    normalized_status = normalize_platform_status(status or event_type)
    safe_item_id = _safe_id(item_id)
    safe_item_name = _safe_text(item_name)
    safe_chat_id = _safe_id(chat_id)
    valid_sets = int(sets) if sets in (1, 5) else None
    now = int(time.time())
    existing = orders.get(safe_deal_id)

    if not isinstance(existing, dict):
        record = {
            "deal_id": safe_deal_id,
            "platform_status": normalized_status,
            "fulfillment_status": _initial_fulfillment(normalized_status, valid_sets),
            "item_id": safe_item_id,
            "item_name": safe_item_name,
            "chat_id": safe_chat_id,
            "sets": valid_sets,
            "trade_url_received": False,
            "created_at": now,
            "updated_at": now,
        }
        orders[safe_deal_id] = record
        return record, True, True

    record = existing
    changed = False

    previous_item_id = _safe_id(record.get("item_id"))
    if previous_item_id and safe_item_id and previous_item_id != safe_item_id:
        if record.get("fulfillment_status") != "needs_review":
            record["fulfillment_status"] = "needs_review"
            changed = True
        if record.get("mapping_conflict") is not True:
            record["mapping_conflict"] = True
            changed = True
    elif not previous_item_id and safe_item_id:
        record["item_id"] = safe_item_id
        changed = True

    if safe_item_name and not record.get("item_name"):
        record["item_name"] = safe_item_name
        changed = True
    if safe_chat_id and not record.get("chat_id"):
        record["chat_id"] = safe_chat_id
        changed = True

    previous_sets = record.get("sets")
    if previous_sets not in {1, 5} and valid_sets in {1, 5} and not record.get("mapping_conflict"):
        record["sets"] = valid_sets
        changed = True
    elif previous_sets in {1, 5} and valid_sets in {1, 5} and previous_sets != valid_sets:
        record["fulfillment_status"] = "needs_review"
        record["mapping_conflict"] = True
        changed = True

    current_status = normalize_platform_status(record.get("platform_status"))
    terminal = current_status in FINAL_PLATFORM_STATUSES
    if normalized_status != "unknown" and normalized_status != current_status:
        # Do not let late/replayed events move a completed deal backward.
        should_update = (
            not terminal
            and (
                normalized_status in {"confirmed", "rolled_back", "problem", "problem_resolved"}
                or (current_status == "pending" and normalized_status == "paid")
                or (current_status in {"unknown", "pending", "paid"} and normalized_status in {"sent", "confirmed"})
                or (current_status == "paid" and normalized_status == "pending")
            )
        )
        if should_update:
            record["platform_status"] = normalized_status
            if normalized_status in {"confirmed", "rolled_back", "problem"}:
                record["fulfillment_status"] = normalized_status
            elif normalized_status == "problem_resolved":
                record["fulfillment_status"] = "manual_review"
            elif normalized_status == "sent":
                record["fulfillment_status"] = "sent"
            changed = True

    if record.get("sets") not in {1, 5} and record.get("fulfillment_status") not in {"confirmed", "rolled_back", "problem"}:
        if record.get("fulfillment_status") != "needs_review":
            record["fulfillment_status"] = "needs_review"
            changed = True
    elif record.get("fulfillment_status") == "needs_review" and not record.get("mapping_conflict") and record.get("sets") in {1, 5}:
        # The mapping is now resolved; continue with manual review if the link
        # has already arrived, otherwise wait for the required buyer input.
        new_fulfillment = "manual_review" if record.get("trade_url_received") else (
            "awaiting_trade_url" if record.get("platform_status") in {"paid", "pending"} else record.get("fulfillment_status")
        )
        if new_fulfillment != record.get("fulfillment_status"):
            record["fulfillment_status"] = new_fulfillment
            changed = True

    if changed:
        record["updated_at"] = now
    return record, False, changed


def active_orders_for_chat(orders: dict, chat_id: Any) -> list[dict]:
    safe_chat_id = _safe_id(chat_id)
    if not safe_chat_id:
        return []
    return [
        record for record in orders.values()
        if isinstance(record, dict)
        and _safe_id(record.get("chat_id")) == safe_chat_id
        and record.get("fulfillment_status") in ACTIVE_INPUT_STATUSES
        and normalize_platform_status(record.get("platform_status")) not in CLOSED_FOR_INPUT_STATUSES
    ]


def mark_trade_url_received(orders: dict, *, chat_id: Any) -> tuple[dict | None, str]:
    """Record only the fact a trade link arrived, never the link itself."""
    matches = active_orders_for_chat(orders, chat_id)
    if not matches:
        return None, "unmatched"
    if len(matches) != 1:
        return None, "ambiguous"
    record = matches[0]
    now = int(time.time())
    changed = not bool(record.get("trade_url_received"))
    record["trade_url_received"] = True
    if record.get("fulfillment_status") not in {"confirmed", "rolled_back", "problem", "sent"}:
        record["fulfillment_status"] = "needs_review" if record.get("sets") not in {1, 5} else "manual_review"
    if changed:
        record["updated_at"] = now
    return record, "recorded" if changed else "duplicate"


def summarize_orders(orders: dict) -> dict[str, int]:
    records = [record for record in orders.values() if isinstance(record, dict)]
    fulfillment = [str(record.get("fulfillment_status") or "unknown") for record in records]
    platform_status = [normalize_platform_status(record.get("platform_status")) for record in records]
    return {
        "total": len(records),
        "paid": sum(
            1 for record, status in zip(records, platform_status)
            if status in {"paid", "pending"}
            and record.get("fulfillment_status") != "needs_review"
        ),
        "sent": sum(1 for status in platform_status if status == "sent"),
        "confirmed": sum(1 for status in platform_status if status == "confirmed"),
        "rolled_back": sum(1 for status in platform_status if status == "rolled_back"),
        "problem": sum(1 for status in platform_status if status == "problem"),
        "needs_review": sum(1 for status in fulfillment if status == "needs_review"),
        "awaiting_trade_url": sum(1 for status in fulfillment if status == "awaiting_trade_url"),
        "trade_url_received": sum(1 for record in records if record.get("trade_url_received")),
    }


def _safe_timestamp(value: Any) -> int | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if numeric <= 0 or numeric != numeric or numeric == float("inf"):
        return None
    return int(numeric)


_ALLOWED_FULFILLMENT = {
    "awaiting_trade_url", "manual_review", "needs_review", "awaiting_payment",
    "sent", "confirmed", "rolled_back", "problem", "problem_resolved",
}


def _sanitize_record(record: Any, fallback_id: Any = None) -> dict | None:
    if not isinstance(record, dict):
        return None
    deal_id = _safe_id(record.get("deal_id") or fallback_id)
    if not deal_id:
        return None

    platform_status = normalize_platform_status(record.get("platform_status"))
    fulfillment_status = _safe_text(record.get("fulfillment_status"), 40)
    if fulfillment_status not in _ALLOWED_FULFILLMENT:
        fulfillment_status = "unknown"

    sets = record.get("sets")
    if not (isinstance(sets, int) and not isinstance(sets, bool) and sets in (1, 5)):
        sets = None

    safe_record = {
        "deal_id": deal_id,
        "platform_status": platform_status,
        "fulfillment_status": fulfillment_status,
        "item_id": _safe_id(record.get("item_id")),
        "item_name": _safe_text(record.get("item_name")),
        "chat_id": _safe_id(record.get("chat_id")),
        "sets": sets,
        "trade_url_received": record.get("trade_url_received") is True,
        "created_at": _safe_timestamp(record.get("created_at")),
        "updated_at": _safe_timestamp(record.get("updated_at")),
    }
    if record.get("mapping_conflict") is True:
        safe_record["mapping_conflict"] = True
    return safe_record


def _sanitize_ledger(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("orders"), dict):
        raise ValueError("Invalid Playerok order ledger structure")
    safe_orders = {}
    for key, value in data["orders"].items():
        safe_record = _sanitize_record(value, key)
        if safe_record:
            safe_orders[safe_record["deal_id"]] = safe_record
    return {"orders": safe_orders}


def load_orders(path: str | os.PathLike[str] = "playerok_orders.json") -> dict:
    """Load a local ledger and discard unknown/sensitive fields from old records."""
    target = Path(path)
    if not target.exists():
        return {"orders": {}}
    with _LOCK:
        with target.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        safe_data = _sanitize_ledger(data)
        if safe_data != data:
            # Migrate existing local records once, removing any unapproved keys
            # rather than merely hiding them from the in-memory representation.
            save_orders(safe_data, target)
        return safe_data


def save_orders(data: dict, path: str | os.PathLike[str] = "playerok_orders.json") -> None:
    """Atomically persist a strict allow-list of metadata; drop URLs/tokens."""
    safe_data = _sanitize_ledger(data)
    target = Path(path)
    parent = target.parent if str(target.parent) else Path(".")
    parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(safe_data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.remove(temp_name)
