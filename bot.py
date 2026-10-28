import os
import re
import html
import json
import logging
import time
import base64
import random
import string
import tempfile
import winreg
import threading
import urllib.parse
import requests
import rsa
import telebot
from telebot import types

# --- КОНФИГУРАЦИЯ ---
TG_TOKEN = os.getenv("TG_TOKEN", "")
STEAM_ID = os.getenv("STEAM_ID", "")
STEAM_LOGIN = os.getenv("STEAM_LOGIN", "")
STEAM_PASSWORD = os.getenv("STEAM_PASSWORD", "")
ASF_IPC_URL = "http://127.0.0.1:1242/Api/Command"

BAT_PATH = r"C:\CardBot\start.bat"
REG_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
REG_APP_NAME = "CardBotFarmAutostart"

DB_FILE = "vouchers.json"
CACHE_FILE = "inventory_cache.json"
SESSION_FILE = "steam_session.json"
FP_CONFIG_FILE = "funpay_config.json"
FP_ORDERS_FILE = "funpay_orders.json"
PLAYEROK_ORDERS_FILE = "playerok_orders.json"

if not TG_TOKEN:
    raise RuntimeError("Установи переменную окружения TG_TOKEN перед запуском бота.")

bot = telebot.TeleBot(TG_TOKEN)

try:
    BOT_USERNAME = bot.get_me().username
except Exception:
    BOT_USERNAME = "PeakSteamCards_bot"

def load_json(filename, default):
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(filename, data):
    target = os.path.abspath(filename)
    directory = os.path.dirname(target)
    fd, temp_path = tempfile.mkstemp(prefix=".cardbot-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

logging.basicConfig(
    filename="bot_errors.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

# Telegram may reject malformed Markdown/HTML in dynamic text. Retry only
# entity-parsing failures as plain text; preserve all other API errors.
_original_send_message = bot.send_message
_original_edit_message_text = bot.edit_message_text

def _send_message_with_plain_text_fallback(*args, **kwargs):
    try:
        return _original_send_message(*args, **kwargs)
    except Exception as exc:
        error = str(exc).lower()
        if "can't parse entities" not in error and "can't find end of the entity" not in error:
            raise
        kwargs.pop("parse_mode", None)
        logging.warning("Telegram rejected message entities; retrying as plain text.")
        return _original_send_message(*args, **kwargs)

def _edit_message_with_plain_text_fallback(*args, **kwargs):
    try:
        return _original_edit_message_text(*args, **kwargs)
    except Exception as exc:
        error = str(exc).lower()
        if "can't parse entities" not in error and "can't find end of the entity" not in error:
            raise
        kwargs.pop("parse_mode", None)
        logging.warning("Telegram rejected edited-message entities; retrying as plain text.")
        return _original_edit_message_text(*args, **kwargs)

bot.send_message = _send_message_with_plain_text_fallback
bot.edit_message_text = _edit_message_with_plain_text_fallback

# --- МОДУЛЬ 1: АВТОЗАПУСК WINDOWS ---
def is_autostart_enabled():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_READ)
        val, _ = winreg.QueryValueEx(key, REG_APP_NAME)
        winreg.CloseKey(key)
        return bool(val)
    except Exception:
        return False

def toggle_autostart():
    enabled = is_autostart_enabled()
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.DeleteValue(key, REG_APP_NAME)
            winreg.CloseKey(key)
            return False, "🔴 Автозапуск с Windows **ВЫКЛЮЧЕН**."
        else:
            winreg.SetValueEx(key, REG_APP_NAME, 0, winreg.REG_SZ, f'"{BAT_PATH}"')
            winreg.CloseKey(key)
            return True, "🟢 Автозапуск с Windows **ВКЛЮЧЕН**!"
    except Exception as e:
        return enabled, f"❌ Ошибка реестра: {e}"

# --- МОДУЛЬ 2: FUNPAY 24/7 + АВТОБОТ ПРОДАЖ ---
fp_state = {
    "online": False,
    "user_id": None,
    "csrf_token": None,
    "phpsessid": None,
    "last_ping": "Никогда",
    "last_raise": "Ожидание лотов",
    "error": None
}

active_reply_target = {}
pending_customer_codes = {}
delivery_lock = threading.Lock()
funpay_orders_lock = threading.Lock()
playerok_orders_lock = threading.RLock()
playerok_monitor = None

def get_fp_session():
    cfg = load_json(FP_CONFIG_FILE, {"golden_key": ""})
    golden_key = cfg.get("golden_key", "").strip()
    if not golden_key:
        return None, None, "Не задан golden_key (отправь /fp КЛЮЧ)"

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    })
    s.cookies.set("golden_key", golden_key, domain="funpay.com")
    if fp_state.get("phpsessid"):
        s.cookies.set("PHPSESSID", fp_state["phpsessid"], domain="funpay.com")
    return s, cfg, None

def funpay_ping_and_raise(force_raise=False):
    s, cfg, err = get_fp_session()
    if err:
        fp_state["online"] = False
        fp_state["error"] = err
        return False, err

    try:
        r = s.get("https://funpay.com/", timeout=15)
        if "PHPSESSID" in s.cookies:
            fp_state["phpsessid"] = s.cookies["PHPSESSID"]

        match = re.search(r'data-app-data="([^"]+)"', r.text)
        if not match:
            fp_state["online"] = False
            fp_state["error"] = "Не удалось прочитать FunPay."
            return False, fp_state["error"]

        app_data = json.loads(html.unescape(match.group(1)))
        user_id = app_data.get("userId", 0)
        csrf_token = app_data.get("csrf-token", "")

        if not user_id:
            fp_state["online"] = False
            fp_state["error"] = "Неверный или устаревший golden_key!"
            return False, fp_state["error"]

        fp_state["online"] = True
        fp_state["user_id"] = user_id
        fp_state["csrf_token"] = csrf_token
        fp_state["last_ping"] = time.strftime("%H:%M:%S")
        fp_state["error"] = None

        last_raise_ts = cfg.get("last_raise_ts", 0)
        if force_raise or (time.time() - last_raise_ts >= 14400):
            prof = s.get(f"https://funpay.com/users/{user_id}/", timeout=15)
            nodes = list(set(re.findall(r'https://funpay\.com/lots/(\d+)/trade', prof.text)))
            if nodes:
                raised_cnt = 0
                for node_id in nodes:
                    trade_page = s.get(f"https://funpay.com/lots/{node_id}/trade", timeout=15)
                    g_match = re.search(r'data-game="(\d+)"\s+data-node="' + str(node_id) + r'"', trade_page.text)
                    if g_match:
                        game_id = g_match.group(1)
                        ajax_headers = {"X-Requested-With": "XMLHttpRequest", "Referer": f"https://funpay.com/lots/{node_id}/trade"}
                        resp = s.post("https://funpay.com/lots/raise", data={"game_id": game_id, "node_id": node_id}, headers=ajax_headers, timeout=15).json()
                        if "modal" in resp:
                            sub_nodes = re.findall(r'name="node_ids\[\]"\s+value="(\d+)"', resp["modal"])
                            if sub_nodes:
                                resp = s.post("https://funpay.com/lots/raise", data={"game_id": game_id, "node_id": node_id, "node_ids[]": sub_nodes}, headers=ajax_headers, timeout=15).json()
                        if resp.get("error") == 0:
                            raised_cnt += 1
                cfg["last_raise_ts"] = time.time()
                save_json(FP_CONFIG_FILE, cfg)
                fp_state["last_raise"] = f"Поднято разделов: {raised_cnt}/{len(nodes)} в {time.strftime('%H:%M')}"

        return True, f"Онлайн активен (ID: {user_id})"
    except Exception as e:
        fp_state["error"] = f"Ошибка связи с FunPay: {e}"
        return False, fp_state["error"]

def send_funpay_chat_message(node_id, text):
    # The account has received a moderation warning. This integration can only
    # send ordinary support text: it must never become a channel for directing
    # a buyer to an external site, messenger, or delivery flow.
    if re.search(r"(?:https?://|www\.|t\.me|telegram|discord|steamcommunity)", text or "", re.IGNORECASE):
        return False, "Отправка ссылок во внешний сервис через FunPay заблокирована защитой модерации."
    s, cfg, err = get_fp_session()
    if err:
        return False, err

    try:
        r = s.get(f"https://funpay.com/chat/?node={node_id}", timeout=15)
        match = re.search(r'data-app-data="([^"]+)"', r.text)
        if not match:
            return False, "Не удалось получить csrf-token чата."

        app_data = json.loads(html.unescape(match.group(1)))
        csrf_token = app_data.get("csrf-token")
        user_id = app_data.get("userId")
        if not csrf_token or not user_id:
            return False, "Сессия FunPay не авторизована."

        # node_id из URL /chat/?node=... — это ID чата.
        # data-tag — служебный тег Runner, а не идентификатор чата.
        chat_node = str(node_id)
        objects = [{"type": "chat_node", "id": chat_node, "tag": "00000000",
                    "data": {"node": chat_node, "last_message": -1, "content": ""}}]
        request_action = {"action": "chat_message",
                          "data": {"node": chat_node, "last_message": -1, "content": text}}
        payload = {"objects": json.dumps(objects), "request": json.dumps(request_action),
                   "csrf_token": csrf_token}
        ajax_headers = {"X-Requested-With": "XMLHttpRequest", "Origin": "https://funpay.com",
                        "Referer": f"https://funpay.com/chat/?node={node_id}"}

        resp = s.post("https://funpay.com/runner/", data=payload, headers=ajax_headers, timeout=15)
        resp.raise_for_status()
        res_json = resp.json()
        response_data = res_json.get("response")
        if isinstance(response_data, dict) and not response_data.get("error"):
            # Обновляем отметку только после положительного ответа FunPay.
            seen = cfg.get("seen_msgs", {})
            seen[str(node_id)] = f"me:{text}"
            cfg["seen_msgs"] = seen
            save_json(FP_CONFIG_FILE, cfg)
            return True, "Сообщение доставлено!"
        return False, f"FunPay не подтвердил отправку: {res_json}"
    except Exception as e:
        logging.exception("Ошибка отправки сообщения в FunPay, node=%s", node_id)
        return False, f"Ошибка отправки: {e}"

from order_parser import detect_order_sets_and_quantity, is_paid_order_notification
from funpay_orders import (decode_text_attachment, extract_last_chat_message_fragment, extract_steam_trade_url, extract_text_attachment_href, filter_reserved_assets, find_open_order_for_chat, funpay_cold_start_blockers, inventory_cache_freshness_error, is_funpay_system_message, mark_input_received, register_payment, message_signature, voucher_redemption_block_reason, voucher_reset_block_reason)
from sales_analytics import summarize_sales
from playerok_orders import (
    active_orders_for_chat,
    load_orders as load_playerok_orders,
    mark_trade_url_received,
    save_orders as save_playerok_orders,
    summarize_orders,
    upsert_deal,
)
from playerok_monitor import PlayerokMonitor


def load_funpay_orders():
    data = load_json(FP_ORDERS_FILE, {"orders": {}})
    if not isinstance(data, dict):
        data = {"orders": {}}
    if not isinstance(data.get("orders"), dict):
        data["orders"] = {}
    return data


def get_funpay_delivery_policy():
    """Read the explicit, locally persisted FunPay delivery settings."""
    cfg = load_json(FP_CONFIG_FILE, {})
    return {
        "input_mode": str(cfg.get("delivery_input_mode", "both")).lower(),
        "allow_chat_trade_url": cfg.get("allow_chat_trade_url", False) is True,
        "auto_delivery": cfg.get("auto_delivery_funpay", False) is True,
    }


def migrate_funpay_delivery_config():
    """Apply the seller-approved in-chat workflow once; never enable FunPay built-in auto-delivery."""
    cfg = load_json(FP_CONFIG_FILE, {})
    if cfg.get("funpay_workflow_version", 0) >= 2:
        return
    # The owner explicitly chose the two input methods for this custom bot:
    # a Steam trade URL in FunPay chat or a small text attachment in that chat.
    # No external file-hosting URL or Telegram contact is sent to buyers.
    cfg["delivery_input_mode"] = "both"
    cfg["allow_chat_trade_url"] = True
    cfg["auto_delivery_funpay"] = True
    cfg["funpay_workflow_version"] = 2
    save_json(FP_CONFIG_FILE, cfg)


def recover_interrupted_funpay_deliveries():
    """Quarantine interrupted Steam requests; never retry an ambiguous POST."""
    changed_keys = []
    with funpay_orders_lock:
        data = load_funpay_orders()
        for key, record in data["orders"].items():
            if isinstance(record, dict) and record.get("status") == "sending":
                record["status"] = "delivery_uncertain"
                record["updated_at"] = int(time.time())
                record.setdefault("notes", []).append(
                    "Бот перезапустился во время отправки Steam-предложения; повторная отправка заблокирована во избежание дубля."
                )
                changed_keys.append(key)
        if changed_keys:
            save_json(FP_ORDERS_FILE, data)

    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    voucher_changes = False
    for code, voucher in db.get("vouchers", {}).items():
        if isinstance(voucher, dict) and voucher.get("delivery_state") == "sending":
            voucher["delivery_state"] = "uncertain"
            voucher["delivery_error"] = "Бот перезапустился во время запроса; проверь историю Steam вручную."
            voucher_changes = True
            changed_keys.append(f"код {code}")
    if voucher_changes:
        save_json(DB_FILE, db)

    if changed_keys and db.get("admin_id"):
        bot.send_message(
            db["admin_id"],
            "⚠️ После перезапуска обнаружены операции с неопределённым результатом Steam: "
            + ", ".join(changed_keys[:8])
            + ". Проверь историю предложений Steam вручную. Автоматический повтор заблокирован."
        )


def restore_reservation_to_cache(reservation):
    """Restore a reservation only after verified cancellation/return of the Steam offer."""
    locations = reservation.get("asset_locations", []) if isinstance(reservation, dict) else []
    if not locations:
        return False
    with delivery_lock:
        cache = load_json(CACHE_FILE, {})
        games = cache.get("games", {})
        for item in locations:
            game = item.get("game")
            card = item.get("card")
            asset_id = str(item.get("asset_id", ""))
            if not game or not card or not asset_id:
                continue
            game_cards = games.setdefault(game, {})
            card_assets = game_cards.setdefault(card, [])
            if asset_id not in card_assets:
                card_assets.append(asset_id)
        cache["total_cards"] = sum(
            len(asset_ids) for cards in games.values() for asset_ids in cards.values()
        )
        save_json(CACHE_FILE, cache)
    return True


def reserve_funpay_order(record, ledger_data=None):
    """Reserve exact card asset IDs once, journal them first, then update the cache."""
    with delivery_lock:
        if record.get("reservation"):
            return True, "Резерв уже существует."
        asset_ids, cache_bundle, err = pick_cards_for_sets(record["sets_total"])
        if err:
            record["status"] = "review"
            record.setdefault("notes", []).append(f"Резерв не создан: {err}")
            record["updated_at"] = int(time.time())
            return False, err
        updated_cache, picked_games_info, asset_locations = cache_bundle
        record["reservation"] = {
            "asset_ids": asset_ids,
            "asset_locations": asset_locations,
            "picked_games": picked_games_info,
            "created_at": int(time.time()),
        }
        record["status"] = "reserved"
        record["updated_at"] = int(time.time())
        # Journal the exact asset reservation before changing the cache on disk.
        if ledger_data is not None:
            save_json(FP_ORDERS_FILE, ledger_data)
        save_json(CACHE_FILE, updated_cache)
        return True, "Карточки зарезервированы."


def register_funpay_payment(node_id, buyer, text, sets_per_item, quantity):
    """Create an idempotent order record and reserve stock exactly once."""
    with funpay_orders_lock:
        data = load_funpay_orders()
        record, created = register_payment(
            data["orders"], node_id=str(node_id), buyer=buyer, text=text,
            sets_per_item=sets_per_item, quantity=quantity,
        )
        if created:
            # Register the paid order first; then persist exact asset IDs before cache mutation.
            save_json(FP_ORDERS_FILE, data)
            reserve_funpay_order(record, data)
            save_json(FP_ORDERS_FILE, data)
        return record, created


def record_funpay_buyer_input(node_id, trade_url, input_kind):
    """Safely bind a validated Steam URL to exactly one paid FunPay order and send once."""
    normalized_url = extract_steam_trade_url(trade_url)
    if not normalized_url:
        return None, "invalid_url", "Не удалось выделить ровно одну корректную Steam trade-ссылку."

    policy = get_funpay_delivery_policy()
    if not policy["allow_chat_trade_url"] or not policy["auto_delivery"]:
        return None, "disabled", "Автовыдача отключена настройкой FunPay; заказ оставлен без изменений."

    with funpay_orders_lock:
        data = load_funpay_orders()
        record = find_open_order_for_chat(data["orders"], node_id)
        if not record:
            candidates = [
                item for item in data["orders"].values()
                if isinstance(item, dict)
                and str(item.get("node_id")) == str(node_id)
                and item.get("status") in {"sending", "delivery_uncertain", "awaiting_input", "input_received", "reserved", "review", "retry_required"}
            ]
            if len(candidates) > 1:
                return None, "ambiguous", "В этом чате несколько активных заказов."
            if len(candidates) == 1 and candidates[0].get("status") == "sending":
                return candidates[0], "in_progress", "Заказ уже обрабатывается; повторная отправка не выполняется."
            if len(candidates) == 1 and candidates[0].get("status") == "delivery_uncertain":
                return candidates[0], "uncertain", "Результат предыдущей попытки неизвестен; повторная отправка заблокирована."
            return None, "unmatched", "Не найден единственный подходящий оплаченный заказ."

        if record.get("steam_trade_offer_id") or record.get("status") in {
            "offer_sent", "offer_pending_confirmation", "completed", "cancelled"
        }:
            return record, "duplicate", "У заказа уже есть Steam-предложение или он закрыт."

        if record.get("status") == "sending":
            return record, "in_progress", "Заказ уже обрабатывается; повторная отправка не выполняется."
        if record.get("status") == "delivery_uncertain":
            return record, "uncertain", "Результат предыдущей попытки неизвестен; повторная отправка заблокирована."

        if not record.get("reservation"):
            reserved, reserve_message = reserve_funpay_order(record, data)
            if not reserved or not record.get("reservation"):
                record["status"] = "review"
                record["updated_at"] = int(time.time())
                save_json(FP_ORDERS_FILE, data)
                return record, "no_stock", reserve_message

        reservation = record.get("reservation") or {}
        asset_ids = [str(value) for value in reservation.get("asset_ids", []) if str(value)]
        if not asset_ids:
            record["status"] = "review"
            record["updated_at"] = int(time.time())
            record.setdefault("notes", []).append("Резерв пуст: Steam-предложение не создавалось.")
            save_json(FP_ORDERS_FILE, data)
            return record, "no_stock", "В резерве не оказалось карточек."

        order_key = str(record.get("key") or "")
        order_label = str(record.get("funpay_order_number") or order_key)
        record["status"] = "sending"
        record["input_kind"] = input_kind
        record["input_received_at"] = int(time.time())
        record["delivery_attempt_started_at"] = int(time.time())
        record["delivery_attempts"] = int(record.get("delivery_attempts", 0)) + 1
        record["updated_at"] = int(time.time())
        record.setdefault("notes", []).append(
            f"Начата отправка Steam-предложения по входу {input_kind}; ссылка не сохраняется в журнале."
        )
        save_json(FP_ORDERS_FILE, data)

    outcome, result_message, offer_id = send_trade_offer(
        normalized_url, asset_ids, f"FunPay-{order_label}"
    )

    with funpay_orders_lock:
        data = load_funpay_orders()
        record = data["orders"].get(order_key)
        if not isinstance(record, dict):
            logging.error("FunPay order %s disappeared while Steam offer was being sent.", order_key)
            return None, "uncertain", "Запись заказа не найдена после отправки; проверь Steam вручную."

        record["updated_at"] = int(time.time())
        record["last_delivery_message"] = str(result_message)[:350]
        if outcome == "sent":
            record["status"] = "offer_sent"
            record["steam_trade_offer_id"] = str(offer_id)
            record["steam_offer_confirmed"] = True
            record["trade_offer_created_at"] = int(time.time())
        elif outcome == "created_unconfirmed":
            # The offer exists: never retry its creation. ASF confirmation may still be pending.
            record["status"] = "offer_pending_confirmation"
            record["steam_trade_offer_id"] = str(offer_id)
            record["steam_offer_confirmed"] = False
            record["trade_offer_created_at"] = int(time.time())
            record.setdefault("notes", []).append("Steam создал предложение, но ASF не подтвердил автоматическое подтверждение.")
        elif outcome == "unknown":
            record["status"] = "delivery_uncertain"
            record.setdefault("notes", []).append(
                "Ответ Steam не позволил определить результат. Повторная отправка автоматически запрещена во избежание дублирования."
            )
        else:
            # A definite rejection did not return a tradeofferid. Keep stock reserved
            # and allow the buyer to send corrected data; do not save the URL.
            record["status"] = "retry_required"
            record.setdefault("notes", []).append("Steam вернул отказ без ID предложения; резерв сохранён, повтор возможен после нового входа.")
        save_json(FP_ORDERS_FILE, data)
        return record, record["status"], result_message


MAX_FUNPAY_TEXT_ATTACHMENT_BYTES = 64 * 1024


def download_funpay_text_attachment(session, attachment_url, chat_url):
    """Download a small .txt-like attachment from FunPay only, without external redirects."""
    current_url = urllib.parse.urljoin(chat_url, attachment_url)
    allowed_hosts = ("funpay.com",)
    response = None
    try:
        for _ in range(4):
            parsed = urllib.parse.urlparse(current_url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not (host == "funpay.com" or host.endswith("." + allowed_hosts[0])):
                return None, "Ссылка на вложение ведёт не на FunPay; загрузка заблокирована."
            response = session.get(
                current_url, timeout=12, allow_redirects=False, stream=True
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                response = None
                if not location:
                    return None, "FunPay вернул перенаправление без адреса файла."
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            if response.status_code >= 400:
                return None, f"FunPay не дал скачать вложение (HTTP {response.status_code})."

            disposition = response.headers.get("Content-Disposition", "")
            name_match = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", disposition, re.IGNORECASE)
            filename = urllib.parse.unquote(name_match.group(1).strip().strip('"')) if name_match else ""
            path_name = urllib.parse.urlparse(current_url).path
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            extension_ok = any(
                value.lower().endswith(ext)
                for value in (filename, path_name)
                for ext in (".txt", ".text", ".csv", ".log")
            )
            if not extension_ok and content_type not in {"text/plain", "text/csv"}:
                return None, "Вложение не подтверждено как текстовый файл (.txt/.text/.csv/.log)."

            size_header = response.headers.get("Content-Length")
            if size_header and size_header.isdigit() and int(size_header) > MAX_FUNPAY_TEXT_ATTACHMENT_BYTES:
                return None, "Текстовый файл слишком большой (лимит 64 КБ)."
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_FUNPAY_TEXT_ATTACHMENT_BYTES:
                    return None, "Текстовый файл слишком большой (лимит 64 КБ)."
                chunks.append(chunk)
            try:
                return decode_text_attachment(b"".join(chunks), MAX_FUNPAY_TEXT_ATTACHMENT_BYTES), None
            except ValueError as exc:
                return None, str(exc)
        return None, "Слишком много перенаправлений при скачивании вложения."
    except requests.RequestException as exc:
        return None, f"Ошибка загрузки вложения FunPay ({type(exc).__name__})."
    finally:
        if response is not None:
            response.close()


def apply_reserved_assets_to_cache(cache_data, orders=None, vouchers=None):
    """Filter the current cache using persistent FunPay and voucher reservations."""
    if orders is None:
        orders = load_funpay_orders().get("orders", {})
    if vouchers is None:
        vouchers = load_json(DB_FILE, {"admin_id": None, "vouchers": {}}).get("vouchers", {})
    return filter_reserved_assets(cache_data, orders, vouchers)


def funpay_check_unread_chats():
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    admin_id = db.get("admin_id")
    if not admin_id:
        return

    s, cfg, err = get_fp_session()
    if err:
        return

    seen_msgs_value = cfg.get("seen_msgs", {})
    seen_msgs = seen_msgs_value if isinstance(seen_msgs_value, dict) else {}
    first_run = "seen_msgs" not in cfg or not isinstance(seen_msgs_value, dict)

    try:
        r = s.get("https://funpay.com/chat/", timeout=15)
        r.raise_for_status()
        if "PHPSESSID" in s.cookies:
            fp_state["phpsessid"] = s.cookies["PHPSESSID"]

        blocks = r.text.split('<a href="https://funpay.com/chat/?node=')[1:]
        changed = False

        # Migrate old raw preview signatures to hashes so local config no longer
        # retains message text (which could include a trade URL).
        for old_node, old_signature in list(seen_msgs.items()):
            if isinstance(old_signature, str) and old_signature.startswith("sha256:v2:"):
                continue
            if isinstance(old_signature, str) and old_signature.startswith("sha256:"):
                # Preserve old hashes without retaining possibly sensitive chat text.
                # Historical read chats are baselined instead of replayed.
                seen_msgs[old_node] = "legacy:" + old_signature
            elif isinstance(old_signature, str) and ":" in old_signature:
                old_sender, old_preview = old_signature.split(":", 1)
                seen_msgs[old_node] = "legacy:" + message_signature(old_sender, old_preview)
            else:
                seen_msgs.pop(old_node, None)
            changed = True

        for b in blocks[:15]:
            node_m = re.match(r'(\d+)"\s+class="([^"]*)"', b)
            if not node_m:
                continue
            node_id = node_m.group(1)
            classes = node_m.group(2)

            name_m = re.search(r'<div class="media-user-name">\s*([^<]+?)\s*</div>', b)
            sender_name = html.unescape(name_m.group(1).strip()) if name_m else f"Диалог {node_id}"
            msg_m = re.search(r'<div class="contact-item-message">([\s\S]*?)</div>', b)
            msg_preview = html.unescape(re.sub(r'<[^>]+>', '', msg_m.group(1)).strip()) if msg_m else ""
            signature_block = re.sub(
                r'class=(["\'])(.*?)\1',
                lambda match: 'class=' + match.group(1) + " ".join(
                    token for token in match.group(2).split()
                    if token.lower() not in {"unread", "active", "selected"}
                ) + match.group(1),
                b,
                flags=re.IGNORECASE,
            )
            sig = "sha256:v2:" + message_signature(
                sender_name, msg_preview + "|" + signature_block
            )

            # On first start, remember only already-read chats. Unread chats
            # are processed normally so paid orders are not skipped.
            if first_run and "unread" not in classes:
                seen_msgs[node_id] = sig
                changed = True
                continue

            previous_sig = seen_msgs.get(node_id)
            # Upgrade old signatures without replaying historical read chats.
            # Unread chats remain eligible during migration so new payments aren't skipped.
            if not str(previous_sig or "").startswith("sha256:v2:") and "unread" not in classes:
                seen_msgs[node_id] = sig
                changed = True
                continue

            if previous_sig == sig:
                continue

            # Process a changed message even if the user opened the chat and it is
            # no longer marked unread by FunPay.
            if node_id not in seen_msgs and "unread" not in classes:
                seen_msgs[node_id] = sig
                changed = True
                continue

            # Do not acknowledge an unread message until its detail page was
            # retrieved and the message was handled. Failed reads will retry.
            full_text = msg_preview
            last_message_html = ""
            detail_loaded = False
            try:
                r_node = s.get(f"https://funpay.com/chat/?node={node_id}", timeout=10)
                r_node.raise_for_status()
                # Restrict extraction to the most recent message wrapper. Looking at
                # the last .chat-msg-text alone can accidentally reuse an old link
                # when the buyer's latest message is an attachment.
                last_message_html = extract_last_chat_message_fragment(r_node.text) or ""
                if last_message_html:
                    full_text = html.unescape(re.sub(r"<[^>]+>", "", last_message_html).strip())
                    detail_loaded = True
                    if not full_text.strip() and re.search(
                        r"chat-msg-(?:file|attachment)|data-type=['\"]attachment['\"]",
                        last_message_html, re.IGNORECASE
                    ):
                        full_text = "Вложение в чате FunPay"
                else:
                    # A single text node is safe as a compatibility fallback. Multiple
                    # old text nodes without a recognized wrapper are ambiguous: retry
                    # rather than risking delivery to a link from an earlier message.
                    full_msgs = re.findall(r'<div class="chat-msg-text">([\s\S]*?)</div>', r_node.text)
                    if len(full_msgs) == 1:
                        last_message_html = full_msgs[0]
                        full_text = html.unescape(re.sub(r"<[^>]+>", "", last_message_html).strip())
                        detail_loaded = bool(full_text.strip())
            except Exception as exc:
                logging.warning("Не удалось прочитать детали чата FunPay #node%s: %s", node_id, type(exc).__name__)
                continue

            if not detail_loaded or not full_text.strip():
                logging.warning("Детали последнего сообщения FunPay #node%s пока недоступны; повторим проверку.", node_id)
                continue

            lower_txt = full_text.lower()
            is_order_payment = (
                is_funpay_system_message(last_message_html)
                and is_paid_order_notification(full_text)
            )
            sets_per_item, order_quantity = detect_order_sets_and_quantity(full_text)
            mentions_product = any(term in lower_txt for term in
                                    ("карт", "сет", "level", "уровень", "steam", "карточ"))

            chat_page_url = f"https://funpay.com/chat/?node={node_id}"
            trade_url = extract_steam_trade_url(full_text)
            contains_trade_url = "steamcommunity.com/tradeoffer/new" in lower_txt
            attachment_href = extract_text_attachment_href(last_message_html, chat_page_url)
            has_file = bool(attachment_href or re.search(
                r"(?:chat-msg-(?:file|attachment)|data-type=['\"]attachment['\"])",
                last_message_html, re.IGNORECASE
            ) or re.search(r"\b(?:файл|вложение|attachment)\b", msg_preview, re.IGNORECASE))
            is_buyer_input = bool(trade_url or contains_trade_url or attachment_href or has_file)

            # Buyer-provided links/files take precedence over payment wording.
            # "I paid, here's my link" must be processed as input, not a new order.
            if is_order_payment and not is_buyer_input and mentions_product and sets_per_item in (1, 5):
                record, created = register_funpay_payment(
                    node_id, sender_name, full_text, sets_per_item, order_quantity
                )
                if created:
                    order_no = record.get("funpay_order_number") or "не найден в тексте"
                    bot.send_message(
                        admin_id,
                        f"⚠️ Оплачен заказ FunPay.\n"
                        f"Заказ: {order_no}\n"
                        f"Покупатель: {sender_name}\n"
                        f"Лот: {sets_per_item} сет. × {order_quantity} шт.\n"
                        f"Резерв: {record.get('status')}.\n"
                        f"Ожидается Steam trade-ссылка в чате или текстовый файл."
                    )
            else:
                input_kind = "chat_trade_url"
                input_error = None
                if trade_url:
                    pass  # Prefer a valid URL sent directly in the FunPay chat.
                elif has_file:
                    if not attachment_href:
                        input_error = (
                            "В чате FunPay есть вложение, но не удалось безопасно определить ссылку на файл. "
                            "Поддерживаются небольшие текстовые файлы, прикреплённые непосредственно к чату."
                        )
                    else:
                        input_text, input_error = download_funpay_text_attachment(
                            s, attachment_href, chat_page_url
                        )
                        input_kind = "text_file"
                        if input_text:
                            trade_url = extract_steam_trade_url(input_text)
                            if not trade_url:
                                input_error = (
                                    "Текстовый файл прочитан, но в нём нет ровно одной корректной "
                                    "Steam trade-ссылки."
                                )
                elif contains_trade_url:
                    input_error = "В сообщении есть похожая на Steam trade-ссылку строка, но формат ссылки некорректен."

                if trade_url:
                    input_record, input_result, detail = record_funpay_buyer_input(
                        node_id, trade_url, input_kind
                    )
                    order_label = (
                        (input_record.get("funpay_order_number") or input_record.get("key"))
                        if input_record else f"чат #{node_id}"
                    )
                    if input_result == "offer_sent":
                        bot.send_message(
                            admin_id,
                            f"✅ FunPay: Steam-предложение создано. Заказ: {order_label}. "
                            f"Предложение Steam #{input_record.get('steam_trade_offer_id')}. "
                            f"Источник ссылки: {input_kind}. Наборы: {input_record.get('sets_total')}."
                        )
                    elif input_result == "offer_pending_confirmation":
                        bot.send_message(
                            admin_id,
                            f"⚠️ FunPay: предложение Steam #{input_record.get('steam_trade_offer_id')} создано "
                            f"для заказа {order_label}, но ASF не подтвердил автоматическое подтверждение. "
                            "Не отправляю повторно; проверь подтверждения ASF/Steam вручную."
                        )
                    elif input_result == "delivery_uncertain":
                        bot.send_message(
                            admin_id,
                            f"🚨 FunPay: неопределённый результат Steam для заказа {order_label}. "
                            "Повторная отправка заблокирована во избежание дубля. Проверь историю предложений Steam вручную."
                        )
                    elif input_result == "retry_required":
                        bot.send_message(
                            admin_id,
                            f"⚠️ FunPay: Steam не создал предложение для заказа {order_label}. "
                            f"Резерв сохранён. Причина: {detail} Отправь новую корректную ссылку/файл после проверки."
                        )
                    elif input_result == "no_stock":
                        bot.send_message(
                            admin_id,
                            f"🚨 FunPay: недостаточно доступных карточек для заказа {order_label}. "
                            f"Автообмен не выполнялся. {detail}"
                        )
                    elif input_result in {"ambiguous", "unmatched"}:
                        issue = "несколько активных заказов в чате" if input_result == "ambiguous" else "не найден подходящий оплаченный заказ"
                        bot.send_message(
                            admin_id,
                            f"⚠️ В чате FunPay #{node_id} получена trade-ссылка, но {issue}. "
                            "Автоматическая выдача остановлена; нужна ручная проверка."
                        )
                    elif input_result in {"in_progress", "duplicate"}:
                        pass
                    elif input_result == "uncertain":
                        bot.send_message(admin_id, f"🚨 FunPay: {order_label}: {detail}")
                    elif input_result == "disabled":
                        bot.send_message(admin_id, f"⚠️ FunPay: вход получен, но автовыдача отключена настройкой: {detail}")
                    elif input_result == "invalid_url":
                        bot.send_message(admin_id, f"⚠️ FunPay: ссылка из чата #{node_id} не прошла проверку: {detail}")
                elif input_error:
                    bot.send_message(
                        admin_id,
                        f"⚠️ Не удалось обработать вход FunPay в чате #{node_id}: {input_error} "
                        "Карточки автоматически не отправлялись."
                    )
                else:
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton("✍️ Ответить покупателю", callback_data=f"fpreply_{node_id}"))
                    safe_text = re.sub(
                        r"https?://steamcommunity\.com/tradeoffer/new/\?[^\s<]+",
                        "[трейд-ссылка скрыта]", full_text, flags=re.IGNORECASE
                    )
                    bot.send_message(
                        admin_id,
                        f"💬 Новое сообщение на FunPay\n"
                        f"От: {sender_name} (чат #node{node_id})\n\n"
                        f"Сообщение: {safe_text}\n\n"
                        "Нажми Reply на это сообщение или кнопку ниже.",
                        reply_markup=markup
                    )

            # Signature is persisted only after successful processing/notification.
            seen_msgs[node_id] = sig
            changed = True
            first_run = False

        if changed or first_run:
            cfg["seen_msgs"] = seen_msgs
            save_json(FP_CONFIG_FILE, cfg)
    except Exception:
        logging.exception("Ошибка проверки непрочитанных чатов FunPay")
def funpay_background_worker():
    # The owner explicitly approved this custom chat-based fulfillment workflow.
    # It does not activate FunPay's built-in "Automatic delivery" field or send external links.
    try:
        migrate_funpay_delivery_config()
        recover_interrupted_funpay_deliveries()
    except Exception:
        logging.exception("Не удалось инициализировать безопасный процесс выдачи FunPay")
    last_ping_time = 0
    while True:
        try:
            cfg = load_json(FP_CONFIG_FILE, {"golden_key": ""})
            if cfg.get("golden_key"):
                if time.time() - last_ping_time >= 180:
                    funpay_ping_and_raise(force_raise=False)
                    last_ping_time = time.time()
                funpay_check_unread_chats()
        except Exception:
            logging.exception("Ошибка фонового цикла FunPay")
        time.sleep(15)

# --- МОДУЛЬ 3: STEAM & ASF ---
def asf_command(cmd):
    try:
        r = requests.post(ASF_IPC_URL, json={"Command": cmd}, timeout=15)
        data = r.json()
        return data.get("Success", False), data.get("Result", "")
    except Exception as e:
        return False, f"Нет связи с ASF: {e}"

def get_steam_session(force_new=False):
    sess = load_json(SESSION_FILE, {})
    if not force_new and sess.get("steamLoginSecure") and time.time() - sess.get("timestamp", 0) < 43200:
        return sess["steamLoginSecure"], None

    try:
        r1 = requests.get(
            "https://api.steampowered.com/IAuthenticationService/GetPasswordRSAPublicKey/v1/",
            params={"input_json": json.dumps({"account_name": STEAM_LOGIN})},
            timeout=10
        ).json().get("response", {})

        mod = int(r1["publickey_mod"], 16)
        exp = int(r1["publickey_exp"], 16)
        rsa_timestamp = r1["timestamp"]

        pub_key = rsa.PublicKey(mod, exp)
        enc_password = base64.b64encode(rsa.encrypt(STEAM_PASSWORD.encode("utf-8"), pub_key)).decode("utf-8")

        begin_payload = {
            "account_name": STEAM_LOGIN,
            "encrypted_password": enc_password,
            "encryption_timestamp": rsa_timestamp,
            "remember_login": True,
            "platform_type": 2,
            "persistence": 1,
            "website_id": "Community"
        }
        r2 = requests.post(
            "https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1/",
            data={"input_json": json.dumps(begin_payload)},
            timeout=10
        ).json().get("response", {})

        client_id = r2.get("client_id")
        request_id = r2.get("request_id")
        steamid = r2.get("steamid", STEAM_ID)
        if not client_id:
            return None, "Steam отклонил автологин."

        ok, asf_res = asf_command(f"2fa {STEAM_LOGIN}")
        if not ok:
            return None, f"Не удалось получить 2FA из ASF: {asf_res}"

        match = re.search(r":\s*([A-Z0-9]{5})\b", asf_res)
        if not match:
            return None, f"Не найден код 2FA в ответе ASF: {asf_res}"
        twofa_code = match.group(1)

        update_payload = {"client_id": client_id, "steamid": steamid, "code": twofa_code, "code_type": 3}
        requests.post(
            "https://api.steampowered.com/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1/",
            data={"input_json": json.dumps(update_payload)},
            timeout=10
        )

        poll_payload = {"client_id": client_id, "request_id": request_id}
        r4 = requests.post(
            "https://api.steampowered.com/IAuthenticationService/PollAuthSessionStatus/v1/",
            data={"input_json": json.dumps(poll_payload)},
            timeout=10
        ).json().get("response", {})

        access_token = r4.get("access_token")
        if not access_token:
            return None, "Не удалось получить токен сессии Steam."

        cookie_val = f"{steamid}%7C%7C{access_token}"
        save_json(SESSION_FILE, {"steamLoginSecure": cookie_val, "timestamp": time.time()})
        return cookie_val, None
    except Exception as e:
        return None, f"Ошибка автологина: {e}"

def update_inventory_cache():
    steam_login_secure, _ = get_steam_session()
    session_id = "".join(random.choices("0123456789abcdef", k=24))
    cookies = {"sessionid": session_id, "Steam_Language": "russian"}
    if steam_login_secure:
        cookies["steamLoginSecure"] = steam_login_secure

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": f"https://steamcommunity.com/profiles/{STEAM_ID}/inventory/"
    }
    url = f"https://steamcommunity.com/inventory/{STEAM_ID}/753/6?l=russian&count=750"

    try:
        r = requests.get(url, headers=headers, cookies=cookies, timeout=20)
        if r.status_code == 429:
            return None, "Steam вернул ошибку 429. Подожди 2 минуты."
        data = r.json()
    except Exception as e:
        return None, f"Ошибка чтения инвентаря: {e}"

    if not data or not data.get("success"):
        return None, "Не удалось прочитать инвентарь."

    assets = data.get("assets", [])
    descriptions = {f"{d['classid']}_{d['instanceid']}": d for d in data.get("descriptions", [])}

    games = {}
    total_cards = 0
    for asset in assets:
        key = f"{asset['classid']}_{asset['instanceid']}"
        desc = descriptions.get(key, {})
        tags = desc.get("tags", [])
        is_card = any(t.get("internal_name") == "item_class_2" for t in tags)
        is_normal = any(t.get("internal_name") == "cardborder_0" for t in tags)
        if not (is_card and is_normal):
            continue
        total_cards += 1
        game_name = "Неизвестная игра"
        for t in tags:
            if t.get("category") == "Game":
                game_name = t.get("localized_tag_name", "Неизвестная игра")
                break
        card_name = desc.get("market_hash_name", desc.get("name", "Card"))
        games.setdefault(game_name, {}).setdefault(card_name, []).append(str(asset["assetid"]))

    cache_data = {"updated_at": time.time(), "total_cards": total_cards, "games": games}
    # Steam keeps items in inventory while a trade offer is pending. Exclude every
    # asset held by a pending/uncertain offer so the next refresh cannot sell it twice.
    with funpay_orders_lock:
        order_data = load_funpay_orders()
        db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
        with delivery_lock:
            cache_data = apply_reserved_assets_to_cache(
                cache_data,
                orders=order_data.get("orders", {}),
                vouchers=db.get("vouchers", {}),
            )
            save_json(CACHE_FILE, cache_data)
    return cache_data, None

def count_available_sets(cache_data):
    games = cache_data.get("games", {})
    total_sets = 0
    details = []
    for game_name, cards_dict in sorted(games.items(), key=lambda x: len(x[1])):
        unique_count = len(cards_dict)
        if unique_count >= 5:
            sets_in_game = min(len(aids) for aids in cards_dict.values())
            if sets_in_game > 0:
                total_sets += sets_in_game
                details.append((game_name, unique_count, sets_in_game))
    return total_sets, details

def pick_cards_for_sets(sets_needed):
    cache_data = load_json(CACHE_FILE, {})
    # Refresh the exclusion set on every selection. Items can remain in Steam's
    # inventory while a trade offer is pending and must not be offered twice.
    cache_data = apply_reserved_assets_to_cache(cache_data)
    games = cache_data.get("games", {})
    if not games:
        return None, None, "Кэш пуст! Нажми «🔄 Обновить кэш склада»."
    freshness_error = inventory_cache_freshness_error(cache_data)
    if freshness_error:
        return None, None, freshness_error

    selected_assets = []
    asset_locations = []
    picked_games_info = []
    for game_name, cards_dict in sorted(games.items(), key=lambda x: len(x[1])):
        unique_count = len(cards_dict)
        if unique_count < 5:
            continue
        available_here = min(len(aids) for aids in cards_dict.values())
        take_sets = min(available_here, sets_needed)
        if take_sets > 0:
            for _ in range(take_sets):
                for card_name in cards_dict:
                    asset_id = cards_dict[card_name].pop(0)
                    selected_assets.append(asset_id)
                    asset_locations.append({
                        "game": game_name,
                        "card": card_name,
                        "asset_id": str(asset_id),
                    })
            picked_games_info.append(f"{game_name} ({take_sets} сет. по {unique_count} карт)")
            sets_needed -= take_sets
        if sets_needed <= 0:
            break

    if sets_needed > 0:
        return None, None, "На складе временно закончились полные сеты."
    return selected_assets, (cache_data, picked_games_info, asset_locations), None

def send_trade_offer(trade_url, asset_ids, code):
    """Create one Steam trade offer and distinguish definite failure from uncertain outcome."""
    normalized_url = extract_steam_trade_url(trade_url)
    if not normalized_url:
        return "failed", "Неверная Steam trade-ссылка (требуются partner и token).", None
    parsed = urllib.parse.urlparse(normalized_url)
    params = urllib.parse.parse_qs(parsed.query)
    partner_id = params.get("partner", [None])[0]
    token = params.get("token", [None])[0]
    if not partner_id or not partner_id.isdigit() or not token:
        return "failed", "Неверная Steam trade-ссылка (отсутствует partner или token).", None

    asset_ids = list(dict.fromkeys(str(value) for value in asset_ids if str(value).isdigit()))
    if not asset_ids:
        return "failed", "Не выбраны карточки для обмена.", None

    partner_steamid64 = str(int(partner_id) + 76561197960265728)
    steam_login_secure, err = get_steam_session()
    if err or not steam_login_secure:
        return "failed", err or "Steam-сессия недоступна.", None

    session_id = "".join(random.choices("0123456789abcdef", k=24))
    cookies = {"sessionid": session_id, "steamLoginSecure": steam_login_secure}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        "Referer": normalized_url,
        "Origin": "https://steamcommunity.com"
    }
    assets_list = [
        {"appid": 753, "contextid": "6", "amount": 1, "assetid": aid}
        for aid in asset_ids
    ]
    tradeoffer_json = {
        "newversion": True,
        "version": 2,
        "me": {"assets": assets_list, "currency": [], "ready": False},
        "them": {"assets": [], "currency": [], "ready": False}
    }
    form_data = {
        "sessionid": session_id,
        "serverid": "1",
        "partner": partner_steamid64,
        "tradeoffermessage": f"Автовыдача по заказу {str(code)[:80]}. Спасибо за покупку!",
        "json_tradeoffer": json.dumps(tradeoffer_json),
        "captcha": "",
        "trade_offer_create_params": json.dumps({"trade_offer_access_token": token})
    }

    try:
        response = requests.post(
            "https://steamcommunity.com/tradeoffer/new/send",
            data=form_data, cookies=cookies, headers=headers, timeout=20
        )
    except requests.RequestException as exc:
        # A read timeout can happen after Steam has already created the offer.
        # Quarantine the items and do not automatically repeat the POST.
        return "unknown", f"Связь со Steam прервалась ({type(exc).__name__}); результат неясен.", None

    if response.status_code >= 500:
        return "unknown", f"Steam вернул HTTP {response.status_code}; результат создания предложения неясен.", None
    try:
        res_json = response.json()
    except (ValueError, requests.RequestException):
        if response.status_code >= 400:
            return "failed", f"Steam отклонил запрос (HTTP {response.status_code}).", None
        return "unknown", "Steam вернул непонятный ответ; результат создания предложения неясен.", None

    if response.status_code >= 400 or not isinstance(res_json, dict):
        return "failed", f"Steam отклонил запрос (HTTP {response.status_code}).", None
    if "tradeofferid" not in res_json:
        error_text = str(res_json.get("strError") or "Steam не вернул ID предложения")[:240]
        return "failed", f"Steam отклонил обмен: {error_text}", None

    offer_id = str(res_json["tradeofferid"])
    time.sleep(2)
    confirmation_ok, confirmation_message = asf_command(f"2faok {STEAM_LOGIN}")
    if confirmation_ok:
        return "sent", f"Предложение Steam #{offer_id} создано; ASF подтвердил запрос подтверждения.", offer_id
    return (
        "created_unconfirmed",
        f"Предложение Steam #{offer_id} уже создано, но ASF не подтвердил автоматическое подтверждение: {str(confirmation_message)[:180]}. Повторно не отправлять.",
        offer_id,
    )


def execute_delivery(chat_id, code, trade_url, db):
    # Serialize a delivery transaction and re-read the code under the lock.
    with delivery_lock:
        current_db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
        voucher = current_db.get("vouchers", {}).get(code)
        if not isinstance(voucher, dict) or voucher.get("used"):
            bot.send_message(chat_id, "❌ Этот пин-код не найден или уже был активирован ранее!")
            return False
        redemption_block = voucher_redemption_block_reason(voucher)
        if redemption_block:
            bot.send_message(
                chat_id,
                f"⚠️ Выдача кода заблокирована: {redemption_block}"
            )
            return False
        return _execute_delivery_locked(chat_id, code, trade_url, current_db)


def _execute_delivery_locked(chat_id, code, trade_url, db):
    voucher = db["vouchers"][code]
    sets_needed = voucher["sets"]
    platform_tag = voucher.get("platform", "funpay").upper()
    bot.send_message(chat_id, f"Код {code} проверен! Собираю {sets_needed} полных сет(ов) и отправляю трейд...")

    selected_assets, cache_bundle, err = pick_cards_for_sets(sets_needed)
    if err:
        bot.send_message(chat_id, f"❌ {err}")
        return False

    updated_cache, picked_games_info, _asset_locations = cache_bundle
    # Persist the exact assets before the network request so a timeout/restart
    # cannot make them appear available for another order.
    voucher["delivery_state"] = "sending"
    voucher["reserved_asset_ids"] = list(selected_assets)
    save_json(DB_FILE, db)

    outcome, result_msg, offer_id = send_trade_offer(trade_url, selected_assets, code)
    if outcome == "unknown":
        voucher["delivery_state"] = "uncertain"
        voucher["delivery_error"] = result_msg[:300]
        save_json(DB_FILE, db)
        bot.send_message(
            chat_id,
            "⚠️ Запрос к Steam завершился с неопределённым результатом. Код временно заблокирован, "
            "чтобы не отправить обмен дважды. Администратор должен проверить историю предложений Steam."
        )
        pending_customer_codes.pop(chat_id, None)
        return False

    if outcome == "failed":
        voucher["delivery_state"] = "failed"
        voucher.pop("reserved_asset_ids", None)
        voucher["delivery_error"] = result_msg[:300]
        save_json(DB_FILE, db)
        bot.send_message(
            chat_id,
            f"⚠️ Steam не создал предложение (код не использован):\n{result_msg}\n\n"
            "Проверьте настройки приватности инвентаря Steam и отправьте корректную трейд-ссылку ещё раз."
        )
        pending_customer_codes[chat_id] = code
        return False

    voucher["used"] = True
    voucher["used_at"] = int(time.time())
    voucher["trade_offer_id"] = str(offer_id)
    voucher["delivery_state"] = outcome
    voucher.pop("delivery_error", None)
    save_json(DB_FILE, db)
    apply_reserved_assets_to_cache(updated_cache, vouchers=db.get("vouchers", {}))
    save_json(CACHE_FILE, updated_cache)
    pending_customer_codes.pop(chat_id, None)

    games_str = ", ".join(picked_games_info)
    if outcome == "sent":
        customer_text = (
            f"✅ Предложение Steam #{offer_id} создано и подтверждение ASF обработано.\n"
            f"📦 Наборы: {games_str} (карточек: {len(selected_assets)} шт.)\n\n"
            "Откройте Steam и проверьте входящее предложение."
        )
    else:
        customer_text = (
            f"⚠️ Предложение Steam #{offer_id} создано, но автоматическое подтверждение не завершилось.\n"
            f"📦 Наборы: {games_str} (карточек: {len(selected_assets)} шт.)\n\n"
            "Не отправляйте код повторно. Проверьте Steam и при необходимости обратитесь к продавцу."
        )
    bot.send_message(chat_id, customer_text)

    if db.get("admin_id"):
        rem_sets, _ = count_available_sets(updated_cache)
        stock_warning = ""
        if rem_sets < 5:
            stock_warning = "\n🚨 Критический остаток: меньше 5 полных сетов. Обнови склад и проверь запас."
        elif rem_sets < 10:
            stock_warning = "\n⚠️ Низкий остаток: меньше 10 полных сетов. Проверь запас."
        label = "создано" if outcome == "sent" else "создано, подтверждение требует проверки"
        bot.send_message(
            db["admin_id"],
            f"💰 Предложение Steam {label} ({platform_tag})!\n"
            f"Код: {code}\n"
            f"ID предложения: {offer_id}\n"
            f"Отправлено: {games_str}\n"
            f"Остаток доступных сетов: {rem_sets}" + stock_warning
        )
    return True


# --- КЛАВИАТУРЫ ---

def build_sales_analytics_text():
    """Show stock health and code/offer counts without guessing revenue."""
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    cache = load_json(CACHE_FILE, {})
    total_sets, _ = count_available_sets(cache) if cache else (0, [])
    summary = summarize_sales(
        db.get("vouchers", {}),
        total_sets,
        cache.get("updated_at") if isinstance(cache, dict) else None,
    )

    lines = ["📈 **ПРОДАЖИ И СОСТОЯНИЕ СКЛАДА**"]
    lines.append(f"📦 Полных сетов в кэше: **{summary['stock_sets']}**")
    updated_at = cache.get("updated_at") if isinstance(cache, dict) else None
    if updated_at:
        lines.append(f"🕒 Кэш обновлён: {time.strftime('%d.%m.%Y %H:%M', time.localtime(float(updated_at)))}")
    else:
        lines.append("🕒 Время обновления кэша неизвестно.")

    if summary["cache_stale"]:
        if summary["cache_age_seconds"] is None:
            lines.append("⚠️ **Кэш не имеет времени обновления. Обнови склад перед выдачей.**")
        else:
            age_hours = summary["cache_age_seconds"] / 3600
            lines.append(f"⚠️ **Кэш старше 6 часов ({age_hours:.1f} ч). Обнови склад перед выдачей.**")
    else:
        age_minutes = int(summary["cache_age_seconds"] // 60)
        lines.append(f"✅ Возраст кэша: {age_minutes} мин.")

    if summary["stock_state"] == "critical":
        lines.append("🚨 **Критический остаток:** меньше 5 полных сетов.")
    elif summary["stock_state"] == "low":
        lines.append("⚠️ **Низкий остаток:** меньше 10 полных сетов.")

    titles = {"funpay": "🟢 FunPay", "playerok": "🔵 Playerok"}
    for platform in ("funpay", "playerok"):
        stats = summary["by_platform"][platform]
        lines.extend([
            "",
            f"**{titles[platform]}**",
            f"• Всего создано кодов: **{stats['generated_codes']}** (сегодня: **{stats['generated_today']}**)",
            f"• Неиспользованные коды: **{stats['unused_codes']}** — по 1 сету: **{stats['unused_1_set']}**, по 5 сетов: **{stats['unused_5_sets']}**",
            f"• Отправлено предложений Steam: **{stats['offers_sent']}**; сетов в предложениях: **{stats['sets_sent']}**",
            f"• Сегодня по новым временным отметкам: **{stats['offers_sent_today']}** предложений / **{stats['sets_sent_today']}** сетов",
        ])

    lines.extend([
        "",
        "ℹ️ «Отправлено предложений» означает, что Steam вернул ID предложения; это не подтверждает, что покупатель его принял.",
        "📊 Доход и прибыль не рассчитываются: цены, комиссии и возвраты пока не записываются.",
        "🗓 Дневная статистика точна только для кодов с временными отметками этой версии; старые коды остаются в общих итогах.",
    ])
    return "\n".join(lines)


def _get_playerok_monitor_status():
    if playerok_monitor is None:
        return {
            "state": "not_configured" if not os.getenv("PLAYEROK_TOKEN", "").strip() else "stopped",
            "username": None,
            "last_event_at": None,
            "last_error": None,
            "configured": bool(os.getenv("PLAYEROK_TOKEN", "").strip()),
            "one_set_mapping_ready": bool(os.getenv("PLAYEROK_ITEM_1_SET_ID", "").strip()),
            "five_set_mapping_ready": bool(os.getenv("PLAYEROK_ITEM_5_SET_ID", "").strip()),
            "mappings_distinct": bool(os.getenv("PLAYEROK_ITEM_1_SET_ID", "").strip() and os.getenv("PLAYEROK_ITEM_5_SET_ID", "").strip() and os.getenv("PLAYEROK_ITEM_1_SET_ID") != os.getenv("PLAYEROK_ITEM_5_SET_ID")),
            "auto_delivery": False,
        }
    return playerok_monitor.status()


def build_playerok_dashboard_text(show_orders=False):
    state = _get_playerok_monitor_status()
    try:
        with playerok_orders_lock:
            ledger = load_playerok_orders(PLAYEROK_ORDERS_FILE)
            orders = ledger["orders"]
    except Exception as exc:
        logging.error("Playerok ledger could not be read (%s)", type(exc).__name__)
        orders = {}
        ledger_error = True
    else:
        ledger_error = False
    stats = summarize_orders(orders)
    state_labels = {
        "not_configured": "Не настроен",
        "stopped": "Остановлен",
        "connecting": "Подключение…",
        "listening": "Авторизация прошла; слушатель запущен",
        "missing_dependency": "Не установлена дополнительная библиотека",
        "error": "Ошибка подключения",
    }
    lines = ["<b>🔵 PLAYEROK — ПАНЕЛЬ ПРОДАВЦА</b>"]
    lines.append(f"Подключение: <b>{html.escape(state_labels.get(state.get('state'), str(state.get('state', 'неизвестно'))))}</b>")
    if state.get("username"):
        lines.append(f"Аккаунт: <code>{html.escape(str(state['username']))}</code>")
    if state.get("last_error"):
        lines.append(f"Последняя ошибка: <code>{html.escape(str(state['last_error']))}</code>")
    if state.get("last_event_at"):
        lines.append("Последнее событие: <code>" + time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(state["last_event_at"])) + "</code>")
    else:
        lines.append("Последнее событие: пока не получено")
    if state.get("last_sync_at"):
        lines.append(
            "Последняя сверка открытых сделок: <code>"
            + time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(state["last_sync_at"]))
            + f"</code> — просмотрено: <b>{int(state.get('last_sync_count') or 0)}</b>"
        )
    if state.get("last_sync_error"):
        lines.append(
            "⚠️ Ошибка первой сверки: <code>"
            + html.escape(str(state["last_sync_error"]))
            + "</code>. Поток событий продолжает работать."
        )
    lines.append(f"Лот на 1 сет: {'✅ ID задан' if state.get('one_set_mapping_ready') else '⚠️ ID не задан'}")
    lines.append(f"Лот на 5 сетов: {'✅ ID задан' if state.get('five_set_mapping_ready') else '⚠️ ID не задан'}")
    if state.get("one_set_mapping_ready") and state.get("five_set_mapping_ready") and not state.get("mappings_distinct"):
        lines.append("🚨 <b>Оба лота имеют одинаковый ID. Заказы будут требовать ручной проверки.</b>")
    lines.extend([
        "",
        "<b>Сделки в локальном журнале</b>",
        f"Всего: <b>{stats['total']}</b> | Оплачено, ожидает данных: <b>{stats['paid']}</b>",
        f"Ожидают трейд-ссылку: <b>{stats['awaiting_trade_url']}</b>",
        f"Трейд-ссылка замечена: <b>{stats['trade_url_received']}</b>",
        f"Отправлено по статусу площадки: <b>{stats['sent']}</b> | Подтверждено: <b>{stats['confirmed']}</b>",
        f"Проблемы: <b>{stats['problem']}</b> | Возвраты: <b>{stats['rolled_back']}</b>",
        f"Ручная проверка: <b>{stats['needs_review']}</b>",
        "",
        "🚦 Автовыдача и изменение статусов Playerok: <b>ВЫКЛЮЧЕНЫ</b>",
        "ℹ️ Журнал не сохраняет сами трейд-ссылки или текст сообщений.",
    ])
    if ledger_error:
        lines.append("⚠️ Не удалось прочитать журнал. Файл не перезаписывался.")
    if show_orders:
        latest = sorted(orders.values(), key=lambda item: item.get("updated_at") or 0, reverse=True)[:8]
        lines.extend(["", "<b>Последние сделки</b>"])
        for record in latest:
            item_name = html.escape(str(record.get("item_name") or "Лот не определён"))
            deal_id = html.escape(str(record.get("deal_id") or "без ID"))
            status = html.escape(str(record.get("platform_status") or "unknown"))
            fulfillment = html.escape(str(record.get("fulfillment_status") or "unknown"))
            sets = record.get("sets")
            lines.append(f"• <code>{deal_id}</code> — {sets if sets in (1, 5) else '?'} сет. — {status}/{fulfillment} — {item_name}")
        if not latest:
            lines.append("Пока сделок нет.")
    return "\n".join(lines)


def build_playerok_dashboard_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔄 Обновить", callback_data="podash_refresh"),
        types.InlineKeyboardButton("📋 Последние сделки", callback_data="podash_orders"),
    )
    return markup


def handle_playerok_event(event):
    """Persist a redacted Playerok event and notify only the configured admin."""
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    admin_id = db.get("admin_id")
    event_type = str(event.get("event_type") or "UNKNOWN")
    deal_id = event.get("deal_id")
    trade_link_result = None
    record = None
    created = False
    changed = False
    try:
        with playerok_orders_lock:
            ledger = load_playerok_orders(PLAYEROK_ORDERS_FILE)
            orders = ledger["orders"]
            if deal_id:
                record, created, changed = upsert_deal(
                    orders,
                    deal_id=deal_id,
                    status=event.get("status") or event_type,
                    item_id=event.get("item_id"),
                    item_name=event.get("item_name"),
                    chat_id=event.get("chat_id"),
                    sets=event.get("sets"),
                    event_type=event_type,
                )
            if event.get("has_trade_url") and event.get("chat_id"):
                matched, trade_link_result = mark_trade_url_received(orders, chat_id=event["chat_id"])
                if matched is not None:
                    record = matched
                    changed = changed or trade_link_result == "recorded"
            if created or changed:
                save_playerok_orders(ledger, PLAYEROK_ORDERS_FILE)
    except Exception as exc:
        logging.error("Playerok event processing failed (%s)", type(exc).__name__)
        if admin_id:
            bot.send_message(admin_id, "⚠️ Не удалось записать событие Playerok в журнал. Автоматических действий не выполнялось.")
        return

    if not admin_id:
        return
    if created and not event.get("is_initial_sync"):
        sets_text = str(record.get("sets")) if record and record.get("sets") in (1, 5) else "не определено"
        item_name = html.escape(str(record.get("item_name") or "лот не сопоставлен")) if record else "лот не сопоставлен"
        review = " Требуется ручная проверка ID лота." if not record or record.get("sets") not in (1, 5) else ""
        bot.send_message(
            admin_id,
            f"🛒 <b>Новая сделка Playerok</b>\nID: <code>{html.escape(str(deal_id))}</code>\nЛот: {item_name}\nКоличество: {sets_text} сет.\nСтатус: <code>{html.escape(str(record.get('platform_status', 'unknown')))}</code>.\nАвтовыдача выключена.{review}",
            parse_mode="HTML",
        )
    elif changed and record and record.get("platform_status") in {"sent", "confirmed", "rolled_back", "problem"}:
        bot.send_message(
            admin_id,
            f"🔔 <b>Изменение сделки Playerok</b>\nID: <code>{html.escape(str(record.get('deal_id')))}</code>\nСтатус: <code>{html.escape(str(record.get('platform_status')))}</code>\nАвтовыдача остаётся выключенной.",
            parse_mode="HTML",
        )
    if event.get("has_trade_url") and trade_link_result:
        if trade_link_result == "recorded":
            bot.send_message(
                admin_id,
                f"📥 В чате Playerok замечена трейд-ссылка.\nСделка: <code>{html.escape(str(record.get('deal_id') if record else 'не сопоставлена'))}</code>. Сама ссылка не сохранялась. Проверь сделку вручную; автовыдача выключена.",
                parse_mode="HTML",
            )
        elif trade_link_result == "ambiguous":
            bot.send_message(admin_id, "⚠️ Трейд-ссылка в Playerok получена в чате с несколькими открытыми сделками. Автоматическое сопоставление запрещено; нужна ручная проверка.")
        elif trade_link_result == "unmatched":
            bot.send_message(admin_id, "⚠️ В Playerok замечена трейд-ссылка, но подходящая открытая сделка не найдена. Нужна ручная проверка.")


def handle_playerok_monitor_status(status):
    state = status.get("state")
    if state not in {"listening", "missing_dependency", "error"}:
        return
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    admin_id = db.get("admin_id")
    if not admin_id:
        return
    messages = {
        "listening": (
            "🟢 Playerok: аккаунт авторизован, слушатель событий запущен. "
            f"При запуске просмотрено открытых сделок: {int(status.get('last_sync_count') or 0)}. "
            + (f"Сверка не завершилась: {status.get('last_sync_error')}. " if status.get("last_sync_error") else "")
            + "Сделки только наблюдаются; автовыдача выключена."
        ),
        "missing_dependency": "⚠️ Playerok: не установлена дополнительная библиотека. Запусти configure_playerok.bat только после прочтения предупреждения о неофициальном API.",
        "error": f"⚠️ Playerok: подключение остановилось ({html.escape(str(status.get('last_error') or 'неизвестная ошибка'))}). Проверь статус вручную; автоматических действий не выполнялось.",
    }
    try:
        bot.send_message(admin_id, messages[state], parse_mode="HTML")
    except Exception:
        logging.exception("Could not notify admin about Playerok monitor status")


def start_playerok_monitor():
    global playerok_monitor
    playerok_monitor = PlayerokMonitor(
        token=os.getenv("PLAYEROK_TOKEN", ""),
        user_agent=os.getenv("PLAYEROK_USER_AGENT", ""),
        one_set_item_id=os.getenv("PLAYEROK_ITEM_1_SET_ID", ""),
        five_set_item_id=os.getenv("PLAYEROK_ITEM_5_SET_ID", ""),
        on_event=handle_playerok_event,
        on_status=handle_playerok_monitor_status,
    )
    if os.getenv("PLAYEROK_TOKEN", "").strip():
        playerok_monitor.start()
    else:
        logging.info("Playerok monitor not configured; no Playerok account was accessed.")

ADMIN_MAIN_MENU = "🏠 Главное меню"
ADMIN_BACK_MENU = "⬅️ Назад"
admin_menu_state = {}

def build_admin_keyboard():
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    admin_id = db.get("admin_id")
    section = admin_menu_state.get(admin_id, "main")
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)

    if section == "stock":
        markup.add("🔄 Обновить кэш склада", "📊 Остатки в кэше")
        markup.add("🟢 Коды FunPay (1 сет)", "🟢 Коды FunPay (5 сетов)")
        markup.add("🔵 Коды Playerok (1 сет)", "🔵 Коды Playerok (5 сетов)")
        markup.add(ADMIN_BACK_MENU)
    elif section == "funpay":
        markup.add("📋 Панель FunPay — сводка", "📋 Последние заказы FunPay")
        markup.add("🔄 Проверить соединение FunPay", ADMIN_BACK_MENU)
    elif section == "playerok":
        markup.add("🔵 Playerok — панель", "📋 Последние сделки Playerok")
        markup.add(ADMIN_BACK_MENU)
    elif section == "sales":
        markup.add("📈 Продажи и склад", "📊 Остатки в кэше")
        markup.add("🔄 Обновить кэш склада", ADMIN_BACK_MENU)
    elif section == "settings":
        auto_label = "⚙️ Автозапуск Windows: ВКЛ 🟢" if is_autostart_enabled() else "⚙️ Автозапуск Windows: ВЫКЛ 🔴"
        markup.add(auto_label)
        markup.add("🔄 Проверить состояние бота", ADMIN_BACK_MENU)
    else:
        markup.add("📦 Склад и коды", "🟢 FunPay")
        markup.add("🔵 Playerok", "📈 Продажи и аналитика")
        markup.add("⚙️ Настройки")
    return markup

def build_customer_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔗 Где взять трейд-ссылку?", "❓ Как получить карточки?")
    return markup

def get_tradeurl_help_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🌐 Открыть мою трейд-ссылку в Steam", url="https://steamcommunity.com/id/me/tradeoffers/privacy#trade_offer_access_url"))
    return markup

@bot.message_handler(commands=["start"])
def start_cmd(message):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if db["admin_id"] is None:
        db["admin_id"] = message.chat.id
        save_json(DB_FILE, db)

    if message.chat.id == db["admin_id"]:
        bot.send_message(
            message.chat.id,
            f"👋 **Комбайн v5.2 MULTI-MARKET (Изолированные базы FunPay / Playerok) запущен!**\n"
            f"🤖 Бот для покупателей: `@{BOT_USERNAME}`\n\n"
            f"• Коды **FunPay** (🟢) и **Playerok** (🔵) теперь разделены и никогда не пересекаются.\n"
            f"• Чтобы сбросить тестовый пин-код: `/reset КОД`",
            reply_markup=build_admin_keyboard(),
            parse_mode="Markdown"
        )
    else:
        bot.send_message(
            message.chat.id,
            "👋 **Добро пожаловать в магазин автоматической выдачи карточек Steam 24/7!**\n\n"
            "📦 **Чтобы получить ваши карточки за 10 секунд:**\n"
            "1️⃣ Отправьте сюда ваш **Пин-код** (начинается на `SET...`), полученный после оплаты.\n"
            "2️⃣ Отправьте вашу **Трейд-ссылку Steam** (вида `.../tradeoffer/new/?partner=...&token=...`).\n\n"
            "⚠️ *Внимание: обычная ссылка на профиль (`/profiles/...`) не подходит — нужна именно ссылка на обмен!*",
            reply_markup=build_customer_keyboard(),
            parse_mode="Markdown"
        )

@bot.message_handler(commands=["reset"])
def reset_voucher_cmd(message):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db.get("admin_id"):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: `/reset SET1-XXXXXX`", parse_mode="Markdown")
        return
    code = parts[1].strip().upper()
    voucher = db.get("vouchers", {}).get(code)
    if not isinstance(voucher, dict):
        bot.send_message(message.chat.id, f"❌ Код `{code}` не найден в базе.", parse_mode="Markdown")
        return

    reset_block = voucher_reset_block_reason(voucher)
    if reset_block:
        bot.send_message(
            message.chat.id,
            f"⛔ Код `{code}` не сброшен. {reset_block}",
            parse_mode="Markdown",
        )
        return

    voucher["used"] = False
    voucher["sent_to_fp"] = False
    voucher.pop("used_at", None)
    voucher.pop("delivery_error", None)
    if voucher.get("delivery_state") == "failed":
        voucher.pop("delivery_state", None)
        voucher.pop("reserved_asset_ids", None)
    save_json(DB_FILE, db)
    bot.send_message(message.chat.id, f"✅ Код `{code}` сброшен. Проверки повторной выдачи сохранены.", parse_mode="Markdown")


@bot.message_handler(commands=["fpcoldstart"])
def funpay_cold_start_cmd(message):
    """Safely archive and clear only the FunPay order ledger after confirmation."""
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db.get("admin_id") or message.from_user.id != db.get("admin_id"):
        return

    parts = (message.text or "").strip().split(maxsplit=1)
    with funpay_orders_lock:
        data = load_funpay_orders()
        orders = data.get("orders", {})
        if len(parts) < 2 or parts[1].strip().upper() != "CONFIRM":
            counts = {}
            for item in orders.values():
                if isinstance(item, dict):
                    status = str(item.get("status", "unknown"))
                    counts[status] = counts.get(status, 0) + 1
            summary = ", ".join(f"{status}: {count}" for status, count in sorted(counts.items())) or "нет записей"
            bot.send_message(
                message.chat.id,
                "🧹 <b>Подготовка к чистому старту FunPay</b>\n"
                f"Записей в журнале: {len(orders)}\nСтатусы: {html.escape(summary)}\n\n"
                "Команда удалит только журнал заказов FunPay после создания резервной копии. "
                "Склад, кэш карточек, коды, настройки и Playerok не изменятся.\n"
                "Если заказов действительно нет и ты хочешь продолжить, отправь: "
                "<code>/fpcoldstart CONFIRM</code>",
                parse_mode="HTML",
            )
            return

        blockers = funpay_cold_start_blockers(orders)
        if blockers:
            preview = ", ".join(
                f"{key} ({status})" for key, status in blockers[:5]
            )
            more = f" и ещё {len(blockers) - 5}" if len(blockers) > 5 else ""
            bot.send_message(
                message.chat.id,
                "❌ Журнал FunPay не очищен: есть незавершённые заказы или активные резервы. "
                "Очистка потеряла бы защиту от повторной продажи тех же карточек.\n"
                f"Заказы: {html.escape(preview + more)}\n"
                "Сначала завершите заказ или выполните /fprelease только если Steam-предложение точно не создавалось. "
                "Для отправленного/неопределённого предложения сначала проверьте Steam и используйте "
                "/fpofferresolve КЛЮЧ accepted|returned.\n"
                "После завершения всех заказов команда /fpcoldstart CONFIRM снова станет доступна."
            )
            return

        if not orders:
            bot.send_message(message.chat.id, "Журнал FunPay уже пуст. Склад и остальные данные не менялись.")
            return

        import shutil
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{FP_ORDERS_FILE}.backup_{stamp}.json"
        shutil.copy2(FP_ORDERS_FILE, backup_path)
        save_json(FP_ORDERS_FILE, {"orders": {}})
        logging.warning("Admin requested FunPay cold start; archived %s orders to %s", len(orders), backup_path)

    bot.send_message(
        message.chat.id,
        "✅ Журнал заказов FunPay очищен. Резервная копия сохранена: "
        f"<code>{html.escape(backup_path)}</code>\n"
        "Кэш склада, карточки, коды, настройки и данные Playerok не затронуты.",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["fporders"])
def funpay_orders_cmd(message):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db.get("admin_id"):
        return
    with funpay_orders_lock:
        orders = list(load_funpay_orders()["orders"].values())
    if not orders:
        bot.send_message(message.chat.id, "📋 Журнал FunPay пока пуст.")
        return
    orders.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
    lines = ["📋 **Последние заказы FunPay**"]
    for item in orders[:10]:
        number = item.get("funpay_order_number") or item.get("key")
        lines.append(
            f"• `{number}` — {item.get('sets_total')} сет., "
            f"{item.get('status')}, данные: {item.get('input_kind') or 'нет'}\n"
            f"  ключ: `{item.get('key')}`"
        )
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")


@bot.message_handler(commands=["fpofferresolve"])
def funpay_offer_resolve_cmd(message):
    """Manually reconcile an offer only after its real status was checked in Steam."""
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db.get("admin_id") or message.from_user.id != db.get("admin_id"):
        return
    parts = (message.text or "").strip().split()
    if len(parts) != 3 or parts[2].lower() not in {"accepted", "returned"}:
        bot.send_message(
            message.chat.id,
            "Использование: /fpofferresolve КЛЮЧ accepted|returned\n"
            "Команда допустима только после ручной проверки истории предложения в Steam."
        )
        return

    key, resolution = parts[1], parts[2].lower()
    with funpay_orders_lock:
        data = load_funpay_orders()
        record = data["orders"].get(key)
        if not isinstance(record, dict):
            bot.send_message(message.chat.id, "❌ Заказ не найден.")
            return
        if record.get("status") not in {
            "offer_sent", "offer_pending_confirmation", "delivery_uncertain", "sending"
        }:
            bot.send_message(message.chat.id, "❌ У заказа нет предложения, которое нужно сверить.")
            return
        if resolution == "accepted":
            record["reservation"] = None
            record["status"] = "completed"
            record["resolved_at"] = int(time.time())
            record.setdefault("notes", []).append(
                "Администратор подтвердил принятие предложения в Steam."
            )
            result_text = "✅ Заказ отмечен завершённым. Карточки обратно в склад не возвращались."
        else:
            reservation = record.get("reservation")
            if not isinstance(reservation, dict) or not reservation.get("asset_locations"):
                bot.send_message(
                    message.chat.id,
                    "❌ У заказа нет полного резерва. Автоматическое восстановление невозможно; нужна ручная сверка склада."
                )
                return
            restored = restore_reservation_to_cache(reservation)
            if not restored:
                bot.send_message(message.chat.id, "❌ Не удалось восстановить резерв; данные заказа не изменены.")
                return
            record["reservation"] = None
            record.pop("steam_trade_offer_id", None)
            record["steam_offer_confirmed"] = False
            record["status"] = "retry_required"
            record["resolved_at"] = int(time.time())
            record.setdefault("notes", []).append(
                "Администратор подтвердил, что предложение отменено/отклонено/истекло и карточки вернулись в инвентарь."
            )
            result_text = "✅ Резерв восстановлен. Заказ ждёт новую trade-ссылку/текстовый файл."
        record["updated_at"] = int(time.time())
        save_json(FP_ORDERS_FILE, data)
    bot.send_message(message.chat.id, result_text)


@bot.message_handler(commands=["fprelease"])
def funpay_release_cmd(message):
    """Release an undelivered reservation by its journal key, with an audit note."""
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db.get("admin_id"):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Использование: `/fprelease КЛЮЧ_ИЗ_fporders`", parse_mode="Markdown")
        return
    key = parts[1].strip()
    with funpay_orders_lock:
        data = load_funpay_orders()
        record = data["orders"].get(key)
        if not isinstance(record, dict):
            bot.send_message(message.chat.id, "❌ Заказ не найден.")
            return
        if record.get("status") in {"sending", "delivery_uncertain", "offer_sent", "offer_pending_confirmation", "completed"} or record.get("steam_trade_offer_id"):
            bot.send_message(
                message.chat.id,
                "❌ У заказа есть или могло быть создано предложение Steam. Используй /fpofferresolve КЛЮЧ accepted|returned после проверки истории Steam."
            )
            return
        if record.get("status") == "retry_required" and not record.get("reservation"):
            bot.send_message(message.chat.id, "❌ Резерва уже нет; заказ ожидает новые данные, а не снятие резерва.")
            return
        if record.get("status") == "delivered":
            bot.send_message(message.chat.id, "❌ Нельзя снять резерв с уже выданного заказа.")
            return
        restored = restore_reservation_to_cache(record.get("reservation"))
        record["reservation"] = None
        record["status"] = "cancelled"
        record["updated_at"] = int(time.time())
        record.setdefault("notes", []).append("Резерв снят администратором через /fprelease.")
        save_json(FP_ORDERS_FILE, data)
    bot.send_message(message.chat.id, "✅ Резерв снят." if restored else "✅ Заказ помечен отмененным; активного резерва не было.")

@bot.message_handler(func=lambda m: m.text == "🔗 Где взять трейд-ссылку?")
def customer_help_tradeurl(message):
    bot.send_message(
        message.chat.id,
        "🔗 **Где найти вашу ссылку на обмен (Trade URL):**\n\n"
        "1. Нажмите кнопку ниже (или зайдите в **Steam ➔ Инвентарь ➔ Предложения обмена ➔ Кто может отправлять мне предложения обмена**).\n"
        "2. Пролистайте в самый низ до раздела **«Сторонние сайты»** и скопируйте ссылку, в которой есть слова `tradeoffer/new/?partner=...&token=...`\n"
        "3. ⚠️ Убедитесь, что ваш **Инвентарь открыт (Публичный)**!",
        reply_markup=get_tradeurl_help_markup(),
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda m: m.text == "❓ Как получить карточки?")
def customer_help_howto(message):
    bot.send_message(
        message.chat.id,
        "❓ **Инструкция по активации пин-кода:**\n\n"
        "Отправьте пин-код (`SET...`) и вашу **Трейд-ссылку** (`https://steamcommunity.com/tradeoffer/new/?partner=...&token=...`) в одном или двух сообщениях.\n\n"
        "⚠️ Обычная ссылка на профиль (`/profiles/...` или `/id/...`) не является трейд-ссылкой!",
        reply_markup=get_tradeurl_help_markup(),
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda m: m.text and m.text.startswith("⚙️ Автозапуск Windows:"))
def handle_toggle_autostart(message):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db["admin_id"]:
        return
    _, msg = toggle_autostart()
    bot.send_message(message.chat.id, msg, reply_markup=build_admin_keyboard(), parse_mode="Markdown")

@bot.message_handler(commands=["fp"])
def set_fp_key(message):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db["admin_id"]:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "⚠️ Напиши ключ через пробел: `/fp твой_golden_key`", parse_mode="Markdown")
        return
    key = parts[1].strip()
    config = load_json(FP_CONFIG_FILE, {})
    config["golden_key"] = key
    config["last_raise_ts"] = 0
    # The owner selected the in-chat link + text-file workflow for this custom bot.
    # Buyers are never directed to Telegram or an external file-hosting service.
    config["delivery_input_mode"] = "both"
    config["allow_chat_trade_url"] = True
    config["auto_delivery_funpay"] = True
    config["funpay_workflow_version"] = 2
    save_json(FP_CONFIG_FILE, config)
    bot.send_message(message.chat.id, "⏳ Проверяю ключ `golden_key` на FunPay...", parse_mode="Markdown")
    ok, msg = funpay_ping_and_raise(force_raise=True)
    if ok:
        bot.send_message(
            message.chat.id,
            f"✅ **FunPay подключен.** (ID: `{fp_state['user_id']}`)\n"
            "Автоматизация обрабатывает ссылку из чата или текстовое вложение; внешние ссылки покупателям не отправляются.",
            reply_markup=build_admin_keyboard(),
            parse_mode="Markdown"
        )
    else:
        bot.send_message(message.chat.id, f"❌ Ошибка: {msg}")

def build_funpay_dashboard_text():
    """Build a read-only owner dashboard; never triggers delivery or lot raising."""
    policy = get_funpay_delivery_policy()
    with funpay_orders_lock:
        orders = list(load_funpay_orders()["orders"].values())

    counts = {}
    for order in orders:
        status = str(order.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1

    cache = load_json(CACHE_FILE, {})
    available_sets, _ = count_available_sets(cache) if cache else (0, [])
    reserved = sum(1 for order in orders if order.get("reservation"))
    pending = sum(1 for order in orders if order.get("status") in {"awaiting_input", "input_received", "reserved", "retry_required"})
    review = sum(counts.get(status, 0) for status in {"review", "delivery_uncertain"})
    offers_sent = counts.get("offer_sent", 0)
    offers_pending_confirmation = counts.get("offer_pending_confirmation", 0)
    completed = counts.get("completed", 0)
    cancelled = counts.get("cancelled", 0)
    mode = policy.get("input_mode", "both")
    mode_label = {"file": "📎 Текстовый файл", "chat": "💬 Чат", "both": "💬 Чат + 📎 текстовый файл"}.get(mode, mode)

    return (
        "📊 <b>ПАНЕЛЬ FUNPAY</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📡 Подключение: <b>{'🟢 онлайн' if fp_state.get('online') else '🔴 не подключён'}</b>\n"
        f"🕒 Последний пинг: <code>{html.escape(str(fp_state.get('last_ping', 'Никогда')))}</code>\n"
        f"📦 Доступно полных сетов в кэше: <b>{available_sets}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Всего заказов в журнале: <b>{len(orders)}</b>\n"
        f"⏳ Ожидают данные: <b>{pending}</b>\n"
        f"📌 Зарезервировано заказов: <b>{reserved}</b>\n"
        f"🛡 Требуют проверки: <b>{review}</b>\n"
        f"📤 Steam-предложений создано: <b>{offers_sent}</b>\n"
        f"🕒 Ожидают подтверждения ASF: <b>{offers_pending_confirmation}</b>\n"
        f"✅ Сделки вручную подтверждены: <b>{completed}</b>\n"
        f"🚫 Отменены: <b>{cancelled}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📨 Приём ссылки: <b>{html.escape(mode_label)}</b>\n"
        f"🚦 Автовыдача по чату/текстовому файлу: <b>{'ВКЛЮЧЕНА' if policy.get('auto_delivery') else 'ВЫКЛЮЧЕНА'}</b>\n"
        "🔒 Проверка домена, формата файла, уникальности заказа и дублей: <b>АКТИВНА</b>\n"
        + (f"⚠️ Последняя ошибка: <code>{html.escape(str(fp_state.get('error'))[:250])}</code>\n" if fp_state.get("error") else "")
        + "\n<i>Это информационная панель. Открытие не запускает выдачу и не меняет настройки.</i>"
    )


def build_funpay_dashboard_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔄 Обновить сводку", callback_data="fpdash_refresh"),
        types.InlineKeyboardButton("📋 Последние заказы", callback_data="fpdash_orders"),
    )
    return markup


@bot.message_handler(func=lambda m: m.text == "📋 Панель FunPay — сводка")
def show_funpay_dashboard(message):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db.get("admin_id"):
        return
    bot.send_message(
        message.chat.id,
        build_funpay_dashboard_text(),
        reply_markup=build_funpay_dashboard_markup(),
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda call: call.data in {"fpdash_refresh", "fpdash_orders"})
def funpay_dashboard_callback(call):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if (
        not call.message
        or call.message.chat.id != db.get("admin_id")
        or call.from_user.id != db.get("admin_id")
    ):
        bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
        return
    if call.data == "fpdash_orders":
        with funpay_orders_lock:
            orders = list(load_funpay_orders()["orders"].values())
        orders.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
        lines = ["📋 <b>ПОСЛЕДНИЕ ЗАКАЗЫ FUNPAY</b>"]
        for item in orders[:10]:
            number = html.escape(str(item.get("funpay_order_number") or item.get("key") or "без номера"))
            status = html.escape(str(item.get("status", "unknown")))
            kind = html.escape(str(item.get("input_kind") or "данные не получены"))
            lines.append(
                f"• <b>{number}</b> — {int(item.get('sets_total', 0) or 0)} сет. — {status}\n"
                f"  Вход: {kind}"
            )
        text = "\n".join(lines) if len(lines) > 1 else "📋 Журнал заказов FunPay пока пуст."
    else:
        text = build_funpay_dashboard_text()
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=build_funpay_dashboard_markup(), parse_mode="HTML"
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logging.exception("Не удалось обновить панель FunPay")
    bot.answer_callback_query(call.id, "Сводка обновлена." if call.data == "fpdash_refresh" else "Журнал обновлён.")


@bot.callback_query_handler(func=lambda call: call.data in {"podash_refresh", "podash_orders"})
def playerok_dashboard_callback(call):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if (
        not call.message
        or call.message.chat.id != db.get("admin_id")
        or call.from_user.id != db.get("admin_id")
    ):
        bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
        return
    text = build_playerok_dashboard_text(show_orders=(call.data == "podash_orders"))
    try:
        bot.edit_message_text(
            text, call.message.chat.id, call.message.message_id,
            reply_markup=build_playerok_dashboard_markup(), parse_mode="HTML"
        )
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logging.exception("Не удалось обновить панель Playerok")
    bot.answer_callback_query(call.id, "Сделки обновлены." if call.data == "podash_refresh" else "Журнал обновлён.")

@bot.message_handler(func=lambda m: m.text == "🟢 Статус FunPay 24/7")
def check_fp_status(message):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db.get("admin_id"):
        return
    # Read the last known state only. Connectivity checks and scheduled lot raises
    # are handled by the background worker, never by this read-only dashboard.
    status_icon = "🟢 ОНЛАЙН АКТИВЕН" if fp_state["online"] else "🔴 НЕ ПОДКЛЮЧЕН"
    auto_status = "ВКЛЮЧЕН 🟢" if is_autostart_enabled() else "ВЫКЛЮЧЕН 🔴"
    cfg = load_json(FP_CONFIG_FILE, {})
    golden_key_configured = bool(str(cfg.get("golden_key", "")).strip())
    if fp_state.get("error"):
        connection_detail = str(fp_state["error"])[:300]
    elif not golden_key_configured:
        connection_detail = "Ключ FunPay не настроен. Для подключения отправь /fp <golden_key> боту от имени администратора."
    elif not fp_state.get("online"):
        connection_detail = "Ключ сохранён, но соединение ещё не подтверждено. Подожди до 3 минут или повторно проверь ключ командой /fp."
    else:
        connection_detail = "Подключение подтверждено."
    text = (
        f"📡 **Статус системы FunPay:**\n\n"
        f"• Статус: **{status_icon}** (ID: `{fp_state['user_id']}`)\n"
        f"• Последний пинг: `{fp_state['last_ping']}`\n"
        f"• Автоподнятие лотов: `{fp_state['last_raise']}`\n"
        f"• Диагностика: {connection_detail}\n"
        f"• Автовыдача FunPay: **Приостановлена до подтверждения разрешенного способа передачи**\n"
        f"• Автозапуск Windows: **{auto_status}**\n\n"
        "🔐 Не отправляй golden_key в групповые чаты и не публикуй его в сообщениях покупателям."
    )
    bot.send_message(message.chat.id, text, reply_markup=build_admin_keyboard(), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith("fpreply_"))
def callback_fp_reply(call):
    parts = call.data.split("_", 2)
    node_id = parts[1]
    buyer_name = parts[2] if len(parts) > 2 else node_id
    active_reply_target[call.message.chat.id] = node_id
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"✍️ Напиши следующим сообщением ответ покупателю (чат #node{node_id}).\n"
        f"Отправь /cancel для отмены."
    )

@bot.message_handler(func=lambda m: m.text == "🔄 Обновить кэш склада")
def handle_refresh_cache(message):
    bot.send_message(message.chat.id, "🔐 Авторизуюсь в Steam через ASF и скачиваю инвентарь...")
    cache_data, err = update_inventory_cache()
    if err:
        bot.send_message(message.chat.id, f"⚠️ {err}")
        return
    total_sets, details = count_available_sets(cache_data)
    lines = [f"✅ **Кэш успешно обновлен!**", f"Всего карточек: **{cache_data['total_cards']} шт.**", f"Готовых полных сетов: **{total_sets} шт.**\n"]
    for g_name, u_cnt, s_cnt in details:
        lines.append(f"• **{g_name}** ({u_cnt} карт): `{s_cnt} сет.`")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "📊 Остатки в кэше")
def handle_show_cache(message):
    cache_data = load_json(CACHE_FILE, {})
    if not cache_data:
        bot.send_message(message.chat.id, "⚠️ Кэш еще не создан.")
        return
    total_sets, details = count_available_sets(cache_data)

    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    vouchers = db.get("vouchers", {})
    fp_1 = sum(1 for v in vouchers.values() if v.get("platform", "funpay") == "funpay" and v.get("sets") == 1 and not v.get("used") and not v.get("sent_to_fp"))
    fp_5 = sum(1 for v in vouchers.values() if v.get("platform", "funpay") == "funpay" and v.get("sets") == 5 and not v.get("used") and not v.get("sent_to_fp"))
    pl_1 = sum(1 for v in vouchers.values() if v.get("platform") == "playerok" and v.get("sets") == 1 and not v.get("used"))
    pl_5 = sum(1 for v in vouchers.values() if v.get("platform") == "playerok" and v.get("sets") == 5 and not v.get("used"))

    lines = [
        f"📊 **Сводка склада и пулов кодов:**",
        f"📦 Полных сетов в кэше Steam: **{total_sets} шт.**",
        f"🟢 Свободных кодов **FunPay**: `{fp_1} шт.` (по 1 сету) | `{fp_5} шт.` (по 5 сетов)",
        f"🔵 Свободных кодов **Playerok**: `{pl_1} шт.` (по 1 сету) | `{pl_5} шт.` (по 5 сетов)\n"
    ]
    for g_name, u_cnt, s_cnt in details:
        lines.append(f"• **{g_name}** ({u_cnt} карт): `{s_cnt} сет.`")
    bot.send_message(message.chat.id, "\n".join(lines), parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text in [
    "🟢 Коды FunPay (1 сет)", "🟢 Коды FunPay (5 сетов)",
    "🔵 Коды Playerok (1 сет)", "🔵 Коды Playerok (5 сетов)"
])
def gen_codes_formatted(message):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db["admin_id"]:
        return

    is_playerok = "Playerok" in message.text
    platform_tag = "playerok" if is_playerok else "funpay"
    sets_count = 1 if "1 сет" in message.text else 5
    count_to_gen = 5 if is_playerok else 10

    ready_lines = []
    for _ in range(count_to_gen):
        code = f"SET{sets_count}-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        db["vouchers"][code] = {
            "sets": sets_count,
            "used": False,
            "platform": platform_tag,
            "sent_to_fp": False,
            "created_at": int(time.time())
        }
        if is_playerok:
            ready_lines.append(
                f"Ваш пин-код ({sets_count} полн. сет): {code} | Для мгновенного получения карточек (24/7) введите этот код и вашу трейд-ссылку в нашем боте автовыдачи (поиск в TG: @{BOT_USERNAME}), либо просто напишите вашу трейд-ссылку сюда в чат заказа!"
            )
        else:
            ready_lines.append(f"{code}")

    save_json(DB_FILE, db)

    if is_playerok:
        out_block = "\n".join(ready_lines)
        bot.send_message(
            message.chat.id,
            f"🔵 **Сгенерировано 5 изолированных кодов для Playerok ({sets_count} сет.):**\n"
            f"_(Автоответчик FunPay их НЕ трогает! Копируй по 1 строчке в объявление на Playerok)_\n\n"
            f"```text\n{out_block}\n```",
            parse_mode="Markdown"
        )
    else:
        codes_list = "\n".join(ready_lines)
        bot.send_message(
            message.chat.id,
            f"🟢 **Добавлено 10 кодов по {sets_count} сет(ов) в изолированный пул FunPay!**\n\n"
            f"Список кодов:\n`{codes_list}`",
            parse_mode="Markdown"
        )

@bot.message_handler(func=lambda m: True)
def handle_all_messages(message):
    text = (message.text or "").strip()
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})

    # Admin navigation is handled here so all existing actions can reuse
    # build_admin_keyboard() and return to the currently open submenu.
    if message.chat.id == db.get("admin_id"):
        section_buttons = {
            "📦 Склад и коды": "stock",
            "🟢 FunPay": "funpay",
            "🔵 Playerok": "playerok",
            "📈 Продажи и аналитика": "sales",
            "⚙️ Настройки": "settings",
        }
        if text in section_buttons:
            admin_menu_state[message.chat.id] = section_buttons[text]
            bot.send_message(message.chat.id, "Выбери действие в разделе:", reply_markup=build_admin_keyboard())
            return
        if text in {ADMIN_BACK_MENU, ADMIN_MAIN_MENU}:
            admin_menu_state[message.chat.id] = "main"
            bot.send_message(message.chat.id, "Главное меню:", reply_markup=build_admin_keyboard())
            return
        if text == "📋 Последние заказы FunPay":
            funpay_orders_cmd(message)
            return
        if text == "📋 Последние сделки Playerok":
            bot.send_message(message.chat.id, build_playerok_dashboard_text(show_orders=True), reply_markup=build_playerok_dashboard_markup(), parse_mode="HTML")
            return
        if text == "🔄 Проверить соединение FunPay":
            bot.send_message(message.chat.id, "Статус подключения отображается в сводке FunPay. Обновление состояния выполняется фоновым монитором.", reply_markup=build_admin_keyboard())
            return
        if text == "🔄 Проверить состояние бота":
            cfg = load_json(FP_CONFIG_FILE, {})
            playerok_configured = bool(os.getenv("PLAYEROK_TOKEN", "").strip())
            diagnostics = (
                "🔎 <b>Диагностика</b>\\n"
                f"FunPay: {'ключ настроен' if str(cfg.get('golden_key', '')).strip() else 'ключ не задан'}\\n"
                f"Playerok: {'токен задан' if playerok_configured else 'не настроен'}\\n"
                f"Автозапуск Windows: {'включён' if is_autostart_enabled() else 'выключен'}\\n"
                "Автовыдача FunPay: приостановлена защитой модерации.\\n"
                "Автовыдача Playerok: выключена."
            )
            bot.send_message(message.chat.id, diagnostics, reply_markup=build_admin_keyboard(), parse_mode="HTML")
            return

    # Route the dashboard action explicitly before general customer processing.
    if text == "📋 Панель FunPay — сводка" and message.chat.id == db.get("admin_id"):
        show_funpay_dashboard(message)
        return

    if text in {"📈 Продажи и склад", "/sales"} and message.chat.id == db.get("admin_id"):
        bot.send_message(
            message.chat.id,
            build_sales_analytics_text(),
            reply_markup=build_admin_keyboard(),
            parse_mode="Markdown",
        )
        return

    if text in {"🔵 Playerok — панель", "/playerok_status"} and message.chat.id == db.get("admin_id"):
        bot.send_message(
            message.chat.id,
            build_playerok_dashboard_text(),
            reply_markup=build_playerok_dashboard_markup(),
            parse_mode="HTML",
        )
        return

    if text == "/playerok_orders" and message.chat.id == db.get("admin_id"):
        bot.send_message(
            message.chat.id,
            build_playerok_dashboard_text(show_orders=True),
            reply_markup=build_playerok_dashboard_markup(),
            parse_mode="HTML",
        )
        return

    # 1. Ответ админа в чат FunPay
    if message.chat.id == db.get("admin_id"):
        if text == "/cancel":
            active_reply_target.pop(message.chat.id, None)
            bot.send_message(message.chat.id, "❎ Режим ответа отменен.")
            return

        target_node = None
        if message.reply_to_message and message.reply_to_message.text:
            m_node = re.search(r'#node(\d+)', message.reply_to_message.text)
            if m_node:
                target_node = m_node.group(1)
        elif message.chat.id in active_reply_target:
            # The admin explicitly entered reply mode; send the next text verbatim,
            # including codes or URLs that would otherwise be mistaken for customer input.
            target_node = active_reply_target[message.chat.id]

        if target_node:
            bot.send_message(message.chat.id, f"⏳ Отправляю ответ в чат FunPay #node{target_node}...")
            ok, res_msg = send_funpay_chat_message(target_node, text)
            if ok:
                active_reply_target.pop(message.chat.id, None)
                bot.send_message(message.chat.id, f"✅ Ответ отправлен на FunPay!\nВаше сообщение: {text}")
            else:
                bot.send_message(message.chat.id, f"❌ Не удалось отправить: {res_msg}\nРежим ответа оставлен включенным; можно повторить сообщение или отправить /cancel.")
            return

    # 2. Поиск пин-кода, трейд-ссылки ИЛИ ошибочной ссылки на профиль
    code_match = re.search(r'\b(SET\d+-[A-Z0-9]{6})\b', text.upper())
    url_match = re.search(r'(https?://steamcommunity\.com/tradeoffer/new/\?[^\s]+)', text)
    profile_url_match = re.search(r'https?://steamcommunity\.com/(?:profiles|id)/[^\s]+', text)

    code = code_match.group(1) if code_match else None
    trade_url = url_match.group(1).replace("&amp;", "&") if url_match else None
    is_profile_link = bool(profile_url_match and not trade_url)

    if text == "/cancel":
        pending_code = pending_customer_codes.pop(message.chat.id, None)
        if pending_code:
            bot.send_message(message.chat.id, "❎ Сохраненный пин-код удален. Начните заново, отправив новый код.")
        else:
            bot.send_message(message.chat.id, "Сейчас нет сохраненного пин-кода для отмены.")
        return

    # Если покупатель отправил код и трейд-ссылку отдельными сообщениями,
    # подставляем сохраненный код к следующей ссылке.
    if not code and (trade_url or is_profile_link):
        code = pending_customer_codes.get(message.chat.id)

    # Случай А: Прислали и пин-код, и настоящую трейд-ссылку
    if code and trade_url:
        if code not in db["vouchers"] or db["vouchers"][code]["used"]:
            bot.send_message(message.chat.id, "❌ Этот пин-код не найден или уже был активирован ранее!")
            return
        execute_delivery(message.chat.id, code, trade_url, db)
        return

    # Случай Б: Прислали пин-код (или пин-код + ошибочную ссылку на профиль)
    if code and not trade_url:
        if code not in db["vouchers"] or db["vouchers"][code]["used"]:
            bot.send_message(message.chat.id, "❌ Этот пин-код не найден или уже был активирован ранее!")
            return
        pending_customer_codes[message.chat.id] = code
        sets_cnt = db["vouchers"][code]["sets"]
        if is_profile_link:
            bot.send_message(
                message.chat.id,
                f"✅ **Пин-код `{code}` ({sets_cnt} сет.) сохранен!**\n\n"
                f"⚠️ **ОШИБКА ССЫЛКИ:** Вы прислали ссылку на ваш **ПРОФИЛЬ** Steam (`/profiles/...`), а по ней отправить карточки невозможно!\n\n"
                f"👉 Пожалуйста, нажмите кнопку ниже, скопируйте вашу **Трейд-ссылку** (в ней должны быть слова `tradeoffer/new/?partner=...&token=...`) и отправьте её сюда:",
                reply_markup=get_tradeurl_help_markup(),
                parse_mode="Markdown"
            )
        else:
            bot.send_message(
                message.chat.id,
                f"✅ **Пин-код `{code}` на {sets_cnt} полных сет(ов) принят!**\n\n"
                f"👉 **Теперь отправьте вашу трейд-ссылку Steam** (вида `https://steamcommunity.com/tradeoffer/new/?partner=...&token=...`), и я мгновенно отправлю вам карточки!",
                reply_markup=get_tradeurl_help_markup(),
                parse_mode="Markdown"
            )
def acquire_python_instance_mutex():
    # This guard also covers direct "py bot.py" launches, outside start.bat.
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create_mutex.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    ctypes.set_last_error(0)
    handle = create_mutex(None, True, "Local\\CardBotPythonTelegramPolling")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


_instance_mutex = acquire_python_instance_mutex()
if _instance_mutex is None:
    logging.error("Повторный запуск пропущен: уже работает другой экземпляр Telegram-бота.")
    raise SystemExit(23)

try:
    threading.Thread(target=funpay_background_worker, daemon=True).start()
    start_playerok_monitor()
    bot.infinity_polling()
finally:
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle(_instance_mutex)
