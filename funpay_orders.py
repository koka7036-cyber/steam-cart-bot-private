"""Small, dependency-free order ledger helpers for the FunPay watcher.

The ledger deliberately stores no Steam trade URL, passwords, or session data.
It records only the minimum data required to match payment notifications and
keep one reservation per order.
"""
from __future__ import annotations

import hashlib
import html
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse


OPEN_STATUSES = {"awaiting_input", "input_received", "reserved", "review", "retry_required"}



STEAM_TRADE_URL_RE = re.compile(
    r"https?://steamcommunity\.com/tradeoffer/new/\?[^\s<>\"']+",
    re.IGNORECASE,
)
_ALLOWED_TEXT_EXTENSIONS = {".txt", ".text", ".csv", ".log"}
_MAX_TEXT_ATTACHMENT_BYTES = 64 * 1024


def extract_steam_trade_url(text: str) -> str | None:
    """Extract exactly one valid Steam Trade URL; reject off-host or ambiguous input."""
    candidates = STEAM_TRADE_URL_RE.findall(html.unescape(text or ""))
    normalized = []
    for candidate in candidates:
        candidate = candidate.rstrip(".,;:!?)]}\"'")
        parsed = urlparse(candidate)
        query = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(parsed.query)
        if (
            parsed.scheme == "https"
            and parsed.hostname == "steamcommunity.com"
            and parsed.path.rstrip("/") == "/tradeoffer/new"
            and query.get("partner", [""])[0].isdigit()
            and query.get("token", [""])[0]
        ):
            normalized.append(candidate)
    unique = list(dict.fromkeys(normalized))
    return unique[0] if len(unique) == 1 else None


def _is_text_filename(value: str) -> bool:
    if not value:
        return False
    path = urlparse(value).path
    return any(path.lower().endswith(ext) for ext in _ALLOWED_TEXT_EXTENSIONS)


def _is_trusted_funpay_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "funpay.com" or host.endswith(".funpay.com"))


class _AttachmentLinkParser(HTMLParser):
    """Collect only download links that look like in-chat file attachments."""

    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._stack = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = {key.lower(): (value or "") for key, value in attrs}
        class_tokens = (attrs.get("class", "") + " " + attrs.get("data-type", "")).lower()
        label = " ".join(attrs.get(key, "") for key in ("title", "aria-label", "data-name", "data-filename", "download"))
        href = attrs.get("href", "")
        inherited_context = any(item[1] for item in self._stack)
        own_context = bool(re.search(r"(?:attachment|chat-msg-file|file-upload|file-attachment)", class_tokens))
        filename_hint = _is_text_filename(label) or _is_text_filename(href)
        is_file_link = tag.lower() == "a" and bool(href) and (
            inherited_context or own_context or filename_hint or "download" in attrs
        )
        if is_file_link:
            self.links.append((href, label or href))
        if tag.lower() not in self._VOID_TAGS:
            self._stack.append((tag.lower(), inherited_context or own_context))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                return


def extract_text_attachment_href(html_fragment: str, base_url: str) -> str | None:
    """Return one trusted FunPay-hosted text attachment URL from message HTML.

    A single matching attachment is required. External hosts, unrelated links,
    and ambiguous multiple attachments are rejected.
    """
    parser = _AttachmentLinkParser()
    try:
        parser.feed(html_fragment or "")
        parser.close()
    except Exception:
        return None
    candidates = []
    for href, _label in parser.links:
        resolved = urljoin(base_url, html.unescape(href.strip()))
        if _is_trusted_funpay_url(resolved):
            candidates.append(resolved)
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


def decode_text_attachment(data: bytes, max_bytes: int = _MAX_TEXT_ATTACHMENT_BYTES) -> str:
    """Decode a small plain-text attachment; reject oversized or binary files."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("Вложение не является двоичными данными.")
    if not data:
        raise ValueError("Текстовый файл пуст.")
    if len(data) > max_bytes:
        raise ValueError(f"Текстовый файл превышает лимит {max_bytes} байт.")
    payload = bytes(data)
    encodings = ("utf-8-sig", "utf-16", "cp1251")
    last_error = None
    for encoding in encodings:
        try:
            text = payload.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        if "\x00" in text:
            continue
        bad_controls = sum(1 for char in text if ord(char) < 32 and char not in "\r\n\t")
        if bad_controls:
            continue
        if text.strip():
            return text
    raise ValueError("Не удалось прочитать файл как обычный текст UTF-8/UTF-16/Windows-1251.") from last_error


def extract_order_number(text: str) -> str | None:
    """Extract the visible FunPay order number when it is present in a message."""
    match = re.search(r"(?:заказ|order)\s*[№#]?\s*(\d{4,})", text or "", re.IGNORECASE)
    return match.group(1) if match else None


def payment_key(node_id: str, text: str) -> str:
    """Return a stable fallback key for payment messages without an order number."""
    order_no = extract_order_number(text)
    if order_no:
        return f"fp-order-{order_no}"
    normalized = " ".join((text or "").lower().split())
    digest = hashlib.sha256(f"{node_id}|{normalized}".encode("utf-8")).hexdigest()[:16]
    return f"fp-chat-{node_id}-{digest}"



def message_signature(sender_name: str, preview: str) -> str:
    """Create a stable signature without storing the message preview itself."""
    source = f"{sender_name or ''}:{preview or ''}".encode("utf-8", errors="replace")
    return hashlib.sha256(source).hexdigest()

def register_payment(orders: dict, *, node_id: str, buyer: str, text: str,
                     sets_per_item: int, quantity: int) -> tuple[dict, bool]:
    """Create exactly one record for a payment notification.

    Returns ``(record, created)``. A repeated FunPay notification reuses the
    existing record and can never create a second inventory reservation.
    """
    key = payment_key(node_id, text)
    existing = orders.get(key)
    if isinstance(existing, dict):
        return existing, False

    now = int(time.time())
    record = {
        "key": key,
        "funpay_order_number": extract_order_number(text),
        "node_id": str(node_id),
        "buyer": buyer,
        "sets_per_item": int(sets_per_item),
        "quantity": int(quantity),
        "sets_total": int(sets_per_item) * int(quantity),
        "status": "awaiting_input",
        "reservation": None,
        "input_kind": None,
        "input_received_at": None,
        "updated_at": now,
        "created_at": now,
        "notes": [],
    }
    orders[key] = record
    return record, True


def find_open_order_for_chat(orders: dict, node_id: str) -> dict | None:
    """Return an order only when exactly one candidate exists in this chat.

    If multiple open orders exist, an incoming file/link cannot be reliably
    assigned to one of them from the chat id alone. Returning ``None`` forces
    manual review instead of silently attaching the input to the wrong order.
    """
    candidates = [
        item for item in orders.values()
        if isinstance(item, dict)
        and str(item.get("node_id")) == str(node_id)
        and item.get("status") in OPEN_STATUSES
    ]
    return candidates[0] if len(candidates) == 1 else None


def mark_input_received(record: dict, input_kind: str) -> None:
    """Mark that input arrived without storing its sensitive contents."""
    # Repeated events must not overwrite the first input or move an order
    # backwards from review/delivered/cancelled to input_received.
    if record.get("status") not in {"awaiting_input", "reserved"}:
        return
    record["input_kind"] = input_kind
    record["input_received_at"] = int(time.time())
    record["updated_at"] = int(time.time())
    record["status"] = "input_received"
