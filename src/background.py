"""
プロジェクト背景調査モジュール
コミュ/人物/会社/資金バック/関係者の自動精査

データソース（全て無料）:
- DeFiLlama: プロトコルTVL・チェーン情報
- CoinGecko: チーム情報・資金調達
- GitHub: 開発者プロフィール・コミット履歴
- Twitter/Nitter: チームメンバーの活動
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


@dataclass
class TeamMember:
    name: str
    role: str = ""
    twitter: str = ""
    github: str = ""
    linkedin: str = ""
    is_doxxed: bool = False  # 本人確認済みか


@dataclass
class FundingInfo:
    total_raised: float = 0.0  # USD
    investors: list = field(default_factory=list)
    rounds: list = field(default_factory=list)  # [{"round": "seed", "amount": 5000000, "date": "2024-03"}]


@dataclass
class ProjectBackground:
    """プロジェクト背景レポート"""
    # 基本情報
    name: str = ""
    website: str = ""

    # チーム
    team: list = field(default_factory=list)  # list[TeamMember]
    team_doxxed: bool = False
    team_size_estimate: int = 0

    # 資金
    funding: FundingInfo = field(default_factory=FundingInfo)
    has_vc_backing: bool = False

    # コミュニティ
    community_score: float = 0.0
    discord_health: dict = field(default_factory=dict)  # {"members": 0, "active_ratio": 0}
    twitter_health: dict = field(default_factory=dict)

    # 開発
    github_health: dict = field(default_factory=dict)  # {"commits_30d": 0, "contributors": 0}
    is_fork: bool = False
    code_quality: str = "unknown"

    # 関連プロジェクト
    related_projects: list = field(default_factory=list)
    ecosystem: str = ""  # "jupiter", "marinade" etc

    # リスク
    red_flags: list = field(default_factory=list)
    trust_score: float = 0.0  # 0-100

    def summary(self) -> str:
        lines = [f"📋 {self.name} 背景調査"]
        lines.append(f"  チーム: {'Doxxed ✅' if self.team_doxxed else 'Anonymous ⚠️'} ({self.team_size_estimate}人)")
        if self.has_vc_backing:
            investors = ", ".join(self.funding.investors[:3])
            lines.append(f"  資金: ${self.funding.total_raised:,.0f} ({investors})")
        else:
            lines.append("  資金: VC情報なし")
        lines.append(f"  信頼度: {self.trust_score:.0f}/100")
        if self.red_flags:
            for f in self.red_flags[:3]:
                lines.append(f"  🚩 {f}")
        return "\n".join(lines)


class BackgroundInvestigator:
    """プロジェクトの背景を自動調査"""

    NITTER_INSTANCES = [
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
    ]

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def investigate(self, name: str, website: str = "",
                          twitter_handle: str = "", github_url: str = "",
                          discord_invite: str = "") -> ProjectBackground:
        """フル調査を実行"""
        bg = ProjectBackground(name=name, website=website)

        tasks = [
            self._check_coingecko(name, bg),
            self._check_defillama(name, bg),
            self._check_github_team(github_url, bg),
            self._check_twitter_team(twitter_handle, bg),
            self._check_website(website, bg),
        ]

        await asyncio.gather(*tasks, return_exceptions=True)

        # 信頼度スコア計算
        bg.trust_score = self._calculate_trust(bg)

        return bg

    async def _check_coingecko(self, name: str, bg: ProjectBackground):
        """CoinGecko APIでプロジェクト情報・資金調達を確認"""
        try:
            search_url = f"https://api.coingecko.com/api/v3/search?query={name}"
            async with self.session.get(search_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

            coins = data.get("coins", [])
            if not coins:
                return

            coin_id = coins[0].get("id", "")
            if not coin_id:
                return

            # 詳細取得
            detail_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
            params = {"localization": "false", "tickers": "false", "market_data": "false",
                      "community_data": "true", "developer_data": "true"}
            async with self.session.get(detail_url, params=params,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                detail = await resp.json()

            # コミュニティデータ
            community = detail.get("community_data", {})
            if community:
                bg.twitter_health = {
                    "followers": community.get("twitter_followers", 0),
                }
                bg.discord_health = {
                    "members": community.get("telegram_channel_user_count", 0),
                }

            # 開発データ
            dev = detail.get("developer_data", {})
            if dev:
                bg.github_health = {
                    "commits_4w": dev.get("commit_count_4_weeks", 0),
                    "forks": dev.get("forks", 0),
                    "stars": dev.get("stars", 0),
                    "contributors": dev.get("pull_request_contributors", 0),
                }
                if dev.get("forks", 0) > dev.get("stars", 0) * 5:
                    bg.is_fork = True

            # リンク情報
            links = detail.get("links", {})
            repos = links.get("repos_url", {}).get("github", [])
            if repos:
                bg.github_health["repos"] = repos[:3]

            logger.info(f"  CoinGecko: {coin_id} found")

        except Exception as e:
            logger.debug(f"CoinGecko error: {e}")

    async def _check_defillama(self, name: str, bg: ProjectBackground):
        """DeFiLlamaでTVL・エコシステム情報確認"""
        try:
            url = "https://api.llama.fi/protocols"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

            for protocol in data:
                if name.lower() in protocol.get("name", "").lower():
                    tvl = protocol.get("tvl", 0) or 0
                    chains = protocol.get("chains", [])
                    category = protocol.get("category", "")

                    bg.ecosystem = category
                    bg.related_projects = [
                        p.get("name") for p in data
                        if category and p.get("category") == category
                        and "Solana" in p.get("chains", [])
                    ][:5]

                    if tvl > 10_000_000:
                        bg.has_vc_backing = True  # 高TVL = 資金バックあり推定

                    logger.info(f"  DeFiLlama: TVL=${tvl:,.0f}, category={category}")
                    break

        except Exception as e:
            logger.debug(f"DeFiLlama error: {e}")

    async def _check_github_team(self, github_url: str, bg: ProjectBackground):
        """GitHubから開発チーム情報を取得"""
        if not github_url:
            return

        try:
            # org/repo形式を抽出
            match = re.search(r'github\.com/([^/]+)(?:/([^/]+))?', github_url)
            if not match:
                return

            org = match.group(1)

            # org membersを取得
            members_url = f"https://api.github.com/orgs/{org}/members"
            async with self.session.get(members_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    members = await resp.json()
                    bg.team_size_estimate = len(members)

                    for m in members[:5]:
                        bg.team.append(TeamMember(
                            name=m.get("login", ""),
                            github=m.get("html_url", ""),
                        ))

            # 最近のコミット活動
            repo = match.group(2) if match.group(2) else ""
            if repo:
                commits_url = f"https://api.github.com/repos/{org}/{repo}/commits?per_page=30"
                async with self.session.get(commits_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        commits = await resp.json()
                        unique_authors = set()
                        for c in commits:
                            author = c.get("author", {})
                            if author:
                                unique_authors.add(author.get("login", ""))
                        bg.github_health["active_devs_30d"] = len(unique_authors)

        except Exception as e:
            logger.debug(f"GitHub team error: {e}")

    async def _check_twitter_team(self, handle: str, bg: ProjectBackground):
        """Nitter経由でTwitterプロフィールからチーム情報を推定"""
        if not handle:
            return

        for inst in self.NITTER_INSTANCES:
            try:
                url = f"{inst}/{handle}"
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=8),
                                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()

                soup = BeautifulSoup(html, "html.parser")

                # Bio分析
                bio = soup.select_one(".profile-bio")
                if bio:
                    bio_text = bio.get_text(strip=True).lower()
                    # Doxxed判定のヒント
                    if any(kw in bio_text for kw in ["team", "founded by", "ceo", "co-founder", "built by"]):
                        bg.team_doxxed = True

                    # VCバッキングヒント
                    vc_keywords = ["backed by", "invested", "a16z", "paradigm", "polychain",
                                   "multicoin", "jump", "alameda", "solana ventures"]
                    if any(kw in bio_text for kw in vc_keywords):
                        bg.has_vc_backing = True

                break
            except Exception:
                continue

    async def _check_website(self, url: str, bg: ProjectBackground):
        """ウェブサイトからチーム/投資家情報を抽出"""
        if not url:
            return

        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                         headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status != 200:
                    return
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text().lower()

            # チームセクション検出
            if any(kw in text for kw in ["team", "founders", "about us"]):
                bg.team_doxxed = True

            # 投資家検出
            vc_names = ["a16z", "paradigm", "polychain", "multicoin", "jump crypto",
                        "solana ventures", "coinbase ventures", "binance labs",
                        "sequoia", "dragonfly", "pantera"]
            found_vcs = [vc for vc in vc_names if vc in text]
            if found_vcs:
                bg.has_vc_backing = True
                bg.funding.investors = found_vcs
                bg.red_flags = [f for f in bg.red_flags if "VC" not in f]

        except Exception as e:
            logger.debug(f"Website check error: {e}")

    def _calculate_trust(self, bg: ProjectBackground) -> float:
        """信頼度スコアを計算（0-100）"""
        score = 30  # ベース

        # チーム（+25）
        if bg.team_doxxed:
            score += 15
        if bg.team_size_estimate >= 5:
            score += 10
        elif bg.team_size_estimate >= 2:
            score += 5

        # 資金（+25）
        if bg.has_vc_backing:
            score += 15
        if bg.funding.total_raised > 5_000_000:
            score += 10
        elif bg.funding.total_raised > 1_000_000:
            score += 5

        # 開発活動（+20）
        commits = bg.github_health.get("commits_4w", 0)
        if commits > 50:
            score += 15
        elif commits > 10:
            score += 10
        elif commits > 0:
            score += 5

        contributors = bg.github_health.get("contributors", 0)
        if contributors > 5:
            score += 5

        # レッドフラグ（減点）
        if bg.is_fork:
            score -= 10
            bg.red_flags.append("コードがフォーク")
        if not bg.team_doxxed and not bg.has_vc_backing:
            bg.red_flags.append("チーム匿名 & VC情報なし")
        if bg.team_size_estimate == 0 and not bg.github_health:
            bg.red_flags.append("開発活動が確認できない")

        return max(0, min(100, score))
