"""Behavior tests for Playerok order tracking.

These tests intentionally use only synthetic deal data and never contact Playerok.
"""
from playerok_orders import (
    active_orders_for_chat,
    map_item_to_sets,
    mark_trade_url_received,
    normalize_platform_status,
    summarize_orders,
    upsert_deal,
)


def test_item_mapping_requires_explicit_unique_listing_ids():
    assert map_item_to_sets("item-one", "item-one", "item-five") == 1
    assert map_item_to_sets("item-five", "item-one", "item-five") == 5
    assert map_item_to_sets("unknown", "item-one", "item-five") is None
    assert map_item_to_sets("same", "same", "same") is None


def test_duplicate_paid_event_does_not_duplicate_order_or_change_it():
    orders = {}
    first, created, changed = upsert_deal(
        orders,
        deal_id="deal-123",
        status="PAID",
        item_id="item-one",
        item_name="Steam card set x1",
        chat_id="chat-4",
        sets=1,
        event_type="ITEM_PAID",
    )
    assert created is True
    assert changed is True
    same, created_again, changed_again = upsert_deal(
        orders,
        deal_id="deal-123",
        status="PAID",
        item_id="item-one",
        item_name="Steam card set x1",
        chat_id="chat-4",
        sets=1,
        event_type="NEW_DEAL",
    )
    assert same["deal_id"] == first["deal_id"]
    assert created_again is False
    assert changed_again is False
    assert len(orders) == 1


def test_older_paid_event_cannot_regress_sent_or_confirmed_deal():
    orders = {}
    upsert_deal(
        orders, deal_id="d1", status="PAID", item_id="i1",
        chat_id="c1", sets=1, event_type="ITEM_PAID",
    )
    upsert_deal(
        orders, deal_id="d1", status="SENT", item_id="i1",
        chat_id="c1", sets=1, event_type="ITEM_SENT",
    )
    record, _, changed = upsert_deal(
        orders, deal_id="d1", status="PAID", item_id="i1",
        chat_id="c1", sets=1, event_type="ITEM_PAID",
    )
    assert record["platform_status"] == "sent"
    assert changed is False


def test_unknown_listing_is_held_for_manual_review():
    orders = {}
    record, created, _ = upsert_deal(
        orders,
        deal_id="deal-unknown",
        status="PAID",
        item_id="item-missing-mapping",
        item_name="Unknown lot",
        chat_id="chat-10",
        sets=None,
        event_type="ITEM_PAID",
    )
    assert created is True
    assert record["fulfillment_status"] == "needs_review"
    assert record["sets"] is None


def test_trade_link_event_matches_only_one_open_order_and_never_stores_url():
    orders = {}
    upsert_deal(
        orders, deal_id="d1", status="PAID", item_id="i1",
        chat_id="chat-one", sets=1, event_type="ITEM_PAID",
    )
    record, result = mark_trade_url_received(orders, chat_id="chat-one")
    assert result == "recorded"
    assert record["trade_url_received"] is True
    assert record["fulfillment_status"] == "manual_review"
    assert "trade_url" not in record
    assert "token" not in record
    assert active_orders_for_chat(orders, "chat-one") == [record]


def test_trade_link_is_not_assigned_when_chat_has_multiple_open_deals():
    orders = {}
    for deal_id in ("d1", "d2"):
        upsert_deal(
            orders, deal_id=deal_id, status="PAID", item_id="i1",
            chat_id="same-chat", sets=1, event_type="ITEM_PAID",
        )
    record, result = mark_trade_url_received(orders, chat_id="same-chat")
    assert record is None
    assert result == "ambiguous"
    assert all(not row["trade_url_received"] for row in orders.values())


def test_trade_link_without_open_deal_requires_manual_matching():
    record, result = mark_trade_url_received({}, chat_id="no-order")
    assert record is None
    assert result == "unmatched"


def test_status_normalization_handles_enums_and_unknown_values():
    class Status:
        name = "CONFIRMED_AUTOMATICALLY"

    assert normalize_platform_status(Status()) == "confirmed"
    assert normalize_platform_status("DEAL_HAS_PROBLEM") == "problem"
    assert normalize_platform_status("ROLLED_BACK") == "rolled_back"
    assert normalize_platform_status("random-new-status") == "unknown"


def test_summary_counts_orders_without_revenue_estimates():
    orders = {}
    upsert_deal(
        orders, deal_id="d1", status="PAID", item_id="i1",
        chat_id="c1", sets=1, event_type="ITEM_PAID",
    )
    upsert_deal(
        orders, deal_id="d2", status="PAID", item_id="unknown",
        chat_id="c2", sets=None, event_type="ITEM_PAID",
    )
    upsert_deal(
        orders, deal_id="d3", status="CONFIRMED", item_id="i5",
        chat_id="c3", sets=5, event_type="DEAL_CONFIRMED",
    )
    summary = summarize_orders(orders)
    assert summary["total"] == 3
    assert summary["paid"] == 1
    assert summary["confirmed"] == 1
    assert summary["needs_review"] == 1
    assert "revenue" not in summary


def test_atomic_ledger_save_strips_unapproved_fields_and_trade_urls(tmp_path):
    from playerok_orders import load_orders, save_orders

    target = tmp_path / "playerok_orders.json"
    link = "https://steamcommunity.com/tradeoffer/new/?partner=123&token=private-token"
    save_orders(
        {
            "orders": {
                "d1": {
                    "deal_id": "d1",
                    "platform_status": "paid",
                    "fulfillment_status": "manual_review",
                    "sets": 1,
                    "trade_url_received": True,
                    "trade_url": link,
                    "message_text": link,
                    "session_token": "secret-session-token",
                }
            }
        },
        target,
    )
    raw = target.read_text(encoding="utf-8")
    saved = load_orders(target)["orders"]["d1"]
    assert link not in raw
    assert "private-token" not in raw
    assert "secret-session-token" not in raw
    assert "trade_url" not in saved
    assert "message_text" not in saved
    assert "session_token" not in saved


def test_listing_name_redacts_urls_before_persistence():
    orders = {}
    record, _, _ = upsert_deal(
        orders,
        deal_id="d-url",
        status="PAID",
        item_id="item-one",
        item_name="Steam cards https://steamcommunity.com/tradeoffer/new/?token=never-store",
        chat_id="c-url",
        sets=1,
    )
    assert "tradeoffer/new" not in record["item_name"]
    assert "never-store" not in record["item_name"]


def test_monitor_without_token_stays_disabled_without_network():
    from playerok_monitor import PlayerokMonitor

    monitor = PlayerokMonitor(token="", one_set_item_id="i1", five_set_item_id="i5")
    assert monitor.start() is False
    status = monitor.status()
    assert status["state"] == "not_configured"
    assert status["auto_delivery"] is False


def test_problem_deal_is_not_used_for_automatic_chat_matching():
    orders = {}
    upsert_deal(
        orders, deal_id="problem-deal", status="PAID", item_id="one",
        chat_id="problem-chat", sets=1, event_type="ITEM_PAID",
    )
    upsert_deal(
        orders, deal_id="problem-deal", status="DEAL_HAS_PROBLEM", item_id="one",
        chat_id="problem-chat", sets=1, event_type="DEAL_HAS_PROBLEM",
    )
    assert active_orders_for_chat(orders, "problem-chat") == []
    assert summarize_orders(orders)["problem"] == 1


def test_corrupted_untrusted_quantity_does_not_crash_ledger_sanitizer(tmp_path):
    from playerok_orders import save_orders, load_orders

    target = tmp_path / "playerok_test_corrupt_tmp.json"
    save_orders(
        {
            "orders": {
                "d-corrupt": {
                    "deal_id": "d-corrupt",
                    "platform_status": "PAID",
                    "fulfillment_status": "needs_review",
                    "sets": ["not", "a", "number"],
                    "trade_url": "https://steamcommunity.com/tradeoffer/new/?token=secret",
                }
            }
        },
        target,
    )
    saved = load_orders(target)["orders"]["d-corrupt"]
    assert saved["sets"] is None
    assert "trade_url" not in saved
