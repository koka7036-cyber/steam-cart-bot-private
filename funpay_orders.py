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
from urllib.parse import parse_qs, urljoin, urlparse


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
        query = parse_qs(parsed.query)
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
        own_context = bool(re.search(r"(?:attachment|chat-msg-file|file-upload|file-attachment|chat-msg-item)", class_tokens))
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


class _LastChatMessageParser(HTMLParser):
    """Extract the most recent top-level FunPay chat message without mixing old text into a new attachment."""

    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    _MESSAGE_CLASSES = {"chat-msg", "chat-msg-item", "chat-msg-container", "chat-msg-wrapper"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._depth = 0
        self._parts = None
        self.messages = []

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text() or ""
        class_value = ""
        for key, value in attrs:
            if key.lower() == "class":
                class_value = value or ""
                break
        classes = set(class_value.split())
        is_message = tag.lower() in {"div", "li"} and bool(classes & self._MESSAGE_CLASSES)

        if self._parts is None and is_message:
            self._parts = [raw]
            self._depth = 0 if tag.lower() in self._VOID_TAGS else 1
            if self._depth == 0:
                self.messages.append(raw)
                self._parts = None
            return

        if self._parts is not None:
            self._parts.append(raw)
            if tag.lower() not in self._VOID_TAGS:
                self._depth += 1

    def handle_startendtag(self, tag, attrs):
        raw = self.get_starttag_text() or ""
        class_value = ""
        for key, value in attrs:
            if key.lower() == "class":
                class_value = value or ""
                break
        classes = set(class_value.split())
        is_message = tag.lower() in {"div", "li"} and bool(classes & self._MESSAGE_CLASSES)
        if self._parts is None and is_message:
            self.messages.append(raw)
        elif self._parts is not None:
            self._parts.append(raw)

    def handle_endtag(self, tag):
        if self._parts is None:
            return
        self._parts.append(f"</{tag}>")
        self._depth -= 1
        if self._depth <= 0:
            self.messages.append("".join(self._parts))
            self._parts = None
            self._depth = 0

    def handle_data(self, data):
        if self._parts is not None:
            self._parts.append(data)

    def handle_entityref(self, name):
        if self._parts is not None:
            self._parts.append(f"&{name};")

    def handle_charref(self, name):
        if self._parts is not None:
            self._parts.append(f"&#{name};")

    def handle_comment(self, data):
        if self._parts is not None:
            self._parts.append(f"<!--{data}-->")


def extract_last_chat_message_fragment(html_document: str) -> str | None:
    """Return only the newest chat message wrapper, so an older URL cannot trigger delivery for a newer file."""
    parser = _LastChatMessageParser()
    try:
        parser.feed(html_document or "")
        parser.close()
    except Exception:
        return None
    return parser.messages[-1] if parser.messages else None


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
    # UTF-16 must be detected by its BOM; otherwise CP1251's single-byte decoder
    # can parse UTF-16/incorrect binary bytes into plausible-looking gibberish.
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ("utf-16",)
    else:
        # Do not guess UTF-16 without a BOM; arbitrary binary bytes can decode as gibberish.
        encodings = ("utf-8-sig", "cp1251")
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


def filter_reserved_assets(cache_data: dict, orders: dict | None = None,
                          vouchers: dict | None = None) -> dict:
    """Return a stock cache with assets belonging to pending/uncertain offers removed."""
    if not isinstance(cache_data, dict):
        return {}
    orders = orders if isinstance(orders, dict) else {}
    vouchers = vouchers if isinstance(vouchers, dict) else {}

    reserved_ids = set()
    for record in orders.values():
        if not isinstance(record, dict) or record.get("status") == "cancelled":
            continue
        reservation = record.get("reservation")
        if isinstance(reservation, dict):
            reserved_ids.update(
                str(asset_id) for asset_id in reservation.get("asset_ids", [])
                if str(asset_id)
            )

    reserved_voucher_states = {"sending", "uncertain", "sent", "created_unconfirmed"}
    for voucher in vouchers.values():
        if not isinstance(voucher, dict):
            continue
        if voucher.get("delivery_state") in reserved_voucher_states or (
            voucher.get("used") and voucher.get("trade_offer_id")
        ):
            reserved_ids.update(
                str(asset_id) for asset_id in voucher.get("reserved_asset_ids", [])
                if str(asset_id)
            )

    filtered = dict(cache_data)
    filtered_games = {}
    games = cache_data.get("games", {})
    if isinstance(games, dict):
        for game_name, cards in games.items():
            if not isinstance(cards, dict):
                continue
            filtered_cards = {}
            for card_name, asset_ids in cards.items():
                if not isinstance(asset_ids, list):
                    continue
                filtered_cards[card_name] = [
                    str(asset_id) for asset_id in asset_ids
                    if str(asset_id) not in reserved_ids
                ]
            filtered_games[game_name] = filtered_cards
    filtered["games"] = filtered_games
    filtered["total_cards"] = sum(
        len(asset_ids) for cards in filtered_games.values() for asset_ids in cards.values()
    )
    return filtered


def voucher_redemption_block_reason(voucher: dict) -> str | None:
    """Reject another redemption if a prior Steam offer may already exist."""
    if not isinstance(voucher, dict):
        return "Запись кода повреждена; автоматическая выдача небезопасна."
    state = str(voucher.get("delivery_state") or "").lower()
    if voucher.get("trade_offer_id") or state in {
        "sending", "uncertain", "sent", "created_unconfirmed"
    }:
        return (
            "У кода есть созданное или потенциально созданное Steam-предложение. "
            "Повторная выдача заблокирована, чтобы не отправить карточки дважды; проверь историю Steam."
        )
    if voucher.get("reserved_asset_ids") and state != "failed":
        return (
            "У кода есть зарезервированные карточки. Сначала проверь состояние предыдущей операции в Steam."
        )
    return None


def voucher_reset_block_reason(voucher: dict) -> str | None:
    """Reject a manual code reset when a Steam offer may already exist or the code is consumed."""
    offer_block = voucher_redemption_block_reason(voucher)
    if offer_block:
        return offer_block
    if voucher.get("used"):
        return (
            "Код уже отмечен использованным. Автоматический сброс запрещён: сначала вручную "
            "убедись, что предложение не создавалось и карточки вернулись в инвентарь."
        )
    return None


def funpay_cold_start_blockers(orders: dict) -> list[tuple[str, str]]:
    """Return unresolved FunPay orders that must stay in the journal to protect reservations."""
    blockers = []
    if not isinstance(orders, dict):
        return [("unknown", "invalid_ledger")]
    for key, record in orders.items():
        if not isinstance(record, dict):
            blockers.append((str(key), "invalid_record"))
            continue
        status = str(record.get("status", "unknown"))
        if status not in {"completed", "cancelled"} or record.get("reservation"):
            blockers.append((str(key), status))
    return blockers


def inventory_cache_freshness_error(cache_data: dict, now: float | None = None,
                                    max_age_seconds: int = 6 * 60 * 60) -> str | None:
    """Fail closed when available-card counts come from a missing, future or stale cache."""
    if not isinstance(cache_data, dict):
        return "Кэш склада повреждён. Обнови кэш склада перед выдачей."
    updated_at = cache_data.get("updated_at")
    if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool) or updated_at <= 0:
        return "У кэша склада нет достоверного времени обновления. Нажми «🔄 Обновить кэш склада»."
    current_time = time.time() if now is None else float(now)
    age = current_time - float(updated_at)
    if age < -300:
        return "Время кэша склада выглядит некорректным. Обнови кэш склада перед выдачей."
    if age > max_age_seconds:
        hours = age / 3600
        return (
            f"Кэш склада устарел ({hours:.1f} ч). Выдача остановлена, чтобы не использовать "
            "устаревшие asset ID. Нажми «🔄 Обновить кэш склада»."
        )
    return None


def is_funpay_system_message(html_fragment: str) -> bool:
    """Trust payment text only inside FunPay's alert block used for system-authored messages."""
    if not html_fragment:
        return False
    # FunPay's own parser identifies system messages via author_id == 0 and extracts
    # message text from a div[role=alert] or .alert.alert-with-icon.alert-info.
    # A buyer can imitate the wording but cannot make their chat message a system alert.
    return bool(
        re.search(r"<[^>]+\brole\s*=\s*(['\"])alert\1", html_fragment, re.IGNORECASE)
        or re.search(
            r"<[^>]+\bclass\s*=\s*(['\"])[^'\"]*\balert-with-icon\b[^'\"]*\balert-info\b[^'\"]*\1",
            html_fragment,
            re.IGNORECASE,
        )
    )


def extract_order_number(text: str) -> str | None:
    """Extract a FunPay order identifier (which may contain letters)."""
    match = re.search(
        r"(?:заказ|order)\s*[№#]?\s*([A-Z0-9]{4,24})",
        text or "",
        re.IGNORECASE,
    )
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
