"""
マニア基準スコアリング拡張モジュール
- Smart Money検知（Helius APIでホルダー詳細分析）
- ソーシャル伸び率（DexScreener profile age vs followers）
- Bot水増し検知（フォロワー/フォロー比率の異常パターン）
- Dev wallet行動追跡

全て無料APIで動作
"""
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

from .scanner import SolanaProject
from .config import config

logger = logging.getLogger(__name__)


class SmartMoneyAnalyzer:
    """
    Helius無料API (10M req/月) でトークンのホルダー分析
    - 上位ホルダーの過去の勝率
    - 既知のスマートマネーウォレットとの照合
    - Dev walletの挙動チェック
    """

    # 既知のスマートマネーウォレット（公開情報ベース、随時更新可能）
    KNOWN_SMART_WALLETS = {
        # 有名なSolanaトレーダー/ファンドのウォレット（公開されているもの）
        # 実際の運用時に追加していく
    }

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.helius_key = config.helius_api_key if hasattr(config, 'helius_api_key') else ""

    async def analyze(self, project: SolanaProject) -> dict:
        """スマートマネー分析"""
        result = {
            "smart_money_score": 0,
            "smart_money_count": 0,
            "dev_wallet_risk": "unknown",
            "holder_quality": 0,
        }

        holders = await self._get_holders(project.token_address)
        if not holders:
            return result

        # 上位20ホルダーを分析
        top_holders = holders[:20]
        smart_count = 0
        total_balance = sum(h.get("amount", 0) for h in top_holders)

        for h in top_holders:
            addr = h.get("owner", "")

            # 既知スマートマネーチェック
            if addr in self.KNOWN_SMART_WALLETS:
                smart_count += 1

            # ホルダーの質を推定（残高の分散度）
            # 1人に集中してたらリスク
            if total_balance > 0:
                pct = h.get("amount", 0) / total_balance * 100
                if pct > 30:
                    result["dev_wallet_risk"] = "high"

        # スマートマネースコア
        result["smart_money_count"] = smart_count
        result["smart_money_score"] = min(100, smart_count * 25)

        # ホルダー分散度（ジニ係数的な計算）
        if total_balance > 0 and len(top_holders) > 1:
            balances = sorted([h.get("amount", 0) for h in top_holders], reverse=True)
            top1_pct = balances[0] / total_balance
            top5_pct = sum(balances[:5]) / total_balance

            # 分散してるほど高スコア
            if top1_pct < 0.1:
                result["holder_quality"] = 90
            elif top1_pct < 0.2:
                result["holder_quality"] = 70
            elif top1_pct < 0.3:
                result["holder_quality"] = 50
            elif top1_pct < 0.5:
                result["holder_quality"] = 30
            else:
                result["holder_quality"] = 10

        return result

    async def _get_holders(self, token_address: str) -> list:
        """Helius APIまたはDexScreener経由でホルダー情報取得"""
        # Helius無料枠がある場合
        if self.helius_key:
            try:
                url = f"https://api.helius.xyz/v0/token-metadata?api-key={self.helius_key}"
                payload = {"mintAccounts": [token_address], "includeOffChain": False}
                async with self.session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and len(data) > 0:
                            return data[0].get("onChainAccountInfo", {}).get("holders", [])
            except Exception as e:
                logger.debug(f"Helius holder fetch error: {e}")

        # フォールバック: RugCheck APIのtopHolders（safety.pyでも使ってるが別角度で分析）
        try:
            url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report/summary"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("topHolders", [])
        except Exception:
            pass

        return []


class SocialVelocityAnalyzer:
    """
    ソーシャルの「伸び率」を評価
    - プロジェクト作成日からのフォロワー増加速度
    - フォロワー数の絶対値ではなく「加速度」を見る
    """

    def analyze(self, project: SolanaProject, twitter_raw: dict) -> dict:
        result = {
            "velocity_score": 0,
            "followers_per_day": 0,
            "age_days": 0,
        }

        followers = twitter_raw.get("followers", 0)
        if not followers:
            return result

        # プロジェクト作成日からの経過日数
        now = datetime.now(timezone.utc)
        age = (now - project.created_at).total_seconds() / 86400
        age = max(age, 0.1)  # ゼロ除算防止
        result["age_days"] = round(age, 1)

        # フォロワー/日
        fpd = followers / age
        result["followers_per_day"] = round(fpd, 1)

        # 伸び率スコア（対数スケール）
        # 1日で1000人 = 異常な速さ = 100点
        # 1日で100人 = かなり速い = 80点
        # 1日で10人 = 普通 = 40点
        # 1日で1人 = 遅い = 10点
        if fpd > 0:
            velocity = min(100, math.log10(max(1, fpd)) * 33)
        else:
            velocity = 0

        # 新しいプロジェクト（3日以内）で高フォロワー → ボーナス
        if age < 3 and followers > 500:
            velocity = min(100, velocity * 1.5)

        # 古いプロジェクト（30日以上）で低フォロワー → ペナルティ
        if age > 30 and followers < 100:
            velocity *= 0.3

        result["velocity_score"] = round(velocity, 1)
        return result


class BotDetector:
    """
    Twitter Bot水増し検知
    フォロワー/フォロー比率、エンゲージメント率の異常パターンを検出
    """

    def analyze(self, twitter_raw: dict) -> dict:
        result = {
            "bot_risk": "low",  # low / medium / high
            "bot_score": 0,     # 0-100 (高いほどBot疑い)
            "indicators": [],
        }

        followers = twitter_raw.get("followers", 0)
        following = twitter_raw.get("following", 1) or 1
        tweets = twitter_raw.get("tweets", 0)
        likes = twitter_raw.get("likes", 0)

        if followers == 0:
            return result

        indicators = []
        bot_score = 0

        # 1. フォロワー/フォロー比率が異常（買いフォロワー疑い）
        ratio = followers / following
        if ratio > 100 and followers > 10000:
            # フォロワーが多いのにフォローが極端に少ない → 普通は有名人パターン
            # でも新規プロジェクトでこれは怪しい
            pass
        elif ratio < 0.5 and followers > 1000:
            # フォロワーよりフォローが多い → フォロバ狙いBot
            bot_score += 25
            indicators.append("フォロー数がフォロワーより多い")

        # 2. ツイート数とフォロワーの不整合
        if followers > 5000 and tweets < 10:
            bot_score += 30
            indicators.append(f"フォロワー{followers:,}に対しツイート{tweets}件")

        # 3. エンゲージメント率（いいね/ツイート）
        if tweets > 0:
            engagement_per_tweet = likes / tweets
            if followers > 1000 and engagement_per_tweet < 1:
                bot_score += 20
                indicators.append("ツイートあたりのいいねが極端に少ない")

        # 4. フォロワーのキリ番（1000, 5000, 10000ちょうど等）
        if followers > 500:
            str_followers = str(followers)
            trailing_zeros = len(str_followers) - len(str_followers.rstrip('0'))
            if trailing_zeros >= 3:
                bot_score += 15
                indicators.append(f"フォロワー数がキリ番（{followers:,}）")

        # 判定
        if bot_score >= 50:
            result["bot_risk"] = "high"
        elif bot_score >= 25:
            result["bot_risk"] = "medium"

        result["bot_score"] = min(100, bot_score)
        result["indicators"] = indicators

        return result


class ManiaScorer:
    """
    マニア基準スコアリング統合クラス
    通常スコアに加算する追加スコアを計算
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.smart_money = SmartMoneyAnalyzer(session)
        self.velocity = SocialVelocityAnalyzer()
        self.bot_detector = BotDetector()

    async def enhance_scores(self, project: SolanaProject) -> dict:
        """プロジェクトの追加スコアを計算"""
        twitter_raw = project.scores.get("_twitter_raw", {})

        # 並列実行
        smart_money_result = await self.smart_money.analyze(project)
        velocity_result = self.velocity.analyze(project, twitter_raw)
        bot_result = self.bot_detector.analyze(twitter_raw)

        # 追加スコア計算
        mania_scores = {
            # Smart Money（ホルダー品質）
            "smart_money": smart_money_result.get("smart_money_score", 0),
            "holder_quality": smart_money_result.get("holder_quality", 0),

            # ソーシャル伸び率
            "social_velocity": velocity_result.get("velocity_score", 0),

            # Bot検知（ペナルティとして適用）
            "bot_penalty": -bot_result.get("bot_score", 0) * 0.3,  # Bot疑いで減点

            # 生データ
            "_mania_raw": {
                "smart_money": smart_money_result,
                "velocity": velocity_result,
                "bot": bot_result,
            }
        }

        # 総合マニアスコア
        mania_total = (
            mania_scores["smart_money"] * 0.2 +
            mania_scores["holder_quality"] * 0.3 +
            mania_scores["social_velocity"] * 0.3 +
            max(0, 50 + mania_scores["bot_penalty"]) * 0.2  # Bot影響は20%まで
        )
        mania_scores["mania_total"] = round(mania_total, 1)

        return mania_scores
