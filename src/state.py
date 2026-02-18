"""
状態管理：重複排除 / 履歴保存 / スコア変動追跡
JSONファイルベースで永続化（DB不要）
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from .scanner import SolanaProject

logger = logging.getLogger(__name__)

STATE_FILE = "data/state.json"
HISTORY_FILE = "data/history.json"


class StateManager:
    """
    - 通知済みトークンの追跡（重複排除）
    - スコア履歴の保存
    - 前回比スコア変動の計算
    """

    def __init__(self):
        os.makedirs("data", exist_ok=True)
        self.state = self._load(STATE_FILE, default={"notified": {}})
        self.history = self._load(HISTORY_FILE, default={"scans": []})

    # ============================
    # 重複排除
    # ============================
    def filter_new(self, projects: list[SolanaProject]) -> list[SolanaProject]:
        """通知済みトークンを除外"""
        notified = self.state.get("notified", {})
        new = []
        for p in projects:
            key = p.token_address
            if key in notified:
                prev = notified[key]
                # 24時間以内に通知済み → スキップ
                prev_time = datetime.fromisoformat(prev["last_notified"])
                if datetime.now(timezone.utc) - prev_time < timedelta(hours=24):
                    logger.debug(f"  スキップ（通知済み）: {p.symbol}")
                    continue
            new.append(p)

        logger.info(f"重複排除: {len(projects)}件 → {len(new)}件（新規）")
        return new

    def mark_notified(self, projects: list[SolanaProject]):
        """通知済みとしてマーク"""
        now = datetime.now(timezone.utc).isoformat()
        for p in projects:
            self.state["notified"][p.token_address] = {
                "symbol": p.symbol,
                "name": p.name,
                "score": p.total_score,
                "last_notified": now,
            }
        self._cleanup_old_entries()
        self._save(STATE_FILE, self.state)

    def _cleanup_old_entries(self):
        """7日以上前の通知記録を削除"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        notified = self.state.get("notified", {})
        self.state["notified"] = {
            k: v for k, v in notified.items()
            if datetime.fromisoformat(v["last_notified"]) > cutoff
        }

    # ============================
    # スコア変動追跡
    # ============================
    def get_score_changes(self, projects: list[SolanaProject]) -> dict[str, dict]:
        """
        前回スコアとの差分を計算
        戻り値: {token_address: {"prev": 65.2, "diff": +12.3}} 
        """
        notified = self.state.get("notified", {})
        changes = {}
        for p in projects:
            prev = notified.get(p.token_address)
            if prev and "score" in prev:
                diff = p.total_score - prev["score"]
                changes[p.token_address] = {
                    "prev": prev["score"],
                    "diff": round(diff, 1),
                }
            else:
                changes[p.token_address] = {
                    "prev": None,
                    "diff": None,
                }
        return changes

    # ============================
    # スキャン履歴保存
    # ============================
    def save_scan(self, projects: list[SolanaProject]):
        """スキャン結果を履歴に追加"""
        now = datetime.now(timezone.utc).isoformat()
        scan_record = {
            "timestamp": now,
            "count": len(projects),
            "top": [
                {
                    "symbol": p.symbol,
                    "name": p.name,
                    "address": p.token_address,
                    "score": p.total_score,
                    "liquidity_usd": p.liquidity_usd,
                    "volume_24h_usd": p.volume_24h_usd,
                }
                for p in projects
            ]
        }
        self.history["scans"].append(scan_record)

        # 直近100件のスキャンのみ保持
        if len(self.history["scans"]) > 100:
            self.history["scans"] = self.history["scans"][-100:]

        self._save(HISTORY_FILE, self.history)
        logger.info(f"履歴保存: {len(projects)}件（累計{len(self.history['scans'])}スキャン）")

    # ============================
    # ファイル操作
    # ============================
    @staticmethod
    def _load(path: str, default: dict) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    @staticmethod
    def _save(path: str, data: dict):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"ファイル保存エラー ({path}): {e}")
