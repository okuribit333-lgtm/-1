"""
Pump.fun卒業検知モジュール
移行アカウント 39azUYF... を監視して卒業トークンを検出

2025年3月以降: ほとんどのトークンはPumpSwapに移行（95%+）
一部はまだRaydiumにも移行する

方式: Solana RPC ポーリング（WebSocketは接続維持が不安定なためポーリングを採用）
無料RPC: Chainstack / Helius Free Tier / 公式RPC で動作
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from dataclasses import dataclass

import aiohttp

from .config import config

logger = logging.getLogger(__name__)

# Pump.fun 移行アカウント
PUMPFUN_MIGRATION_ACCOUNT = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"

# プログラムID
RAYDIUM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# 無料Solana RPCエンドポイント
SOLANA_RPC_ENDPOINTS = [
    "https://api.mainnet-beta.solana.com",
]


@dataclass
class GraduatedToken:
    """卒業したトークン"""
    token_address: str
    pool_address: str
    destination: str  # "pumpswap" or "raydium"
    signature: str
    slot: int
    timestamp: datetime


class PumpFunGraduationMonitor:
    """
    Pump.fun → PumpSwap/Raydium 卒業トークンの検出
    DexScreenerがインデックスするより前に検知可能
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.rpc_url = self._get_rpc_url()
        self.last_signature: Optional[str] = None

    def _get_rpc_url(self) -> str:
        """利用可能なRPC URLを選択"""
        helius_key = getattr(config, 'helius_api_key', '')
        if helius_key:
            return f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        return SOLANA_RPC_ENDPOINTS[0]

    async def check_recent_graduations(self, limit: int = 20) -> list[GraduatedToken]:
        """最近の卒業トークンを取得"""
        graduated = []

        try:
            # 移行アカウントの最新トランザクションを取得
            signatures = await self._get_signatures(limit)
            if not signatures:
                return graduated

            for sig_info in signatures:
                sig = sig_info.get("signature", "")
                if sig == self.last_signature:
                    break

                tx = await self._get_transaction(sig)
                if not tx:
                    continue

                token = self._parse_graduation(tx, sig_info)
                if token:
                    graduated.append(token)
                    logger.info(f"  🎓 卒業検出: {token.token_address[:8]}... → {token.destination}")

                await asyncio.sleep(0.2)  # レート制限対策

            # 最新シグネチャを記録
            if signatures:
                self.last_signature = signatures[0].get("signature")

        except Exception as e:
            logger.error(f"Pump.fun卒業検知エラー: {e}")

        return graduated

    async def _get_signatures(self, limit: int) -> list:
        """移行アカウントの最新トランザクションシグネチャを取得"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [
                PUMPFUN_MIGRATION_ACCOUNT,
                {"limit": limit}
            ]
        }

        try:
            async with self.session.post(
                self.rpc_url, json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("result", [])
        except Exception as e:
            logger.debug(f"RPC getSignatures error: {e}")
            return []

    async def _get_transaction(self, signature: str) -> Optional[dict]:
        """トランザクションの詳細を取得"""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0
                }
            ]
        }

        try:
            async with self.session.post(
                self.rpc_url, json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("result")
        except Exception:
            return None

    def _parse_graduation(self, tx: dict, sig_info: dict) -> Optional[GraduatedToken]:
        """トランザクションから卒業情報をパース"""
        if not tx or tx.get("meta", {}).get("err"):
            return None

        try:
            message = tx.get("transaction", {}).get("message", {})
            instructions = message.get("instructions", [])
            inner_instructions = tx.get("meta", {}).get("innerInstructions", [])

            # アカウントキーを取得
            account_keys = []
            for ak in message.get("accountKeys", []):
                if isinstance(ak, dict):
                    account_keys.append(ak.get("pubkey", ""))
                else:
                    account_keys.append(str(ak))

            destination = None
            token_address = None
            pool_address = None

            # 外部命令からプログラムを確認
            for ix in instructions:
                program_id = ix.get("programId", "")
                if program_id == RAYDIUM_PROGRAM:
                    destination = "raydium"
                elif program_id == PUMPFUN_PROGRAM:
                    destination = "pumpswap"

            # 内部命令も確認
            if not destination:
                for inner in inner_instructions:
                    for ix in inner.get("instructions", []):
                        program_id = ix.get("programId", "")
                        if program_id == RAYDIUM_PROGRAM:
                            destination = "raydium"
                            break
                        elif program_id == PUMPFUN_PROGRAM:
                            destination = "pumpswap"
                            break

            if not destination:
                return None

            # トークンアドレスを特定（トークン転送から）
            pre_balances = tx.get("meta", {}).get("preTokenBalances", [])
            post_balances = tx.get("meta", {}).get("postTokenBalances", [])
            for bal in post_balances:
                mint = bal.get("mint", "")
                if mint and mint != "So11111111111111111111111111111111111111112":
                    token_address = mint
                    break

            if not token_address:
                return None

            slot = sig_info.get("slot", 0)
            block_time = tx.get("blockTime", 0)
            timestamp = datetime.fromtimestamp(block_time, tz=timezone.utc) if block_time else datetime.now(timezone.utc)

            return GraduatedToken(
                token_address=token_address,
                pool_address=pool_address or "",
                destination=destination,
                signature=sig_info.get("signature", ""),
                slot=slot,
                timestamp=timestamp,
            )

        except Exception as e:
            logger.debug(f"Parse graduation error: {e}")
            return None
