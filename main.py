# -*- coding: utf-8 -*-
import os
import re
import hmac
import json
import time
import hashlib
import secrets
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify
import telebot
from telebot import types

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
TZ = timezone(timedelta(hours=7))
FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
FLASK_PORT = int(os.getenv("PORT", os.getenv("FLASK_PORT", "8080")))

GIST_ID = os.getenv("GIST_ID", "8a3b40053089341ad248e9f948e12237").strip()
GIST_OWNER = os.getenv("GIST_OWNER", "hgkhuyen255").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

USERS_FILE = os.getenv("GIST_USERS_FILE", "bot_users.json")
TOOLS_FILE = os.getenv("GIST_TOOLS_FILE", "bot_tools.json")
LICENSES_FILE = os.getenv("GIST_LICENSES_FILE", "bot_licenses.json")
COUPONS_FILE = os.getenv("GIST_COUPONS_FILE", "bot_coupons.json")
ORDERS_FILE = os.getenv("GIST_ORDERS_FILE", "bot_orders.json")
REMINDERS_FILE = os.getenv("GIST_REMINDERS_FILE", "bot_reminders.json")

PAYOS_CLIENT_ID = os.getenv("PAYOS_CLIENT_ID", "")
PAYOS_API_KEY = os.getenv("PAYOS_API_KEY", "")
PAYOS_CHECKSUM_KEY = os.getenv("PAYOS_CHECKSUM_KEY", "")
PAYOS_BASE_URL = os.getenv("PAYOS_BASE_URL", "https://api-merchant.payos.vn")
PAYOS_RETURN_URL = os.getenv("PAYOS_RETURN_URL", f"{PUBLIC_BASE_URL}/payment-return" if PUBLIC_BASE_URL else "")
PAYOS_CANCEL_URL = os.getenv("PAYOS_CANCEL_URL", f"{PUBLIC_BASE_URL}/payment-cancel" if PUBLIC_BASE_URL else "")
PAYOS_WEBHOOK_PATH = os.getenv("PAYOS_WEBHOOK_PATH", "/payos-webhook")
PAYOS_WEBHOOK_URL = f"{PUBLIC_BASE_URL}{PAYOS_WEBHOOK_PATH}" if PUBLIC_BASE_URL else ""

BANK_NAME = os.getenv("BANK_NAME", "").strip()
BANK_ACCOUNT_NO = os.getenv("BANK_ACCOUNT_NO", "").strip()
BANK_ACCOUNT_NAME = os.getenv("BANK_ACCOUNT_NAME", "").strip()
PAYMENT_NOTE_PREFIX = os.getenv("PAYMENT_NOTE_PREFIX", "TOOL")

REMINDER_CHECK_INTERVAL_SECONDS = int(os.getenv("REMINDER_CHECK_INTERVAL_SECONDS", "3600"))
REMINDER_DAYS = [7, 3, 1, 0]

DEFAULT_TOOLS = {
    "GROKTOOL": {"code": "GROKTOOL", "name": "Tool Auto Grok", "price": 50000, "description": "Tool tạo video tự động", "active": 1},
    "FBREELTOOL": {"code": "FBREELTOOL", "name": "Tool Auto Reels Facebook", "price": 50000, "description": "Tool up reels tự động", "active": 1},
}

if not BOT_TOKEN:
    raise SystemExit("Thiếu BOT_TOKEN")
if not ADMIN_IDS:
    raise SystemExit("Thiếu ADMIN_IDS")
if not GIST_ID:
    raise SystemExit("Thiếu GIST_ID")
if not GITHUB_TOKEN:
    raise SystemExit("Thiếu GITHUB_TOKEN")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)
BUY_STATE = {}

def now_vn():
    return datetime.now(TZ)

def iso_now():
    return now_vn().isoformat()

def fmt_dt(dt_str):
    if not dt_str:
        return "Chưa có"
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.astimezone(TZ).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(dt_str)

def fmt_money(v):
    try:
        return f"{int(v):,}đ"
    except Exception:
        return f"{v}đ"

def safe_upper(s):
    return (s or "").strip().upper()

def norm_machine_id(s):
    s = (s or "").strip().upper()
    s = re.sub(r"\s+", "", s)
    for bad in ["MACHINEID=", "MACHINE_ID=", "MACHINE-ID=", "MACHINE ID=", "MACHINE ID :", "MACHINE ID", "MACHINEID:"]:
        s = s.replace(bad, "")
    return s.replace("=", "").replace(":", "")

def is_valid_machine_id(s):
    return bool(re.fullmatch(r"[A-F0-9]{16,64}", norm_machine_id(s)))

def user_label(user):
    full_name = " ".join(x for x in [getattr(user, "first_name", ""), getattr(user, "last_name", "")] if x).strip()
    return full_name or getattr(user, "username", "") or str(getattr(user, "id", ""))

def is_admin(user_id):
    return int(user_id) in ADMIN_IDS

def admin_only(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bạn không có quyền dùng lệnh admin.")
        return False
    return True

def notify_admins(text):
    for aid in ADMIN_IDS:
        try:
            bot.send_message(aid, text)
        except Exception:
            pass

GIST_API_URL = f"https://api.github.com/gists/{GIST_ID}"
GIST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
}

def gist_raw_url(filename):
    return f"https://gist.githubusercontent.com/{GIST_OWNER}/{GIST_ID}/raw/{filename}"

def _safe_json_load(text, fallback):
    try:
        return json.loads(text)
    except Exception:
        return fallback

def load_gist_json(filename, fallback):
    try:
        r = requests.get(gist_raw_url(filename), timeout=20)
        if r.status_code == 404:
            return fallback
        r.raise_for_status()
        txt = r.text.strip()
        if not txt:
            return fallback
        return _safe_json_load(txt, fallback)
    except Exception:
        try:
            r = requests.get(GIST_API_URL, headers=GIST_HEADERS, timeout=20)
            r.raise_for_status()
            gist = r.json()
            content = ((gist.get("files") or {}).get(filename) or {}).get("content")
            if content is None:
                return fallback
            return _safe_json_load(content, fallback)
        except Exception:
            return fallback

def save_gist_json(filename, data):
    payload = {"files": {filename: {"content": json.dumps(data, ensure_ascii=False, indent=2)}}}
    r = requests.patch(GIST_API_URL, headers=GIST_HEADERS, json=payload, timeout=25)
    r.raise_for_status()
    return True

def bootstrap_gist():
    defaults = {
        USERS_FILE: {},
        TOOLS_FILE: DEFAULT_TOOLS,
        LICENSES_FILE: {},
        COUPONS_FILE: {},
        ORDERS_FILE: {},
        REMINDERS_FILE: {},
    }
    for fn, default in defaults.items():
        existing = load_gist_json(fn, None)
        if existing is None:
            save_gist_json(fn, default)

def get_users():
    return load_gist_json(USERS_FILE, {})

def save_users(data):
    save_gist_json(USERS_FILE, data)

def get_tools():
    return load_gist_json(TOOLS_FILE, DEFAULT_TOOLS.copy())

def save_tools(data):
    save_gist_json(TOOLS_FILE, data)

def get_licenses():
    return load_gist_json(LICENSES_FILE, {})

def save_licenses(data):
    save_gist_json(LICENSES_FILE, data)

def get_coupons():
    return load_gist_json(COUPONS_FILE, {})

def save_coupons(data):
    save_gist_json(COUPONS_FILE, data)

def get_orders():
    return load_gist_json(ORDERS_FILE, {})

def save_orders(data):
    save_gist_json(ORDERS_FILE, data)

def get_reminders():
    return load_gist_json(REMINDERS_FILE, {})

def save_reminders(data):
    save_gist_json(REMINDERS_FILE, data)

def ensure_user(user):
    users = get_users()
    uid = str(user.id)
    item = users.get(uid) or {
        "user_id": user.id,
        "username": getattr(user, "username", "") or "",
        "full_name": user_label(user),
        "created_at": iso_now(),
        "updated_at": iso_now(),
        "is_blocked": 0,
    }
    item["username"] = getattr(user, "username", "") or item.get("username", "")
    item["full_name"] = user_label(user)
    item["updated_at"] = iso_now()
    users[uid] = item
    save_users(users)
    return item

def list_tools():
    return [v for v in get_tools().values() if int(v.get("active", 1)) == 1]

def get_tool(code):
    return (get_tools() or {}).get(safe_upper(code))

def set_tool_price(code, price):
    data = get_tools()
    code = safe_upper(code)
    if code not in data:
        return False
    data[code]["price"] = int(price)
    save_tools(data)
    return True

def add_tool(code, name, price, description=""):
    data = get_tools()
    code = safe_upper(code)
    if code in data:
        raise ValueError("exists")
    data[code] = {
        "code": code,
        "name": name.strip(),
        "price": int(price),
        "description": description.strip(),
        "active": 1,
        "created_at": iso_now(),
    }
    save_tools(data)

def get_license_key(user_id, tool_code):
    return f"{int(user_id)}__{safe_upper(tool_code)}"

def get_user_licenses(user_id):
    data = get_licenses()
    out = []
    for _, v in data.items():
        if int(v.get("user_id", 0)) == int(user_id):
            out.append(v)
    out.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return out

def extend_license(user_id, tool_code, days, machine_id=None):
    licenses = get_licenses()
    key = get_license_key(user_id, tool_code)
    now = now_vn()
    item = licenses.get(key) or {
        "user_id": int(user_id),
        "tool_code": safe_upper(tool_code),
        "machine_id": norm_machine_id(machine_id) if machine_id else "",
        "created_at": iso_now(),
    }
    old = item.get("expires_at")
    if old:
        try:
            old_dt = datetime.fromisoformat(old)
            base = old_dt if old_dt > now else now
        except Exception:
            base = now
    else:
        base = now
    new_exp = base + timedelta(days=int(days))
    if machine_id:
        item["machine_id"] = norm_machine_id(machine_id)
    item["expires_at"] = new_exp.isoformat()
    item["status"] = "active"
    item["updated_at"] = iso_now()
    licenses[key] = item
    save_licenses(licenses)
    return new_exp

def add_coupon(code, discount_type, discount_value, max_uses, expires_at):
    coupons = get_coupons()
    code = safe_upper(code)
    if code in coupons:
        raise ValueError("exists")
    coupons[code] = {
        "code": code,
        "discount_type": discount_type,
        "discount_value": int(discount_value),
        "max_uses": int(max_uses),
        "used_count": 0,
        "expires_at": expires_at,
        "active": 1,
        "created_at": iso_now(),
    }
    save_coupons(coupons)

def get_coupon(code):
    return get_coupons().get(safe_upper(code))

def validate_coupon(user_id, code, base_price):
    coupon = get_coupon(code)
    if not coupon:
        return False, "Mã giảm giá không tồn tại.", 0
    if int(coupon.get("active", 1)) != 1:
        return False, "Mã giảm giá đã bị tắt.", 0
    if coupon.get("expires_at"):
        if datetime.fromisoformat(coupon["expires_at"]) < now_vn():
            return False, "Mã giảm giá đã hết hạn.", 0
    if int(coupon.get("used_count", 0)) >= int(coupon.get("max_uses", 1)):
        return False, "Mã giảm giá đã hết lượt dùng.", 0
    used_key = f"used_by_{int(user_id)}"
    if coupon.get(used_key):
        return False, "Bạn đã dùng mã này rồi.", 0
    if coupon["discount_type"] == "percent":
        discount = max(0, min(base_price, (base_price * int(coupon["discount_value"])) // 100))
    else:
        discount = max(0, min(base_price, int(coupon["discount_value"])))
    return True, "OK", discount

def mark_coupon_used(user_id, code):
    coupons = get_coupons()
    code = safe_upper(code)
    if code not in coupons:
        return
    coupons[code]["used_count"] = int(coupons[code].get("used_count", 0)) + 1
    coupons[code][f"used_by_{int(user_id)}"] = iso_now()
    save_coupons(coupons)

def payos_headers():
    return {"x-client-id": PAYOS_CLIENT_ID, "x-api-key": PAYOS_API_KEY, "Content-Type": "application/json"}

def sign_payos_payment_request(amount, order_code, description, cancel_url, return_url):
    raw = f"amount={amount}&cancelUrl={cancel_url}&description={description}&orderCode={order_code}&returnUrl={return_url}"
    return hmac.new(PAYOS_CHECKSUM_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()

def build_payos_order_code():
    return int(time.time() * 1000) % 900000000000 + 100000000000

def generate_qr(amount, payment_code):
    if BANK_ACCOUNT_NO:
        return f"https://img.vietqr.io/image/970436-{BANK_ACCOUNT_NO}-compact2.png?amount={int(amount)}&addInfo={urllib.parse.quote(payment_code)}"
    return ""

def create_payos_payment_link(amount, payment_code, product_name):
    payos_order_code = build_payos_order_code()
    description = payment_code[:25]
    result = {"payos_order_code": payos_order_code, "checkout_url": "", "qr_url": generate_qr(amount, payment_code), "qr_code": "", "provider": "fallback", "description": description}
    if not (PAYOS_CLIENT_ID and PAYOS_API_KEY and PAYOS_CHECKSUM_KEY and PAYOS_RETURN_URL and PAYOS_CANCEL_URL):
        return result
    payload = {
        "orderCode": payos_order_code,
        "amount": int(amount),
        "description": description,
        "items": [{"name": product_name[:25], "quantity": 1, "price": int(amount)}],
        "cancelUrl": PAYOS_CANCEL_URL,
        "returnUrl": PAYOS_RETURN_URL,
    }
    payload["signature"] = sign_payos_payment_request(int(amount), payos_order_code, description, PAYOS_CANCEL_URL, PAYOS_RETURN_URL)
    try:
        r = requests.post(f"{PAYOS_BASE_URL}/v2/payment-requests", headers=payos_headers(), json=payload, timeout=20)
        data = r.json()
        if r.ok and str(data.get("code")) == "00" and data.get("data"):
            info = data["data"]
            qr_raw = info.get("qrCode") or ""
            qr_img = f"https://api.qrserver.com/v1/create-qr-code/?size=512x512&data={urllib.parse.quote(qr_raw)}" if qr_raw else result["qr_url"]
            result.update({"checkout_url": info.get("checkoutUrl", ""), "qr_url": qr_img, "qr_code": qr_raw, "provider": "payos", "payment_link_id": info.get("paymentLinkId", "")})
    except Exception as e:
        print("create_payos_payment_link error:", e)
    return result

def get_payos_payment_status(order_code):
    if not (PAYOS_CLIENT_ID and PAYOS_API_KEY):
        return {"ok": False, "error": "payos_not_configured"}
    try:
        r = requests.get(f"{PAYOS_BASE_URL}/v2/payment-requests/{order_code}", headers=payos_headers(), timeout=20)
        data = r.json()
        if not r.ok:
            return {"ok": False, "error": data}
        return {"ok": True, "data": data.get("data") or {}}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def deep_sort_data(obj):
    if isinstance(obj, dict):
        return {k: deep_sort_data(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [deep_sort_data(x) for x in obj]
    return obj

def flatten_signature_data(data, prefix=""):
    pairs = []
    if isinstance(data, dict):
        for key in sorted(data.keys()):
            new_prefix = f"{prefix}.{key}" if prefix else key
            pairs.extend(flatten_signature_data(data[key], new_prefix))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            pairs.extend(flatten_signature_data(item, f"{prefix}[{idx}]"))
    else:
        value = "" if data is None else ("true" if data is True else ("false" if data is False else str(data)))
        pairs.append((prefix, value))
    return pairs

def verify_payos_webhook_signature(payload):
    signature = payload.get("signature")
    data = payload.get("data")
    if not signature or data is None or not PAYOS_CHECKSUM_KEY:
        return False
    sorted_data = deep_sort_data(data)
    pairs = flatten_signature_data(sorted_data)
    raw = "&".join(f"{k}={v}" for k, v in pairs)
    expected = hmac.new(PAYOS_CHECKSUM_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

def confirm_payos_webhook_url():
    if not (PAYOS_CLIENT_ID and PAYOS_API_KEY and PAYOS_WEBHOOK_URL):
        return
    try:
        r = requests.post(f"{PAYOS_BASE_URL}/confirm-webhook", headers=payos_headers(), json={"webhookUrl": PAYOS_WEBHOOK_URL}, timeout=20)
        print("confirm webhook:", r.status_code, r.text)
    except Exception as e:
        print("confirm webhook error:", e)

def create_order(user_id, username, full_name, tool_code, machine_id, months, base_price, coupon_code, discount_amount, final_price):
    payos = create_payos_payment_link(final_price, "ODTMP" + secrets.token_hex(3).upper(), (get_tool(tool_code) or {}).get("name", tool_code))
    order_code = "OD" + secrets.token_hex(4).upper()
    orders = get_orders()
    orders[order_code] = {
        "order_code": order_code,
        "payos_order_code": int(payos.get("payos_order_code") or 0) if payos.get("payos_order_code") else None,
        "user_id": int(user_id),
        "username": username or "",
        "full_name": full_name or "",
        "tool_code": safe_upper(tool_code),
        "machine_id": norm_machine_id(machine_id),
        "months": int(months),
        "base_price": int(base_price),
        "coupon_code": safe_upper(coupon_code) if coupon_code else None,
        "discount_amount": int(discount_amount),
        "final_price": int(final_price),
        "status": "pending",
        "payment_status": "unpaid",
        "payment_ref": None,
        "checkout_url": payos.get("checkout_url", ""),
        "qr_url": payos.get("qr_url", ""),
        "qr_code": payos.get("qr_code", ""),
        "payment_provider": payos.get("provider", "fallback"),
        "payment_description": payos.get("description", ""),
        "paid_at": None,
        "created_at": iso_now(),
        "updated_at": iso_now(),
    }
    save_orders(orders)
    return orders[order_code]

def get_order(order_code):
    return get_orders().get(safe_upper(order_code))

def get_order_by_payos_code(payos_code):
    for _, order in get_orders().items():
        if str(order.get("payos_order_code")) == str(payos_code):
            return order
    return None

def save_order(order):
    orders = get_orders()
    orders[safe_upper(order["order_code"])] = order
    save_orders(orders)

def approve_paid_order(order_code, payment_ref=None, source="payos"):
    order = get_order(order_code)
    if not order:
        return False, "Order không tồn tại."
    if order["status"] == "paid":
        return True, "Order đã duyệt trước đó."
    days = int(order["months"]) * 30
    new_exp = extend_license(order["user_id"], order["tool_code"], days, order["machine_id"])
    if order.get("coupon_code"):
        try:
            mark_coupon_used(order["user_id"], order["coupon_code"])
        except Exception:
            pass
    order["payment_status"] = "paid"
    order["status"] = "paid"
    order["payment_ref"] = payment_ref
    order["paid_at"] = iso_now()
    order["updated_at"] = iso_now()
    save_order(order)
    try:
        bot.send_message(order["user_id"], f"✅ <b>Thanh toán thành công</b>\nMã đơn: <code>{order['order_code']}</code>\nTool: <b>{order['tool_code']}</b>\nMachine ID: <code>{order['machine_id']}</code>\nThời hạn: <b>{order['months']}</b> tháng\nHết hạn mới: <b>{fmt_dt(new_exp.isoformat())}</b>\nRef: <code>{payment_ref or '-'}</code>", reply_markup=main_menu_markup(order["user_id"]))
    except Exception:
        pass
    notify_admins(f"💰 <b>Đơn đã tự duyệt</b>\nNguồn: <b>{source}</b>\nOrder: <code>{order['order_code']}</code>\nUser: <code>{order['user_id']}</code>\nTool: <b>{order['tool_code']}</b>\nMachine ID: <code>{order['machine_id']}</code>\nSố tháng: <b>{order['months']}</b>\nSố tiền: <b>{fmt_money(order['final_price'])}</b>\nRef: <code>{payment_ref or '-'}</code>\nHết hạn mới: <b>{fmt_dt(new_exp.isoformat())}</b>")
    return True, "OK"

def reminder_sent(user_id, tool_code, reminder_key):
    return bool(get_reminders().get(f"{int(user_id)}__{safe_upper(tool_code)}__{reminder_key}"))

def mark_reminder_sent(user_id, tool_code, reminder_key):
    data = get_reminders()
    data[f"{int(user_id)}__{safe_upper(tool_code)}__{reminder_key}"] = iso_now()
    save_reminders(data)

def process_expiry_reminders():
    rows = get_licenses().values()
    now = now_vn()
    admin_lines = []
    for r in rows:
        try:
            exp = datetime.fromisoformat(r["expires_at"])
        except Exception:
            continue
        delta_days = (exp.date() - now.date()).days
        if delta_days not in [7, 3, 1, 0]:
            continue
        reminder_key = f"{delta_days}_{now.date().isoformat()}"
        if reminder_sent(r["user_id"], r["tool_code"], reminder_key):
            continue
        tool = get_tool(r["tool_code"]) or {}
        tool_name = tool.get("name") or r["tool_code"]
        if delta_days > 0:
            user_text = f"⏰ <b>Nhắc hạn tool</b>\nTool: <b>{tool_name}</b>\nMachine ID: <code>{r.get('machine_id') or '-'}</code>\nCòn <b>{delta_days}</b> ngày sẽ hết hạn.\nHết hạn lúc: <b>{fmt_dt(r['expires_at'])}</b>"
        else:
            user_text = f"⚠️ <b>Tool đã hết hạn</b>\nTool: <b>{tool_name}</b>\nMachine ID: <code>{r.get('machine_id') or '-'}</code>\nHết hạn lúc: <b>{fmt_dt(r['expires_at'])}</b>"
        try:
            bot.send_message(int(r["user_id"]), user_text, reply_markup=main_menu_markup(int(r["user_id"])))
        except Exception:
            pass
        admin_lines.append(f"• User <code>{r['user_id']}</code> | {r['tool_code']} | còn {delta_days} ngày | hết hạn {fmt_dt(r['expires_at'])}")
        mark_reminder_sent(r["user_id"], r["tool_code"], reminder_key)
    if admin_lines:
        notify_admins("📋 <b>Danh sách user sắp hết hạn</b>\n" + "\n".join(admin_lines))

def reminder_loop():
    while True:
        try:
            process_expiry_reminders()
        except Exception as e:
            notify_admins(f"⚠️ Reminder loop lỗi: <code>{e}</code>")
        time.sleep(REMINDER_CHECK_INTERVAL_SECONDS)

def main_menu_markup(user_id):
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(types.InlineKeyboardButton("🛍 Mua tool", callback_data="menu_buy"), types.InlineKeyboardButton("📅 Hạn dùng của tôi", callback_data="menu_my"))
    mk.add(types.InlineKeyboardButton("🎁 Mã giảm giá", callback_data="menu_coupon_help"), types.InlineKeyboardButton("☎️ Liên hệ admin", callback_data="menu_contact"))
    if is_admin(user_id):
        mk.add(types.InlineKeyboardButton("🛠 Admin", callback_data="menu_admin"))
    return mk

def buy_menu_markup():
    mk = types.InlineKeyboardMarkup(row_width=1)
    for t in list_tools():
        mk.add(types.InlineKeyboardButton(f"{t['name']} • {fmt_money(t['price'])}/tháng", callback_data=f"buytool:{t['code']}"))
    mk.add(types.InlineKeyboardButton("⬅️ Về menu", callback_data="back_main"))
    return mk

def months_markup(tool_code):
    mk = types.InlineKeyboardMarkup(row_width=3)
    for m in [1, 3, 6, 12]:
        mk.add(types.InlineKeyboardButton(f"{m} tháng", callback_data=f"months:{tool_code}:{m}"))
    mk.add(types.InlineKeyboardButton("⬅️ Chọn tool khác", callback_data="menu_buy"))
    return mk

def coupon_decision_markup():
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(types.InlineKeyboardButton("🎁 Nhập mã giảm giá", callback_data="enter_coupon"), types.InlineKeyboardButton("➡️ Bỏ qua", callback_data="skip_coupon"))
    return mk

def payment_markup(order_code, checkout_url=""):
    mk = types.InlineKeyboardMarkup(row_width=1)
    if checkout_url:
        mk.add(types.InlineKeyboardButton("💳 Thanh toán qua payOS", url=checkout_url))
    mk.add(types.InlineKeyboardButton("🔄 Kiểm tra trạng thái", callback_data=f"checkorder:{order_code}"))
    mk.add(types.InlineKeyboardButton("⬅️ Về menu", callback_data="back_main"))
    return mk

def admin_menu_markup():
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(types.InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"), types.InlineKeyboardButton("⏰ Chạy nhắc hạn", callback_data="admin_run_remind"))
    mk.add(types.InlineKeyboardButton("⬅️ Về menu", callback_data="back_main"))
    return mk

def build_payment_text(order):
    transfer_note = f"{PAYMENT_NOTE_PREFIX} {order['order_code']}"
    lines = ["<b>Đơn hàng của bạn</b>", f"Mã đơn: <code>{order['order_code']}</code>", f"Tool: <b>{order['tool_code']}</b>", f"Số tháng: <b>{order['months']}</b>", f"Machine ID: <code>{order['machine_id']}</code>", f"Giá gốc: <b>{fmt_money(order['base_price'])}</b>", f"Giảm giá: <b>{fmt_money(order['discount_amount'])}</b>", f"Cần thanh toán: <b>{fmt_money(order['final_price'])}</b>"]
    if order["payment_provider"] == "payos":
        lines += ["", "Bấm nút bên dưới để thanh toán qua payOS. Sau khi thanh toán xong, hệ thống sẽ tự duyệt đơn."]
    else:
        lines += ["", "payOS chưa cấu hình đầy đủ, hiện bot dùng thông tin chuyển khoản thủ công."]
        if BANK_NAME and BANK_ACCOUNT_NO and BANK_ACCOUNT_NAME:
            lines += [f"Ngân hàng: <b>{BANK_NAME}</b>", f"Số tài khoản: <code>{BANK_ACCOUNT_NO}</code>", f"Chủ tài khoản: <b>{BANK_ACCOUNT_NAME}</b>", f"Nội dung CK: <code>{transfer_note}</code>"]
    return "\n".join(lines)

@bot.message_handler(commands=["start"])
def cmd_start(message):
    ensure_user(message.from_user)
    bot.send_message(message.chat.id, f"Xin chào <b>{user_label(message.from_user)}</b>\n\nBot này lưu users, tools, licenses, coupons, orders, reminders bằng <b>GitHub Gist JSON</b>.", reply_markup=main_menu_markup(message.from_user.id))

@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id, "<b>Lệnh user</b>\n/start\n/tools\n/mylicense\n\n<b>Lệnh admin</b>\n/addtool CODE | Tên tool | Giá | Mô tả\n/setprice CODE | Giá_mới\n/adduser user_id | TOOL_CODE | số_ngày | MACHINE_ID(optional)\n/extend user_id | TOOL_CODE | số_ngày | MACHINE_ID(optional)\n/coupon CODE | percent|fixed | value | max_uses | YYYY-MM-DD hoặc -\n/approve ORDER_CODE\n/broadcast nội dung\n/run_reminders\n")

@bot.message_handler(commands=["tools"])
def cmd_tools(message):
    tools = list_tools()
    if not tools:
        bot.send_message(message.chat.id, "Chưa có tool nào.", reply_markup=main_menu_markup(message.from_user.id))
        return
    lines = ["<b>Danh sách tool</b>"]
    for t in tools:
        desc = f"\n  {t['description']}" if t.get("description") else ""
        lines.append(f"• <b>{t['code']}</b> — {t['name']} — {fmt_money(t['price'])}/tháng{desc}")
    bot.send_message(message.chat.id, "\n".join(lines), reply_markup=buy_menu_markup())

@bot.message_handler(commands=["mylicense"])
def cmd_mylicense(message):
    rows = get_user_licenses(message.from_user.id)
    if not rows:
        bot.send_message(message.chat.id, "Bạn chưa có tool nào được kích hoạt.", reply_markup=main_menu_markup(message.from_user.id))
        return
    now = now_vn()
    lines = ["<b>Hạn dùng của bạn</b>"]
    for r in rows:
        exp = datetime.fromisoformat(r["expires_at"])
        remain = (exp.date() - now.date()).days
        status = "Còn hạn" if exp > now else "Hết hạn"
        tool = get_tool(r["tool_code"]) or {}
        name = tool.get("name") or r["tool_code"]
        lines.append(f"• <b>{name}</b> ({r['tool_code']})\n  Machine ID: <code>{r.get('machine_id') or '-'}</code>\n  Hết hạn: <b>{fmt_dt(r['expires_at'])}</b>\n  Trạng thái: <b>{status}</b> | Còn: <b>{remain}</b> ngày")
    bot.send_message(message.chat.id, "\n\n".join(lines), reply_markup=main_menu_markup(message.from_user.id))

@bot.callback_query_handler(func=lambda c: True)
def callbacks(call):
    user_id = call.from_user.id
    ensure_user(call.from_user)
    try:
        if call.data == "back_main":
            bot.edit_message_text("Chọn chức năng bên dưới:", call.message.chat.id, call.message.message_id, reply_markup=main_menu_markup(user_id))
            return
        if call.data == "menu_buy":
            bot.edit_message_text("<b>Chọn tool bạn muốn mua</b>", call.message.chat.id, call.message.message_id, reply_markup=buy_menu_markup())
            return
        if call.data == "menu_my":
            rows = get_user_licenses(user_id)
            if not rows:
                txt = "Bạn chưa có tool nào được kích hoạt."
            else:
                now = now_vn()
                parts = ["<b>Hạn dùng của bạn</b>"]
                for r in rows:
                    exp = datetime.fromisoformat(r["expires_at"])
                    remain = (exp.date() - now.date()).days
                    parts.append(f"• <b>{r['tool_code']}</b> | hết hạn {fmt_dt(r['expires_at'])} | còn {remain} ngày\n  Machine ID: <code>{r.get('machine_id') or '-'}</code>")
                txt = "\n\n".join(parts)
            bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=main_menu_markup(user_id))
            return
        if call.data == "menu_coupon_help":
            bot.send_message(call.message.chat.id, "Khi mua tool, bot sẽ hỏi mã giảm giá trước khi tới bước thanh toán.")
            return
        if call.data == "menu_contact":
            bot.send_message(call.message.chat.id, "Nhắn trực tiếp admin để được hỗ trợ nhanh.")
            return
        if call.data == "menu_admin":
            if not is_admin(user_id):
                return
            bot.edit_message_text("<b>Khu vực admin</b>", call.message.chat.id, call.message.message_id, reply_markup=admin_menu_markup())
            return
        if call.data == "admin_broadcast":
            if not is_admin(user_id):
                return
            msg = bot.send_message(call.message.chat.id, "Gửi nội dung broadcast ngay sau tin nhắn này:")
            bot.register_next_step_handler(msg, handle_admin_broadcast)
            return
        if call.data == "admin_run_remind":
            if not is_admin(user_id):
                return
            process_expiry_reminders()
            bot.send_message(call.message.chat.id, "Đã chạy nhắc hạn xong.")
            return
        if call.data.startswith("buytool:"):
            tool_code = call.data.split(":", 1)[1]
            tool = get_tool(tool_code)
            if not tool:
                return
            bot.edit_message_text(f"<b>{tool['name']}</b>\nGiá: <b>{fmt_money(tool['price'])}/tháng</b>\n\nChọn thời hạn:", call.message.chat.id, call.message.message_id, reply_markup=months_markup(tool['code']))
            return
        if call.data.startswith("months:"):
            _, tool_code, months = call.data.split(":")
            tool = get_tool(tool_code)
            if not tool:
                return
            BUY_STATE[user_id] = {"tool_code": tool_code, "months": int(months), "coupon_code": None, "machine_id": None}
            msg = bot.send_message(call.message.chat.id, f"Bạn đã chọn <b>{tool['name']}</b> — <b>{months}</b> tháng.\n\nVui lòng nhập <b>Machine ID</b>.\nVí dụ:\n<code>Machine ID=B8A8334E67D60DCE1D38FFE40CDA3F1F</code>")
            bot.register_next_step_handler(msg, handle_machine_id_step)
            return
        if call.data == "enter_coupon":
            msg = bot.send_message(call.message.chat.id, "Nhập mã giảm giá:")
            bot.register_next_step_handler(msg, handle_coupon_step)
            return
        if call.data == "skip_coupon":
            create_order_and_show_payment(call.message.chat.id, user_id, call.from_user)
            return
        if call.data.startswith("checkorder:"):
            order_code = call.data.split(":", 1)[1]
            order = get_order(order_code)
            if not order or int(order["user_id"]) != int(user_id):
                return
            if order["status"] != "paid" and order.get("payos_order_code"):
                status_resp = get_payos_payment_status(order["payos_order_code"])
                if status_resp.get("ok"):
                    st = str((status_resp.get("data") or {}).get("status") or "").upper()
                    amount_paid = int((status_resp.get("data") or {}).get("amountPaid") or 0)
                    if st == "PAID" or amount_paid >= int(order["final_price"]):
                        approve_paid_order(order["order_code"], payment_ref="manual_check", source="payos_status_sync")
                        order = get_order(order_code)
            txt = f"Mã đơn: <code>{order['order_code']}</code>\nTrạng thái đơn: <b>{order['status']}</b>\nTrạng thái thanh toán: <b>{order['payment_status']}</b>\nTool: <b>{order['tool_code']}</b>\nMachine ID: <code>{order['machine_id']}</code>\nSố tiền: <b>{fmt_money(order['final_price'])}</b>"
            bot.send_message(call.message.chat.id, txt, reply_markup=payment_markup(order_code, order.get("checkout_url", "")))
            return
    except Exception as e:
        bot.send_message(call.message.chat.id, f"Lỗi xử lý callback: <code>{e}</code>")

def handle_machine_id_step(message):
    user_id = message.from_user.id
    state = BUY_STATE.get(user_id)
    if not state:
        bot.send_message(message.chat.id, "Flow mua hàng đã hết hạn.", reply_markup=buy_menu_markup())
        return
    machine_id = norm_machine_id(message.text)
    if not is_valid_machine_id(machine_id):
        msg = bot.send_message(message.chat.id, "Machine ID chưa hợp lệ.\nVí dụ:\n<code>B8A8334E67D60DCE1D38FFE40CDA3F1F</code>\n\nNhập lại:")
        bot.register_next_step_handler(msg, handle_machine_id_step)
        return
    state["machine_id"] = machine_id
    BUY_STATE[user_id] = state
    bot.send_message(message.chat.id, f"✅ Đã nhận Machine ID:\n<code>{machine_id}</code>\n\nBạn có muốn nhập mã giảm giá không?", reply_markup=coupon_decision_markup())

def handle_coupon_step(message):
    user_id = message.from_user.id
    state = BUY_STATE.get(user_id)
    if not state:
        bot.send_message(message.chat.id, "Flow mua hàng đã hết hạn.")
        return
    code = safe_upper(message.text)
    tool = get_tool(state["tool_code"])
    base_price = int(tool["price"]) * int(state["months"])
    ok, msg, discount = validate_coupon(user_id, code, base_price)
    if not ok:
        msg_obj = bot.send_message(message.chat.id, f"❌ {msg}\nNhập lại mã khác hoặc gửi <code>SKIP</code> để bỏ qua:")
        bot.register_next_step_handler(msg_obj, handle_coupon_retry_step)
        return
    state["coupon_code"] = code
    BUY_STATE[user_id] = state
    bot.send_message(message.chat.id, f"✅ Áp dụng mã <b>{code}</b> thành công.\nGiảm: <b>{fmt_money(discount)}</b>")
    create_order_and_show_payment(message.chat.id, user_id, message.from_user)

def handle_coupon_retry_step(message):
    if message.text.strip().upper() == "SKIP":
        create_order_and_show_payment(message.chat.id, message.from_user.id, message.from_user)
        return
    handle_coupon_step(message)

def create_order_and_show_payment(chat_id, user_id, user_obj):
    state = BUY_STATE.get(user_id)
    if not state:
        bot.send_message(chat_id, "Flow mua hàng đã hết hạn.")
        return
    tool = get_tool(state["tool_code"])
    if not tool:
        bot.send_message(chat_id, "Tool không còn tồn tại.")
        return
    base_price = int(tool["price"]) * int(state["months"])
    discount_amount = 0
    if state.get("coupon_code"):
        ok, msg, discount_amount = validate_coupon(user_id, state["coupon_code"], base_price)
        if not ok:
            state["coupon_code"] = None
            BUY_STATE[user_id] = state
            bot.send_message(chat_id, f"Mã giảm giá không còn hợp lệ: {msg}")
            discount_amount = 0
    final_price = max(0, base_price - discount_amount)
    order = create_order(user_id, getattr(user_obj, "username", "") or "", user_label(user_obj), tool["code"], state["machine_id"], state["months"], base_price, state.get("coupon_code"), discount_amount, final_price)
    bot.send_message(chat_id, build_payment_text(order), reply_markup=payment_markup(order["order_code"], order.get("checkout_url", "")))
    notify_admins(f"🧾 Đơn mới chờ thanh toán\nOrder: <code>{order['order_code']}</code>\nUser: <code>{user_id}</code>\nTool: <b>{order['tool_code']}</b>\nTháng: <b>{order['months']}</b>\nMachine ID: <code>{order['machine_id']}</code>\nCoupon: <b>{order.get('coupon_code') or 'Không'}</b>\nTổng tiền: <b>{fmt_money(order['final_price'])}</b>")
    BUY_STATE.pop(user_id, None)

@bot.message_handler(commands=["addtool"])
def cmd_addtool(message):
    if not admin_only(message):
        return
    raw = message.text.replace("/addtool", "", 1).strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) < 3:
        bot.reply_to(message, "Ví dụ:\n/addtool GROKTOOL | Tool Auto Grok | 50000 | Mô tả")
        return
    code, name, price_s = parts[:3]
    desc = parts[3] if len(parts) >= 4 else ""
    if not price_s.isdigit():
        bot.reply_to(message, "Giá phải là số.")
        return
    try:
        add_tool(code, name, int(price_s), desc)
        bot.reply_to(message, f"Đã thêm tool <b>{safe_upper(code)}</b>.")
    except ValueError:
        bot.reply_to(message, "Mã tool đã tồn tại.")

@bot.message_handler(commands=["setprice"])
def cmd_setprice(message):
    if not admin_only(message):
        return
    raw = message.text.replace("/setprice", "", 1).strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(message, "Ví dụ:\n/setprice GROKTOOL | 59000")
        return
    ok = set_tool_price(parts[0], int(parts[1]))
    bot.reply_to(message, "Đã cập nhật giá." if ok else "Không tìm thấy tool.")

@bot.message_handler(commands=["adduser"])
def cmd_adduser(message):
    if not admin_only(message):
        return
    raw = message.text.replace("/adduser", "", 1).strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) < 3:
        bot.reply_to(message, "Ví dụ:\n/adduser 123456789 | GROKTOOL | 30 | B8A8334E67D60DCE1D38FFE40CDA3F1F")
        return
    user_id_s, tool_code, days_s = parts[:3]
    machine_id = parts[3] if len(parts) >= 4 else None
    if not user_id_s.isdigit() or not re.fullmatch(r"-?\d+", days_s):
        bot.reply_to(message, "user_id và số ngày phải hợp lệ.")
        return
    if machine_id and not is_valid_machine_id(machine_id):
        bot.reply_to(message, "Machine ID không hợp lệ.")
        return
    if not get_tool(tool_code):
        bot.reply_to(message, "Tool code không tồn tại.")
        return
    new_exp = extend_license(int(user_id_s), tool_code, int(days_s), machine_id)
    bot.reply_to(message, f"Đã cấp user <code>{user_id_s}</code> tới <b>{fmt_dt(new_exp.isoformat())}</b>.")

@bot.message_handler(commands=["extend"])
def cmd_extend(message):
    if not admin_only(message):
        return
    raw = message.text.replace("/extend", "", 1).strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) < 3:
        bot.reply_to(message, "Ví dụ:\n/extend 123456789 | GROKTOOL | 30 | B8A8334E67D60DCE1D38FFE40CDA3F1F")
        return
    user_id_s, tool_code, days_s = parts[:3]
    machine_id = parts[3] if len(parts) >= 4 else None
    if not user_id_s.isdigit() or not re.fullmatch(r"-?\d+", days_s):
        bot.reply_to(message, "user_id và số ngày phải hợp lệ.")
        return
    if machine_id and not is_valid_machine_id(machine_id):
        bot.reply_to(message, "Machine ID không hợp lệ.")
        return
    if not get_tool(tool_code):
        bot.reply_to(message, "Tool code không tồn tại.")
        return
    new_exp = extend_license(int(user_id_s), tool_code, int(days_s), machine_id)
    bot.reply_to(message, f"Đã gia hạn tới <b>{fmt_dt(new_exp.isoformat())}</b>.")

@bot.message_handler(commands=["coupon"])
def cmd_coupon(message):
    if not admin_only(message):
        return
    raw = message.text.replace("/coupon", "", 1).strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) != 5:
        bot.reply_to(message, "Ví dụ:\n/coupon SALE10 | percent | 10 | 100 | 2026-12-31")
        return
    code, dtype, value_s, max_uses_s, exp_date = parts
    dtype = dtype.lower()
    if dtype not in {"percent", "fixed"}:
        bot.reply_to(message, "discount_type chỉ nhận percent hoặc fixed.")
        return
    if not value_s.isdigit() or not max_uses_s.isdigit():
        bot.reply_to(message, "value và max_uses phải là số.")
        return
    expires_at = None
    if exp_date != "-":
        try:
            expires_at = datetime.strptime(exp_date, "%Y-%m-%d").replace(tzinfo=TZ, hour=23, minute=59, second=59).isoformat()
        except ValueError:
            bot.reply_to(message, "Ngày hết hạn phải dạng YYYY-MM-DD hoặc dùng -")
            return
    try:
        add_coupon(code, dtype, int(value_s), int(max_uses_s), expires_at)
        bot.reply_to(message, f"Đã tạo coupon <b>{safe_upper(code)}</b>.")
    except ValueError:
        bot.reply_to(message, "Coupon đã tồn tại.")

@bot.message_handler(commands=["approve"])
def cmd_approve(message):
    if not admin_only(message):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "Ví dụ: /approve ODAB12CD")
        return
    ok, msg = approve_paid_order(parts[1], source="admin")
    bot.reply_to(message, msg)

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    if not admin_only(message):
        return
    content = message.text.replace("/broadcast", "", 1).strip()
    if not content:
        bot.reply_to(message, "Ví dụ:\n/broadcast Shop đang có ưu đãi")
        return
    sent = 0
    failed = 0
    for uid, u in get_users().items():
        if int(u.get("is_blocked", 0)) == 1:
            continue
        try:
            bot.send_message(int(uid), content)
            sent += 1
        except Exception:
            failed += 1
    bot.reply_to(message, f"Broadcast xong.\nThành công: {sent}\nThất bại: {failed}")

def handle_admin_broadcast(message):
    if not admin_only(message):
        return
    sent = 0
    failed = 0
    for uid, u in get_users().items():
        if int(u.get("is_blocked", 0)) == 1:
            continue
        try:
            bot.send_message(int(uid), message.text)
            sent += 1
        except Exception:
            failed += 1
    bot.send_message(message.chat.id, f"Broadcast xong.\nThành công: {sent}\nThất bại: {failed}")

@bot.message_handler(commands=["run_reminders"])
def cmd_run_reminders(message):
    if not admin_only(message):
        return
    process_expiry_reminders()
    bot.reply_to(message, "Đã chạy nhắc hạn xong.")

@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(message):
    ensure_user(message.from_user)
    bot.send_message(message.chat.id, "Chọn menu bên dưới hoặc dùng /help để xem lệnh.", reply_markup=main_menu_markup(message.from_user.id))

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "running", "storage": "github_gist", "gist_id": GIST_ID, "gist_owner": GIST_OWNER, "payos_webhook_path": PAYOS_WEBHOOK_PATH, "time": iso_now()})

@app.route(PAYOS_WEBHOOK_PATH, methods=["POST"])
def payos_webhook():
    payload = request.get_json(silent=True) or {}
    if not verify_payos_webhook_signature(payload):
        return jsonify({"error": 1, "message": "invalid signature"}), 400
    data = payload.get("data") or {}
    payos_order_code = data.get("orderCode")
    amount = int(data.get("amount") or data.get("amountPaid") or 0)
    payment_ref = str(data.get("reference") or data.get("paymentLinkId") or data.get("code") or "")
    desc = str(data.get("description") or "")
    status = str(data.get("status") or "").upper()
    order = get_order_by_payos_code(payos_order_code)
    if not order:
        return jsonify({"error": 0, "message": "order not found but webhook accepted"})
    if order["status"] == "paid":
        return jsonify({"error": 0, "message": "already paid"})
    if status == "PAID" or amount >= int(order["final_price"]) or (desc and order["order_code"][:8] in desc):
        ok, msg = approve_paid_order(order["order_code"], payment_ref=payment_ref, source="payos_webhook")
        return jsonify({"error": 0 if ok else 1, "message": msg})
    order["payment_status"] = status.lower() if status else "pending"
    order["payment_ref"] = payment_ref
    order["updated_at"] = iso_now()
    save_order(order)
    return jsonify({"error": 0, "message": f"status updated {status or 'PENDING'}"})

@app.route("/payment-return", methods=["GET"])
def payment_return():
    order_code = request.args.get("order_code", "")
    order = get_order(order_code) if order_code else None
    if not order:
        return "Order không tồn tại.", 404
    return f"Đơn {order['order_code']} | trạng thái {order['status']} | payment {order['payment_status']}"

@app.route("/payment-cancel", methods=["GET"])
def payment_cancel():
    return "Thanh toán đã bị hủy."

def run_flask():
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    bootstrap_gist()
    confirm_payos_webhook_url()
    notify_admins("🤖 Bot Gist storage đang khởi động.")
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()
    bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=60)
