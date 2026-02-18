"""
期待値の数値化モジュール
「どれくらい熱いならどの割合の金を入れるか」を自動判定

スコア → 熱量レベル → 推奨ポジションサイズ（通知に表示）
あくまで参考値。最終判断は人間。
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ExpectationValue:
    """期待値レポート"""
    heat_level: int         # 1-5（🔥の数）
    heat_label: str         # "超高" "高" "中" "低" "様子見"
    confidence: float       # 確信度 0-100
    position_pct: float     # 推奨ポジション割合 0-100%
    position_label: str     # "全力" "強め" "標準" "少額" "見送り"
    risk_reward: str        # "ハイリスク・ハイリターン" etc
    reasoning: list         # 判定理由


class ExpectationCalculator:
    """
    複数のスコアを統合して期待値を数値化

    入力:
    - total_score: メインスコア (0-100)
    - safety: 安全性チェック結果
    - mania_scores: マニア基準スコア
    - background: 背景調査結果
    - market_context: 市場コンテキスト（SOLの状態等）
    """

    # ポジションサイズ基準（ユーザーがカスタマイズ可能）
    POSITION_TABLE = {
        5: {"pct": 10.0, "label": "強め（10%）"},
        4: {"pct": 5.0,  "label": "標準（5%）"},
        3: {"pct": 2.0,  "label": "少額（2%）"},
        2: {"pct": 0.5,  "label": "最小（0.5%）"},
        1: {"pct": 0.0,  "label": "見送り"},
    }

    def calculate(self, total_score: float,
                  safety_result: dict = None,
                  mania_scores: dict = None,
                  trust_score: float = None,
                  sol_price_trend: str = None) -> ExpectationValue:
        """期待値を計算"""

        safety_result = safety_result or {}
        mania_scores = mania_scores or {}
        reasoning = []

        # ========================================
        # 1. ベーススコアからの期待値
        # ========================================
        base_heat = 0
        if total_score >= 75:
            base_heat = 5
            reasoning.append(f"スコア{total_score:.0f}/100（非常に高い）")
        elif total_score >= 60:
            base_heat = 4
            reasoning.append(f"スコア{total_score:.0f}/100（高い）")
        elif total_score >= 45:
            base_heat = 3
            reasoning.append(f"スコア{total_score:.0f}/100（中程度）")
        elif total_score >= 30:
            base_heat = 2
            reasoning.append(f"スコア{total_score:.0f}/100（低め）")
        else:
            base_heat = 1
            reasoning.append(f"スコア{total_score:.0f}/100（低い）")

        # ========================================
        # 2. 安全性補正
        # ========================================
        risk_level = safety_result.get("risk_level", "unknown")
        safety_modifier = 0

        if risk_level == "danger":
            safety_modifier = -2
            reasoning.append("🔴 安全性DANGER（大幅減点）")
        elif risk_level == "warning":
            safety_modifier = -1
            reasoning.append("🟡 安全性WARNING（減点）")
        elif risk_level == "safe":
            safety_modifier = 0
            reasoning.append("🟢 安全性OK")

        # ========================================
        # 3. マニア基準補正
        # ========================================
        mania_total = mania_scores.get("mania_total", 0)
        mania_modifier = 0

        if mania_total >= 70:
            mania_modifier = 1
            reasoning.append(f"マニア基準{mania_total:.0f}（高評価、ボーナス）")
        elif mania_total <= 20:
            mania_modifier = -1
            reasoning.append(f"マニア基準{mania_total:.0f}（低評価、減点）")

        # Bot検知
        bot_risk = mania_scores.get("_mania_raw", {}).get("bot", {}).get("bot_risk", "low")
        if bot_risk == "high":
            mania_modifier -= 1
            reasoning.append("🤖 Bot水増し疑い（減点）")

        # ========================================
        # 4. 背景調査補正
        # ========================================
        trust_modifier = 0
        if trust_score is not None:
            if trust_score >= 70:
                trust_modifier = 1
                reasoning.append(f"プロジェクト信頼度{trust_score:.0f}（高い）")
            elif trust_score <= 30:
                trust_modifier = -1
                reasoning.append(f"プロジェクト信頼度{trust_score:.0f}（低い）")

        # ========================================
        # 5. 市場コンテキスト補正
        # ========================================
        market_modifier = 0
        if sol_price_trend == "bullish":
            market_modifier = 1
            reasoning.append("SOL上昇トレンド（ボーナス）")
        elif sol_price_trend == "bearish":
            market_modifier = -1
            reasoning.append("SOL下落トレンド（減点）")

        # ========================================
        # 最終計算
        # ========================================
        final_heat = max(1, min(5, base_heat + safety_modifier + mania_modifier + trust_modifier + market_modifier))

        # 確信度（各要素の整合性）
        factors = [base_heat, 3 + safety_modifier, 3 + mania_modifier, 3 + trust_modifier]
        avg = sum(factors) / len(factors)
        variance = sum((f - avg) ** 2 for f in factors) / len(factors)
        confidence = max(10, min(100, 100 - variance * 15))

        # ポジション
        pos = self.POSITION_TABLE.get(final_heat, self.POSITION_TABLE[1])

        # リスク・リターン分類
        if final_heat >= 4 and risk_level in ("safe", "unknown"):
            rr = "高リターン期待・リスク管理済み"
        elif final_heat >= 4 and risk_level == "warning":
            rr = "ハイリスク・ハイリターン"
        elif final_heat <= 2:
            rr = "ローリターン・リスク高め"
        else:
            rr = "標準的なリスク・リターン"

        heat_labels = {5: "🔥🔥🔥🔥🔥 超高", 4: "🔥🔥🔥🔥 高", 3: "🔥🔥🔥 中", 2: "🔥🔥 低", 1: "🔥 様子見"}

        return ExpectationValue(
            heat_level=final_heat,
            heat_label=heat_labels[final_heat],
            confidence=round(confidence, 1),
            position_pct=pos["pct"],
            position_label=pos["label"],
            risk_reward=rr,
            reasoning=reasoning,
        )

    def format_for_notification(self, ev: ExpectationValue) -> str:
        """通知用テキスト生成"""
        lines = [
            f"期待値: {ev.heat_label}",
            f"推奨: {ev.position_label}",
            f"確信度: {ev.confidence:.0f}%",
            f"R/R: {ev.risk_reward}",
        ]
        return "\n".join(lines)
