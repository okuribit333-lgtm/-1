"""
エアドロップ情報自動収集
- Airdrops.io スクレイピング（Solana対応エアドロ）
- DeFiLlama Protocol情報
- 各プロトコルのTwitter監視（Nitter経由）

全て無料で動作
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class AirdropInfo:
    """エアドロップ情報"""
    name: str
    platform: str  # "solana" etc
    description: str = ""
    url: str = ""
    status: str = "active"  # active / upcoming / ended
    estimated_value: str = ""
    requirements: list = field(default_factory=list)
    source: str = ""

    def __repr__(self):
        return f"<Airdrop: {self.name} | {self.platform} | {self.status}>"


class AirdropScanner:
    """エアドロップ情報を複数ソースから収集"""

    # Nitterインスタンス（Twitterモニタリング用）
    NITTER_INSTANCES = [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
    ]

    # エアドロ関連キーワード
    AIRDROP_KEYWORDS = [
        "airdrop", "claim", "token distribution",
        "retroactive", "points program", "rewards",
    ]

    # Solanaエコシステムの主要プロトコル（エアドロ期待）
    SOLANA_PROTOCOLS_TO_WATCH = [
        "jupiter", "marginfi", "kamino", "drift",
        "tensor", "jito", "sanctum", "phantom",
        "backpack", "zeta", "parcl", "meteora",
    ]

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def scan_all(self) -> list[AirdropInfo]:
        """全ソースからエアドロ情報を収集"""
        results = await asyncio.gather(
            self._scrape_airdrops_io(),
            self._check_defi_protocols(),
            self._monitor_twitter(),
            return_exceptions=True,
        )

        all_airdrops = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning(f"Airdrop source {i} error: {r}")
            else:
                all_airdrops.extend(r)

        # 重複排除（名前ベース）
        seen = set()
        unique = []
        for a in all_airdrops:
            key = a.name.lower().strip()
            if key not in seen:
                seen.add(key)
                unique.append(a)

        logger.info(f"エアドロスキャン: {len(unique)}件検出")
        return unique

    async def _scrape_airdrops_io(self) -> list[AirdropInfo]:
        """Airdrops.io からSolana関連のエアドロを取得"""
        airdrops = []
        try:
            url = "https://airdrops.io/speculative/"
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0"}
            ) as resp:
                if resp.status != 200:
                    return airdrops
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")

            # エアドロカード要素を取得
            cards = soup.select(".airdrop-card, .card, article")
            for card in cards[:30]:
                title_el = card.select_one("h3, h2, .title, .card-title")
                desc_el = card.select_one("p, .description, .card-text")
                link_el = card.select_one("a[href]")

                if not title_el:
                    continue

                name = title_el.get_text(strip=True)
                desc = desc_el.get_text(strip=True) if desc_el else ""
                link = link_el.get("href", "") if link_el else ""

                # Solana関連かチェック
                text = f"{name} {desc}".lower()
                is_solana = any(kw in text for kw in ["solana", "sol", "spl", "phantom", "jupiter"])

                if is_solana or any(p in text for p in self.SOLANA_PROTOCOLS_TO_WATCH):
                    airdrops.append(AirdropInfo(
                        name=name,
                        platform="solana",
                        description=desc[:200],
                        url=link,
                        status="active",
                        source="airdrops.io",
                    ))

            logger.info(f"Airdrops.io: {len(airdrops)}件（Solana関連）")
        except Exception as e:
            logger.debug(f"Airdrops.io scrape error: {e}")

        return airdrops

    async def _check_defi_protocols(self) -> list[AirdropInfo]:
        """DeFiLlama経由でSolanaプロトコルのTVL・ポイントプログラム確認"""
        airdrops = []
        try:
            url = "https://api.llama.fi/protocols"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return airdrops
                data = await resp.json()

            # Solanaプロトコルでトークン未発行のもの → エアドロ期待
            for protocol in data:
                chains = protocol.get("chains", [])
                if "Solana" not in chains:
                    continue

                name = protocol.get("name", "")
                symbol = protocol.get("symbol", "")
                tvl = protocol.get("tvl", 0) or 0

                # トークンが"-"またはなし = 未発行 = エアドロ期待
                has_token = symbol and symbol != "-" and symbol != ""

                if not has_token and tvl > 1_000_000:
                    airdrops.append(AirdropInfo(
                        name=name,
                        platform="solana",
                        description=f"TVL: ${tvl:,.0f} | トークン未発行 → エアドロ期待",
                        url=protocol.get("url", ""),
                        status="upcoming",
                        estimated_value=f"TVL ${tvl/1e6:.1f}M",
                        source="defillama",
                    ))

            logger.info(f"DeFiLlama: {len(airdrops)}件（Solana未トークンプロトコル）")
        except Exception as e:
            logger.debug(f"DeFiLlama error: {e}")

        return airdrops

    async def _monitor_twitter(self) -> list[AirdropInfo]:
        """Solanaプロトコルのツイートからエアドロ情報を検出"""
        airdrops = []

        for protocol in self.SOLANA_PROTOCOLS_TO_WATCH[:5]:
            for inst in self.NITTER_INSTANCES:
                try:
                    search_url = f"{inst}/search?q={protocol}+airdrop+solana"
                    async with self.session.get(
                        search_url, timeout=aiohttp.ClientTimeout(total=8),
                        headers={"User-Agent": "Mozilla/5.0"}
                    ) as resp:
                        if resp.status != 200:
                            continue
                        html = await resp.text()

                    soup = BeautifulSoup(html, "html.parser")
                    tweets = soup.select(".timeline-item")

                    if tweets:
                        # 直近のツイートにエアドロ言及あり
                        for tweet in tweets[:3]:
                            text = tweet.get_text(strip=True).lower()
                            if any(kw in text for kw in self.AIRDROP_KEYWORDS):
                                airdrops.append(AirdropInfo(
                                    name=f"{protocol.capitalize()} Airdrop",
                                    platform="solana",
                                    description=tweet.get_text(strip=True)[:200],
                                    status="active",
                                    source=f"twitter/{protocol}",
                                ))
                                break
                    break  # 1つのNitterで成功したら次のプロトコルへ
                except Exception:
                    continue

            await asyncio.sleep(0.5)

        logger.info(f"Twitter: {len(airdrops)}件")
        return airdrops
