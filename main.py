"""
SOL Auto Screener - 統合版
"""
import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# インポートパスの修正
from config import config
from scanner import DexScreenerScanner
from scorer import ScoringEngine
from notifier import NotificationHub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime )s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("sol-screener")
JST = timezone(timedelta(hours=9))

async def run_screening_cycle():
    now = datetime.now(JST)
    logger.info(f"🚀 スクリーニング開始: {now.strftime('%Y/%m/%d %H:%M:%S')} JST")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60 )) as session:
        scanner = DexScreenerScanner(session)
        projects = await scanner.fetch_new_pairs(hours_back=24)
        if not projects: return
        for p in projects[:30]: await scanner.enrich_github(p)
        engine = ScoringEngine(session)
        scored = await engine.score_projects(projects)
        top = scored[:config.top_n]
        hub = NotificationHub(session)
        await hub.broadcast(top)
        logger.info(f"🏁 完了: {datetime.now(JST).strftime('%H:%M:%S')} JST")
    return top

async def run_daemon():
    scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")
    scheduler.add_job(run_screening_cycle, "cron", hour=config.morning_scan_hour, minute=0)
    scheduler.add_job(run_screening_cycle, "interval", minutes=config.scan_interval_minutes)
    scheduler.start()
    logger.info("⏰ デーモン起動")
    await run_screening_cycle()
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass
    await stop.wait()

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "once": asyncio.run(run_screening_cycle())
    elif mode == "daemon": asyncio.run(run_daemon())

if __name__ == "__main__":
    main()
