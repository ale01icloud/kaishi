# app.py —— 单文件版财务记账机器人（Polling 模式）

import os
import re
import threading
import json
import math
import datetime
from pathlib import Path
from dotenv import load_dotenv
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, List, Any, Optional, Set, Tuple

import requests  # 当前没有用到，用于以后需要时保留

# ========== 加载环境 ==========
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# 支持多个超级管理员：
# 示例：
#   OWNER_ID=7121576441,7566017299
#   SUPER_ADMINS=123456789
OWNER_ID_ENV = os.getenv("OWNER_ID", "").strip()
SUPER_ADMINS_ENV = os.getenv("SUPER_ADMINS", "").strip()


def _parse_id_list(s: str) -> Set[int]:
    ids: Set[int] = set()
    if not s:
        return ids
    for part in s.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


# 最终超级管理员集合（OWNER_ID + SUPER_ADMINS 合并）
SUPER_ADMINS: Set[int] = _parse_id_list(OWNER_ID_ENV) | _parse_id_list(SUPER_ADMINS_ENV)

# ========== 记账核心状态（多群组支持） ==========
DATA_DIR = Path("./data")
GROUPS_DIR = DATA_DIR / "groups"
LOG_DIR = DATA_DIR / "logs"
ADMINS_FILE = DATA_DIR / "admins.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
GROUPS_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 群组状态缓存 {chat_id: state_dict}
groups_state: Dict[int, Dict[str, Any]] = {}


def get_default_state() -> Dict[str, Any]:
    """返回默认群组状态"""
    return {
        "defaults": {
            "in": {"rate": 0.0, "fx": 0.0},
            "out": {"rate": 0.0, "fx": 0.0, "fee_usdt": 0.0},  # 出金手续费（USDT/笔）
        },
        "countries": {},  # 可扩展国家专属设置
        "precision": {"mode": "truncate", "digits": 2},
        "bot_name": "全球海外支付",
        "recent": {"in": [], "out": []},  # out 中包含普通出金 + 下发记录
        "summary": {"should_send_usdt": 0.0, "sent_usdt": 0.0},  # 保留兼容，不参与计算
        "last_date": "",

        # ✅ 新增：每日清空时间（北京时间），默认 00:00
        "reset_time": "00:00",
        # ✅ 新增：上一账期标识（用于判断是否需要清空）
        "last_period": "",
    }


def group_file_path(chat_id: int) -> Path:
    return GROUPS_DIR / f"group_{chat_id}.json"


def load_group_state(chat_id: int) -> Dict[str, Any]:
    if chat_id in groups_state:
        return groups_state[chat_id]

    file_path = group_file_path(chat_id)
    if file_path.exists():
        try:
            with file_path.open("r", encoding="utf-8") as f:
                state = json.load(f)

            # 兼容老数据补齐字段
            state.setdefault("recent", {"in": [], "out": []})
            state.setdefault("summary", {"should_send_usdt": 0.0, "sent_usdt": 0.0})
            state.setdefault(
                "defaults",
                {"in": {"rate": 0.0, "fx": 0.0}, "out": {"rate": 0.0, "fx": 0.0}},
            )
            state.setdefault("countries", {})
            state.setdefault("bot_name", "全球海外支付")
            state.setdefault("last_date", "")

            # 补齐出金手续费字段
            state["defaults"].setdefault("out", {})
            state["defaults"]["out"].setdefault("fee_usdt", 0.0)

            # ✅ 新增字段兼容
            state.setdefault("reset_time", "00:00")
            state.setdefault("last_period", "")

            groups_state[chat_id] = state
            return state
        except Exception as e:
            print(f"⚠️ 加载群组状态文件失败: {e}")

    state = get_default_state()
    groups_state[chat_id] = state
    save_group_state(chat_id)
    return state


def save_group_state(chat_id: int) -> None:
    if chat_id not in groups_state:
        return
    file_path = group_file_path(chat_id)
    try:
        with file_path.open("w", encoding="utf-8") as f:
            json.dump(groups_state[chat_id], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ 保存群组状态文件失败: {e}")


# ========== 机器人管理员（额外权限） ==========
admins_cache: Optional[List[int]] = None


def load_admins() -> List[int]:
    """从 JSON 加载机器人管理员列表"""
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

    admins_cache = []
    save_admins(admins_cache)
    return admins_cache


def save_admins(admin_list: List[int]) -> None:
    global admins_cache
    admins_cache = admin_list
    try:
        with ADMINS_FILE.open("w", encoding="utf-8") as f:
            json.dump({"admins": admin_list}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ 保存管理员文件失败: {e}")


def add_admin(user_id: int) -> bool:
    admins = load_admins()
    if user_id not in admins:
        admins.append(user_id)
        save_admins(admins)
        return True
    return False


def remove_admin(user_id: int) -> bool:
    admins = load_admins()
    if user_id in admins:
        admins.remove(user_id)
        save_admins(admins)
        return True
    return False


def list_admins() -> List[int]:
    return load_admins()


# ========== 工具函数 ==========
def trunc2(x: float) -> float:
    rounded = round(float(x), 6)
    return math.floor(rounded * 100.0) / 100.0


def round2(x: float) -> float:
    return round(float(x), 2)


def fmt_usdt(x: float) -> str:
    return f"{x:.2f} USDT"


def fmt_rate_percent(rate: float) -> str:
    """
    支持小数费率显示：
      0.035 -> 3.5%
      0.04  -> 4%
    """
    p = float(rate) * 100.0
    if abs(p - round(p)) < 1e-12:
        return f"{int(round(p))}%"
    s = f"{p:.2f}".rstrip("0").rstrip(".")
    return f"{s}%"


def fmt_rate_point(rate: float) -> str:
    """
    将费率小数显示成“点位”，不带 %：
      0.24 -> 24
      0.035 -> 3.5
      0.04 -> 4
    """
    p = float(rate) * 100.0
    if abs(p - round(p)) < 1e-12:
        return str(int(round(p)))
    return f"{p:.2f}".rstrip("0").rstrip(".")


def fmt_num(x: float) -> str:
    """
    数字显示优化（去掉无意义的小数 0）：
      1.0   -> 1
      153.0 -> 153
      22.80 -> 22.8
      22.00 -> 22
    """
    if x is None:
        return ""
    s = f"{float(x):.10f}".rstrip("0").rstrip(".")
    return s


def _beijing_now() -> datetime.datetime:
    beijing_tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(beijing_tz)


def now_ts() -> str:
    return _beijing_now().strftime("%H:%M")


def today_str() -> str:
    return _beijing_now().strftime("%Y-%m-%d")


def _parse_hhmm(hhmm: str) -> Tuple[int, int]:
    hhmm = (hhmm or "").strip()
    m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", hhmm)
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2))


def _current_period_id(reset_time: str) -> str:
    """
    返回当前账期标识（YYYY-MM-DD），规则：
    - 以北京时间 reset_time 为边界
    - now >= 今日边界 => period = 今日
    - 否则 period = 昨日
    """
    now = _beijing_now()
    hh, mm = _parse_hhmm(reset_time)
    boundary_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now >= boundary_today:
        period_date = boundary_today.date()
    else:
        period_date = (boundary_today - datetime.timedelta(days=1)).date()
    return period_date.strftime("%Y-%m-%d")


def check_and_reset_daily(chat_id: int) -> bool:
    """按设定清空时间（北京时间）跨账期自动清空（在下一次群消息触发时执行）"""
    state = load_group_state(chat_id)

    reset_time = state.get("reset_time", "00:00")
    period = _current_period_id(reset_time)
    last_period = state.get("last_period", "")

    # 初始化
    if not last_period:
        state["last_period"] = period
        # 兼容：保留 last_date 字段（不影响）
        state["last_date"] = today_str()
        save_group_state(chat_id)
        return False

    # 跨账期：清空
    if last_period != period:
        state["recent"]["in"] = []
        state["recent"]["out"] = []
        state["summary"]["should_send_usdt"] = 0.0
        state["summary"]["sent_usdt"] = 0.0
        state["last_period"] = period
        state["last_date"] = today_str()
        save_group_state(chat_id)
        return True

    return False


def log_path(chat_id: int, country: Optional[str], date_str: str) -> Path:
    folder = f"group_{chat_id}"
    if country:
        folder = f"{folder}/{country}"
    else:
        folder = f"{folder}/通用"
    p = LOG_DIR / folder
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{date_str}.log"


def append_log(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text.strip() + "\n")


def push_recent(chat_id: int, kind: str, item: Dict[str, Any]) -> None:
    state = load_group_state(chat_id)
    arr = state["recent"][kind]
    arr.insert(0, item)  # 最新放在前面
    save_group_state(chat_id)


def resolve_params(chat_id: int, direction: str, country: Optional[str]) -> Dict[str, float]:
    """
    兼容国家专属设置：
    - rate / fx 若国家专属没设置，则用 defaults
    """
    state = load_group_state(chat_id)
    countries = state.get("countries", {})
    defaults = state.get("defaults", {})
    res: Dict[str, float] = {"rate": 0.0, "fx": 0.0}

    rate: Optional[float] = None
    fx: Optional[float] = None

    if country and country in countries:
        if direction in countries[country]:
            rate = countries[country][direction].get("rate")
            fx = countries[country][direction].get("fx")

    if rate is None:
        rate = defaults.get(direction, {}).get("rate", 0.0)
    if fx is None:
        fx = defaults.get(direction, {}).get("fx", 0.0)

    res["rate"] = float(rate or 0.0)
    res["fx"] = float(fx or 0.0)
    return res


def parse_amount_and_country(text: str) -> Tuple[Optional[float], Optional[str]]:
    """
    解析金额 + 国家，支持：
      +100
      +1千 / +1万 / +1.5万
      +1000 / 日本
      +1万 / 日本
    """
    s = text.strip()
    m = re.match(r"^[\+\-]\s*([0-9]+(?:\.[0-9]+)?)\s*([万千kKwW]?)", s)
    if not m:
        return None, None

    amount = float(m.group(1))
    unit = m.group(2)

    if unit in ("千", "k", "K"):
        amount *= 1000
    elif unit in ("万", "w", "W"):
        amount *= 10000

    m2 = re.search(r"/\s*([^\s]+)$", s)
    country = m2.group(1) if m2 else None
    return amount, country


def short_peer_name(name: str, n: int = 6) -> str:
    """回复对象名字截取前 n 位（默认 6）"""
    name = (name or "").strip()
    if not name:
        return ""
    return name[:n]


# ========== 权限系统 ==========
def is_super_admin(user_id: int) -> bool:
    """超级管理员判断：仅依赖环境变量"""
    return user_id in SUPER_ADMINS


def is_bot_admin(user_id: int) -> bool:
    """机器人管理员 / 超级管理员：可以操作所有记账功能"""
    if is_super_admin(user_id):
        return True
    return user_id in load_admins()


def can_manage_bot_admin(user_id: int) -> bool:
    """只有超级管理员可以设置 / 删除机器人管理员"""
    return is_super_admin(user_id)


# ========== 汇总渲染 ==========
def compute_totals(state: Dict[str, Any]) -> Dict[str, Any]:
    rec_in = state.get("recent", {}).get("in", [])
    rec_out = state.get("recent", {}).get("out", [])

    normal_out = [r for r in rec_out if r.get("type") != "下发"]
    send_out = [r for r in rec_out if r.get("type") == "下发"]

    total_in = trunc2(sum(float(r.get("usdt", 0.0)) for r in rec_in))
    total_out = trunc2(sum(float(r.get("usdt", 0.0)) for r in normal_out))
    total_send = trunc2(sum(float(r.get("usdt", 0.0)) for r in send_out))

    should = total_in                          # 应下发 = 已入账合计
    sent = trunc2(total_out + total_send)      # 已下发 = 出账合计 + 下发合计
    diff = trunc2(should - sent)               # 未下发 = 应下发 - 已下发（可为负）

    return {
        "total_in": total_in,
        "total_out": total_out,
        "total_send": total_send,
        "should": should,
        "sent": sent,
        "diff": diff,
        "normal_out": normal_out,
        "send_out": send_out,
        "rec_in": rec_in,
        "rec_out": rec_out,
    }


def _render_line_peer(r: Dict[str, Any]) -> str:
    peer = (r.get("peer") or "").strip()
    return f" [{peer}]" if peer else ""


def render_group_summary(chat_id: int) -> str:
    state = load_group_state(chat_id)
    bot = state.get("bot_name", "全球海外支付")
    reset_time = state.get("reset_time", "00:00")

    totals = compute_totals(state)
    rec_in = totals["rec_in"]
    normal_out = totals["normal_out"]
    send_out = totals["send_out"]

    rin = float(state["defaults"]["in"]["rate"])
    fin = float(state["defaults"]["in"]["fx"])
    rout = float(state["defaults"]["out"]["rate"])
    fout = float(state["defaults"]["out"]["fx"])

    lines: List[str] = []
    lines.append(f"【{bot} 账单汇总】\n")

    # 入金（前5条）
    lines.append(f"已入账 ({len(rec_in)}笔)")
    for r in rec_in[:5]:
        raw = r.get("raw", 0)
        fx = r.get("fx", fin)
        rate = float(r.get("rate", rin))
        usdt = trunc2(float(r.get("usdt", 0.0)))
        ts = r.get("ts", "")
        # 紧凑显示：点位/汇率，不带% & 去掉无意义小数0
        lines.append(
            f"{ts} {fmt_num(raw)} {fmt_rate_point(rate)}/{fmt_num(fx)} = {fmt_num(usdt)}{_render_line_peer(r)}"
        )
    lines.append("")

    # 出金（前5条）
    lines.append(f"已出账 ({len(normal_out)}笔)")
    for r in normal_out[:5]:
        raw = r.get("raw", 0)
        fx = r.get("fx", fout)
        rate = float(r.get("rate", rout))
        usdt = float(r.get("usdt", 0.0))
        ts = r.get("ts", "")
        fee = float(r.get("fee_usdt", 0.0))
        fee_txt = f" (手续费{fmt_num(fee)})" if fee > 0 else ""
        lines.append(
            f"{ts} {fmt_num(raw)} {fmt_rate_point(rate)}/{fmt_num(fx)} = {fmt_num(usdt)}{fee_txt}{_render_line_peer(r)}"
        )
    lines.append("")

    # 下发（前5条，保留正负）
    lines.append(f"已下发记录 ({len(send_out)}笔)")
    for r in send_out[:5]:
        ts = r.get("ts", "")
        usdt = trunc2(float(r.get("usdt", 0.0)))  # 保留正负
        lines.append(f"{ts} {fmt_num(usdt)}{_render_line_peer(r)}")
    lines.append("")

    # 当前费率也用点位显示（不带%）
    lines.append(f"当前费率： 入 {fmt_rate_point(rin)} ⇄ 出 {fmt_rate_point(abs(rout))}")
    lines.append(f"固定汇率： 入 {fmt_num(fin)} ⇄ 出 {fmt_num(fout)}")
    lines.append(f"应下发：{fmt_usdt(totals['should'])}")
    lines.append(f"已下发：{fmt_usdt(totals['sent'])}")
    lines.append(f"未下发：{fmt_usdt(totals['diff'])}")
    lines.append("")
    lines.append("**查看更多记录**：发送「更多记录」")
    return "\n".join(lines)


def render_full_summary(chat_id: int) -> str:
    state = load_group_state(chat_id)
    bot = state.get("bot_name", "全球海外支付")
    reset_time = state.get("reset_time", "00:00")

    totals = compute_totals(state)
    rec_in = totals["rec_in"]
    normal_out = totals["normal_out"]
    send_out = totals["send_out"]

    rin = float(state["defaults"]["in"]["rate"])
    fin = float(state["defaults"]["in"]["fx"])
    rout = float(state["defaults"]["out"]["rate"])
    fout = float(state["defaults"]["out"]["fx"])
    fee_usdt = float(state["defaults"]["out"].get("fee_usdt", 0.0))

    lines: List[str] = []
    lines.append(f"【{bot} 完整账单】\n")

    lines.append(f"已入账 ({len(rec_in)}笔)")
    for r in rec_in:
        raw = r.get("raw", 0)
        fx = r.get("fx", fin)
        rate = float(r.get("rate", rin))
        usdt = trunc2(float(r.get("usdt", 0.0)))
        ts = r.get("ts", "")
        lines.append(
            f"{ts} {fmt_num(raw)} {fmt_rate_point(rate)}/{fmt_num(fx)} = {fmt_num(usdt)}{_render_line_peer(r)}"
        )
    lines.append("")

    lines.append(f"已出账 ({len(normal_out)}笔)")
    for r in normal_out:
        raw = r.get("raw", 0)
        fx = r.get("fx", fout)
        rate = float(r.get("rate", rout))
        usdt = float(r.get("usdt", 0.0))
        ts = r.get("ts", "")
        fee = float(r.get("fee_usdt", 0.0))
        fee_txt = f" (手续费{fmt_num(fee)})" if fee > 0 else ""
        lines.append(
            f"{ts} {fmt_num(raw)} {fmt_rate_point(rate)}/{fmt_num(fx)} = {fmt_num(usdt)}{fee_txt}{_render_line_peer(r)}"
        )
    lines.append("")

    lines.append(f"已下发记录 ({len(send_out)}笔)")
    for r in send_out:
        ts = r.get("ts", "")
        usdt = trunc2(float(r.get("usdt", 0.0)))
        lines.append(f"{ts} {fmt_num(usdt)}{_render_line_peer(r)}")
    lines.append("")

    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"当前费率： 入 {fmt_rate_point(rin)} ⇄ 出 {fmt_rate_point(abs(rout))}")
    lines.append(f"固定汇率： 入 {fmt_num(fin)} ⇄ 出 {fmt_num(fout)}")
    lines.append(f"出金手续费： {fmt_num(fee_usdt)} USDT/笔")
    lines.append(f"应下发：{fmt_usdt(totals['should'])}")
    lines.append(f"已下发：{fmt_usdt(totals['sent'])}")
    lines.append(f"未下发：{fmt_usdt(totals['diff'])}")
    lines.append("━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ========== Telegram ==========
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    if chat.type == "private":
        if is_bot_admin(user.id):
            await update.message.reply_text(
                "🤖 你好，我是财务记账机器人。\n\n"
                "📊 记账操作（仅机器人管理员 / 超级管理员）：\n"
                "  入金：+10000 或 +10000 / 日本\n"
                "  出金：-10000 或 -10000 / 日本\n"
                "  支持：+1千 / +1万 / +1.5万 等简写\n"
                "  查看账单：+0 或 更多记录\n\n"
                "💰 下发记录（仅机器人管理员 / 超级管理员）：\n"
                "  下发100（记一条+100）\n"
                "  下发-100（记一条-100，账单里显示-100）\n\n"
                "🧾 出金手续费（仅机器人管理员 / 超级管理员）：\n"
                "  设置出金手续费 1   （每笔出金 +1 USDT）\n"
                "  设置出金手续费 0   （关闭手续费）\n\n"
                "⏰ 清空时间（仅机器人管理员 / 超级管理员）：\n"
                "  设置清空时间 06:00（北京时间，账期仍为 24 小时）\n"
                "  查看清空时间\n\n"
                "🔄 撤销功能（仅机器人管理员 / 超级管理员）：\n"
                "  撤销入金 / 撤销出金 / 撤销下发\n\n"
                "🧹 清空数据（仅机器人管理员 / 超级管理员）：\n"
                "  清除数据 / 清空数据 / 清楚数据 / 清除账单 / 清空账单\n\n"
                "⚙️ 参数设置（仅机器人管理员 / 超级管理员）：\n"
                "  重置默认值\n"
                "  设置入金费率 3.5\n"
                "  设置入金汇率 153\n"
                "  设置出金费率 2\n"
                "  设置出金汇率 137\n\n"
                "👥 机器人管理员管理（仅超级管理员）：\n"
                "  设置管理员（回复用户消息）\n"
                "  删除管理员（回复用户消息）\n"
                "  显示管理员\n\n"
                "📌 提示：你在群里操作入金/出金/下发时，如果是“回复某人的消息”再发指令，账单会显示对方名字前6位。"
            )
        else:
            await update.message.reply_text(
                "👋 你好！欢迎使用财务记账机器人\n\n"
                "• +0 可查看账单汇总\n"
                "• 更多记录 可查看完整账单\n\n"
                "如需记账权限，请联系超级管理员设置你为机器人管理员。"
            )
    else:
        await update.message.reply_text(
            "🤖 你好，我是财务记账机器人。\n\n"
            "📌 所有人可用：\n"
            "  +0 查看汇总 / 更多记录 查看完整账单\n\n"
            "🔒 仅机器人管理员 / 超级管理员可用：\n"
            "  入金：+10000 或 +10000 / 日本\n"
            "  出金：-10000 或 -10000 / 日本\n"
            "  下发：下发100 / 下发-100\n"
            "  撤销：撤销入金 / 撤销出金 / 撤销下发\n"
            "  清空：清除数据 / 清空账单\n"
            "  手续费：设置出金手续费 1（0关闭）\n"
            "  清空时间：设置清空时间 06:00（查看：查看清空时间）\n\n"
            "👥 仅超级管理员可用：\n"
            "  设置管理员（回复用户消息）/ 删除管理员（回复用户消息）/ 显示管理员"
        )


async def resolve_target_user_for_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[Any]:
    """
    仅支持：回复用户消息（最稳定）
    """
    msg = update.message
    if not msg:
        return None

    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user

    return None


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    chat_id = chat.id
    text = (update.message.text or update.message.caption or "").strip()
    ts, dstr = now_ts(), today_str()

    # ========== 私聊转发给第一个超级管理员 ==========
    if chat.type == "private":
        private_log_dir = LOG_DIR / "private_chats"
        private_log_dir.mkdir(exist_ok=True)
        user_log_file = private_log_dir / f"user_{user.id}.log"

        log_entry = f"[{ts}] {user.full_name} (@{user.username or 'N/A'}): {text}\n"
        with open(user_log_file, "a", encoding="utf-8") as f:
            f.write(log_entry)

        if SUPER_ADMINS:
            main_owner = list(SUPER_ADMINS)[0]
            if user.id != main_owner:
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

                    sent_msg = await context.bot.send_message(
                        chat_id=main_owner,
                        text=forward_msg,
                    )

                    if "private_msg_map" not in context.bot_data:
                        context.bot_data["private_msg_map"] = {}
                    context.bot_data["private_msg_map"][sent_msg.message_id] = user.id

                    await update.message.reply_text("✅ 您的消息已发送给客服\n⏳ 请耐心等待回复")
                    return
                except Exception as e:
                    print(f"转发私聊消息失败: {e}")

            else:
                # 超级管理员在私聊里回复用户
                if update.message.reply_to_message:
                    replied_msg_id = update.message.reply_to_message.message_id
                    target_user_id = context.bot_data.get("private_msg_map", {}).get(replied_msg_id)
                    if target_user_id:
                        try:
                            await context.bot.send_message(
                                chat_id=target_user_id,
                                text=f"💬 客服回复：\n\n{text}",
                            )
                            await update.message.reply_text("✅ 回复已发送")
                            target_log_file = private_log_dir / f"user_{target_user_id}.log"
                            reply_log_entry = f"[{ts}] OWNER回复: {text}\n"
                            with open(target_log_file, "a", encoding="utf-8") as f:
                                f.write(reply_log_entry)
                            return
                        except Exception as e:
                            await update.message.reply_text(f"❌ 发送失败: {e}")
                            return

        await update.message.reply_text("💡 已记录您的消息，如需查看账单请在群里发送 +0。")
        return

    # ========== 群组消息处理 ==========
    check_and_reset_daily(chat_id)
    state = load_group_state(chat_id)

    # 获取“回复对象名称（前6位）”
    peer6 = ""
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        peer6 = short_peer_name(update.message.reply_to_message.from_user.full_name, 6)

    # 所有人都可查看汇总
    if text == "+0":
        await update.message.reply_text(render_group_summary(chat_id))
        return

    # 所有人都可看完整记录
    if text in ["更多记录", "查看更多记录", "更多账单", "显示历史账单"]:
        await update.message.reply_text(render_full_summary(chat_id))
        return

    # ========== 管理机器人管理员（仅超级管理员） ==========
    # 仅保留：回复用户消息 -> 发送「设置管理员」「删除管理员」
    if text.strip() in ("设置管理员", "删除管理员", "显示管理员"):
        admins = list_admins()

        if text.strip() == "显示管理员":
            lines: List[str] = []
            lines.append("👥 机器人权限列表\n")

            if SUPER_ADMINS:
                lines.append("⭐ 超级管理员：")
                for sid in sorted(SUPER_ADMINS):
                    try:
                        cm = await context.bot.get_chat_member(chat_id, sid)
                        u = cm.user
                        username = f"@{u.username}" if u.username else ""
                        if username:
                            lines.append(f"  - {u.full_name} ({username}) - ID: {sid}")
                        else:
                            lines.append(f"  - {u.full_name} - ID: {sid}")
                    except Exception:
                        lines.append(f"  - ID: {sid}")
                lines.append("")
            else:
                lines.append("⭐ 超级管理员：未设置\n")

            if admins:
                lines.append("📋 机器人管理员：")
                for aid in admins:
                    try:
                        cm = await context.bot.get_chat_member(chat_id, aid)
                        u = cm.user
                        username = f"@{u.username}" if u.username else ""
                        if username:
                            lines.append(f"  - {u.full_name} ({username}) - ID: {aid}")
                        else:
                            lines.append(f"  - {u.full_name} - ID: {aid}")
                    except Exception:
                        lines.append(f"  - ID: {aid}")
            else:
                lines.append("暂无机器人管理员")

            await update.message.reply_text("\n".join(lines))
            return

        if not can_manage_bot_admin(user.id):
            await update.message.reply_text("🚫 只有超级管理员可以设置/删除机器人管理员。")
            return

        target = await resolve_target_user_for_admin(update, context)

        if not target or getattr(target, "id", None) is None:
            await update.message.reply_text(
                "❌ 请先【回复对方的消息】再发送：设置管理员 或 删除管理员\n"
                "示例：回复某人一句话 → 发送「设置管理员」"
            )
            return

        target_id = int(target.id)

        # mention_html 兼容：target 可能没有该方法（这里 target 来自 reply，一般有）
        target_mention = ""
        try:
            target_mention = target.mention_html()
        except Exception:
            uname = getattr(target, "username", None)
            fname = getattr(target, "full_name", None) or str(target_id)
            target_mention = f"{fname} (@{uname})" if uname else f"{fname} (ID:{target_id})"

        if text.strip() == "设置管理员":
            add_admin(target_id)
            await update.message.reply_text(
                f"✅ 已将 {target_mention} 设置为机器人管理员。",
                parse_mode="HTML",
            )
            return

        if text.strip() == "删除管理员":
            remove_admin(target_id)
            await update.message.reply_text(
                f"🗑️ 已移除 {target_mention} 的机器人管理员权限。",
                parse_mode="HTML",
            )
            return

    # 以下所有操作：仅机器人管理员 / 超级管理员
    if not is_bot_admin(user.id):
        return

    # ========== 设置账单名称 ==========
    if text.startswith("设置账单名称"):
        new_name = text.replace("设置账单名称", "", 1).strip()
        if not new_name:
            await update.message.reply_text("❌ 请输入账单名称，例如：设置账单名称 东启海外支付")
            return
        state["bot_name"] = new_name
        save_group_state(chat_id)
        await update.message.reply_text(f"✅ 账单名称已修改为：{new_name}")
        return

    # ========== 设置清空时间（北京时间） ==========
    if text.startswith("设置清空时间"):
        val = text.replace("设置清空时间", "", 1).strip()
        m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", val)
        if not m:
            await update.message.reply_text("❌ 格式：设置清空时间 HH:MM（例如：设置清空时间 06:00）")
            return

        state["reset_time"] = val
        # 立即对齐当前账期，避免设置后下一条消息误判
        state["last_period"] = _current_period_id(val)
        save_group_state(chat_id)

        await update.message.reply_text(f"✅ 已设置每日清空时间（北京时间）：{val}\n📌 账期长度仍为 24 小时。")
        await update.message.reply_text(render_group_summary(chat_id))
        return

    if text.strip() in ("查看清空时间", "当前清空时间"):
        rt = state.get("reset_time", "00:00")
        await update.message.reply_text(f"⏰ 当前每日清空时间（北京时间）：{rt}\n📌 账期长度：24 小时。")
        return

    # ========== 设置出金手续费（USDT/笔） ==========
    if text.startswith("设置出金手续费"):
        val_str = text.replace("设置出金手续费", "", 1).strip()
        if not val_str:
            await update.message.reply_text("❌ 格式：设置出金手续费 1（0关闭）")
            return
        try:
            fee = float(val_str)
            if fee < 0:
                await update.message.reply_text("❌ 手续费不能为负数")
                return
            state["defaults"].setdefault("out", {})
            state["defaults"]["out"]["fee_usdt"] = round2(fee)
            save_group_state(chat_id)
            await update.message.reply_text(f"✅ 已设置出金手续费：{round2(fee):.2f} USDT/笔（0为关闭）")
            await update.message.reply_text(render_group_summary(chat_id))
            return
        except ValueError:
            await update.message.reply_text("❌ 请输入有效数字，例如：设置出金手续费 1 或 设置出金手续费 0")
            return

    # ========== 查询国家点位 ==========
    if text.endswith("当前点位"):
        country = text.replace("当前点位", "").strip()
        if not country:
            await update.message.reply_text("❌ 请指定国家名称，例如：日本当前点位")
            return

        countries = state["countries"]
        defaults = state["defaults"]

        def _get(direction: str, key: str):
            v = None
            src = "默认"
            if country in countries and direction in countries[country]:
                if key in countries[country][direction]:
                    v = countries[country][direction][key]
                    src = f"{country}专属"
            if v is None:
                v = defaults[direction].get(key, 0)
                src = "默认"
            return v, src

        in_rate, in_rate_src = _get("in", "rate")
        in_fx, in_fx_src = _get("in", "fx")
        out_rate, out_rate_src = _get("out", "rate")
        out_fx, out_fx_src = _get("out", "fx")
        out_fee = float(defaults["out"].get("fee_usdt", 0.0))
        reset_time = state.get("reset_time", "00:00")

        lines = [
            f"📍【{country} 当前点位】\n",
            "📥 入金设置：",
            f"  • 费率：{fmt_rate_point(float(in_rate))} ({in_rate_src})",
            f"  • 汇率：{fmt_num(in_fx)} ({in_fx_src})\n",
            "📤 出金设置：",
            f"  • 费率：{fmt_rate_point(abs(float(out_rate)))} ({out_rate_src})",
            f"  • 汇率：{fmt_num(out_fx)} ({out_fx_src})",
            f"  • 手续费：{fmt_num(out_fee)} USDT/笔（默认）\n",
            f"⏰ 清空时间（北京时间）：{reset_time}（账期 24 小时）",
        ]
        await update.message.reply_text("\n".join(lines))
        return

    # ========== 重置默认值 ==========
    if text in ("重置默认值", "恢复默认值"):
        state["defaults"] = {
            "in": {"rate": 0.10, "fx": 153},
            "out": {
                "rate": 0.02,
                "fx": 137,
                "fee_usdt": float(state["defaults"]["out"].get("fee_usdt", 0.0)),
            },
        }
        save_group_state(chat_id)
        await update.message.reply_text(
            "✅ 已重置为推荐默认值\n\n"
            "📥 入金设置：费率 10% / 汇率 153\n"
            "📤 出金设置：费率 2% / 汇率 137\n"
            f"🧾 出金手续费：{float(state['defaults']['out'].get('fee_usdt', 0.0)):.2f} USDT/笔"
        )
        return

    # ========== 简单设置默认费率/汇率（支持小数费率） ==========
    if text.startswith(("设置入金费率", "设置入金汇率", "设置出金费率", "设置出金汇率")):
        try:
            direction = ""
            key = ""
            val = 0.0
            display_val = ""

            if text.startswith("设置入金费率"):
                direction, key = "in", "rate"
                val = float(text.replace("设置入金费率", "", 1).strip()) / 100.0
                display_val = fmt_rate_percent(val)
            elif text.startswith("设置入金汇率"):
                direction, key = "in", "fx"
                val = float(text.replace("设置入金汇率", "", 1).strip())
                display_val = str(val)
            elif text.startswith("设置出金费率"):
                direction, key = "out", "rate"
                val = float(text.replace("设置出金费率", "", 1).strip()) / 100.0
                display_val = fmt_rate_percent(val)
            elif text.startswith("设置出金汇率"):
                direction, key = "out", "fx"
                val = float(text.replace("设置出金汇率", "", 1).strip())
                display_val = str(val)

            state["defaults"].setdefault(direction, {})
            state["defaults"][direction][key] = val
            save_group_state(chat_id)

            type_name = "费率" if key == "rate" else "汇率"
            dir_name = "入金" if direction == "in" else "出金"
            await update.message.reply_text(f"✅ 已设置默认{dir_name}{type_name}\n📊 新值：{display_val}")
            return
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入有效的数字\n例如：设置入金费率 3.5")
            return

    # ========== 高级设置（指定国家）（费率支持小数） ==========
    if text.startswith("设置") and not text.startswith(("设置入金", "设置出金", "设置账单名称", "设置出金手续费", "设置清空时间")):
        pattern = r"^设置\s*(.+?)(入|出)(费率|汇率)\s*(\d+(?:\.\d+)?)\s*$"
        match = re.match(pattern, text)
        if match:
            scope = match.group(1).strip()
            direction = "in" if match.group(2) == "入" else "out"
            key = "rate" if match.group(3) == "费率" else "fx"
            try:
                val = float(match.group(4))
                if key == "rate":
                    val /= 100.0

                if scope == "默认":
                    state["defaults"].setdefault(direction, {})
                    state["defaults"][direction][key] = val
                else:
                    state["countries"].setdefault(scope, {}).setdefault(direction, {})[key] = val

                save_group_state(chat_id)

                type_name = "费率" if key == "rate" else "汇率"
                dir_name = "入金" if direction == "in" else "出金"
                display_val = fmt_rate_percent(val) if key == "rate" else str(val)
                await update.message.reply_text(f"✅ 已设置 {scope} {dir_name}{type_name}\n📊 新值：{display_val}")
                return
            except ValueError:
                await update.message.reply_text("❌ 数值格式错误")
                return

    # ========== 清空今日数据 ==========
    if text in ("清除数据", "清空数据", "清楚数据", "清除账单", "清空账单"):
        totals = compute_totals(state)
        in_count = len(state["recent"]["in"])
        out_count = len(state["recent"]["out"])

        state["recent"]["in"] = []
        state["recent"]["out"] = []
        state["summary"]["should_send_usdt"] = 0.0
        state["summary"]["sent_usdt"] = 0.0
        save_group_state(chat_id)

        msg = (
            "✅ 已清除当前账期所有数据\n\n"
            f"📥 入金记录：{in_count} 笔\n"
            f"📤 出金 + 下发记录：{out_count} 笔\n"
            f"🧾 清除前应下发：{fmt_usdt(totals['should'])}\n"
            f"📤 清除前已下发：{fmt_usdt(totals['sent'])}"
        )
        await update.message.reply_text(msg)
        await update.message.reply_text(render_group_summary(chat_id))
        return

    # ========== 撤销入金（撤销最近一笔入金） ==========
    if text == "撤销入金":
        rec_in = state["recent"]["in"]
        if not rec_in:
            await update.message.reply_text("ℹ️ 当前账期暂无入金记录，无需撤销")
            return
        last = rec_in.pop(0)
        save_group_state(chat_id)
        append_log(
            log_path(chat_id, last.get("country"), dstr),
            f"[撤销入金] 时间:{ts} 原始:{last.get('raw')} USDT:{last.get('usdt')} 备注:{last.get('peer','')}",
        )
        await update.message.reply_text(f"✅ 已撤销最近一笔入金：{last.get('raw')} → {last.get('usdt')} USDT")
        await update.message.reply_text(render_group_summary(chat_id))
        return

    # ========== 撤销出金（撤销最近一笔普通出金） ==========
    if text == "撤销出金":
        rec_out = state["recent"]["out"]
        target_idx = None
        for idx, r in enumerate(rec_out):
            if r.get("type") != "下发":
                target_idx = idx
                break
        if target_idx is None:
            await update.message.reply_text("ℹ️ 当前账期暂无出金记录，无需撤销")
            return
        last = rec_out.pop(target_idx)
        save_group_state(chat_id)
        append_log(
            log_path(chat_id, last.get("country"), dstr),
            f"[撤销出金] 时间:{ts} 原始:{last.get('raw')} USDT:{last.get('usdt')} 手续费:{last.get('fee_usdt',0)} 备注:{last.get('peer','')}",
        )
        await update.message.reply_text(f"✅ 已撤销最近一笔出金：{last.get('raw')} → {last.get('usdt')} USDT")
        await update.message.reply_text(render_group_summary(chat_id))
        return

    # ========== 撤销下发（撤销最近一笔下发） ==========
    if text == "撤销下发":
        rec_out = state["recent"]["out"]
        target_idx = None
        for idx, r in enumerate(rec_out):
            if r.get("type") == "下发":
                target_idx = idx
                break
        if target_idx is None:
            await update.message.reply_text("ℹ️ 当前账期暂无下发记录，无需撤销")
            return
        last = rec_out.pop(target_idx)
        save_group_state(chat_id)
        append_log(
            log_path(chat_id, None, dstr),
            f"[撤销下发] 时间:{ts} USDT:{last.get('usdt')} 备注:{last.get('peer','')}",
        )
        await update.message.reply_text(f"✅ 已撤销最近一笔下发记录：{last.get('usdt')} USDT")
        await update.message.reply_text(render_group_summary(chat_id))
        return

    # ========== 入金 ==========
    if text.startswith("+"):
        amt, country = parse_amount_and_country(text)
        if amt is None:
            return
        p = resolve_params(chat_id, "in", country)
        if p["fx"] == 0:
            await update.message.reply_text("⚠️ 请先设置入金费率和汇率")
            return

        usdt = trunc2(amt * (1 - p["rate"]) / p["fx"])
        item = {
            "ts": ts,
            "raw": amt,
            "usdt": usdt,
            "country": country,
            "fx": p["fx"],
            "rate": p["rate"],
        }
        if peer6:
            item["peer"] = peer6

        push_recent(chat_id, "in", item)

        append_log(
            log_path(chat_id, country, dstr),
            f"[入金] 时间:{ts} 国家:{country or '通用'} 原始:{amt} 汇率:{p['fx']} 费率:{p['rate']*100:.4f}% 结果:{usdt} 备注:{peer6}",
        )
        await update.message.reply_text(render_group_summary(chat_id))
        return

    # ========== 出金（+ 可配置手续费） ==========
    if text.startswith("-"):
        amt, country = parse_amount_and_country(text)
        if amt is None:
            return
        p = resolve_params(chat_id, "out", country)
        if p["fx"] == 0:
            await update.message.reply_text("⚠️ 请先设置出金费率和汇率")
            return

        fee_usdt = float(state["defaults"]["out"].get("fee_usdt", 0.0))
        base_usdt = round2(amt * (1 + p["rate"]) / p["fx"])
        usdt = round2(base_usdt + fee_usdt) if fee_usdt > 0 else base_usdt

        item = {
            "ts": ts,
            "raw": amt,
            "usdt": usdt,
            "base_usdt": base_usdt,
            "fee_usdt": round2(fee_usdt),
            "country": country,
            "fx": p["fx"],
            "rate": p["rate"],
        }
        if peer6:
            item["peer"] = peer6

        push_recent(chat_id, "out", item)

        append_log(
            log_path(chat_id, country, dstr),
            f"[出金] 时间:{ts} 国家:{country or '通用'} 原始:{amt} 汇率:{p['fx']} 费率:{p['rate']*100:.4f}% 基础:{base_usdt} 手续费:{fee_usdt} 合计:{usdt} 备注:{peer6}",
        )
        await update.message.reply_text(render_group_summary(chat_id))
        return

    # ========== 下发记录（保留正负，且展示时原样显示） ==========
    if text.startswith("下发"):
        usdt_str = text.replace("下发", "", 1).strip()
        if not usdt_str:
            await update.message.reply_text("❌ 格式：下发100 或 下发-100")
            return
        try:
            usdt = trunc2(float(usdt_str))  # 保留正负
            item = {"ts": ts, "usdt": usdt, "type": "下发"}
            if peer6:
                item["peer"] = peer6

            push_recent(chat_id, "out", item)
            append_log(
                log_path(chat_id, None, dstr),
                f"[下发] 时间:{ts} 金额:{usdt} 备注:{peer6}",
            )
            await update.message.reply_text(render_group_summary(chat_id))
            return
        except ValueError:
            await update.message.reply_text("❌ 格式错误，请输入有效数字，例如：下发100 或 下发-100")
            return

    # 其他消息忽略
    return


# ========== HTTP 健康检查 ==========
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ["/", "/health"]:
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


# ========== 初始化 ==========
def init_bot():
    print("=" * 50)
    print("🚀 正在启动财务记账机器人...")
    print("=" * 50)

    if not BOT_TOKEN:
        print("❌ 错误：未找到 TELEGRAM_BOT_TOKEN 环境变量")
        exit(1)

    print("✅ Bot Token 已加载")
    print(f"📊 数据目录: {DATA_DIR}")
    print(
        f"⭐ 超级管理员列表: {', '.join(str(i) for i in sorted(SUPER_ADMINS)) or '未设置（请配置 OWNER_ID / SUPER_ADMINS）'}"
    )

    port = int(os.getenv("PORT", "10000"))
    print(f"\n🌐 启动 HTTP 健康检查服务器（端口 {port}）...")

    def run_http_server():
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        print(f"✅ HTTP 服务器已启动: http://0.0.0.0:{port}")
        server.serve_forever()

    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    print("\n🤖 配置 Telegram Bot (Polling 模式)...")
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            handle_text,
        )
    )
    print("✅ Bot 处理器已注册")
    print("\n🎉 机器人正在运行，等待消息...")
    print("=" * 50)
    application.run_polling()


if __name__ == "__main__":
    init_bot()
