import os
import json
import threading
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Dict, Any, List

from flask import Flask, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ----------------- 基本配置 -----------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# 北京时间（UTC+8）
CST = timezone(timedelta(hours=8))

JSON_DB_FILE = os.path.join(DATA_DIR, "records.json")

_data_lock = threading.Lock()


def _load_db() -> Dict[str, Any]:
    """加载 JSON 数据库"""
    if not os.path.exists(JSON_DB_FILE):
        return {"chats": {}}
    try:
        with open(JSON_DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("读取 JSON 数据库失败: %s", e)
        return {"chats": {}}


def _save_db(db: Dict[str, Any]) -> None:
    """保存 JSON 数据库（原子写入）"""
    tmp_path = JSON_DB_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, JSON_DB_FILE)


def _get_today_str() -> str:
    """返回今天（北京时间）的日期字符串"""
    return datetime.now(CST).strftime("%Y-%m-%d")


def _normalize_deposit(amount: Decimal) -> Decimal:
    """
    入账金额：截断到小数点后两位，不四舍五入。
    例如：1.239 -> 1.23，1.2 -> 1.20
    """
    return amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _normalize_withdraw(amount: Decimal) -> Decimal:
    """
    出账金额：四舍五入到两位小数。
    例如：1.235 -> 1.24，1.234 -> 1.23
    """
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_amount(text: str) -> Decimal:
    """从文本中提取数字金额（支持 +100、-50.25 这种格式）"""
    import re

    m = re.search(r"[-+]?\d+(?:\.\d+)?", text.replace(",", ""))
    if not m:
        raise ValueError("未找到数字金额")
    return Decimal(m.group(0))


def _get_chat(db: Dict[str, Any], chat_id: int) -> Dict[str, Any]:
    """确保 chat 结构存在"""
    sid = str(chat_id)
    if "chats" not in db:
        db["chats"] = {}
    if sid not in db["chats"]:
        db["chats"][sid] = {"records": []}
    return db["chats"][sid]


def _today_records(chat: Dict[str, Any]) -> List[Dict[str, Any]]:
    """获取今天的所有记录"""
    today = _get_today_str()
    return [r for r in chat.get("records", []) if r.get("date") == today]


def _add_record(chat_id: int, rtype: str, amount: Decimal, raw: str) -> Dict[str, Any]:
    """
    新增一条记录
    rtype: "deposit" 或 "withdraw"
    """
    with _data_lock:
        db = _load_db()
        chat = _get_chat(db, chat_id)
        now = datetime.now(CST)
        rec = {
            "id": len(chat["records"]) + 1,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "type": rtype,  # deposit / withdraw
            "amount": float(amount),
            "raw": raw,
        }
        chat["records"].append(rec)
        _save_db(db)
    return rec


def _undo_last(chat_id: int) -> Dict[str, Any] | None:
    """撤销今天最后一条记录"""
    with _data_lock:
        db = _load_db()
        chat = _get_chat(db, chat_id)
        today = _get_today_str()
        for i in range(len(chat["records"]) - 1, -1, -1):
            if chat["records"][i].get("date") == today:
                rec = chat["records"].pop(i)
                _save_db(db)
                return rec
    return None


def _clear_today(chat_id: int) -> int:
    """清空今天所有记录"""
    with _data_lock:
        db = _load_db()
        chat = _get_chat(db, chat_id)
        today = _get_today_str()
        before = len(chat["records"])
        chat["records"] = [r for r in chat["records"] if r.get("date") != today]
        removed = before - len(chat["records"])
        _save_db(db)
    return removed


def _build_summary(chat_id: int) -> str:
    """构建今日统计文本"""
    with _data_lock:
        db = _load_db()
        chat = _get_chat(db, chat_id)
        records = _today_records(chat)

    dep_count = 0
    dep_sum = Decimal("0")
    wd_count = 0
    wd_sum = Decimal("0")

    for r in records:
        amt = Decimal(str(r.get("amount", 0)))
        if r.get("type") == "deposit":
            dep_count += 1
            dep_sum += amt
        elif r.get("type") == "withdraw":
            wd_count += 1
            wd_sum += amt

    net = dep_sum - wd_sum
    lines = [
        "📊 今日统计：",
        f"  ▫ 入账 {dep_count} 笔，合计 {dep_sum:.2f}",
        f"  ▫ 出账 {wd_count} 笔，合计 {wd_sum:.2f}",
        f"  ▫ 流入净额：{net:.2f}",
    ]
    return "\n".join(lines)


def _build_details(chat_id: int) -> str:
    """构建今日明细文本（【已入账】【已出账】分栏）"""
    with _data_lock:
        db = _load_db()
        chat = _get_chat(db, chat_id)
        records = _today_records(chat)

    dep_lines: List[str] = []
    wd_lines: List[str] = []

    for r in records:
        line = f'{r.get("time")}  {r.get("amount"):,.2f}'
        if r.get("type") == "deposit":
            dep_lines.append(line)
        elif r.get("type") == "withdraw":
            wd_lines.append(line)

    text_lines: List[str] = ["🇮🇹【全球支付 账单汇总】", ""]

    # 已入账
    text_lines.append(f"已入账（{len(dep_lines)}笔）")
    if dep_lines:
        text_lines.extend(dep_lines)
    else:
        text_lines.append("（无）")
    text_lines.append("")

    # 已出账
    text_lines.append(f"已出账（{len(wd_lines)}笔）")
    if wd_lines:
        text_lines.extend(wd_lines)
    else:
        text_lines.append("（无）")
    text_lines.append("")

    # 汇总
    dep_sum = sum(
        Decimal(str(r.get("amount"))) for r in records if r.get("type") == "deposit"
    )
    wd_sum = sum(
        Decimal(str(r.get("amount"))) for r in records if r.get("type") == "withdraw"
    )
    net = dep_sum - wd_sum

    text_lines.extend(
        [
            "━━━━━━━━━━━━━━━━",
            "📌 当前概要：",
            f"  入账合计：{dep_sum:.2f}",
            f"  出账合计：{wd_sum:.2f}",
            f"  流入净额：{net:.2f}",
        ]
    )

    return "\n".join(text_lines)


# ----------------- Telegram Bot 逻辑 -----------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    logger.info("收到 /start, chat_id=%s, user=%s", chat_id, user.id if user else None)

    text = [
        "👋 欢迎使用  全球支付记账机器人",
        "",
        "发送格式：",
        "  ➕ 入账：例如  “+100”  “+100.5”",
        "  ➖ 出账：例如  “-50”   “-12.34”",
        "",
        "其它指令：",
        "  撤销 / /undo  —— 撤销今天最后一条记录",
        "  清空 / 清空今天 —— 删除今天所有记录",
        "",
        _build_summary(chat_id),
    ]
    if update.message:
        await update.message.reply_text("\n".join(text))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.text is None:
        return

    chat_id = message.chat.id
    text = message.text.strip()

    # ---------- 控制指令 ----------
    if text in ("/undo", "撤销"):
        rec = _undo_last(chat_id)
        if rec:
            reply = (
                f"✅ 已撤销一条记录：{rec.get('type')} {rec.get('amount'):.2f}\n"
                + _build_summary(chat_id)
            )
        else:
            reply = "今天没有可以撤销的记录。"
        await message.reply_text(reply)
        return

    if text in ("清空", "清空今天", "/clear"):
        count = _clear_today(chat_id)
        reply = f"✅ 已清空今天的 {count} 条记录。\n" + _build_summary(chat_id)
        await message.reply_text(reply)
        return

    # ---------- 记账指令 ----------
    first_char = text[0]
    if first_char not in ("+", "-"):
        # 不是记账文本，直接忽略（不打扰普通聊天）
        return

    try:
        amount = _parse_amount(text)
    except Exception:
        await message.reply_text("❌ 无法识别金额，请使用类似 “+100” 或 “-50.25” 的格式。")
        return

    if first_char == "+":
        # 入账：截断到两位小数
        norm_amount = _normalize_deposit(amount)
        rtype = "deposit"
    else:
        # 出账：取绝对值 + 四舍五入
        norm_amount = _normalize_withdraw(abs(amount))
        rtype = "withdraw"

    rec = _add_record(chat_id, rtype, norm_amount, text)

    if rtype == "deposit":
        head = f"✅ 已记录一条入账：+{norm_amount:.2f}"
    else:
        head = f"✅ 已记录一条出账：-{norm_amount:.2f}"

    summary = _build_summary(chat_id)

    keyboard = [
        [InlineKeyboardButton("🇮🇹 查看账单明细", callback_data="SHOW_DETAILS")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await message.reply_text(f"{head}\n\n{summary}", reply_markup=reply_markup)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    chat_id = query.message.chat.id
    data = query.data

    if data == "SHOW_DETAILS":
        text = _build_details(chat_id)
        await query.answer()
        await query.edit_message_text(text)


# ----------------- Flask Web 部分 -----------------

flask_app = Flask(__name__)


@flask_app.route("/")
def index():
    return jsonify(
        {
            "status": "ok",
            "message": "Telegram 财务 Bot (JSON) 正在运行",
            "time": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    logger.info("🌐 启动 Flask Web 服务, 端口: %s", port)
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


# ----------------- 程序入口 -----------------


def main():
    logger.info("==================================================")
    logger.info("🚀 启动Telegram财务Bot (JSON 文件数据库版本)...")
    logger.info("==================================================")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("未设置 TELEGRAM_BOT_TOKEN 环境变量，程序退出。")
        return

    owner_id = os.environ.get("OWNER_ID")
    logger.info("OWNER_ID=%s", owner_id)

    # 先启动 Flask（后台线程）
    threading.Thread(target=run_flask, daemon=True).start()

    # 再启动 Telegram Bot：polling 模式，不再使用 webhook 和多重 event loop
    logger.info("🤖 初始化 Telegram Bot Application (polling 模式)...")
    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    logger.info("✅ 开始轮询 Telegram 更新 ...")
    application.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
