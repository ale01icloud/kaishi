"""
数据库操作层 - PostgreSQL版本
替代原来的JSON文件存储
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from decimal import Decimal
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


@contextmanager
def get_db_connection():
    """获取数据库连接的上下文管理器"""
    DATABASE_URL = os.environ.get('DATABASE_URL')
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable must be set")

    from urllib.parse import urlparse
    result = urlparse(DATABASE_URL)

    conn = None
    try:
        conn = psycopg2.connect(
            host=result.hostname,
            port=result.port or 5432,
            user=result.username,
            password=result.password,
            database=result.path[1:],
            cursor_factory=RealDictCursor
        )
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        if conn:
            conn.close()


def init_database():
    """初始化数据库表结构"""
    with open('database_schema.sql', 'r', encoding='utf-8') as f:
        schema_sql = f.read()

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql)

    logger.info("✅ Database initialized successfully")


def cleanup_old_transactions(keep_days: int = 30):
    """
    清理 N 天之前的交易记录（按 created_at 字段判断）
    keep_days: 只保留最近 keep_days 天的数据
    """
    cutoff = datetime.utcnow() - timedelta(days=keep_days)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM transactions WHERE created_at < %s RETURNING id",
                (cutoff,)
            )
            deleted_rows = cur.fetchall()
            count = len(deleted_rows)
    logger.info(f"🧹 已删除 {count} 条 {keep_days} 天之前的交易记录，只保留最近 {keep_days} 天的数据")


# ==================== 群组配置相关 ====================

def get_group_config(chat_id: int) -> Dict:
    """获取群组配置"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM groups WHERE chat_id = %s",
                (chat_id,)
            )
            result = cur.fetchone()

            if not result:
                # 创建新群组（默认值为0）
                cur.execute(
                    """INSERT INTO groups (chat_id, in_rate, in_fx, out_rate, out_fx)
                       VALUES (%s, 0, 0, 0, 0) RETURNING *""",
                    (chat_id,)
                )
                result = cur.fetchone()
                conn.commit()

            return dict(result) if result else {}


def update_group_config(chat_id: int, **kwargs):
    """更新群组配置"""
    allowed_fields = ['in_rate', 'in_fx', 'out_rate', 'out_fx',
                      'in_fx_source', 'out_fx_source', 'group_name']

    updates = []
    values = []

    for field, value in kwargs.items():
        if field in allowed_fields:
            updates.append(f"{field} = %s")
            values.append(value)

    if not updates:
        return

    values.append(chat_id)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            sql = f"UPDATE groups SET {', '.join(updates)} WHERE chat_id = %s"
            cur.execute(sql, values)


# ==================== 国家配置相关 ====================

def get_country_config(chat_id: int, country: str) -> Optional[Dict]:
    """获取指定国家的配置"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM group_country_configs WHERE chat_id = %s AND country = %s",
                (chat_id, country)
            )
            result = cur.fetchone()
            return dict(result) if result else None


def set_country_config(chat_id: int, country: str,
                       in_rate=None, in_fx=None, out_rate=None, out_fx=None):
    """设置指定国家的配置"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO group_country_configs 
                   (chat_id, country, in_rate, in_fx, out_rate, out_fx)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (chat_id, country) 
                   DO UPDATE SET 
                       in_rate = COALESCE(EXCLUDED.in_rate, group_country_configs.in_rate),
                       in_fx = COALESCE(EXCLUDED.in_fx, group_country_configs.in_fx),
                       out_rate = COALESCE(EXCLUDED.out_rate, group_country_configs.out_rate),
                       out_fx = COALESCE(EXCLUDED.out_fx, group_country_configs.out_fx),
                       updated_at = CURRENT_TIMESTAMP""",
                (chat_id, country, in_rate, in_fx, out_rate, out_fx)
            )


def get_all_country_configs(chat_id: int) -> List[Dict]:
    """获取群组所有国家的配置"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM group_country_configs WHERE chat_id = %s ORDER BY country",
                (chat_id,)
            )
            return [dict(row) for row in cur.fetchall()]


def delete_country_config(chat_id: int, country: str):
    """删除指定国家的配置"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM group_country_configs WHERE chat_id = %s AND country = %s",
                (chat_id, country)
            )


# ==================== 交易记录相关 ====================

def add_transaction(
    chat_id: int,
    transaction_type: str,
    amount: Decimal,
    rate: Decimal,
    fx: Decimal,
    usdt: Decimal,
    timestamp: str,
    country: str = '通用',
    message_id: Optional[int] = None,
    operator_id: Optional[int] = None,
    operator_name: Optional[str] = None
) -> int:
    """添加交易记录，返回transaction ID"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO transactions 
                   (chat_id, transaction_type, amount, rate, fx, usdt, country, 
                    timestamp, message_id, operator_id, operator_name)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (chat_id, transaction_type, amount, rate, fx, usdt, country,
                 timestamp, message_id, operator_id, operator_name)
            )
            result = cur.fetchone()
            return result['id'] if result else None


def get_recent_transactions(chat_id: int, limit: int = 50) -> List[Dict]:
    """获取最近的交易记录"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM transactions 
                   WHERE chat_id = %s 
                   ORDER BY created_at DESC 
                   LIMIT %s""",
                (chat_id, limit)
            )
            return [dict(row) for row in cur.fetchall()]


def get_today_transactions(chat_id: int) -> List[Dict]:
    """获取今日交易记录（北京时间）"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # 使用created_at字段，数据库存储的是UTC时间
            # 需要转换为北京时间（UTC+8）后判断日期
            cur.execute(
                """SELECT * FROM transactions 
                   WHERE chat_id = %s 
                   AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Shanghai')::date = CURRENT_DATE
                   ORDER BY created_at ASC""",
                (chat_id,)
            )
            return [dict(row) for row in cur.fetchall()]


def update_transaction_message_id(transaction_id: int, message_id: int):
    """更新交易记录的message_id"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE transactions SET message_id = %s WHERE id = %s",
                (message_id, transaction_id)
            )


def delete_transaction_by_message_id(message_id: int) -> Optional[Dict]:
    """通过message_id删除交易（用于撤销功能）"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # 先查询要删除的记录
            cur.execute(
                "SELECT * FROM transactions WHERE message_id = %s",
                (message_id,)
            )
            record = cur.fetchone()

            if record:
                # 删除记录
                cur.execute(
                    "DELETE FROM transactions WHERE message_id = %s",
                    (message_id,)
                )
                return dict(record)

            return None


def clear_today_transactions(chat_id: int) -> Dict:
    """清除今日交易数据，返回统计信息"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # 统计要删除的记录
            cur.execute(
                """SELECT 
                       transaction_type,
                       COUNT(*) as count,
                       SUM(usdt) as total_usdt
                   FROM transactions 
                   WHERE chat_id = %s 
                   AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Shanghai')::date = CURRENT_DATE
                   GROUP BY transaction_type""",
                (chat_id,)
            )
            stats = {
                row['transaction_type']: {
                    'count': row['count'],
                    'usdt': float(row['total_usdt'] or 0)
                }
                for row in cur.fetchall()
            }

            # 删除今日记录
            cur.execute(
                """DELETE FROM transactions 
                   WHERE chat_id = %s 
                   AND (created_at AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Shanghai')::date = CURRENT_DATE""",
                (chat_id,)
            )

            return stats


def get_transactions_summary(chat_id: int) -> Dict:
    """获取交易汇总统计"""
    today_txns = get_today_transactions(chat_id)

    in_usdt = sum(t['usdt'] for t in today_txns if t['transaction_type'] == 'in')
    out_usdt = sum(t['usdt'] for t in today_txns if t['transaction_type'] == 'out')
    send_usdt = sum(t['usdt'] for t in today_txns if t['transaction_type'] == 'send')

    return {
        'in_usdt': float(in_usdt),
        'out_usdt': float(out_usdt),
        'send_usdt': float(send_usdt),
        'should_send': float(in_usdt - out_usdt),
        'unsent': float(in_usdt - out_usdt - send_usdt),
        'in_records': [t for t in today_txns if t['transaction_type'] == 'in'],
        'out_records': [t for t in today_txns if t['transaction_type'] == 'out'],
        'send_records': [t for t in today_txns if t['transaction_type'] == 'send']
    }


# ==================== 管理员相关 ====================

def add_admin(user_id: int, username: str = None,
              first_name: str = None, is_owner: bool = False):
    """添加管理员"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO admins (user_id, username, first_name, is_owner)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE 
                   SET username = EXCLUDED.username, 
                       first_name = EXCLUDED.first_name,
                       is_owner = EXCLUDED.is_owner""",
                (user_id, username, first_name, is_owner)
            )


def remove_admin(user_id: int):
    """移除管理员"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM admins WHERE user_id = %s AND is_owner = FALSE",
                (user_id,)
            )


def get_all_admins() -> List[Dict]:
    """获取所有管理员"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM admins ORDER BY added_at ASC")
            return [dict(row) for row in cur.fetchall()]


def is_admin(user_id: int) -> bool:
    """检查是否为管理员"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM admins WHERE user_id = %s", (user_id,))
            return cur.fetchone() is not None


# ==================== 私聊用户相关 ====================

def add_private_chat_user(user_id: int,
                          username: str = None,
                          first_name: str = None):
    """记录私聊用户"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO private_chat_users (user_id, username, first_name)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE 
                   SET username = EXCLUDED.username, 
                       first_name = EXCLUDED.first_name,
                       last_message_at = CURRENT_TIMESTAMP""",
                (user_id, username, first_name)
            )


def get_all_private_chat_users() -> List[Dict]:
    """获取所有私聊过的用户"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM private_chat_users ORDER BY last_message_at DESC"
            )
            return [dict(row) for row in cur.fetchall()]
