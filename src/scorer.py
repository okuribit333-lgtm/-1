"""
スコアリング：4ソース並列評価
オンチェーン / Twitter(Nitter) / Discord(公開API) / GitHub
全て無料で動作
"""
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

from .config import config
from .scanner import SolanaProject

logger = logging.getLogger(__name__)


# ============================================================
# Twitter スコアラー（Nitter + BeautifulSoup）
# ============================================================
class TwitterScorer:

    INSTANCES = [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
        "https://nitter.woodland.cafe",
    ]

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def score(self, handle: Optional[str]) -> dict:
        if not handle:
            return {"twitter_followers": 0, "twitter_engagement": 0}

        data = await self._scrape(handle)
        if data:
            return self._calc(data, handle)
        return self._fallback(handle)

    async def _scrape(self, handle: str) -> Optional[dict]:
        """Nitterからプロフィール情報をスクレイピング（BeautifulSoup使用）"""
        for inst in self.INSTANCES:
            try:
                url = f"{inst}/{handle}"
                async with self.session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10),
                    headers={"User-Agent": "Mozilla/5.0"}
                ) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()

                soup = BeautifulSoup(html, "html.parser")

                # プロフィールカードから統計を取得
                stats = soup.select(".profile-stat-num")
                if len(stats) >= 4:
                    tweets = self._parse_num(stats[0].text)
                    following = self._parse_num(stats[1].text)
                    followers = self._parse_num(stats[2].text)
                    likes = self._parse_num(stats[3].text)

                    logger.info(f"Nitter ({inst}): @{handle} = {followers:,} followers")
                    return {
                        "followers": followers,
                        "following": following,
                        "tweets": tweets,
                        "likes": likes,
                    }

                # フォールバック: 検索で存在確認
                search_url = f"{inst}/search?q={handle}"
                async with self.session.get(search_url, timeout=aiohttp.ClientTimeout(total=8)) as resp2:
                    if resp2.status == 200:
                        soup2 = BeautifulSoup(await resp2.text(), "html.parser")
                        if soup2.find(class_="timeline-item"):
                            return {"followers": 0, "following": 0, "tweets": 0, "likes": 0, "exists": True}

            except Exception as e:
                logger.debug(f"Nitter {inst} エラー: {e}")
                continue
        return None

    def _calc(self, data: dict, handle: str) -> dict:
        followers = data.get("followers", 0)
        following = data.get("following", 1) or 1

        # フォロワースコア（対数スケール、上限100）
        follower_score = min(100, math.log10(max(1, followers)) * 25)

        # フォロワー/フォロー比率 → エンゲージメント推定
        ratio = followers / following
        engagement_score = min(100, ratio * 15)

        # フォロワー100未満はペナルティ
        if followers < 100:
            follower_score *= 0.3
            engagement_score *= 0.3

        return {
            "twitter_followers": round(follower_score, 1),
            "twitter_engagement": round(engagement_score, 1),
            "_twitter_raw": {
                "followers": followers,
                "following": data.get("following", 0),
                "tweets": data.get("tweets", 0),
                "handle": handle,
            }
        }

    def _fallback(self, handle: str) -> dict:
        """全Nitter死亡時 — ハンドル存在だけで最低スコア"""
        return {
            "twitter_followers": 15.0,
            "twitter_engagement": 10.0,
            "_twitter_raw": {"handle": handle, "source": "fallback"}
        }

    @staticmethod
    def _parse_num(text: str) -> int:
        text = text.strip().replace(",", "")
        m = 1
        if text.upper().endswith("K"):
            m = 1_000; text = text[:-1]
        elif text.upper().endswith("M"):
            m = 1_000_000; text = text[:-1]
        try:
            return int(float(text) * m)
        except ValueError:
            return 0


# ============================================================
# Discord スコアラー（公開API、Bot不要）
# ============================================================
class DiscordScorer:

    async def score(self, project: SolanaProject, session: aiohttp.ClientSession) -> dict:
        if not project.discord_url:
            return {"discord_members": 0, "discord_activity": 0}

        code = self._extract_code(project.discord_url)
        if not code:
            return {"discord_members": 0, "discord_activity": 0}

        try:
            url = f"https://discord.com/api/v10/invites/{code}"
            async with session.get(url, params={"with_counts": "true"}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return {"discord_members": 0, "discord_activity": 0}
                data = await resp.json()

            members = data.get("approximate_member_count", 0)
            online = data.get("approximate_presence_count", 0)

            member_score = min(100, math.log10(max(1, members)) * 25)
            online_ratio = online / max(1, members)
            activity_score = min(100, online_ratio * 500)

            return {
                "discord_members": round(member_score, 1),
                "discord_activity": round(activity_score, 1),
                "_discord_raw": {
                    "members": members,
                    "online": online,
                    "online_ratio": round(online_ratio * 100, 1),
                }
            }
        except Exception as e:
            logger.warning(f"Discord scoring error: {e}")
            return {"discord_members": 0, "discord_activity": 0}

    @staticmethod
    def _extract_code(url: str) -> Optional[str]:
        if not url:
            return None
        for p in ["https://discord.gg/", "https://discord.com/invite/", "http://discord.gg/"]:
            if url.startswith(p):
                return url[len(p):].strip("/").split("?")[0]
        return None


# ============================================================
# GitHub スコアラー（トークン不要でも動作）
# ============================================================
class GitHubScorer:

    BASE = "https://api.github.com"

    def __init__(self):
        self.headers = {}
        if config.github_token:
            self.headers["Authorization"] = f"token {config.github_token}"

    async def score(self, project: SolanaProject, session: aiohttp.ClientSession) -> dict:
        if not project.github_url:
            # URLがなくてもトークン名でGitHub検索（元コードの良いアイデア）
            return await self._search_by_name(project.name, session)

        owner, repo = self._parse_url(project.github_url)
        if not owner:
            return {"github_commits": 0, "github_stars": 0}

        try:
            if repo:
                return await self._score_repo(owner, repo, session)
            return await self._score_org(owner, session)
        except Exception as e:
            logger.warning(f"GitHub error: {e}")
            return {"github_commits": 0, "github_stars": 0}

    async def _search_by_name(self, name: str, session: aiohttp.ClientSession) -> dict:
        """トークン名でGitHubリポジトリを検索（元コードから採用）"""
        if not name or name in ("Unknown", "Wrapped SOL"):
            return {"github_commits": 0, "github_stars": 0}
        try:
            url = f"{self.BASE}/search/repositories"
            async with session.get(
                url, headers=self.headers,
                params={"q": name, "per_page": 3},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return {"github_commits": 0, "github_stars": 0}
                data = await resp.json()

            if data.get("total_count", 0) > 0:
                top = data["items"][0]
                stars = top.get("stargazers_count", 0)
                return {
                    "github_commits": 10.0,  # 存在するだけで最低点
                    "github_stars": round(min(100, math.log10(max(1, stars)) * 30), 1),
                    "_github_raw": {"stars": stars, "source": "search", "name": top.get("full_name")}
                }
        except Exception:
            pass
        return {"github_commits": 0, "github_stars": 0}

    async def _score_repo(self, owner: str, repo: str, session: aiohttp.ClientSession) -> dict:
        async with session.get(
            f"{self.BASE}/repos/{owner}/{repo}",
            headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)
        ) as resp:
            if resp.status != 200:
                return await self._score_org(owner, session)
            data = await resp.json()

        stars = data.get("stargazers_count", 0)
        star_score = min(100, math.log10(max(1, stars)) * 30)

        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        try:
            async with session.get(
                f"{self.BASE}/repos/{owner}/{repo}/commits",
                headers=self.headers, params={"since": since, "per_page": 50},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                commits = await resp.json() if resp.status == 200 else []
        except Exception:
            commits = []

        return {
            "github_commits": round(min(100, len(commits) * 5), 1),
            "github_stars": round(star_score, 1),
            "_github_raw": {"stars": stars, "recent_commits": len(commits), "url": f"{owner}/{repo}"}
        }

    async def _score_org(self, org: str, session: aiohttp.ClientSession) -> dict:
        try:
            async with session.get(
                f"{self.BASE}/orgs/{org}/repos",
                headers=self.headers, params={"sort": "updated", "per_page": 5},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    return {"github_commits": 0, "github_stars": 0}
                repos = await resp.json()
        except Exception:
            return {"github_commits": 0, "github_stars": 0}

        total_stars = sum(r.get("stargazers_count", 0) for r in repos)
        return {
            "github_commits": round(min(100, len(repos) * 15), 1),
            "github_stars": round(min(100, math.log10(max(1, total_stars)) * 30), 1),
            "_github_raw": {"stars": total_stars, "repos": len(repos)}
        }

    @staticmethod
    def _parse_url(url: str) -> tuple[Optional[str], Optional[str]]:
        if not url:
            return None, None
        url = url.rstrip("/")
        parts = url.replace("https://github.com/", "").replace("http://github.com/", "").split("/")
        return (parts[0] if len(parts) >= 1 else None, parts[1] if len(parts) >= 2 else None)


# ============================================================
# オンチェーンスコアラー
# ============================================================
class OnChainScorer:

    def score(self, p: SolanaProject) -> dict:
        liq = min(100, math.log10(max(1, p.liquidity_usd)) * 20)
        vol = min(100, math.log10(max(1, p.volume_24h_usd)) * 18)

        pc = p.price_change_24h
        if pc is None:
            price = 50
        elif 0 < pc <= 100:
            price = min(100, 50 + pc * 0.5)
        elif pc > 100:
            price = max(30, 100 - (pc - 100) * 0.2)  # 急騰はラグプルリスク
        elif -50 < pc <= 0:
            price = max(0, 50 + pc)
        else:
            price = 0

        tx = min(100, math.log10(max(1, p.tx_count_24h)) * 30)
        web = 80 if p.website_url else 0

        return {
            "liquidity": round(liq, 1),
            "volume": round(vol, 1),
            "price_change": round(price, 1),
            "tx_count": round(tx, 1),
            "website_exists": round(web, 1),
            "audit_exists": 0,
            "_onchain_raw": {
                "liquidity_usd": p.liquidity_usd,
                "volume_24h_usd": p.volume_24h_usd,
                "price_change_24h": pc,
                "tx_count_24h": p.tx_count_24h,
                "makers_24h": p.makers_24h,
            }
        }


# ============================================================
# 統合スコアリングエンジン
# ============================================================
class ScoringEngine:

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.twitter = TwitterScorer(session)
        self.discord = DiscordScorer()
        self.github = GitHubScorer()
        self.onchain = OnChainScorer()

    async def score_projects(self, projects: list[SolanaProject]) -> list[SolanaProject]:
        tasks = [self._score_one(p) for p in projects]
        scored = await asyncio.gather(*tasks)
        scored.sort(key=lambda p: p.total_score, reverse=True)
        return scored

    async def _score_one(self, p: SolanaProject) -> SolanaProject:
        onchain = self.onchain.score(p)

        twitter, discord, github = await asyncio.gather(
            self.twitter.score(p.twitter_handle),
            self.discord.score(p, self.session),
            self.github.score(p, self.session),
        )

        all_scores = {}
        for d in [onchain, twitter, discord, github]:
            all_scores.update(d)
        p.scores = all_scores

        total = sum(all_scores.get(k, 0) * w for k, w in config.weights.items())
        p.total_score = round(total, 1)

        logger.info(f"  {p.symbol}: {p.total_score}点")
        return p
