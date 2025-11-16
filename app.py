#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flask + Telegram Webhook 版 财务记账机器人（PostgreSQL）
"""

import os
import re
import hmac
import math
import hashlib
import logging
import asyncio
import threading
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    session,
)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import pytz
import database as db

# ========== 环境 & Flask 初始化 ==========

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
SESSION_SECRET = os.getenv("SESSION_SECRET")
WEB_BASE_URL = os.getenv("WEB_BASE_URL")  # 例如 https://xxx.ap-northeast-1.clawcloudrun.com
WEBHOOK_URL = os.getenv("WEBHOOK_URL")    # 例如 https://xxx.ap-northeast-1.clawcloudrun.com
PORT = int(os.getenv("PORT", "5000"))

if not BOT_TOKEN:
    raise RuntimeError("❌ 未设置 TELEGRAM_BOT_TOKEN 环境变量")

if not SESSION_SECRET:
    print("⚠️ SESSION_SECRET 未设置，Web 查账功能将不可用")

app = Flask(__name__)
app.secret_key = SESSION_SECRET or os.urandom(24)

# ========== 日志 & 目录 ==========

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DATA_DIR = Path("./data")
LOG_DIR = DATA_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

telegram_app: Application | None = None
bot_loop: asyncio.AbstractEventLoop | None = None

# ========== 工具函数 ==========


def trunc2(x) -> float:
    """截断到小数点后两位（用于入金），兼容 float / Decimal"""
    x = float(x)
    rounded = round(x, 6)
    return math.floor(rounded * 100.0) / 100.0


def round2(x) -> float:
    """四舍五入到小数点后两位（用于出金 / 下发），兼容 float / Decimal"""
    x = float(x)
    return round(x, 2)


def fmt_usdt(x: float) -> str:
    return f"{x:.2f} USDT"


def to_superscript(num: int) -> str:
    """将数字转换为上标"""
    mp = {
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
    return "".join(mp.get(c, c) for c in str(num))


def now_ts() -> str:
    """当前北京时间 HH:MM"""
    tz = pytz.timezone("Asia/Shanghai")
    return datetime.now(tz).strftime("%H:%M")


def today_str() -> str:
    """当前北京时间 YYYY-MM-DD"""
    tz = pytz.timezone("Asia/Shanghai")
    return datetime.now(tz).strftime("%Y-%m-%d")


def log_path(chat_id: int, country: str | None = None, date_str: str | None = None) -> Path:
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
    """解析 +10000 / 日本 形式"""
    m = re.match(r"^[\+\-]\s*([0-9]+(?:\.[0-9]+)?)", text.strip())
    if not m:
        return None, None
    amount = float(m.group(1))
    m2 = re.search(r"/\s*([^\s]+)$", text)
    country = m2.group(1) if m2 else "通用"
    return amount, country


def is_bot_admin(user_id: int) -> bool:
    """是否机器人管理员（包含 OWNER）"""
    if OWNER_ID and OWNER_ID.isdigit() and int(OWNER_ID) == user_id:
        return True
    return db.is_admin(user_id)


# ========== Web Token 认证（仪表盘用） ==========


def generate_web_token(chat_id: int, user_id: int, hours: int = 24) -> str | None:
    if not SESSION_SECRET:
        return None
    expires_at = int((datetime.now() + timedelta(hours=hours)).timestamp())
    data = f"{chat_id}:{user_id}:{expires_at}"
    sig = hmac.new(SESSION_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}:{sig}"


def verify_token(token: str):
    if not SESSION_SECRET:
        return None
    try:
        parts = token.split(":")
        if len(parts) != 4:
            return None
        chat_id, user_id, expires_at, sig = parts
        chat_id = int(chat_id)
        user_id = int(user_id)
        expires_at = int(expires_at)
        data = f"{chat_id}:{user_id}:{expires_at}"
        expect = hmac.new(SESSION_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()
        if sig != expect:
            return None
        if datetime.now().timestamp() > expires_at:
            return None
        return {"chat_id": chat_id, "user_id": user_id}
    except Exception:
        return None


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.args.get("token") or session.get("token")
        if not token:
            return "未授权访问", 403
        info = verify_token(token)
        if not info:
            return "Token 无效或已过期", 403
        session["token"] = token
        session["user_info"] = info
        return f(*args, **kwargs)

    return wrapper


def generate_web_url(chat_id: int, user_id: int) -> str | None:
    """只有设置了 WEB_BASE_URL 才生成按钮，避免 localhost 报错"""
    if not SESSION_SECRET:
        return None
    if not WEB_BASE_URL:
        return None
    token = generate_web_token(chat_id, user_id)
    return f"{WEB_BASE_URL.rstrip('/')}/dashboard?token={token}"


# ========== 渲染账单文本 ==========


def render_group_summary(chat_id: int) -> str:
    config = db.get_group_config(chat_id)
    summary = db.get_transactions_summary(chat_id)

    bot_name = config.get("group_name") or "AA全球国际支付"

    in_records = summary["in_records"]
    out_records = summary["out_records"]
    send_records = summary["send_records"]

    should = trunc2(summary["should_send"])
    sent = trunc2(summary["send_usdt"])
    diff = trunc2(should - sent)

    rin = config.get("in_rate", 0)
    fin = config.get("in_fx", 0)
    rout = config.get("out_rate", 0)
    fout = config.get("out_fx", 0)

    lines: list[str] = []
    lines.append(f"📊【{bot_name} 账单汇总】\n")

    # 入金记录（最新在上）
    lines.append(f"已入账 ({len(in_records)}笔)")
    for r in reversed(in_records[-5:]):
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = trunc2(float(r["usdt"]))
        ts = r["timestamp"]
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw:.2f}  {rate_sup}/ {fx:.2f} = {usdt:.2f}")

    lines.append("")

    # 出金记录（最新在上）
    lines.append(f"已出账 ({len(out_records)}笔)")
    for r in reversed(out_records[-5:]):
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = round2(float(r["usdt"]))
        ts = r["timestamp"]
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw:.2f}  {rate_sup}/ {fx:.2f} = {usdt:.2f}")

    lines.append("")

    # 下发记录（最新在上）
    if send_records:
        lines.append(f"已下发 ({len(send_records)}笔)")
        for r in reversed(send_records[-5:]):
            usdt = round2(float(r["usdt"]))
            ts = r["timestamp"]
            lines.append(f"{ts} {usdt:.2f}")
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
    config = db.get_group_config(chat_id)
    summary = db.get_transactions_summary(chat_id)

    bot_name = config.get("group_name") or "全球国际支付"

    in_records = summary["in_records"]
    out_records = summary["out_records"]
    send_records = summary["send_records"]

    should = trunc2(summary["should_send"])
    sent = trunc2(summary["send_usdt"])
    diff = trunc2(should - sent)

    rin = config.get("in_rate", 0)
    fin = config.get("in_fx", 0)
    rout = config.get("out_rate", 0)
    fout = config.get("out_fx", 0)

    lines: list[str] = []
    lines.append(f"📊【{bot_name} 完整账单】\n")

    lines.append(f"已入账 ({len(in_records)}笔)")
    for r in in_records:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = trunc2(float(r["usdt"]))
        ts = r["timestamp"]
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw:.2f}  {rate_sup}/ {fx:.2f} = {usdt:.2f}")

    lines.append("")
    lines.append(f"已出账 ({len(out_records)}笔)")
    for r in out_records:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = round2(float(r["usdt"]))
        ts = r["timestamp"]
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw:.2f}  {rate_sup}/ {fx:.2f} = {usdt:.2f}")

    lines.append("")
    if send_records:
        lines.append(f"已下发 ({len(send_records)}笔)")
        for r in send_records:
            usdt = round2(float(r["usdt"]))
            ts = r["timestamp"]
            lines.append(f"{ts} {usdt:.2f}")
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

    markup = None
    web_url = generate_web_url(chat_id, user_id)
    if web_url:
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📊 查看账单明细", url=web_url)]]
        )

    if markup:
        msg = await update.message.reply_text(text, reply_markup=markup)
    else:
        msg = await update.message.reply_text(text)

    return msg


# ========== Telegram 指令 & 消息处理 ==========


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    help_text = (
        "🤖 你好，我是财务记账机器人。\n\n"
        "📊 记账操作：\n"
        "  入金：+10000 或 +10000 / 日本\n"
        "  出金：-10000 或 -10000 / 日本\n"
        "  查看账单：+0 或 更多记录\n\n"
        "💰 USDT 下发（仅管理员）：\n"
        "  下发35.04（记录下发并扣除应下发）\n"
        "  下发-35.04（撤销下发并增加应下发）\n\n"
        "🔄 撤销操作（仅管理员）：\n"
        "  回复账单消息 + 输入：撤销\n\n"
        "⚙️ 快速设置（仅管理员）：\n"
        "  重置默认值\n"
        "  清除数据\n"
        "  设置入金费率 10\n"
        "  设置入金汇率 153\n"
        "  设置出金费率 2\n"
        "  设置出金汇率 142\n"
        "  设置账单名称 AA全球国际支付\n\n"
        "👥 管理员管理：\n"
        "  设置机器人管理员（回复消息）\n"
        "  删除机器人管理员（回复消息）\n"
        "  显示机器人管理员"
    )

    if chat.type == "private":
        db.add_private_chat_user(user.id, user.username, user.first_name)

    await update.message.reply_text(help_text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    chat = update.effective_chat
    chat_id = chat.id
    text = (update.message.text or update.message.caption or "").strip()
    if not text:
        return

    ts = now_ts()
    dstr = today_str()

    # ---------- 私聊 ----------
    if chat.type == "private":
        db.add_private_chat_user(user.id, user.username, user.first_name)

        private_dir = LOG_DIR / "private_chats"
        private_dir.mkdir(exist_ok=True)
        log_file = private_dir / f"user_{user.id}.log"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {user.full_name} (@{user.username or 'N/A'}): {text}\n")

        return

    # ---------- 群聊 ----------
    db.get_group_config(chat_id)  # 确保群记录存在

    # === 所有人都能用：+0 查看账单 ===
    if text.replace(" ", "") == "+0":
        await send_summary_with_button(update, chat_id, user.id)
        return

    # 显示机器人管理员
    if text == "显示机器人管理员":
        if not is_bot_admin(user.id):
            return
        admins = db.get_all_admins()
        if not admins:
            await update.message.reply_text("👥 当前没有设置机器人管理员")
            return
        lines = ["👥 机器人管理员列表：\n"]
        for a in admins:
            name = a.get("first_name", "Unknown")
            username = a.get("username") or "N/A"
            uid = a["user_id"]
            is_owner = a.get("is_owner", False)
            status = " 🔱" if is_owner else ""
            lines.append(f"• {name} (@{username}){status}")
            lines.append(f"  ID: {uid}")
        await update.message.reply_text("\n".join(lines))
        return

    # 设置/删除机器人管理员
    if text in ("设置机器人管理员", "添加机器人管理员"):
        if not is_bot_admin(user.id):
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要设置为管理员的用户消息")
            return
        target = update.message.reply_to_message.from_user
        db.add_admin(target.id, target.username, target.first_name, is_owner=False)
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
        db.remove_admin(target.id)
        await update.message.reply_text(f"✅ 已移除 {target.first_name} 的管理员权限")
        return

    # 撤销
    if text == "撤销":
        if not is_bot_admin(user.id):
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要撤销的账单消息")
            return
        msg_id = update.message.reply_to_message.message_id
        deleted = db.delete_transaction_by_message_id(msg_id)
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

    # 重置默认值
    if text == "重置默认值":
        if not is_bot_admin(user.id):
            return
        db.update_group_config(
            chat_id,
            in_rate=0.10,
            in_fx=153,
            out_rate=0.02,
            out_fx=137,
        )
        await update.message.reply_text(
            "✅ 已重置为默认值\n\n"
            "📥 入金：费率 10%  汇率 153\n"
            "📤 出金：费率 2%   汇率 137"
        )
        await send_summary_with_button(update, chat_id, user.id)
        return

    # 清除数据（今日 00:00 起）
    if text == "清除数据":
        if not is_bot_admin(user.id):
            return
        stats = db.clear_today_transactions(chat_id)
        in_c = stats.get("in", {}).get("count", 0)
        in_u = stats.get("in", {}).get("usdt", 0)
        out_c = stats.get("out", {}).get("count", 0)
        out_u = stats.get("out", {}).get("usdt", 0)
        send_c = stats.get("send", {}).get("count", 0)
        send_u = stats.get("send", {}).get("usdt", 0)
        total = in_c + out_c + send_c
        if total == 0:
            await update.message.reply_text(
                "ℹ️ 今日 00:00 之后暂无数据， 无需清除。"
            )
        else:
            lines = [
                "✅ 已清除今日数据（00:00 至现在）\n",
                f"📥 入账：{in_c} 笔（{in_u:.2f} USDT）",
                f"📤 出账：{out_c} 笔（{out_u:.2f} USDT）",
                f"💰 下发：{send_c} 笔（{send_u:.2f} USDT）",
            ]
            await update.message.reply_text("\n".join(lines))
        await send_summary_with_button(update, chat_id, user.id)
        return

    # 设置账单名称
    if text.startswith("设置账单名称"):
        if not is_bot_admin(user.id):
            return
        name = text.replace("设置账单名称", "", 1).strip()
        if not name:
            await update.message.reply_text("❌ 请输入新的账单名称，例如：设置账单名称 AA全球国际支付")
            return
        db.update_group_config(chat_id, group_name=name)
        await update.message.reply_text(f"✅ 已将账单名称设置为：{name}")
        await send_summary_with_button(update, chat_id, user.id)
        return

    # 设置费率 / 汇率
    if text.startswith(("设置入金费率", "设置入金汇率", "设置出金费率", "设置出金汇率")):
        if not is_bot_admin(user.id):
            return
        try:
            if "入金费率" in text:
                val = float(text.replace("设置入金费率", "").strip()) / 100.0
                db.update_group_config(chat_id, in_rate=val)
                await update.message.reply_text(f"✅ 已设置入金费率为 {val*100:.0f}%")
            elif "入金汇率" in text:
                val = float(text.replace("设置入金汇率", "").strip())
                db.update_group_config(chat_id, in_fx=val)
                await update.message.reply_text(f"✅ 已设置入金汇率为 {val}")
            elif "出金费率" in text:
                val = float(text.replace("设置出金费率", "").strip()) / 100.0
                db.update_group_config(chat_id, out_rate=val)
                await update.message.reply_text(f"✅ 已设置出金费率为 {val*100:.0f}%")
            elif "出金汇率" in text:
                val = float(text.replace("设置出金汇率", "").strip())
                db.update_group_config(chat_id, out_fx=val)
                await update.message.reply_text(f"✅ 已设置出金汇率为 {val}")
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入数字")
        return

    # 入金（仅管理员，注意已经排除 +0）
    if text.startswith("+"):
        if not is_bot_admin(user.id):
            return
        amt, country = parse_amount_and_country(text)
        if amt is None or amt == 0:
            return
        config = db.get_group_config(chat_id)
        rate = config.get("in_rate", 0)
        fx = config.get("in_fx", 0)
        if fx == 0:
            await update.message.reply_text("⚠️ 请先设置费率和汇率")
            return
        amt_f = float(amt)
        rate_f = float(rate)
        fx_f = float(fx)
        usdt = trunc2(amt_f * (1 - rate_f) / fx_f)

        txn_id = db.add_transaction(
            chat_id=chat_id,
            transaction_type="in",
            amount=Decimal(str(amt)),
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
            f"[入金] 时间:{ts} 国家:{country or '通用'} 原始:{amt} "
            f"汇率:{fx} 费率:{rate*100:.2f}% 结果:{usdt}",
        )

        msg = await send_summary_with_button(update, chat_id, user.id)
        if msg and txn_id:
            db.update_transaction_message_id(txn_id, msg.message_id)
        return

    # 出金（仅管理员）
    if text.startswith("-"):
        if not is_bot_admin(user.id):
            return
        amt, country = parse_amount_and_country(text)
        if amt is None or amt == 0:
            return
        config = db.get_group_config(chat_id)
        rate = config.get("out_rate", 0)
        fx = config.get("out_fx", 0)
        if fx == 0:
            await update.message.reply_text("⚠️ 请先设置费率和汇率")
            return
        amt_f = float(amt)
        rate_f = float(rate)
        fx_f = float(fx)
        usdt = round2(amt_f * (1 + rate_f) / fx_f)

        txn_id = db.add_transaction(
            chat_id=chat_id,
            transaction_type="out",
            amount=Decimal(str(amt)),
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
            f"[出金] 时间:{ts} 国家:{country or '通用'} 原始:{amt} "
            f"汇率:{fx} 费率:{rate*100:.2f}% 下发:{usdt}",
        )

        msg = await send_summary_with_button(update, chat_id, user.id)
        if msg and txn_id:
            db.update_transaction_message_id(txn_id, msg.message_id)
        return

    # 下发 USDT（仅管理员）  下发100 / 下发-100
    if text.startswith("下发"):
        if not is_bot_admin(user.id):
            return
        try:
            usdt_str = text.replace("下发", "", 1).strip()
            usdt_val = float(usdt_str)
        except ValueError:
            await update.message.reply_text(
                "❌ 格式错误，请输入：下发35.04 或 下发-35.04"
            )
            return

        txn_id = db.add_transaction(
            chat_id=chat_id,
            transaction_type="send",
            amount=Decimal(str(abs(usdt_val))),
            rate=Decimal("0"),
            fx=Decimal("0"),
            usdt=Decimal(str(usdt_val)),  # 正数为下发，负数为撤销下发
            timestamp=ts,
            country="通用",
            operator_id=user.id,
            operator_name=user.first_name,
        )

        if usdt_val > 0:
            append_log(
                log_path(chat_id, None, dstr),
                f"[下发USDT] 时间:{ts} 金额:{usdt_val:.2f} USDT",
            )
        else:
            append_log(
                log_path(chat_id, None, dstr),
                f"[撤销下发] 时间:{ts} 金额:{abs(usdt_val):.2f} USDT",
            )

        msg = await send_summary_with_button(update, chat_id, user.id)
        if msg and txn_id:
            db.update_transaction_message_id(txn_id, msg.message_id)
        return

    # 更多记录
    if text in ("更多记录", "查看更多记录", "更多账单", "显示历史账单"):
        await update.message.reply_text(render_full_summary(chat_id))
        return


# ========== Telegram Application 事件循环 ==========


def run_bot_loop():
    """在独立线程中运行 Telegram Application"""
    global telegram_app, bot_loop

    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)

    async def _init_app():
        global telegram_app
        logger.info("🤖 初始化 Telegram Bot Application...")
        telegram_app = (
            Application.builder()
            .token(BOT_TOKEN)
            .build()
        )

        telegram_app.add_handler(CommandHandler("start", cmd_start))
        telegram_app.add_handler(MessageHandler(filters.ALL, handle_text))

        # 设置 Webhook
        if WEBHOOK_URL:
            webhook_url = f"{WEBHOOK_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
            logger.info(f"🔗 设置 Webhook: {webhook_url}")
            await telegram_app.bot.set_webhook(url=webhook_url)
            logger.info("✅ Webhook 已设置")
        else:
            logger.warning("⚠️ 未设置 WEBHOOK_URL，Bot 无法通过 Webhook 收到消息")

        await telegram_app.initialize()
        await telegram_app.start()
        logger.info("✅ Telegram Bot 初始化完成")

    try:
        bot_loop.run_until_complete(_init_app())
        bot_loop.run_forever()
    except Exception as e:
        logger.exception("❌ Telegram Bot 事件循环异常: %s", e)
    finally:
        if telegram_app:
            bot_loop.run_until_complete(telegram_app.stop())
            bot_loop.run_until_complete(telegram_app.shutdown())


# ========== Flask 路由 ==========


@app.route("/")
def index():
    return "Telegram Bot + Web Dashboard - 运行中", 200


@app.route("/health")
def health():
    return "OK", 200


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global telegram_app, bot_loop
    if not telegram_app or not bot_loop:
        return "Bot not ready", 503
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)
        asyncio.run_coroutine_threadsafe(
            telegram_app.process_update(update), bot_loop
        )
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook 处理错误: {e}")
        return "Error", 500


@app.route("/dashboard")
@login_required
def dashboard():
    info = session["user_info"]
    chat_id = info["chat_id"]
    user_id = info["user_id"]

    cfg = db.get_group_config(chat_id)
    display_cfg = {
        "deposit_fee_rate": cfg.get("in_rate", 0) * 100,
        "deposit_fx": cfg.get("in_fx", 0),
        "withdrawal_fee_rate": cfg.get("out_rate", 0) * 100,
        "withdrawal_fx": cfg.get("out_fx", 0),
        "group_name": cfg.get("group_name") or "AA全球国际支付",
    }

    is_owner = OWNER_ID and OWNER_ID.isdigit() and int(OWNER_ID) == user_id

    return render_template(
        "dashboard.html",
        chat_id=chat_id,
        user_id=user_id,
        is_owner=is_owner,
        config=display_cfg,
    )


@app.route("/api/transactions")
@login_required
def api_transactions():
    info = session["user_info"]
    chat_id = info["chat_id"]

    txns = db.get_today_transactions(chat_id)
    records = []
    for t in txns:
        rec_type = {
            "in": "deposit",
            "out": "withdrawal",
            "send": "disbursement",
        }.get(t["transaction_type"], "unknown")

        records.append(
            {
                "time": t["timestamp"],
                "type": rec_type,
                "amount": float(t["amount"]),
                "fee_rate": float(t["rate"]) * 100,
                "exchange_rate": float(t["fx"]),
                "usdt": float(t["usdt"]),
                "operator": t.get("operator_name", "未知"),
                "message_id": t.get("message_id"),
                "timestamp": t.get("created_at").timestamp()
                if t.get("created_at")
                else 0,
            }
        )

    stats = {
        "total_deposit": sum(r["amount"] for r in records if r["type"] == "deposit"),
        "total_deposit_usdt": sum(
            r["usdt"] for r in records if r["type"] == "deposit"
        ),
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
        if r["type"] == "deposit":
            stats["by_operator"][op]["deposit_count"] += 1
            stats["by_operator"][op]["deposit_usdt"] += r["usdt"]
        elif r["type"] == "withdrawal":
            stats["by_operator"][op]["withdrawal_count"] += 1
            stats["by_operator"][op]["withdrawal_usdt"] += r["usdt"]
        elif r["type"] == "disbursement":
            stats["by_operator"][op]["disbursement_count"] += 1
            stats["by_operator"][op]["disbursement_usdt"] += r["usdt"]

    return jsonify({"success": True, "records": records, "statistics": stats})


@app.route("/api/rollback", methods=["POST"])
@login_required
def api_rollback():
    info = session["user_info"]
    user_id = info["user_id"]

    is_owner = OWNER_ID and OWNER_ID.isdigit() and int(OWNER_ID) == user_id
    if not is_owner:
        return jsonify({"success": False, "error": "无权限"}), 403

    data = request.json or {}
    msg_id = data.get("message_id")
    if not msg_id:
        return jsonify({"success": False, "error": "参数错误"}), 400

    deleted = db.delete_transaction_by_message_id(msg_id)
    if deleted:
        return jsonify({"success": True, "message": "交易已回退"})
    return jsonify({"success": False, "error": "未找到该交易记录"}), 404


# ========= 应用初始化函数 =========


def log_env_info():
    logger.info("📋 环境变量检查：")
    logger.info(f"   PORT={PORT}")
    logger.info(
        f"   DATABASE_URL={'已设置' if os.getenv('DATABASE_URL') else '未设置'}"
    )
    logger.info(f"   TELEGRAM_BOT_TOKEN={'已设置' if BOT_TOKEN else '未设置'}")
    logger.info(f"   OWNER_ID={OWNER_ID or '未设置'}")
    logger.info(f"   WEBHOOK_URL={WEBHOOK_URL or '未设置'}")
    logger.info(
        f"   SESSION_SECRET={'已设置' if SESSION_SECRET else '未设置'}"
    )


def init_app():
    """初始化数据库、管理员、Webhook 等"""
    logger.info("=" * 50)
    logger.info("🚀 启动 Telegram Bot + Web Dashboard")
    logger.info("=" * 50)

    log_env_info()

    # 1. 初始化数据库
    try:
        db.init_database()
        # ✅ 只保留最近 N 天的交易记录（目前是 30 天）
        db.cleanup_old_transactions(30)
        logger.info("✅ 数据库初始化完成")
    except Exception as e:
        logger.exception("❌ 数据库初始化失败: %s", e)
        raise

    # 2. 初始化 OWNER 管理员
    if OWNER_ID and OWNER_ID.isdigit():
        try:
            db.add_admin(int(OWNER_ID), None, "Owner", is_owner=True)
            logger.info(f"✅ OWNER 已设置为管理员: {OWNER_ID}")
        except Exception as e:
            logger.exception("❌ 初始化 OWNER 管理员失败: %s", e)
    else:
        logger.warning("⚠️ 未设置 OWNER_ID，建议在环境变量中配置群主的 Telegram ID")

    logger.info("✅ 应用初始化完成")
    logger.info("=" * 50)

    # 3. 启动 Bot 事件循环线程
    logger.info("🔄 启动 Bot 事件循环线程...")
    t = threading.Thread(target=run_bot_loop, daemon=True)
    t.start()


# ========= 程序入口 =========

if __name__ == "__main__":
    init_app()
    logger.info(f"🌐 Flask 应用启动在端口: {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
