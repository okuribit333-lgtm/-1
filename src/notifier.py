"""
通知：Discord Webhook / Telegram Bot / LINE Notify
3チャネル同時配信。各チャネル独立動作。
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from .config import config
from .scanner import SolanaProject

logger = logging.getLogger(__name__)
JST = timezone(timedelta(hours=9))


class DiscordNotifier:
    """Discord Webhook（Embed形式）"""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.url = config.discord_webhook_url
        self.enabled = bool(self.url)

    async def send(self, projects: list[SolanaProject], score_changes: dict = None, safety_results: dict = None):
        if not self.enabled:
            return

        score_changes = score_changes or {}
        safety_results = safety_results or {}
        now = datetime.now(JST)
        embeds = []

        for i, p in enumerate(projects, 1):
            raw = p.scores
            onchain = raw.get("_onchain_raw", {})
            tw = raw.get("_twitter_raw", {})
            dc = raw.get("_discord_raw", {})

            # スコアバー + 前回比
            bar_len = int(p.total_score / 5)
            score_bar = "█" * bar_len + "░" * (20 - bar_len)
            change = score_changes.get(p.token_address, {})
            diff = change.get("diff")
            diff_str = f" ({diff:+.1f})" if diff is not None else " 🆕"
            score_text = f"**スコア: {p.total_score:.1f}/100{diff_str}**\n`{score_bar}`"

            # 安全性
            safety = safety_results.get(p.token_address, {})
            risk = safety.get("risk_level", "unknown")
            risk_emoji = {"safe": "🟢", "warning": "🟡", "danger": "🔴"}.get(risk, "⚪")
            warnings = safety.get("warnings", [])

            if warnings:
                score_text += f"\n{risk_emoji} **安全性: {risk.upper()}**"
                for w in warnings[:3]:  # 最大3件
                    score_text += f"\n　{w}"

            fields = [
                {"name": "💰 流動性", "value": f"${onchain.get('liquidity_usd', 0):,.0f}", "inline": True},
                {"name": "📊 24h出来高", "value": f"${onchain.get('volume_24h_usd', 0):,.0f}", "inline": True},
                {"name": "📈 24h変動", "value": f"{onchain.get('price_change_24h', 0):+.1f}%", "inline": True},
            ]

            # ホルダー集中率
            top_pct = safety.get("top_holders_pct")
            if top_pct is not None:
                fields.append({"name": "👥 Top10ホルダー", "value": f"{top_pct:.0f}%", "inline": True})

            if tw and tw.get("followers") not in (None, "unknown", 0):
                fields.append({"name": "🐦 Twitter", "value": f"[@{tw.get('handle','')}](https://x.com/{tw.get('handle','')}) ({tw.get('followers',0):,}人)", "inline": True})
            if dc:
                fields.append({"name": "💬 Discord", "value": f"{dc.get('members',0):,}人 (Online {dc.get('online_ratio',0):.0f}%)", "inline": True})

            # 色: 安全性も考慮
            if risk == "danger":
                color = 0xFF0000
            elif p.total_score >= 60:
                color = 0x00FF00
            elif p.total_score >= 40:
                color = 0xFFFF00
            else:
                color = 0xFF6600

            embeds.append({
                "title": f"#{i} {p.name} (${p.symbol})",
                "description": score_text,
                "url": f"https://dexscreener.com/solana/{p.pair_address}",
                "color": color,
                "fields": fields,
                "footer": {"text": f"DEX: {p.dex} | 作成: {p.created_at.strftime('%m/%d %H:%M')} UTC"},
            })

        payload = {
            "content": f"🔍 Solana新規プロジェクト TOP{len(projects)} ({now.strftime('%Y/%m/%d %H:%M')} JST)",
            "embeds": embeds[:10],
        }

        try:
            async with self.session.post(self.url, json=payload) as resp:
                if resp.status in (200, 204):
                    logger.info(f"Discord通知送信完了 ({len(embeds)}件)")
                else:
                    logger.error(f"Discord通知エラー: {resp.status}")
        except Exception as e:
            logger.error(f"Discord通知例外: {e}")


class TelegramNotifier:
    """Telegram Bot通知"""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.token = config.telegram_bot_token
        self.chat_id = config.telegram_chat_id
        self.enabled = bool(self.token and self.chat_id)

    async def send(self, projects: list[SolanaProject], score_changes: dict = None, safety_results: dict = None):
        if not self.enabled:
            return

        score_changes = score_changes or {}
        safety_results = safety_results or {}
        now = datetime.now(JST)
        lines = [f"🔍 Solana新規プロジェクト TOP{len(projects)}\n📅 {now.strftime('%Y/%m/%d %H:%M')} JST\n"]

        for i, p in enumerate(projects, 1):
            onchain = p.scores.get("_onchain_raw", {})
            change = score_changes.get(p.token_address, {})
            diff = change.get("diff")
            diff_str = f" ({diff:+.1f})" if diff is not None else " 🆕"

            safety = safety_results.get(p.token_address, {})
            risk = safety.get("risk_level", "unknown")
            risk_emoji = {"safe": "🟢", "warning": "🟡", "danger": "🔴"}.get(risk, "⚪")
            warnings = safety.get("warnings", [])

            block = (
                f"#{i} {p.name} (${p.symbol})\n"
                f"   スコア: {p.total_score:.1f}/100{diff_str}\n"
                f"   安全性: {risk_emoji} {risk.upper()}\n"
                f"   流動性: ${onchain.get('liquidity_usd', 0):,.0f}\n"
                f"   出来高: ${onchain.get('volume_24h_usd', 0):,.0f}\n"
                f"   変動: {onchain.get('price_change_24h', 0):+.1f}%\n"
            )
            if warnings:
                for w in warnings[:2]:
                    block += f"   {w}\n"
            block += f"   https://dexscreener.com/solana/{p.pair_address}\n"
            lines.append(block)

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": "\n".join(lines), "disable_web_page_preview": True}

        try:
            async with self.session.post(url, json=payload) as resp:
                if resp.status == 200:
                    logger.info("Telegram通知送信完了")
                else:
                    logger.error(f"Telegram通知エラー: {resp.status}")
        except Exception as e:
            logger.error(f"Telegram通知例外: {e}")


class LINENotifier:
    """LINE Notify通知"""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.token = config.line_notify_token
        self.enabled = bool(self.token)

    async def send(self, projects: list[SolanaProject], score_changes: dict = None, safety_results: dict = None):
        if not self.enabled:
            return

        score_changes = score_changes or {}
        safety_results = safety_results or {}
        now = datetime.now(JST)
        lines = [f"\n🔍 Solana新規プロジェクト TOP{len(projects)}", f"📅 {now.strftime('%Y/%m/%d %H:%M')} JST\n"]

        for i, p in enumerate(projects, 1):
            onchain = p.scores.get("_onchain_raw", {})
            change = score_changes.get(p.token_address, {})
            diff = change.get("diff")
            diff_str = f" ({diff:+.1f})" if diff is not None else " 🆕"

            safety = safety_results.get(p.token_address, {})
            risk = safety.get("risk_level", "unknown")
            risk_emoji = {"safe": "🟢", "warning": "🟡", "danger": "🔴"}.get(risk, "⚪")

            lines.append(
                f"#{i} {p.name} (${p.symbol})\n"
                f"   スコア: {p.total_score:.1f}/100{diff_str}\n"
                f"   安全性: {risk_emoji} {risk.upper()}\n"
                f"   流動性: ${onchain.get('liquidity_usd', 0):,.0f}\n"
                f"   出来高: ${onchain.get('volume_24h_usd', 0):,.0f}\n"
                f"   https://dexscreener.com/solana/{p.pair_address}"
            )

        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with self.session.post(
                "https://notify-api.line.me/api/notify",
                headers=headers, data={"message": "\n".join(lines)}
            ) as resp:
                if resp.status == 200:
                    logger.info("LINE通知送信完了")
                else:
                    logger.error(f"LINE通知エラー: {resp.status}")
        except Exception as e:
            logger.error(f"LINE通知例外: {e}")


class NotificationHub:
    """3チャネル同時配信"""

    def __init__(self, session: aiohttp.ClientSession):
        self.discord = DiscordNotifier(session)
        self.telegram = TelegramNotifier(session)
        self.line = LINENotifier(session)

    async def broadcast(self, projects: list[SolanaProject], score_changes: dict = None, safety_results: dict = None):
        if not projects:
            logger.info("通知対象なし")
            return

        logger.info(f"📢 {len(projects)}件を通知中...")
        results = await asyncio.gather(
            self.discord.send(projects, score_changes, safety_results),
            self.telegram.send(projects, score_changes, safety_results),
            self.line.send(projects, score_changes, safety_results),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"通知エラー [{['Discord','Telegram','LINE'][i]}]: {r}")
        logger.info("📢 通知完了")
