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
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
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

def trunc2(x: float) -> float:
    """截断到小数点后两位（用于入金计算）"""
    rounded = round(x, 6)
    return math.floor(rounded * 100.0) / 100.0

def round2(x: float) -> float:
    """四舍五入到小数点后两位（用于出金计算）"""
    return round(x, 2)

def fmt_usdt(x: float) -> str:
    return f"{x:.2f} USDT"

def to_superscript(num: int) -> str:
    """将数字转换为上标"""
    superscript_map = {
        '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
        '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
        '-': '⁻'
    }
    return ''.join(superscript_map.get(c, c) for c in str(num))

def now_ts():
    """当前时间（北京时间 HH:MM）"""
    import pytz
    beijing_tz = pytz.timezone('Asia/Shanghai')
    return datetime.now(beijing_tz).strftime("%H:%M")

def today_str():
    """当前日期（北京时间 YYYY-MM-DD）"""
    import pytz
    beijing_tz = pytz.timezone('Asia/Shanghai')
    return datetime.now(beijing_tz).strftime("%Y-%m-%d")

def log_path(chat_id: int, country: str = None, date_str: str = None) -> Path:
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
    """解析金额和国家"""
    m = re.match(r"^[\+\-]\s*([0-9]+(?:\.[0-9]+)?)", text.strip())
    if not m:
        return None, None
    amount = float(m.group(1))
    m2 = re.search(r"/\s*([^\s]+)$", text)
    country = m2.group(1) if m2 else '通用'
    return amount, country

def is_bot_admin(user_id: int) -> bool:
    """检查是否为机器人管理员"""
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
        SESSION_SECRET.encode(),
        data.encode(),
        hashlib.sha256
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
            SESSION_SECRET.encode(),
            data.encode(),
            hashlib.sha256
        ).hexdigest()
        
        if signature != expected_signature:
            return None
        
        if datetime.now().timestamp() > expires_at:
            return None
        
        return {"chat_id": chat_id, "user_id": user_id}
    except:
        return None

def login_required(f):
    """登录验证装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.args.get('token') or session.get('token')
        if not token:
            return "未授权访问", 403
        
        user_info = verify_token(token)
        if not user_info:
            return "Token无效或已过期", 403
        
        session['token'] = token
        session['user_info'] = user_info
        
        return f(*args, **kwargs)
    return decorated_function

def generate_web_url(chat_id: int, user_id: int):
    """生成Web查账访问URL"""
    if not SESSION_SECRET:
        return None
    
    token = generate_web_token(chat_id, user_id)
    return f"{WEB_BASE_URL}/dashboard?token={token}"

# ========== Telegram Bot处理函数 ==========

def render_group_summary(chat_id: int) -> str:
    """渲染群组账单汇总"""
    config = db.get_group_config(chat_id)
    summary = db.get_transactions_summary(chat_id)
    
    bot_name = config.get('group_name', 'AA全球国际支付')
    in_records = summary['in_records']
    out_records = summary['out_records']
    send_records = summary['send_records']
    
    should = trunc2(summary['should_send'])
    sent = trunc2(summary['send_usdt'])
    diff = trunc2(should - sent)
    
    rin = config.get('in_rate', 0)
    fin = config.get('in_fx', 0)
    rout = config.get('out_rate', 0)
    fout = config.get('out_fx', 0)
    
    lines = []
    lines.append(f"📊【{bot_name} 账单汇总】\n")
    
    # 入金记录（最多显示5条）
    lines.append(f"已入账 ({len(in_records)}笔)")
    for r in in_records[:5]:
        raw = float(r['amount'])
        fx = float(r['fx'])
        rate = float(r['rate'])
        usdt = trunc2(float(r['usdt']))
        ts = r['timestamp']
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw}  {rate_sup}/ {fx} = {usdt}")
    
    lines.append("")
    
    # 出金记录（最多显示5条）
    lines.append(f"已出账 ({len(out_records)}笔)")
    for r in out_records[:5]:
        raw = float(r['amount'])
        fx = float(r['fx'])
        rate = float(r['rate'])
        usdt = round2(float(r['usdt']))
        ts = r['timestamp']
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw}  {rate_sup}/ {fx} = {usdt}")
    
    lines.append("")
    
    # 下发记录（只有当有下发记录时才显示，最多显示5条）
    if send_records:
        lines.append(f"已下发 ({len(send_records)}笔)")
        for r in send_records[:5]:
            usdt = round2(abs(float(r['usdt'])))
            ts = r['timestamp']
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
    
    bot_name = config.get('group_name', 'AA全球国际支付')
    in_records = summary['in_records']
    out_records = summary['out_records']
    send_records = summary['send_records']
    
    should = trunc2(summary['should_send'])
    sent = trunc2(summary['send_usdt'])
    diff = trunc2(should - sent)
    
    rin = config.get('in_rate', 0)
    fin = config.get('in_fx', 0)
    rout = config.get('out_rate', 0)
    fout = config.get('out_fx', 0)
    
    lines = []
    lines.append(f"📊【{bot_name} 完整账单】\n")
    
    # 所有入金记录
    lines.append(f"已入账 ({len(in_records)}笔)")
    for r in in_records:
        raw = float(r['amount'])
        fx = float(r['fx'])
        rate = float(r['rate'])
        usdt = trunc2(float(r['usdt']))
        ts = r['timestamp']
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw}  {rate_sup}/ {fx} = {usdt}")
    
    lines.append("")
    
    # 所有出金记录
    lines.append(f"已出账 ({len(out_records)}笔)")
    for r in out_records:
        raw = float(r['amount'])
        fx = float(r['fx'])
        rate = float(r['rate'])
        usdt = round2(float(r['usdt']))
        ts = r['timestamp']
        rate_percent = int(rate * 100)
        rate_sup = to_superscript(rate_percent)
        lines.append(f"{ts} {raw}  {rate_sup}/ {fx} = {usdt}")
    
    lines.append("")
    
    # 所有下发记录
    if send_records:
        lines.append(f"已下发 ({len(send_records)}笔)")
        for r in send_records:
            usdt = round2(abs(float(r['usdt'])))
            ts = r['timestamp']
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
    """处理/start命令"""
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
        '  （必须准确输入"撤销"二字）\n\n'
        "⚙️ 快速设置（仅管理员）：\n"
        "  重置默认值（一键设置推荐费率/汇率）\n"
        "  清除数据（清除今日00:00至现在的所有数据）\n"
        "  设置入金费率 10\n"
        "  设置入金汇率 153\n"
        "  设置出金费率 -2\n"
        "  设置出金汇率 137\n\n"
        "👥 管理员管理：\n"
        "  设置机器人管理员（回复消息）\n"
        "  删除机器人管理员（回复消息）\n"
        "  显示机器人管理员"
    )
    
    # 记录私聊用户
    if chat.type == "private":
        db.add_private_chat_user(
            user.id,
            user.username,
            user.first_name
        )
    
    await update.message.reply_text(help_text)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息"""
    user = update.effective_user
    chat = update.effective_chat
    chat_id = chat.id
    text = (update.message.text or update.message.caption or "").strip()
    ts = now_ts()
    dstr = today_str()
    
    # 私聊消息处理
    if chat.type == "private":
        db.add_private_chat_user(user.id, user.username, user.first_name)
        
        # 记录私聊日志
        private_log_dir = LOG_DIR / "private_chats"
        private_log_dir.mkdir(exist_ok=True)
        user_log_file = private_log_dir / f"user_{user.id}.log"
        
        log_entry = f"[{ts}] {user.full_name} (@{user.username or 'N/A'}): {text}\n"
        with open(user_log_file, "a", encoding="utf-8") as f:
            f.write(log_entry)
        
        # OWNER专属功能
        if OWNER_ID and OWNER_ID.isdigit() and user.id == int(OWNER_ID):
            # 广播功能
            if text.startswith("广播 ") or text.startswith("群发 "):
                broadcast_text = text.split(" ", 1)[1] if len(text.split(" ", 1)) > 1 else ""
                
                if not broadcast_text:
                    await update.message.reply_text(
                        "❌ 请输入广播内容\n\n"
                        "使用方法：\n"
                        "广播 您的消息内容"
                    )
                    return
                
                # 获取所有私聊用户
                users = db.get_all_private_chat_users()
                
                success = 0
                failed = 0
                
                await update.message.reply_text(
                    f"📢 开始广播...\n"
                    f"目标用户数：{len(users)}"
                )
                
                for user_data in users:
                    target_user_id = user_data['user_id']
                    
                    # 不给自己发送
                    if target_user_id == int(OWNER_ID):
                        continue
                    
                    try:
                        await context.bot.send_message(
                            chat_id=target_user_id,
                            text=f"📢 系统通知：\n\n{broadcast_text}"
                        )
                        success += 1
                    except Exception as e:
                        logger.error(f"广播失败 (用户 {target_user_id}): {e}")
                        failed += 1
                
                await update.message.reply_text(
                    f"✅ 广播完成！\n\n"
                    f"成功：{success} 人\n"
                    f"失败：{failed} 人"
                )
                return
            
            # 显示帮助
            if text in ["help", "帮助", "功能"]:
                await update.message.reply_text(
                    "👑 OWNER专属功能：\n\n"
                    "📢 广播功能：\n"
                    "• 广播 您的消息内容\n"
                    "• 群发 您的消息内容\n\n"
                    "💬 使用说明：\n"
                    "• 回复任意私聊用户的消息可直接回复\n"
                    "• 广播会发送给所有私聊过的用户"
                )
                return
        
        # 转发给OWNER
        if OWNER_ID and OWNER_ID.isdigit():
            owner_id = int(OWNER_ID)
            if user.id != owner_id:
                try:
                    user_info = f"👤 {user.full_name}"
                    if user.username:
                        user_info += f" (@{user.username})"
                    user_info += f"\n🆔 User ID: {user.id}"
                    
                    forward_msg = (
                        f"📨 收到私聊消息\n"
                        f"{user_info}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"{text}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"💡 回复此消息可直接回复用户"
                    )
                    
                    await context.bot.send_message(chat_id=owner_id, text=forward_msg)
                except Exception as e:
                    logger.error(f"转发私聊消息失败: {e}")
        
        return
    
    # 群聊消息处理
    # 确保群组存在于数据库
    db.get_group_config(chat_id)
    
    # 管理员管理
    if text == "显示机器人管理员":
        if not is_bot_admin(user.id):
            return
        
        admins = db.get_all_admins()
        if not admins:
            await update.message.reply_text("👥 当前没有设置机器人管理员")
            return
        
        lines = ["👥 机器人管理员列表：\n"]
        for admin in admins:
            name = admin.get('first_name', 'Unknown')
            username = admin.get('username', '')
            user_id = admin['user_id']
            is_owner = admin.get('is_owner', False)
            
            status = " 🔱" if is_owner else ""
            lines.append(f"• {name} (@{username or 'N/A'}){status}")
            lines.append(f"  ID: {user_id}")
        
        await update.message.reply_text("\n".join(lines))
        return
    
    if text in ["设置机器人管理员", "添加机器人管理员"]:
        if not is_bot_admin(user.id):
            return
        
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要设置为管理员的用户消息")
            return
        
        target_user = update.message.reply_to_message.from_user
        db.add_admin(
            target_user.id,
            target_user.username,
            target_user.first_name,
            is_owner=False
        )
        
        await update.message.reply_text(
            f"✅ 已将 {target_user.first_name} 设置为机器人管理员\n"
            f"🆔 User ID: {target_user.id}"
        )
        return
    
    if text in ["删除机器人管理员", "移除机器人管理员"]:
        if not is_bot_admin(user.id):
            return
        
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ 请回复要删除的管理员消息")
            return
        
        target_user = update.message.reply_to_message.from_user
        db.remove_admin(target_user.id)
        
        await update.message.reply_text(
            f"✅ 已移除 {target_user.first_name} 的管理员权限"
        )
        return
    
    # 撤销功能
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
            out_fx=137
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
    
    # 清除数据
    if text == "清除数据":
        if not is_bot_admin(user.id):
            return
        
        stats = db.clear_today_transactions(chat_id)
        
        in_count = stats.get('in', {}).get('count', 0)
        in_usdt = stats.get('in', {}).get('usdt', 0)
        out_count = stats.get('out', {}).get('count', 0)
        out_usdt = stats.get('out', {}).get('usdt', 0)
        send_count = stats.get('send', {}).get('count', 0)
        send_usdt = stats.get('send', {}).get('usdt', 0)
        
        total_cleared = in_count + out_count + send_count
        
        if total_cleared == 0:
            await update.message.reply_text(
                "ℹ️ 今日00:00之后暂无数据\n"
                "📊 无需清除"
            )
        else:
            lines = [
                "✅ 已清除今日数据（00:00至现在）\n",
                f"📥 已入账：清除 {in_count} 笔 ({in_usdt:.2f} USDT)",
                f"📤 已出账：清除 {out_count} 笔 ({out_usdt:.2f} USDT)",
                f"💰 已下发：清除 {send_count} 笔 ({send_usdt:.2f} USDT)",
            ]
            await update.message.reply_text("\n".join(lines))
        
        await send_summary_with_button(update, chat_id, user.id)
        return
    
    # 设置费率/汇率
    if text.startswith(("设置入金费率", "设置入金汇率", "设置出金费率", "设置出金汇率")):
        if not is_bot_admin(user.id):
            return
        
        try:
            config = db.get_group_config(chat_id)
            
            if "入金费率" in text:
                val = float(text.replace("设置入金费率", "").strip()) / 100.0
                db.update_group_config(chat_id, in_rate=val)
                await update.message.reply_text(f"✅ 已设置默认入金费率\n📊 新值：{val*100:.0f}%")
            
            elif "入金汇率" in text:
                val = float(text.replace("设置入金汇率", "").strip())
                db.update_group_config(chat_id, in_fx=val)
                await update.message.reply_text(f"✅ 已设置默认入金汇率\n📊 新值：{val}")
            
            elif "出金费率" in text:
                val = float(text.replace("设置出金费率", "").strip()) / 100.0
                db.update_group_config(chat_id, out_rate=val)
                await update.message.reply_text(f"✅ 已设置默认出金费率\n📊 新值：{val*100:.0f}%")
            
            elif "出金汇率" in text:
                val = float(text.replace("设置出金汇率", "").strip())
                db.update_group_config(chat_id, out_fx=val)
                await update.message.reply_text(f"✅ 已设置默认出金汇率\n📊 新值：{val}")
        
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入有效的数字")
        
        return
    
    # 入金
    if text.startswith("+"):
        if not is_bot_admin(user.id):
            return
        
        amt, country = parse_amount_and_country(text)
        if amt is None:
            return
        
        if amt == 0:
            await send_summary_with_button(update, chat_id, user.id)
            return
        
        config = db.get_group_config(chat_id)
        rate = config.get('in_rate', 0)
        fx = config.get('in_fx', 0)
        
        if fx == 0:
            await update.message.reply_text("⚠️ 请先设置费率和汇率")
            return
        
        usdt = trunc2(amt * (1 - rate) / fx)
        
        txn_id = db.add_transaction(
            chat_id=chat_id,
            transaction_type='in',
            amount=Decimal(str(amt)),
            rate=Decimal(str(rate)),
            fx=Decimal(str(fx)),
            usdt=Decimal(str(usdt)),
            timestamp=ts,
            country=country,
            operator_id=user.id,
            operator_name=user.first_name
        )
        
        append_log(
            log_path(chat_id, country, dstr),
            f"[入金] 时间:{ts} 国家:{country} 原始:{amt} 汇率:{fx} 费率:{rate*100:.2f}% 结果:{usdt}"
        )
        
        msg = await send_summary_with_button(update, chat_id, user.id)
        
        # 保存message_id用于撤销功能
        if msg and txn_id:
            db.update_transaction_message_id(txn_id, msg.message_id)
        
        return
    
    # 出金
    if text.startswith("-"):
        if not is_bot_admin(user.id):
            return
        
        amt, country = parse_amount_and_country(text)
        if amt is None:
            return
        
        config = db.get_group_config(chat_id)
        rate = config.get('out_rate', 0)
        fx = config.get('out_fx', 0)
        
        if fx == 0:
            await update.message.reply_text("⚠️ 请先设置费率和汇率")
            return
        
        usdt = round2(amt * (1 + rate) / fx)
        
        txn_id = db.add_transaction(
            chat_id=chat_id,
            transaction_type='out',
            amount=Decimal(str(amt)),
            rate=Decimal(str(rate)),
            fx=Decimal(str(fx)),
            usdt=Decimal(str(usdt)),
            timestamp=ts,
            country=country,
            operator_id=user.id,
            operator_name=user.first_name
        )
        
        append_log(
            log_path(chat_id, country, dstr),
            f"[出金] 时间:{ts} 国家:{country} 原始:{amt} 汇率:{fx} 费率:{rate*100:.2f}% 下发:{usdt}"
        )
        
        msg = await send_summary_with_button(update, chat_id, user.id)
        
        # 保存message_id用于撤销功能
        if msg and txn_id:
            db.update_transaction_message_id(txn_id, msg.message_id)
        
        return
    
    # 下发USDT
    if text.startswith("下发"):
        if not is_bot_admin(user.id):
            return
        
        try:
            usdt_str = text.replace("下发", "").strip()
            usdt = float(usdt_str)
            
            txn_id = db.add_transaction(
                chat_id=chat_id,
                transaction_type='send',
                amount=Decimal(str(abs(usdt))),
                rate=Decimal('0'),
                fx=Decimal('0'),
                usdt=Decimal(str(abs(usdt))),
                timestamp=ts,
                country='通用',
                operator_id=user.id,
                operator_name=user.first_name
            )
            
            if usdt > 0:
                append_log(log_path(chat_id, None, dstr), f"[下发USDT] 时间:{ts} 金额:{usdt} USDT")
            else:
                append_log(log_path(chat_id, None, dstr), f"[撤销下发] 时间:{ts} 金额:{abs(usdt)} USDT")
            
            msg = await send_summary_with_button(update, chat_id, user.id)
            
            # 保存message_id用于撤销功能
            if msg and txn_id:
                db.update_transaction_message_id(txn_id, msg.message_id)
        
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入有效的数字\n例如：下发35.04 或 下发-35.04")
        
        return
    
    # 查看更多记录
    if text in ["更多记录", "查看更多记录", "更多账单", "显示历史账单"]:
        await update.message.reply_text(render_full_summary(chat_id))
        return

# ========== Flask路由 ==========

@app.route("/")
def index():
    """首页"""
    return "Telegram Bot + Web Dashboard - 运行中", 200

@app.route("/health")
def health():
    """健康检查"""
    return "OK", 200

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    """Telegram Webhook处理"""
    try:
        update_data = request.get_json(force=True)
        update = Update.de_json(update_data, telegram_app.bot)
        
        # 在bot的事件循环中异步处理更新
        if bot_loop and telegram_app:
            asyncio.run_coroutine_threadsafe(
                telegram_app.process_update(update),
                bot_loop
            )
        
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook处理错误: {e}")
        return "Error", 500

@app.route("/dashboard")
@login_required
def dashboard():
    """Web查账Dashboard"""
    user_info = session.get('user_info')
    chat_id = user_info['chat_id']
    user_id = user_info['user_id']
    
    config = db.get_group_config(chat_id)
    
    display_config = {
        "deposit_fee_rate": config.get('in_rate', 0) * 100,
        "deposit_fx": config.get('in_fx', 0),
        "withdrawal_fee_rate": config.get('out_rate', 0) * 100,
        "withdrawal_fx": config.get('out_fx', 0)
    }
    
    is_owner = False
    if OWNER_ID and OWNER_ID.isdigit():
        is_owner = (user_id == int(OWNER_ID))
    
    return render_template(
        "dashboard.html",
        chat_id=chat_id,
        user_id=user_id,
        is_owner=is_owner,
        config=display_config
    )

@app.route("/api/transactions")
@login_required
def api_transactions():
    """获取交易记录API"""
    user_info = session.get('user_info')
    chat_id = user_info['chat_id']
    
    # 获取日期筛选参数
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    
    # 获取今日交易（暂时只支持今日，可扩展）
    transactions = db.get_today_transactions(chat_id)
    
    # 转换格式以适配前端
    records = []
    for txn in transactions:
        record = {
            "time": txn['timestamp'],
            "type": {
                "in": "deposit",
                "out": "withdrawal",
                "send": "disbursement"
            }.get(txn['transaction_type'], "unknown"),
            "amount": float(txn['amount']),
            "fee_rate": float(txn['rate']) * 100,
            "exchange_rate": float(txn['fx']),
            "usdt": float(txn['usdt']),
            "operator": txn.get('operator_name', '未知'),
            "message_id": txn.get('message_id'),
            "timestamp": txn['created_at'].timestamp() if txn.get('created_at') else 0
        }
        records.append(record)
    
    # 计算统计数据
    stats = {
        "total_deposit": sum(r['amount'] for r in records if r['type'] == 'deposit'),
        "total_deposit_usdt": sum(r['usdt'] for r in records if r['type'] == 'deposit'),
        "total_withdrawal": sum(r['amount'] for r in records if r['type'] == 'withdrawal'),
        "total_withdrawal_usdt": sum(r['usdt'] for r in records if r['type'] == 'withdrawal'),
        "total_disbursement": sum(r['usdt'] for r in records if r['type'] == 'disbursement'),
        "pending_disbursement": 0,
        "by_operator": {}
    }
    
    stats["pending_disbursement"] = stats["total_deposit_usdt"] - stats["total_withdrawal_usdt"] - stats["total_disbursement"]
    
    # 按操作员统计
    for record in records:
        operator = record['operator']
        if operator not in stats["by_operator"]:
            stats["by_operator"][operator] = {
                "deposit_count": 0,
                "deposit_usdt": 0,
                "withdrawal_count": 0,
                "withdrawal_usdt": 0,
                "disbursement_count": 0,
                "disbursement_usdt": 0
            }
        
        if record['type'] == 'deposit':
            stats["by_operator"][operator]["deposit_count"] += 1
            stats["by_operator"][operator]["deposit_usdt"] += record['usdt']
        elif record['type'] == 'withdrawal':
            stats["by_operator"][operator]["withdrawal_count"] += 1
            stats["by_operator"][operator]["withdrawal_usdt"] += record['usdt']
        elif record['type'] == 'disbursement':
            stats["by_operator"][operator]["disbursement_count"] += 1
            stats["by_operator"][operator]["disbursement_usdt"] += record['usdt']
    
    return jsonify({
        "success": True,
        "records": records,
        "statistics": stats
    })

@app.route("/api/rollback", methods=["POST"])
@login_required
def api_rollback():
    """回退交易API（仅OWNER）"""
    user_info = session.get('user_info')
    user_id = user_info['user_id']
    
    # 仅OWNER可以回退
    is_owner = False
    if OWNER_ID and OWNER_ID.isdigit():
        is_owner = (user_id == int(OWNER_ID))
    
    if not is_owner:
        return jsonify({"success": False, "error": "无权限"}), 403
    
    data = request.json
    message_id = data.get("message_id")
    
    if not message_id:
        return jsonify({"success": False, "error": "参数错误"}), 400
    
    deleted = db.delete_transaction_by_message_id(message_id)
    
    if deleted:
        return jsonify({"success": True, "message": "交易已回退"})
    else:
        return jsonify({"success": False, "error": "未找到该交易记录"}), 404

# ========== 初始化 ==========

async def setup_telegram_bot():
    """设置Telegram Bot（Webhook模式）"""
    global telegram_app
    
    logger.info("🤖 初始化 Telegram Bot Application...")
    
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    
    # 注册命令处理器
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
        handle_text
    ))
    
    # 初始化bot
    await telegram_app.initialize()
    
    # 设置webhook
    if WEBHOOK_URL:
        webhook_path = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
        logger.info(f"🔗 设置 Webhook: {webhook_path}")
        await telegram_app.bot.set_webhook(url=webhook_path)
        logger.info("✅ Webhook已设置")
    else:
        logger.warning("⚠️  WEBHOOK_URL未设置，需要手动设置webhook")
    
    logger.info("✅ Telegram Bot 初始化完成")
    
    return telegram_app

def init_app():
    """初始化应用"""
    logger.info("=" * 50)
    logger.info("🚀 启动 Telegram Bot + Web Dashboard")
    logger.info("=" * 50)
    
    # 初始化数据库
    try:
        db.init_database()
        logger.info("✅ 数据库初始化完成")
    except Exception as e:
        logger.error(f"❌ 数据库初始化失败: {e}")
        raise
    
    # 初始化OWNER为管理员
    if OWNER_ID and OWNER_ID.isdigit():
        db.add_admin(int(OWNER_ID), None, "Owner", is_owner=True)
        logger.info(f"✅ OWNER已设置为管理员: {OWNER_ID}")
    
    logger.info("=" * 50)
    logger.info("✅ 应用初始化完成")
    logger.info("=" * 50)

# ========== 在单独线程中运行事件循环 ==========

import threading

bot_loop = None
bot_thread = None

def run_bot_loop():
    """在单独线程中运行bot的事件循环"""
    global bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    
    try:
        bot_loop.run_until_complete(setup_telegram_bot())
        bot_loop.run_forever()
    except Exception as e:
        logger.error(f"Bot事件循环错误: {e}")
    finally:
        bot_loop.close()

# ========== 运行 ==========

if __name__ == "__main__":
    # 初始化应用
    init_app()
    
    # 在单独线程中启动bot事件循环
    logger.info("🔄 启动Bot事件循环线程...")
    bot_thread = threading.Thread(target=run_bot_loop, daemon=True)
    bot_thread.start()
    
    # 等待bot初始化
    import time
    time.sleep(2)
    
    # 启动Flask应用
    port = int(os.getenv("PORT", "5000"))
    logger.info(f"🌐 Flask应用启动在端口: {port}")
    
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False  # 禁用reloader避免双重启动
    )
