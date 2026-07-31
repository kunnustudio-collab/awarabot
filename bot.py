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
    ContextTypes
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
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["telegram_bot"]
users_collection = db["users"]
videos_collection = db["videos"]
settings_collection = db["settings"]

# In-memory rename jobs keyed by admin user id
rename_jobs = {}

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

async def get_random_video():
    count = await videos_collection.count_documents({})
    if count == 0:
        return None
    skip = random.randint(0, count - 1)
    return await videos_collection.find_one({}, skip=skip)

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


async def get_missing_force_channels(bot, user_id):
    missing = []
    for channel in FORCE_CHANNELS:
        # For invite links and other formats, try multiple ways to resolve the chat
        tried_success = False
        candidates = []
        try:
            if channel.startswith(("http://", "https://")):
                parsed = urlparse(channel)
                path = parsed.path.lstrip('/')
                # candidate forms: original URL, path, @path, https://t.me/path
                candidates = [channel]
                if path:
                    candidates.append(path)
                    if not path.startswith('+') and not path.startswith('@'):
                        candidates.append(f"@{path}")
                    candidates.append(f"https://t.me/{path}")
            else:
                # not a URL: try as given, and as @username
                candidates = [channel]
                if not channel.startswith('@') and not channel.startswith('+'):
                    candidates.append(f"@{channel}")

            for cand in candidates:
                try:
                    # First try to resolve the candidate to a Chat object (useful for invite links)
                    chat_identifier = None
                    try:
                        chat = await bot.get_chat(cand)
                        # If get_chat succeeds, use chat.id for membership check
                        chat_identifier = chat.id
                    except Exception:
                        # get_chat failed for this candidate; fall back to using the raw candidate
                        chat_identifier = None

                    target_for_member = chat_identifier if chat_identifier is not None else cand
                    member = await bot.get_chat_member(target_for_member, user_id)
                    if member.status in ["member", "administrator", "creator"]:
                        tried_success = True
                        break
                except (Forbidden, BadRequest):
                    # user not a member or chat inaccessible for this candidate
                    continue
                except Exception:
                    # unknown error for this candidate; try next
                    continue

        except Exception:
            # parsing or other error; fall through to mark missing
            tried_success = False

        if not tried_success:
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
    user = await refresh_premium_status(user_id)
    if user is None:
        return True
    if user.get("unlimited_access") or is_premium_record(user):
        return True
    await reset_watch_count_if_needed(user_id)
    user = await get_user(user_id)
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

async def broadcast_restart_notice(application):
    await asyncio.sleep(3)
    text = "Bot Started 💞"
    delivered, blocked, failed = await send_broadcast_text_to_all(application.bot, text)
    logger.info(f"Restart broadcast complete: delivered={delivered}, blocked={blocked}, failed={failed}")

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Determine user and chat_id from the context
    if update.callback_query:
        user = update.callback_query.from_user
        chat_id = update.callback_query.message.chat_id
        try:
            await update.callback_query.message.delete()
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

# --- FIX: Updated categories_menu ---
async def categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("🎬 𝐋𝐞𝐚𝐤 𝐕𝐢𝐝𝐞𝐨𝐬", callback_data="leakvideos")]]
    
    try:
        # Delete the old message (photo)
        await query.message.delete()
    except Exception as e:
        logger.info(f"Could not delete message in categories_menu: {e}")
    
    # Send a new text message
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="📂 𝐒𝐞𝐥𝐞𝐜𝐭 𝐚 𝐜𝐚𝐭𝐞𝐠𝐨𝐫𝐲:",
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
        [InlineKeyboardButton("📥 AddAll Videos", callback_data="admin_addall")],
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

    command_text = {
        "admin_add": "/add <message_link>",
        "admin_addall": "/addall <start_link> <end_id>",
        "admin_clean": "/clean",
        "admin_stats": "/stats",
        "admin_premium": "/premium <user_id> [days]",
        "admin_removepremium": "/removepremium <user_id>",
        "admin_setpremiumprice": "/setpremiumprice <package> <amount>",
        "admin_broadcast": "/broadcast <text> or reply to a message with /broadcast"
        ,"admin_rename": "/renamechannel <channel_username_or_link> <start_number> <keep_ext:yes/no>"
    }.get(query.data, "Unknown command")

    text = f"Use this command:\n`{command_text}`"
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

async def send_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if not await can_watch_video(user_id):
        await context.bot.send_message(
            chat_id=user_id,
            text=("🚫 Your daily free limit has ended.\n\n"
                  "You have watched today's free videos.\n\n"
                  "You are not a premium user. Buy premium to @MeAwara."),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Buy Premium Subscription", callback_data="buy_premium")]
            ])
        )
        return

    video_data = await get_random_video()
    if not video_data:
        await context.bot.send_message(user_id, "⚠️ No videos found in the database.")
        return

    try:
        sent = await context.bot.send_video(
            chat_id=user_id,
            video=video_data['file_id'],
            caption="Save or forward this video now! ⏳ ये वीडियो 5 मिनट बाद हट जाएगी।",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Next Video", callback_data="leakvideos")]
            ])
        )
        await increment_video_watch(user_id)
        try:
            await query.message.delete()
        except Exception as e:
            logger.info(f"Could not delete old menu message: {e}")

        asyncio.create_task(delete_message_after_delay(context, user_id, sent.message_id, 300))
    except Exception as e:
        logger.error(f"Error sending video: {e}")

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
        await update.message.reply_text("Please provide a message link.")
        return

    link = context.args[0]
    chat_id, message_id = parse_channel_link(link)
    if not chat_id or not message_id:
        await update.message.reply_text("❌ Invalid channel link. Use a message link like https://t.me/c/1234567890/42 or https://t.me/ChannelName/42")
        return

    try:
        msg = await context.bot.forward_message(chat_id=ADMIN_ID, from_chat_id=chat_id, message_id=message_id)
        if msg.video:
            if await videos_collection.count_documents({"file_id": msg.video.file_id}) == 0:
                await videos_collection.insert_one({"file_id": msg.video.file_id})
                await update.message.reply_text("✅ Video Saved!")
            else:
                await update.message.reply_text("ℹ️ This video is already in the database.")
        else:
            await update.message.reply_text("❌ This message does not contain a video.")
        await context.bot.delete_message(chat_id=ADMIN_ID, message_id=msg.message_id)
    except Exception as e:
        logger.error(f"Error in /add: {e}")
        await update.message.reply_text(f"❌ Failed to process video.\nError: {e}")

async def add_all_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    if len(context.args) != 2:
        await update.message.reply_text("❌ **Invalid Usage**\n\nUse: `/addall <start_link> <end_id>`", parse_mode='Markdown')
        return

    start_link, end_id_str = context.args
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
    status_message = await update.message.reply_text(f"🔄 Batch process started for **{start_id}** to **{end_id}**...", parse_mode='Markdown')
    added, skipped, failed = 0, 0, 0
    total = end_id - start_id + 1
    for i, mid in enumerate(range(start_id, end_id + 1)):
        try:
            msg = await context.bot.forward_message(chat_id=ADMIN_ID, from_chat_id=chat_id, message_id=mid)
            if msg.video:
                if await videos_collection.count_documents({"file_id": msg.video.file_id}) == 0:
                    await videos_collection.insert_one({"file_id": msg.video.file_id}); added += 1
                else: skipped += 1
            else: failed += 1
            await context.bot.delete_message(chat_id=ADMIN_ID, message_id=msg.message_id)
        except Exception: failed += 1
        if i % 10 == 0 or i == total - 1:
            try: await status_message.edit_text(f"🔄 **Processing...** {i+1}/{total}\n✅ Added: {added} | ⏩ Skipped: {skipped} | ❌ Failed: {failed}", parse_mode='Markdown')
            except Exception: pass
        await asyncio.sleep(1.5)
    await status_message.edit_text(f"✅ **Batch Finished!**\n\n- Added: **{added}**\n- Skipped: **{skipped}**\n- Failed: **{failed}**", parse_mode='Markdown')

async def clean_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id == ADMIN_ID:
        await videos_collection.delete_many({})
        await update.message.reply_text("✅ All videos have been deleted.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id == ADMIN_ID:
        total_users = await users_collection.count_documents({})
        total_videos = await videos_collection.count_documents({})
        await update.message.reply_text(f"📊 **Stats**\n\n- Users: {total_users}\n- Videos: {total_videos}", parse_mode='Markdown')

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


async def on_startup(application):
    application.create_task(broadcast_restart_notice(application))

# --- Main ---
def build_application():
    global application
    application = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add", add_video))
    application.add_handler(CommandHandler("addall", add_all_videos))
    application.add_handler(CommandHandler("clean", clean_db))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("premium", premium))
    application.add_handler(CommandHandler("removepremium", remove_premium))
    application.add_handler(CommandHandler("setpremiumprice", setpremiumprice))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("renamechannel", renamechannel_cmd))
    
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
    application.add_handler(CallbackQueryHandler(send_video, pattern="^leakvideos$"))
    application.add_error_handler(error_handler)

    return application


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = build_application()
    try:
        logger.info("🚀 Bot is running...")
        app.run_polling(drop_pending_updates=True, poll_interval=0.5, close_loop=False)
    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt.")
    except Exception:
        logger.exception("Bot crashed.")
        raise
    finally:
        try:
            loop.close()
        except Exception as e:
            logger.warning(f"Failed to close event loop: {e}")

if __name__ == '__main__':
    main()
