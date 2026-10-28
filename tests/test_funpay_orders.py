"""Regression tests for safe FunPay order matching."""
from funpay_orders import (decode_text_attachment, extract_last_chat_message_fragment, extract_steam_trade_url, extract_text_attachment_href, filter_reserved_assets, find_open_order_for_chat, funpay_cold_start_blockers, inventory_cache_freshness_error, is_funpay_system_message, mark_input_received, register_payment, voucher_redemption_block_reason, voucher_reset_block_reason)
from order_parser import is_paid_order_notification


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



def test_filter_reserved_assets_excludes_pending_and_uncertain_offers():
    cache = {
        "total_cards": 4,
        "games": {
            "Game": {
                "Card A": ["1", "2"],
                "Card B": ["3", "4"],
            }
        },
    }
    orders = {
        "paid": {
            "status": "offer_sent",
            "reservation": {"asset_ids": ["1"], "asset_locations": []},
        },
        "uncertain": {
            "status": "delivery_uncertain",
            "reservation": {"asset_ids": ["3"], "asset_locations": []},
        },
    }
    filtered = filter_reserved_assets(cache, orders, {})
    assert filtered["games"]["Game"]["Card A"] == ["2"]
    assert filtered["games"]["Game"]["Card B"] == ["4"]
    assert filtered["total_cards"] == 2
    assert cache["games"]["Game"]["Card A"] == ["1", "2"]


def test_filter_reserved_assets_excludes_uncertain_voucher_assets():
    cache = {"games": {"Game": {"Card": ["1", "2", "3"]}}}
    vouchers = {
        "SET1-A": {
            "delivery_state": "uncertain",
            "reserved_asset_ids": ["2"],
        }
    }
    filtered = filter_reserved_assets(cache, {}, vouchers)
    assert filtered["games"]["Game"]["Card"] == ["1", "3"]



def test_extract_attachment_link_inside_funpay_message_wrapper():
    fragment = (
        '<div class="chat-msg-item">'
        '<div class="chat-message"><a href="/file/download/987">trade-url.txt</a></div>'
        '</div>'
    )
    assert extract_text_attachment_href(
        fragment, "https://funpay.com/chat/?node=987"
    ) == "https://funpay.com/file/download/987"



def test_order_creation_notification_is_not_treated_as_paid():
    assert not is_paid_order_notification("Создан заказ #123456: набор из 1 сета Steam")


def test_order_number_and_product_description_are_not_payment_proof():
    assert not is_paid_order_notification("Заказ #123456 — 1 сет карточек Steam")


def test_explicit_paid_order_notification_is_recognized():
    assert is_paid_order_notification("Покупатель оплатил заказ #123456: 1 сет карточек Steam")
    assert is_paid_order_notification("Покупатель CoolBuyer оплатил заказ #123456. Лот: 1 сет Steam")
    assert is_paid_order_notification("Buyer CoolBuyer paid for order #123456: 1 set of Steam cards")
    assert is_paid_order_notification("Order #123456 has been paid: 5 Steam card sets")
    assert is_paid_order_notification("Заказ успешно оплачен: 5 сетов Steam")
    assert is_paid_order_notification("Оплата получена, переведите товар покупателю")


def test_negative_payment_text_is_not_treated_as_paid():
    assert not is_paid_order_notification("Покупатель не оплатил заказ #123456: 1 сет Steam")
    assert not is_paid_order_notification("Оплата не прошла")



def test_system_message_requires_funpay_alert_marker():
    assert is_funpay_system_message('<div role="alert">Покупатель Alex оплатил заказ #ABC12345</div>')
    assert is_funpay_system_message(
        '<div class="alert alert-with-icon alert-info">Заказ оплачен</div>'
    )
    assert not is_funpay_system_message(
        '<div class="chat-msg-text">Покупатель Alex оплатил заказ #ABC12345</div>'
    )


def test_funpay_order_number_can_contain_letters():
    from funpay_orders import extract_order_number, payment_key
    text = "Покупатель Alex оплатил заказ #ABC12345. Лот: Steam cards"
    assert extract_order_number(text) == "ABC12345"
    assert payment_key("123", text) == "fp-order-ABC12345"



def test_cold_start_is_blocked_for_unresolved_orders_and_reservations():
    orders = {
        "completed": {"status": "completed", "reservation": None},
        "cancelled": {"status": "cancelled", "reservation": None},
        "paid": {"status": "reserved", "reservation": {"asset_ids": ["123"]}},
        "sent": {"status": "offer_sent", "reservation": {"asset_ids": ["456"]}},
        "review": {"status": "review", "reservation": None},
    }
    assert funpay_cold_start_blockers(orders) == [
        ("paid", "reserved"),
        ("sent", "offer_sent"),
        ("review", "review"),
    ]


def test_cold_start_allows_only_terminal_orders_without_reservations():
    assert funpay_cold_start_blockers({
        "completed": {"status": "completed", "reservation": None},
        "cancelled": {"status": "cancelled", "reservation": None},
    }) == []
    assert funpay_cold_start_blockers({
        "bad": {"status": "completed", "reservation": {"asset_ids": ["1"]}}
    }) == [("bad", "completed")]


def test_inventory_cache_freshness_guard():
    now = 1_800_000_000
    assert inventory_cache_freshness_error({"updated_at": now, "games": {}}, now=now) is None
    assert "устарел" in inventory_cache_freshness_error(
        {"updated_at": now - 7 * 3600, "games": {}}, now=now
    )
    assert "времени обновления" in inventory_cache_freshness_error({"games": {}}, now=now)
    assert "некорректным" in inventory_cache_freshness_error(
        {"updated_at": now + 600, "games": {}}, now=now
    )



def test_reset_blocks_used_or_previously_sent_vouchers():
    assert voucher_reset_block_reason({"used": True}) is not None
    assert voucher_reset_block_reason({"used": False, "delivery_state": "sending"}) is not None
    assert voucher_reset_block_reason({
        "used": False, "delivery_state": "uncertain", "reserved_asset_ids": ["123"]
    }) is not None
    assert voucher_reset_block_reason({
        "used": True, "delivery_state": "sent", "trade_offer_id": "987654"
    }) is not None
    assert voucher_reset_block_reason({
        "used": False, "delivery_state": "created_unconfirmed", "trade_offer_id": "987654"
    }) is not None


def test_reset_allows_unused_voucher_or_definite_failed_attempt():
    assert voucher_reset_block_reason({"used": False}) is None
    assert voucher_reset_block_reason({
        "used": False, "delivery_state": "failed", "delivery_error": "rejected"
    }) is None
    assert voucher_reset_block_reason(None) is not None



def test_redemption_guard_blocks_stale_reset_state_even_when_used_is_false():
    assert voucher_redemption_block_reason({
        "used": False, "delivery_state": "sent", "trade_offer_id": "987654"
    }) is not None
    assert voucher_redemption_block_reason({
        "used": False, "delivery_state": "created_unconfirmed", "reserved_asset_ids": ["1"]
    }) is not None
    assert voucher_redemption_block_reason({
        "used": False, "delivery_state": "sending", "reserved_asset_ids": ["1"]
    }) is not None


def test_redemption_guard_allows_new_or_definitely_failed_code():
    assert voucher_redemption_block_reason({"used": False}) is None
    assert voucher_redemption_block_reason({
        "used": False, "delivery_state": "failed"
    }) is None
