"""
Token/Pool Scoring Model
Master Playbook v1.0 — Weighted scoring with 70/100 minimum threshold.

Scores a pool candidate on 8 dimensions with penalties.
Only pools scoring >= 70 are eligible for entry.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("dlmm_bot.scoring")


# =============================================================================
# SCORING WEIGHTS (total positive = 100)
# =============================================================================

@dataclass
class ScoreWeights:
    """
    Scoring dimensions and max points.
    Based on Master Playbook token scoring sheet.
    """
    volume_quality: int = 20       # Is volume real, sustained, growing?
    chart_structure: int = 15      # Uptrend/sideways/support, not waterfall
    holder_distribution: int = 15  # Not concentrated in few wallets
    liquidity_depth: int = 10      # Enough liquidity to avoid manipulation
    fee_per_min: int = 15          # Actual fee earning potential
    pool_jupiter_sync: int = 10    # Pool price matches Jupiter/market
    narrative_social: int = 5      # Community buzz, narrative momentum
    # Reserve 10 pts for bonus factors
    freshness_bonus: int = 5       # Sweet spot age bonus
    volume_growth_bonus: int = 5   # Volume increasing (not just high)

    @property
    def max_positive(self) -> int:
        return 100


@dataclass
class ScorePenalties:
    """
    Penalty dimensions (subtracted from score).
    Any single penalty can auto-reject a pool.
    """
    dev_wallet_risk: int = 20      # Dev can dump large %
    downtrend_risk: int = 20       # Confirmed downtrend
    wash_volume: int = 15          # Fake/circular volume detected
    low_txn_count: int = 10        # High volume but few transactions
    pool_desync: int = 15          # Pool price != market price
    honeypot_risk: int = 20        # Can't sell / high sell tax


# =============================================================================
# SCORING THRESHOLDS
# =============================================================================

MINIMUM_SCORE = 70          # Must score >= 70 to enter
AUTO_REJECT_PENALTY = 15    # Single penalty >= 15 = auto-reject
EXCELLENT_SCORE = 85        # Score >= 85 = priority entry


# =============================================================================
# SCORE RESULT
# =============================================================================

@dataclass
class PoolScore:
    """Complete scoring result for a pool."""
    pool_address: str
    pool_name: str
    total_score: float = 0.0
    passed: bool = False
    auto_rejected: bool = False
    reject_reason: str = ""

    # Individual dimension scores
    volume_quality_score: float = 0.0
    chart_structure_score: float = 0.0
    holder_distribution_score: float = 0.0
    liquidity_depth_score: float = 0.0
    fee_per_min_score: float = 0.0
    pool_sync_score: float = 0.0
    narrative_score: float = 0.0
    freshness_score: float = 0.0
    volume_growth_score: float = 0.0

    # Penalties applied
    penalties: Dict[str, float] = field(default_factory=dict)
    total_penalty: float = 0.0

    # Metadata
    details: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# SCORER
# =============================================================================

class PoolScorer:
    """
    Scores pools on a 0-100 scale with penalty deductions.
    
    Usage:
        scorer = PoolScorer()
        result = scorer.score_pool(pool_data)
        if result.passed:
            # eligible for entry
    """

    def __init__(self, weights: Optional[ScoreWeights] = None):
        self.weights = weights or ScoreWeights()

    def score_pool(
        self,
        pool_address: str,
        pool_name: str,
        # Volume metrics
        volume_5m_usd: float = 0.0,
        volume_1h_usd: float = 0.0,
        volume_prev_5m_usd: float = 0.0,  # previous 5-min for trend
        txn_count_5m: int = 0,
        # Chart/price
        price_change_1h_pct: float = 0.0,
        price_change_5m_pct: float = 0.0,
        is_downtrend: bool = False,
        has_support: bool = False,
        # Holders
        holder_count: int = 0,
        top5_holder_pct: float = 100.0,
        top10_holder_pct: float = 100.0,
        dev_wallet_pct: float = 0.0,
        # Liquidity
        liquidity_usd: float = 0.0,
        # Fees
        fee_rate_bps: int = 0,
        estimated_fee_per_min_usd: float = 0.0,
        # Pool sync
        pool_price: float = 0.0,
        jupiter_price: float = 0.0,
        # Social/narrative
        has_social_buzz: bool = False,
        has_narrative: bool = False,
        # Age
        pool_age_seconds: int = 0,
        # Flags
        is_honeypot: bool = False,
        has_wash_volume: bool = False,
    ) -> PoolScore:
        """
        Score a pool candidate. Returns PoolScore with pass/fail.
        """
        result = PoolScore(
            pool_address=pool_address,
            pool_name=pool_name,
        )

        # --- AUTO-REJECT CHECKS (before scoring) ---
        if is_honeypot:
            result.auto_rejected = True
            result.reject_reason = "Honeypot detected"
            result.penalties["honeypot_risk"] = 20
            logger.debug(f"[{pool_name}] AUTO-REJECT: honeypot")
            return result

        if has_wash_volume and volume_5m_usd > 0:
            result.auto_rejected = True
            result.reject_reason = "Wash volume detected"
            result.penalties["wash_volume"] = 15
            logger.debug(f"[{pool_name}] AUTO-REJECT: wash volume")
            return result

        # --- DIMENSION SCORING ---

        # 1. Volume Quality (0-20)
        result.volume_quality_score = self._score_volume_quality(
            volume_5m_usd, volume_1h_usd, txn_count_5m
        )

        # 2. Chart Structure (0-15)
        result.chart_structure_score = self._score_chart_structure(
            price_change_1h_pct, price_change_5m_pct,
            is_downtrend, has_support
        )

        # 3. Holder Distribution (0-15)
        result.holder_distribution_score = self._score_holders(
            holder_count, top5_holder_pct, top10_holder_pct
        )

        # 4. Liquidity Depth (0-10)
        result.liquidity_depth_score = self._score_liquidity(liquidity_usd)

        # 5. Fee/min (0-15)
        result.fee_per_min_score = self._score_fee_potential(
            estimated_fee_per_min_usd, volume_5m_usd, fee_rate_bps, liquidity_usd
        )

        # 6. Pool/Jupiter Sync (0-10)
        result.pool_sync_score = self._score_price_sync(
            pool_price, jupiter_price
        )

        # 7. Narrative/Social (0-5)
        result.narrative_score = self._score_narrative(
            has_social_buzz, has_narrative
        )

        # 8. Freshness Bonus (0-5)
        result.freshness_score = self._score_freshness(pool_age_seconds)

        # 9. Volume Growth Bonus (0-5)
        result.volume_growth_score = self._score_volume_growth(
            volume_5m_usd, volume_prev_5m_usd
        )

        # --- SUM POSITIVE SCORES ---
        raw_score = (
            result.volume_quality_score
            + result.chart_structure_score
            + result.holder_distribution_score
            + result.liquidity_depth_score
            + result.fee_per_min_score
            + result.pool_sync_score
            + result.narrative_score
            + result.freshness_score
            + result.volume_growth_score
        )

        # --- APPLY PENALTIES ---

        # Dev wallet risk
        if dev_wallet_pct >= 20:
            penalty = min(20, dev_wallet_pct * 0.8)
            result.penalties["dev_wallet_risk"] = penalty
        elif dev_wallet_pct >= 10:
            result.penalties["dev_wallet_risk"] = dev_wallet_pct * 0.5

        # Downtrend risk
        if is_downtrend:
            penalty = min(20, abs(price_change_1h_pct) * 0.5)
            result.penalties["downtrend_risk"] = max(penalty, 10)

        # Low txn count (volume but few txns = suspicious)
        if volume_5m_usd > 5000 and txn_count_5m < 20:
            result.penalties["low_txn_count"] = 10

        # Pool desync
        if pool_price > 0 and jupiter_price > 0:
            desync_pct = abs(pool_price - jupiter_price) / jupiter_price * 100
            if desync_pct > 5:
                result.penalties["pool_desync"] = min(15, desync_pct * 2)

        result.total_penalty = sum(result.penalties.values())

        # Check if any single penalty triggers auto-reject
        for penalty_name, penalty_val in result.penalties.items():
            if penalty_val >= AUTO_REJECT_PENALTY:
                result.auto_rejected = True
                result.reject_reason = f"Penalty too high: {penalty_name}={penalty_val:.0f}"
                break

        # --- FINAL SCORE ---
        result.total_score = max(0, raw_score - result.total_penalty)
        result.passed = (
            not result.auto_rejected
            and result.total_score >= MINIMUM_SCORE
        )

        # Log
        level = logging.INFO if result.passed else logging.DEBUG
        logger.log(
            level,
            f"[{pool_name}] Score: {result.total_score:.0f}/100 "
            f"({'PASS' if result.passed else 'FAIL'}) | "
            f"Vol={result.volume_quality_score:.0f} "
            f"Chart={result.chart_structure_score:.0f} "
            f"Hold={result.holder_distribution_score:.0f} "
            f"Liq={result.liquidity_depth_score:.0f} "
            f"Fee={result.fee_per_min_score:.0f} "
            f"Sync={result.pool_sync_score:.0f} "
            f"Pen=-{result.total_penalty:.0f}"
        )

        return result

    # =========================================================================
    # INDIVIDUAL SCORING FUNCTIONS
    # =========================================================================

    def _score_volume_quality(
        self, vol_5m: float, vol_1h: float, txn_count: int
    ) -> float:
        """Score volume quality 0-20."""
        score = 0.0
        max_pts = self.weights.volume_quality

        # 5-min volume thresholds
        if vol_5m >= 50000:
            score += max_pts * 0.5  # 10 pts
        elif vol_5m >= 20000:
            score += max_pts * 0.4
        elif vol_5m >= 10000:
            score += max_pts * 0.3
        elif vol_5m >= 5000:
            score += max_pts * 0.2
        elif vol_5m >= 2000:
            score += max_pts * 0.1

        # 1h volume (consistency)
        if vol_1h >= 200000:
            score += max_pts * 0.3
        elif vol_1h >= 100000:
            score += max_pts * 0.25
        elif vol_1h >= 50000:
            score += max_pts * 0.2
        elif vol_1h >= 20000:
            score += max_pts * 0.1

        # Transaction count (quality check)
        if txn_count >= 100:
            score += max_pts * 0.2
        elif txn_count >= 50:
            score += max_pts * 0.15
        elif txn_count >= 20:
            score += max_pts * 0.1

        return min(score, max_pts)

    def _score_chart_structure(
        self,
        change_1h: float,
        change_5m: float,
        is_downtrend: bool,
        has_support: bool,
    ) -> float:
        """Score chart structure 0-15."""
        max_pts = self.weights.chart_structure

        if is_downtrend:
            return 0.0  # no points for downtrend

        score = 0.0

        # Ideal: slight uptrend or sideways
        if -5 <= change_1h <= 30:
            score += max_pts * 0.4  # healthy range
        elif 30 < change_1h <= 100:
            score += max_pts * 0.3  # pumping but risky
        elif change_1h > 100:
            score += max_pts * 0.1  # too parabolic

        # 5-min momentum
        if -3 <= change_5m <= 10:
            score += max_pts * 0.3  # calm / slight up
        elif 10 < change_5m <= 30:
            score += max_pts * 0.2  # active

        # Support presence
        if has_support:
            score += max_pts * 0.3

        return min(score, max_pts)

    def _score_holders(
        self, count: int, top5_pct: float, top10_pct: float
    ) -> float:
        """Score holder distribution 0-15."""
        max_pts = self.weights.holder_distribution
        score = 0.0

        # Holder count
        if count >= 1000:
            score += max_pts * 0.4
        elif count >= 500:
            score += max_pts * 0.35
        elif count >= 300:
            score += max_pts * 0.3
        elif count >= 200:
            score += max_pts * 0.2
        elif count >= 100:
            score += max_pts * 0.1

        # Top 5 concentration (lower = better)
        if top5_pct <= 20:
            score += max_pts * 0.35
        elif top5_pct <= 30:
            score += max_pts * 0.3
        elif top5_pct <= 40:
            score += max_pts * 0.2
        elif top5_pct <= 50:
            score += max_pts * 0.1
        # >50% = 0 bonus

        # Top 10 distribution
        if top10_pct <= 40:
            score += max_pts * 0.25
        elif top10_pct <= 55:
            score += max_pts * 0.15
        elif top10_pct <= 70:
            score += max_pts * 0.05

        return min(score, max_pts)

    def _score_liquidity(self, liquidity_usd: float) -> float:
        """Score liquidity depth 0-10."""
        max_pts = self.weights.liquidity_depth

        if liquidity_usd >= 200000:
            return max_pts
        elif liquidity_usd >= 100000:
            return max_pts * 0.8
        elif liquidity_usd >= 50000:
            return max_pts * 0.6
        elif liquidity_usd >= 20000:
            return max_pts * 0.4
        elif liquidity_usd >= 10000:
            return max_pts * 0.2
        return 0.0

    def _score_fee_potential(
        self,
        fee_per_min: float,
        volume_5m: float,
        fee_rate_bps: int,
        liquidity_usd: float,
    ) -> float:
        """Score fee earning potential 0-15."""
        max_pts = self.weights.fee_per_min
        score = 0.0

        # Direct fee/min if available
        if fee_per_min > 0:
            if fee_per_min >= 50:
                score += max_pts * 0.6
            elif fee_per_min >= 20:
                score += max_pts * 0.5
            elif fee_per_min >= 10:
                score += max_pts * 0.4
            elif fee_per_min >= 5:
                score += max_pts * 0.3
            elif fee_per_min >= 1:
                score += max_pts * 0.2
        else:
            # Estimate from volume * fee_rate / liquidity
            if liquidity_usd > 0 and volume_5m > 0:
                fee_capture_ratio = (volume_5m * fee_rate_bps / 10000) / liquidity_usd
                if fee_capture_ratio >= 0.01:
                    score += max_pts * 0.5
                elif fee_capture_ratio >= 0.005:
                    score += max_pts * 0.3
                elif fee_capture_ratio >= 0.001:
                    score += max_pts * 0.15

        # Fee tier bonus
        if 200 <= fee_rate_bps <= 500:
            score += max_pts * 0.3  # sweet spot
        elif 100 <= fee_rate_bps < 200:
            score += max_pts * 0.2
        elif 500 < fee_rate_bps <= 1000:
            score += max_pts * 0.2  # high but risky

        return min(score, max_pts)

    def _score_price_sync(
        self, pool_price: float, jupiter_price: float
    ) -> float:
        """Score pool/Jupiter price sync 0-10."""
        max_pts = self.weights.pool_jupiter_sync

        if pool_price <= 0 or jupiter_price <= 0:
            return max_pts * 0.5  # can't verify = half points

        desync_pct = abs(pool_price - jupiter_price) / jupiter_price * 100

        if desync_pct <= 0.5:
            return max_pts       # perfect sync
        elif desync_pct <= 1.0:
            return max_pts * 0.8
        elif desync_pct <= 2.0:
            return max_pts * 0.6
        elif desync_pct <= 3.0:
            return max_pts * 0.3
        elif desync_pct <= 5.0:
            return max_pts * 0.1
        return 0.0  # >5% desync = danger

    def _score_narrative(
        self, has_buzz: bool, has_narrative: bool
    ) -> float:
        """Score social/narrative momentum 0-5."""
        max_pts = self.weights.narrative_social
        score = 0.0
        if has_buzz:
            score += max_pts * 0.6
        if has_narrative:
            score += max_pts * 0.4
        return min(score, max_pts)

    def _score_freshness(self, age_seconds: int) -> float:
        """Score pool freshness bonus 0-5."""
        max_pts = self.weights.freshness_bonus

        # Sweet spots
        if 120 <= age_seconds <= 600:
            return max_pts  # 2-10 min = perfect
        elif 600 < age_seconds <= 1800:
            return max_pts * 0.7  # 10-30 min = good
        elif 1800 < age_seconds <= 3600:
            return max_pts * 0.4  # 30-60 min = ok
        elif 60 <= age_seconds < 120:
            return max_pts * 0.5  # very new but ok
        return 0.0

    def _score_volume_growth(
        self, current_vol: float, prev_vol: float
    ) -> float:
        """Score volume growth trend 0-5."""
        max_pts = self.weights.volume_growth_bonus

        if prev_vol <= 0:
            return max_pts * 0.3  # unknown trend = small bonus

        growth_ratio = current_vol / prev_vol

        if growth_ratio >= 2.0:
            return max_pts  # volume doubling
        elif growth_ratio >= 1.5:
            return max_pts * 0.8
        elif growth_ratio >= 1.1:
            return max_pts * 0.5
        elif growth_ratio >= 0.9:
            return max_pts * 0.3  # flat is ok
        return 0.0  # declining volume
