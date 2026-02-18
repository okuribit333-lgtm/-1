"""
安全性チェック：ラグプル / ハニーポット / LP Lock 検知
Solana公開RPC + RugCheck.xyz API（無料）で動作
"""
import asyncio
import logging
from typing import Optional

import aiohttp

from .scanner import SolanaProject

logger = logging.getLogger(__name__)


class SafetyChecker:
    """
    無料APIでトークンの安全性をチェック
    - RugCheck.xyz: ラグプルリスクスコア（無料、キー不要）
    - Solana RPC: ミント権限確認
    """

    RUGCHECK_API = "https://api.rugcheck.xyz/v1"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def check(self, project: SolanaProject) -> dict:
        """全チェックを実行して結果を返す"""
        results = await asyncio.gather(
            self._rugcheck(project.token_address),
            return_exceptions=True,
        )

        rugcheck = results[0] if not isinstance(results[0], Exception) else {}

        safety = {
            "is_safe": True,
            "risk_level": "unknown",  # safe / warning / danger / unknown
            "warnings": [],
            "rugcheck_score": None,
            "mint_authority": None,
            "lp_locked": None,
            "top_holders_pct": None,
        }

        # RugCheck結果を反映
        if rugcheck:
            score = rugcheck.get("score", 0)
            safety["rugcheck_score"] = score
            risks = rugcheck.get("risks", [])

            # リスク分類
            for risk in risks:
                name = risk.get("name", "")
                level = risk.get("level", "")
                desc = risk.get("description", "")

                if level in ("danger", "critical"):
                    safety["warnings"].append(f"🔴 {name}: {desc}")
                elif level == "warn":
                    safety["warnings"].append(f"🟡 {name}: {desc}")

            # ミント権限
            if any("mint" in r.get("name", "").lower() for r in risks):
                safety["mint_authority"] = "active"
                safety["warnings"].append("🔴 ミント権限が放棄されていない")

            # LP Lock
            lp_locked = not any("lp" in r.get("name", "").lower() and r.get("level") in ("danger", "critical") for r in risks)
            safety["lp_locked"] = lp_locked
            if not lp_locked:
                safety["warnings"].append("🔴 LP未ロック（ラグプルリスク）")

            # トップホルダー集中
            top_holders = rugcheck.get("topHolders", [])
            if top_holders:
                total_pct = sum(h.get("pct", 0) for h in top_holders[:10])
                safety["top_holders_pct"] = round(total_pct, 1)
                if total_pct > 50:
                    safety["warnings"].append(f"🔴 上位10ホルダーが{total_pct:.0f}%保有（集中リスク）")
                elif total_pct > 30:
                    safety["warnings"].append(f"🟡 上位10ホルダーが{total_pct:.0f}%保有")

            # リスクレベル判定
            danger_count = sum(1 for w in safety["warnings"] if w.startswith("🔴"))
            warn_count = sum(1 for w in safety["warnings"] if w.startswith("🟡"))

            if danger_count >= 2:
                safety["risk_level"] = "danger"
                safety["is_safe"] = False
            elif danger_count >= 1:
                safety["risk_level"] = "warning"
            elif warn_count >= 2:
                safety["risk_level"] = "warning"
            else:
                safety["risk_level"] = "safe"

        return safety

    async def _rugcheck(self, token_address: str) -> dict:
        """RugCheck.xyz APIからトークンレポートを取得"""
        try:
            url = f"{self.RUGCHECK_API}/tokens/{token_address}/report/summary"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"  RugCheck: score={data.get('score', 'N/A')}, risks={len(data.get('risks', []))}")
                    return data
                else:
                    logger.debug(f"  RugCheck: status={resp.status}")
                    return {}
        except Exception as e:
            logger.debug(f"  RugCheck error: {e}")
            return {}

    async def check_multiple(self, projects: list[SolanaProject]) -> dict[str, dict]:
        """複数プロジェクトを一括チェック"""
        tasks = [(p.token_address, self.check(p)) for p in projects]
        results = {}
        for addr, task in tasks:
            try:
                results[addr] = await task
            except Exception as e:
                logger.warning(f"Safety check failed for {addr}: {e}")
                results[addr] = {"is_safe": True, "risk_level": "unknown", "warnings": []}
        return results
