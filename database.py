import os
import json
import threading
from typing import Dict, Any, List


class FinanceDB:
    """
    JSON 文件数据库（每个用户一个文件）

    data/
      └── user_<user_id>.json

    文件示例:
    {
      "user_id": 6851029179,
      "transactions": [
        {
          "id": 1,
          "date": "2025-11-18",
          "time": "21:59",
          "amount": 1000.0,
          "type": "in",      # "in" / "out"
          "raw": "+1千"
        }
      ]
    }
    """

    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        self._lock = threading.Lock()

    # ---------- 基础 ----------

    def init_database(self):
        """初始化数据目录"""
        os.makedirs(self.data_dir, exist_ok=True)

    def _user_file(self, user_id: int) -> str:
        return os.path.join(self.data_dir, f"user_{user_id}.json")

    def _load_user_data(self, user_id: int) -> Dict[str, Any]:
        path = self._user_file(user_id)
        if not os.path.exists(path):
            return {"user_id": user_id, "transactions": []}

        with open(path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {"user_id": user_id, "transactions": []}

        if "transactions" not in data:
            data["transactions"] = []
        return data

    def _save_user_data(self, user_id: int, data: Dict[str, Any]):
        path = self._user_file(user_id)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    # ---------- 业务方法 ----------

    def add_transaction(
        self,
        user_id: int,
        date_str: str,
        time_str: str,
        amount: float,
        t_type: str,
        raw_text: str = "",
    ) -> None:
        """
        新增一条交易记录

        :param t_type: "in" / "out"
        """
        with self._lock:
            data = self._load_user_data(user_id)
            txs: List[Dict[str, Any]] = data.get("transactions", [])
            next_id = (txs[-1]["id"] + 1) if txs else 1

            tx = {
                "id": next_id,
                "date": date_str,
                "time": time_str,
                "amount": float(amount),
                "type": t_type,
                "raw": raw_text,
            }
            txs.append(tx)
            data["transactions"] = txs
            self._save_user_data(user_id, data)

    def get_day_transactions(self, user_id: int, date_str: str) -> List[Dict[str, Any]]:
        """获取某一天所有交易记录"""
        with self._lock:
            data = self._load_user_data(user_id)
            txs: List[Dict[str, Any]] = data.get("transactions", [])
            return [t for t in txs if t.get("date") == date_str]

    def get_day_summary(self, user_id: int, date_str: str) -> Dict[str, float]:
        """当天入账 / 出账汇总"""
        txs = self.get_day_transactions(user_id, date_str)
        total_in = 0.0
        total_out = 0.0
        for t in txs:
            if t.get("type") == "in":
                total_in += float(t.get("amount", 0.0))
            else:
                total_out += float(t.get("amount", 0.0))
        return {
            "total_in": total_in,
            "total_out": total_out,
            "net": total_in - total_out,
        }


# ---------- 兼容旧启动脚本：提供 module 级别的 init_database() ----------

_default_db = FinanceDB(data_dir="data")


def init_database():
    """
    兼容旧脚本里使用 from database import init_database 的调用。
    只做一件事：确保 data/ 目录存在。
    """
    _default_db.init_database()
