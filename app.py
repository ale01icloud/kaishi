import os
import logging
import threading
import asyncio
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from database import FinanceDB  # JSON 文件数据库

# ----------------- 日志配置 -----------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 北京时间偏移（UTC+8）
TZ_OFFSET = 8

# Flask 应用
flask_app = Flask(__name__)

# JSON 数据库实例
db = FinanceDB(data_dir="data")

# Telegram Application（全局）
tg_app: Application | None = None

# 环境变量
PORT = int(os.getenv("PORT", "5000"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or "").rstrip("/")
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")


# ----------------- 工具函数 -----------------


def now_bj() -> datetime:
    """当前北京时间"""
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET)


def today_str() -> str:
    """北京时间当日 YYYY-MM-DD"""
    return now_bj().strftime("%Y-%m-%d")


def format_amount(value: float) -> str:
    return f"{value:.2f}"


def parse_plus_minus_text(text: str):
    """
    解析 +100 / -50 / +1千 / +1万 / +2.5万 等格式
    返回:
        (direction, amount) or None
        direction: "in" / "out"
        amount: float 绝对值
    """
    text = text.strip()
    if not text:
        return None

    # 统一符号
    text = text.replace("＋", "+").replace("－", "-").replace(" ", "")

    if text[0] not in {"+", "-"}:
        return None

    sign = 1 if text[0] == "+" else -1
    body = text[1:]
    if not body:
        return None

    unit = 1.0
    # 支持 “万 / 千 / k”
    if body.endswith("万"):
        unit = 10000.0
        body = body[:-1]
    elif body.endswith("千"):
        unit = 1000.0
        body = body[:-1]
    elif body.lower().endswith("k"):
        unit = 1000.0
        body = body[:-1]

    try:
        num = float(body)
    except ValueError:
        return None

    amount = num * unit * sign
    direction = "in" if amount > 0 else "out"
    return direction, abs(amount)


# ----------------- 业务逻辑：命令 & 文本 -----------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = user.full_name if user else "朋友"

    text = (
        f"你好，{name} 👋\n\n"
        "我是你的账单小助手，目前运行在【JSON 文件数据库模式】。\n"
        "你可以直接发送：\n"
        "  •  +100   表示入账 100\n"
        "  •  -50    表示出账 50\n"
        "  •  +1千   等于 +1000\n"
        "  •  +1万   等于 +10000\n\n"
        "发送 “查看账单明细” 可以查看今天的汇总账单。\n"
        "发送 “清除数据” 或 /clear 可以清空当天 00:00 以来的所有记录。"
    )
    if update.message:
        await update.message.reply_text(text)


async def send_today_summary(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
):
    """发送当天账单汇总"""
    date_str = today_str()
    day_data = db.get_day_transactions(user_id, date_str)

    if not day_data:
        if update.message:
            await update.message.reply_text("今天还没有任何记录哦～")
        elif update.callback_query:
            await update.callback_query.edit_message_text("今天还没有任何记录哦～")
        return

    in_count = 0
    out_count = 0
    in_lines = []
    out_lines = []
    total_in = 0.0
    total_out = 0.0

    for t in day_data:
        line = f"{t['time']} {format_amount(t['amount'])}"
        if t["type"] == "in":
            in_count += 1
            total_in += t["amount"]
            in_lines.append(line)
        else:
            out_count += 1
            total_out += t["amount"]
            out_lines.append(line)

    net = total_in - total_out

    header = "📊【全球支付 账单汇总】\n"
    lines = [header]

    lines.append(f"已入账 ({in_count}笔)")
    lines.extend(in_lines or ["（无）"])

    lines.append("")
    lines.append(f"已出账 ({out_count}笔)")
    lines.extend(out_lines or ["（无）"])

    lines.append("\n📌 今日小结：")
    lines.append(f"  入账合计：{format_amount(total_in)} USDT")
    lines.append(f"  出账合计：{format_amount(total_out)} USDT")
    lines.append(f"  净入：{format_amount(net)} USDT")
    lines.append("\n⚙ 当前模式：JSON 文件数据库（每个用户一个文件）")

    text = "\n".join(lines)

    keyboard = [
        [InlineKeyboardButton("📖 查看账单明细", callback_data="show_today_summary")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /summary 命令：查看今天汇总 """
    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id if user else (chat.id if chat else 0)
    await send_today_summary(update, context, user_id)


async def cmd_clear_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """清空当天记录：从 00:00 到现在的所有记录"""
    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id if user else (chat.id if chat else 0)

    date_str = today_str()
    deleted = db.clear_day_transactions(user_id, date_str)

    if deleted == 0:
        msg = f"今天（{date_str}）本来就没有记录，无需清除～"
    else:
        msg = f"✅ 已清除今天（{date_str}）从 00:00 至现在的 {deleted} 条记录。"

    if update.message:
        await update.message.reply_text(msg)
    elif update.callback_query:
        await update.callback_query.edit_message_text(msg)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()

    user = query.from_user
    chat = query.message.chat if query.message else None
    user_id = user.id if user else (chat.id if chat else 0)

    if query.data == "show_today_summary":
        await send_today_summary(update, context, user_id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理普通文本消息：+100 / -50 / +1万 等"""
    if update.message is None:
        return

    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id if user else (chat.id if chat else 0)

    text = update.message.text.strip()

    # 清除当天数据
    if text in {"清除数据", "清空数据", "重置数据"}:
        await cmd_clear_today(update, context)
        return

    # 关键字：查看账单
    keywords = {"查看账单明细", "查看账单", "更多记录", "账单", "账单明细"}
    if text in keywords:
        await send_today_summary(update, context, user_id)
        return

    parsed = parse_plus_minus_text(text)
    if parsed is None:
        # 其它无关文本就忽略
        return

    direction, amount = parsed  # "in"/"out", 金额绝对值

    local_now = now_bj()
    date_str = local_now.strftime("%Y-%m-%d")
    time_str = local_now.strftime("%H:%M")

    db.add_transaction(
        user_id=user_id,
        date_str=date_str,
        time_str=time_str,
        amount=amount,
        t_type=direction,
        raw_text=text,
    )

    summary = db.get_day_summary(user_id, date_str)
    total_in = summary["total_in"]
    total_out = summary["total_out"]
    net = summary["net"]

    direction_cn = "入账" if direction == "in" else "出账"
    sign = "+" if direction == "in" else "-"

    reply = (
        f"✅ 已记录 {direction_cn} {sign}{format_amount(amount)} USDT\n"
        f"今天入账：{format_amount(total_in)}，出账：{format_amount(total_out)}，净入：{format_amount(net)}"
    )
    await update.message.reply_text(reply)


# ----------------- Flask 路由 -----------------


@flask_app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "status": "ok",
            "mode": "json-db",
            "time_bj": now_bj().isoformat(),
        }
    )


@flask_app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@flask_app.route("/webhook/<token>", methods=["POST"])
def telegram_webhook(token: str):
    """Telegram 调用的 Webhook 入口"""
    global tg_app

    if token != TELEGRAM_BOT_TOKEN:
        return "Invalid token", 403

    if tg_app is None:
        return "Bot not ready", 503

    try:
        data = request.get_json(force=True)
    except Exception as e:
        logger.exception("解析 Telegram 更新失败: %s", e)
        return "Bad Request", 400

    update = Update.de_json(data, tg_app.bot)
    tg_app.update_queue.put_nowait(update)
    return "OK", 200


# ----------------- 启动 Telegram Bot -----------------


def start_telegram_bot():
    """在单独线程中跑 Telegram Bot（Webhook 模式）"""

    async def _runner():
        global tg_app

        logger.info("==================================================")
        logger.info("🚀 启动 Telegram Bot 应用 (JSON 文件数据库模式)")
        logger.info("==================================================")

        tg_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

        # 命令
        tg_app.add_handler(CommandHandler("start", cmd_start))
        tg_app.add_handler(CommandHandler("summary", cmd_summary))
        tg_app.add_handler(CommandHandler("clear", cmd_clear_today))
        tg_app.add_handler(CommandHandler("reset", cmd_clear_today))

        # 文本消息
        tg_app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
        )

        # 回调按钮
        tg_app.add_handler(CallbackQueryHandler(handle_callback))

        # WebApp Data（可选）
        tg_app.add_handler(
            MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_text)
        )

        await tg_app.initialize()
        await tg_app.start()

        # 设置 Webhook
        if WEBHOOK_URL:
            webhook_full = f"{WEBHOOK_URL}/webhook/{TELEGRAM_BOT_TOKEN}"
            await tg_app.bot.set_webhook(webhook_full)
            logger.info("🔗 设置 Webhook: %s", webhook_full)
        else:
            logger.warning("⚠️ WEBHOOK_URL 未设置，Telegram 收不到消息，请在环境变量里设置。")

        logger.info("✅ Telegram Bot 初始化完成")

        # 挂起等待
        await asyncio.Event().wait()

    asyncio.run(_runner())


# ----------------- 整体初始化 -----------------


def init_app():
    logger.info("==================================================")
    logger.info("🚀 启动 Telegram 财务 Bot + Web Dashboard (JSON DB)")
    logger.info("==================================================")

    logger.info("📋 环境变量检查：")
    logger.info("   PORT=%s", PORT)
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        logger.info("   DATABASE_URL=已设置（JSON 模式下不会使用）")
    else:
        logger.info("   DATABASE_URL=未设置（JSON 模式下也不需要）")

    logger.info("   TELEGRAM_BOT_TOKEN=%s", "已设置" if TELEGRAM_BOT_TOKEN else "未设置")
    logger.info("   OWNER_ID=%s", OWNER_ID)
    logger.info("   WEBHOOK_URL=%s", WEBHOOK_URL or "未设置")
    logger.info("   SESSION_SECRET=%s", "已设置" if SESSION_SECRET else "未设置")

    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN 未设置，无法启动 Bot")
        raise SystemExit(1)

    # 初始化 JSON 数据目录
    db.init_database()
    logger.info("✅ JSON 文件数据库初始化完成，目录：%s", db.data_dir)

    # 启动 Telegram 线程
    t = threading.Thread(target=start_telegram_bot, daemon=True)
    t.start()
    logger.info("🔄 已启动 Bot 事件循环线程...")


# ----------------- 主入口 -----------------

if __name__ == "__main__":
    init_app()
    logger.info("🌐 启动 Flask 应用（Bot + Web Dashboard）在端口: %s", PORT)
    flask_app.run(host="0.0.0.0", port=PORT)
