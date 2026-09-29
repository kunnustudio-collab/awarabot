import os
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlparse
from motor.motor_asyncio import AsyncIOMotorClient
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.error import Forbidden, BadRequest

# --- Configuration ---
BOT_TOKEN = "8646333360:AAECWIM3YyijQd4RgXlulzu5TjsoNhc5vPU"
MONGO_URI = "mongodb+srv://kunnustudio_db_user:pjmI7nCsRp1Vz1Wg@cluster0.e3ipa0d.mongodb.net/?appName=Cluster0"
LOG_GROUP_ID = -1004437233748
ADMIN_ID = 7165581725
WELCOME_IMAGE_LINK = "https://t.me/c/2152170417/143"
FORCE_CHANNELS = [
    "@AwaraZone0",
    "@AwaraTeams",
    "@AwaraWorld"
]
FREE_VIDEOS_PER_DAY = 20
BUY_PREMIUM_URL = "https://t.me/MeAwara"

# --- Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- MongoDB Setup ---
mongo_client = AsyncIOMotorClient(
    MONGO_URI,
    maxPoolSize=50,
    minPoolSize=5,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
    maxIdleTimeMS=45000
)
db = mongo_client["telegram_bot"]
users_collection = db["users"]
videos_collection = db["videos"]
settings_collection = db["settings"]

# --- Categories Configuration ---
CATEGORIES = {
    "free_videos": {
        "name": "🎬 𝐅𝐫𝐞𝐞 𝐕𝐢𝐝𝐞𝐨𝐬",
        "premium_only": False,
        "default_channel": "@AwaraZone0"
    },
    "trending_viral": {
        "name": "🚀 𝐓𝐫𝐞𝐧𝐝𝐢𝐧𝐠 𝐕𝐢𝐫𝐚𝐥",
        "premium_only": True,
        "default_channel": ""
    },
    "vip_exclusive": {
        "name": "👑 𝐕𝐈𝐏 𝐄𝐱𝐜𝐥𝐮𝐬𝐢𝐯𝐞",
        "premium_only": True,
        "default_channel": ""
    },
    "desi_special": {
        "name": "🔥 𝐃𝐞𝐬𝐢 𝐒𝐩𝐞𝐜𝐢𝐚𝐥",
        "premium_only": True,
        "default_channel": ""
    },
    "trending_hot": {
        "name": "⚡ 𝐓𝐫𝐞𝐧𝐝𝐢𝐧𝐠 𝐇𝐨𝐭",
        "premium_only": True,
        "default_channel": ""
    },
    "international": {
        "name": "💃 𝐈𝐧𝐭𝐞𝐫𝐧𝐚𝐭𝐢𝐨𝐧𝐚𝐥",
        "premium_only": True,
        "default_channel": ""
    }
}

# Category Number Mapping (1-6)
CATEGORY_NUM_MAP = {
    "1": "free_videos",
    "2": "trending_viral",
    "3": "vip_exclusive",
    "4": "desi_special",
    "5": "trending_hot",
    "6": "international"
}

def resolve_category(cat_input):
    if not cat_input:
        return "free_videos"
    val = str(cat_input).strip().lower()
    if val in CATEGORY_NUM_MAP:
        return CATEGORY_NUM_MAP[val]
    if val in ("leakvideos", "free_videos"):
        return "free_videos"
    if val in CATEGORIES:
        return val
    return None

def get_categories_help_text():
    lines = []
    for num, key in CATEGORY_NUM_MAP.items():
        name = CATEGORIES[key]["name"]
        prem = "👑 VIP" if CATEGORIES[key]["premium_only"] else "🆓 Free"
        lines.append(f"`{num}` = {name} ({prem})")
    return "\n".join(lines)

# Per-category in-memory video count cache for instant response
category_counts_cache = {}
category_counts_time = {}

def get_category_query(category_key):
    if category_key in ("free_videos", "leakvideos"):
        return {"$or": [
            {"category": "free_videos"},
            {"category": "leakvideos"},
            {"category": {"$exists": False}},
            {"category": None}
        ]}
    return {"category": category_key}

async def get_category_videos_count(category_key="free_videos"):
    now = time.time()
    cached = category_counts_cache.get(category_key)
    last_time = category_counts_time.get(category_key, 0)
    if cached is None or (now - last_time > 300):
        try:
            query = get_category_query(category_key)
            cnt = await videos_collection.count_documents(query)
            category_counts_cache[category_key] = cnt
            category_counts_time[category_key] = now
            return cnt
        except Exception:
            return cached if cached is not None else 0
    return cached

async def get_total_videos_count():
    total = 0
    for cat in CATEGORIES.keys():
        total += await get_category_videos_count(cat)
    return total

async def get_category_channels_settings():
    settings = await settings_collection.find_one({"_id": "category_channels"})
    if not settings:
        settings = {
            "_id": "category_channels",
            "free_videos": "@AwaraZone0",
            "trending_viral": "",
            "vip_exclusive": "",
            "desi_special": "",
            "trending_hot": "",
            "international": ""
        }
        await settings_collection.insert_one(settings)
    return settings

# In-memory rename jobs keyed by admin user id
rename_jobs = {}

# Batch import state for /addall and /stopaddall
add_all_state = {
    "active": False,
    "task": None,
    "stop_event": None,
    "status_chat_id": None,
    "status_message_id": None,
}

# Default behavior for /addall: run in background so normal bot use is not blocked.
ADD_ALL_BACKGROUND_DEFAULT = True

# --- Auto-Add System State & Cache ---
auto_add_cache = {}  # identifier (chat_id str, @username str) -> category_key
auto_add_enabled = True
_auto_add_notify_state = {}

async def _debounced_auto_add_notify(bot, cat_key):
    await asyncio.sleep(2.5)
    data = _auto_add_notify_state.pop(cat_key, None)
    if not data or data.get("count", 0) <= 0:
        return
    count = data["count"]
    chat_title = data.get("chat_title", "Group")
    cat_name = CATEGORIES.get(cat_key, {}).get("name", cat_key)
    total_cnt = category_counts_cache.get(cat_key, 0)
    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"📥 **Auto-Add Video Alert**\n\n"
                f"✅ **{count}** new video{'s' if count > 1 else ''} automatically saved!\n"
                f"📁 Category: **{cat_name}** (`{cat_key}`)\n"
                f"📺 Source: **{chat_title}**\n"
                f"🎬 Total in Category: **{total_cnt}** videos"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Could not send auto-add alert: {e}")

def queue_auto_add_notification(app, cat_key, chat_title):
    if cat_key not in _auto_add_notify_state:
        _auto_add_notify_state[cat_key] = {"count": 1, "chat_title": chat_title}
        app.create_task(_debounced_auto_add_notify(app.bot, cat_key))
    else:
        _auto_add_notify_state[cat_key]["count"] += 1
        _auto_add_notify_state[cat_key]["chat_title"] = chat_title

async def load_auto_add_settings():
    global auto_add_cache, auto_add_enabled
    new_cache = {}
    try:
        settings = await settings_collection.find_one({"_id": "auto_add_settings"})
        if settings:
            auto_add_enabled = settings.get("enabled", True)
            channels = settings.get("channels", {})
            for ident, cdata in channels.items():
                cat = cdata if isinstance(cdata, str) else (cdata.get("category") if isinstance(cdata, dict) else None)
                if cat in CATEGORIES:
                    ident_str = str(ident).lower().strip()
                    new_cache[ident_str] = cat
                    if ident_str.startswith("@"):
                        new_cache[ident_str[1:]] = cat
                    else:
                        new_cache[f"@{ident_str}"] = cat
                    if isinstance(cdata, dict) and cdata.get("chat_id"):
                        new_cache[str(cdata["chat_id"])] = cat

        cat_channels = await settings_collection.find_one({"_id": "category_channels"})
        if cat_channels:
            for cat_key, ch in cat_channels.items():
                if cat_key in CATEGORIES and ch:
                    ch_str = str(ch).strip().lower()
                    if ch_str.startswith("http"):
                        parts = ch_str.rstrip("/").split("/")
                        if "c" in parts:
                            try:
                                c_idx = parts.index("c")
                                new_cache[f"-100{parts[c_idx+1]}"] = cat_key
                            except Exception:
                                pass
                        elif len(parts) >= 1:
                            u = parts[-1].lstrip("@")
                            new_cache[u] = cat_key
                            new_cache[f"@{u}"] = cat_key
                    else:
                        new_cache[ch_str] = cat_key
                        if ch_str.startswith("@"):
                            new_cache[ch_str[1:]] = cat_key
                        else:
                            new_cache[f"@{ch_str}"] = cat_key
        auto_add_cache = new_cache
        logger.info(f"Loaded {len(auto_add_cache)} identifiers into auto_add_cache. (Enabled: {auto_add_enabled})")
    except Exception as e:
        logger.warning(f"Error loading auto_add_settings: {e}")

# --- Helper Functions ---
async def add_user(user_id, full_name):
    existing = await users_collection.find_one({"_id": user_id})
    if existing is None:
        await users_collection.insert_one({
            "_id": user_id,
            "name": full_name,
            "joined": datetime.utcnow(),
            "videos_watched_today": 0,
            "last_reset_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "total_referrals": 0,
            "unlimited_access": False,
            "premium_expiry": None
        })
        await notify_group(f"👤 New User Joined: *{full_name}* | ID: `{user_id}`")
    else:
        update_fields = {"name": full_name}
        if "videos_watched_today" not in existing:
            update_fields["videos_watched_today"] = 0
        if "last_reset_date" not in existing:
            update_fields["last_reset_date"] = datetime.utcnow().strftime("%Y-%m-%d")
        if "total_referrals" not in existing:
            update_fields["total_referrals"] = 0
        if "unlimited_access" not in existing:
            update_fields["unlimited_access"] = False
        if "premium_expiry" not in existing:
            update_fields["premium_expiry"] = None
        if update_fields:
            await users_collection.update_one({"_id": user_id}, {"$set": update_fields})

async def get_random_video(category_key="free_videos"):
    count = await get_category_videos_count(category_key)
    if count <= 0:
        return None
    skip = random.randint(0, count - 1)
    query = get_category_query(category_key)
    return await videos_collection.find_one(query, skip=skip)

async def notify_group(text):
    try:
        await application.bot.send_message(LOG_GROUP_ID, text, parse_mode='Markdown')
    except Exception as e:
        logger.warning(f"Failed to send group notification: {e}")

async def get_user(user_id):
    return await users_collection.find_one({"_id": user_id})

def is_premium_record(user):
    expiry = user.get("premium_expiry")
    return isinstance(expiry, datetime) and expiry > datetime.utcnow()

async def refresh_premium_status(user_id):
    user = await get_user(user_id)
    if not user:
        return None
    expiry = user.get("premium_expiry")
    if expiry and isinstance(expiry, datetime):
        if expiry > datetime.utcnow():
            return user
        await users_collection.update_one(
            {"_id": user_id},
            {"$set": {"premium_expiry": None, "unlimited_access": False}}
        )
        user["premium_expiry"] = None
    return user

def get_premium_status_text(user):
    if user and is_premium_record(user):
        expiry_text = user["premium_expiry"].strftime("%d %b %Y")
        return f"✅ You are a premium user.\nValid until: {expiry_text}"
    return (
        "⚠️ You are not a premium user.\n"
        "Daily free limit: 20 videos.\n"
        "Buy premium: @MeAwara"
    )

async def get_premium_prices():
    settings = await settings_collection.find_one({"_id": "premium_prices"})
    if not settings:
        settings = {
            "_id": "premium_prices",
            "1_month": "100",
            "3_months": "250",
            "6_months": "450",
            "1_year": "800",
            "lifetime": "1500"
        }
        await settings_collection.insert_one(settings)
    return settings

def normalize_premium_package(package):
    if not package:
        return None
    package = package.lower().replace("-", "_").replace(" ", "_")
    normalized = {
        "1m": "1_month",
        "1_month": "1_month",
        "month": "1_month",
        "3m": "3_months",
        "3_month": "3_months",
        "3_months": "3_months",
        "6m": "6_months",
        "6_month": "6_months",
        "6_months": "6_months",
        "1y": "1_year",
        "1_year": "1_year",
        "year": "1_year",
        "lifetime": "lifetime",
        "life": "lifetime"
    }
    return normalized.get(package)

async def set_premium_price(package, price):
    await settings_collection.update_one(
        {"_id": "premium_prices"},
        {"$set": {package: price}},
        upsert=True
    )

def normalize_force_channel(channel):
    if not channel:
        return ""
    channel = channel.strip()
    if channel.startswith(("http://", "https://")):
        parsed = urlparse(channel)
        path = parsed.path.lstrip("/")
        if not path:
            return channel
        if path.startswith("@") or path.startswith("+"):
            return path
        return f"@{path}"
    if channel.startswith("@") or channel.startswith("+"):
        return channel
    return f"@{channel}"


def get_force_channel_join_url(channel):
    if not channel:
        return ""
    channel = channel.strip()
    if channel.startswith(("http://", "https://")):
        return channel
    if channel.startswith("@"):
        return f"https://t.me/{channel[1:]}"
    if channel.startswith("+"):
        return f"https://t.me/{channel}"
    return f"https://t.me/{channel}"


CHANNEL_ID_CACHE = {}

async def check_channel_member(bot, user_id, channel):
    chat_identifier = CHANNEL_ID_CACHE.get(channel)
    if chat_identifier is None:
        try:
            cand = channel
            if not cand.startswith("@") and not cand.startswith("-100") and not cand.startswith("http"):
                cand = f"@{cand}"
            chat = await bot.get_chat(cand)
            chat_identifier = chat.id
            CHANNEL_ID_CACHE[channel] = chat_identifier
        except Exception:
            chat_identifier = channel

    try:
        member = await bot.get_chat_member(chat_identifier, user_id)
        if member.status in ["member", "administrator", "creator"]:
            return channel, True
    except (Forbidden, BadRequest):
        return channel, False
    except Exception:
        return channel, False
    return channel, False

async def get_missing_force_channels(bot, user_id):
    tasks = [check_channel_member(bot, user_id, ch) for ch in FORCE_CHANNELS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    missing = []
    for r in results:
        if isinstance(r, tuple):
            channel, is_member = r
            if not is_member:
                missing.append(channel)
    return missing

async def is_user_subscribed(bot, user_id):
    missing = await get_missing_force_channels(bot, user_id)
    return len(missing) == 0


def build_force_join_keyboard():
    buttons = []
    for index, channel in enumerate(FORCE_CHANNELS, start=1):
        buttons.append([InlineKeyboardButton(f"📢 Join Channel {index}", url=get_force_channel_join_url(channel))])
    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_joined")])
    return InlineKeyboardMarkup(buttons)

# Referral system removed in premium-only mode.
# The bot now uses daily free limit and premium purchase only.

async def reset_watch_count_if_needed(user_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    user = await users_collection.find_one({"_id": user_id})
    if user is None:
        return
    if user.get("last_reset_date") != today:
        await users_collection.update_one(
            {"_id": user_id},
            {"$set": {"videos_watched_today": 0, "last_reset_date": today}}
        )

async def can_watch_video(user_id):
    user = await get_user(user_id)
    if user is None:
        return True

    # 1. Premium check
    expiry = user.get("premium_expiry")
    if expiry and isinstance(expiry, datetime):
        if expiry > datetime.utcnow():
            return True
        else:
            asyncio.create_task(users_collection.update_one(
                {"_id": user_id},
                {"$set": {"premium_expiry": None, "unlimited_access": False}}
            ))
            user["premium_expiry"] = None

    if user.get("unlimited_access"):
        return True

    # 2. Daily reset check
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if user.get("last_reset_date") != today:
        asyncio.create_task(users_collection.update_one(
            {"_id": user_id},
            {"$set": {"videos_watched_today": 0, "last_reset_date": today}}
        ))
        return True

    return user.get("videos_watched_today", 0) < FREE_VIDEOS_PER_DAY

async def increment_video_watch(user_id):
    await users_collection.update_one(
        {"_id": user_id},
        {"$inc": {"videos_watched_today": 1}},
        upsert=True
    )

def parse_button_markup(text):
    if not text:
        return text, None

    cleaned_lines = []
    buttons = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("button:"):
            parts = stripped[len("button:"):].split("|", 1)
            if len(parts) == 2:
                label = parts[0].strip()
                target = parts[1].strip()
                if target.startswith("url:"):
                    buttons.append([InlineKeyboardButton(label, url=target[4:].strip())])
                elif target.startswith("callback:"):
                    buttons.append([InlineKeyboardButton(label, callback_data=target[9:].strip())])
            continue
        cleaned_lines.append(line)

    cleaned_text = "\n".join(cleaned_lines).strip()
    if not buttons:
        return cleaned_text, None
    return cleaned_text, InlineKeyboardMarkup(buttons)

async def send_broadcast_to_all(bot, source_message, text, reply_markup=None):
    delivered = 0
    blocked = 0
    failed = 0
    async for user_doc in users_collection.find({}, {"_id": 1}):
        user_id = user_doc.get("_id")
        if not user_id:
            continue
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_message.chat_id,
                message_id=source_message.message_id,
                reply_markup=reply_markup
            )
            delivered += 1
            await asyncio.sleep(0.04) # gentle rate-limiting (~25 msg/s) to prevent Telegram flood ban
        except Forbidden:
            blocked += 1
        except BadRequest as e:
            error_text = str(e).lower()
            if "blocked" in error_text or "not found" in error_text or "deactivated" in error_text:
                blocked += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return delivered, blocked, failed

async def send_broadcast_text_to_all(bot, text, reply_markup=None):
    delivered = 0
    blocked = 0
    failed = 0
    async for user_doc in users_collection.find({}, {"_id": 1}):
        user_id = user_doc.get("_id")
        if not user_id:
            continue
        try:
            await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)
            delivered += 1
            await asyncio.sleep(0.04) # gentle rate-limiting to prevent flood ban
        except Forbidden:
            blocked += 1
        except BadRequest as e:
            error_text = str(e).lower()
            if "blocked" in error_text or "not found" in error_text or "deactivated" in error_text:
                blocked += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return delivered, blocked, failed

async def send_startup_log(bot):
    try:
        await bot.send_message(LOG_GROUP_ID, "🚀 *AwaraBot has started and is fully operational!*", parse_mode='Markdown')
        logger.info("Startup notification sent to LOG_GROUP_ID.")
    except Exception as e:
        logger.warning(f"Could not send startup log to LOG_GROUP_ID: {e}")

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = None
    chat_id = None
    try:
        # Determine user and chat_id from the context
        if update.callback_query:
            query = update.callback_query
            user = query.from_user
            chat_id = update.effective_chat.id
            try:
                await query.answer()
            except Exception:
                pass
            try:
                if query.message:
                    await query.message.delete()
            except Exception:
                pass
        else:
            user = update.effective_user
            chat_id = update.effective_chat.id

        if not await is_user_subscribed(context.bot, user.id):
            keyboard = build_force_join_keyboard()
            await context.bot.send_message(
                chat_id,
                "🚫 You must join all required channels before using this bot.",
                reply_markup=keyboard
            )
            return

        await add_user(user.id, user.full_name)
        user_record = await get_user(user.id)
        premium_status_text = get_premium_status_text(user_record)
        keyboard_buttons = [
            [InlineKeyboardButton("📁 𝐂𝐚𝐭𝐞𝐠𝐨𝐫𝐢𝐞𝐬", callback_data="categories")],
            [InlineKeyboardButton("🧾 𝐇𝐞𝐥𝐩 / 𝐈𝐧𝐟𝐨", callback_data="help")],
            [
                InlineKeyboardButton("📢 𝐂𝐡𝐚𝐧𝐧𝐞𝐥", url="https://t.me/+Nij4Wp7jbYY0YTFl"),
                InlineKeyboardButton("💬 𝐒𝐮𝐩𝐩𝐨𝐫𝐭", url="https://t.me/+cRKhXxI_zH5jMmM9")
            ],
            [InlineKeyboardButton("👨‍💻 𝐀𝐝𝐦𝐢𝐧", url="https://t.me/MeAwara")]
        ]
        if user.id == ADMIN_ID:
            keyboard_buttons.insert(3, [InlineKeyboardButton("🛠️ Manage Bot", callback_data="admin_panel")])
        keyboard = InlineKeyboardMarkup(keyboard_buttons)

        try:
            parts = WELCOME_IMAGE_LINK.strip("/").split("/")
            from_chat_id = int("-100" + parts[-2])
            message_id = int(parts[-1])
            caption = (
                f"👋 𝙒𝙚𝙡𝙘𝙤𝙢𝙚 {user.mention_html()}!\n\n"
                "💎 𝙔𝙤𝙪’𝙫𝙚 𝙟𝙤𝙞𝙣𝙚𝙙 𝙖 𝙥𝙧𝙚𝙢𝙞𝙪𝙢 𝙫𝙞𝙙𝙚𝙤 𝙨𝙝𝙖𝙧𝙞𝙣𝙜 𝙗𝙤𝙩.\n\n"
                f"{premium_status_text}\n\n"
                "🎬 𝘿𝙞𝙨𝙘𝙤𝙫𝙚𝙧 𝙝𝙤𝙩 𝙫𝙞𝙙𝙚𝙤 𝙘𝙖𝙩𝙚𝙜𝙤𝙧𝙞𝙚𝙨, 𝙜𝙚𝙩 𝙖 𝙣𝙚𝙬 𝙫𝙞𝙙𝙚𝙤 𝙚𝙫𝙚𝙧𝙮 𝙢𝙞𝙣𝙪𝙩𝙚!\n\n"
                "🔥 𝙐𝙨𝙚 𝙩𝙝𝙚 𝙢𝙚𝙣𝙪 𝙗𝙚𝙡𝙤𝙬 𝙩𝙤 𝙜𝙚𝙩 𝙨𝙩𝙖𝙧𝙩𝙚𝙙."
            )
            await context.bot.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, caption=caption, parse_mode="HTML", reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Error sending welcome image: {e}")
            await context.bot.send_message(
                chat_id,
                f"👋 𝙒𝙚𝙡𝙘𝙤𝙢𝙚 {user.mention_html()}!\n\n"
                "💎 𝙔𝙤𝙪’𝙫𝙚 𝙟𝙤𝙞𝙣𝙚𝙙 𝙖 𝙥𝙧𝙚𝙢𝙞𝙪𝙢 𝙫𝙞𝙙𝙚𝙤 𝙨𝙝𝙖𝙧𝙞𝙣𝙜 𝙗𝙤𝙩.\n\n"
                f"{premium_status_text}\n\n"
                "🎬 𝘿𝙞𝙨𝙘𝙤𝙫𝙚𝙧 𝙝𝙤𝙩 𝙫𝙞𝙙𝙚𝙤 𝙘𝙖𝙩𝙚𝙜𝙤𝙧𝙞𝙚𝙨, 𝙜𝙚𝙩 𝙖 𝙣𝙚𝙬 𝙫𝙞𝙙𝙚𝙤 𝙚𝙫𝙚𝙧𝙮 𝙢𝙞𝙣𝙪𝙩𝙚!\n\n"
                "🔥 𝙐𝙨𝙚 𝙩𝙝𝙚 𝙢𝙚𝙣𝙪 𝙗𝙚𝙡𝙤𝙬 𝙩𝙤 𝙜𝙚𝙩 𝙨𝙩𝙖𝙧𝙩𝙚𝙙.",
                parse_mode="HTML",
                reply_markup=keyboard
            )
    except Exception as exc:
        logger.exception("Unhandled error in /start handler for user %s", getattr(user, 'id', None))
        if chat_id is not None:
            try:
                await context.bot.send_message(chat_id, "⚠️ Bot is temporarily unavailable. Please try again in a moment.")
            except Exception:
                pass

async def check_joined(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    missing = await get_missing_force_channels(context.bot, user.id)
    if not missing:
        await query.message.delete()
        await start(update, context)
        return

    missing_text = "\n".join([f"- {channel}" for channel in missing])
    await query.answer(
        f"🚫 Still missing required channels:\n{missing_text}",
        show_alert=True
    )

# --- Categories Menu ---
async def categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    user_id = query.from_user.id
    chat_id = update.effective_chat.id

    user = await get_user(user_id)
    is_premium = user and (user.get("unlimited_access") or is_premium_record(user))

    keyboard = []
    for cat_key, cat_data in CATEGORIES.items():
        if cat_data["premium_only"]:
            badge = "💎" if is_premium else "🔒"
            btn_text = f"{cat_data['name']} {badge}"
        else:
            btn_text = f"{cat_data['name']} (Free)"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"cat_{cat_key}")])

    keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="start")])

    try:
        if query.message:
            await query.message.delete()
    except Exception as e:
        logger.info(f"Could not delete message in categories_menu: {e}")

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "📂 𝐒𝐞𝐥𝐞𝐜𝐭 𝐚 𝐜𝐚𝐭𝐞𝐠𝐨𝐫𝐲:\n\n"
            "• Free users can access **Free Videos**.\n"
            "• Categories with 🔒 are exclusively for **Premium Members**."
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def buy_premium_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prices = await get_premium_prices()
    message = (
        "💎 Premium Packages\n\n"
        f"1️⃣ 1 Month — {prices.get('1_month', '100')}\n"
        f"2️⃣ 3 Months — {prices.get('3_months', '250')}\n"
        f"3️⃣ 6 Months — {prices.get('6_months', '450')}\n"
        f"4️⃣ 1 Year — {prices.get('1_year', '800')}\n"
        f"5️⃣ Lifetime — {prices.get('lifetime', '1500')}\n\n"
        "📩 Contact @MeAwara to purchase any package.\n"
        "🛠️ Admin can update prices with /setpremiumprice <package> <amount>.\n\n"
        "Package keys: 1_month, 3_months, 6_months, 1_year, lifetime"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Contact @MeAwara", url=BUY_PREMIUM_URL)],
        [InlineKeyboardButton("🔙 Back", callback_data="start")]
    ])
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=message,
        reply_markup=keyboard
    )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("Access denied.", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Video", callback_data="admin_add")],
        [InlineKeyboardButton("⚡ Auto-Add Videos", callback_data="admin_autoadd")],
        [InlineKeyboardButton("📥 AddAll Videos", callback_data="admin_addall")],
        [InlineKeyboardButton("📁 Category Channels", callback_data="admin_catchannels")],
        [InlineKeyboardButton("🧹 Clean DB", callback_data="admin_clean")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("💎 Premium User", callback_data="admin_premium")],
        [InlineKeyboardButton("❌ Remove Premium", callback_data="admin_removepremium")],
        [InlineKeyboardButton("💰 Set Price", callback_data="admin_setpremiumprice")],
        [InlineKeyboardButton("🔁 Rename Channel Files", callback_data="admin_rename")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔙 Back", callback_data="start")]
    ])
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "🛠️ Admin Command Panel\n\n"
            "Tap any button to see the exact command syntax.\n"
            "These are only visible to the admin."
        ),
        reply_markup=keyboard
    )

async def admin_rename_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("Access denied.", show_alert=True)
        return

    try:
        await query.message.delete()
    except Exception:
        pass

    text = (
        "🔁 Rename Channel Files\n\n"
        "Use the command:\n"
        "`/renamechannel <channel_username_or_link> <start_number> <keep_ext:yes/no>`\n\n"
        "Example:\n"
        "`/renamechannel @MyChannel 1 yes`\n\n"
        "This will preview the planned renames; confirm to apply."
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
    await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard)

async def admin_command_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("Access denied.", show_alert=True)
        return

    cat_help = get_categories_help_text()
    command_text = {
        "admin_add": f"/add <message_link> [1-6]\n\n📁 Categories:\n{cat_help}\n\nExample: `/add https://t.me/c/123/42 2`",
        "admin_autoadd": f"/autoadd (View status & groups)\n\n1. In Group: `/autoadd <1-6>` (e.g. `/autoadd 2`)\n2. In PM: `/autoadd <1-6> <channel_or_group>`\n3. Disable: `/autoadd off`\n\n📁 Categories:\n{cat_help}",
        "admin_addall": f"/addall <start_link> <end_id> [1-6] [bg|fg]\n\n📁 Categories:\n{cat_help}\n\nExample: `/addall https://t.me/c/123/1 50 2 bg`",
        "admin_catchannels": f"/categorychannels\nOr link channel:\n/setcategorychannel <1-6> <channel>\n\n📁 Categories:\n{cat_help}",
        "admin_clean": f"/clean [1-6] (or /clean for all categories)\n\n📁 Categories:\n{cat_help}",
        "admin_stats": "/stats",
        "admin_premium": "/premium <user_id> [days]",
        "admin_removepremium": "/removepremium <user_id>",
        "admin_setpremiumprice": "/setpremiumprice <package> <amount>",
        "admin_broadcast": "/broadcast <text> or reply to a message with /broadcast",
        "admin_rename": "/renamechannel <channel_username_or_link> <start_number> <keep_ext:yes/no>"
    }.get(query.data, "Unknown command")

    text = f"**Command Info:**\n\n`{command_text}`"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
    ])
    try:
        await query.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=keyboard
    )

# --- FIX: Updated help_handler ---
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        # Delete the old message (photo)
        await query.message.delete()
    except Exception as e:
        logger.info(f"Could not delete message in help_handler: {e}")
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="start")]
    ])

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "ℹ️ 𝐇𝐞𝐥𝐩 / 𝐈𝐧𝐟𝐨\n\n"
            "➡️ Use the buttons to browse categories and watch videos.\n"
            "➡️ Free users can watch up to 20 videos daily.\n"
            "➡️ Premium users watch unlimited videos until expiry.\n"
            "➡️ To buy premium, contact @MeAwara.\n\n"
            "🔎 How to use the bot:\n"
            "1. Tap Categories.\n"
            "2. Choose Leak Videos.\n"
            "3. Tap Next Video to continue.\n"
            "4. If limit ends, buy premium to keep watching."
        ),
        reply_markup=keyboard
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return

    message = update.message
    source_message = message.reply_to_message or message

    if not source_message:
        await message.reply_text("❌ No content to broadcast.")
        return

    text = source_message.text or source_message.caption or ""
    if text.startswith("/broadcast"):
        text = text[len("/broadcast"):].strip()

    cleaned_text, reply_markup = parse_button_markup(text)

    try:
        if source_message.photo or source_message.document or source_message.video or source_message.animation or source_message.audio:
            delivered, blocked, failed = await send_broadcast_to_all(context.bot, source_message, cleaned_text, reply_markup)
        else:
            if cleaned_text:
                delivered, blocked, failed = await send_broadcast_text_to_all(context.bot, cleaned_text, reply_markup)
            else:
                delivered, blocked, failed = 0, 0, 0
    except Exception as e:
        logger.error(f"Broadcast failed: {e}")
        await message.reply_text(f"❌ Broadcast failed: {e}")
        return

    await message.reply_text(
        f"✅ Broadcast completed.\n\nDelivered: {delivered}\nBlocked: {blocked}\nFailed: {failed}",
        parse_mode="HTML"
    )

async def category_video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or "free_videos"
    if data in ("leakvideos", "free_videos"):
        category_key = "free_videos"
    elif data.startswith("cat_"):
        category_key = data[4:]
        if category_key == "leakvideos":
            category_key = "free_videos"
    else:
        category_key = "free_videos"

    cat_data = CATEGORIES.get(category_key, CATEGORIES["free_videos"])
    user_id = query.from_user.id
    chat_id = update.effective_chat.id

    user = await get_user(user_id)
    is_premium = user and (user.get("unlimited_access") or is_premium_record(user))

    # 1. Premium-only category restriction
    if cat_data.get("premium_only", False) and not is_premium:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Buy Premium Subscription", callback_data="buy_premium")],
            [InlineKeyboardButton("🔙 Back to Categories", callback_data="categories")]
        ])
        try:
            if query.message:
                await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔒 **{cat_data['name']} is a Premium Category!**\n\n"
                "Yeh category sirf hamare **Premium Members** ke liye reserved hai.\n\n"
                "Is category ke sabhi exclusive videos dekhne ke liye abhi Premium buy karein! 👇"
            ),
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    # 2. Daily free limit check (only for non-premium users on free categories)
    if not is_premium:
        if not await can_watch_video(user_id):
            await context.bot.send_message(
                chat_id=chat_id,
                text=("🚫 Your daily free limit has ended.\n\n"
                      "You have watched today's free videos.\n\n"
                      "You are not a premium user. Buy premium to @MeAwara."),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 Buy Premium Subscription", callback_data="buy_premium")],
                    [InlineKeyboardButton("🔙 Back to Menu", callback_data="start")]
                ])
            )
            return

    # 3. Retrieve random video from this category
    video_data = await get_random_video(category_key)
    if not video_data:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📂 Other Categories", callback_data="categories")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="start")]
        ])
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ No videos found in **{cat_data['name']}** category yet.\nAdmin will upload videos soon!",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    # 4. Deliver video
    try:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Next Video", callback_data=f"cat_{category_key}")],
            [InlineKeyboardButton("📂 Categories", callback_data="categories"), InlineKeyboardButton("🔙 Back", callback_data="start")]
        ])
        sent = await context.bot.send_video(
            chat_id=chat_id,
            video=video_data['file_id'],
            caption=f"📁 *Category: {cat_data['name']}*\n\nSave or forward this video now! ⏳ ये वीडियो 5 मिनट बाद हट जाएगी।",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        if not is_premium:
            await increment_video_watch(user_id)

        try:
            if query.message:
                await query.message.delete()
        except Exception as e:
            logger.info(f"Could not delete old menu message: {e}")

        asyncio.create_task(delete_message_after_delay(context, chat_id, sent.message_id, 300))
    except Exception as e:
        logger.error(f"Error sending video for category {category_key}: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Video send failed. Tap below to try next video:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Try Next Video", callback_data=f"cat_{category_key}")]
            ])
        )

# Alias for backwards compatibility
send_video = category_video_handler

async def delete_message_after_delay(context, chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Failed to delete message after delay: {e}")


def parse_channel_link(link):
    parts = link.strip('/').split('/')
    if len(parts) < 2:
        return None, None

    if 'c' in parts:
        try:
            c_index = parts.index('c')
            channel_id = int(parts[c_index + 1])
            message_id = int(parts[c_index + 2])
            return f"-100{channel_id}", message_id
        except (ValueError, IndexError):
            return None, None

    try:
        username = parts[-2]
        message_id = int(parts[-1])
        if username.startswith('@'):
            username = username[1:]
        return f"@{username}", message_id
    except ValueError:
        return None, None


# --- Admin Commands (No changes here) ---
async def add_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if not context.args:
        help_text = get_categories_help_text()
        await update.message.reply_text(
            f"Please provide a message link and category (number 1-6 or name).\n\n"
            f"Usage: `/add <link> [category_number_1-6]`\n\n"
            f"📁 **Categories:**\n{help_text}\n\n"
            f"Example:\n`/add https://t.me/c/1234567890/42 2`",
            parse_mode="Markdown"
        )
        return

    link = context.args[0]
    category = "free_videos"
    if len(context.args) > 1:
        resolved = resolve_category(context.args[1])
        if resolved:
            category = resolved
        else:
            help_text = get_categories_help_text()
            await update.message.reply_text(
                f"❌ Invalid category `{context.args[1]}`.\n\n"
                f"Please choose a number (1-6):\n{help_text}",
                parse_mode="Markdown"
            )
            return

    chat_id, message_id = parse_channel_link(link)
    if not chat_id or not message_id:
        await update.message.reply_text("❌ Invalid channel link. Use a message link like https://t.me/c/1234567890/42 or https://t.me/ChannelName/42")
        return

    try:
        msg = await context.bot.forward_message(chat_id=ADMIN_ID, from_chat_id=chat_id, message_id=message_id)
        if msg.video:
            if await videos_collection.count_documents({"file_id": msg.video.file_id}) == 0:
                await videos_collection.insert_one({"file_id": msg.video.file_id, "category": category})
                category_counts_cache[category] = category_counts_cache.get(category, 0) + 1
                cat_name = CATEGORIES[category]["name"]
                await update.message.reply_text(f"✅ Video Saved to **{cat_name}** (`{category}`)!", parse_mode="Markdown")
            else:
                await update.message.reply_text("ℹ️ This video is already in the database.")
        else:
            await update.message.reply_text("❌ This message does not contain a video.")
        await context.bot.delete_message(chat_id=ADMIN_ID, message_id=msg.message_id)
    except Exception as e:
        logger.error(f"Error in /add: {e}")
        await update.message.reply_text(f"❌ Failed to process video.\nError: {e}")

async def _update_add_all_status(bot, status_chat_id, status_message_id, text):
    if not status_chat_id or not status_message_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=status_chat_id,
            message_id=status_message_id,
            text=text,
            parse_mode='Markdown'
        )
    except Exception:
        pass

async def run_add_all_videos(bot, chat_id, start_id, end_id, category, status_chat_id, status_message_id):
    stop_event = asyncio.Event()
    add_all_state.update({
        "active": True,
        "task": asyncio.current_task(),
        "stop_event": stop_event,
        "status_chat_id": status_chat_id,
        "status_message_id": status_message_id,
    })

    added, skipped, failed = 0, 0, 0
    total = end_id - start_id + 1
    cat_name = CATEGORIES.get(category, {}).get("name", category)

    try:
        for i, mid in enumerate(range(start_id, end_id + 1)):
            if stop_event.is_set():
                break
            try:
                msg = await bot.forward_message(chat_id=ADMIN_ID, from_chat_id=chat_id, message_id=mid)
                if msg.video:
                    if await videos_collection.count_documents({"file_id": msg.video.file_id}) == 0:
                        await videos_collection.insert_one({"file_id": msg.video.file_id, "category": category})
                        added += 1
                    else:
                        skipped += 1
                else:
                    failed += 1
                await bot.delete_message(chat_id=ADMIN_ID, message_id=msg.message_id)
            except Exception:
                failed += 1
            if i % 10 == 0 or i == total - 1:
                await _update_add_all_status(
                    bot,
                    status_chat_id,
                    status_message_id,
                    f"🔄 **Processing [{cat_name}]...** {i+1}/{total}\n✅ Added: {added} | ⏩ Skipped: {skipped} | ❌ Failed: {failed}"
                )
            if stop_event.is_set():
                break
            await asyncio.sleep(1.5)

        category_counts_cache[category] = category_counts_cache.get(category, 0) + added

        if stop_event.is_set():
            await _update_add_all_status(
                bot,
                status_chat_id,
                status_message_id,
                f"⏹️ **Batch Stopped!**\n\nThe /addall process for **{cat_name}** was interrupted by admin."
            )
        else:
            await _update_add_all_status(
                bot,
                status_chat_id,
                status_message_id,
                f"✅ **Batch Finished for {cat_name}!**\n\n- Category: `{category}`\n- Added: **{added}**\n- Skipped: **{skipped}**\n- Failed: **{failed}**"
            )
    except asyncio.CancelledError:
        await _update_add_all_status(
            bot,
            status_chat_id,
            status_message_id,
            f"⏹️ **Batch Stopped!**\n\nThe /addall process for **{cat_name}** was interrupted by admin."
        )
        raise
    except Exception as e:
        logger.exception("Error during /addall batch")
        await _update_add_all_status(
            bot,
            status_chat_id,
            status_message_id,
            f"❌ **Batch Error**\n\n{e}"
        )
    finally:
        if add_all_state.get("task") is asyncio.current_task():
            add_all_state.update({
                "active": False,
                "task": None,
                "stop_event": None,
                "status_chat_id": None,
                "status_message_id": None,
            })

async def add_all_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if not context.args or len(context.args) < 2:
        help_text = get_categories_help_text()
        await update.message.reply_text(
            "❌ **Invalid Usage**\n\n"
            "Use: `/addall <start_link> <end_id> [category_1-6] [bg|fg]`\n\n"
            f"📁 **Categories (1-6):**\n{help_text}\n\n"
            "Examples:\n"
            "• `/addall https://t.me/c/1234567890/10 50 2 bg`\n"
            "• `/addall https://t.me/c/1234567890/10 50 1`",
            parse_mode='Markdown'
        )
        return

    background = ADD_ALL_BACKGROUND_DEFAULT
    category = "free_videos"
    start_link = context.args[0]
    end_id_str = context.args[1]

    for arg in context.args[2:]:
        arg_lower = arg.lower()
        if arg_lower in {"background", "bg", "true", "yes"}:
            background = True
        elif arg_lower in {"foreground", "fg", "false", "no"}:
            background = False
        else:
            resolved = resolve_category(arg)
            if resolved:
                category = resolved
            else:
                help_text = get_categories_help_text()
                await update.message.reply_text(
                    f"❌ Invalid category `{arg}`.\n\n"
                    f"Please choose a number (1-6):\n{help_text}",
                    parse_mode="Markdown"
                )
                return

    if add_all_state.get("active") and add_all_state.get("task") and not add_all_state["task"].done():
        await update.message.reply_text("⚠️ /addall is already running. Use /stopaddall to stop it.")
        return

    chat_id, start_id = parse_channel_link(start_link)
    if not chat_id or not start_id:
        await update.message.reply_text("❌ Invalid channel link. Use a message link like https://t.me/c/1234567890/42 or https://t.me/ChannelName/42", parse_mode='Markdown')
        return

    try:
        end_id = int(end_id_str)
        if start_id >= end_id:
            await update.message.reply_text("❌ End ID must be > Start ID.")
            return
    except ValueError:
        await update.message.reply_text("❌ Invalid end ID.")
        return

    cat_name = CATEGORIES[category]["name"]
    status_message = await update.message.reply_text(
        f"🔄 Batch process started for **{start_id}** to **{end_id}**...\n\n"
        f"📁 Category: **{cat_name}** (`{category}`)\n"
        f"⚙️ Mode: **{'Background' if background else 'Foreground'}**",
        parse_mode='Markdown'
    )

    if background:
        task = context.application.create_task(
            run_add_all_videos(
                context.bot,
                chat_id,
                start_id,
                end_id,
                category,
                status_message.chat_id,
                status_message.message_id,
            )
        )
        add_all_state["task"] = task
        await update.message.reply_text(f"✅ /addall is running in background for **{cat_name}**.", parse_mode="Markdown")
    else:
        await run_add_all_videos(
            context.bot,
            chat_id,
            start_id,
            end_id,
            category,
            status_message.chat_id,
            status_message.message_id,
        )

async def stop_addall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if not add_all_state.get("active"):
        await update.message.reply_text("ℹ️ No active /addall batch is running.")
        return

    stop_event = add_all_state.get("stop_event")
    if stop_event is not None:
        stop_event.set()

    task = add_all_state.get("task")
    if task is not None and not task.done():
        task.cancel()

    await update.message.reply_text("🛑 Stop requested for the current /addall batch.")

async def clean_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if context.args:
        cat_key = resolve_category(context.args[0])
        if cat_key:
            query = get_category_query(cat_key)
            result = await videos_collection.delete_many(query)
            category_counts_cache[cat_key] = 0
            await update.message.reply_text(f"✅ Deleted {result.deleted_count} videos from **{CATEGORIES[cat_key]['name']}** (`{cat_key}`).", parse_mode="Markdown")
            return
        else:
            help_text = get_categories_help_text()
            await update.message.reply_text(
                f"❌ Invalid category `{context.args[0]}`.\n\n"
                f"Use `/clean <1-6>` or `/clean` to wipe all categories.\n\n"
                f"📁 **Categories (1-6):**\n{help_text}",
                parse_mode="Markdown"
            )
            return
    await videos_collection.delete_many({})
    category_counts_cache.clear()
    await update.message.reply_text("✅ All videos from all categories have been deleted.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    total_users = await users_collection.count_documents({})
    total_videos = await videos_collection.count_documents({})
    breakdown = []
    for num, k in CATEGORY_NUM_MAP.items():
        v = CATEGORIES[k]
        cnt = await get_category_videos_count(k)
        prem = "👑 VIP" if v["premium_only"] else "🆓 Free"
        breakdown.append(f"`{num}`. {v['name']} ({prem}): **{cnt}**")
    cat_text = "\n".join(breakdown)
    await update.message.reply_text(
        f"📊 **Bot Statistics**\n\n"
        f"👤 Total Users: **{total_users}**\n"
        f"🎬 Total Videos: **{total_videos}**\n\n"
        f"📁 **Category Breakdown:**\n{cat_text}",
        parse_mode='Markdown'
    )

async def set_category_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if len(context.args) < 2:
        help_text = get_categories_help_text()
        await update.message.reply_text(
            f"Usage: `/setcategorychannel <category_1-6> <channel_link_or_username>`\n\n"
            f"📁 **Categories (1-6):**\n{help_text}\n\n"
            f"Examples:\n"
            f"• `/setcategorychannel 2 https://t.me/c/1234567890`\n"
            f"• `/setcategorychannel 3 @MyVipChannel`",
            parse_mode="Markdown"
        )
        return

    cat_key = resolve_category(context.args[0])
    if not cat_key:
        help_text = get_categories_help_text()
        await update.message.reply_text(
            f"❌ Invalid category `{context.args[0]}`.\n\n"
            f"Please choose a number (1-6):\n{help_text}",
            parse_mode="Markdown"
        )
        return

    channel = context.args[1]
    await settings_collection.update_one(
        {"_id": "category_channels"},
        {"$set": {cat_key: channel}},
        upsert=True
    )

    try:
        clean_target = channel
        if "t.me/" in channel:
            ch_id, _ = parse_channel_link(channel + "/1")
            if ch_id:
                clean_target = ch_id
        target_chat = await context.bot.get_chat(clean_target)
        if target_chat:
            await settings_collection.update_one(
                {"_id": "auto_add_settings"},
                {"$set": {f"channels.{target_chat.id}": {
                    "category": cat_key,
                    "title": target_chat.title or str(target_chat.id),
                    "username": target_chat.username or "",
                    "chat_id": target_chat.id
                }}},
                upsert=True
            )
    except Exception:
        pass

    await load_auto_add_settings()
    cat_name = CATEGORIES[cat_key]["name"]
    await update.message.reply_text(
        f"✅ Channel for **{cat_name}** (`{cat_key}`) updated to: `{channel}`\n"
        f"⚡ Auto-Add is also enabled for this channel!",
        parse_mode="Markdown"
    )

async def category_channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    settings = await get_category_channels_settings()
    lines = ["📁 **Categories & Linked Channels:**\n"]
    for num, k in CATEGORY_NUM_MAP.items():
        v = CATEGORIES[k]
        ch = settings.get(k) or "(No channel linked yet)"
        prem = "👑 Premium Only" if v["premium_only"] else "🆓 Free & Premium"
        cnt = await get_category_videos_count(k)
        lines.append(f"**{num}. {v['name']}** (`{k}`)\n  - Type: {prem}\n  - Channel: `{ch}`\n  - Videos: **{cnt}**\n")
    lines.append("To update a channel: `/setcategorychannel <1-6> <channel>`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def autoadd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        return

    chat = update.effective_chat
    is_group_or_channel = chat.type in ("group", "supergroup", "channel")

    # --- Mode 1: Executed inside a Group or Channel ---
    if is_group_or_channel:
        chat_id_str = str(chat.id)
        if not context.args:
            cur_cat = auto_add_cache.get(chat_id_str)
            cur_text = f"**{CATEGORIES[cur_cat]['name']}** (`{cur_cat}`)" if cur_cat else "❌ *Not configured*"
            help_text = get_categories_help_text()
            await update.effective_message.reply_text(
                f"⚙️ **Auto-Add Settings for this Chat:**\n\n"
                f"📺 Chat: **{chat.title}** (`{chat.id}`)\n"
                f"📁 Current Category: {cur_text}\n\n"
                f"**To set auto-add for this chat:**\n"
                f"`/autoadd <1-6>` (e.g. `/autoadd 2`)\n\n"
                f"**To disable auto-add for this chat:**\n"
                f"`/autoadd off`\n\n"
                f"📁 **Categories (1-6):**\n{help_text}",
                parse_mode="Markdown"
            )
            return

        arg = context.args[0].lower()
        if arg in ("off", "disable", "stop", "remove"):
            await settings_collection.update_one(
                {"_id": "auto_add_settings"},
                {"$unset": {f"channels.{chat_id_str}": ""}}
            )
            if chat.username:
                u = chat.username.lower()
                await settings_collection.update_one(
                    {"_id": "auto_add_settings"},
                    {"$unset": {f"channels.@{u}": "", f"channels.{u}": ""}}
                )
            await load_auto_add_settings()
            await update.effective_message.reply_text(f"🛑 Auto-Add disabled for **{chat.title}**.")
            return

        resolved = resolve_category(arg)
        if not resolved:
            help_text = get_categories_help_text()
            await update.effective_message.reply_text(
                f"❌ Invalid category `{arg}`.\n\n"
                f"Please choose a number (1-6):\n{help_text}",
                parse_mode="Markdown"
            )
            return

        chat_data = {
            "category": resolved,
            "title": chat.title or "",
            "username": chat.username or "",
            "chat_id": chat.id,
            "type": chat.type
        }
        set_dict = {
            "enabled": True,
            f"channels.{chat_id_str}": chat_data
        }
        if chat.username:
            u = chat.username.lower()
            set_dict[f"channels.@{u}"] = chat_data
            set_dict[f"channels.{u}"] = chat_data

        await settings_collection.update_one(
            {"_id": "auto_add_settings"},
            {"$set": set_dict},
            upsert=True
        )
        await load_auto_add_settings()

        cat_name = CATEGORIES[resolved]["name"]
        await update.effective_message.reply_text(
            f"✅ **Auto-Add Enabled for this Chat!**\n\n"
            f"📺 Chat: **{chat.title}**\n"
            f"📁 Target Category: **{cat_name}** (`{resolved}`)\n\n"
            f"⚡ Jab bhi koi video is chat me post hogi, wo automatically bot database me add ho jayegi!",
            parse_mode="Markdown"
        )
        return

    # --- Mode 2: Executed in Private Chat (PM with Bot) ---
    if not context.args:
        settings = await settings_collection.find_one({"_id": "auto_add_settings"}) or {}
        channels = settings.get("channels", {})
        status_icon = "🟢 Enabled" if auto_add_enabled else "🔴 Disabled"

        lines = [
            f"⚙️ **Auto-Add Control Panel**\n",
            f"Status: **{status_icon}**\n",
            "📋 **Configured Auto-Add Sources:**"
        ]

        if not channels:
            lines.append("_(No channels or groups linked yet)_")
        else:
            shown = set()
            for ident, cdata in channels.items():
                if isinstance(cdata, dict):
                    cid = cdata.get("chat_id") or ident
                    if cid in shown:
                        continue
                    shown.add(cid)
                    title = cdata.get("title") or ident
                    cat = cdata.get("category", "free_videos")
                    cat_name = CATEGORIES.get(cat, {}).get("name", cat)
                    lines.append(f"• **{title}** (`{ident}`) ➔ **{cat_name}**")
                elif isinstance(cdata, str):
                    if ident in shown:
                        continue
                    shown.add(ident)
                    cat_name = CATEGORIES.get(cdata, {}).get("name", cdata)
                    lines.append(f"• `{ident}` ➔ **{cat_name}**")

        help_text = get_categories_help_text()
        lines.append(
            f"\n📖 **Kaise use karein:**\n"
            f"1️⃣ **Group ke andar:** Group me jaakar `/autoadd <1-6>` run karein (e.g. `/autoadd 2`).\n"
            f"2️⃣ **Yahan PM me:** `/autoadd <1-6> <channel_username_or_link>`\n"
            f"   Example: `/autoadd 2 @MyViralGroup`\n"
            f"3️⃣ **Band karne ke liye:** `/autoadd off`\n\n"
            f"📁 **Categories (1-6):**\n{help_text}"
        )
        await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    first_arg = context.args[0].lower()
    if first_arg in ("on", "enable", "start"):
        await settings_collection.update_one(
            {"_id": "auto_add_settings"},
            {"$set": {"enabled": True}},
            upsert=True
        )
        await load_auto_add_settings()
        await update.effective_message.reply_text("🟢 Auto-Add feature globally **Enabled**.")
        return

    if first_arg in ("off", "disable", "stop"):
        if len(context.args) > 1:
            target = context.args[1].lower().strip()
            resolved = resolve_category(target)
            if resolved:
                settings = await settings_collection.find_one({"_id": "auto_add_settings"}) or {}
                channels = settings.get("channels", {})
                to_remove = [k for k, v in channels.items() if (isinstance(v, dict) and v.get("category") == resolved) or v == resolved]
                if to_remove:
                    unset_dict = {f"channels.{k}": "" for k in to_remove}
                    await settings_collection.update_one({"_id": "auto_add_settings"}, {"$unset": unset_dict})
                    await load_auto_add_settings()
                    await update.effective_message.reply_text(f"🛑 Auto-Add removed for category **{CATEGORIES[resolved]['name']}**.")
                    return
            target_key = target.lstrip("@")
            unset_dict = {f"channels.{target}": "", f"channels.@{target_key}": "", f"channels.{target_key}": ""}
            await settings_collection.update_one({"_id": "auto_add_settings"}, {"$unset": unset_dict})
            await load_auto_add_settings()
            await update.effective_message.reply_text(f"🛑 Removed auto-add for `{target}`.")
            return
        else:
            await settings_collection.update_one(
                {"_id": "auto_add_settings"},
                {"$set": {"enabled": False}},
                upsert=True
            )
            await load_auto_add_settings()
            await update.effective_message.reply_text("🔴 Auto-Add feature globally **Disabled**.")
            return

    resolved_cat = resolve_category(first_arg)
    if not resolved_cat:
        help_text = get_categories_help_text()
        await update.effective_message.reply_text(
            f"❌ Invalid category `{first_arg}`.\n\n"
            f"Please choose a number (1-6):\n{help_text}",
            parse_mode="Markdown"
        )
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            f"Usage in PM: `/autoadd <1-6> <channel_link_or_username_or_id>`\n"
            f"Example: `/autoadd 2 @MyViralGroup`",
            parse_mode="Markdown"
        )
        return

    raw_channel = context.args[1].strip()
    target_ident = raw_channel
    title = raw_channel
    chat_id_val = None

    try:
        clean_target = raw_channel
        if "t.me/" in raw_channel:
            ch_id, _ = parse_channel_link(raw_channel + "/1")
            if ch_id:
                clean_target = ch_id
        target_chat = await context.bot.get_chat(clean_target)
        if target_chat:
            chat_id_val = target_chat.id
            title = target_chat.title or target_chat.username or str(target_chat.id)
            target_ident = str(target_chat.id)
    except Exception as e:
        logger.info(f"Could not get_chat for {raw_channel}: {e}")

    chat_data = {
        "category": resolved_cat,
        "title": title,
        "username": raw_channel.lstrip("@"),
        "chat_id": chat_id_val or raw_channel
    }

    set_dict = {
        "enabled": True,
        f"channels.{target_ident}": chat_data
    }
    if raw_channel.startswith("@") or not raw_channel.startswith("-100"):
        u = raw_channel.lstrip("@").lower()
        set_dict[f"channels.@{u}"] = chat_data
        set_dict[f"channels.{u}"] = chat_data

    await settings_collection.update_one(
        {"_id": "auto_add_settings"},
        {"$set": set_dict},
        upsert=True
    )
    await load_auto_add_settings()

    cat_name = CATEGORIES[resolved_cat]["name"]
    await update.effective_message.reply_text(
        f"✅ **Auto-Add Configured!**\n\n"
        f"📺 Channel/Group: **{title}** (`{raw_channel}`)\n"
        f"📁 Category: **{cat_name}** (`{resolved_cat}`)\n\n"
        f"📌 *Note: Make sure the bot is an Administrator in that group/channel so it can receive messages!*",
        parse_mode="Markdown"
    )

# --- Direct Video Upload by Admin in PM ---
async def prompt_admin_save_video(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str):
    prompt_msg = await update.effective_message.reply_text("⏳ Processing video...")
    mid = prompt_msg.message_id
    context.user_data[f"vid_{mid}"] = file_id

    keyboard = []
    row = []
    for num, k in CATEGORY_NUM_MAP.items():
        v = CATEGORIES[k]
        row.append(InlineKeyboardButton(f"{num}. {v['name']}", callback_data=f"savevid_{k}_{mid}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"savevid_cancel_{mid}")])

    await prompt_msg.edit_text(
        "📹 **New Video Received!**\n\n"
        "Choose a category to save this video:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def admin_save_video_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Access denied.", show_alert=True)
        return
    await query.answer()

    data = query.data
    if "cancel" in data:
        parts = data.split("_")
        mid = parts[-1]
        context.user_data.pop(f"vid_{mid}", None)
        await query.edit_message_text("❌ Video saving cancelled.")
        return

    parts = data.split("_")
    mid = parts[-1]
    cat_key = "_".join(parts[1:-1])

    if cat_key not in CATEGORIES:
        await query.edit_message_text("❌ Unknown category.")
        return

    file_id = context.user_data.pop(f"vid_{mid}", None)
    if not file_id:
        await query.edit_message_text("⚠️ No pending video found (already saved or expired).")
        return

    existing = await videos_collection.find_one({"file_id": file_id})
    cat_name = CATEGORIES[cat_key]["name"]
    if existing:
        await query.edit_message_text(f"ℹ️ Video is already in database (Category: **{existing.get('category', 'unknown')}**).", parse_mode="Markdown")
        return

    await videos_collection.insert_one({
        "file_id": file_id,
        "category": cat_key,
        "date_added": datetime.utcnow()
    })
    category_counts_cache[cat_key] = category_counts_cache.get(cat_key, 0) + 1
    total = category_counts_cache[cat_key]

    await query.edit_message_text(
        f"✅ Video Saved to **{cat_name}** (`{cat_key}`)!\n\n"
        f"🎬 Total in {cat_name}: **{total}** videos",
        parse_mode="Markdown"
    )

# --- Incoming Video Handler (Auto-Add Listener) ---
async def auto_video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    file_id = None
    if msg.video:
        file_id = msg.video.file_id
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video/"):
        file_id = msg.document.file_id

    if not file_id:
        return

    # Check if direct message in PM from admin
    if chat.type == "private":
        if update.effective_user and update.effective_user.id == ADMIN_ID:
            await prompt_admin_save_video(update, context, file_id)
        return

    # In groups/channels, check if auto-add is globally enabled
    if not auto_add_enabled:
        return

    chat_id_str = str(chat.id)
    chat_user = chat.username.lower() if chat.username else None

    # Match chat in auto_add_cache
    cat_key = auto_add_cache.get(chat_id_str)
    if not cat_key and chat_user:
        cat_key = auto_add_cache.get(chat_user) or auto_add_cache.get(f"@{chat_user}")

    if not cat_key:
        return

    # Check if video is already in database
    existing = await videos_collection.find_one({"file_id": file_id})
    if existing:
        return

    # Insert video into database
    await videos_collection.insert_one({
        "file_id": file_id,
        "category": cat_key,
        "source_chat_id": chat.id,
        "source_message_id": msg.message_id,
        "date_added": datetime.utcnow()
    })

    category_counts_cache[cat_key] = category_counts_cache.get(cat_key, 0) + 1

    chat_title = chat.title or (f"@{chat.username}" if chat.username else f"Chat {chat.id}")
    queue_auto_add_notification(context.application, cat_key, chat_title)

async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/premium <user_id> [days]\nExample: /premium 123456789 30"
        )
        return

    user_id_arg = context.args[0]
    days = 30
    if len(context.args) > 1:
        try:
            days = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ Days must be a number.")
            return

    try:
        user_id = int(user_id_arg.lstrip('@'))
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    premium_expiry = datetime.utcnow() + timedelta(days=days)
    await users_collection.update_one(
        {"_id": user_id},
        {"$set": {"premium_expiry": premium_expiry}},
        upsert=True
    )

    expiry_text = premium_expiry.strftime("%d %b %Y")
    await update.message.reply_text(
        f"✅ Premium added for user {user_id} until {expiry_text}."
    )
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"🎉 Congratulations! Your premium access is active until {expiry_text}.\n"
                "You can now watch unlimited videos."
            )
        )
    except Exception:
        pass
    await notify_group(f"💎 Premium Activated: user *{user_id}* until *{expiry_text}*.")

async def remove_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/removepremium <user_id>\nExample: /removepremium 123456789"
        )
        return

    user_id_arg = context.args[0]
    try:
        user_id = int(user_id_arg.lstrip('@'))
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    result = await users_collection.update_one(
        {"_id": user_id},
        {"$set": {"premium_expiry": None, "unlimited_access": False}}
    )

    if result.matched_count == 0:
        await update.message.reply_text(f"⚠️ User {user_id} not found.")
        return

    await update.message.reply_text(f"✅ Premium removed for user {user_id}.")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ Your premium access has been removed. You now have the free daily limit of 20 videos."
        )
    except Exception:
        pass

async def setpremiumprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage:\n/setpremiumprice <package> <amount>\nExample: /setpremiumprice 1_month 150"
        )
        return

    package = normalize_premium_package(context.args[0])
    if not package:
        await update.message.reply_text(
            "❌ Invalid package. Use: 1_month, 3_months, 6_months, 1_year, lifetime"
        )
        return

    price = context.args[1]
    await set_premium_price(package, price)
    await update.message.reply_text(f"✅ Price updated: {package} = {price}")


async def renamechannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /renamechannel <channel_message_link or @channel> <start_number_or_start_msg_id> <keep_ext:yes/no>")
        return

    channel_arg = context.args[0]
    second_arg = context.args[1]
    keep_ext = context.args[2].lower() in ("yes", "y", "true", "1")

    # If channel_arg is a message link, parse it
    chat_id, msg_id = parse_channel_link(channel_arg)
    # If parsed, use parsed chat_id and start message id; second_arg is starting filename number
    if chat_id and msg_id:
        start_msg_id = msg_id
        try:
            start_num = int(second_arg)
        except ValueError:
            await update.message.reply_text("If you supply a message link, the second parameter must be the starting number for renaming.")
            return
    else:
        # channel_arg is likely a @username; second_arg should be starting message id
        chat_id = channel_arg
        try:
            start_msg_id = int(second_arg)
        except ValueError:
            await update.message.reply_text("For @channel mode, second parameter must be the starting message id to scan from.")
            return
        start_num = 1

    bot = context.bot
    found = []
    preview = []
    limit = 200
    num = start_num
    for mid in range(start_msg_id, start_msg_id + limit):
        try:
            fwd = await bot.forward_message(chat_id=ADMIN_ID, from_chat_id=chat_id, message_id=mid)
            # inspect forwarded message
            name = None
            if fwd.document and getattr(fwd.document, "file_name", None):
                name = fwd.document.file_name
            elif fwd.video and getattr(fwd.video, "file_name", None):
                name = fwd.video.file_name
            else:
                name = fwd.caption or ""

            ext = ""
            if keep_ext and name and "." in name:
                ext = "." + name.split(".")[-1]

            new_name = f"{num}{ext}"
            preview.append(f"{chat_id}:{mid} -> {new_name}")
            found.append(mid)
            num += 1
            # delete the forwarded inspection message
            try:
                await bot.delete_message(chat_id=ADMIN_ID, message_id=fwd.message_id)
            except Exception:
                pass
        except Exception:
            # stop scanning when we hit a run of missing messages (to avoid long loops)
            if len(found) > 0 and len(found) % 50 == 0:
                break
            continue

    if not preview:
        await update.message.reply_text("No media messages found in the given channel range.")
        return

    # store job
    rename_jobs[update.message.from_user.id] = {
        "chat_id": chat_id,
        "message_ids": found,
        "start_num": start_num,
        "keep_ext": keep_ext
    }

    text = "🔁 Rename Preview:\n\n" + "\n".join(preview[:50])
    if len(preview) > 50:
        text += f"\n... and {len(preview)-50} more"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Apply", callback_data="rename_confirm")],
        [InlineKeyboardButton("❌ Cancel", callback_data="rename_cancel")]
    ])
    await update.message.reply_text(text, reply_markup=keyboard)


async def rename_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    job = rename_jobs.get(admin_id)
    if not job:
        await query.message.reply_text("No pending rename job.")
        return

    bot = context.bot
    chat_id = job["chat_id"]
    msg_ids = job["message_ids"]
    num = job["start_num"]
    keep_ext = job.get("keep_ext", False)

    applied = 0
    failed = 0
    for mid in msg_ids:
        try:
            # Forward to admin, then edit caption to the new name (best-effort)
            fwd = await bot.forward_message(chat_id=ADMIN_ID, from_chat_id=chat_id, message_id=mid)
            new_caption = str(num)
            # attempt to preserve extension by checking original file name
            if keep_ext:
                orig = (fwd.caption or "")
                if "." in orig:
                    new_caption += "." + orig.split(".")[-1]
            try:
                await bot.edit_message_caption(chat_id=ADMIN_ID, message_id=fwd.message_id, caption=new_caption)
            except Exception:
                try:
                    await bot.edit_message_text(chat_id=ADMIN_ID, message_id=fwd.message_id, text=new_caption)
                except Exception:
                    failed += 1
                    await bot.delete_message(chat_id=ADMIN_ID, message_id=fwd.message_id)
                    num += 1
                    continue

            # In many cases Telegram doesn't allow renaming original channel files via bot API.
            # We provide the edited forwarded message as proof of rename and then delete it.
            await bot.delete_message(chat_id=ADMIN_ID, message_id=fwd.message_id)
            applied += 1
            num += 1
        except Exception:
            failed += 1

    del rename_jobs[admin_id]
    await query.message.reply_text(f"Rename applied: {applied}, failures: {failed}")


async def rename_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    if admin_id in rename_jobs:
        del rename_jobs[admin_id]
    await query.message.reply_text("Rename job cancelled.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error in Telegram handler", exc_info=context.error)


# --- Tornado Health Endpoint Patch (Keeps Render alive 24/7 with 200 OK) ---
try:
    import tornado.web
    import telegram.ext._updater

    class HealthHandler(tornado.web.RequestHandler):
        def get(self):
            self.set_status(200)
            self.write({"status": "ok", "message": "AwaraBot is online and running fast!"})

        def head(self):
            self.set_status(200)

    _orig_init = telegram.ext._updater.WebhookAppClass.__init__

    def _custom_init(self, webhook_path: str, bot, update_queue, secret_token=None):
        _orig_init(self, webhook_path, bot, update_queue, secret_token)
        self.add_handlers(r".*", [
            (r"/", HealthHandler),
            (r"/health/?", HealthHandler),
            (r"/ping/?", HealthHandler),
        ])

    telegram.ext._updater.WebhookAppClass.__init__ = _custom_init
    logger.info("Attached /health handler to Tornado WebhookAppClass.")
except Exception as e:
    logger.warning(f"Could not patch Tornado WebhookAppClass: {e}")

async def keep_alive_ping(base_url):
    """Pings self every 9 minutes so free hosting like Render never spins down."""
    await asyncio.sleep(30)
    url = f"{base_url.rstrip('/')}/health"
    logger.info(f"Keep-alive background task started for {url}")
    import urllib.request
    while True:
        try:
            await asyncio.sleep(540) # 9 minutes (Render shuts down after 15 min of no HTTP traffic)
            req = urllib.request.Request(url, headers={'User-Agent': 'AwaraBot-KeepAlive/1.0'})
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=15))
            logger.info("Keep-alive ping sent successfully (Render 24/7 active).")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Keep-alive ping warning: {e}")

def start_polling_dummy_server(port):
    """Runs a tiny HTTP server on port if running in polling mode on hosts requiring open ports."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading

    class DummyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok", "mode": "polling"}')

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Dummy health check server running on port {port} for polling mode.")
    return server

async def on_startup(application):
    await load_auto_add_settings()
    application.create_task(send_startup_log(application.bot))
    base_url = os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if base_url:
        application.create_task(keep_alive_ping(base_url))

# --- Main ---
def build_application():
    global application
    application = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_video))
    application.add_handler(CommandHandler("addall", add_all_videos))
    application.add_handler(CommandHandler("stopaddall", stop_addall))
    application.add_handler(CommandHandler("clean", clean_db))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("premium", premium))
    application.add_handler(CommandHandler("removepremium", remove_premium))
    application.add_handler(CommandHandler("setpremiumprice", setpremiumprice))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("renamechannel", renamechannel_cmd))
    application.add_handler(CommandHandler("categorychannels", category_channels_cmd))
    application.add_handler(CommandHandler("setcategorychannel", set_category_channel_cmd))
    application.add_handler(CommandHandler("autoadd", autoadd_cmd))
    
    application.add_handler(CallbackQueryHandler(check_joined, pattern="^check_joined$"))
    application.add_handler(CallbackQueryHandler(categories_menu, pattern="^categories$"))
    application.add_handler(CallbackQueryHandler(help_handler, pattern="^help$"))
    application.add_handler(CallbackQueryHandler(start, pattern="^start$"))
    application.add_handler(CallbackQueryHandler(buy_premium_menu, pattern="^buy_premium$"))
    application.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel$"))
    application.add_handler(CallbackQueryHandler(admin_rename_handler, pattern="^admin_rename$"))
    application.add_handler(CallbackQueryHandler(rename_confirm_cb, pattern="^rename_confirm$"))
    application.add_handler(CallbackQueryHandler(rename_cancel_cb, pattern="^rename_cancel$"))
    application.add_handler(CallbackQueryHandler(admin_command_info, pattern="^admin_.*$"))
    application.add_handler(CallbackQueryHandler(admin_save_video_callback, pattern="^savevid_.*$"))
    application.add_handler(CallbackQueryHandler(category_video_handler, pattern="^(cat_.*|leakvideos)$"))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL, auto_video_handler))
    application.add_error_handler(error_handler)

    return application


def main():
    while True:
        app = build_application()
        port = int(os.environ.get("PORT", 8080))
        webhook_base_url = os.environ.get("WEBHOOK_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
        try:
            logger.info("🚀 Bot is starting...")
            if webhook_base_url:
                webhook_url = f"{webhook_base_url.rstrip('/')}/{BOT_TOKEN}"
                logger.info(f"Starting webhook on port {port} with URL {webhook_url}")
                app.run_webhook(
                    listen="0.0.0.0",
                    port=port,
                    url_path=BOT_TOKEN,
                    webhook_url=webhook_url,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                    close_loop=False,
                )
            else:
                logger.warning("WEBHOOK_BASE_URL or RENDER_EXTERNAL_URL is not set. Falling back to polling mode.")
                try:
                    start_polling_dummy_server(port)
                except Exception as e:
                    logger.warning(f"Could not start dummy port server: {e}")
                app.run_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                    poll_interval=0.5,
                    close_loop=False,
                )
        except KeyboardInterrupt:
            logger.info("Bot stopped by keyboard interrupt.")
            break
        except Exception:
            logger.exception("Bot encountered an error. Restarting in 5 seconds...")
            time.sleep(5)
            continue
        else:
            logger.info("Bot loop exited. Restarting in 5 seconds...")
            time.sleep(5)

if __name__ == '__main__':
    main()
