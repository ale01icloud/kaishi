# bot.py
import os, re, threading, json, math, datetime, hmac, hashlib
from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv
import requests
from decimal import Decimal

# ========== 加载环境 ==========
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_ID  = os.getenv("OWNER_ID")  # 可选：你的 Telegram ID（字符串），拥有永久管理员权限

# Web查账系统配置
SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    print("⚠️  警告：SESSION_SECRET未设置，Web查账按钮将不可用")
    print("   如需使用Web查账功能，请设置：export SESSION_SECRET=$(openssl rand -hex 32)")
    SESSION_SECRET = None  # 标记为未配置

# Web应用的基础URL：优先 WEB_BASE_URL，其次 WEBHOOK_URL，最后才是 localhost
WEB_BASE_URL = (
    os.getenv("WEB_BASE_URL")
    or os.getenv("WEBHOOK_URL")
    or "http://localhost:5000"
)

# ========== 记账核心状态（多群组支持）==========
DATA_DIR = Path("./data")
GROUPS_DIR = DATA_DIR / "groups"
LOG_DIR  = DATA_DIR / "logs"
ADMINS_FILE = DATA_DIR / "admins.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
GROUPS_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 群组状态缓存 {chat_id: state_dict}
groups_state = {}

def get_default_state():
    """返回默认群组状态（初始费率/汇率为0，需要管理员设置）"""
    return {
        "defaults": {
            "in":  {"rate": 0, "fx": 0},
            "out": {"rate": 0, "fx": 0},
        },
        "countries": {},
        "precision": {"mode": "truncate", "digits": 2},
        "bot_name": "AA全球国际支付",
        "recent": {"in": [], "out": []},
        "summary": {"should_send_usdt": 0.0, "sent_usdt": 0.0},
        "last_date": ""
    }


def group_file_path(chat_id: int) -> Path:
    """获取群组状态文件路径"""
    return GROUPS_DIR / f"group_{chat_id}.json"

def load_group_state(chat_id: int) -> dict:
    """从JSON文件加载群组状态"""
    # 先检查缓存
    if chat_id in groups_state:
        return groups_state[chat_id]
    
    # 从文件读取
    file_path = group_file_path(chat_id)
    if file_path.exists():
        try:
            with file_path.open("r", encoding="utf-8") as f:
                state = json.load(f)
            
            groups_state[chat_id] = state
            return state
        except Exception as e:
            print(f"⚠️ 加载群组状态文件失败: {e}")
    
    # 创建新群组状态
    state = get_default_state()
    groups_state[chat_id] = state
    save_group_state(chat_id)
    return state

def save_group_state(chat_id: int):
    """保存群组状态到JSON文件"""
    if chat_id not in groups_state:
        return
    
    file_path = group_file_path(chat_id)
    try:
        with file_path.open("w", encoding="utf-8") as f:
            json.dump(groups_state[chat_id], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ 保存群组状态文件失败: {e}")

# 管理员缓存（从JSON文件加载）
admins_cache = None

def load_admins():
    """从JSON文件加载管理员列表"""
    global admins_cache
    if admins_cache is not None:
        return admins_cache
    
    if ADMINS_FILE.exists():
        try:
            with ADMINS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
                admins_cache = data.get("admins", [])
                return admins_cache
        except Exception as e:
            print(f"⚠️ 加载管理员文件失败: {e}")
    
    # 初始化管理员（如果有OWNER_ID）
    admins_cache = []
    if OWNER_ID and OWNER_ID.isdigit():
        admins_cache.append(int(OWNER_ID))
    save_admins(admins_cache)
    return admins_cache

def save_admins(admin_list):
    """保存管理员列表到JSON文件"""
    global admins_cache
    admins_cache = admin_list
    try:
        with ADMINS_FILE.open("w", encoding="utf-8") as f:
            json.dump({"admins": admin_list}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ 保存管理员文件失败: {e}")

def add_admin(user_id: int) -> bool:
    """添加管理员"""
    admins = load_admins()
    if user_id not in admins:
        admins.append(user_id)
        save_admins(admins)
        return True
    return False

def remove_admin(user_id: int) -> bool:
    """移除管理员"""
    admins = load_admins()
    if user_id in admins:
        admins.remove(user_id)
        save_admins(admins)
        return True
    return False


# ========== 工具函数 ==========
def trunc2(x) -> float:
    """截断到小数点后两位（用于入金计算），兼容 float / Decimal"""
    # 统一先转成 float，避免 Decimal 和 float 混合运算报错
    x = float(x)
    rounded = round(x, 6)
    return math.floor(rounded * 100.0) / 100.0

def round2(x) -> float:
    """四舍五入到小数点后两位（用于出金 / 下发计算），兼容 float / Decimal"""
    x = float(x)
    return round(x, 2)


def fmt_usdt(x: float) -> str:
    return f"{x:.2f} USDT"

def to_superscript(num: int) -> str:
    """将数字转换为上标，用于显示费率"""
    superscript_map = {
        '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
        '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
        '-': '⁻'
    }
    return ''.join(superscript_map.get(c, c) for c in str(num))

def now_ts():
    # 使用北京时间（UTC+8）
    beijing_tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(beijing_tz).strftime("%H:%M")

def today_str():
    # 使用北京时间（UTC+8）
    beijing_tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(beijing_tz).strftime("%Y-%m-%d")

def check_and_reset_daily(chat_id: int):
    """检查日期，如果日期变了（过了0点），清空账单"""
    state = load_group_state(chat_id)
    current_date = today_str()
    last_date = state.get("last_date", "")
    
    if last_date and last_date != current_date:
        # 日期变了，清空账单
        state["recent"]["in"] = []
        state["recent"]["out"] = []
        state["summary"]["should_send_usdt"] = 0.0
        state["summary"]["sent_usdt"] = 0.0
        state["last_date"] = current_date
        save_group_state(chat_id)
        return True  # 返回True表示已重置
    elif not last_date:
        # 首次运行，设置日期
        state["last_date"] = current_date
        save_group_state(chat_id)
    
    return False  # 返回False表示未重置

def log_path(chat_id: int, country: str|None, date_str: str) -> Path:
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

def push_recent(chat_id: int, kind: str, item: dict):
    state = load_group_state(chat_id)
    arr = state["recent"][kind]
    arr.insert(0, item)
    save_group_state(chat_id)

def resolve_params(chat_id: int, direction: str, country: str|None) -> dict:
    state = load_group_state(chat_id)
    d = {"rate": None, "fx": None}
    countries = state["countries"]
    
    # 如果指定了国家，先查找该国家的专属设置
    if country and country in countries:
        if direction in countries[country]:
            d["rate"] = countries[country][direction].get("rate", None)
            d["fx"]   = countries[country][direction].get("fx", None)
    
    # 如果没有找到，使用默认值（不再回退到入金设置）
    if d["rate"] is None: 
        d["rate"] = state["defaults"][direction]["rate"]
    if d["fx"] is None: 
        d["fx"] = state["defaults"][direction]["fx"]
    
    return d

def parse_amount_and_country(text: str):
    m = re.match(r"^[\+\-]\s*([0-9]+(?:\.[0-9]+)?)", text.strip())
    if not m: return None, None
    amount = float(m.group(1))
    m2 = re.search(r"/\s*([^\s]+)$", text)
    country = m2.group(1) if m2 else None
    return amount, country

# ========== 管理员系统 ==========
def is_admin(user_id: int) -> bool:
    if OWNER_ID and OWNER_ID.isdigit() and int(OWNER_ID) == user_id:
        return True
    admin_list = load_admins()
    return user_id in admin_list


def list_admins():
    """获取管理员列表"""
    return load_admins()

# ========== Web查账Token生成 ==========
def generate_web_token(chat_id: int, user_id: int, expires_hours: int = 24):
    """生成Web查账访问token"""
    from datetime import datetime
    expires_at = int((datetime.now() + timedelta(hours=expires_hours)).timestamp())
    data = f"{chat_id}:{user_id}:{expires_at}"
    signature = hmac.new(
        SESSION_SECRET.encode(),
        data.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"{data}:{signature}"

def generate_web_url(chat_id: int, user_id: int):
    """生成Web查账访问URL"""
    token = generate_web_token(chat_id, user_id)
    return f"{WEB_BASE_URL}/dashboard?token={token}"

# ========== 群内汇总显示 ==========
def render_group_summary(chat_id: int) -> str:
    state = load_group_state(chat_id)
    bot = state["bot_name"]
    rec_in, rec_out = state["recent"]["in"], state["recent"]["out"]
    should, sent = trunc2(state["summary"]["should_send_usdt"]), trunc2(state["summary"]["sent_usdt"])
    diff = trunc2(should - sent)
    rin, fin = state["defaults"]["in"]["rate"], state["defaults"]["in"]["fx"]
    rout, fout = state["defaults"]["out"]["rate"], state["defaults"]["out"]["fx"]

    lines = []
    lines.append(f"📊【{bot} 账单汇总】\n")
    
    # 分离出金记录中的"下发"和普通出金
    normal_out = [r for r in rec_out if r.get('type') != '下发']
    send_out = [r for r in rec_out if r.get('type') == '下发']
    
    # 入金记录
    lines.append(f"已入账 ({len(rec_in)}笔)")
    if rec_in:
        for r in rec_in[:5]:
            raw = r.get('raw', 0)
            fx = r.get('fx', fin)  # 如果没有保存汇率，使用默认汇率
            rate = r.get('rate', rin)  # 获取费率
            usdt = trunc2(r['usdt'])
            rate_percent = int(rate * 100)  # 转换为百分比整数
            rate_sup = to_superscript(rate_percent)  # 转换为上标
            lines.append(f"{r['ts']} {raw}  {rate_sup}/ {fx} = {usdt}")
    
    lines.append("")
    
    # 出金记录
    lines.append(f"已出账 ({len(normal_out)}笔)")
    if normal_out:
        for r in normal_out[:5]:
            if 'raw' in r:
                raw = r.get('raw', 0)
                fx = r.get('fx', fout)
                rate = r.get('rate', rout)
                usdt = round2(r['usdt'])  # 出金使用四舍五入
                rate_percent = int(rate * 100)
                rate_sup = to_superscript(rate_percent)
                lines.append(f"{r['ts']} {raw}  {rate_sup}/ {fx} = {usdt}")
    
    lines.append("")
    
    # 下发记录（只有当有下发记录时才显示）
    if send_out:
        lines.append(f"已下发 ({len(send_out)}笔)")
        for r in send_out[:5]:
            usdt = round2(abs(r['usdt']))  # 下发使用四舍五入
            lines.append(f"{r['ts']} {usdt}")
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
    """显示当天所有记录"""
    state = load_group_state(chat_id)
    bot = state["bot_name"]
    rec_in, rec_out = state["recent"]["in"], state["recent"]["out"]
    should, sent = trunc2(state["summary"]["should_send_usdt"]), trunc2(state["summary"]["sent_usdt"])
    diff = trunc2(should - sent)
    rin, fin = state["defaults"]["in"]["rate"], state["defaults"]["in"]["fx"]
    rout, fout = state["defaults"]["out"]["rate"], state["defaults"]["out"]["fx"]

    lines = []
    lines.append(f"📊【{bot} 完整账单】\n")
    
    # 分离出金记录中的"下发"和普通出金
    normal_out = [r for r in rec_out if r.get('type') != '下发']
    send_out = [r for r in rec_out if r.get('type') == '下发']
    
    # 入金记录
    lines.append(f"已入账 ({len(rec_in)}笔)")
    if rec_in:
        for r in rec_in:
            raw = r.get('raw', 0)
            fx = r.get('fx', fin)
            rate = r.get('rate', rin)
            usdt = trunc2(r['usdt'])
            rate_percent = int(rate * 100)
            rate_sup = to_superscript(rate_percent)
            lines.append(f"{r['ts']} {raw}  {rate_sup}/ {fx} = {usdt}")
    
    lines.append("")
    
    # 出金记录
    lines.append(f"已出账 ({len(normal_out)}笔)")
    if normal_out:
        for r in normal_out:
            if 'raw' in r:
                raw = r.get('raw', 0)
                fx = r.get('fx', fout)
                rate = r.get('rate', rout)
                usdt = round2(r['usdt'])  # 出金使用四舍五入
                rate_percent = int(rate * 100)
                rate_sup = to_superscript(rate_percent)
                lines.append(f"{r['ts']} {raw}  {rate_sup}/ {fx} = {usdt}")
    
    lines.append("")
    
    # 下发记录（只有当有下发记录时才显示）
    if send_out:
        lines.append(f"已下发 ({len(send_out)}笔)")
        for r in send_out:
            usdt = round2(abs(r['usdt']))  # 下发使用四舍五入
            lines.append(f"{r['ts']} {usdt}")
        lines.append("")
    
    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"⚙️ 当前费率：入 {rin*100:.0f}% ⇄ 出 {rout*100:.0f}%")
    lines.append(f"💱 固定汇率：入 {fin} ⇄ 出 {fout}")
    lines.append(f"📊 应下发：{fmt_usdt(should)}")
    lines.append(f"📤 已下发：{fmt_usdt(sent)}")
    lines.append(f"{'❗' if diff != 0 else '✅'} 未下发：{fmt_usdt(diff)}")
    lines.append("━━━━━━━━━━━━━━")
    return "\n".join(lines)

# ========== Telegram ==========
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

async def send_summary_with_button(update: Update, chat_id: int, user_id: int):
    """发送带Web查账按钮的汇总消息，返回发送的消息对象"""
    summary_text = render_group_summary(chat_id)
    
    # 仅在SESSION_SECRET配置时显示Web查账按钮
    if SESSION_SECRET:
        web_url = generate_web_url(chat_id, user_id)
        keyboard = [[InlineKeyboardButton("📊 查看账单明细", url=web_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = await update.message.reply_text(summary_text, reply_markup=reply_markup)
    else:
        # 未配置SESSION_SECRET时，只发送纯文本汇总
        msg = await update.message.reply_text(summary_text)
    
    return msg

async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """检查用户是否是群组管理员或群主"""
    chat = update.effective_chat
    if chat.type == "private":
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in ["creator", "administrator"]
    except Exception:
        return False

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    
    # 私聊模式
    if chat.type == "private":
        if is_admin(user.id):
            # 管理员私聊 - 显示完整操作说明
            await update.message.reply_text(
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
                "🔧 高级设置（指定国家）：\n"
                "  设置 日本 入 费率 8\n"
                "  设置 日本 入 汇率 127\n\n"
                "👥 管理员管理：\n"
                "  设置机器人管理员（回复消息）\n"
                "  删除机器人管理员（回复消息）\n"
                "  显示机器人管理员"
            )
        else:
            # 非管理员私聊 - 显示如何成为管理员的步骤
            await update.message.reply_text(
                "👋 你好！欢迎使用财务记账机器人\n\n"
                "💬 发送 /start 查看完整操作说明\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "📌 如何成为机器人管理员（详细步骤）：\n\n"
                "第1步：添加机器人到群组\n"
                "  • 点击机器人头像\n"
                "  • 选择「添加到群组或频道」\n"
                "  • 选择你的工作群组\n"
                "  • 确认添加\n\n"
                "第2步：在群里发送一条消息\n"
                "  • 进入群组聊天\n"
                "  • 发送任意消息（比如：\"申请管理员权限\"）\n"
                "  • 这样群管理员才能回复你的消息\n\n"
                "第3步：让机器人管理员授权\n"
                "  • 机器人管理员点击「回复」你的消息\n"
                "  • 在回复框中输入：设置机器人管理员\n"
                "  • 发送后，你就获得了机器人管理员权限\n\n"
                "第4步：开始使用机器人\n"
                "  • 发送 /start 查看所有指令\n"
                "  • 或直接在群里发送：+10000（记录入金）\n\n"
                "✅ 成为管理员后你可以：\n"
                "  • 📊 记录入金/出金交易\n"
                "  • 📋 查看和管理账单\n"
                "  • 💰 记录USDT下发\n"
                "  • ⚙️ 设置费率和汇率\n"
                "  • 🔄 撤销错误操作\n"
                "  • 🌍 设置不同国家的费率\n\n"
                "💡 重要提示：\n"
                "  ⚠️ 只有机器人管理员的操作才会被响应\n"
                "  ⚠️ 普通成员的操作机器人不会回复\n"
                "  ⚠️ 只有机器人管理员能授权其他成员（包括普通成员）\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "👉 再次发送 /start 查看完整功能列表"
            )
    else:
        # 群聊模式 - 显示完整操作说明
        await update.message.reply_text(
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
            "🔧 高级设置（指定国家）：\n"
            "  设置 日本 入 费率 8\n"
            "  设置 日本 入 汇率 127\n\n"
            "👥 管理员管理：\n"
            "  设置机器人管理员（回复消息）\n"
            "  删除机器人管理员（回复消息）\n"
            "  显示机器人管理员"
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    chat_id = chat.id
    # 支持纯文本和图片说明文字
    text = (update.message.text or update.message.caption or "").strip()
    ts, dstr = now_ts(), today_str()
    
    # ========== 私聊消息转发功能 ==========
    if chat.type == "private":
        # 记录私聊日志
        private_log_dir = LOG_DIR / "private_chats"
        private_log_dir.mkdir(exist_ok=True)
        user_log_file = private_log_dir / f"user_{user.id}.log"
        
        log_entry = f"[{ts}] {user.full_name} (@{user.username or 'N/A'}): {text}\n"
        with open(user_log_file, "a", encoding="utf-8") as f:
            f.write(log_entry)
        
        # 如果设置了OWNER_ID，且发送者不是OWNER，则转发给OWNER
        if OWNER_ID and OWNER_ID.isdigit():
            owner_id = int(OWNER_ID)
            
            if user.id != owner_id:
                # 非OWNER发送的私聊消息 - 转发给OWNER
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
                    
                    # 转发给OWNER并存储消息ID用于回复
                    sent_msg = await context.bot.send_message(
                        chat_id=owner_id,
                        text=forward_msg
                    )
                    
                    # 存储映射关系: OWNER收到的消息ID -> 原始用户ID
                    # 用于OWNER回复时知道发给谁
                    if 'private_msg_map' not in context.bot_data:
                        context.bot_data['private_msg_map'] = {}
                    context.bot_data['private_msg_map'][sent_msg.message_id] = user.id
                    
                    # 给用户回复确认消息
                    await update.message.reply_text(
                        "✅ 您的消息已发送给客服\n"
                        "⏳ 请耐心等待回复"
                    )
                    return
                    
                except Exception as e:
                    print(f"转发私聊消息失败: {e}")
                    # 继续处理，不影响后续逻辑
            else:
                # OWNER发送的私聊消息 - 检查是否是回复转发的消息
                if update.message.reply_to_message:
                    replied_msg_id = update.message.reply_to_message.message_id
                    
                    # 检查是否有映射关系
                    if 'private_msg_map' in context.bot_data:
                        target_user_id = context.bot_data['private_msg_map'].get(replied_msg_id)
                        
                        if target_user_id:
                            # OWNER正在回复某个用户的私聊
                            try:
                                await context.bot.send_message(
                                    chat_id=target_user_id,
                                    text=f"💬 客服回复：\n\n{text}"
                                )
                                await update.message.reply_text("✅ 回复已发送")
                                
                                # 记录回复日志到目标用户的日志文件
                                target_log_file = private_log_dir / f"user_{target_user_id}.log"
                                reply_log_entry = f"[{ts}] OWNER回复: {text}\n"
                                with open(target_log_file, "a", encoding="utf-8") as f:
                                    f.write(reply_log_entry)
                                
                                return
                            except Exception as e:
                                await update.message.reply_text(f"❌ 发送失败: {e}")
                                return
                
                # OWNER广播功能 - 群发消息给所有私聊过的用户
                if text.startswith("广播 ") or text.startswith("群发 "):
                    broadcast_text = text.split(" ", 1)[1] if len(text.split(" ", 1)) > 1 else ""
                    
                    if not broadcast_text:
                        await update.message.reply_text(
                            "❌ 请输入广播内容\n\n"
                            "使用格式：\n"
                            "广播 您的消息内容"
                        )
                        return
                    
                    # 获取所有私聊过的用户ID
                    user_ids = []
                    try:
                        if private_log_dir.exists():
                            for log_file in private_log_dir.glob("user_*.log"):
                                try:
                                    user_id = int(log_file.stem.split("_")[1])
                                    if user_id != int(OWNER_ID):  # 排除OWNER自己
                                        user_ids.append(user_id)
                                except (ValueError, IndexError):
                                    continue
                    except Exception as e:
                        await update.message.reply_text(f"❌ 读取用户列表失败: {e}")
                        return
                    
                    if not user_ids:
                        await update.message.reply_text("❌ 没有找到任何私聊用户")
                        return
                    
                    # 开始群发
                    await update.message.reply_text(
                        f"📢 开始广播...\n"
                        f"📊 目标用户数：{len(user_ids)}"
                    )
                    
                    success_count = 0
                    fail_count = 0
                    
                    for user_id in user_ids:
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=f"📢 系统通知：\n\n{broadcast_text}"
                            )
                            success_count += 1
                        except Exception as e:
                            fail_count += 1
                            print(f"广播失败 (用户 {user_id}): {e}")
                    
                    # 发送统计报告
                    await update.message.reply_text(
                        f"✅ 广播完成！\n\n"
                        f"📊 发送统计：\n"
                        f"• 成功：{success_count} 人\n"
                        f"• 失败：{fail_count} 人\n"
                        f"• 总计：{len(user_ids)} 人"
                    )
                    return
                
                # OWNER发送的非回复私聊消息 - 提示用法
                await update.message.reply_text(
                    "💡 使用提示：\n"
                    "• 回复转发的消息可以回复用户\n"
                    "• 在群组中使用记账功能\n\n"
                    "📢 广播功能：\n"
                    "• 广播 您的消息内容\n"
                    "• 群发 您的消息内容"
                )
                return
    
    # ========== 群组消息处理 ==========
    # 检查日期并在需要时重置账单（每个群组独立）
    check_and_reset_daily(chat_id)
    
    # 获取当前群组状态
    state = load_group_state(chat_id)
    
    # 撤销操作（必须：回复机器人消息 + 输入"撤销"）
    if text == "撤销" and update.message.reply_to_message and update.message.reply_to_message.from_user.is_bot:
        if not is_admin(user.id):
            return  # 非管理员不回复
        
        # 获取被回复消息的message_id
        replied_msg_id = update.message.reply_to_message.message_id
        
        # 在所有记录中查找这个message_id
        found_record = None
        record_type = None
        
        # 搜索入金记录
        for record in state["recent"]["in"]:
            if record.get("msg_id") == replied_msg_id:
                found_record = record
                record_type = "in"
                break
        
        # 如果在入金中没找到，搜索出金/下发记录
        if not found_record:
            for record in state["recent"]["out"]:
                if record.get("msg_id") == replied_msg_id:
                    found_record = record
                    record_type = "out"
                    break
        
        # 如果找到了记录，执行撤销
        if found_record and record_type:
            if record_type == "in":
                # 撤销入金
                raw_amt = found_record.get("raw", 0)
                usdt_amt = found_record.get("usdt", 0)
                country = found_record.get("country", "")
                
                # 反向操作：减少应下发
                state["summary"]["should_send_usdt"] = trunc2(state["summary"]["should_send_usdt"] - usdt_amt)
                
                # 从记录中移除
                state["recent"]["in"] = [r for r in state["recent"]["in"] if r.get("msg_id") != replied_msg_id]
                
                save_group_state(chat_id)
                append_log(log_path(chat_id, country, dstr), 
                          f"[撤销入金] 时间:{ts} 原金额:{raw_amt} USDT:{usdt_amt} 国家:{country or '通用'} 标记:无效操作")
                
                await update.message.reply_text(
                    f"✅ 已撤销入金记录\n"
                    f"📊 原金额：+{raw_amt} → {usdt_amt} USDT\n"
                    f"🌍 国家：{country or '通用'}"
                )
                await send_summary_with_button(update, chat_id, user.id)
                return
                
            elif record_type == "out":
                # 判断是出金还是下发
                is_disbursement = found_record.get("type") == "下发"
                usdt_amt = found_record.get("usdt", 0)
                
                if is_disbursement:
                    # 撤销下发：反向操作
                    state["summary"]["should_send_usdt"] = trunc2(state["summary"]["should_send_usdt"] + usdt_amt)
                    
                    # 从记录中移除
                    state["recent"]["out"] = [r for r in state["recent"]["out"] if r.get("msg_id") != replied_msg_id]
                    
                    save_group_state(chat_id)
                    append_log(log_path(chat_id, None, dstr), 
                              f"[撤销下发] 时间:{ts} USDT:{usdt_amt} 标记:无效操作")
                    
                    await update.message.reply_text(
                        f"✅ 已撤销下发记录\n"
                        f"📊 原金额：{usdt_amt} USDT"
                    )
                else:
                    # 撤销出金
                    raw_amt = found_record.get("raw", 0)
                    country = found_record.get("country", "")
                    
                    # 反向操作：减少已下发
                    state["summary"]["sent_usdt"] = round2(state["summary"]["sent_usdt"] - usdt_amt)
                    
                    # 从记录中移除
                    state["recent"]["out"] = [r for r in state["recent"]["out"] if r.get("msg_id") != replied_msg_id]
                    
                    save_group_state(chat_id)
                    append_log(log_path(chat_id, country, dstr), 
                              f"[撤销出金] 时间:{ts} 原金额:{raw_amt} USDT:{usdt_amt} 国家:{country or '通用'} 标记:无效操作")
                    
                    await update.message.reply_text(
                        f"✅ 已撤销出金记录\n"
                        f"📊 原金额：-{raw_amt} → {usdt_amt} USDT\n"
                        f"🌍 国家：{country or '通用'}"
                    )
                
                await send_summary_with_button(update, chat_id, user.id)
                return
        
        # 如果没找到记录
        await update.message.reply_text(
            "❌ 无法找到要撤销的交易记录\n\n"
            "💡 可能原因：\n"
            "• 这条消息不是交易记录\n"
            "• 记录可能已被清除或撤销\n"
            "• 数据已过每日重置时间"
        )
        return

    # 查看账单（+0 不记录）
    if text == "+0":
        await send_summary_with_button(update, chat_id, user.id)
        return
    
    # 管理员管理命令
    if text.startswith(("设置机器人管理员", "删除机器人管理员", "显示机器人管理员")):
        lst = list_admins()
        if text.startswith("显示"):
            lines = ["👥 机器人管理员列表\n"]
            lines.append(f"⭐ 超级管理员：{OWNER_ID or '未设置'}\n")
            
            if lst:
                lines.append("📋 机器人管理员：")
                for admin_id in lst:
                    try:
                        # 尝试获取用户信息
                        chat_member = await context.bot.get_chat_member(update.effective_chat.id, admin_id)
                        user_info = chat_member.user
                        
                        # 构建显示信息
                        name = user_info.full_name
                        username = f"@{user_info.username}" if user_info.username else ""
                        
                        if username:
                            lines.append(f"• {name} ({username}) - ID: {admin_id}")
                        else:
                            lines.append(f"• {name} - ID: {admin_id}")
                    except Exception:
                        # 如果获取失败，只显示ID
                        lines.append(f"• ID: {admin_id}")
            else:
                lines.append("暂无机器人管理员")
            
            await update.message.reply_text("\n".join(lines))
            return
        
        # 检查权限：只有机器人管理员可以设置
        if not is_admin(user.id):
            await update.message.reply_text("🚫 你没有权限设置机器人管理员。\n💡 只有机器人管理员可以执行此操作。")
            return
        
        # 获取目标用户：优先使用@mention，其次使用回复消息
        target = None
        
        # 方式1：检查消息中是否有@mention
        if update.message.entities:
            for entity in update.message.entities:
                if entity.type == "text_mention":
                    # @了一个没有用户名的用户
                    target = entity.user
                    break
                elif entity.type == "mention":
                    # @了一个有用户名的用户，但需要通过回复或其他方式获取完整信息
                    # 这种情况我们还是优先用回复消息
                    pass
        
        # 方式2：如果没有@mention，检查是否回复了消息
        if not target and update.message.reply_to_message:
            target = update.message.reply_to_message.from_user
        
        # 如果两种方式都没有获取到目标用户
        if not target:
            await update.message.reply_text(
                "❌ 请指定要操作的用户\n\n"
                "方式1：@用户名 设置机器人管理员\n"
                "方式2：回复用户消息 + 设置机器人管理员"
            )
            return
        
        # 执行操作
        if text.startswith("设置"):
            add_admin(target.id)
            await update.message.reply_text(f"✅ 已将 {target.mention_html()} 设置为机器人管理员。", parse_mode="HTML")
        elif text.startswith("删除"):
            remove_admin(target.id)
            await update.message.reply_text(f"🗑️ 已移除 {target.mention_html()} 的机器人管理员权限。", parse_mode="HTML")
        return

    # 查询国家点位（费率/汇率）
    if text.endswith("当前点位"):
        if not is_admin(user.id):
            return  # 非管理员不回复
        
        # 提取国家名（去掉"当前点位"）
        country = text.replace("当前点位", "").strip()
        
        if not country:
            await update.message.reply_text("❌ 请指定国家名称\n例如：美国当前点位")
            return
        
        # 获取该国家的费率和汇率
        countries = state["countries"]
        defaults = state["defaults"]
        
        # 查询入金费率和汇率
        in_rate = None
        in_fx = None
        if country in countries and "in" in countries[country]:
            in_rate = countries[country]["in"].get("rate")
            in_fx = countries[country]["in"].get("fx")
        
        # 如果没有专属设置，使用默认值
        if in_rate is None:
            in_rate = defaults["in"]["rate"]
            in_rate_source = "默认"
        else:
            in_rate_source = f"{country}专属"
            
        if in_fx is None:
            in_fx = defaults["in"]["fx"]
            in_fx_source = "默认"
        else:
            in_fx_source = f"{country}专属"
        
        # 查询出金费率和汇率
        out_rate = None
        out_fx = None
        if country in countries and "out" in countries[country]:
            out_rate = countries[country]["out"].get("rate")
            out_fx = countries[country]["out"].get("fx")
        
        if out_rate is None:
            out_rate = defaults["out"]["rate"]
            out_rate_source = "默认"
        else:
            out_rate_source = f"{country}专属"
            
        if out_fx is None:
            out_fx = defaults["out"]["fx"]
            out_fx_source = "默认"
        else:
            out_fx_source = f"{country}专属"
        
        # 构建回复消息
        lines = [
            f"📍【{country} 当前点位】\n",
            f"📥 入金设置：",
            f"  • 费率：{in_rate*100:.0f}% ({in_rate_source})",
            f"  • 汇率：{in_fx} ({in_fx_source})\n",
            f"📤 出金设置：",
            f"  • 费率：{abs(out_rate)*100:.0f}% ({out_rate_source})",
            f"  • 汇率：{out_fx} ({out_fx_source})"
        ]
        
        await update.message.reply_text("\n".join(lines))
        return
    
    # 重置为推荐默认值
    if text == "重置默认值" or text == "恢复默认值":
        if not is_admin(user.id):
            return  # 非管理员不回复
        
        # 重置为推荐默认值
        state["defaults"] = {
            "in":  {"rate": 0.10, "fx": 153},
            "out": {"rate": -0.02, "fx": 137},
        }
        save_group_state(chat_id)
        
        await update.message.reply_text(
            "✅ 已重置为推荐默认值\n\n"
            "📥 入金设置：\n"
            "  • 费率：10%\n"
            "  • 汇率：153\n\n"
            "📤 出金设置：\n"
            "  • 费率：2%\n"
            "  • 汇率：137"
        )
        return
    
    # 清除今日数据（从00:00到当前时间）
    if text == "清除数据":
        if not is_admin(user.id):
            return  # 非管理员不回复
        
        # 获取今天00:00的时间戳
        from datetime import datetime
        import pytz
        
        beijing_tz = pytz.timezone('Asia/Shanghai')
        now = datetime.now(beijing_tz)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 统计清除前的数据
        rec_in = state["recent"]["in"]
        rec_out = state["recent"]["out"]
        
        # 分离下发记录和普通出金记录
        normal_out = [r for r in rec_out if r.get('type') != '下发']
        send_out = [r for r in rec_out if r.get('type') == '下发']
        
        # 统计要清除的记录数量
        clear_in_count = 0
        clear_out_count = 0
        clear_send_count = 0
        
        # 统计清除的USDT金额
        cleared_in_usdt = 0.0
        cleared_out_usdt = 0.0
        cleared_send_usdt = 0.0
        
        # 筛选并清除入金记录
        new_in = []
        for r in rec_in:
            # 解析时间字符串 "🕐 14:30"
            time_str = r.get('ts', '')
            try:
                # 提取时间部分，例如 "🕐 14:30" -> "14:30"
                import re
                time_match = re.search(r'(\d{1,2}:\d{2})', time_str)
                if time_match:
                    time_part = time_match.group(1)
                    hour, minute = map(int, time_part.split(':'))
                    record_time = today_start.replace(hour=hour, minute=minute)
                    
                    # 如果记录时间在今天00:00之后，标记清除
                    if record_time >= today_start:
                        clear_in_count += 1
                        cleared_in_usdt += r.get('usdt', 0)
                    else:
                        new_in.append(r)
                else:
                    # 无法解析时间，保留记录
                    new_in.append(r)
            except:
                # 解析失败，保留记录
                new_in.append(r)
        
        # 筛选并清除出金记录（普通出金）
        new_normal_out = []
        for r in normal_out:
            time_str = r.get('ts', '')
            try:
                import re
                time_match = re.search(r'(\d{1,2}:\d{2})', time_str)
                if time_match:
                    time_part = time_match.group(1)
                    hour, minute = map(int, time_part.split(':'))
                    record_time = today_start.replace(hour=hour, minute=minute)
                    
                    if record_time >= today_start:
                        clear_out_count += 1
                        cleared_out_usdt += r.get('usdt', 0)
                    else:
                        new_normal_out.append(r)
                else:
                    new_normal_out.append(r)
            except:
                new_normal_out.append(r)
        
        # 筛选并清除下发记录
        new_send_out = []
        for r in send_out:
            time_str = r.get('ts', '')
            try:
                import re
                time_match = re.search(r'(\d{1,2}:\d{2})', time_str)
                if time_match:
                    time_part = time_match.group(1)
                    hour, minute = map(int, time_part.split(':'))
                    record_time = today_start.replace(hour=hour, minute=minute)
                    
                    if record_time >= today_start:
                        clear_send_count += 1
                        cleared_send_usdt += abs(r.get('usdt', 0))
                    else:
                        new_send_out.append(r)
                else:
                    new_send_out.append(r)
            except:
                new_send_out.append(r)
        
        # 合并出金和下发记录
        new_out = new_normal_out + new_send_out
        
        # 更新state
        state["recent"]["in"] = new_in
        state["recent"]["out"] = new_out
        
        # 重新计算汇总数据
        # 应下发 = 剩余入金USDT总和（使用截断）
        # 已下发 = 剩余出金USDT总和 + 剩余下发USDT总和（使用四舍五入）
        total_in_usdt = sum(r.get('usdt', 0) for r in new_in)
        total_out_usdt = sum(r.get('usdt', 0) for r in new_normal_out)
        total_send_usdt = sum(abs(r.get('usdt', 0)) for r in new_send_out)
        
        state["summary"]["should_send_usdt"] = trunc2(total_in_usdt)
        state["summary"]["sent_usdt"] = round2(total_out_usdt + total_send_usdt)
        
        save_group_state(chat_id)
        
        # 记录到日志
        append_log(log_path(chat_id, None, dstr), 
                   f"[清除数据] 时间:{ts} 清除入金:{clear_in_count}笔/{cleared_in_usdt:.2f}USDT "
                   f"出金:{clear_out_count}笔/{cleared_out_usdt:.2f}USDT "
                   f"下发:{clear_send_count}笔/{cleared_send_usdt:.2f}USDT")
        
        # 构建回复消息
        total_cleared = clear_in_count + clear_out_count + clear_send_count
        
        if total_cleared == 0:
            await update.message.reply_text(
                "ℹ️ 今日00:00之后暂无数据\n"
                "📊 无需清除"
            )
        else:
            lines = [
                "✅ 已清除今日数据（00:00至现在）\n",
                f"📥 已入账：清除 {clear_in_count} 笔 ({cleared_in_usdt:.2f} USDT)",
                f"📤 已出账：清除 {clear_out_count} 笔 ({cleared_out_usdt:.2f} USDT)",
                f"💰 已下发：清除 {clear_send_count} 笔 ({cleared_send_usdt:.2f} USDT)\n",
                "━━━━━━━━━━━━━━",
                f"📊 重新计算后：",
                f"  • 应下发：{total_in_usdt:.2f} USDT",
                f"  • 已下发：{total_out_usdt + total_send_usdt:.2f} USDT",
                f"  • 未下发：{total_in_usdt - total_out_usdt - total_send_usdt:.2f} USDT"
            ]
            await update.message.reply_text("\n".join(lines))
        
        # 显示更新后的账单
        await send_summary_with_button(update, chat_id, user.id)
        return
    
    # 简化的设置命令
    if text.startswith(("设置入金费率", "设置入金汇率", "设置出金费率", "设置出金汇率")):
        if not is_admin(user.id):
            return  # 非管理员不回复
        try:
            direction = ""
            key = ""
            val = 0.0
            display_val = ""
            
            # 解析命令
            if "入金费率" in text:
                direction, key = "in", "rate"
                val = float(text.replace("设置入金费率", "").strip())
                val /= 100.0  # 转换为小数
                display_val = f"{val*100:.0f}%"
            elif "入金汇率" in text:
                direction, key = "in", "fx"
                val = float(text.replace("设置入金汇率", "").strip())
                display_val = str(val)
            elif "出金费率" in text:
                direction, key = "out", "rate"
                val = float(text.replace("设置出金费率", "").strip())
                val /= 100.0  # 转换为小数
                display_val = f"{val*100:.0f}%"
            elif "出金汇率" in text:
                direction, key = "out", "fx"
                val = float(text.replace("设置出金汇率", "").strip())
                display_val = str(val)
            
            # 更新默认设置
            state["defaults"][direction][key] = val
            save_group_state(chat_id)
            
            # 构建回复消息
            type_name = "费率" if key == "rate" else "汇率"
            dir_name = "入金" if direction == "in" else "出金"
            await update.message.reply_text(
                f"✅ 已设置默认{dir_name}{type_name}\n"
                f"📊 新值：{display_val}"
            )
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入有效的数字\n例如：设置入金费率 10")
        return

    # 高级设置命令（指定国家）- 支持无空格格式
    if text.startswith("设置") and not text.startswith(("设置入金", "设置出金")):
        if not is_admin(user.id):
            return  # 非管理员不回复
        
        # 尝试匹配格式：设置 + 国家名 + 入/出 + 费率/汇率 + 数字
        # 例如：设置美国入费率7, 设置美国入汇率10
        import re
        pattern = r'^设置\s*(.+?)(入|出)(费率|汇率)\s*(\d+(?:\.\d+)?)\s*$'
        match = re.match(pattern, text)
        
        if match:
            scope = match.group(1).strip()  # 国家名
            direction = "in" if match.group(2) == "入" else "out"
            key = "rate" if match.group(3) == "费率" else "fx"
            try:
                val = float(match.group(4))
                if key == "rate": 
                    val /= 100.0  # 转换为小数
                
                if scope == "默认":
                    state["defaults"][direction][key] = val
                else:
                    state["countries"].setdefault(scope, {}).setdefault(direction, {})[key] = val
                
                save_group_state(chat_id)
                
                # 构建友好的回复消息
                type_name = "费率" if key == "rate" else "汇率"
                dir_name = "入金" if direction == "in" else "出金"
                display_val = f"{val*100:.0f}%" if key == "rate" else str(val)
                
                await update.message.reply_text(
                    f"✅ 已设置 {scope} {dir_name}{type_name}\n"
                    f"📊 新值：{display_val}"
                )
            except ValueError:
                await update.message.reply_text("❌ 数值格式错误")
            return
        else:
            # 尝试旧格式（有空格）：设置 国家 入 费率 值
            tokens = text.split()
            if len(tokens) >= 3:
                scope = tokens[1]
                direction = "in" if "入" in text else "out"
                key = "rate" if "费率" in text else "fx"
                try:
                    val = float(tokens[-1])
                    if key == "rate": val /= 100.0
                    if scope == "默认": 
                        state["defaults"][direction][key] = val
                    else:
                        state["countries"].setdefault(scope, {}).setdefault(direction, {})[key] = val
                    save_group_state(chat_id)
                    await update.message.reply_text(f"✅ 已设置 {scope} {direction} {key} = {val}")
                except ValueError:
                    return
                return

    # 入金
    if text.startswith("+"):
        if not is_admin(user.id):
            return  # 非管理员不回复
        amt, country = parse_amount_and_country(text)
        p = resolve_params(chat_id, "in", country)
        
        # 检查汇率是否已设置（费率可以为0）
        if p["fx"] == 0:
            await update.message.reply_text("⚠️ 请先设置费率和汇率")
            return
        
        usdt = trunc2(amt * (1 - p["rate"]) / p["fx"])
        # 先不保存message_id，发送后再更新
        push_recent(chat_id, "in", {"ts": ts, "raw": amt, "usdt": usdt, "country": country, "fx": p["fx"], "rate": p["rate"]})
        state["summary"]["should_send_usdt"] = trunc2(state["summary"]["should_send_usdt"] + usdt)
        save_group_state(chat_id)
        append_log(log_path(chat_id, country, dstr),
                   f"[入金] 时间:{ts} 国家:{country or '通用'} 原始:{amt} 汇率:{p['fx']} 费率:{p['rate']*100:.2f}% 结果:{usdt}")
        
        # 发送账单并保存message_id
        msg = await send_summary_with_button(update, chat_id, user.id)
        
        # 将message_id保存到最新一条入金记录中（索引0）
        if msg and state["recent"]["in"] and "msg_id" not in state["recent"]["in"][0]:
            state["recent"]["in"][0]["msg_id"] = msg.message_id
            save_group_state(chat_id)
        
        return

    # 出金
    if text.startswith("-"):
        if not is_admin(user.id):
            return  # 非管理员不回复
        amt, country = parse_amount_and_country(text)
        p = resolve_params(chat_id, "out", country)
        
        # 检查汇率是否已设置（费率可以为0）
        if p["fx"] == 0:
            await update.message.reply_text("⚠️ 请先设置费率和汇率")
            return
        
        usdt = round2(amt * (1 + p["rate"]) / p["fx"])
        push_recent(chat_id, "out", {"ts": ts, "raw": amt, "usdt": usdt, "country": country, "fx": p["fx"], "rate": p["rate"]})
        state["summary"]["sent_usdt"] = round2(state["summary"]["sent_usdt"] + usdt)
        save_group_state(chat_id)
        append_log(log_path(chat_id, country, dstr),
                   f"[出金] 时间:{ts} 国家:{country or '通用'} 原始:{amt} 汇率:{p['fx']} 费率:{p['rate']*100:.2f}% 下发:{usdt}")
        
        # 发送账单并保存message_id
        msg = await send_summary_with_button(update, chat_id, user.id)
        
        # 将message_id保存到最新一条出金记录中（索引0）
        if msg and state["recent"]["out"] and "msg_id" not in state["recent"]["out"][0]:
            state["recent"]["out"][0]["msg_id"] = msg.message_id
            save_group_state(chat_id)
        
        return

    # 下发USDT（仅管理员）
    if text.startswith("下发"):
        if not is_admin(user.id):
            return  # 非管理员不回复
        try:
            usdt_str = text.replace("下发", "").strip()
            usdt = trunc2(float(usdt_str))  # 对输入也进行精度截断
            
            if usdt > 0:
                # 正数：扣除应下发
                state["summary"]["should_send_usdt"] = trunc2(state["summary"]["should_send_usdt"] - usdt)
                push_recent(chat_id, "out", {"ts": ts, "usdt": usdt, "type": "下发"})
                append_log(log_path(chat_id, None, dstr), f"[下发USDT] 时间:{ts} 金额:{usdt} USDT")
            else:
                # 负数：增加应下发（撤销）
                usdt_abs = trunc2(abs(usdt))  # 对绝对值也进行精度截断
                state["summary"]["should_send_usdt"] = trunc2(state["summary"]["should_send_usdt"] + usdt_abs)
                push_recent(chat_id, "out", {"ts": ts, "usdt": usdt, "type": "下发"})
                append_log(log_path(chat_id, None, dstr), f"[撤销下发] 时间:{ts} 金额:{usdt_abs} USDT")
            
            save_group_state(chat_id)
            
            # 发送账单并保存message_id
            msg = await send_summary_with_button(update, chat_id, user.id)
            
            # 将message_id保存到最新一条下发记录中（索引0）
            if msg and state["recent"]["out"] and "msg_id" not in state["recent"]["out"][0]:
                state["recent"]["out"][0]["msg_id"] = msg.message_id
                save_group_state(chat_id)
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入有效的数字\n例如：下发35.04 或 下发-35.04")
        return

    # 查看更多记录
    if text in ["更多记录", "查看更多记录", "更多账单", "显示历史账单"]:
        await update.message.reply_text(render_full_summary(chat_id))
        return

    # 无效操作不回复

# ========== 初始化函数 ==========
def init_bot():
    """初始化Bot - Polling模式"""
    print("=" * 50)
    print("🚀 正在启动财务记账机器人...")
    print("=" * 50)
    
    if not BOT_TOKEN:
        print("❌ 错误：未找到 TELEGRAM_BOT_TOKEN 环境变量")
        exit(1)
    
    print("✅ Bot Token 已加载")
    print(f"📊 数据目录: {DATA_DIR}")
    print(f"👑 超级管理员: {OWNER_ID or '未设置'}")
    
    print("\n🤖 配置 Telegram Bot (Polling模式)...")
    from telegram.ext import ApplicationBuilder
    
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    # 支持纯文本和图片说明文字
    application.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_text))
    print("✅ Bot 处理器已注册")
    
    print("\n🎉 机器人正在运行，等待消息...")
    print("=" * 50)
    application.run_polling()

# ========== 程序入口 ==========
if __name__ == "__main__":
    init_bot()
