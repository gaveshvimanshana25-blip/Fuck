import os
import re
import html
import random
import time
from datetime import datetime, timedelta, timezone
import asyncio
import logging

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("moviebot")

# ---------- CONFIG (Railway Variables) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
TMDB_TOKEN = os.environ["TMDB_TOKEN"]  # TMDB "API Read Access Token"
CHANNEL_ID = os.environ["CHANNEL_ID"]  # e.g. -1001234567890 or @yourchannel
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

DOWNLOAD_URL = os.environ.get("DOWNLOAD_URL", "https://moviemanialk.com")  # fixed for every post
WHATSAPP_URL = os.environ.get(
    "WHATSAPP_URL", "https://whatsapp.com/channel/0029Vb6dIi242DccwGQfTr2R"
)
FB_URL = os.environ.get(
    "FB_URL", "https://www.facebook.com/profile.php?id=61578959931824"
)

# ---- Auto-post (Firebase admin panel) ----
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "https://cineflix1-1fdc6-default-rtdb.firebaseio.com").rstrip("/")
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "AIzaSyAh6zMB65iWqt3R2Ep0C5_7eV8ZaUHI7t8")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))
SITE_URL = os.environ.get("SITE_URL", "https://www.moviemanialk.com").rstrip("/")
SUB_BY = os.environ.get("SUB_BY", "Piratelk.com")
REQUEST_BOT_URL = os.environ.get("REQUEST_BOT_URL", "https://t.me/Moviemanialkrequest_bot")
POSTED_PATH = "telegram_bot/posted"
POSTED: set = set()  # ids already handled (shared by auto-post + /add)

TMDB = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"
HEADERS = {"Authorization": f"Bearer {TMDB_TOKEN}", "accept": "application/json"}


# ---------- TMDB ----------
async def tmdb_get(path: str, **params):
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{TMDB}{path}", headers=HEADERS, params=params)
        r.raise_for_status()
        return r.json()


async def search(query: str):
    data = await tmdb_get("/search/multi", query=query, include_adult="false")
    results = [x for x in data.get("results", []) if x.get("media_type") in ("movie", "tv")]
    return results[:8]


async def details(media_type: str, tmdb_id: int):
    return await tmdb_get(
        f"/{media_type}/{tmdb_id}", append_to_response="credits,videos,images",
        include_image_language="en,null",
    )


# ---------- POST BUILDER ----------
SEP = "━━━━━━━━━━━━━━━━━━"
SINHALA_LINE = "🇱🇰 <b>සිංහල උපසිරස සමග Download කරගැනිමට හා Online නැරඹිමට පිවිසෙන්න</b> 👇"


def stars(rating: float) -> str:
    full = int(round(rating / 2))
    return "★" * full + "☆" * (5 - full)


def hashtags(genres: list, year: str) -> str:
    tags = []
    for g in genres[:3]:
        t = "".join(ch for ch in g.title() if ch.isalnum())
        if t:
            tags.append("#" + t)
    tags.append("#SinhalaSub")
    if year:
        tags.append(f"#{year}")
    return " ".join(tags)


def render_card(title, year, kind, rating, genres, runtime, quality, country,
                director, cast, overview) -> str:
    e = html.escape
    head = f"{kind} <b>{e(title.upper())}</b>" + (f" <i>({e(str(year))})</i>" if year else "")
    lines = [head, SEP]
    if rating:
        lines.append(f"⭐ <b>{rating:.1f}</b>/10  {stars(rating)}")
    if genres:
        lines.append("🎭 " + " • ".join(e(g) for g in genres))
    meta = []
    if runtime:
        meta.append(f"⏱ {e(str(runtime))}")
    if quality:
        meta.append(f"💿 {e(str(quality))}")
    if country:
        meta.append(f"🌍 {e(str(country))}")
    if meta:
        lines.append("  |  ".join(meta))
    lines.append(SEP)
    if director:
        lines.append(f"🎥 <b>Director:</b> {e(str(director))}")
    if cast:
        lines.append(f"👥 <b>Cast:</b> {e(str(cast))}")

    tail = ["", SINHALA_LINE, "", hashtags(genres, str(year or ""))]
    overview = (overview or "").strip()
    limit = 300
    while True:
        body = list(lines)
        if overview:
            ov = overview if len(overview) <= limit else overview[: limit - 3].rsplit(" ", 1)[0] + "..."
            body += ["", f"<blockquote expandable>📝 {e(ov)}</blockquote>"]
        text = "\n".join(body + tail)
        if len(text) <= 1024 or limit <= 60:
            return text
        limit -= 40


def build_caption(d: dict, media_type: str) -> str:
    title = d.get("title") or d.get("name") or "Unknown"
    date = d.get("release_date") or d.get("first_air_date") or ""
    if media_type == "movie":
        mins = d.get("runtime") or 0
        runtime = f"{mins // 60}h {mins % 60}m" if mins else ""
    else:
        eps = d.get("episode_run_time") or []
        runtime = f"{eps[0]}m / ep" if eps else ""
    credits = d.get("credits", {})
    cast = ", ".join(c["name"] for c in credits.get("cast", [])[:5])
    directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"]
    return render_card(
        title, date[:4], "🎬" if media_type == "movie" else "📺",
        d.get("vote_average") or 0,
        [g["name"] for g in d.get("genres", [])],
        runtime, "", "", ", ".join(directors[:2]), cast, d.get("overview"),
    )


def trailer_url(d: dict):
    for v in d.get("videos", {}).get("results", []):
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            return f"https://www.youtube.com/watch?v={v['key']}"
    return None


def btn(text: str, url: str, style: str | None = None) -> InlineKeyboardButton:
    """URL button with optional colour: primary=blue, success=green, danger=red."""
    if not style:
        return InlineKeyboardButton(text, url=url)
    try:
        return InlineKeyboardButton(text, url=url, style=style)
    except TypeError:  # older python-telegram-bot without native `style`
        return InlineKeyboardButton(text, url=url, api_kwargs={"style": style})


def post_keyboard(download_url: str, trailer: str | None) -> InlineKeyboardMarkup:
    rows = [[
        btn("📥 Download Now", download_url, "primary"),   # blue
        btn("▶️ Watch Online", download_url, "danger"),    # red
    ]]
    if trailer:
        rows.append([btn("🎞 Watch Trailer", trailer)])
    rows.append([btn("🎬 Request Movie", REQUEST_BOT_URL, "success")])  # green
    rows.append([
        btn("💬 WhatsApp", WHATSAPP_URL, "success"),       # green
        btn("👍 Facebook", FB_URL, "primary"),             # blue
    ])
    return InlineKeyboardMarkup(rows)


async def send_post(bot, chat_id, post: dict, download_url: str):
    kb = post_keyboard(download_url, post["trailer"])
    if post["poster"]:
        return await bot.send_photo(
            chat_id, post["poster"], caption=post["caption"],
            parse_mode="HTML", reply_markup=kb,
        )
    return await bot.send_message(
        chat_id, post["caption"], parse_mode="HTML", reply_markup=kb
    )


# ---------- HELPERS ----------
def is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return uid in ADMIN_IDS


# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        kb = InlineKeyboardMarkup([
            [btn("🌐 Website", SITE_URL, "primary")],
            [
                btn("💬 WhatsApp", WHATSAPP_URL, "success"),
                btn("👍 Facebook", FB_URL, "primary"),
            ],
        ])
        await update.message.reply_text(
            "🎬 <b>MovieMania LK</b>\n\n"
            "සිංහල උපසිරස සමග Movies & TV Series 🇱🇰\n\n"
            "📩 ඔයාට ඕන Movie / Series එකේ නම මෙතන type කරන්න, අපි ඒක add කරන්නම්!",
            parse_mode="HTML", reply_markup=kb,
        )
        return
    await update.message.reply_text(
        "👋 <b>Admin Panel</b>\n\n"
        "➕ /add – Movie එකක් site එකට add කරන්න (TMDB)\n"
        "🔎 Movie නමක් type කරලා manual post එකක් හදන්න\n\n"
        "<b>Commands</b>\n"
        "/autopost on|off – auto post පාලනය\n"
        "/add – movie එකක් site එකට add කරන්න\n"
        "/stats – සංඛ්‍යා ලේඛන\n"
        "/last – අන්තිමට post කළ 5\n"
        "/requests – members ලාගේ requests\n"
        "/find &lt;name&gt; – site එකේ movie හොයන්න\n"
        "/version – bot version\n"
        "/vip &lt;id&gt; – VIP on/off\n"
        "/del &lt;id&gt; – movie delete\n"
        "/repost &lt;id&gt; – movie එකක් ආයෙ post කරන්න",
        parse_mode="HTML",
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your ID: {update.effective_user.id}")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await handle_request(update, context)
        return
    text = update.message.text.strip()

    if context.user_data.get("state") in ("dl_tg", "dl_url", "dl_size"):
        await handle_dl_input(update, context, text)
        return

    # Add-to-site flow (waiting for links / player / description / subtitle by ...)
    if context.user_data.get("state") in ("add_links", "add_sub", "add_desc", "add_subby", "add_player"):
        await handle_add_input(update, context, text)
        return

    # Step 1: search
    context.user_data.clear()
    try:
        results = await search(text)
    except Exception:
        log.exception("search failed")
        await update.message.reply_text("⚠️ TMDB error. පස්සේ try කරන්න.")
        return
    if not results:
        await update.message.reply_text("😕 කිසිවක් හම්බුනේ නෑ. වෙන නමකින් try කරන්න.")
        return

    lines = ["🔎 <b>Results</b> — අංකය තොරන්න:\n"]
    buttons = []
    for i, r in enumerate(results, 1):
        t = r.get("title") or r.get("name")
        y = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        icon = "🎬" if r["media_type"] == "movie" else "📺"
        lines.append(f"{i}. {icon} {html.escape(t)} ({y}) ⭐{r.get('vote_average', 0):.1f}")
        buttons.append(
            InlineKeyboardButton(str(i), callback_data=f"pick:{r['media_type']}:{r['id']}")
        )
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if update.effective_user.id not in ADMIN_IDS:
        return
    data = q.data
    if data.startswith("rq:"):
        await handle_req_action(update, context)
        return
    if data.startswith(("add:", "del:", "vip:")):
        await handle_site_action(update, context)
        return

    if data.startswith("pick:"):
        _, media_type, tmdb_id = data.split(":")
        d = await details(media_type, int(tmdb_id))
        poster = f"{IMG}{d['poster_path']}" if d.get("poster_path") else None
        context.user_data["post"] = {
            "caption": build_caption(d, media_type),
            "poster": poster,
            "trailer": trailer_url(d),
        }
        context.user_data["download_url"] = DOWNLOAD_URL
        context.user_data["tmdb"] = (d, media_type)
        await send_post(context.bot, update.effective_chat.id, context.user_data["post"], DOWNLOAD_URL)
        await q.message.reply_text(
            "👆 Preview එක. මොකක් කරමුද?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("➕ Site එකට Add කරන්න", callback_data="add:start")],
                    [
                        InlineKeyboardButton("📣 Channel post only", callback_data="post"),
                        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
                    ],
                ]
            ),
        )

    elif data == "post":
        post = context.user_data.get("post")
        url = context.user_data.get("download_url")
        if not post or not url:
            await q.message.reply_text("⚠️ Session expire. ආයෙ නමක් එවන්න.")
            return
        try:
            await send_post(context.bot, CHANNEL_ID, post, url)
            await q.edit_message_text("✅ Channel එකට post කළා!")
        except Exception as ex:
            log.exception("post failed")
            await q.edit_message_text(f"❌ Failed: {ex}\nBot එක channel admin ද බලන්න.")
        context.user_data.clear()

    elif data == "cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ Cancel කළා.")


# ---------- PREMIUM EXTRAS ----------
AUTOPOST = True
REQ_COOLDOWN = 60
_last_req: dict = {}


_requests: dict = {}   # rid -> record (cache)
_req_msgs: dict = {}   # rid -> [(admin_chat_id, message_id)]

STATUS_LINE = {
    "pending": "⏳ <b>Pending</b>",
    "seen": "👀 <b>Seen</b>",
    "added": "✅ <b>Added</b>",
    "rejected": "❌ <b>Not available</b>",
}
STATUS_ICON = {"pending": "⏳", "seen": "👀", "added": "✅", "rejected": "❌"}


def req_text(rec: dict) -> str:
    line = STATUS_LINE.get(rec["status"], rec["status"])
    if rec.get("by") and rec["status"] != "pending":
        line += f" — {html.escape(rec['by'])}"
    return (
        "📩 <b>New Request</b>\n"
        f"🎬 {html.escape(rec['text'])}\n"
        f"👤 <a href=\"tg://user?id={rec['uid']}\">{html.escape(rec['name'])}</a>\n"
        f"📌 {line}"
    )


def req_keyboard(rec: dict):
    rid, st = rec["id"], rec["status"]
    if st == "pending":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👀 Seen", callback_data=f"rq:seen:{rid}")],
            [
                InlineKeyboardButton("✅ Added", callback_data=f"rq:added:{rid}"),
                InlineKeyboardButton("❌ Not available", callback_data=f"rq:rejected:{rid}"),
            ],
        ])
    if st == "seen":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Added", callback_data=f"rq:added:{rid}"),
            InlineKeyboardButton("❌ Not available", callback_data=f"rq:rejected:{rid}"),
        ]])
    return None


async def save_req(rec: dict):
    try:
        await fb_put(f"telegram_bot/requests/{rec['id']}", rec)
    except Exception:
        log.warning("could not save request %s", rec["id"])


async def handle_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()[:100]
    if not text:
        return
    now = time.time()
    wait = REQ_COOLDOWN - (now - _last_req.get(user.id, 0))
    if wait > 0:
        await update.message.reply_text(f"⏳ තව තත්පර {int(wait)}කින් ආයෙ request කරන්න.")
        return
    _last_req[user.id] = now

    rec = {
        "id": str(int(now * 1000)), "uid": user.id, "name": user.full_name or "User",
        "text": text, "status": "pending", "ts": int(now * 1000),
    }
    _requests[rec["id"]] = rec
    _req_msgs[rec["id"]] = []
    for admin in ADMIN_IDS:
        try:
            msg = await context.bot.send_message(
                admin, req_text(rec), parse_mode="HTML", reply_markup=req_keyboard(rec)
            )
            _req_msgs[rec["id"]].append((admin, msg.message_id))
        except Exception:
            pass
    await save_req(rec)
    await update.message.reply_text(
        "✅ ඔයාගේ request එක ලැබුණා!\nAdmin දැක්කම ඔයාට දැනුම් දෙන්නම් 🎬"
    )


async def handle_req_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, action, rid = q.data.split(":", 2)
    rec = _requests.get(rid)
    if rec is None:
        try:
            rec = await fb_get(f"telegram_bot/requests/{rid}")
        except Exception:
            rec = None
    if not isinstance(rec, dict):
        await q.edit_message_text("⚠️ ඒ request එක හොයාගන්න බෑ.")
        return
    _requests[rid] = rec
    if rec["status"] == action or rec["status"] in ("added", "rejected"):
        try:
            await q.answer("දැනටමත් update කරලා ✔️")
        except Exception:
            pass
        return

    rec["status"] = action
    rec["by"] = update.effective_user.first_name or "Admin"
    await save_req(rec)

    # update every admin's copy of the message
    targets = _req_msgs.get(rid) or [(q.message.chat_id, q.message.message_id)]
    for chat_id, msg_id in targets:
        try:
            await context.bot.edit_message_text(
                req_text(rec), chat_id=chat_id, message_id=msg_id,
                parse_mode="HTML", reply_markup=req_keyboard(rec),
            )
        except Exception:
            pass

    # tell the member
    title = html.escape(rec["text"])
    kb = None
    if action == "seen":
        msg = f"👀 <b>ඔයාගේ request එක admin දැක්කා!</b>\n🎬 {title}\nඉක්මනින් add කරන්නම්."
    elif action == "added":
        msg = f"✅ <b>ඔයාගේ request එක add කළා!</b>\n🎬 {title}\nදැන් site එකෙන් බලන්න 👇"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Open Website", url=SITE_URL)]])
    else:
        msg = f"❌ සමාවෙන්න, <b>{title}</b> දැනට ලබාගන්න අමාරුයි."
    try:
        await context.bot.send_message(rec["uid"], msg, parse_mode="HTML", reply_markup=kb)
    except Exception:
        try:
            await q.answer("Member ට message යවන්න බැරි වුණා", show_alert=True)
        except Exception:
            pass


async def requests_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        data = await fb_get("telegram_bot/requests", orderBy='"$key"', limitToLast="10") or {}
    except Exception:
        await update.message.reply_text("⚠️ Firebase error.")
        return
    recs = sorted(
        (r for r in data.values() if isinstance(r, dict)),
        key=lambda r: num(r.get("ts")), reverse=True,
    )
    if not recs:
        await update.message.reply_text("📭 Requests නෑ.")
        return
    pending = sum(1 for r in recs if r.get("status") in ("pending", "seen"))
    lines = [f"📩 <b>Requests</b> (අන්තිම {len(recs)}, pending: {pending})\n"]
    for r in recs:
        icon = STATUS_ICON.get(r.get("status"), "•")
        lines.append(f"{icon} {html.escape(str(r.get('text')))} — {html.escape(str(r.get('name')))}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def autopost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global AUTOPOST
    if not is_admin(update):
        return
    arg = context.args[0].lower() if context.args else ""
    if arg in ("on", "off"):
        AUTOPOST = arg == "on"
        try:
            await fb_put("telegram_bot/autopost", AUTOPOST)
        except Exception:
            log.warning("could not save autopost setting")
    state = "ON ✅" if AUTOPOST else "OFF ⏸"
    await update.message.reply_text(f"🤖 Auto post: {state}\n\n/autopost on  |  /autopost off")


def num(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def fmt(n: int) -> str:
    return f"{n:,}"


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    wait = await update.message.reply_text("⏳ Stats ගන්නවා...")
    try:
        movies = await fb_get("movies") or {}
        deleted = await fb_get("analytics/deletedStats") or {}
        visitors_total = num(await fb_get("analytics/totalVisitors"))
        hist = await fb_get("telegram_bot/history") or {}
        daily = {}
        for name in ("viewsDaily", "downloadsDaily", "onlineWatchDaily", "visitorsDaily"):
            daily[name] = await fb_get(
                f"analytics/{name}", orderBy='"$key"', limitToLast="7"
            ) or {}
    except Exception:
        log.exception("stats failed")
        await wait.edit_text("⚠️ Firebase error. පස්සේ try කරන්න.")
        return

    items = [(k, v) for k, v in movies.items() if isinstance(v, dict)]
    views = sum(num(v.get("views")) for _, v in items) + num(deleted.get("totalViews"))
    dls = sum(num(v.get("downloads_count")) for _, v in items) + num(deleted.get("totalDownloads"))
    watch = sum(num(v.get("online_watch")) for _, v in items) + num(deleted.get("totalOnlineWatch"))

    # site stores day keys as YYYY-MM-DD (Sri Lanka time)
    lk = timezone(timedelta(hours=5, minutes=30))
    today_key = datetime.now(lk).strftime("%Y-%m-%d")
    week_keys = [(datetime.now(lk) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    def today(name):
        return num(daily[name].get(today_key))

    def week(name):
        return sum(num(daily[name].get(k)) for k in week_keys)

    now_ms = time.time() * 1000
    hist_items = [h for h in hist.values() if isinstance(h, dict)]
    posted_24h = sum(1 for h in hist_items if now_ms - num(h.get("ts")) < 86400000)

    def top(field, n):
        ranked = sorted(items, key=lambda kv: num(kv[1].get(field)), reverse=True)[:n]
        return [
            f"{i}. {html.escape(str(v.get('title', k)))} — <b>{fmt(num(v.get(field)))}</b>"
            for i, (k, v) in enumerate(ranked, 1) if num(v.get(field)) > 0
        ]

    lines = [
        "📊 <b>MovieMania Stats</b>",
        SEP,
        f"🎞 Titles: <b>{fmt(len(items))}</b>   📤 Bot posts: <b>{fmt(len(hist_items))}</b>",
        "",
        "🏆 <b>All time</b>",
        f"👁 Views: <b>{fmt(views)}</b>",
        f"📥 Downloads: <b>{fmt(dls)}</b>",
        f"▶️ Online Watch: <b>{fmt(watch)}</b>",
        f"👥 Visitors: <b>{fmt(visitors_total)}</b>",
        "",
        "📅 <b>Today</b>",
        f"👁 {fmt(today('viewsDaily'))}  |  📥 {fmt(today('downloadsDaily'))}  |  "
        f"▶️ {fmt(today('onlineWatchDaily'))}  |  👥 {fmt(today('visitorsDaily'))}",
        "",
        "📆 <b>Last 7 days</b>",
        f"👁 {fmt(week('viewsDaily'))}  |  📥 {fmt(week('downloadsDaily'))}  |  "
        f"▶️ {fmt(week('onlineWatchDaily'))}  |  👥 {fmt(week('visitorsDaily'))}",
    ]
    tv, td = top("views", 5), top("downloads_count", 3)
    if tv:
        lines += ["", "🔥 <b>Top viewed</b>"] + tv
    if td:
        lines += ["", "⬇️ <b>Top downloaded</b>"] + td
    lines += [
        "", SEP,
        f"🤖 Auto post: <b>{'ON' if AUTOPOST else 'OFF'}</b>   🕐 Posted 24h: <b>{posted_24h}</b>",
    ]
    await wait.edit_text("\n".join(lines), parse_mode="HTML")


async def last_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        hist = await fb_get("telegram_bot/history") or {}
    except Exception:
        await update.message.reply_text("⚠️ Firebase error.")
        return
    items = sorted(
        (h for h in hist.values() if isinstance(h, dict)),
        key=lambda h: float(h.get("ts", 0)), reverse=True,
    )[:5]
    if not items:
        await update.message.reply_text("තාම post history එකක් නෑ.")
        return
    lines = ["🕘 <b>Last posts</b>\n"]
    for i, h in enumerate(items, 1):
        lines.append(f"{i}. {html.escape(str(h.get('title', '?')))}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def repost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /repost movie-id\n(උදා: /repost buddy-21)")
        return
    movie_id = context.args[0]
    try:
        m = await fb_get(f"movies/{movie_id}")
        if not isinstance(m, dict):
            await update.message.reply_text("❌ ඒ ID එකෙන් movie එකක් නෑ.")
            return
        await post_site_movie(context.bot, movie_id, m)
        await update.message.reply_text(f"✅ Reposted: {m.get('title')}")
    except Exception as ex:
        log.exception("repost failed")
        await update.message.reply_text(f"❌ Failed: {ex}")


# ---------- SITE ADMIN (add / manage movies from Telegram) ----------
TMDB_IMG = "https://image.tmdb.org/t/p/"
QUALITIES = ["WEB-RIP", "WEB-DL", "BLURAY", "HD-RIP", "CAM", "CAMCOPY", "ORIGINAL WEB", "4K"]
SUB_BY = os.environ.get("SUB_BY", "Piratelk.com")


def pd_convert(url: str) -> str:
    """Same logic as pdConvert() in the admin panel (Pixeldrain share -> stream link)."""
    url = (url or "").strip()
    if not url:
        return url
    if re.search(r"pixeldrain\.com/api/file/[A-Za-z0-9_-]+$", url) and "?download" not in url:
        return url
    if "/api/file/" in url and "?download" in url:
        return url.replace("?download", "")
    m = re.search(r"pixeldrain\.com/[ul]/([A-Za-z0-9_-]+)", url)
    if m:
        return f"https://pixeldrain.com/api/file/{m.group(1)}"
    return url


def slugify(t: str) -> str:
    t = re.sub(r"['’`]", "", (t or "").lower())
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:80] or "untitled"


async def unique_movie_id(title: str) -> str:
    import random
    base = slugify(title)
    for _ in range(8):
        cid = f"{base}-{random.randint(10, 99)}"
        if await fb_get(f"movies/{cid}") is None:
            return cid
    return f"{base}-{str(int(time.time() * 1000))[-4:]}"


def draft_from_tmdb(d: dict, media_type: str) -> dict:
    """Builds the same record the admin panel's saveMovie() writes."""
    vids = d.get("videos", {}).get("results", [])
    yt = [v for v in vids if v.get("site") == "YouTube"]
    trailer = (
        next((v for v in yt if v.get("type") == "Trailer" and v.get("official")), None)
        or next((v for v in yt if v.get("type") == "Trailer"), None)
        or (yt[0] if yt else None)
    )
    crew = d.get("credits", {}).get("crew", [])
    cast = d.get("credits", {}).get("cast", [])
    if d.get("production_countries"):
        country = ", ".join(c["name"] for c in d["production_countries"])
    else:
        country = ", ".join(d.get("origin_country") or [])
    mins = d.get("runtime") or 0
    backdrops = d.get("images", {}).get("backdrops", [])[:6]
    date = d.get("release_date") or d.get("first_air_date") or ""
    vote = d.get("vote_average") or 0
    coll = (d.get("belongs_to_collection") or {}).get("name", "")
    return {
        "title": d.get("title") or d.get("name") or "",
        "year": date[:4],
        "genre": ", ".join(g["name"] for g in d.get("genres", [])),
        "runtime": f"{mins // 60}h {mins % 60}m" if mins else "",
        "rating": f"{vote:.1f}" if vote else "",
        "description": d.get("overview") or "",
        "describedBy": "",
        "poster": f"{TMDB_IMG}original{d['poster_path']}" if d.get("poster_path") else "",
        "backdrop": f"{TMDB_IMG}original{d['backdrop_path']}" if d.get("backdrop_path") else "",
        "director": ", ".join(c["name"] for c in crew if c.get("job") == "Director"),
        "cast": ", ".join(c["name"] for c in cast[:8]),
        "country": country,
        "trailer": f"https://www.youtube.com/watch?v={trailer['key']}" if trailer else "",
        "screenshots": [f"{TMDB_IMG}original{b['file_path']}" for b in backdrops],
        "quality": "WEB-RIP",
        "type": media_type,
        "players": [],
        "subBy": SUB_BY,
        "subtitleUrl": "",
        "subUrl": "",
        "dlSubUrl": "",
        "adult": bool(d.get("adult")),
        "tmdbId": str(d.get("id") or ""),
        "collection": coll,
        "collectionOrder": 0,
        "adminNote": "",
        "downloads": [],
        "seasons": [],
        "seriesStatus": "ongoing",
        "isVip": False,
        "showSubNotice": False,
        "scheduledAt": None,
    }


TG_RE = re.compile(r"(?:t\.me|telegram\.me)/", re.I)
RES_RE = re.compile(r"(?<![0-9])(2160|1080|720|480|360)p?(?![0-9a-z])", re.I)
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s?(GB|MB)", re.I)


def quality_from_name(name: str) -> str:
    """WEB-DL / WEB-RIP / BLURAY / HD-RIP / CAM / 4K from a file name."""
    n = (name or "").lower()
    if re.search(r"web[-._ ]?dl", n):
        return "WEB-DL"
    if re.search(r"web[-._ ]?rip", n):
        return "WEB-RIP"
    if re.search(r"blu[-._ ]?ray|bdrip|brrip", n):
        return "BLURAY"
    if re.search(r"hd[-._ ]?rip", n):
        return "HD-RIP"
    if re.search(r"hd[-._ ]?cam|cam[-._ ]?rip|(?<![a-z])cam(?![a-z])", n):
        return "CAM"
    if re.search(r"2160p|(?<![a-z0-9])4k(?![a-z0-9])|uhd", n):
        return "4K"
    return ""


def human_size(num_bytes) -> str:
    b = num(num_bytes)
    if b <= 0:
        return ""
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f} GB"
    return f"{b / 1024 ** 2:.0f} MB"


async def pd_info(url: str):
    """Pixeldrain file info (name, size) so resolution / size / quality can be auto-detected."""
    m = re.search(r"pixeldrain\.com/(?:u|api/file)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://pixeldrain.com/api/file/{m.group(1)}/info")
            if r.status_code == 200:
                return r.json()
    except Exception:
        log.warning("pixeldrain info failed for %s", url)
    return None


async def build_downloads(text: str, quality: str):
    """Each line: links (+ optional resolution / size). Missing resolution, size and quality
    are auto-detected from the Pixeldrain file name. t.me -> Telegram button; first other link
    -> main download; extra links -> server 1-3; same resolution lines are merged.
    Returns (downloads, players, detected_quality)."""
    by_res: dict = {}
    players = []
    detected = ""
    for line in text.splitlines():
        urls = re.findall(r"https?://\S+", line)
        if not urls:
            continue
        tg = [u for u in urls if TG_RE.search(u)]
        others = [u for u in urls if not TG_RE.search(u)]
        words = line
        for u in urls:
            words = words.replace(u, " ")

        info = await pd_info(others[0]) if others else None
        fname = (info or {}).get("name", "")

        rm = RES_RE.search(words) or RES_RE.search(" ".join(others)) or RES_RE.search(fname)
        res = int(rm.group(1)) if rm else 720
        res_s = f"{res}p"
        sm = SIZE_RE.search(words) or SIZE_RE.search(" ".join(others))
        size = f"{sm.group(1)} {sm.group(2).upper()}" if sm else human_size((info or {}).get("size"))
        q = quality_from_name(fname) or quality_from_name(" ".join(others)) or quality_from_name(words)
        detected = detected or q

        e = by_res.setdefault(res_s, {
            "quality": q or quality, "res": res_s, "size": "", "url": "",
            "pd480": "", "pd720": "", "pd1080": "",
            "tgLink": "", "srv1": "", "srv2": "", "srv3": "",
        })
        if size and not e["size"]:
            e["size"] = size
        if tg and not e["tgLink"]:
            e["tgLink"] = tg[0]
        if others:
            if not e["url"]:
                e["url"] = others[0]
                extra = others[1:4]
            else:
                extra = others[:3]
            for u in extra:
                for k in ("srv1", "srv2", "srv3"):
                    if not e[k]:
                        e[k] = u
                        break
            pdv = pd_convert(others[0])
            if "pixeldrain.com/api/file/" in pdv:
                if res in (480, 720, 1080) and not e[f"pd{res}"]:
                    e[f"pd{res}"] = pdv
                players.append({"name": "MovieManiaLK", "quality": res_s, "url": pdv, "type": "direct"})
    return list(by_res.values()), players, detected


def detect_player_type(name: str, url: str) -> str:
    if name == "Facebook":
        return "facebook"
    if name == "VLC":
        return "vlc"
    return "hls" if ".m3u8" in url else "direct"


def parse_players(text: str):
    """Each line: [Facebook|VLC|MovieManiaLK] [720p] <video url or Facebook video ID>."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        name = "Facebook" if "facebook" in low else "VLC" if "vlc" in low else "MovieManiaLK"
        um = re.search(r"https?://\S+", line)
        rest = line
        if um:
            url = um.group(0)
            rest = line.replace(url, " ")
        else:
            fm = re.search(r"\b\d{8,}\b", line)
            if not (name == "Facebook" and fm):
                continue
            url = f"https://fb-con.vercel.app/video?video_id={fm.group(0)}"
            rest = line.replace(fm.group(0), " ")
        rm = re.search(r"\b(2160|1080|720|480|360)p?\b", rest)
        quality = f"{rm.group(1)}p" if rm else "720p"
        if name != "Facebook":
            url = pd_convert(url)
        out.append({"name": name, "quality": quality, "url": url,
                    "type": detect_player_type(name, url)})
    return out


def draft_summary(dr: dict, meta: dict) -> str:
    e = html.escape
    links = ", ".join(
        f"{x['res']}{(' ' + x['size']) if x['size'] else ''}{' 📨' if x.get('tgLink') else ''}"
        for x in dr["downloads"]
    ) or "නෑ"
    lines = [
        "➕ <b>Add to Site</b>", SEP,
        f"🎬 <b>{e(dr['title'])}</b> ({e(dr['year'])})",
        f"💿 Quality: <b>{e(dr['quality'])}</b>   ⭐ VIP: <b>{'ON' if dr['isVip'] else 'OFF'}</b>",
        f"📥 Links: <b>{e(links)}</b>",
        f"▶️ Players: <b>{len(dr['players']) or '—'}</b>   💬 Subtitle file: <b>{'✅' if dr['dlSubUrl'] else '—'}</b>",
        f"🏷 Subtitle by: <b>{e(dr['subBy'])}</b>",
        f"📝 Description: <b>{len(dr['description'])} chars</b>" + (" ✏️" if meta.get("custom_desc") else ""),
    ]
    if meta.get("dup"):
        lines.append(f"⚠️ Site එකේ දැනටමත් තියෙනවා: <code>{e(meta['dup'])}</code>")
    return "\n".join(lines)


def draft_keyboard(dr: dict) -> InlineKeyboardMarkup:
    have = {x["res"] for x in dr["downloads"]}
    res_row = [
        InlineKeyboardButton(f"{'✅' if r in have else '➕'} {r}", callback_data=f"add:dl:{r[:-1]}")
        for r in ("480p", "720p", "1080p")
    ]
    return InlineKeyboardMarkup([
        res_row,
        [
            InlineKeyboardButton("🔗 Bulk links", callback_data="add:links"),
            InlineKeyboardButton("▶️ Player", callback_data="add:player"),
        ],
        [
            InlineKeyboardButton(f"💿 {dr['quality']}", callback_data="add:qual"),
            InlineKeyboardButton(f"⭐ VIP: {'ON' if dr['isVip'] else 'OFF'}", callback_data="add:vip"),
        ],
        [
            InlineKeyboardButton("📝 Description", callback_data="add:desc"),
            InlineKeyboardButton("🏷 Subtitle By", callback_data="add:subby"),
        ],
        [InlineKeyboardButton("💬 Subtitle file link", callback_data="add:sub")],
        [cbtn("✅ Save & Post to Channel", "add:savepost", "success")],
        [
            InlineKeyboardButton("💾 Save only", callback_data="add:saveonly"),
            cbtn("❌ Cancel", "add:cancel", "danger"),
        ],
    ])


def norm_tg(text: str):
    t = text.strip()
    if re.match(r"^(?:t\.me|telegram\.me)/", t, re.I):
        t = "https://" + t
    return t if t.startswith("http") else None


def norm_size(text: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB|G|M)?", text, re.I)
    if not m:
        return None
    unit = (m.group(2) or "GB").upper()
    return f"{m.group(1)} {'GB' if unit.startswith('G') else 'MB'}"


def players_from_downloads(downloads: list) -> list:
    out = []
    for x in downloads:
        pdv = pd_convert(x.get("url", ""))
        if "pixeldrain.com/api/file/" in pdv:
            out.append({"name": "MovieManiaLK", "quality": x["res"], "url": pdv, "type": "direct"})
    return out


async def handle_dl_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """One resolution at a time: Telegram link -> download URL -> size."""
    ud = context.user_data
    dr = ud.get("draft")
    res = ud.get("dl_res")
    if not dr or not res:
        ud.clear()
        await update.message.reply_text("⚠️ Session expire. ආයෙ /add කරන්න.")
        return
    tmp = ud.setdefault("dl_tmp", {})
    skip = text.lower() in ("/skip", "skip", "-")
    st = ud["state"]

    if st == "dl_tg":
        if not skip:
            tg = norm_tg(text)
            if not tg:
                await update.message.reply_text("❌ t.me link එකක් එවන්න (නැත්නම් /skip).")
                return
            tmp["tg"] = tg
        ud["state"] = "dl_url"
        await update.message.reply_text(
            f"🔗 <b>{res}</b> — Download URL එක එවන්න." + ("\n(t.me link එක දුන්නා, URL එක නැත්නම් /skip)" if tmp.get("tg") else ""),
            parse_mode="HTML",
        )
        return

    if st == "dl_url":
        if text.lower() == "remove":
            dr["downloads"] = [x for x in dr["downloads"] if x["res"] != res]
            if not ud.get("draft_meta", {}).get("custom_players"):
                dr["players"] = players_from_downloads(dr["downloads"])
            for k in ("state", "dl_res", "dl_tmp"):
                ud.pop(k, None)
            await update.message.reply_text(
                draft_summary(dr, ud.get("draft_meta", {})), parse_mode="HTML", reply_markup=draft_keyboard(dr))
            return
        if skip:
            if not tmp.get("tg"):
                await update.message.reply_text("❌ t.me link එකක්වත් නැති නිසා Download URL එක ඕනේ.")
                return
        else:
            if not text.startswith("http"):
                await update.message.reply_text("❌ Link එක http:// හෝ https:// වලින් පටන් ගන්න ඕනේ.")
                return
            tmp["url"] = text
        ud["state"] = "dl_size"
        await update.message.reply_text(
            f"💿 <b>{res}</b> — File size එක එවන්න (උදා <code>1.5GB</code>). /skip කළොත් auto detect කරනවා.",
            parse_mode="HTML",
        )
        return

    # dl_size -> build the entry
    size = None if skip else norm_size(text)
    if not skip and not size:
        await update.message.reply_text("❌ Size එක හරියට එවන්න (උදා 1.5GB) හෝ /skip.")
        return
    line = " ".join(x for x in (res, size or "", tmp.get("url", ""), tmp.get("tg", "")) if x)
    entries, _players, det = await build_downloads(line, dr["quality"])
    if not entries:
        await update.message.reply_text("⚠️ Link එකක් හොයාගන්න බැරි වුණා. ආයෙ try කරන්න.")
        return
    dr["downloads"] = sorted(
        [x for x in dr["downloads"] if x["res"] != res] + entries,
        key=lambda x: num(x["res"].rstrip("p")),
    )
    if det:
        dr["quality"] = det
    for x in dr["downloads"]:  # one quality label for the whole movie
        x["quality"] = dr["quality"]
    if not ud.get("draft_meta", {}).get("custom_players"):
        dr["players"] = players_from_downloads(dr["downloads"])
    for k in ("state", "dl_res", "dl_tmp"):
        ud.pop(k, None)
    await update.message.reply_text(
        draft_summary(dr, ud.get("draft_meta", {})), parse_mode="HTML", reply_markup=draft_keyboard(dr))


LINKS_PROMPT = (
    "🔗 <b>Download links එවන්න</b> (පේළියකට එක quality එකක්)\n\n"
    "<code>720p 1.2GB https://pixeldrain.com/u/abc https://t.me/yourbot?start=abc720\n"
    "1080p 2.4GB https://pixeldrain.com/u/xyz https://t.me/yourbot?start=xyz1080</code>\n\n"
    "• <b>t.me</b> link එක → Telegram download button එකට\n"
    "• ඉතුරු පළවෙනි link එක → main download, තව links → Server 2/3/4\n"
    "• Pixeldrain link නම් resolution, size, quality (WEB-DL...) file name එකෙන් auto detect වෙනවා\n"
    "• Pixeldrain link Watch Online එකටත් auto set වෙනවා"
)


async def handle_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    ud = context.user_data
    dr = ud.get("draft")
    if not dr:
        ud.clear()
        await update.message.reply_text("⚠️ Session expire. ආයෙ නමක් එවන්න.")
        return
    meta = ud.setdefault("draft_meta", {})
    skip = text.lower() in ("/skip", "skip", "-")
    st = ud["state"]
    if st == "add_links":
        downloads, players, det = await build_downloads(text, dr["quality"])
        if not downloads:
            await update.message.reply_text("❌ http:// හෝ https:// link එකක් හොයාගන්න බෑ. ආයෙ එවන්න.")
            return
        dr["downloads"] = downloads
        if det:
            dr["quality"] = det  # auto-detected from the file name
        if not meta.get("custom_players"):  # keep hand-made players
            dr["players"] = players
    elif st == "add_player":
        if not skip:
            players = parse_players(text)
            if not players:
                await update.message.reply_text("❌ Video link එකක් හොයාගන්න බෑ. ආයෙ එවන්න (නැත්නම් /skip).")
                return
            dr["players"] = players
            meta["custom_players"] = True
    elif st == "add_desc":
        if not skip:
            dr["description"] = text
            meta["custom_desc"] = True
    elif st == "add_subby":
        if not skip:
            dr["subBy"] = text[:60]
    else:  # add_sub
        if not skip:
            if not text.startswith("http"):
                await update.message.reply_text("❌ Link එක http:// වලින් පටන් ගන්න ඕනේ. නැත්නම් /skip")
                return
            dr["dlSubUrl"] = text
            if text.lower().split("?")[0].endswith((".srt", ".vtt")):
                dr["subUrl"] = dr["subtitleUrl"] = text
    ud["state"] = None
    await update.message.reply_text(
        draft_summary(dr, ud.get("draft_meta", {})), parse_mode="HTML",
        reply_markup=draft_keyboard(dr),
    )


async def handle_site_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    ud = context.user_data
    parts = q.data.split(":", 2)
    kind, action = parts[0], parts[1]

    # ---- delete / vip (from /del and /vip) ----
    if kind == "del":
        if action == "no":
            await q.edit_message_text("❌ Cancel කළා.")
            return
        mid = parts[2]
        try:
            m = await fb_get(f"movies/{mid}")
            if not isinstance(m, dict):
                await q.edit_message_text("❌ ඒ movie එක නෑ.")
                return
            v, dl, sub = num(m.get("views")), num(m.get("downloads_count")), num(m.get("subtitle_downloads_count"))
            if v or dl or sub:  # keep history, same as the admin panel
                cur = await fb_get("analytics/deletedStats") or {}
                await fb_put("analytics/deletedStats", {
                    "totalViews": num(cur.get("totalViews")) + v,
                    "totalDownloads": num(cur.get("totalDownloads")) + dl,
                    "totalSubDownloads": num(cur.get("totalSubDownloads")) + sub,
                    "deletedCount": num(cur.get("deletedCount")) + 1,
                })
            await fb_delete(f"movies/{mid}")
            await q.edit_message_text(f"🗑 Deleted: {html.escape(str(m.get('title')))}", parse_mode="HTML")
        except Exception as ex:
            log.exception("delete failed")
            await q.edit_message_text(f"❌ Failed: {ex}")
        return
    if kind == "vip":
        mid = action if len(parts) == 2 else f"{action}:{parts[2]}"
        try:
            m = await fb_get(f"movies/{mid}")
            if not isinstance(m, dict):
                await q.edit_message_text("❌ ඒ movie එක නෑ.")
                return
            new = not bool(m.get("isVip"))
            await fb_put(f"movies/{mid}/isVip", new)
            await q.edit_message_text(
                f"⭐ <b>{html.escape(str(m.get('title')))}</b>\nVIP: <b>{'ON' if new else 'OFF'}</b>",
                parse_mode="HTML",
            )
        except Exception as ex:
            await q.edit_message_text(f"❌ Failed: {ex}")
        return

    # ---- add flow ----
    if action == "start":
        tmdb = ud.get("tmdb")
        if not tmdb:
            await q.message.reply_text("⚠️ Session expire. ආයෙ නමක් එවන්න.")
            return
        d, media_type = tmdb
        if media_type == "tv":
            await q.message.reply_text(
                "📺 TV Series (seasons/episodes) bot එකෙන් add කරන්න තාම බෑ.\n"
                "Admin panel එකෙන් add කරන්න. (Channel post only වැඩ කරනවා.)"
            )
            return
        dr = draft_from_tmdb(d, media_type)
        meta = {}
        try:  # warn if this TMDB id is already on the site
            for k, v in (await fb_get("movies") or {}).items():
                if isinstance(v, dict) and str(v.get("tmdbId")) == dr["tmdbId"]:
                    meta["dup"] = k
                    break
        except Exception:
            pass
        ud["draft"], ud["draft_meta"], ud["state"] = dr, meta, None
        await q.message.reply_text(
            draft_summary(dr, meta) + "\n\n👇 <b>480p / 720p / 1080p</b> button එබුවම links අහනවා.",
            parse_mode="HTML", reply_markup=draft_keyboard(dr),
        )
        return

    dr = ud.get("draft")
    if not dr:
        await q.edit_message_text("⚠️ Session expire. ආයෙ නමක් එවන්න.")
        return
    meta = ud.get("draft_meta", {})

    if action == "cancel":
        ud.clear()
        await q.edit_message_text("❌ Cancel කළා.")
    elif action in ("qual", "vip"):
        if action == "qual":
            i = QUALITIES.index(dr["quality"]) if dr["quality"] in QUALITIES else -1
            dr["quality"] = QUALITIES[(i + 1) % len(QUALITIES)]
            for x in dr["downloads"]:
                x["quality"] = dr["quality"]
        else:
            dr["isVip"] = not dr["isVip"]
        await q.edit_message_text(draft_summary(dr, meta), parse_mode="HTML", reply_markup=draft_keyboard(dr))
    elif action == "dl":
        res = f"{parts[2]}p"
        ud["dl_res"], ud["dl_tmp"], ud["state"] = res, {}, "dl_tg"
        cur = next((x for x in dr["downloads"] if x["res"] == res), None)
        note = f"\n\nදැනට: <code>{html.escape(cur['url'] or cur['tgLink'])[:200]}</code>\n(මකන්න ඕනේ නම් URL step එකේදී <code>remove</code> කියලා ලියන්න)" if cur else ""
        await q.message.reply_text(
            f"📨 <b>{res}</b> — Telegram (t.me) link එක එවන්න.\nනැත්නම් /skip{note}",
            parse_mode="HTML",
        )
    elif action == "links":
        ud["state"] = "add_links"
        await q.message.reply_text(LINKS_PROMPT, parse_mode="HTML")
    elif action == "player":
        ud["state"] = "add_player"
        cur = "\n".join(f"{p['name']} {p['quality']} {p['url']}" for p in dr["players"]) or "—"
        await q.message.reply_text(
            "▶️ <b>Player link(s) එවන්න</b> (පේළියකට එකක්)\n\n"
            "<code>720p https://example.com/video.mp4\n"
            "VLC 1080p https://example.com/stream.m3u8\n"
            "Facebook 720p 1234567890123</code>\n\n"
            "නම නොදුන්නොත් <b>MovieManiaLK</b>. Facebook නම් video ID එකත් ඇති.\n"
            f"දැන්: <code>{html.escape(cur)[:400]}</code>\n\nවෙනස් නොකරන්න /skip",
            parse_mode="HTML",
        )
    elif action == "desc":
        ud["state"] = "add_desc"
        await q.message.reply_text(
            "📝 <b>Description එක එවන්න</b> (සිංහලෙන් වුණත් ඉතිං).\n"
            f"දැන්: <i>{html.escape(dr['description'])[:300]}</i>\n\nවෙනස් නොකරන්න /skip",
            parse_mode="HTML",
        )
    elif action == "subby":
        ud["state"] = "add_subby"
        await q.message.reply_text(
            f"🏷 <b>Subtitle By</b> නම එවන්න.\nදැන්: <b>{html.escape(dr['subBy'])}</b>\n\nවෙනස් නොකරන්න /skip",
            parse_mode="HTML",
        )
    elif action == "sub":
        ud["state"] = "add_sub"
        await q.message.reply_text("💬 Subtitle <b>file එක</b> (.srt / .vtt / .zip) කෙලින්ම forward/upload කරන්න, නැත්නම් link එකක් එවන්න. /skip", parse_mode="HTML")
    elif action in ("savepost", "saveonly"):
        if not dr["downloads"]:
            await q.answer("Download link එකක් මුලින් දාන්න!", show_alert=True)
            return
        try:
            movie = dict(dr)
            movie["addedAt"] = int(time.time() * 1000)
            cid = await unique_movie_id(movie["title"])
            POSTED.add(cid)  # so the auto-post loop doesn't post it a second time
            await fb_put(f"movies/{cid}", movie)
        except Exception as ex:
            log.exception("site save failed")
            await q.edit_message_text(f"❌ Save failed: {ex}")
            return
        ud.clear()
        result = (
            f"✅ <b>Site එකට add කළා!</b>\n🎬 {html.escape(movie['title'])}\n"
            f"🆔 <code>{html.escape(cid)}</code>"
        )
        try:
            await fb_put(f"{POSTED_PATH}/{cid}", True)
        except Exception:
            pass
        if action == "savepost":
            try:
                await post_site_movie(context.bot, cid, movie)
                result += "\n📢 Channel එකට post කළා"
                await fb_put(
                    f"telegram_bot/history/{cid}",
                    {"title": movie["title"], "ts": int(time.time() * 1000)},
                )
            except Exception as ex:
                log.exception("post after save failed")
                result += f"\n⚠️ Post failed: {ex}\n(/repost {cid})"
        else:
            result += "\n💾 Channel එකට post කළේ නෑ (/repost " + cid + ")"
        await q.edit_message_text(
            result, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[btn("🌐 Open on site", movie_link(cid, movie), "primary")]]),
        )


# ---------- SUBTITLE FILE UPLOAD (send .srt/.vtt/.zip to the bot) ----------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://oqisvxsvkjyltvpwnago.supabase.co")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY", "")  # optional - Railway variable only
SUPABASE_SUB_BUCKET = os.environ.get("SUPABASE_SUB_BUCKET", "subtitles")
FIREBASE_STORAGE_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "cineflix1-1fdc6.firebasestorage.app")
SUB_CTYPES = {".srt": "text/plain; charset=utf-8", ".vtt": "text/vtt; charset=utf-8", ".zip": "application/zip"}


async def upload_subtitle(filename: str, data: bytes, ext: str) -> str:
    """Same targets as the site's /api/admin/upload: Supabase if a key is set, else Firebase Storage."""
    ctype = SUB_CTYPES.get(ext, "application/octet-stream")
    safe = re.sub(r"[^\w.\-]+", "_", filename)
    async with httpx.AsyncClient(timeout=60) as client:
        if SUPABASE_SECRET_KEY:
            r = await client.post(
                f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_SUB_BUCKET}/{safe}",
                headers={"Authorization": f"Bearer {SUPABASE_SECRET_KEY}", "apikey": SUPABASE_SECRET_KEY,
                         "Content-Type": ctype, "x-upsert": "true"},
                content=data,
            )
            r.raise_for_status()
            return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_SUB_BUCKET}/{safe}"
        from urllib.parse import quote
        path = f"subtitles/{int(time.time() * 1000)}_{safe}"
        r = await client.post(
            f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_STORAGE_BUCKET}/o?name={quote(path, safe='')}",
            headers={"Content-Type": ctype}, content=data,
        )
        r.raise_for_status()
        tok = r.json().get("downloadTokens")
        return (f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_STORAGE_BUCKET}/o/"
                f"{quote(path, safe='')}?alt=media" + (f"&token={tok}" if tok else ""))


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    ud = context.user_data
    dr = ud.get("draft")
    doc = update.message.document
    if not dr:
        await update.message.reply_text("ℹ️ මුලින් /add එකෙන් movie එකක් තොරන්න, ඊට පස්සේ subtitle file එක එවන්න.")
        return
    name = (doc.file_name or "subtitle").strip()
    ext = os.path.splitext(name)[1].lower()
    if ext not in SUB_CTYPES:
        await update.message.reply_text("❌ .srt / .vtt / .zip files විතරයි.")
        return
    if (doc.file_size or 0) > 20 * 1024 * 1024:
        await update.message.reply_text("❌ File එක 20MB ට වැඩියි.")
        return
    wait = await update.message.reply_text("⏳ Subtitle upload කරනවා...")
    try:
        f = await context.bot.get_file(doc.file_id)
        data = bytes(await f.download_as_bytearray())
        url = await upload_subtitle(f"{slugify(dr['title'])}-{dr['year']}-{int(time.time()) % 100000}{ext}", data, ext)
    except Exception as ex:
        log.exception("subtitle upload failed")
        await wait.edit_text(f"❌ Upload failed: {ex}")
        return
    dr["dlSubUrl"] = url
    if ext in (".srt", ".vtt"):
        dr["subUrl"] = dr["subtitleUrl"] = url
    ud["state"] = None
    await wait.edit_text(
        draft_summary(dr, ud.get("draft_meta", {})), parse_mode="HTML", reply_markup=draft_keyboard(dr)
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data.clear()
    await update.message.reply_text(
        "➕ <b>Add Movie</b>\n\n🎬 Movie එකේ නම type කරන්න (TMDB එකෙන් හොයනවා)\n\n/cancel – නවත්වන්න",
        parse_mode="HTML",
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data.clear()
    await update.message.reply_text("❌ Cancel කළා.")


def cbtn(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    if not style:
        return InlineKeyboardButton(text, callback_data=data)
    try:
        return InlineKeyboardButton(text, callback_data=data, style=style)
    except TypeError:
        return InlineKeyboardButton(text, callback_data=data, api_kwargs={"style": style})


async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    term = " ".join(context.args).strip().lower()
    if not term:
        await update.message.reply_text("Usage: /find movie-name")
        return
    try:
        movies = await fb_get("movies") or {}
    except Exception:
        await update.message.reply_text("⚠️ Firebase error.")
        return
    hits = [(k, v) for k, v in movies.items()
            if isinstance(v, dict) and term in str(v.get("title", "")).lower()][:8]
    if not hits:
        await update.message.reply_text("😕 කිසිවක් හම්බුනේ නෑ.")
        return
    lines = ["🔎 <b>Results</b>\n"]
    for k, v in hits:
        vip = " ⭐VIP" if v.get("isVip") else ""
        lines.append(
            f"🎬 <b>{html.escape(str(v.get('title')))}</b> ({html.escape(str(v.get('year', '')))}){vip}\n"
            f"   <code>{html.escape(k)}</code>  👁 {fmt(num(v.get('views')))}  📥 {fmt(num(v.get('downloads_count')))}"
        )
    lines.append("\n/vip id  •  /del id  •  /repost id")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /del movie-id  (id එක /find එකෙන් ගන්න)")
        return
    mid = context.args[0]
    m = await fb_get(f"movies/{mid}")
    if not isinstance(m, dict):
        await update.message.reply_text("❌ ඒ ID එකෙන් movie එකක් නෑ.")
        return
    await update.message.reply_text(
        f"🗑 <b>{html.escape(str(m.get('title')))}</b> delete කරන්නද?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Yes, delete", callback_data=f"del:yes:{mid}"),
            InlineKeyboardButton("❌ No", callback_data="del:no"),
        ]]),
    )


async def vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /vip movie-id")
        return
    mid = context.args[0]
    m = await fb_get(f"movies/{mid}")
    if not isinstance(m, dict):
        await update.message.reply_text("❌ ඒ ID එකෙන් movie එකක් නෑ.")
        return
    new = not bool(m.get("isVip"))
    await fb_put(f"movies/{mid}/isVip", new)
    await update.message.reply_text(
        f"⭐ <b>{html.escape(str(m.get('title')))}</b>\nVIP: <b>{'ON' if new else 'OFF'}</b>", parse_mode="HTML"
    )


# ---------- AUTO POST (admin panel -> channel) ----------
async def fb_get(path: str, **params):
    if FIREBASE_API_KEY:
        params["key"] = FIREBASE_API_KEY
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{FIREBASE_DB_URL}/{path}.json", params=params)
        r.raise_for_status()
        return r.json()


async def fb_delete(path: str):
    params = {"key": FIREBASE_API_KEY} if FIREBASE_API_KEY else {}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.delete(f"{FIREBASE_DB_URL}/{path}.json", params=params)
        r.raise_for_status()


async def fb_put(path: str, value):
    params = {"key": FIREBASE_API_KEY} if FIREBASE_API_KEY else {}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.put(f"{FIREBASE_DB_URL}/{path}.json", params=params, json=value)
        r.raise_for_status()


def site_caption(m: dict) -> str:
    try:
        rating = float(m.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    genres = [g.strip() for g in str(m.get("genre") or "").split(",") if g.strip()]
    kind = "📺" if m.get("type") in ("tv", "series") else "🎬"
    return render_card(
        m.get("title") or "Unknown", m.get("year") or "", kind, rating, genres,
        m.get("runtime"), m.get("quality"), m.get("country"),
        m.get("director"), m.get("cast"), m.get("description"),
    )


def movie_link(movie_id: str, m: dict) -> str:
    kind = "tv" if m.get("type") in ("tv", "series") else "movie"
    return f"{SITE_URL}/{kind}/{movie_id}-sinhala-subtitles"


async def post_site_movie(bot, movie_id: str, m: dict):
    caption = site_caption(m)
    kb = post_keyboard(movie_link(movie_id, m), (m.get("trailer") or "").strip() or None)
    poster = (m.get("poster") or m.get("backdrop") or "").strip()
    poster = poster.replace("/t/p/original/", "/t/p/w780/")  # Telegram photo size limit
    if poster.startswith("http"):
        try:
            await bot.send_photo(CHANNEL_ID, poster, caption=caption, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            log.exception("photo failed, sending text")
    await bot.send_message(CHANNEL_ID, caption, parse_mode="HTML", reply_markup=kb)


def is_due(m: dict) -> bool:
    sched = m.get("scheduledAt")
    return not sched or float(sched) <= time.time() * 1000


async def check_new_movies(bot, posted: set):
    keys = await fb_get("movies", shallow="true") or {}
    for movie_id in keys:
        if movie_id in posted:
            continue
        m = await fb_get(f"movies/{movie_id}")
        if not isinstance(m, dict) or not is_due(m):
            continue  # scheduled for later - check again next round
        if m.get("adult"):
            posted.add(movie_id)  # never auto-post adult content
            continue
        try:
            await post_site_movie(bot, movie_id, m)
        except Exception:
            log.exception("auto post failed: %s", movie_id)
            continue
        posted.add(movie_id)
        try:
            await fb_put(f"{POSTED_PATH}/{movie_id}", True)
        except Exception:
            log.warning("could not persist posted flag for %s", movie_id)
        try:
            await fb_put(
                f"telegram_bot/history/{movie_id}",
                {"title": m.get("title"), "ts": int(time.time() * 1000)},
            )
        except Exception:
            pass
        for admin in ADMIN_IDS:
            try:
                await bot.send_message(admin, f"✅ Auto posted: {m.get('title')}")
            except Exception:
                pass
        await asyncio.sleep(3)  # Telegram flood control


async def auto_post_loop(app: Application):
    if not FIREBASE_DB_URL:
        log.warning("FIREBASE_DB_URL not set - auto post disabled")
        return
    bot = app.bot
    global AUTOPOST
    try:
        if await fb_get("telegram_bot/autopost") is False:
            AUTOPOST = False
    except Exception:
        pass
    # Load already-posted ids. First ever run: mark everything existing as posted
    # so the channel is not flooded with old movies.
    try:
        saved = await fb_get(POSTED_PATH)
        if saved is None:
            keys = await fb_get("movies", shallow="true") or {}
            POSTED.update(keys)
            posted = POSTED
            try:
                await fb_put(POSTED_PATH, {k: True for k in posted} or {"_init": True})
            except Exception:
                log.warning("Firebase write not allowed - posted list kept in memory only")
        else:
            POSTED.update(saved)
            posted = POSTED
    except Exception:
        log.exception("auto post init failed")
        return
    log.info("Auto post started (%d known movies)", len(posted))

    while True:
        try:
            if AUTOPOST:
                await check_new_movies(bot, posted)
        except Exception:
            log.exception("auto post check failed")
        await asyncio.sleep(POLL_SECONDS)


async def post_init(app: Application):
    asyncio.create_task(auto_post_loop(app))


BOT_VERSION = "2026-10-17 · site-admin  v9 ultra 💔"


async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        f"🤖 Bot version: {BOT_VERSION}\n"
        "✅ Add to Site • Description • Subtitle By • Player • Links"
    )


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("handler error", exc_info=context.error)
    if update is None:  # network / polling errors - don't spam admins
        return
    err = f"{type(context.error).__name__}: {context.error}"[:300]
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(admin, f"⚠️ Bot error:\n{err}")
        except Exception:
            pass


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("autopost", autopost_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("last", last_cmd))
    app.add_handler(CommandHandler("repost", repost_cmd))
    app.add_handler(CommandHandler("requests", requests_cmd))
    app.add_handler(CommandHandler("find", find_cmd))
    app.add_handler(CommandHandler("version", version_cmd))
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("vip", vip_cmd))
    app.add_handler(CommandHandler("skip", on_text))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
