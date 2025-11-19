import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

import pytz
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# 基础配置 & 日志
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "5000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
OWNER_ID = os.getenv("OWNER_ID", "").strip()  # 可选
SESSION_SECRET = os.getenv("SESSION_SECRET", "change-me")

# 结算采用北京时间
CN_TZ = pytz.timezone("Asia/Shanghai")

DATA_DIR = Path("data")
DATA_FILE = DATA_DIR / "records.json"


# =========================================================
# JSON 数据存储
# =========================================================

def get_today_str() -> str:
    """返回北京时间的今天日期字符串 YYYY-MM-DD"""
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def load_db() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        return {"chats": {}}
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("加载 JSON 数据失败，将重新初始化: %s", e)
        return {"chats": {}}


def save_db(db: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = DATA_FILE.with_suffix(".tmp")
    with tmp_file.open("w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    tmp_file.replace(DATA_FILE)


def get_chat_state(chat_id: int) -> Dict[str, Any]:
    """获取某个 chat 的数据结构，不存在则创建。"""
    db = load_db()
    chats = db.setdefault("chats", {})
    cid = str(chat_id)
    if cid not in chats:
        chats[cid] = {
            "last_reset_date": get_today_str(),
            "transactions": [],  # list of tx dicts
        }
        save_db(db)
    return chats[cid]


def update_chat_state(chat_id: int, state: Dict[str, Any]) -> None:
    db = load_db()
    chats = db.setdefault("chats", {})
    chats[str(chat_id)] = state
    save_db(db)


# =========================================================
# 工具函数：解析金额 / 统计 / 文本格式
# =========================================================

def parse_amount_text(text: str) -> Optional[Tuple[float, str]]:
    """
    解析用户输入的金额指令：
    返回 (amount, direction) 其中 direction: "in" / "out"
    支持示例：
      +100
      -50
      +100.5
      +1万 / +1.5万
      +2千 / +3百
    """
    raw = text.strip()
    if not raw:
        return None

    direction = "in"
    if raw[0] == "+":
        direction = "in"
        raw = raw[1:].strip()
    elif raw[0] == "-":
        direction = "out"
        raw = raw[1:].strip()
    else:
        # 没有符号默认是 +
        direction = "in"

    if not raw:
        return None

    multiplier = 1.0
    if raw.endswith("万"):
        multiplier = 10000.0
        raw = raw[:-1]
    elif raw.endswith("千"):
        multiplier = 1000.0
        raw = raw[:-1]
    elif raw.endswith("百"):
        multiplier = 100.0
        raw = raw[:-1]

    try:
        val = float(raw)
    except ValueError:
        return None

    amount = val * multiplier
    if amount <= 0:
        return None

    return amount, direction


def get_today_transactions(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    today = get_today_str()
    txs = state.get("transactions", [])
    return [tx for tx in txs if tx.get("date") == today]


def summarize_today(state: Dict[str, Any]) -> Dict[str, Any]:
    txs = get_today_transactions(state)
    total_in = 0.0
    total_out = 0.0
    count_in = 0
    count_out = 0
    for tx in txs:
        if tx["direction"] == "in":
            total_in += tx["amount"]
            count_in += 1
        else:
            total_out += tx["amount"]
            count_out += 1
    net = total_in - total_out
    return {
        "count_in": count_in,
        "count_out": count_out,
        "total_in": total_in,
        "total_out": total_out,
        "net": net,
    }


def format_summary_text(state: Dict[str, Any]) -> str:
    today = get_today_str()
    s = summarize_today(state)
    lines = [
        f"📅 日期（北京时间）：{today}",
        "",
        f"✅ 今日已入账：{s['count_in']} 笔，合计：{s['total_in']:.2f}",
        f"✅ 今日已出账：{s['count_out']} 笔，合计：{s['total_out']:.2f}",
        "",
        f"📊 今日净入账：{s['net']:.2f}",
    ]
    return "\n".join(lines)


# =========================================================
# Bot 逻辑
# =========================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    get_chat_state(chat_id)  # 确保 chat 初始化

    msg = (
        "👋 你好，我是记账机器人（JSON 版本）。\n\n"
        "你可以直接发送：\n"
        "  ➕ `+100`  /  `+1万`  （入账）\n"
        "  ➖ `-50`   /  `-2千`  （出账）\n\n"
        "常用指令：\n"
        "  • `/summary` 或 “查看账单明细”  查看今天汇总\n"
        "  • `/reset_today` 或 “清空今日”   清空今天所有记录\n"
        "  • `/undo` 或 “撤销”              撤销今天最后一条记录\n\n"
        "所有统计均以【北京时间】为当天边界。"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    state = get_chat_state(chat_id)
    text = format_summary_text(state)
    await update.message.reply_text("📒 今日账单汇总：\n\n" + text)


async def cmd_reset_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """清空今天 00:00 至现在的所有记录。"""
    chat_id = update.effective_chat.id
    state = get_chat_state(chat_id)
    today = get_today_str()
    before = len(state.get("transactions", []))
    state["transactions"] = [
        tx for tx in state.get("transactions", [])
        if tx.get("date") != today
    ]
    after = len(state["transactions"])
    update_chat_state(chat_id, state)

    removed = before - after
    await update.message.reply_text(
        f"🧹 已清空今天（北京时间）00:00 至现在的所有记录，共删除 {removed} 条。\n"
        "现在可以重新开始记账了。"
    )


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """撤销今天最后一条记录，可以多次使用。"""
    chat_id = update.effective_chat.id
    state = get_chat_state(chat_id)
    today = get_today_str()

    txs = state.get("transactions", [])
    # 找到今天最后一条
    idx = None
    for i in range(len(txs) - 1, -1, -1):
        if txs[i].get("date") == today:
            idx = i
            break

    if idx is None:
        await update.message.reply_text("今天已经没有可以撤销的记录了。")
        return

    tx = txs.pop(idx)
    update_chat_state(chat_id, state)

    direction_text = "入账" if tx["direction"] == "in" else "出账"
    await update.message.reply_text(
        f"↩️ 已撤销一条记录：{direction_text} {tx['amount']:.2f}\n"
        "如需继续撤销，请再次发送 /undo 或 “撤销”。"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理普通文本：加减金额 / 关键词指令。"""
    if update.message is None:
        return

    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # 关键词：查看账单 / 清空今日 / 撤销
    lower = text.lower()
    if text in ("查看账单明细", "账单明细", "查看账单") or lower == "summary":
        await cmd_summary(update, context)
        return

    if text in ("清空今日", "清空今天", "重置今日") or lower == "reset_today":
        await cmd_reset_today(update, context)
        return

    if text in ("撤销", "撤销一条") or lower == "undo":
        await cmd_undo(update, context)
        return

    # 尝试解析金额
    parsed = parse_amount_text(text)
    if not parsed:
        # 不是金额指令，就忽略或给个简单提示（不打扰正常聊天）
        return

    amount, direction = parsed
    state = get_chat_state(chat_id)

    now = datetime.now(CN_TZ)
    tx = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "amount": amount,
        "direction": direction,  # "in" / "out"
    }
    state.setdefault("transactions", []).append(tx)
    update_chat_state(chat_id, state)

    s = summarize_today(state)
    direction_text = "入账" if direction == "in" else "出账"
    sign = "+" if direction == "in" else "-"

    reply_lines = [
        f"✅ 已记录一条{direction_text}：{sign}{amount:.2f}",
        "",
        f"📊 今日统计：",
        f"  • 入账 {s['count_in']} 笔，合计 {s['total_in']:.2f}",
        f"  • 出账 {s['count_out']} 笔，合计 {s['total_out']:.2f}",
        f"  • 净入账 {s['net']:.2f}",
    ]
    await update.message.reply_text("\n".join(reply_lines))


# =========================================================
# 主函数：启动 Bot（webhook / polling）
# =========================================================

def main() -> None:
    if not BOT_TOKEN:
        logger.error("环境变量 TELEGRAM_BOT_TOKEN 未设置，程序退出。")
        raise SystemExit(1)

    logger.info("==================================================")
    logger.info("🚀 启动Telegram财务Bot (JSON 文件数据库版本)...")
    logger.info("📋 环境变量检查：")
    logger.info("   PORT=%s", PORT)
    logger.info("   DATABASE_URL=（JSON 模式不需要）")
    logger.info("   TELEGRAM_BOT_TOKEN=已设置")
    logger.info("   OWNER_ID=%s", OWNER_ID or "未设置")
    logger.info("   WEBHOOK_URL=%s", WEBHOOK_URL or "未设置")
    logger.info("   SESSION_SECRET=已设置")
    logger.info("✅ JSON 文件数据库初始化完成，目录：%s", DATA_DIR)
    logger.info("==================================================")

    application = Application.builder().token(BOT_TOKEN).build()

    # 注册 handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("summary", cmd_summary))
    application.add_handler(CommandHandler("reset_today", cmd_reset_today))
    application.add_handler(CommandHandler("undo", cmd_undo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    if WEBHOOK_URL:
        # Webhook 模式（适合在 ClawCloud 等服务器常驻）
        url_path = f"webhook/{BOT_TOKEN}"
        full_webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{url_path}"

        logger.info("🤖 Telegram Bot: Webhook 模式")
        logger.info("   监听地址：0.0.0.0:%s", PORT)
        logger.info("   Webhook URL: %s", full_webhook_url)

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=url_path,
            webhook_url=full_webhook_url,
        )
    else:
        # 本地测试 / 简单部署 可以直接使用 polling
        logger.info("🤖 Telegram Bot: 轮询模式（未设置 WEBHOOK_URL）")
        application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
