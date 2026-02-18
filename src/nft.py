"""
NFTミント監視モジュール
Magic Eden API（無料）で新規Solana NFTコレクションを検出

- 新規コレクション検出
- ミント進捗率
- フロア価格推移
- ホルダー数
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class NFTCollection:
    """NFTコレクション"""
    symbol: str
    name: str
    description: str = ""
    image: str = ""

    # マーケットデータ
    floor_price: float = 0.0  # SOL
    listed_count: int = 0
    volume_all: float = 0.0  # SOL
    avg_price_24h: float = 0.0

    # ミント情報
    total_supply: int = 0
    holder_count: int = 0

    # スコア
    scores: dict = field(default_factory=dict)
    total_score: float = 0.0

    def __repr__(self):
        return f"<NFT: {self.name} | Floor: {self.floor_price:.2f} SOL | Supply: {self.total_supply}>"


class MagicEdenScanner:
    """Magic Eden API でSolana NFTを監視"""

    BASE = "https://api-mainnet.magiceden.dev/v2"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.headers = {"User-Agent": "SolAutoScreener/2.0"}

    async def fetch_new_collections(self, limit: int = 20) -> list[NFTCollection]:
        """最近リストされた新規コレクションを取得"""
        collections = []

        try:
            # 人気コレクション（新しい順にソート）
            url = f"{self.BASE}/collections"
            params = {"offset": 0, "limit": limit}

            async with self.session.get(url, params=params, headers=self.headers,
                                         timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.warning(f"Magic Eden API: status={resp.status}")
                    return []
                data = await resp.json()

            for item in data:
                col = NFTCollection(
                    symbol=item.get("symbol", ""),
                    name=item.get("name", "Unknown"),
                    description=item.get("description", ""),
                    image=item.get("image", ""),
                )
                collections.append(col)

            # 各コレクションの詳細を取得
            tasks = [self._enrich(col) for col in collections]
            await asyncio.gather(*tasks, return_exceptions=True)

            # フィルタ: フロア価格 > 0 のみ
            collections = [c for c in collections if c.floor_price > 0]

            logger.info(f"NFTスキャン: {len(data)}件 → フィルタ後 {len(collections)}件")

        except Exception as e:
            logger.error(f"Magic Eden scan error: {e}")

        return collections

    async def _enrich(self, col: NFTCollection):
        """コレクションの詳細データを取得"""
        try:
            url = f"{self.BASE}/collections/{col.symbol}/stats"
            async with self.session.get(url, headers=self.headers,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                stats = await resp.json()

            col.floor_price = (stats.get("floorPrice", 0) or 0) / 1e9  # lamports → SOL
            col.listed_count = stats.get("listedCount", 0) or 0
            col.volume_all = (stats.get("volumeAll", 0) or 0) / 1e9
            col.avg_price_24h = (stats.get("avgPrice24hr", 0) or 0) / 1e9

        except Exception as e:
            logger.debug(f"Enrich error for {col.symbol}: {e}")

    def score_collection(self, col: NFTCollection) -> float:
        """NFTコレクションをスコアリング"""
        import math

        # フロア価格スコア（0.1-100 SOLが理想的）
        if col.floor_price > 0:
            floor_score = min(100, math.log10(max(0.01, col.floor_price)) * 30 + 60)
        else:
            floor_score = 0

        # 出来高スコア
        vol_score = min(100, math.log10(max(1, col.volume_all)) * 20) if col.volume_all > 0 else 0

        # リスト率（上場数 / 供給量 → 低い方が良い = ホルダーが売りたくない）
        if col.total_supply > 0 and col.listed_count > 0:
            list_ratio = col.listed_count / col.total_supply
            list_score = max(0, 100 - list_ratio * 200)
        else:
            list_score = 50

        total = floor_score * 0.35 + vol_score * 0.35 + list_score * 0.30
        col.total_score = round(total, 1)
        col.scores = {
            "floor": round(floor_score, 1),
            "volume": round(vol_score, 1),
            "list_ratio": round(list_score, 1),
        }

        return col.total_score

    async def get_top_collections(self, limit: int = 5) -> list[NFTCollection]:
        """スコア上位のNFTコレクションを返す"""
        collections = await self.fetch_new_collections(limit=30)

        for col in collections:
            self.score_collection(col)

        collections.sort(key=lambda c: c.total_score, reverse=True)
        return collections[:limit]
