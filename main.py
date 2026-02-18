"""
SOL Auto Screener v3 - フル統合版
リサーチ自動化 → 通知 → 人が判断

3つの監視サイクル:
  1. メインスクリーニング（N分間隔）: 新規トークン発見・スコアリング・通知
  2. リアルタイム監視（5分間隔）: ウォレット/LP/レンジ/Meme急騰/NFTフロア
  3. デイリーレポート（1日1回）: エアドロ/TGE/背景調査

使い方:
  python main.py          → 1回実行（メインスクリーニングのみ）
  python main.py daemon   → 全監視デーモン（Railway / VPS向け）
"""
import asyncio
import logging
import os
import signal
import sys
import traceback
from datetime import datetime, timedelta, timezone

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import config
from src.scanner import DexScreenerScanner
from src.scorer import ScoringEngine
from src.notifier import NotificationHub
from src.state import StateManager
from src.safety import SafetyChecker
from src.mania import ManiaScorer
from src.pumpfun import PumpFunGraduationMonitor
from src.airdrop import AirdropScanner
from src.background import BackgroundInvestigator
from src.expectation import ExpectationCalculator
from src.monitors import WalletTracker, LiquidityMonitor, RangeMonitor
from src.market_events import TGEMonitor, NFTFloorMonitor, MemeChartMonitor

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("screener.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("sol-screener")
JST = timezone(timedelta(hours=9))

# 状態管理（永続化）
state = StateManager()
expectation = ExpectationCalculator()


# ============================================================
# エラーアラート
# ============================================================
async def send_error_alert(error_msg: str):
    try:
        async with aiohttp.ClientSession() as session:
            hub = NotificationHub(session)
            now = datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')
            if hub.discord.enabled:
                try:
                    await session.post(hub.discord.url, json={
                        "content": f"⚠️ **SOL Screener エラー** ({now} JST)\n```\n{error_msg[:1500]}\n```"
                    })
                except Exception:
                    pass
    except Exception:
        pass


async def send_alert(session, hub, text: str, embeds: list = None):
    """汎用アラート送信"""
    if hub.discord.enabled:
        payload = {"content": text}
        if embeds:
            payload["embeds"] = embeds
        try:
            async with session.post(hub.discord.url, json=payload) as resp:
                pass
        except Exception:
            pass
    if hub.telegram.enabled:
        try:
            url = f"https://api.telegram.org/bot{hub.telegram.token}/sendMessage"
            await session.post(url, json={"chat_id": hub.telegram.chat_id, "text": text[:4000]})
        except Exception:
            pass


# ============================================================
# サイクル1: メインスクリーニング（N分間隔）
# ============================================================
async def run_screening_cycle():
    """新規トークン発見 → スコアリング → 安全性 → 期待値 → 通知"""
    now = datetime.now(JST)
    logger.info(f"{'='*50}")
    logger.info(f"🚀 メインスクリーニング: {now.strftime('%Y/%m/%d %H:%M:%S')} JST")

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=180),
            headers={"User-Agent": "SolAutoScreener/3.0"}
        ) as session:

            # Step 1: スキャン
            logger.info("📡 Step 1: 新規プロジェクトスキャン...")
            scanner = DexScreenerScanner(session)
            projects = await scanner.fetch_new_pairs(hours_back=24)

            # Pump.fun卒業
            if config.enable_pumpfun:
                logger.info("🎓 Pump.fun卒業トークン検出...")
                pump = PumpFunGraduationMonitor(session)
                graduated = await pump.check_recent_graduations(limit=10)
                if graduated:
                    logger.info(f"  卒業: {len(graduated)}件")
                    for g in graduated:
                        pair = await scanner._get_pair(g.token_address)
                        if pair and pair.token_address not in {p.token_address for p in projects}:
                            projects.append(pair)

            if not projects:
                logger.info("⚠️ 新規プロジェクトなし")
                return

            for p in projects[:30]:
                await scanner.enrich_github(p)

            # Step 2: スコアリング
            logger.info(f"📊 Step 2: {len(projects)}件スコアリング...")
            engine = ScoringEngine(session)
            scored = await engine.score_projects(projects)
            top = scored[:config.top_n]

            # Step 3: マニア基準
            if config.enable_mania_scoring:
                logger.info("🔬 Step 3: マニア基準スコアリング...")
                mania = ManiaScorer(session)
                for p in top:
                    try:
                        ms = await mania.enhance_scores(p)
                        p.scores.update(ms)
                        mt = ms.get("mania_total", 0)
                        p.total_score = round(p.total_score * 0.8 + mt * 0.2, 1)
                    except Exception:
                        pass
                top.sort(key=lambda x: x.total_score, reverse=True)

            # Step 4: 重複排除
            score_changes = state.get_score_changes(top)
            new_projects = state.filter_new(top)
            if not new_projects:
                logger.info("✅ 新規通知対象なし")
                state.save_scan(top)
                return

            # Step 5: 安全性
            logger.info(f"🛡️ Step 5: {len(new_projects)}件 安全性チェック...")
            checker = SafetyChecker(session)
            safety_results = await checker.check_multiple(new_projects)

            # Step 6: 期待値算出
            logger.info("📈 Step 6: 期待値算出...")
            ev_results = {}
            for p in new_projects:
                safety = safety_results.get(p.token_address, {})
                mania_scores = {k: v for k, v in p.scores.items() if k.startswith("mania") or k.startswith("smart") or k.startswith("holder") or k.startswith("social") or k.startswith("bot")}
                ev = expectation.calculate(
                    total_score=p.total_score,
                    safety_result=safety,
                    mania_scores=mania_scores,
                )
                ev_results[p.token_address] = ev
                logger.info(f"  {p.symbol}: {ev.heat_label} | {ev.position_label} | 確信度{ev.confidence:.0f}%")

            # Step 7: 通知
            logger.info("📢 Step 7: 通知送信...")
            hub = NotificationHub(session)
            await hub.broadcast(new_projects, score_changes, safety_results)

            # 期待値を追加通知（Discord embed）
            if hub.discord.enabled:
                ev_lines = []
                for p in new_projects:
                    ev = ev_results.get(p.token_address)
                    if ev:
                        ev_lines.append(f"**${p.symbol}** → {ev.heat_label} | {ev.position_label} | 確信度{ev.confidence:.0f}%")
                if ev_lines:
                    ev_text = "📊 **期待値レポート**\n" + "\n".join(ev_lines)
                    try:
                        async with session.post(hub.discord.url, json={"content": ev_text}) as resp:
                            pass
                    except Exception:
                        pass

            # Step 8: 状態更新
            state.mark_notified(new_projects)
            state.save_scan(top)

            logger.info(f"🏁 完了: {datetime.now(JST).strftime('%H:%M:%S')} JST")
            return new_projects

    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        logger.error(f"スクリーニングエラー: {error_msg}")
        await send_error_alert(error_msg)
        return None


# ============================================================
# サイクル2: リアルタイム監視（5分間隔）
# ============================================================
async def run_realtime_monitor():
    """ウォレット/LP/レンジ/Meme急騰/NFTフロアを監視"""
    logger.info("👁️ リアルタイム監視サイクル開始...")

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            headers={"User-Agent": "SolAutoScreener/3.0"}
        ) as session:
            hub = NotificationHub(session)
            alerts = []

            # Copyウォレット
            wallet_tracker = WalletTracker(session)
            wallet_activities = await wallet_tracker.check_all()
            for wa in wallet_activities:
                alerts.append(f"👛 **{wa.label}** が新規TX: `{wa.signature[:16]}...`")

            # 流動性監視
            liq_monitor = LiquidityMonitor(session)
            liq_alerts = await liq_monitor.check_all()
            for la in liq_alerts:
                emoji = "🚨" if la.alert_type in ("removed", "drop") else "💧"
                alerts.append(
                    f"{emoji} **${la.token_symbol}** LP{la.alert_type}: "
                    f"${la.prev_liquidity:,.0f} → ${la.current_liquidity:,.0f} ({la.change_pct:+.1f}%)"
                )

            # SOLレンジ
            range_monitor = RangeMonitor(session)
            range_alerts = await range_monitor.check_all()
            for ra in range_alerts:
                emoji = "📈" if ra.breach == "above" else "📉"
                alerts.append(
                    f"{emoji} **{ra.asset}** レンジ{'上限突破' if ra.breach == 'above' else '下限割れ'}: "
                    f"${ra.current_price:.2f} (24h: {ra.change_24h:+.1f}%) "
                    f"[レンジ: ${ra.range_low:.0f}-${ra.range_high:.0f}]"
                )

            # Meme急騰
            meme_monitor = MemeChartMonitor(session)
            meme_alerts = await meme_monitor.scan_hot_memes()
            for ma in meme_alerts[:5]:
                alerts.append(
                    f"🚀 **${ma.symbol}** ({ma.name}) 急騰! "
                    f"5m: {ma.price_change_5m:+.1f}% | 1h: {ma.price_change_1h:+.1f}% "
                    f"| LP: ${ma.liquidity_usd:,.0f}"
                )

            # NFTフロア
            nft_monitor = NFTFloorMonitor(session)
            nft_alerts = await nft_monitor.check_all()
            for na in nft_alerts:
                emoji = "📈" if na.alert_type == "pump" else "📉"
                alerts.append(
                    f"{emoji} **NFT {na.collection}** フロア{na.change_pct:+.1f}%: "
                    f"{na.prev_floor:.2f} → {na.current_floor:.2f} SOL"
                )

            # アラートがあれば一括通知
            if alerts:
                now = datetime.now(JST).strftime('%H:%M')
                text = f"🔔 **リアルタイムアラート** ({now} JST)\n\n" + "\n".join(alerts)
                await send_alert(session, hub, text)
                logger.info(f"リアルタイム: {len(alerts)}件アラート送信")
            else:
                logger.debug("リアルタイム: アラートなし")

    except Exception as e:
        logger.error(f"リアルタイム監視エラー: {e}")


# ============================================================
# サイクル3: デイリーレポート（1日1回）
# ============================================================
async def run_daily_report():
    """エアドロ/TGE/背景調査の日次レポート"""
    now = datetime.now(JST)
    logger.info(f"📋 デイリーレポート生成: {now.strftime('%Y/%m/%d')} JST")

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
            headers={"User-Agent": "SolAutoScreener/3.0"}
        ) as session:
            hub = NotificationHub(session)
            report_lines = [f"📋 **デイリーレポート** {now.strftime('%Y/%m/%d')} JST\n"]

            # エアドロ情報
            logger.info("🪂 エアドロスキャン...")
            airdrop_scanner = AirdropScanner(session)
            airdrops = await airdrop_scanner.scan_all()
            if airdrops:
                report_lines.append("**🪂 エアドロップ情報**")
                for a in airdrops[:10]:
                    status = {"active": "🟢", "upcoming": "🟡", "ended": "⚫"}.get(a.status, "⚪")
                    report_lines.append(f"  {status} **{a.name}** ({a.source})")
                    if a.description:
                        report_lines.append(f"    {a.description[:100]}")
                    if a.url:
                        report_lines.append(f"    {a.url}")
                report_lines.append("")

            # TGE（新規ローンチ）
            logger.info("🎯 TGE検出...")
            tge_monitor = TGEMonitor(session)
            tge_events = await tge_monitor.check_new_launches()
            if tge_events:
                report_lines.append("**🎯 新規TGE（Token Launch）**")
                for t in tge_events[:10]:
                    mcap = f"MCap: ${t.initial_mcap:,.0f}" if t.initial_mcap else ""
                    report_lines.append(f"  🆕 **{t.name}** ({t.symbol}) on {t.platform} {mcap}")
                report_lines.append("")

            # スキャン履歴サマリ
            scans = state.history.get("scans", [])
            if scans:
                last_24h = [s for s in scans if s.get("timestamp", "") > (now - timedelta(days=1)).isoformat()]
                if last_24h:
                    total_found = sum(s.get("count", 0) for s in last_24h)
                    report_lines.append(f"**📊 24h統計**")
                    report_lines.append(f"  スキャン回数: {len(last_24h)}回")
                    report_lines.append(f"  検出プロジェクト: {total_found}件")
                    report_lines.append(f"  通知済み: {len(state.state.get('notified', {}))}件")
                    report_lines.append("")

            # 送信
            report = "\n".join(report_lines)
            await send_alert(session, hub, report)
            logger.info("デイリーレポート送信完了")

    except Exception as e:
        logger.error(f"デイリーレポートエラー: {e}")


# ============================================================
# デーモン
# ============================================================
async def run_daemon():
    """全監視デーモン"""
    scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")

    # サイクル1: メインスクリーニング
    scheduler.add_job(run_screening_cycle, "cron",
                      hour=config.morning_scan_hour, minute=0, id="morning")
    scheduler.add_job(run_screening_cycle, "interval",
                      minutes=config.scan_interval_minutes, id="interval")

    # サイクル2: リアルタイム監視（5分間隔）
    rt_interval = int(os.getenv("REALTIME_INTERVAL_MINUTES", "5"))
    scheduler.add_job(run_realtime_monitor, "interval",
                      minutes=rt_interval, id="realtime")

    # サイクル3: デイリーレポート（毎朝9時）
    report_hour = int(os.getenv("DAILY_REPORT_HOUR", "9"))
    scheduler.add_job(run_daily_report, "cron",
                      hour=report_hour, minute=0, id="daily")

    scheduler.start()
    logger.info(f"⏰ デーモン起動（v3フル統合）")
    logger.info(f"   メイン: 毎朝{config.morning_scan_hour}:00 + {config.scan_interval_minutes}分間隔")
    logger.info(f"   リアルタイム: {rt_interval}分間隔")
    logger.info(f"   デイリー: 毎朝{report_hour}:00")

    # 起動直後に1回ずつ実行
    await run_screening_cycle()
    await run_realtime_monitor()

    # シグナルハンドリング
    stop = asyncio.Event()
    def shutdown():
        logger.info("シャットダウン...")
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass

    await stop.wait()
    scheduler.shutdown()
    logger.info("👋 停止完了")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "once":
        print("🔍 1回実行...")
        asyncio.run(run_screening_cycle())
    elif mode == "daemon":
        print("🔄 デーモンモード（v3フル統合）...")
        asyncio.run(run_daemon())
    elif mode == "daily":
        print("📋 デイリーレポート...")
        asyncio.run(run_daily_report())
    elif mode == "realtime":
        print("👁️ リアルタイム監視（1回）...")
        asyncio.run(run_realtime_monitor())
    else:
        print("Usage: python main.py [once|daemon|daily|realtime]")
        sys.exit(1)


if __name__ == "__main__":
    main()
