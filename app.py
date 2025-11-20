#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一 Flask 应用 - Telegram 财务 Bot Webhook + Web Dashboard
使用 JSON 文件存储账单 & 管理员信息（不再需要 PostgreSQL）
【当前版本：JSON + 轮询版，不需要公网 HTTPS / Webhook】
"""

import os
import re
import json
import hmac
import hashlib
import math
import logging
import threading
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from functools import wraps

from flask import Flask, render_template, request, jsonify, session
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========== 环境变量 & 基础配置 ==========

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
SESSION_SECRET = os.getenv("SESSION_SECRET")
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")
# 轮询版不再使用 WEBHOOK_URL
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not BOT_TOKEN:
    raise RuntimeError("❌ 未配置 TELEGRAM_BOT_TOKEN")

if not SESSION_SECRET:
    print("⚠️ SESSION_SECRET 未设置，Web 查账功能将不可用")
    SESSION_SECRET = None

# Flask 应用
app = Flask(__name__)
app.secret_key = SESSION_SECRET or os.urandom(24)

# 日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 数据目录
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
DB_FILE = DATA_DIR / "db.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# JSON 数据锁
_db_lock = threading.Lock()

# 全局 Telegram Application（轮询版不再需要全局事件循环）
telegram_app: Application | None = None

# ========== JSON “数据库” 工具函数 ==========


def _load_db() -> dict:
    """从 JSON 文件读取数据库"""
    if not DB_FILE.exists():
        return {
            "admins": {},
            "groups": {},
            "private_users": {},
            "next_txn_id": 1,
        }
    try:
        with DB_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("读取 JSON 数据库失败: %s", e)
        return {
            "admins": {},
            "groups": {},
            "private_users": {},
            "next_txn_id": 1,
        }


def _save_db(db: dict) -> None:
    """写回 JSON 数据库"""
    tmp = DB_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    tmp.replace(DB_FILE)


def init_database():
    """初始化 JSON 数据库文件"""
    with _db_lock:
        db = _load_db()
        # 保证必要字段存在
        db.setdefault("admins", {})
        db.setdefault("groups", {})
        db.setdefault("private_users", {})
        db.setdefault("next_txn_id", 1)
        _save_db(db)
    logger.info("✅ JSON 文件数据库初始化完成：%s", DB_FILE)


# ========== JSON 版数据操作接口（代替原来的 database.py） ==========

def _ensure_group(db: dict, chat_id: int) -> dict:
    gid = str(chat_id)
    if gid not in db["groups"]:
        db["groups"][gid] = {
            "group_name": f"群 {chat_id}",
            "in_rate": 0.0,
            "in_fx": 0.0,
            "out_rate": 0.0,
            "out_fx": 0.0,
            "transactions": [],  # 全部交易（多天）
        }
    return db["groups"][gid]


def is_admin(user_id: int) -> bool:
    with _db_lock:
        db = _load_db()
        return str(user_id) in db.get("admins", {})


def add_admin(user_id: int, username: str | None, first_name: str | None, is_owner=False):
    with _db_lock:
        db = _load_db()
        db.setdefault("admins", {})
        db["admins"][str(user_id)] = {
            "user_id": user_id,
            "username": username,
            "first_name": first_name or "",
            "is_owner": bool(is_owner),
        }
        _save_db(db)


def remove_admin(user_id: int):
    with _db_lock:
        db = _load_db()
        if str(user_id) in db.get("admins", {}):
            del db["admins"][str(user_id)]
            _save_db(db)


def get_all_admins():
    with _db_lock:
        db = _load_db()
        return list(db.get("admins", {}).values())


def add_private_chat_user(user_id: int, username: str | None, first_name: str | None):
    with _db_lock:
        db = _load_db()
        db.setdefault("private_users", {})
        db["private_users"][str(user_id)] = {
            "user_id": user_id,
            "username": username,
            "first_name": first_name or "",
            "created_at": datetime.now().isoformat(),
        }
        _save_db(db)


def get_group_config(chat_id: int) -> dict:
    with _db_lock:
        db = _load_db()
        g = _ensure_group(db, chat_id)
        _save_db(db)
        return g


def update_group_config(
    chat_id: int,
    in_rate: float | None = None,
    in_fx: float | None = None,
    out_rate: float | None = None,
    out_fx: float | None = None,
):
    with _db_lock:
        db = _load_db()
        g = _ensure_group(db, chat_id)
        if in_rate is not None:
            g["in_rate"] = float(in_rate)
        if in_fx is not None:
            g["in_fx"] = float(in_fx)
        if out_rate is not None:
            g["out_rate"] = float(out_rate)
        if out_fx is not None:
            g["out_fx"] = float(out_fx)
        _save_db(db)


def _today_str() -> str:
    from pytz import timezone

    tz = timezone("Asia/Shanghai")
    return datetime.now(tz).strftime("%Y-%m-%d")


def add_transaction(
    chat_id: int,
    transaction_type: str,
    amount: Decimal,
    rate: Decimal,
    fx: Decimal,
    usdt: Decimal,
    timestamp: str,
    country: str,
    operator_id: int,
    operator_name: str,
) -> int:
    """新增一条交易，返回 txn_id"""
    with _db_lock:
        db = _load_db()
        g = _ensure_group(db, chat_id)
        txn_id = int(db.get("next_txn_id", 1))
        db["next_txn_id"] = txn_id + 1

        rec = {
            "id": txn_id,
            "chat_id": chat_id,
            "date": _today_str(),
            "timestamp": timestamp,  # HH:MM
            "transaction_type": transaction_type,  # in/out/send
            "amount": float(amount),
            "rate": float(rate),
            "fx": float(fx),
            "usdt": float(usdt),
            "country": country,
            "operator_id": operator_id,
            "operator_name": operator_name,
            "message_id": None,
            "created_at": datetime.now().isoformat(),
        }
        g.setdefault("transactions", []).append(rec)
        _save_db(db)
        return txn_id


def update_transaction_message_id(txn_id: int, message_id: int):
    with _db_lock:
        db = _load_db()
        for g in db.get("groups", {}).values():
            for t in g.get("transactions", []):
                if t.get("id") == txn_id:
                    t["message_id"] = message_id
                    _save_db(db)
                    return


def get_today_transactions(chat_id: int):
    today = _today_str()
    with _db_lock:
        db = _load_db()
        g = _ensure_group(db, chat_id)
        return [t for t in g.get("transactions", []) if t.get("date") == today]


def delete_transaction_by_message_id(message_id: int):
    """按消息 ID 撤销今天的交易"""
    today = _today_str()
    with _db_lock:
        db = _load_db()
        for g in db.get("groups", {}).values():
            txns = g.get("transactions", [])
            for i in range(len(txns) - 1, -1, -1):
                t = txns[i]
                if t.get("date") == today and t.get("message_id") == message_id:
                    deleted = txns.pop(i)
                    _save_db(db)
                    return deleted
    return None


def clear_today_transactions(chat_id: int):
    """清除今日所有交易，并返回统计"""
    today = _today_str()
    stats = {
        "in": {"count": 0, "usdt": 0.0},
        "out": {"count": 0, "usdt": 0.0},
        "send": {"count": 0, "usdt": 0.0},
    }
    with _db_lock:
        db = _load_db()
        g = _ensure_group(db, chat_id)
        new_txns = []
        for t in g.get("transactions", []):
            if t.get("date") != today:
                new_txns.append(t)
                continue
            tp = t.get("transaction_type")
            if tp in stats:
                stats[tp]["count"] += 1
                stats[tp]["usdt"] += float(t.get("usdt", 0.0))
        g["transactions"] = new_txns
        _save_db(db)
    return stats


def get_transactions_summary(chat_id: int) -> dict:
    """计算今日入金/出金/下发、应下发等汇总"""
    today = _today_str()
    with _db_lock:
        db = _load_db()
        g = _ensure_group(db, chat_id)
        txns = [t for t in g.get("transactions", []) if t.get("date") == today]

    in_records = [t for t in txns if t["transaction_type"] == "in"]
    out_records = [t for t in txns if t["transaction_type"] == "out"]
    send_records = [t for t in txns if t["transaction_type"] == "send"]

    sum_in = sum(float(t["usdt"]) for t in in_records)
    sum_out = sum(float(t["usdt"]) for t in out_records)
    sum_send = sum(float(t["usdt"]) for t in send_records)

    should_send = sum_in - sum_out  # 应下发
    send_usdt = sum_send            # 已下发

    return {
        "in_records": in_records,
        "out_records": out_records,
        "send_records": send_records,
        "should_send": should_send,
        "send_usdt": send_usdt,
    }


# ========== 工具函数 ==========

def trunc2(x) -> float:
    """截断到小数点后两位（用于入金/应下发计算）"""
    x = float(x)
    rounded = round(x, 6)          # 先规整，避免浮点毛刺
    return math.floor(rounded * 100.0) / 100.0


def round2(x) -> float:
    """四舍五入到两位小数（用于出金/下发显示）"""
    x = float(x)
    return round(x, 2)


def fmt_usdt(x: float) -> str:
    return f"{x:.2f} USDT"


def to_superscript(num: int) -> str:
    """将数字转换为上标形式"""
    m = {
        "0": "⁰",
        "1": "¹",
        "2": "²",
        "3": "³",
        "4": "⁴",
        "5": "⁵",
        "6": "⁶",
        "7": "⁷",
        "8": "⁸",
        "9": "⁹",
        "-": "⁻",
    }
    return "".join(m.get(c, c) for c in str(num))


def now_ts() -> str:
    """北京时间 HH:MM"""
    import pytz
    tz = pytz.timezone("Asia/Shanghai")
    return datetime.now(tz).strftime("%H:%M")


def today_str() -> str:
    """北京时间 YYYY-MM-DD"""
    import pytz
    tz = pytz.timezone("Asia/Shanghai")
    return datetime.now(tz).strftime("%Y-%m-%d")


def log_path(chat_id: int, country: str | None = None, date_str: str | None = None) -> Path:
    """账单本地日志文件路径"""
    if date_str is None:
        date_str = today_str()

    folder = f"group_{chat_id}"
    if country:
        folder = f"{folder}/{country}"
    else:
        folder = f"{folder}/通用"

    p = LOG_DIR / folder
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{date_str}.log"


def append_log(path: Path, text: str):
    with path.open("a", encoding="utf-8") as f:
        f.write(text.strip() + "\n")


def parse_amount_and_country(text: str):
    """
    解析金额 & 国家：
    +10000 / 日本  -> (10000.0, '日本')
    -200  /US      -> (200.0, 'US')
    +3000          -> (3000.0, '通用')
    """
    m = re.match(r"^[\+\-]\s*([0-9]+(?:\.[0-9]+)?)", text.strip())
    if not m:
        return None, None
    amount = float(m.group(1))
    m2 = re.search(r"/\s*([^\s]+)$", text)
    country = m2.group(1) if m2 else "通用"
    return amount, country


def is_bot_admin(user_id: int) -> bool:
    """机器人管理员：OWNER + JSON 中的管理员"""
    if OWNER_ID and OWNER_ID.isdigit() and int(OWNER_ID) == user_id:
        return True
    return is_admin(user_id)

# ========== Web Token 相关 ==========

def generate_web_token(chat_id: int, user_id: int, expires_hours: int = 24) -> str | None:
    if not SESSION_SECRET:
        return None
    expires_at = int((datetime.now() + timedelta(hours=expires_hours)).timestamp())
    data = f"{chat_id}:{user_id}:{expires_at}"
    sig = hmac.new(SESSION_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}:{sig}"


def verify_token(token: str):
    if not SESSION_SECRET:
        return None
    try:
        chat_id_s, user_id_s, exp_s, sig = token.split(":")
        chat_id = int(chat_id_s)
        user_id = int(user_id_s)
        exp = int(exp_s)

        data = f"{chat_id}:{user_id}:{exp}"
        expected = hmac.new(SESSION_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
        if sig != expected:
            return None
        if datetime.now().timestamp() > exp:
            return None
        return {"chat_id": chat_id, "user_id": user_id}
    except Exception:
        return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.args.get("token") or session.get("token")
        if not token:
            return "未授权访问", 403
        user_info = verify_token(token)
        if not user_info:
            return "Token 无效或已过期", 403
        session["token"] = token
        session["user_info"] = user_info
        return fn(*args, **kwargs)

    return wrapper


def generate_web_url(chat_id: int, user_id: int) -> str | None:
    token = generate_web_token(chat_id, user_id)
    if not token:
        return None
    return f"{WEB_BASE_URL}/dashboard?token={token}"

# ========== 渲染账单文本 ==========

def render_group_summary(chat_id: int) -> str:
    config = get_group_config(chat_id)
    summary = get_transactions_summary(chat_id)

    bot_name = config.get("group_name", "AA全球国际支付")

    in_recs = summary["in_records"]
    out_recs = summary["out_records"]
    send_recs = summary["send_records"]

    should = trunc2(summary["should_send"])
    sent = trunc2(summary["send_usdt"])
    diff = trunc2(should - sent)

    rin = float(config.get("in_rate", 0) or 0)
    fin = float(config.get("in_fx", 0) or 0)
    rout = float(config.get("out_rate", 0) or 0)
    fout = float(config.get("out_fx", 0) or 0)

    lines: list[str] = []
    lines.append(f"📊【{bot_name} 账单汇总】\n")

    # 入金
    lines.append(f"已入账 ({len(in_recs)}笔)")
    for r in in_recs[:5]:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = trunc2(float(r["usdt"]))
        ts = r["timestamp"]
        rp = int(rate * 100)
        rs = to_superscript(rp)
        lines.append(f"{ts} {raw}  {rs}/ {fx} = {usdt}")
    lines.append("")

    # 出金
    lines.append(f"已出账 ({len(out_recs)}笔)")
    for r in out_recs[:5]:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = round2(float(r["usdt"]))
        ts = r["timestamp"]
        rp = int(rate * 100)
        rs = to_superscript(rp)
        lines.append(f"{ts} {raw}  {rs}/ {fx} = {usdt}")
    lines.append("")

    # 下发
    if send_recs:
        lines.append(f"已下发 ({len(send_recs)}笔)")
        for r in send_recs[:5]:
            usdt = round2(abs(float(r["usdt"])))
            ts = r["timestamp"]
            lines.append(f"{ts} {usdt}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"⚙️ 当前费率：入 {rin*100:.0f}% ⇄ 出 {rout*100:.0f}%")
    lines.append(f"💱 固定汇率：入 {fin} ⇄ 出 {fout}")
    lines.append(f"📊 应下发：{fmt_usdt(should)}")
    lines.append(f"📤 已下发：{fmt_usdt(sent)}")
    lines.append(f"{'❗' if diff != 0 else '✅'} 未下发：{fmt_usdt(diff)}")
    lines.append("━━━━━━━━━━━━━━")
    lines.append("📚 **查看更多记录**：发送「更多记录」")

    return "\n".join(lines)


def render_full_summary(chat_id: int) -> str:
    config = get_group_config(chat_id)
    summary = get_transactions_summary(chat_id)

    bot_name = config.get("group_name", "AA全球国际支付")

    in_recs = summary["in_records"]
    out_recs = summary["out_records"]
    send_recs = summary["send_records"]

    should = trunc2(summary["should_send"])
    sent = trunc2(summary["send_usdt"])
    diff = trunc2(should - sent)

    rin = float(config.get("in_rate", 0) or 0)
    fin = float(config.get("in_fx", 0) or 0)
    rout = float(config.get("out_rate", 0) or 0)
    fout = float(config.get("out_fx", 0) or 0)

    lines: list[str] = []
    lines.append(f"📊【{bot_name} 完整账单】\n")

    lines.append(f"已入账 ({len(in_recs)}笔)")
    for r in in_recs:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = trunc2(float(r["usdt"]))
        ts = r["timestamp"]
        rp = int(rate * 100)
        rs = to_superscript(rp)
        lines.append(f"{ts} {raw}  {rs}/ {fx} = {usdt}")
    lines.append("")

    lines.append(f"已出账 ({len(out_recs)}笔)")
    for r in out_recs:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = round2(float(r["usdt"]))
        ts = r["timestamp"]
        rp = int(rate * 100)
        rs = to_superscript(rp)
        lines.append(f"{ts} {raw}  {rs}/ {fx} = {usdt}")
    lines.append("")

    if send_recs:
        lines.append(f"已下发 ({len(send_recs)}笔)")
        for r in send_recs:
            usdt = round2(abs(float(r["usdt"])))
            ts = r["timestamp"]
            lines.append(f"{ts} {usdt}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"⚙️ 当前费率：入 {rin*100:.0f}% ⇄ 出 {rout*100:.0f}%")
    lines.append(f"💱 固定汇率：入 {fin} ⇄ 出 {fout}")
    lines.append(f"📊 应下发：{fmt_usdt(should)}")
    lines.append(f"📤 已下发：{fmt_usdt(sent)}")
    lines.append(f"{'❗' if diff != 0 else '✅'} 未下发：{fmt_usdt(diff)}")
    lines.append("━━━━━━━━━━━━━━")

    return "\n".join(lines)


async def send_summary_with_button(update: Update, chat_id: int, user_id: int):
    text = render_group_summary(chat_id)

    if SESSION_SECRET:
        web_url = generate_web_url(chat_id, user_id)
        if web_url:
            keyboard = [[InlineKeyboardButton("📊 查看账单明细", url=web_url)]]
            markup = InlineKeyboardMarkup(keyboard)
            msg = await update.message.reply_text(text, reply_markup=markup)
        else:
            msg = await update.message.reply_text(text)
    else:
        msg = await update.message.reply_text(text)

    return msg

# ========== Telegram 处理 ==========

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    # 记录私聊用户
    if chat.type == "private":
        add_private_chat_user(user.id, user.username, user.first_name)

    help_text = (
        "🤖 你好，我是财务记账机器人。\n\n"
        "📊 记账操作：\n"
        "  入金：+10000 或 +10000 / 日本\n"
        "  出金：-10000 或 -10000 / 日本\n"
        "  查看账单：+0 或 更多记录\n\n"
        "💰 USDT下发（仅管理员）：\n"
        "  下发35.04（记录下发并扣除应下发）\n"
        "  下发-35.04（撤销下发并增加应下发）\n\n"
        "🔄 撤销操作（仅管理员）：\n"
        "  回复账单消息 + 输入：撤销\n"
        "  （必须准确输入“撤销”二字）\n\n"
        "⚙️ 快速设置（仅管理员）：\n"
        "  重置默认值（一键设置推荐费率/汇率）\n"
        "  清除数据（清除今日00:00至现在的所有数据）\n"
        "  设置入金费率 10\n"
        "  设置入金汇率 153\n"
        "  设置出金费率 2\n"
        "  设置出金汇率 137\n\n"
        "👥 管理员管理：\n"
        "  设置机器人管理员（回复消息）\n"
        "  删除机器人管理员（回复消息）\n"
        "  显示机器人管理员"
    )
    await update.message.reply_text(help_text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """所有文本消息（群 + 私聊）统一入口"""
    user = update.effective_user
    chat = update.effective_chat
    chat_id = chat.id
    text = (update.message.text or update.message.caption or "").strip()
    ts = now_ts()
    dstr = today_str()

    logger.info(f"[MSG] chat={chat_id} type={chat.type} from={user.id} text={text}")

    # ---------- 私聊 ----------
    if chat.type == "private":
        add_private_chat_user(user.id, user.username, user.first_name)

        # 转发给 OWNER
        if OWNER_ID and OWNER_ID.isdigit():
            owner_id = int(OWNER_ID)
            if user.id != owner_id:
                info = f"👤 {user.full_name}"
                if user.username:
                    info += f" (@{user.username})"
                info += f"\n🆔 User ID: {user.id}"

                msg_text = (
                    f"📨 收到私聊消息\n"
                    f"{info}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{text}"
                )
                try:
                    await context.bot.send_message(owner_id, msg_text)
                except Exception as e:
                    logger.error(f"转发私聊给 OWNER 失败: {e}")

        return

    # ---------- 群聊：确保群配置存在 ----------
    get_group_config(chat_id)

    # ---------- 管理员管理 ----------
    if text == "显示机器人管理员":
        if not is_bot_admin(user.id):
            return
        admins = get_all_admins()
        if not admins:
            await update.message.reply_text("👥 当前没有设置机器人管理员")
            return

        lines = ["👥 机器人管理员列表：\n"]
        for a in admins:
            name = a.get("first_name", "Unknown")
            username = a.get("username") or "N/A"
            uid = a["user_id"]
            is_owner = a.get("is_owner", False)
            mark = " 🔱" if is_owner else ""
            lines.append(f"• {name} (@{username}){mark}")
            lines.append(f"  ID: {uid}")
        await update.message.reply_text("\n".join(lines))
        return

    if text in ("设置机器人管理员", "添加机器人管理员"):
        if not is_bot_admin(user.id):
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要设置为管理员的用户消息")
            return
        target = update.message.reply_to_message.from_user
        add_admin(target.id, target.username, target.first_name, is_owner=False)
        await update.message.reply_text(
            f"✅ 已将 {target.first_name} 设置为机器人管理员\n🆔 User ID: {target.id}"
        )
        return

    if text in ("删除机器人管理员", "移除机器人管理员"):
        if not is_bot_admin(user.id):
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要删除的管理员消息")
            return
        target = update.message.reply_to_message.from_user
        remove_admin(target.id)
        await update.message.reply_text(f"✅ 已移除 {target.first_name} 的管理员权限")
        return

    # ---------- 撤销 ----------
    if text == "撤销":
        if not is_bot_admin(user.id):
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要撤销的账单消息")
            return

        target_msg_id = update.message.reply_to_message.message_id
        deleted = delete_transaction_by_message_id(target_msg_id)
        if deleted:
            await update.message.reply_text(
                f"✅ 已撤销交易\n"
                f"类型: {deleted['transaction_type']}\n"
                f"金额: {deleted['amount']}\n"
                f"USDT: {deleted['usdt']}"
            )
            await send_summary_with_button(update, chat_id, user.id)
        else:
            await update.message.reply_text("❌ 未找到该消息对应的交易记录")
        return

    # ---------- 账单查看 ----------
    if text == "+0":
        await send_summary_with_button(update, chat_id, user.id)
        return

    if text in ("更多记录", "查看更多记录", "更多账单", "显示历史账单"):
        await update.message.reply_text(render_full_summary(chat_id))
        return

    # ---------- 重置 / 清除 ----------
    if text == "重置默认值":
        if not is_bot_admin(user.id):
            return
        update_group_config(
            chat_id,
            in_rate=0.10,
            in_fx=153,
            out_rate=0.02,
            out_fx=137,
        )
        await update.message.reply_text(
            "✅ 已重置为推荐默认值\n\n"
            "📥 入金设置：费率 10% / 汇率 153\n"
            "📤 出金设置：费率 2% / 汇率 137"
        )
        return

    if text == "清除数据":
        if not is_bot_admin(user.id):
            return
        stats = clear_today_transactions(chat_id)

        in_c = stats.get("in", {}).get("count", 0)
        in_u = stats.get("in", {}).get("usdt", 0.0)
        out_c = stats.get("out", {}).get("count", 0)
        out_u = stats.get("out", {}).get("usdt", 0.0)
        send_c = stats.get("send", {}).get("count", 0)
        send_u = stats.get("send", {}).get("usdt", 0.0)

        total = in_c + out_c + send_c

        if total == 0:
            await update.message.reply_text("ℹ️ 今日暂无数据，无需清除")
        else:
            lines = [
                "✅ 已清除今日数据（00:00 至现在）\n",
                f"📥 入金：清除 {in_c} 笔 ({in_u:.2f} USDT)",
                f"📤 出金：清除 {out_c} 笔 ({out_u:.2f} USDT)",
                f"💰 下发：清除 {send_c} 笔 ({send_u:.2f} USDT)",
            ]
            await update.message.reply_text("\n".join(lines))

        await send_summary_with_button(update, chat_id, user.id)
        return

    # ---------- 设置费率 / 汇率 ----------
    if text.startswith(("设置入金费率", "设置入金汇率", "设置出金费率", "设置出金汇率")):
        if not is_bot_admin(user.id):
            return
        try:
            if "入金费率" in text:
                val = float(text.replace("设置入金费率", "").strip()) / 100.0
                update_group_config(chat_id, in_rate=val)
                await update.message.reply_text(f"✅ 已设置默认入金费率：{val*100:.0f}%")
            elif "入金汇率" in text:
                val = float(text.replace("设置入金汇率", "").strip())
                update_group_config(chat_id, in_fx=val)
                await update.message.reply_text(f"✅ 已设置默认入金汇率：{val}")
            elif "出金费率" in text:
                val = float(text.replace("设置出金费率", "").strip()) / 100.0
                update_group_config(chat_id, out_rate=val)
                await update.message.reply_text(f"✅ 已设置默认出金费率：{val*100:.0f}%")
            elif "出金汇率" in text:
                val = float(text.replace("设置出金汇率", "").strip())
                update_group_config(chat_id, out_fx=val)
                await update.message.reply_text(f"✅ 已设置默认出金汇率：{val}")
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入数字")
        return

    # ---------- 入金 ----------
    if text.startswith("+"):
        if not is_bot_admin(user.id):
            return
        amt, country = parse_amount_and_country(text)
        if amt is None:
            return

        cfg = get_group_config(chat_id)
        rate = float(cfg.get("in_rate", 0) or 0)
        fx = float(cfg.get("in_fx", 0) or 0)

        if fx == 0:
            await update.message.reply_text("⚠️ 请先设置费率和汇率")
            return

        amt_f = float(amt)
        usdt = trunc2(amt_f * (1 - rate) / fx)  # 入金：截断两位小数

        txn_id = add_transaction(
            chat_id=chat_id,
            transaction_type="in",
            amount=Decimal(str(amt_f)),
            rate=Decimal(str(rate)),
            fx=Decimal(str(fx)),
            usdt=Decimal(str(usdt)),
            timestamp=ts,
            country=country,
            operator_id=user.id,
            operator_name=user.first_name,
        )

        append_log(
            log_path(chat_id, country, dstr),
            f"[入金] 时间:{ts} 国家:{country} 原始:{amt_f} "
            f"汇率:{fx} 费率:{rate*100:.2f}% 结果:{usdt}",
        )

        msg = await send_summary_with_button(update, chat_id, user.id)
        if msg and txn_id:
            update_transaction_message_id(txn_id, msg.message_id)
        return

    # ---------- 出金 ----------
    if text.startswith("-"):
        if not is_bot_admin(user.id):
            return
        amt, country = parse_amount_and_country(text)
        if amt is None:
            return

        cfg = get_group_config(chat_id)
        rate = float(cfg.get("out_rate", 0) or 0)
        fx = float(cfg.get("out_fx", 0) or 0)

        if fx == 0:
            await update.message.reply_text("⚠️ 请先设置费率和汇率")
            return

        amt_f = float(amt)
        usdt = round2(amt_f * (1 + rate) / fx)  # 出金：四舍五入两位

        txn_id = add_transaction(
            chat_id=chat_id,
            transaction_type="out",
            amount=Decimal(str(amt_f)),
            rate=Decimal(str(rate)),
            fx=Decimal(str(fx)),
            usdt=Decimal(str(usdt)),
            timestamp=ts,
            country=country,
            operator_id=user.id,
            operator_name=user.first_name,
        )

        append_log(
            log_path(chat_id, country, dstr),
            f"[出金] 时间:{ts} 国家:{country} 原始:{amt_f} "
            f"汇率:{fx} 费率:{rate*100:.2f}% 下发:{usdt}",
        )

        msg = await send_summary_with_button(update, chat_id, user.id)
        if msg and txn_id:
            update_transaction_message_id(txn_id, msg.message_id)
        return

    # ---------- 下发 USDT ----------
    if text.startswith("下发"):
        if not is_bot_admin(user.id):
            return
        try:
            usdt_raw = float(text.replace("下发", "").strip())
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入数字，例如：下发35.04")
            return

        usdt_abs = round2(abs(usdt_raw))  # 下发记录用四舍五入

        txn_id = add_transaction(
            chat_id=chat_id,
            transaction_type="send",
            amount=Decimal(str(usdt_abs)),
            rate=Decimal("0"),
            fx=Decimal("0"),
            usdt=Decimal(str(usdt_abs)),
            timestamp=ts,
            country="通用",
            operator_id=user.id,
            operator_name=user.first_name,
        )

        if usdt_raw > 0:
            append_log(
                log_path(chat_id, None, dstr),
                f"[下发USDT] 时间:{ts} 金额:{usdt_abs} USDT",
            )
        else:
            append_log(
                log_path(chat_id, None, dstr),
                f"[撤销下发] 时间:{ts} 金额:{usdt_abs} USDT",
            )

        msg = await send_summary_with_button(update, chat_id, user.id)
        if msg and txn_id:
            update_transaction_message_id(txn_id, msg.message_id)
        return

    # 其他内容不回复
    return

# ========== Flask 路由 ==========

@app.route("/")
def index():
    return "Telegram Bot + Web Dashboard (JSON DB) 运行中", 200


@app.route("/health")
def health():
    return "OK", 200


# **轮询版不需要 webhook 路由，这里删除 / 注释掉原有 /webhook/<token>**


# ----- Dashboard -----

@app.route("/dashboard")
@login_required
def dashboard():
    user_info = session["user_info"]
    chat_id = user_info["chat_id"]
    user_id = user_info["user_id"]

    cfg = get_group_config(chat_id)
    display = {
        "deposit_fee_rate": float(cfg.get("in_rate", 0) or 0) * 100,
        "deposit_fx": float(cfg.get("in_fx", 0) or 0),
        "withdrawal_fee_rate": float(cfg.get("out_rate", 0) or 0) * 100,
        "withdrawal_fx": float(cfg.get("out_fx", 0) or 0),
    }

    is_owner = False
    if OWNER_ID and OWNER_ID.isdigit():
        is_owner = user_id == int(OWNER_ID)

    return render_template(
        "dashboard.html",
        chat_id=chat_id,
        user_id=user_id,
        is_owner=is_owner,
        config=display,
    )


@app.route("/api/transactions")
@login_required
def api_transactions():
    user_info = session["user_info"]
    chat_id = user_info["chat_id"]

    txns = get_today_transactions(chat_id)
    records = []
    for t in txns:
        rtype = {
            "in": "deposit",
            "out": "withdrawal",
            "send": "disbursement",
        }.get(t["transaction_type"], "unknown")

        created_raw = t.get("created_at")
        ts_val = 0
        if isinstance(created_raw, str):
            try:
                ts_val = datetime.fromisoformat(created_raw).timestamp()
            except Exception:
                ts_val = 0

        records.append(
            {
                "time": t["timestamp"],
                "type": rtype,
                "amount": float(t["amount"]),
                "fee_rate": float(t["rate"]) * 100,
                "exchange_rate": float(t["fx"]),
                "usdt": float(t["usdt"]),
                "operator": t.get("operator_name", "未知"),
                "message_id": t.get("message_id"),
                "timestamp": ts_val,
            }
        )

    stats = {
        "total_deposit": sum(r["amount"] for r in records if r["type"] == "deposit"),
        "total_deposit_usdt": sum(r["usdt"] for r in records if r["type"] == "deposit"),
        "total_withdrawal": sum(
            r["amount"] for r in records if r["type"] == "withdrawal"
        ),
        "total_withdrawal_usdt": sum(
            r["usdt"] for r in records if r["type"] == "withdrawal"
        ),
        "total_disbursement": sum(
            r["usdt"] for r in records if r["type"] == "disbursement"
        ),
        "pending_disbursement": 0,
        "by_operator": {},
    }

    stats["pending_disbursement"] = (
        stats["total_deposit_usdt"]
        - stats["total_withdrawal_usdt"]
        - stats["total_disbursement"]
    )

    for r in records:
        op = r["operator"]
        if op not in stats["by_operator"]:
            stats["by_operator"][op] = {
                "deposit_count": 0,
                "deposit_usdt": 0,
                "withdrawal_count": 0,
                "withdrawal_usdt": 0,
                "disbursement_count": 0,
                "disbursement_usdt": 0,
            }
        bucket = stats["by_operator"][op]
        if r["type"] == "deposit":
            bucket["deposit_count"] += 1
            bucket["deposit_usdt"] += r["usdt"]
        elif r["type"] == "withdrawal":
            bucket["withdrawal_count"] += 1
            bucket["withdrawal_usdt"] += r["usdt"]
        elif r["type"] == "disbursement":
            bucket["disbursement_count"] += 1
            bucket["disbursement_usdt"] += r["usdt"]

    return jsonify({"success": True, "records": records, "statistics": stats})


@app.route("/api/rollback", methods=["POST"])
@login_required
def api_rollback():
    user_info = session["user_info"]
    user_id = user_info["user_id"]

    is_owner = False
    if OWNER_ID and OWNER_ID.isdigit():
        is_owner = user_id == int(OWNER_ID)
    if not is_owner:
        return jsonify({"success": False, "error": "无权限"}), 403

    data = request.json or {}
    msg_id = data.get("message_id")
    if not msg_id:
        return jsonify({"success": False, "error": "参数错误"}), 400

    deleted = delete_transaction_by_message_id(msg_id)
    if deleted:
        return jsonify({"success": True, "message": "交易已回退"})
    return jsonify({"success": False, "error": "未找到交易"}), 404

# ========== Bot 初始化 & 事件循环（轮询） ==========

async def setup_telegram_bot_polling():
    """
    初始化 Telegram Bot，并使用 long polling 接收消息。
    不需要任何公网 HTTPS / Webhook。
    """
    global telegram_app

    logger.info("🤖 初始化 Telegram Bot Application (JSON DB, polling 模式)...")
    telegram_app = Application.builder().token(BOT_TOKEN).build()

    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_text)
    )

    logger.info("🔄 Bot 开始轮询接收消息 (run_polling)...")
    await telegram_app.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("🛑 Bot 轮询结束")


def run_bot_loop():
    """
    在单独线程中启动 asyncio 事件循环，运行轮询。
    """
    asyncio.run(setup_telegram_bot_polling())

# ========== 应用初始化 ==========

def init_app():
    logger.info("=" * 50)
    logger.info("🚀 启动 Telegram Bot + Web Dashboard (JSON DB / polling)")
    logger.info("=" * 50)

    init_database()
    logger.info("✅ JSON 数据库初始化完成")

    if OWNER_ID and OWNER_ID.isdigit():
        add_admin(int(OWNER_ID), None, "Owner", is_owner=True)
        logger.info(f"✅ OWNER 已设置为管理员: {OWNER_ID}")

    logger.info("✅ 应用初始化完成")
    logger.info("=" * 50)

# ========== 主入口 ==========

if __name__ == "__main__":
    init_app()

    logger.info("🔄 启动 Bot 轮询线程...")
    t = threading.Thread(target=run_bot_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "5000"))
    logger.info(f"🌐 Flask 应用启动在端口: {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
