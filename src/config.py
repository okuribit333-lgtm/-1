"""設定管理"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # 通知先
    discord_webhook_url: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    line_notify_token: str = os.getenv("LINE_NOTIFY_TOKEN", "")

    # GitHub（任意）
    github_token: str = os.getenv("GITHUB_TOKEN", "")

    # Helius API（任意、あるとスマートマネー分析の精度UP）
    helius_api_key: str = os.getenv("HELIUS_API_KEY", "")

    # 機能トグル
    enable_pumpfun: bool = os.getenv("ENABLE_PUMPFUN", "true").lower() == "true"
    enable_nft: bool = os.getenv("ENABLE_NFT", "false").lower() == "true"
    enable_mania_scoring: bool = os.getenv("ENABLE_MANIA_SCORING", "true").lower() == "true"

    # リアルタイム監視
    realtime_interval: int = int(os.getenv("REALTIME_INTERVAL_MINUTES", "5"))
    daily_report_hour: int = int(os.getenv("DAILY_REPORT_HOUR", "9"))

    # Copyウォレット: "addr1:ラベル1,addr2:ラベル2"
    watch_wallets: str = os.getenv("WATCH_WALLETS", "")

    # 流動性監視トークン: "addr1,addr2"
    watch_tokens: str = os.getenv("WATCH_TOKENS", "")

    # SOLレンジ
    sol_range_low: float = float(os.getenv("SOL_RANGE_LOW", "0"))
    sol_range_high: float = float(os.getenv("SOL_RANGE_HIGH", "0"))

    # NFT監視: "mad_lads,tensorians"
    watch_nfts: str = os.getenv("WATCH_NFTS", "")

    # スクリーニング設定
    top_n: int = int(os.getenv("TOP_N", "5"))
    scan_interval_minutes: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "60"))
    morning_scan_hour: int = int(os.getenv("MORNING_SCAN_HOUR", "7"))
    min_liquidity_usd: float = float(os.getenv("MIN_LIQUIDITY_USD", "1000"))
    min_volume_24h_usd: float = float(os.getenv("MIN_VOLUME_24H_USD", "500"))

    # スコアリングの重み（合計1.0）
    weights: dict = field(default_factory=lambda: {
        "liquidity": 0.15,
        "volume": 0.15,
        "price_change": 0.10,
        "tx_count": 0.10,
        "twitter_followers": 0.10,
        "twitter_engagement": 0.10,
        "discord_members": 0.08,
        "discord_activity": 0.07,
        "github_commits": 0.05,
        "github_stars": 0.05,
        "website_exists": 0.03,
        "audit_exists": 0.02,
    })


config = Config()
