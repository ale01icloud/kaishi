#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一Flask应用 - Telegram Bot Webhook + Web Dashboard
整合所有功能，使用PostgreSQL数据库
"""

import os
import re
import json
import hmac
import hashlib
import math
import logging
from datetime import datetime, timedelta
from pathlib import Path
from decimal import Decimal
from functools import wraps

from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from dotenv import load_dotenv
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import database as db

# ========== 配置 ==========
load_dotenv()

app = Flask(__name__)

# 环境变量
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
SESSION_SECRET = os.getenv("SESSION_SECRET")
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # 例如: https://your-domain.com

if not BOT_TOKEN:
    raise RuntimeError("❌ 错误：未找到 TELEGRAM_BOT_TOKEN 环境变量")

if not SESSION_SECRET:
    print("⚠️  警告：SESSION_SECRET未设置，Web查账功能将不可用")
    SESSION_SECRET = None

# Flask配置
app.secret_key = SESSION_SECRET or os.urandom(24)

# 日志配置
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 数据目录
DATA_DIR = Path("./data")
LOG_DIR = DATA_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Telegram Bot Application（全局）
telegram_app = None

# ========== 工具函数 ==========


def trunc2(x) -> float:
    """截断到小数点后两位（用于入金 / 应下发），兼容 float / Decimal"""
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
    superscript_map = {
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
    return "".join(superscript_map.get(c, c) for c in str(num))


def now_ts():
    """当前时间（北京时间 HH:MM）"""
    import pytz

    beijing_tz = pytz.timezone("Asia/Shanghai")
    return datetime.now(beijing_tz).strftime("%H:%M")


def today_str():
    """当前日期（北京时间 YYYY-MM-DD）"""
    import pytz

    beijing_tz = pytz.timezone("Asia/Shanghai")
    return datetime.now(beijing_tz).strftime("%Y-%m-%d")


def log_path(chat_id: int, country: str | None = None, date_str: str | None = None) -> Path:
    """获取日志文件路径"""
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
    """追加日志"""
    with path.open("a", encoding="utf-8") as f:
        f.write(text.strip() + "\n")


def parse_amount_and_country(text: str):
    """解析金额和国家，格式：+10000 或 +10000 / 日本"""
    m = re.match(r"^[\+\-]\s*([0-9]+(?:\.[0-9]+)?)", text.strip())
    if not m:
        return None, None
    amount = float(m.group(1))
    m2 = re.search(r"/\s*([^\s]+)$", text)
    country = m2.group(1) if m2 else "通用"
    return amount, country


def is_bot_admin(user_id: int) -> bool:
    """检查是否为机器人管理员（OWNER始终为超级管理员）"""
    if OWNER_ID and OWNER_ID.isdigit() and int(OWNER_ID) == user_id:
        return True
    return db.is_admin(user_id)


# ========== Web Token认证 ==========


def generate_web_token(chat_id: int, user_id: int, expires_hours: int = 24):
    """生成Web查账访问token"""
    if not SESSION_SECRET:
        return None

    expires_at = int((datetime.now() + timedelta(hours=expires_hours)).timestamp())
    data = f"{chat_id}:{user_id}:{expires_at}"
    signature = hmac.new(
        SESSION_SECRET.encode(), data.encode(), hashlib.sha256
    ).hexdigest()
    return f"{data}:{signature}"


def verify_token(token: str):
    """验证token有效性"""
    if not SESSION_SECRET:
        return None

    try:
        parts = token.split(":")
        if len(parts) != 4:
            return None

        chat_id, user_id, expires_at, signature = parts
        chat_id = int(chat_id)
        user_id = int(user_id)
        expires_at = int(expires_at)

        data = f"{chat_id}:{user_id}:{expires_at}"
        expected_signature = hmac.new(
            SESSION_SECRET.encode(), data.encode(), hashlib.sha256
        ).hexdigest()

        if signature != expected_signature:
            return None

        if datetime.now().timestamp() > expires_at:
            return None

        return {"chat_id": chat_id, "user_id": user_id}
    except Exception:
        return None


def login_required(f):
    """登录验证装饰器"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.args.get("token") or session.get("token")
        if not token:
            return "未授权访问", 403

        user_info = verify_token(token)
        if not user_info:
            return "Token无效或已过期", 403

        session["token"] = token
        session["user_info"] = user_info

        return f(*args, **kwargs)

    return decorated_function


def generate_web_url(chat_id: int, user_id: int):
    """生成Web查账访问URL"""
    if not SESSION_SECRET:
        return None

    token = generate_web_token(chat_id, user_id)
    return f"{WEB_BASE_URL}/dashboard?token={token}"


# ========== Telegram 消息渲染 ==========


def render_group_summary(chat_id: int) -> str:
    """渲染群组账单汇总"""
    config = db.get_group_config(chat_id)
    summary = db.get_transactions_summary(chat_id)

    bot_name = config.get("group_name", "AA全球国际支付")
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

    # 入金记录（最多显示5条）
    lines.append(f"已入账 ({len(in_records)}笔)")
    for r in in_records[:5]:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = trunc2(float(r["usdt"]))
        ts = r["timestamp"]
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw}  {rate_sup}/ {fx} = {usdt}")

    lines.append("")

    # 出金记录（最多显示5条）
    lines.append(f"已出账 ({len(out_records)}笔)")
    for r in out_records[:5]:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = round2(float(r["usdt"]))
        ts = r["timestamp"]
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw}  {rate_sup}/ {fx} = {usdt}")

    lines.append("")

    # 下发记录（只有当有下发记录时才显示）
    if send_records:
        lines.append(f"已下发 ({len(send_records)}笔)")
        for r in send_records[:5]:
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
    """显示完整账单（所有记录）"""
    config = db.get_group_config(chat_id)
    summary = db.get_transactions_summary(chat_id)

    bot_name = config.get("group_name", "AA全球国际支付")
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

    # 所有入金记录
    lines.append(f"已入账 ({len(in_records)}笔)")
    for r in in_records:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = trunc2(float(r["usdt"]))
        ts = r["timestamp"]
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw}  {rate_sup}/ {fx} = {usdt}")

    lines.append("")

    # 所有出金记录
    lines.append(f"已出账 ({len(out_records)}笔)")
    for r in out_records:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = round2(float(r["usdt"]))
        ts = r["timestamp"]
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw}  {rate_sup}/ {fx} = {usdt}")

    lines.append("")

    # 所有下发记录
    if send_records:
        lines.append(f"已下发 ({len(send_records)}笔)")
        for r in send_records:
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
    """发送带Web查账按钮的汇总消息"""
    summary_text = render_group_summary(chat_id)

    if SESSION_SECRET:
        web_url = generate_web_url(chat_id, user_id)
        if web_url:
            keyboard = [[InlineKeyboardButton("📊 查看账单明细", url=web_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            msg = await update.message.reply_text(summary_text, reply_markup=reply_markup)
        else:
            msg = await update.message.reply_text(summary_text)
    else:
        msg = await update.message.reply_text(summary_text)

    return msg


# ========== Telegram Bot命令处理器 ==========


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    user = update.effective_user
    chat = update.effective_chat

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

    # 记录私聊用户
    if chat.type == "private":
        db.add_private_chat_user(user.id, user.username, user.first_name)

    await update.message.reply_text(help_text)


# ========== 文本消息处理 ==========


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理所有文本消息（群聊+私聊）"""
    user = update.effective_user
    chat = update.effective_chat
    chat_id = chat.id
    text = (update.message.text or update.message.caption or "").strip()
    ts = now_ts()
    dstr = today_str()

    # ========== 私聊逻辑 ==========
    if chat.type == "private":
        db.add_private_chat_user(user.id, user.username, user.first_name)

        # 记录私聊日志
        private_log_dir = LOG_DIR / "private_chats"
        private_log_dir.mkdir(exist_ok=True)
        user_log_file = private_log_dir / f"user_{user.id}.log"

        log_entry = f"[{ts}] {user.full_name} (@{user.username or 'N/A'}): {text}\n"
        with open(user_log_file, "a", encoding="utf-8") as f:
            f.write(log_entry)

        # OWNER 广播 / 帮助
        if OWNER_ID and OWNER_ID.isdigit() and user.id == int(OWNER_ID):
            # 广播
            if text.startswith("广播 ") or text.startswith("群发 "):
                broadcast_text = (
                    text.split(" ", 1)[1] if len(text.split(" ", 1)) > 1 else ""
                )

                if not broadcast_text:
                    await update.message.reply_text(
                        "❌ 请输入广播内容\n\n使用方法：\n广播 您的消息内容"
                    )
                    return

                users = db.get_all_private_chat_users()
                success = 0
                failed = 0

                await update.message.reply_text(
                    f"📢 开始广播...\n目标用户数：{len(users)}"
                )

                for u in users:
                    target_id = u["user_id"]
                    if target_id == int(OWNER_ID):
                        continue
                    try:
                        await context.bot.send_message(
                            chat_id=target_id,
                            text=f"📢 系统通知：\n\n{broadcast_text}",
                        )
                        success += 1
                    except Exception as e:
                        logger.error(f"广播失败 (用户 {target_id}): {e}")
                        failed += 1

                await update.message.reply_text(
                    f"✅ 广播完成！\n成功：{success} 人\n失败：{failed} 人"
                )
                return

            if text in ["help", "帮助", "功能"]:
                await update.message.reply_text(
                    "👑 OWNER专属功能：\n\n"
                    "📢 广播功能：\n"
                    "• 广播 您的消息内容\n"
                    "• 群发 您的消息内容\n\n"
                    "💬 使用说明：\n"
                    "• 广播会发送给所有私聊过的用户"
                )
                return

        # 普通用户 / 非 OWNER：把消息转发给 OWNER（如果配置了）
        if OWNER_ID and OWNER_ID.isdigit():
            owner_id = int(OWNER_ID)
            if user.id != owner_id:
                try:
                    user_info = f"👤 {user.full_name}"
                    if user.username:
                        user_info += f" (@{user.username})"
                    user_info += f"\n🆔 User ID: {user.id}"

                    forward_msg = (
                        "📨 收到私聊消息\n"
                        f"{user_info}\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"{text}\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "💡 如需回复，请手动复制内容发给用户"
                    )
                    await context.bot.send_message(chat_id=owner_id, text=forward_msg)
                except Exception as e:
                    logger.error(f"转发私聊消息失败: {e}")

        return  # 私聊到此结束

    # ========== 群聊逻辑 ==========

    # 确保群组在数据库中有配置
    db.get_group_config(chat_id)

    # ---- 管理员管理 ----
    if text == "显示机器人管理员":
        if not is_bot_admin(user.id):
            return

        admins = db.get_all_admins()
        if not admins:
            await update.message.reply_text("👥 当前没有设置机器人管理员")
            return

        lines = ["👥 机器人管理员列表：\n"]
        for admin in admins:
            name = admin.get("first_name", "Unknown")
            username = admin.get("username") or "N/A"
            uid = admin["user_id"]
            is_owner = admin.get("is_owner", False)
            mark = " 🔱" if is_owner else ""
            lines.append(f"• {name} (@{username}){mark}")
            lines.append(f"  ID: {uid}")

        await update.message.reply_text("\n".join(lines))
        return

    if text in ["设置机器人管理员", "添加机器人管理员"]:
        if not is_bot_admin(user.id):
            return

        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要设置为管理员的用户消息")
            return

        target = update.message.reply_to_message.from_user
        db.add_admin(
            target.id,
            target.username,
            target.first_name,
            is_owner=False,
        )
        await update.message.reply_text(
            f"✅ 已将 {target.first_name} 设置为机器人管理员\n🆔 User ID: {target.id}"
        )
        return

    if text in ["删除机器人管理员", "移除机器人管理员"]:
        if not is_bot_admin(user.id):
            return

        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要删除的管理员消息")
            return

        target = update.message.reply_to_message.from_user
        db.remove_admin(target.id)
        await update.message.reply_text(
            f"✅ 已移除 {target.first_name} 的管理员权限"
        )
        return

    # ---- 撤销操作 ----
    if text == "撤销":
        if not is_bot_admin(user.id):
            return

        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要撤销的账单消息")
            return

        target_msg_id = update.message.reply_to_message.message_id
        deleted = db.delete_transaction_by_message_id(target_msg_id)

        if deleted:
            await update.message.reply_text(
                "✅ 已撤销交易\n"
                f"类型: {deleted['transaction_type']}\n"
                f"金额: {deleted['amount']}\n"
                f"USDT: {deleted['usdt']}"
            )
            await send_summary_with_button(update, chat_id, user.id)
        else:
            await update.message.reply_text("❌ 未找到该消息对应的交易记录")
        return

    # ---- 重置默认值 ----
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
            "📥 入金设置：\n"
            "  • 费率：10%\n"
            "  • 汇率：153\n\n"
            "📤 出金设置：\n"
            "  • 费率：2%\n"
            "  • 汇率：137"
        )
        return

    # ---- 清除今日数据 ----
    if text == "清除数据":
        if not is_bot_admin(user.id):
            return

        stats = db.clear_today_transactions(chat_id)

        in_count = stats.get("in", {}).get("count", 0)
        in_usdt = stats.get("in", {}).get("usdt", 0.0)
        out_count = stats.get("out", {}).get("count", 0)
        out_usdt = stats.get("out", {}).get("usdt", 0.0)
        send_count = stats.get("send", {}).get("count", 0)
        send_usdt = stats.get("send", {}).get("usdt", 0.0)

        total_cleared = in_count + out_count + send_count

        if total_cleared == 0:
            await update.message.reply_text(
                "ℹ️ 今日00:00之后暂无数据\n📊 无需清除"
            )
        else:
            lines = [
                "✅ 已清除今日数据（00:00至现在）\n",
               
