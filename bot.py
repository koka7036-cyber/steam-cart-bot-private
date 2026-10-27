import os
import re
import html
import json
import logging
import time
import base64
import random
import string
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
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

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

from order_parser import detect_order_sets_and_quantity

def funpay_check_unread_chats():
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    admin_id = db.get("admin_id")
    if not admin_id:
        return

    s, cfg, err = get_fp_session()
    if err:
        return

    seen_msgs = cfg.get("seen_msgs", {})
    first_run = "seen_msgs" not in cfg

    try:
        r = s.get("https://funpay.com/chat/", timeout=15)
        if "PHPSESSID" in s.cookies:
            fp_state["phpsessid"] = s.cookies["PHPSESSID"]

        blocks = r.text.split('<a href="https://funpay.com/chat/?node=')[1:]
        changed = False

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

            sig = f"{sender_name}:{msg_preview}"

            if first_run:
                seen_msgs[node_id] = sig
                changed = True
                continue

            if "unread" in classes and seen_msgs.get(node_id) != sig:
                seen_msgs[node_id] = sig
                changed = True

                full_text = msg_preview
                try:
                    r_node = s.get(f"https://funpay.com/chat/?node={node_id}", timeout=10)
                    full_msgs = re.findall(r'<div class="chat-msg-text">([\s\S]*?)</div>', r_node.text)
                    if full_msgs:
                        full_text = html.unescape(re.sub(r'<[^>]+>', '', full_msgs[-1]).strip())
                except Exception:
                    pass

                lower_txt = full_text.lower()
                is_order_payment = (
                    "оплатил" in lower_txt or "создан заказ" in lower_txt or
                    "переведите" in lower_txt or "заказ #" in lower_txt or
                    bool(re.search(r"\bзаказ\s*#?\d+", lower_txt))
                )
                sets_per_item, order_quantity = detect_order_sets_and_quantity(full_text)
                mentions_product = any(term in lower_txt for term in
                                        ("карт", "сет", "level", "уровень", "steam", "карточ"))

                if is_order_payment and mentions_product and sets_per_item in (1, 5):
                    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
                    vouchers = db.get("vouchers", {})
                    # Подбираем сразу все коды для количества экземпляров заказа.
                    available_codes = [
                        code for code, vdata in vouchers.items()
                        if vdata.get("platform", "funpay") == "funpay"
                        and vdata.get("sets") == sets_per_item
                        and not vdata.get("used") and not vdata.get("sent_to_fp")
                    ][:order_quantity]

                    if len(available_codes) == order_quantity:
                        lines = []
                        for code in available_codes:
                            lines.append(f"Пин-код ({sets_per_item} сет.): {code}")
                        codes_text = "\n".join(lines)
                        auto_reply_text = (
                            f"🤖 Автоматическая выдача 24/7!\n"
                            f"Заказ: {order_quantity} шт. × {sets_per_item} сет.\n"
                            f"{codes_text}\n\n"
                            f"👉 Для получения карточек отправьте каждый пин-код и вашу ТРЕЙД-ССЫЛКУ "
                            f"(не ссылку на профиль!) нашему боту в Telegram: @{BOT_USERNAME}"
                        )
                        ok_reply, reply_err = send_funpay_chat_message(node_id, auto_reply_text)
                        if ok_reply:
                            # Сохраняем резервирование только после подтвержденной отправки.
                            latest_db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
                            for code in available_codes:
                                if code in latest_db.get("vouchers", {}):
                                    latest_db["vouchers"][code]["sent_to_fp"] = True
                            save_json(DB_FILE, latest_db)
                            bot.send_message(
                                admin_id,
                                f"⚡️ **Автовыдача на FunPay сработала!**\n"
                                f"• Покупатель: {sender_name}\n"
                                f"• Лот: {sets_per_item} сет. × {order_quantity} шт.\n"
                                f"• Выдано кодов: {len(available_codes)}",
                                parse_mode="Markdown"
                            )
                        else:
                            bot.send_message(
                                admin_id,
                                f"❌ **Не удалось отправить коды на FunPay.**\n"
                                f"Покупатель: {sender_name}\n"
                                f"Заказ: {order_quantity} × {sets_per_item} сет.\n"
                                f"Коды не помечены выданными. Ошибка: {reply_err}"
                            )
                    else:
                        bot.send_message(
                            admin_id,
                            f"⚠️ **Не хватает кодов FunPay!**\n"
                            f"Покупатель: {sender_name}\n"
                            f"Лот: {sets_per_item} сет. × {order_quantity} шт.\n"
                            f"Доступно кодов: {len(available_codes)} из {order_quantity}. "
                            f"Автоответ не отправлен, чтобы не выдать заказ частично."
                        )
                else:
                    markup = types.InlineKeyboardMarkup()
                    markup.add(types.InlineKeyboardButton(f"✍️ Ответить ({sender_name})", callback_data=f"fpreply_{node_id}_{sender_name[:20]}"))
                    bot.send_message(
                        admin_id,
                        f"💬 **Новое сообщение на FunPay!**\n"
                        f"👤 От: **{sender_name}** (Чат `#node{node_id}`)\n\n"
                        f"✉️ _{full_text}_\n\n"
                        f"💡 *Сделай Reply (Ответить) на это сообщение или нажми кнопку ниже!*",
                        reply_markup=markup,
                        parse_mode="Markdown"
                    )
            elif "unread" not in classes and node_id not in seen_msgs:
                seen_msgs[node_id] = sig
                changed = True

        if changed or first_run:
            cfg["seen_msgs"] = seen_msgs
            save_json(FP_CONFIG_FILE, cfg)
    except Exception:
        pass

def funpay_background_worker():
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
    games = cache_data.get("games", {})
    if not games:
        return None, None, "Кэш пуст! Нажми «🔄 Обновить кэш склада»."

    selected_assets = []
    picked_games_info = []
    for game_name, cards_dict in sorted(games.items(), key=lambda x: len(x[1])):
        unique_count = len(cards_dict)
        if unique_count < 5:
            continue
        available_here = min(len(aids) for aids in cards_dict.values())
        take_sets = min(available_here, sets_needed, 5)
        if take_sets > 0:
            for _ in range(take_sets):
                for card_name in cards_dict:
                    selected_assets.append(cards_dict[card_name].pop(0))
            picked_games_info.append(f"{game_name} ({take_sets} сет. по {unique_count} карт)")
            sets_needed -= take_sets
        if sets_needed <= 0:
            break

    if sets_needed > 0:
        return None, None, "На складе временно закончились полные сеты."
    return selected_assets, (cache_data, picked_games_info), None

def send_trade_offer(trade_url, asset_ids, code):
    parsed = urllib.parse.urlparse(trade_url)
    params = urllib.parse.parse_qs(parsed.query)
    partner_id = params.get("partner", [None])[0]
    token = params.get("token", [None])[0]
    if not partner_id or not token:
        return False, "Неверная трейд-ссылка (отсутствует partner или token)."

    partner_steamid64 = str(int(partner_id) + 76561197960265728)
    steam_login_secure, err = get_steam_session()
    if err:
        return False, err

    session_id = "".join(random.choices("0123456789abcdef", k=24))
    cookies = {"sessionid": session_id, "steamLoginSecure": steam_login_secure}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        "Referer": trade_url,
        "Origin": "https://steamcommunity.com"
    }
    assets_list = [{"appid": 753, "contextid": "6", "amount": 1, "assetid": str(aid)} for aid in asset_ids]
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
        "tradeoffermessage": f"Автовыдача по коду {code}. Спасибо за покупку!",
        "json_tradeoffer": json.dumps(tradeoffer_json),
        "captcha": "",
        "trade_offer_create_params": json.dumps({"trade_offer_access_token": token})
    }

    try:
        r = requests.post("https://steamcommunity.com/tradeoffer/new/send", data=form_data, cookies=cookies, headers=headers, timeout=20)
        res_json = r.json()
    except Exception as e:
        return False, f"Ошибка запроса к Steam: {e}"

    if "tradeofferid" not in res_json:
        return False, f"Steam отклонил обмен (проверьте, открыт ли инвентарь в настройках приватности): {res_json.get('strError', res_json)}"

    offer_id = res_json["tradeofferid"]
    time.sleep(2)
    asf_command(f"2faok {STEAM_LOGIN}")
    return True, f"Трейд #{offer_id} отправлен и подтвержден в ASF"

def execute_delivery(chat_id, code, trade_url, db):
    sets_needed = db["vouchers"][code]["sets"]
    platform_tag = db["vouchers"][code].get("platform", "funpay").upper()
    bot.send_message(chat_id, f"🔄 Код `{code}` проверен! Собираю **{sets_needed} полных сет(ов)** и отправляю трейд...", parse_mode="Markdown")

    selected_assets, cache_bundle, err = pick_cards_for_sets(sets_needed)
    if err:
        bot.send_message(chat_id, f"❌ {err}")
        return

    updated_cache, picked_games_info = cache_bundle
    ok, result_msg = send_trade_offer(trade_url, selected_assets, code)

    if not ok:
        bot.send_message(
            chat_id,
            f"⚠️ **Не удалось отправить обмен (ваш пин-код НЕ сгорел!):**\n`{result_msg}`\n\n"
            f"Проверьте настройки приватности инвентаря Steam и отправьте правильную трейд-ссылку еще раз.",
            parse_mode="Markdown"
        )
        pending_customer_codes[chat_id] = code
        return

    db["vouchers"][code]["used"] = True
    save_json(DB_FILE, db)
    save_json(CACHE_FILE, updated_cache)
    pending_customer_codes.pop(chat_id, None)

    games_str = ", ".join(picked_games_info)
    bot.send_message(
        chat_id,
        f"✅ **Обмен успешно отправлен и подтвержден!**\n"
        f"📦 Выданные наборы: **{games_str}** (всего карточек: {len(selected_assets)} шт.)\n\n"
        f"👉 Перейдите в Steam, примите входящий обмен и не забудьте подтвердить заказ и оставить отзыв на сайте ⭐️",
        parse_mode="Markdown"
    )

    if db.get("admin_id"):
        rem_sets, _ = count_available_sets(updated_cache)
        bot.send_message(
            db["admin_id"],
            f"💰 **Успешная автовыдача ({platform_tag})!**\n"
            f"• Код: `{code}`\n"
            f"• Получатель: `{trade_url}`\n"
            f"• Отправлено: {games_str}\n"
            f"• Остаток в кэше: **{rem_sets} полных сетов**",
            parse_mode="Markdown"
        )

# --- КЛАВИАТУРЫ ---
def build_admin_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔄 Обновить кэш склада", "📊 Остатки в кэше")
    markup.add("🟢 Коды FunPay (1 сет)", "🟢 Коды FunPay (5 сетов)")
    markup.add("🔵 Коды Playerok (1 сет)", "🔵 Коды Playerok (5 сетов)")
    auto_label = "⚙️ Автозапуск Windows: ВКЛ 🟢" if is_autostart_enabled() else "⚙️ Автозапуск Windows: ВЫКЛ 🔴"
    markup.add("🟢 Статус FunPay 24/7", auto_label)
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
    if code in db["vouchers"]:
        db["vouchers"][code]["used"] = False
        db["vouchers"][code]["sent_to_fp"] = False
        save_json(DB_FILE, db)
        bot.send_message(message.chat.id, f"✅ Код `{code}` снова полностью активен!", parse_mode="Markdown")
    else:
        bot.send_message(message.chat.id, f"❌ Код `{code}` не найден в базе.", parse_mode="Markdown")

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
    save_json(FP_CONFIG_FILE, {"golden_key": key, "last_raise_ts": 0})
    bot.send_message(message.chat.id, "⏳ Проверяю ключ `golden_key` на FunPay...", parse_mode="Markdown")
    ok, msg = funpay_ping_and_raise(force_raise=True)
    if ok:
        bot.send_message(
            message.chat.id,
            f"✅ **FunPay Full-Auto подключен!** (ID: `{fp_state['user_id']}`)",
            reply_markup=build_admin_keyboard(),
            parse_mode="Markdown"
        )
    else:
        bot.send_message(message.chat.id, f"❌ Ошибка: {msg}")

@bot.message_handler(func=lambda m: m.text == "🟢 Статус FunPay 24/7")
def check_fp_status(message):
    db = load_json(DB_FILE, {"admin_id": None, "vouchers": {}})
    if message.chat.id != db["admin_id"]:
        return
    funpay_ping_and_raise(force_raise=True)
    status_icon = "🟢 ОНЛАЙН АКТИВЕН" if fp_state["online"] else "🔴 НЕ ПОДКЛЮЧЕН"
    auto_status = "ВКЛЮЧЕН 🟢" if is_autostart_enabled() else "ВЫКЛЮЧЕН 🔴"
    text = (
        f"📡 **Статус системы FunPay Full-Auto:**\n\n"
        f"• Статус: **{status_icon}** (ID: `{fp_state['user_id']}`)\n"
        f"• Последний пинг: `{fp_state['last_ping']}`\n"
        f"• Автоподнятие лотов: `{fp_state['last_raise']}`\n"
        f"• Автовыдача в чат FunPay: **Активна (только пул FunPay) 🚀**\n"
        f"• Автозапуск Windows: **{auto_status}**"
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
        f"✍️ Напиши следующим сообщением свой ответ для покупателя **{buyer_name}** (чат `#node{node_id}`):\n"
        f"_(или отправь /cancel для отмены)_",
        parse_mode="Markdown"
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
            "sent_to_fp": False
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
        elif message.chat.id in active_reply_target and not text.startswith("SET") and "steamcommunity.com" not in text:
            target_node = active_reply_target.pop(message.chat.id)

        if target_node:
            bot.send_message(message.chat.id, f"⏳ Отправляю ответ в чат FunPay `#node{target_node}`...", parse_mode="Markdown")
            ok, res_msg = send_funpay_chat_message(target_node, text)
            if ok:
                bot.send_message(message.chat.id, f"✅ **Ответ отправлен на FunPay!**\n💬 _{text}_", parse_mode="Markdown")
            else:
                bot.send_message(message.chat.id, f"❌ Не удалось отправить: {res_msg}")
            return

    # 2. Поиск пин-кода, трейд-ссылки ИЛИ ошибочной ссылки на профиль
    code_match = re.search(r'\b(SET\d+-[A-Z0-9]{6})\b', text.upper())
    url_match = re.search(r'(https?://steamcommunity\.com/tradeoffer/new/\?[^\s]+)', text)
    profile_url_match = re.search(r'https?://steamcommunity\.com/(?:profiles|id)/[^\s]+', text)

    code = code_match.group(1) if code_match else None
    trade_url = url_match.group(1).replace("&amp;", "&") if url_match else None
    is_profile_link = bool(profile_url_match and not trade_url)

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
threading.Thread(target=funpay_background_worker, daemon=True).start()
bot.infinity_polling()
