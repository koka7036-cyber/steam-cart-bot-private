"""Regression tests for safe FunPay order matching."""
from funpay_orders import (decode_text_attachment, extract_last_chat_message_fragment, extract_steam_trade_url, extract_text_attachment_href, find_open_order_for_chat, mark_input_received, register_payment)


def make_order(key, node_id="123", status="awaiting_input", updated_at=1):
    return {
        "key": key,
        "node_id": node_id,
        "status": status,
        "updated_at": updated_at,
        "input_kind": None,
        "input_received_at": None,
    }


def test_no_open_order_returns_none():
    assert find_open_order_for_chat({}, "123") is None


def test_single_open_order_is_returned():
    order = make_order("only")
    assert find_open_order_for_chat({"only": order}, "123") is order


def test_multiple_open_orders_in_same_chat_are_ambiguous():
    older = make_order("older", updated_at=10)
    newer = make_order("newer", updated_at=20)
    result = find_open_order_for_chat({"older": older, "newer": newer}, "123")
    assert result is None


def test_closed_order_is_not_considered_open():
    order = make_order("done", status="delivered")
    assert find_open_order_for_chat({"done": order}, "123") is None


def test_orders_from_another_chat_are_ignored():
    order = make_order("other", node_id="999")
    assert find_open_order_for_chat({"other": order}, "123") is None


def test_input_received_updates_status_and_input_kind():
    order = make_order("only")
    mark_input_received(order, "file_attachment")
    assert order["status"] == "input_received"
    assert order["input_kind"] == "file_attachment"
    assert isinstance(order["input_received_at"], int)


def test_repeated_input_does_not_overwrite_first_input():
    order = make_order("only")
    mark_input_received(order, "file_attachment")
    first_received_at = order["input_received_at"]
    mark_input_received(order, "chat_trade_url")
    assert order["input_kind"] == "file_attachment"
    assert order["input_received_at"] == first_received_at


def test_delivered_order_cannot_be_moved_back_by_late_input():
    order = make_order("done", status="delivered")
    mark_input_received(order, "file_attachment")
    assert order["status"] == "delivered"
    assert order["input_kind"] is None



def test_review_order_is_not_overwritten_by_late_input():
    order = make_order("review", status="review")
    mark_input_received(order, "chat_trade_url")
    assert order["status"] == "review"
    assert order["input_kind"] is None



def test_payment_registration_is_idempotent_for_order_number():
    orders = {}
    first, created_first = register_payment(
        orders, node_id="123", buyer="Buyer", text="Заказ #123456 оплачен",
        sets_per_item=5, quantity=2,
    )
    second, created_second = register_payment(
        orders, node_id="123", buyer="Buyer", text="Заказ #123456 оплачен повторно",
        sets_per_item=5, quantity=2,
    )
    assert created_first is True
    assert created_second is False
    assert first is second
    assert first["sets_total"] == 10
    assert len(orders) == 1


def test_different_explicit_order_numbers_do_not_collapse():
    orders = {}
    first, created_first = register_payment(
        orders, node_id="123", buyer="Buyer", text="Заказ #123456 оплачен",
        sets_per_item=1, quantity=1,
    )
    second, created_second = register_payment(
        orders, node_id="123", buyer="Buyer", text="Заказ #123457 оплачен",
        sets_per_item=1, quantity=1,
    )
    assert created_first and created_second
    assert first is not second
    assert len(orders) == 2


def test_message_signature_is_stable_and_does_not_expose_preview():
    from funpay_orders import message_signature
    preview = "https://steamcommunity.com/tradeoffer/new/?partner=123&token=secret"
    first = message_signature("Buyer", preview)
    assert first == message_signature("Buyer", preview)
    assert preview not in first
    assert "secret" not in first


def test_message_signature_changes_when_preview_changes():
    from funpay_orders import message_signature
    assert message_signature("Buyer", "payment") != message_signature("Buyer", "file attached")


def test_extract_steam_trade_url_from_plain_text():
    url = "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=AbCdEf12"
    assert extract_steam_trade_url("Steam trade link: " + url) == url


def test_extract_steam_trade_url_rejects_non_steam_hosts():
    assert extract_steam_trade_url("https://example.com/tradeoffer/new/?partner=123&token=abc") is None


def test_extract_steam_trade_url_rejects_missing_token():
    assert extract_steam_trade_url("https://steamcommunity.com/tradeoffer/new/?partner=123") is None


def test_extract_steam_trade_url_rejects_ambiguous_multiple_links():
    a = "https://steamcommunity.com/tradeoffer/new/?partner=123&token=abc"
    b = "https://steamcommunity.com/tradeoffer/new/?partner=456&token=def"
    assert extract_steam_trade_url(a + "\n" + b) is None



def test_extract_text_attachment_href_from_funpay_chat_file():
    fragment = (
        '<div class="chat-msg-file">'
        '<a href="/file/12345" title="trade-url.txt">trade-url.txt</a>'
        '</div>'
    )
    assert extract_text_attachment_href(
        fragment, "https://funpay.com/chat/?node=987"
    ) == "https://funpay.com/file/12345"


def test_extract_text_attachment_href_rejects_external_hosts():
    fragment = '<div class="chat-msg-file"><a href="https://example.com/trade.txt">trade.txt</a></div>'
    assert extract_text_attachment_href(
        fragment, "https://funpay.com/chat/?node=987"
    ) is None


def test_extract_text_attachment_href_requires_unambiguous_attachment():
    fragment = (
        '<div class="chat-msg-file"><a href="/file/1">one.txt</a></div>'
        '<div class="chat-msg-file"><a href="/file/2">two.txt</a></div>'
    )
    assert extract_text_attachment_href(
        fragment, "https://funpay.com/chat/?node=987"
    ) is None


def test_decode_text_attachment_utf8_and_windows1251():
    url = "https://steamcommunity.com/tradeoffer/new/?partner=123456&token=AbCdEf12"
    assert extract_steam_trade_url(
        decode_text_attachment(("Ссылка: " + url).encode("utf-8"))
    ) == url
    assert extract_steam_trade_url(
        decode_text_attachment(("Ссылка: " + url).encode("cp1251"))
    ) == url


def test_decode_text_attachment_rejects_binary_data():
    import pytest
    with pytest.raises(ValueError):
        decode_text_attachment(bytes([0, 1, 2, 3]))


def test_decode_text_attachment_enforces_size_limit():
    import pytest
    with pytest.raises(ValueError):
        decode_text_attachment(b"A" * (64 * 1024 + 1))


def test_retry_required_order_is_open_for_corrected_input():
    order = make_order("retry", status="retry_required")
    assert find_open_order_for_chat({"retry": order}, "123") is order



def test_extract_last_chat_message_fragment_does_not_reuse_old_trade_link_for_latest_file():
    old_url = "https://steamcommunity.com/tradeoffer/new/?partner=123&token=OLD"
    html_doc = (
        '<div class="chat-msg-item"><div class="chat-msg-text">' + old_url + '</div></div>'
        '<div class="chat-msg-item"><div class="chat-msg-file">'
        '<a href="/file/765" title="trade-url.txt">trade-url.txt</a>'
        '</div></div>'
    )
    fragment = extract_last_chat_message_fragment(html_doc)
    assert fragment is not None
    assert "token=OLD" not in fragment
    assert "trade-url.txt" in fragment
    assert extract_text_attachment_href(
        fragment, "https://funpay.com/chat/?node=987"
    ) == "https://funpay.com/file/765"


def test_extract_last_chat_message_fragment_returns_latest_text():
    html_doc = (
        '<div class="chat-msg-item"><div class="chat-msg-text">old</div></div>'
        '<div class="chat-msg-item"><div class="chat-msg-text">new</div></div>'
    )
    assert "new" in extract_last_chat_message_fragment(html_doc)
    assert "old" not in extract_last_chat_message_fragment(html_doc)
