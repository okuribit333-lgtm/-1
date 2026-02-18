"""
市場イベント監視モジュール
- TGE（Token Generation Event）検知
- NFTフロア急変監視
- Memeチャート監視（急騰検知）

全て「通知のみ」。自動売買はしない。
"""
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ============================================================
# 1. TGE（Token Generation Event）監視
# ============================================================

@dataclass
class TGEEvent:
    """TGEイベント"""
    name: str
    symbol: str = ""
    platform: str = ""  # "pump.fun", "raydium", "jupiter lfo"
    token_address: str = ""
    launch_time: Optional[datetime] = None
    initial_mcap: float = 0.0
    initial_liquidity: float = 0.0
    source: str = ""


class TGEMonitor:
    """
    新規トークンローンチイベントを監視
    DexScreener新規ペア + CoinGecko new coins + Pump.fun卒業を統合

    「新規で出てきた瞬間」を捕まえるのが目的
    """

    DEXSCREENER_API = "https://api.dexscreener.com"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.seen_tokens: set = set()

    async def check_new_launches(self, max_age_minutes: int = 30) -> list[TGEEvent]:
        """直近N分以内の新規トークンローンチを検出"""
        events = []

        # DexScreener最新プロフィール
        try:
            url = f"{self.DEXSCREENER_API}/token-profiles/latest/v1"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data:
                        if item.get("chainId") != "solana":
                            continue
                        addr = item.get("tokenAddress", "")
                        if addr in self.seen_tokens:
                            continue

                        self.seen_tokens.add(addr)
                        events.append(TGEEvent(
                            name=item.get("description", "New Token"),
                            token_address=addr,
                            platform="dexscreener",
                            source="dexscreener_profiles",
                        ))
        except Exception as e:
            logger.debug(f"TGE DexScreener error: {e}")

        # DexScreenerブーストされた新規
        try:
            url = f"{self.DEXSCREENER_API}/token-boosts/latest/v1"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data:
                        if item.get("chainId") != "solana":
                            continue
                        addr = item.get("tokenAddress", "")
                        if addr in self.seen_tokens:
                            continue

                        self.seen_tokens.add(addr)
                        events.append(TGEEvent(
                            name=f"Boosted: {addr[:8]}...",
                            token_address=addr,
                            platform="dexscreener",
                            source="dexscreener_boosts",
                        ))
        except Exception as e:
            logger.debug(f"TGE boosts error: {e}")

        # 各TGEの詳細を取得
        for event in events:
            await self._enrich_tge(event)
            await asyncio.sleep(0.2)

        # 古いseen_tokensをクリーンアップ（1000件超えたら半分消す）
        if len(self.seen_tokens) > 1000:
            self.seen_tokens = set(list(self.seen_tokens)[-500:])

        if events:
            logger.info(f"TGE: {len(events)}件の新規ローンチ検出")

        return events

    async def _enrich_tge(self, event: TGEEvent):
        """TGEイベントの詳細を取得"""
        if not event.token_address:
            return

        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{event.token_address}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

            pairs = data.get("pairs", [])
            if not pairs:
                return

            pair = pairs[0]
            event.name = pair.get("baseToken", {}).get("name", event.name)
            event.symbol = pair.get("baseToken", {}).get("symbol", "")
            event.initial_mcap = float(pair.get("marketCap", 0) or 0)
            event.initial_liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)

            dex = pair.get("dexId", "")
            event.platform = dex

        except Exception as e:
            logger.debug(f"TGE enrich error: {e}")


# ============================================================
# 2. NFTフロア価格監視
# ============================================================

@dataclass
class NFTFloorAlert:
    """NFTフロアアラート"""
    collection: str
    symbol: str
    prev_floor: float  # SOL
    current_floor: float
    change_pct: float
    alert_type: str  # "pump" / "dump" / "new_floor"
    volume_24h: float = 0.0


class NFTFloorMonitor:
    """
    NFTコレクションのフロア価格変動を監視
    急騰・急落を検知して通知

    環境変数 WATCH_NFTS にコレクションsymbolをカンマ区切りで設定:
    WATCH_NFTS=mad_lads,tensorians,famous_fox_federation
    """

    MAGIC_EDEN_API = "https://api-mainnet.magiceden.dev/v2"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.watch_nfts = self._load_nfts()
        self.prev_floors: dict[str, float] = {}

    def _load_nfts(self) -> list[str]:
        import os
        raw = os.getenv("WATCH_NFTS", "")
        return [n.strip() for n in raw.split(",") if n.strip()]

    async def check_all(self) -> list[NFTFloorAlert]:
        """全監視NFTのフロアを確認"""
        alerts = []

        for symbol in self.watch_nfts:
            try:
                alert = await self._check_collection(symbol)
                if alert:
                    alerts.append(alert)
            except Exception as e:
                logger.debug(f"NFT floor error {symbol}: {e}")
            await asyncio.sleep(0.3)

        return alerts

    async def _check_collection(self, symbol: str) -> Optional[NFTFloorAlert]:
        """1コレクションのフロア確認"""
        try:
            url = f"{self.MAGIC_EDEN_API}/collections/{symbol}/stats"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                         headers={"User-Agent": "SolAutoScreener/2.0"}) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            floor = (data.get("floorPrice", 0) or 0) / 1e9
            volume = (data.get("volumeAll", 0) or 0) / 1e9

            prev = self.prev_floors.get(symbol)
            self.prev_floors[symbol] = floor

            if prev is None or prev == 0 or floor == 0:
                return None

            change_pct = ((floor - prev) / prev) * 100

            if abs(change_pct) >= 15:
                return NFTFloorAlert(
                    collection=symbol, symbol=symbol,
                    prev_floor=prev, current_floor=floor,
                    change_pct=round(change_pct, 1),
                    alert_type="pump" if change_pct > 0 else "dump",
                    volume_24h=volume,
                )

        except Exception as e:
            logger.debug(f"Magic Eden error: {e}")

        return None


# ============================================================
# 3. Memeチャート監視（急騰検知）
# ============================================================

@dataclass
class MemeAlert:
    """Meme急騰アラート"""
    token_address: str
    symbol: str
    name: str
    price_change_5m: float = 0.0
    price_change_1h: float = 0.0
    price_change_24h: float = 0.0
    volume_surge: float = 0.0  # 出来高急増率
    liquidity_usd: float = 0.0
    alert_type: str = ""  # "5m_pump" / "1h_pump" / "volume_surge"
    pair_address: str = ""


class MemeChartMonitor:
    """
    Solana Memeトークンのチャート監視
    急騰・出来高急増を検知して「熱そうなの高速イン」に対応

    DexScreenerのトレンドエンドポイントを活用
    """

    DEXSCREENER_API = "https://api.dexscreener.com"

    # 急騰判定閾値
    THRESHOLDS = {
        "5m_pump": 20,    # 5分で+20%
        "1h_pump": 50,    # 1時間で+50%
        "volume_surge": 300,  # 出来高3倍
    }

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.prev_volumes: dict[str, float] = {}

    async def scan_hot_memes(self, min_liquidity: float = 5000) -> list[MemeAlert]:
        """急騰中のMemeトークンをスキャン"""
        alerts = []

        try:
            # DexScreener trending + search
            url = f"{self.DEXSCREENER_API}/latest/dex/search?q=solana"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return alerts
                data = await resp.json()

            pairs = data.get("pairs", [])

            for pair in pairs:
                if pair.get("chainId") != "solana":
                    continue

                liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
                if liquidity < min_liquidity:
                    continue

                price_5m = float(pair.get("priceChange", {}).get("m5", 0) or 0)
                price_1h = float(pair.get("priceChange", {}).get("h1", 0) or 0)
                price_24h = float(pair.get("priceChange", {}).get("h24", 0) or 0)
                volume_24h = float(pair.get("volume", {}).get("h24", 0) or 0)

                token_addr = pair.get("baseToken", {}).get("address", "")
                symbol = pair.get("baseToken", {}).get("symbol", "???")
                name = pair.get("baseToken", {}).get("name", "")
                pair_addr = pair.get("pairAddress", "")

                alert_type = None

                # 5分急騰
                if price_5m >= self.THRESHOLDS["5m_pump"]:
                    alert_type = "5m_pump"
                # 1時間急騰
                elif price_1h >= self.THRESHOLDS["1h_pump"]:
                    alert_type = "1h_pump"

                # 出来高急増
                prev_vol = self.prev_volumes.get(token_addr, 0)
                if prev_vol > 0 and volume_24h > 0:
                    vol_surge = (volume_24h / prev_vol - 1) * 100
                    if vol_surge >= self.THRESHOLDS["volume_surge"]:
                        alert_type = alert_type or "volume_surge"
                self.prev_volumes[token_addr] = volume_24h

                if alert_type:
                    alerts.append(MemeAlert(
                        token_address=token_addr, symbol=symbol, name=name,
                        price_change_5m=price_5m, price_change_1h=price_1h,
                        price_change_24h=price_24h,
                        volume_surge=vol_surge if prev_vol > 0 else 0,
                        liquidity_usd=liquidity,
                        alert_type=alert_type, pair_address=pair_addr,
                    ))

            # 古いvolume記録をクリーンアップ
            if len(self.prev_volumes) > 500:
                recent = dict(list(self.prev_volumes.items())[-250:])
                self.prev_volumes = recent

        except Exception as e:
            logger.debug(f"Meme chart scan error: {e}")

        if alerts:
            logger.info(f"Meme急騰: {len(alerts)}件検出")

        return alerts
