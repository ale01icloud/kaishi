#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一Flask应用 - Telegram Bot Webhook + Web Dashboard
整合所有功能，使用PostgreSQL数据库
"""

import os
import re
import hmac
import hashlib
import math
import logging
from datetime import datetime, timedelta
from pathlib import Path
from decimal import Decimal
from functools import wraps

from flask import Flask, render_template, request, jsonify, session
from dotenv import load_dotenv
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import database as db

# ========== 基础配置 ==========

load_dotenv()

app = Flask(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
SESSION_SECRET = os.getenv("SESSION_SECRET")
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # 例如: https://xxx.clawcloudrun.com

if not BOT_TOKEN:
    raise RuntimeError("❌ 错误：未找到 TELEGRAM_BOT_TOKEN 环境变量")

if not SESSION_SECRET:
    print("⚠️  警告：SESSION_SECRET 未设置，Web查账功能将不可用")
    SESSION_SECRET = None

app.secret_key = SESSION_SECRET or os.urandom(24)

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

# ========== 通用工具函数 ==========


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


def now_ts() -> str:
    """当前时间（北京时间 HH:MM）"""
    import pytz

    beijing_tz = pytz.timezone("Asia/Shanghai")
    return datetime.now(beijing_tz).strftime("%H:%M")


def today_str() -> str:
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
    """解析金额和国家，格式：+10000 或 -10000 / 日本"""
    m = re.match(r"^[\+\-]\s*([0-9]+(?:\.[0-9]+)?)", text.strip())
    if not m:
        return None, None
    amount = float(m.group(1))
    m2 = re.search(r"/\s*([^\s]+)$", text)
    country = m2.group(1) if m2 else "通用"
    return amount, country


def is_bot_admin(user_id: int) -> bool:
    """检查是否为机器人管理员（OWNER 永远是管理员）"""
    if OWNER_ID and OWNER_ID.isdigit() and int(OWNER_ID) == user_id:
        return True
    return db.is_admin(user_id)


# ========== Web Token 相关 ==========


def generate_web_token(chat_id: int, user_id: int, expires_hours: int = 24):
    if not SESSION_SECRET:
        return None
    expires_at = int((datetime.now() + timedelta(hours=expires_hours)).timestamp())
    data = f"{chat_id}:{user_id}:{expires_at}"
    signature = hmac.new(
        SESSION_SECRET.encode(), data.encode(), hashlib.sha256
    ).hexdigest()
    return f"{data}:{signature}"


def verify_token(token: str):
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
        expected = hmac.new(
            SESSION_SECRET.encode(), data.encode(), hashlib.sha256
        ).hexdigest()
        if signature != expected:
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
            return "Token无效或已过期", 403
        session["token"] = token
        session["user_info"] = info
        return f(*args, **kwargs)

    return wrapper


def generate_web_url(chat_id: int, user_id: int):
    if not SESSION_SECRET:
        return None
    token = generate_web_token(chat_id, user_id)
    return f"{WEB_BASE_URL}/dashboard?token={token}"


# ========== 账单渲染 ==========


def render_group_summary(chat_id: int) -> str:
    config = db.get_group_config(chat_id)
    summary = db.get_transactions_summary(chat_id)

    name = config.get("group_name", "AA全球国际支付")
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
    lines.append(f"📊【{name} 账单汇总】\n")

    # 入金
    lines.append(f"已入账 ({len(in_records)}笔)")
    for r in in_records[:5]:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = trunc2(float(r["usdt"]))
        ts = r["timestamp"]
        rate_percent = int(rate * 100)
        sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw}  {sup}/ {fx} = {usdt}")
    lines.append("")

    # 出金
    lines.append(f"已出账 ({len(out_records)}笔)")
    for r in out_records[:5]:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = round2(float(r["usdt"]))
        ts = r["timestamp"]
        rate_percent = int(rate * 100)
        sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw}  {sup}/ {fx} = {usdt}")
    lines.append("")

    # 下发
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
    config = db.get_group_config(chat_id)
    summary = db.get_transactions_summary(chat_id)

    name = config.get("group_name", "AA全球国际支付")
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
    lines.append(f"📊【{name} 完整账单】\n")

    lines.append(f"已入账 ({len(in_records)}笔)")
    for r in in_records:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = trunc2(float(r["usdt"]))
        ts = r["timestamp"]
        sup = to_superscript(int(rate * 100))
        lines.append(f"{ts} {raw}  {sup}/ {fx} = {usdt}")
    lines.append("")

    lines.append(f"已出账 ({len(out_records)}笔)")
    for r in out_records:
        raw = float(r["amount"])
        fx = float(r["fx"])
        rate = float(r["rate"])
        usdt = round2(float(r["usdt"]))
        ts = r["timestamp"]
        sup = to_superscript(int(rate * 100))
        lines.append(f"{ts} {raw}  {sup}/ {fx} = {usdt}")
    lines.append("")

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
    text = render_group_summary(chat_id)
    if SESSION_SECRET:
        url = generate_web_url(chat_id, user_id)
        if url:
            keyboard = [[InlineKeyboardButton("📊 查看账单明细", url=url)]]
            markup = InlineKeyboardMarkup(keyboard)
            msg = await update.message.reply_text(text, reply_markup=markup)
        else:
            msg = await update.message.reply_text(text)
    else:
        msg = await update.message.reply_text(text)
    return msg


# ========== Telegram 命令 / 文本处理 ==========


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "  回复账单消息 + 输入：撤销\n\n"
        "⚙️ 快速设置（仅管理员）：\n"
        "  重置默认值\n"
        "  清除数据\n"
        "  设置入金费率 10\n"
        "  设置入金汇率 153\n"
        "  设置出金费率 2\n"
        "  设置出金汇率 137\n\n"
        "👥 管理员管理：\n"
        "  设置机器人管理员（回复消息）\n"
        "  删除机器人管理员（回复消息）\n"
        "  显示机器人管理员"
    )

    if chat.type == "private":
        db.add_private_chat_user(user.id, user.username, user.first_name)

    await update.message.reply_text(help_text)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    chat_id = chat.id
    text = (update.message.text or update.message.caption or "").strip()
    ts = now_ts()
    dstr = today_str()

    # ========= 私聊 =========
    if chat.type == "private":
        db.add_private_chat_user(user.id, user.username, user.first_name)

        # 写私聊日志
        private_dir = LOG_DIR / "private_chats"
        private_dir.mkdir(exist_ok=True)
        log_file = private_dir / f"user_{user.id}.log"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {user.full_name} (@{user.username or 'N/A'}): {text}\n")

        # OWNER 广播 / 帮助
        if OWNER_ID and OWNER_ID.isdigit() and user.id == int(OWNER_ID):
            if text.startswith(("广播 ", "群发 ")):
                msg = text.split(" ", 1)[1] if " " in text else ""
                if not msg:
                    await update.message.reply_text("❌ 请输入广播内容，例如：广播 今天有新活动")
                    return
                users = db.get_all_private_chat_users()
                success = fail = 0
                await update.message.reply_text(f"📢 开始广播，目标用户：{len(users)}")
                for u in users:
                    uid = u["user_id"]
                    if uid == int(OWNER_ID):
                        continue
                    try:
                        await context.bot.send_message(
                            chat_id=uid, text=f"📢 系统通知：\n\n{msg}"
                        )
                        success += 1
                    except Exception as e:
                        logger.error(f"广播失败 {uid}: {e}")
                        fail += 1
                await update.message.reply_text(
                    f"✅ 广播完成\n成功：{success} 人\n失败：{fail} 人"
                )
                return

            if text in ("help", "帮助", "功能"):
                await update.message.reply_text(
                    "👑 OWNER 专属功能：\n\n"
                    "• 广播 文本内容\n"
                    "• 群发 文本内容\n"
                    "会发送给所有曾经私聊过机器人的用户。"
                )
                return

        # 非 OWNER：把消息转给 OWNER 参考
        if OWNER_ID and OWNER_ID.isdigit() and user.id != int(OWNER_ID):
            try:
                info = f"👤 {user.full_name}"
                if user.username:
                    info += f" (@{user.username})"
                info += f"\n🆔 User ID: {user.id}"
                forward = (
                    "📨 收到用户私聊\n"
                    f"{info}\n"
                    "━━━━━━━━━━━━━━\n"
                    f"{text}\n"
                    "━━━━━━━━━━━━━━\n"
                    "（如需联系，请手动添加并回复）"
                )
                await context.bot.send_message(chat_id=int(OWNER_ID), text=forward)
            except Exception as e:
                logger.error(f"转发给 OWNER 失败: {e}")

        return  # 私聊结束

    # ========= 群聊 =========

    # 确保群配置存在
    db.get_group_config(chat_id)

    # --- 管理员列表 ---
    if text == "显示机器人管理员":
        if not is_bot_admin(user.id):
            return
        admins = db.get_all_admins()
        if not admins:
            await update.message.reply_text("👥 当前没有设置机器人管理员")
            return
        lines = ["👥 机器人管理员列表：\n"]
        for a in admins:
            name = a.get("first_name", "未知")
            username = a.get("username") or "N/A"
            uid = a["user_id"]
            mark = " 🔱" if a.get("is_owner") else ""
            lines.append(f"• {name} (@{username}){mark}")
            lines.append(f"  ID: {uid}")
        await update.message.reply_text("\n".join(lines))
        return

    # --- 设置管理员 ---
    if text in ("设置机器人管理员", "添加机器人管理员"):
        if not is_bot_admin(user.id):
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请【回复】要设置的成员消息再发送本命令")
            return
        target = update.message.reply_to_message.from_user
        db.add_admin(target.id, target.username, target.first_name, is_owner=False)
        await update.message.reply_text(
            f"✅ 已将 {target.first_name} 设置为机器人管理员\n🆔 User ID: {target.id}"
        )
        return

    # --- 删除管理员 ---
    if text in ("删除机器人管理员", "移除机器人管理员"):
        if not is_bot_admin(user.id):
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请【回复】要移除的管理员消息再发送本命令")
            return
        target = update.message.reply_to_message.from_user
        db.remove_admin(target.id)
        await update.message.reply_text(
            f"✅ 已移除 {target.first_name} 的机器人管理员权限"
        )
        return

    # --- 撤销 ---
    if text == "撤销":
        if not is_bot_admin(user.id):
            return
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要撤销的账单消息")
            return
        mid = update.message.reply_to_message.message_id
        deleted = db.delete_transaction_by_message_id(mid)
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

    # --- 重置默认值 ---
    if text == "重置默认值":
        if not is_bot_admin(user.id):
            return
        db.update_group_config(
            chat_id, in_rate=0.10, in_fx=153, out_rate=0.02, out_fx=137
        )
        await update.message.reply_text(
            "✅ 已重置为推荐默认值\n\n"
            "📥 入金：10% / 153\n"
            "📤 出金：2% / 137"
        )
        return

    # --- 清除今日数据 ---
    if text == "清除数据":
        if not is_bot_admin(user.id):
            return
        stats = db.clear_today_transactions(chat_id)
        in_c = stats.get("in", {}).get("count", 0)
        in_u = stats.get("in", {}).get("usdt", 0.0)
        out_c = stats.get("out", {}).get("count", 0)
        out_u = stats.get("out", {}).get("usdt", 0.0)
        send_c = stats.get("send", {}).get("count", 0)
        send_u = stats.get("send", {}).get("usdt", 0.0)
        total = in_c + out_c + send_c

        if total == 0:
            await update.message.reply_text("ℹ️ 今日00:00之后暂无数据，无需清除")
        else:
            lines = [
                "✅ 已清除今日数据（00:00 至现在）\n",
                f"📥 入金：清除 {in_c} 笔（{in_u:.2f} USDT）",
                f"📤 出金：清除 {out_c} 笔（{out_u:.2f} USDT）",
                f"💰 下发：清除 {send_c} 笔（{send_u:.2f} USDT）",
            ]
            await update.message.reply_text("\n".join(lines))

        await send_summary_with_button(update, chat_id, user.id)
        return

    # --- 设置费率 / 汇率 ---
    if text.startswith(
        ("设置入金费率", "设置入金汇率", "设置出金费率", "设置出金汇率")
    ):
        if not is_bot_admin(user.id):
            return
        try:
            cfg = {}
            if "入金费率" in text:
                v = float(text.replace("设置入金费率", "").strip()) / 100.0
                cfg["in_rate"] = v
                msg = f"✅ 已设置默认入金费率为 {v*100:.0f}%"
            elif "入金汇率" in text:
                v = float(text.replace("设置入金汇率", "").strip())
                cfg["in_fx"] = v
                msg = f"✅ 已设置默认入金汇率为 {v}"
            elif "出金费率" in text:
                v = float(text.replace("设置出金费率", "").strip()) / 100.0
                cfg["out_rate"] = v
                msg = f"✅ 已设置默认出金费率为 {v*100:.0f}%"
            else:  # 出金汇率
                v = float(text.replace("设置出金汇率", "").strip())
                cfg["out_fx"] = v
                msg = f"✅ 已设置默认出金汇率为 {v}"
            db.update_group_config(chat_id, **cfg)
            await update.message.reply_text(msg)
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入数字，例如：设置入金费率 10")
        return

    # --- 查看账单（+0 不记录） ---
    if text == "+0":
        await send_summary_with_button(update, chat_id, user.id)
        return

    # --- 入金 ---
    if text.startswith("+"):
        if not is_bot_admin(user.id):
            return
        amt, country = parse_amount_and_country(text)
        if amt is None:
            return

        config = db.get_group_config(chat_id)
        rate = float(config.get("in_rate", 0))
        fx = float(config.get("in_fx", 0))

        if fx == 0:
            await update.message.reply_text("⚠️ 请先设置入金费率和汇率")
            return

        amt_f = float(amt)
        usdt = trunc2(amt_f * (1 - rate) / fx)

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
            f"[入金] 时间:{ts} 国家:{country} 原始:{amt} 汇率:{fx} 费率:{rate*100:.2f}% 结果:{usdt}",
        )

        msg = await send_summary_with_button(update, chat_id, user.id)
        if msg and txn_id:
            db.update_transaction_message_id(txn_id, msg.message_id)
        return

    # --- 出金 ---
    if text.startswith("-"):
        if not is_bot_admin(user.id):
            return
        amt, country = parse_amount_and_country(text)
        if amt is None:
            return

        config = db.get_group_config(chat_id)
        rate = float(config.get("out_rate", 0))
        fx = float(config.get("out_fx", 0))
        if fx == 0:
            await update.message.reply_text("⚠️ 请先设置出金费率和汇率")
            return

        amt_f = float(amt)
        usdt = round2(amt_f * (1 + rate) / fx)

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
            f"[出金] 时间:{ts} 国家:{country} 原始:{amt} 汇率:{fx} 费率:{rate*100:.2f}% 下发:{usdt}",
        )

        msg = await send_summary_with_button(update, chat_id, user.id)
        if msg and txn_id:
            db.update_transaction_message_id(txn_id, msg.message_id)
        return

    # --- 下发 USDT ---
    if text.startswith("下发"):
        if not is_bot_admin(user.id):
            return
        try:
            usdt_str = text.replace("下发", "").strip()
            usdt_val = float(usdt_str)
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入数字，例如：下发35.04 或 下发-35.04")
            return

        txn_id = db.add_transaction(
            chat_id=chat_id,
            transaction_type="send",
            amount=Decimal(str(abs(usdt_val))),
            rate=Decimal("0"),
            fx=Decimal("0"),
            usdt=Decimal(str(abs(usdt_val))),
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

    # --- 查看更多记录 ---
    if text in ("更多记录", "查看更多记录", "更多账单", "显示历史账单"):
        await update.message.reply_text(render_full_summary(chat_id))
        return

    # 其他消息不处理（避免刷屏）
    return


# ========== Flask 路由 ==========

@app.route("/")
def index():
    return "Telegram Bot + Web Dashboard - 运行中", 200


@app.route("/health")
def health():
    return "OK", 200


bot_loop: asyncio.AbstractEventLoop | None = None
bot_thread = None


@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    global telegram_app, bot_loop
    try:
        update_json = request.get_json(force=True)
        update = Update.de_json(update_json, telegram_app.bot)
        if telegram_app and bot_loop:
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
    info = session.get("user_info")
    chat_id = info["chat_id"]
    user_id = info["user_id"]

    config = db.get_group_config(chat_id)
    display_cfg = {
        "deposit_fee_rate": config.get("in_rate", 0) * 100,
        "deposit_fx": config.get("in_fx", 0),
        "withdrawal_fee_rate": config.get("out_rate", 0) * 100,
        "withdrawal_fx": config.get("out_fx", 0),
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
    info = session.get("user_info")
    chat_id = info["chat_id"]

    txns = db.get_today_transactions(chat_id)
    records = []
    for t in txns:
        records.append(
            {
                "time": t["timestamp"],
                "type": {
                    "in": "deposit",
                    "out": "withdrawal",
                    "send": "disbursement",
                }.get(t["transaction_type"], "unknown"),
                "amount": float(t["amount"]),
                "fee_rate": float(t["rate"]) * 100,
                "exchange_rate": float(t["fx"]),
                "usdt": float(t["usdt"]),
                "operator": t.get("operator_name", "未知"),
                "message_id": t.get("message_id"),
                "timestamp": t["created_at"].timestamp()
                if t.get("created_at")
                else 0,
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
        s = stats["by_operator"][op]
        if r["type"] == "deposit":
            s["deposit_count"] += 1
            s["deposit_usdt"] += r["usdt"]
        elif r["type"] == "withdrawal":
            s["withdrawal_count"] += 1
            s["withdrawal_usdt"] += r["usdt"]
        elif r["type"] == "disbursement":
            s["disbursement_count"] += 1
            s["disbursement_usdt"] += r["usdt"]

    return jsonify({"success": True, "records": records, "statistics": stats})


@app.route("/api/rollback", methods=["POST"])
@login_required
def api_rollback():
    info = session.get("user_info")
    uid = info["user_id"]

    if not (OWNER_ID and OWNER_ID.isdigit() and int(OWNER_ID) == uid):
        return jsonify({"success": False, "error": "无权限"}), 403

    data = request.get_json(force=True)
    mid = data.get("message_id")
    if not mid:
        return jsonify({"success": False, "error": "参数错误"}), 400

    deleted = db.delete_transaction_by_message_id(mid)
    if deleted:
        return jsonify({"success": True, "message": "交易已回退"})
    return jsonify({"success": False, "error": "未找到该交易记录"}), 404


# ========== 初始化 & 运行 ==========

async def setup_telegram_bot():
    global telegram_app

    logger.info("🤖 初始化 Telegram Bot Application...")
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_text)
    )

    await telegram_app.initialize()

    if WEBHOOK_URL:
        url = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
        logger.info(f"🔗 设置 Webhook: {url}")
        await telegram_app.bot.set_webhook(url=url)
        logger.info("✅ Webhook 已设置")
    else:
        logger.warning("⚠️ 未设置 WEBHOOK_URL，Webhook 不会生效")

    logger.info("✅ Telegram Bot 初始化完成")


def init_app():
    logger.info("=" * 50)
    logger.info("🚀 启动 Telegram Bot + Web Dashboard")
    logger.info("=" * 50)

    db.init_database()
    logger.info("✅ 数据库初始化完成")

    if OWNER_ID and OWNER_ID.isdigit():
        db.add_admin(int(OWNER_ID), None, "Owner", is_owner=True)
        logger.info(f"✅ OWNER 已设置为管理员: {OWNER_ID}")

    logger.info("✅ 应用初始化完成")
    logger.info("=" * 50)


def run_bot_loop():
    global bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    try:
        bot_loop.run_until_complete(setup_telegram_bot())
        bot_loop.run_forever()
    except Exception as e:
        logger.error(f"Bot 事件循环错误: {e}")
    finally:
        bot_loop.close()


if __name__ == "__main__":
    init_app()

    logger.info("🔄 启动 Bot 事件循环线程...")
    bot_thread = __import__("threading").Thread(target=run_bot_loop, daemon=True)
    bot_thread.start()

    # 给 bot 一点时间初始化
    import time

    time.sleep(2)

    port = int(os.getenv("PORT", "5000"))
    logger.info(f"🌐 Flask 应用启动在端口: {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
