"""
ウォレット・流動性・SOLレンジ監視モジュール
全て「通知のみ」。自動売買はしない。

1. Copyウォレット追従: 指定walletのスワップを監視して通知
2. 流動性変動監視: LP引き抜き・急変を検知して通知
3. SOLレンジ監視: SOL価格がレンジ外に出たら通知

データソース: Solana RPC + DexScreener + CoinGecko（全て無料）
"""
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from .config import config

logger = logging.getLogger(__name__)


# ============================================================
# 1. Copyウォレット追従
# ============================================================

@dataclass
class WalletActivity:
    """ウォレットのアクティビティ"""
    wallet: str
    label: str  # "Smart Money A" etc
    action: str  # "buy" / "sell" / "transfer"
    token_address: str = ""
    token_symbol: str = ""
    amount_sol: float = 0.0
    amount_usd: float = 0.0
    signature: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WalletTracker:
    """
    指定ウォレットのトランザクションを監視
    スワップ・大口送金を検出して通知

    環境変数 WATCH_WALLETS に監視対象をカンマ区切りで設定:
    WATCH_WALLETS=wallet1:ラベル1,wallet2:ラベル2
    """

    SOLANA_RPC = "https://api.mainnet-beta.solana.com"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.rpc_url = self._get_rpc()
        self.watch_list = self._load_wallets()
        self.last_signatures: dict[str, str] = {}

    def _get_rpc(self) -> str:
        helius_key = getattr(config, 'helius_api_key', '')
        if helius_key:
            return f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        return self.SOLANA_RPC

    def _load_wallets(self) -> dict[str, str]:
        """環境変数から監視ウォレットを読み込み"""
        import os
        raw = os.getenv("WATCH_WALLETS", "")
        wallets = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if ":" in entry:
                addr, label = entry.split(":", 1)
                wallets[addr.strip()] = label.strip()
            elif entry:
                wallets[entry] = f"Wallet {len(wallets)+1}"
        return wallets

    async def check_all(self) -> list[WalletActivity]:
        """全監視ウォレットのアクティビティを確認"""
        if not self.watch_list:
            return []

        activities = []
        for addr, label in self.watch_list.items():
            try:
                new_activities = await self._check_wallet(addr, label)
                activities.extend(new_activities)
            except Exception as e:
                logger.debug(f"Wallet check error {addr[:8]}: {e}")
            await asyncio.sleep(0.3)

        if activities:
            logger.info(f"ウォレット監視: {len(activities)}件の新規アクティビティ")
        return activities

    async def _check_wallet(self, address: str, label: str) -> list[WalletActivity]:
        """1ウォレットの最新トランザクションを確認"""
        activities = []

        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": 5}]
        }

        try:
            async with self.session.post(self.rpc_url, json=payload,
                                          timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return activities
                data = await resp.json()

            sigs = data.get("result", [])
            last_seen = self.last_signatures.get(address)

            for sig_info in sigs:
                sig = sig_info.get("signature", "")
                if sig == last_seen:
                    break
                if sig_info.get("err"):
                    continue

                # 新しいTXを検出
                activities.append(WalletActivity(
                    wallet=address,
                    label=label,
                    action="transaction",
                    signature=sig,
                    timestamp=datetime.fromtimestamp(
                        sig_info.get("blockTime", 0), tz=timezone.utc
                    ) if sig_info.get("blockTime") else datetime.now(timezone.utc),
                ))

            if sigs:
                self.last_signatures[address] = sigs[0].get("signature", "")

        except Exception as e:
            logger.debug(f"RPC error: {e}")

        return activities


# ============================================================
# 2. 流動性変動監視
# ============================================================

@dataclass
class LiquidityAlert:
    """流動性アラート"""
    token_address: str
    token_symbol: str
    alert_type: str  # "drop" / "surge" / "removed"
    prev_liquidity: float
    current_liquidity: float
    change_pct: float
    pair_address: str = ""


class LiquidityMonitor:
    """
    監視中トークンの流動性変動を検出
    - LP引き抜き（急落）
    - 流動性急増（ポジティブサイン）
    - 完全除去（ラグプル確定）

    環境変数 WATCH_TOKENS にトークンアドレスをカンマ区切りで設定
    """

    DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.prev_liquidity: dict[str, float] = {}
        self.watch_tokens = self._load_tokens()

    def _load_tokens(self) -> list[str]:
        import os
        raw = os.getenv("WATCH_TOKENS", "")
        return [t.strip() for t in raw.split(",") if t.strip()]

    async def check_all(self) -> list[LiquidityAlert]:
        """全監視トークンの流動性を確認"""
        alerts = []

        for token in self.watch_tokens:
            try:
                alert = await self._check_token(token)
                if alert:
                    alerts.append(alert)
            except Exception as e:
                logger.debug(f"Liquidity check error {token[:8]}: {e}")
            await asyncio.sleep(0.3)

        return alerts

    async def _check_token(self, token_address: str) -> Optional[LiquidityAlert]:
        """1トークンの流動性を確認"""
        try:
            url = f"{self.DEXSCREENER_API}/tokens/{token_address}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            pairs = data.get("pairs", [])
            if not pairs:
                return None

            pair = pairs[0]
            current = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            symbol = pair.get("baseToken", {}).get("symbol", "???")
            pair_addr = pair.get("pairAddress", "")

            prev = self.prev_liquidity.get(token_address)
            self.prev_liquidity[token_address] = current

            if prev is None or prev == 0:
                return None

            change_pct = ((current - prev) / prev) * 100

            # アラート判定
            if change_pct <= -50:
                return LiquidityAlert(
                    token_address=token_address, token_symbol=symbol,
                    alert_type="removed" if current < 1000 else "drop",
                    prev_liquidity=prev, current_liquidity=current,
                    change_pct=round(change_pct, 1), pair_address=pair_addr,
                )
            elif change_pct <= -20:
                return LiquidityAlert(
                    token_address=token_address, token_symbol=symbol,
                    alert_type="drop",
                    prev_liquidity=prev, current_liquidity=current,
                    change_pct=round(change_pct, 1), pair_address=pair_addr,
                )
            elif change_pct >= 100:
                return LiquidityAlert(
                    token_address=token_address, token_symbol=symbol,
                    alert_type="surge",
                    prev_liquidity=prev, current_liquidity=current,
                    change_pct=round(change_pct, 1), pair_address=pair_addr,
                )

        except Exception as e:
            logger.debug(f"DexScreener liquidity error: {e}")

        return None


# ============================================================
# 3. SOLレンジ監視
# ============================================================

@dataclass
class RangeAlert:
    """レンジアラート"""
    asset: str  # "SOL"
    current_price: float
    range_low: float
    range_high: float
    breach: str  # "above" / "below"
    change_24h: float


class RangeMonitor:
    """
    SOL（および他銘柄）の価格レンジを監視
    レンジ外に出たら通知

    環境変数:
    SOL_RANGE_LOW=150
    SOL_RANGE_HIGH=220
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.ranges = self._load_ranges()

    def _load_ranges(self) -> dict[str, tuple[float, float]]:
        import os
        ranges = {}
        low = float(os.getenv("SOL_RANGE_LOW", "0"))
        high = float(os.getenv("SOL_RANGE_HIGH", "0"))
        if low > 0 and high > 0:
            ranges["solana"] = (low, high)

        # 他の銘柄も追加可能
        # BTC_RANGE_LOW, BTC_RANGE_HIGH etc
        btc_low = float(os.getenv("BTC_RANGE_LOW", "0"))
        btc_high = float(os.getenv("BTC_RANGE_HIGH", "0"))
        if btc_low > 0 and btc_high > 0:
            ranges["bitcoin"] = (btc_low, btc_high)

        eth_low = float(os.getenv("ETH_RANGE_LOW", "0"))
        eth_high = float(os.getenv("ETH_RANGE_HIGH", "0"))
        if eth_low > 0 and eth_high > 0:
            ranges["ethereum"] = (eth_low, eth_high)

        return ranges

    async def check_all(self) -> list[RangeAlert]:
        """全監視銘柄のレンジを確認"""
        if not self.ranges:
            return []

        alerts = []
        ids = ",".join(self.ranges.keys())

        try:
            url = f"https://api.coingecko.com/api/v3/simple/price"
            params = {"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"}
            async with self.session.get(url, params=params,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return alerts
                data = await resp.json()

            for coin_id, (low, high) in self.ranges.items():
                price_data = data.get(coin_id, {})
                price = price_data.get("usd", 0)
                change = price_data.get("usd_24h_change", 0) or 0

                if price <= 0:
                    continue

                symbol = coin_id.upper()[:3]  # solana→SOL etc

                if price < low:
                    alerts.append(RangeAlert(
                        asset=symbol, current_price=price,
                        range_low=low, range_high=high,
                        breach="below", change_24h=round(change, 2),
                    ))
                    logger.info(f"⚠️ {symbol} レンジ下限割れ: ${price:.2f} < ${low:.2f}")
                elif price > high:
                    alerts.append(RangeAlert(
                        asset=symbol, current_price=price,
                        range_low=low, range_high=high,
                        breach="above", change_24h=round(change, 2),
                    ))
                    logger.info(f"⚠️ {symbol} レンジ上限突破: ${price:.2f} > ${high:.2f}")

        except Exception as e:
            logger.debug(f"Range check error: {e}")

        return alerts
