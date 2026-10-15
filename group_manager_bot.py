import asyncio
import datetime
import logging
import aiosqlite
from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

# ============================================================
# ⚙️ CONFIG — YAHAN APNA DATA DAALO
# ============================================================
API_ID = 32141443                       # my.telegram.org se
API_HASH = "4f34a89257ac316505f5a47b237454cc"       # my.telegram.org se
BOT_TOKEN = "8850375771:AAH_WSl8p3n4m9E98s2Xj4tpzjbaQWhxY-A"     # @BotFather se

GROUP_ID = -1001843179386             # Apne group ki ID

PROMOTE_WEEKLY_MESSAGES = 1000        # 1000+ msgs/week = promote
INACTIVE_DAYS = 3                     # Silent days before tagging
TAG_DELAY = 3                         # Delay between tags (sec)
BATCH_PAUSE = 30                      # Pause after 10 tags

DB_PATH = "group_bot.db"
SESSION_FILE = "session.txt"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Global clients
bot = None
userbot = None


# ============================================================
# 📅 WEEK HELPERS
# ============================================================
def get_week(offset=0):
    d = datetime.datetime.now() - datetime.timedelta(weeks=offset)
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ============================================================
# 🗄️ DATABASE
# ============================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                message_count INTEGER DEFAULT 0,
                last_active TEXT,
                is_limited_admin INTEGER DEFAULT 0,
                was_tagged INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS weekly_stats (
                user_id INTEGER,
                week TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, week)
            )
        """)
        await db.commit()


async def db_inc_message(uid, name, uname):
    now = datetime.datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, first_name, username, message_count, last_active)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                message_count = message_count + 1,
                last_active = ?,
                was_tagged = 0,
                first_name = ?
        """, (uid, name, uname, now, now, name))
        await db.execute("""
            INSERT INTO weekly_stats (user_id, week, count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, week) DO UPDATE SET count = count + 1
        """, (uid, get_week(0)))
        await db.commit()


async def db_get_weekly(uid, week):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT count FROM weekly_stats WHERE user_id=? AND week=?",
            (uid, week)
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def db_mark_admin(uid, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_limited_admin=? WHERE user_id=?",
            (status, uid)
        )
        await db.commit()


async def db_inactive_users(days):
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT user_id, first_name FROM users
            WHERE last_active < ? AND was_tagged=0 AND is_limited_admin=0
        """, (cutoff,))
        return await cur.fetchall()


async def db_mark_tagged(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET was_tagged=1 WHERE user_id=?", (uid,))
        await db.commit()


async def db_all_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, first_name, is_limited_admin FROM users")
        return await cur.fetchall()


async def db_top(limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT first_name, message_count FROM users ORDER BY message_count DESC LIMIT ?",
            (limit,)
        )
        return await cur.fetchall()


async def db_week_top(week, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT u.first_name, w.count FROM weekly_stats w
            JOIN users u ON u.user_id = w.user_id
            WHERE w.week = ? ORDER BY w.count DESC LIMIT ?
        """, (week, limit))
        return await cur.fetchall()


# ============================================================
# 🛡️ PROMOTE / DEMOTE (Userbot ke through)
# ============================================================
async def promote_user(uid):
    """✅ delete, invite, pin, manage_call  |  ❌ change_info, add_admins, ban"""
    try:
        await userbot(functions.channels.EditAdminRequest(
            channel=GROUP_ID,
            user_id=uid,
            admin_rights=types.ChatAdminRights(
                change_info=False,
                post_messages=False,
                edit_messages=False,
                delete_messages=True,
                ban_users=False,
                invite_users=True,
                pin_messages=True,
                add_admins=False,
                anonymous=False,
                manage_call=True,
                other=True,
            ),
            rank="Active"
        ))
        return True
    except Exception as e:
        logger.error(f"Promote failed {uid}: {e}")
        return False


async def demote_user(uid):
    """Remove all admin rights."""
    try:
        await userbot(functions.channels.EditAdminRequest(
            channel=GROUP_ID,
            user_id=uid,
            admin_rights=types.ChatAdminRights(
                change_info=False, post_messages=False, edit_messages=False,
                delete_messages=False, ban_users=False, invite_users=False,
                pin_messages=False, add_admins=False, anonymous=False,
                manage_call=False, other=False,
            ),
            rank=""
        ))
        return True
    except Exception as e:
        logger.error(f"Demote failed {uid}: {e}")
        return False


# ============================================================
# ⏰ WEEKLY PROMOTE / DEMOTE
# ============================================================
async def weekly_job():
    this_w = get_week(0)
    last_w = get_week(1)
    logger.info(f"Weekly check: {this_w} vs {last_w}")

    users = await db_all_users()
    counter = 0

    for uid, name, is_admin in users:
        this_c = await db_get_weekly(uid, this_w)
        last_c = await db_get_weekly(uid, last_w)

        # ---------- PROMOTE ----------
        if this_c >= PROMOTE_WEEKLY_MESSAGES and not is_admin:
            if await promote_user(uid):
                await db_mark_admin(uid, 1)
                try:
                    await userbot.send_message(
                        GROUP_ID,
                        f"🎊 [{name}](tg://user?id={uid}) sent **{this_c}** messages "
                        f"this week! Limited admin granted 🛡️"
                    )
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)

            counter += 1
            await asyncio.sleep(3)
            if counter % 10 == 0:
                await asyncio.sleep(BATCH_PAUSE)

        # ---------- DEMOTE ----------
        elif is_admin and last_c > 0 and this_c < last_c:
            if await demote_user(uid):
                await db_mark_admin(uid, 0)
                try:
                    await userbot.send_message(
                        GROUP_ID,
                        f"⚠️ [{name}](tg://user?id={uid}) activity dropped "
                        f"({last_c} → {this_c}). Admin rights removed."
                    )
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)

            counter += 1
            await asyncio.sleep(3)
            if counter % 10 == 0:
                await asyncio.sleep(BATCH_PAUSE)

    logger.info(f"Weekly job done for {this_w}")


# ============================================================
# ⏰ INACTIVE TAGGING
# ============================================================
async def tag_inactive_job():
    users = await db_inactive_users(INACTIVE_DAYS)
    logger.info(f"Tagging {len(users)} inactive users")

    for i, (uid, name) in enumerate(users):
        try:
            await userbot.send_message(
                GROUP_ID,
                f"👋 [kahan ho bhai?](tg://user?id={uid}) — "
                f"{INACTIVE_DAYS} din se silent ho. Active ho jao! 😴"
            )
            await db_mark_tagged(uid)
            await asyncio.sleep(TAG_DELAY)
            if (i + 1) % 10 == 0:
                await asyncio.sleep(BATCH_PAUSE)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Tag failed {uid}: {e}")


# ============================================================
# ⏰ SCHEDULER LOOP
# ============================================================
async def scheduler():
    last_weekly = None
    last_daily = None

    while True:
        now = datetime.datetime.now()

        # Weekly — Monday 12:00
        if now.weekday() == 0 and now.hour == 12:
            key = now.strftime("%Y-%m-%d")
            if last_weekly != key:
                await weekly_job()
                last_weekly = key

        # Daily inactive — 13:00
        if now.hour == 13 and now.minute < 5:
            key = now.strftime("%Y-%m-%d")
            if last_daily != key:
                await tag_inactive_job()
                last_daily = key

        await asyncio.sleep(60)


# ============================================================
# 🚀 BOT HANDLERS
# ============================================================
def register_handlers(client):

    @client.on(events.NewMessage(pattern=r"/start"))
    async def cmd_start(event):
        await event.reply(
            "🤖 **Group Manager Bot**\n\n"
            "**Commands:**\n"
            "`/connect <session_string>` — Userbot attach karo\n"
            "`/stats` — Apna activity dekho\n"
            "`/top` — Top 10 all-time\n"
            "`/week` — Is hafte ka leaderboard\n"
            "`/promote_now` — Force weekly check\n"
            "`/tag_inactive` — Force inactive tagging"
        )

    @client.on(events.NewMessage(pattern=r"/connect"))
    async def cmd_connect(event):
        global userbot
        parts = event.message.text.split(maxsplit=1)
        if len(parts) < 2:
            await event.reply(
                "**Usage:** `/connect <session_string>`\n\n"
                "Session string my.telegram.org + Telethon generator se banao."
            )
            return

        session_str = parts[1].strip()
        try:
            ub = TelegramClient(StringSession(session_str), API_ID, API_HASH)
            await ub.start()
            me = await ub.get_me()

            with open(SESSION_FILE, "w") as f:
                f.write(session_str)

            userbot = ub

            # Attach message tracker
            @ub.on(events.NewMessage(chats=GROUP_ID))
            async def track(event):
                if event.sender and not event.sender.bot:
                    u = event.sender
                    await db_inc_message(
                        u.id, u.first_name or "User", u.username or ""
                    )

            await event.reply(
                f"✅ Connected as **{me.first_name}** (`{me.id}`)\n"
                f"Ab activity track ho rahi hai."
            )
            logger.info(f"Userbot connected: {me.id}")

        except Exception as e:
            await event.reply(f"❌ Failed: `{e}`")
            logger.error(f"Connect failed: {e}")

    @client.on(events.NewMessage(pattern=r"/stats"))
    async def cmd_stats(event):
        uid = event.sender_id
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT message_count, is_limited_admin FROM users WHERE user_id=?",
                (uid,)
            )
            row = await cur.fetchone()

        if not row:
            await event.reply("Aapka record nahi hai abhi.")
            return

        total, is_admin = row
        weekly = await db_get_weekly(uid, get_week(0))
        status = "🛡️ Limited Admin" if is_admin else "👤 Member"
        need = max(0, PROMOTE_WEEKLY_MESSAGES - weekly)

        await event.reply(
            f"📊 **Aapka Stats**\n"
            f"Total messages: {total}\n"
            f"Is hafte: {weekly}\n"
            f"Status: {status}\n"
            f"Promotion tak: {need} messages (is hafte)"
        )

    @client.on(events.NewMessage(pattern=r"/top$"))
    async def cmd_top(event):
        rows = await db_top(10)
        text = "🏆 **Top 10 All-Time**\n\n"
        for i, (name, cnt) in enumerate(rows, 1):
            text += f"{i}. {name} — {cnt}\n"
        await event.reply(text)

    @client.on(events.NewMessage(pattern=r"/week"))
    async def cmd_week(event):
        week = get_week(0)
        rows = await db_week_top(week, 10)
        if not rows:
            await event.reply("Is hafte abhi data nahi hai.")
            return
        text = f"🏆 **Top 10 ({week})**\n\n"
        for i, (name, cnt) in enumerate(rows, 1):
            text += f"{i}. {name} — {cnt}\n"
        await event.reply(text)

    @client.on(events.NewMessage(pattern=r"/promote_now"))
    async def cmd_promote_now(event):
        if not userbot:
            await event.reply("❌ Pehle `/connect` karo.")
            return
        await event.reply("⏳ Weekly check chala raha hoon...")
        await weekly_job()
        await event.reply("✅ Done!")

    @client.on(events.NewMessage(pattern=r"/tag_inactive"))
    async def cmd_tag_inactive(event):
        if not userbot:
            await event.reply("❌ Pehle `/connect` karo.")
            return
        await event.reply("⏳ Inactive users tag kar raha hoon...")
        await tag_inactive_job()
        await event.reply("✅ Done!")

    @client.on(events.ChatAction(chats=GROUP_ID))
    async def welcome(event):
        if event.user_joined or event.user_added:
            user = await event.get_user()
            if user.bot:
                return
            await db_inc_message(user.id, user.first_name or "User", user.username or "")
            try:
                await event.reply(
                    f"🎉 Welcome [{user.first_name}](tg://user?id={user.id})!\n\n"
                    f"Active raho — {PROMOTE_WEEKLY_MESSAGES} msgs/week bhejo, "
                    f"automatic admin powers pao! 🛡️"
                )
            except Exception as e:
                logger.error(f"Welcome failed: {e}")


# ============================================================
# 🚀 MAIN
# ============================================================
async def main():
    global bot
    await init_db()

    bot = TelegramClient(StringSession(), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    logger.info("Bot started.")

    register_handlers(bot)

    # Auto-load saved session
    try:
        with open(SESSION_FILE) as f:
            saved = f.read().strip()
        if saved:
            global userbot
            ub = TelegramClient(StringSession(saved), API_ID, API_HASH)
            await ub.start()
            userbot = ub

            @ub.on(events.NewMessage(chats=GROUP_ID))
            async def track(event):
                if event.sender and not event.sender.bot:
                    u = event.sender
                    await db_inc_message(
                        u.id, u.first_name or "User", u.username or ""
                    )

            logger.info("Auto-loaded saved session.")
    except FileNotFoundError:
        logger.info("No saved session. Use /connect to attach userbot.")

    # Start scheduler
    asyncio.create_task(scheduler())

    logger.info("Running...")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
