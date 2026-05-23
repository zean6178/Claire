"""
Decision Tree & Entry/Exit Checklist Engine
Master Playbook v1.0

Implements:
- 30-second entry checklist (must pass ALL items)
- Exit decision tree (any single trigger = close)
- Live management rules (rebalance, claim, flip)
- Kill-switch conditions

Each check returns (passed: bool, reason: str) for full auditability.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .scoring import MINIMUM_SCORE, PoolScore
from .strategies import StrategyPreset, StrategyType

logger = logging.getLogger("dlmm_bot.checklist")


# =============================================================================
# CHECKLIST RESULT
# =============================================================================

class CheckStatus(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"  # not applicable for this strategy


@dataclass
class CheckItem:
    """Single checklist item result."""
    name: str
    status: CheckStatus
    reason: str
    value: Any = None  # the actual measured value
    threshold: Any = None  # the threshold it was compared against


@dataclass
class ChecklistResult:
    """Complete checklist result."""
    passed: bool = False
    items: List[CheckItem] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    fail_reasons: List[str] = field(default_factory=list)
    timestamp: int = field(default_factory=lambda: int(time.time()))

    @property
    def pass_count(self) -> int:
        return sum(1 for i in self.items if i.status == CheckStatus.PASS)

    @property
    def fail_count(self) -> int:
        return sum(1 for i in self.items if i.status == CheckStatus.FAIL)

    @property
    def summary(self) -> str:
        total = len([i for i in self.items if i.status != CheckStatus.SKIP])
        return (
            f"{'PASS' if self.passed else 'FAIL'} "
            f"({self.pass_count}/{total} checks passed)"
        )


# =============================================================================
# ENTRY CHECKLIST (30-second pre-entry verification)
# =============================================================================

class EntryChecklist:
    """
    30-second entry checklist. ALL items must pass before opening a position.
    
    Checks in order:
    1. Score >= 70
    2. Pool price synced with Jupiter
    3. Volume active (not decaying)
    4. Chart not in waterfall
    5. Holders not dumping
    6. Liquidity sufficient
    7. Fee tier matches strategy
    8. Not already in this pool
    9. Budget available
    10. Risk limits not exceeded
    """

    def run(
        self,
        # Score
        pool_score: PoolScore,
        # Strategy
        strategy: StrategyPreset,
        # Market data
        pool_price: float,
        jupiter_price: float,
        volume_5m_usd: float,
        volume_prev_5m_usd: float,
        price_change_5m_pct: float,
        price_change_1h_pct: float,
        # Pool data
        holder_count: int,
        top5_holder_pct: float,
        liquidity_usd: float,
        fee_rate_bps: int,
        pool_age_seconds: int,
        bin_step: int,
        # Bot state
        already_in_pool: bool,
        available_sol: float,
        current_open_positions: int,
        daily_loss_sol: float,
        max_daily_loss_sol: float,
    ) -> ChecklistResult:
        """Run all entry checks. Returns ChecklistResult."""

        result = ChecklistResult()

        # 1. Score threshold
        result.items.append(self._check_score(pool_score))

        # 2. Price sync
        result.items.append(
            self._check_price_sync(pool_price, jupiter_price, strategy)
        )

        # 3. Volume active
        result.items.append(
            self._check_volume_active(volume_5m_usd, volume_prev_5m_usd, strategy)
        )

        # 4. Chart not waterfall
        result.items.append(
            self._check_chart_safe(price_change_5m_pct, price_change_1h_pct)
        )

        # 5. Holder safety
        result.items.append(
            self._check_holders(holder_count, top5_holder_pct, strategy)
        )

        # 6. Liquidity
        result.items.append(
            self._check_liquidity(liquidity_usd, strategy)
        )

        # 7. Fee tier
        result.items.append(
            self._check_fee_tier(fee_rate_bps, strategy)
        )

        # 8. Pool age
        result.items.append(
            self._check_pool_age(pool_age_seconds, strategy)
        )

        # 9. Bin step compatibility
        result.items.append(
            self._check_bin_step(bin_step, strategy)
        )

        # 10. Not duplicate
        result.items.append(
            self._check_not_duplicate(already_in_pool)
        )

        # 11. Budget available
        result.items.append(
            self._check_budget(available_sol, strategy)
        )

        # 12. Position limit
        result.items.append(
            self._check_position_limit(current_open_positions, strategy)
        )

        # 13. Daily loss limit
        result.items.append(
            self._check_daily_loss(daily_loss_sol, max_daily_loss_sol)
        )

        # Evaluate: ALL must pass (no FAIL items)
        result.fail_reasons = [
            f"{item.name}: {item.reason}"
            for item in result.items
            if item.status == CheckStatus.FAIL
        ]
        result.warnings = [
            f"{item.name}: {item.reason}"
            for item in result.items
            if item.status == CheckStatus.WARN
        ]
        result.passed = len(result.fail_reasons) == 0

        # Log
        if result.passed:
            logger.info(f"Entry checklist: {result.summary}")
            for w in result.warnings:
                logger.warning(f"  WARN: {w}")
        else:
            logger.info(
                f"Entry checklist: {result.summary} | "
                f"Failures: {result.fail_reasons}"
            )

        return result

    # --- Individual checks ---

    def _check_score(self, score: PoolScore) -> CheckItem:
        if score.auto_rejected:
            return CheckItem(
                "Score", CheckStatus.FAIL,
                f"Auto-rejected: {score.reject_reason}",
                score.total_score, MINIMUM_SCORE
            )
        if score.total_score >= MINIMUM_SCORE:
            return CheckItem(
                "Score", CheckStatus.PASS,
                f"{score.total_score:.0f} >= {MINIMUM_SCORE}",
                score.total_score, MINIMUM_SCORE
            )
        return CheckItem(
            "Score", CheckStatus.FAIL,
            f"{score.total_score:.0f} < {MINIMUM_SCORE}",
            score.total_score, MINIMUM_SCORE
        )

    def _check_price_sync(
        self, pool_price: float, jup_price: float, strategy: StrategyPreset
    ) -> CheckItem:
        if pool_price <= 0 or jup_price <= 0:
            return CheckItem(
                "PriceSync", CheckStatus.WARN,
                "Cannot verify (missing price data)", None, None
            )
        desync = abs(pool_price - jup_price) / jup_price * 100
        threshold = strategy.price_sync_tolerance_pct
        if desync <= threshold:
            return CheckItem(
                "PriceSync", CheckStatus.PASS,
                f"{desync:.1f}% <= {threshold}%", desync, threshold
            )
        return CheckItem(
            "PriceSync", CheckStatus.FAIL,
            f"{desync:.1f}% > {threshold}% (pool desynced)", desync, threshold
        )

    def _check_volume_active(
        self, vol_5m: float, vol_prev: float, strategy: StrategyPreset
    ) -> CheckItem:
        min_vol = strategy.min_volume_5m_usd
        if vol_5m >= min_vol:
            # Also check not decaying too fast
            if vol_prev > 0 and vol_5m < vol_prev * 0.3:
                return CheckItem(
                    "VolumeActive", CheckStatus.WARN,
                    f"Volume decaying fast (${vol_5m:.0f} vs prev ${vol_prev:.0f})",
                    vol_5m, min_vol
                )
            return CheckItem(
                "VolumeActive", CheckStatus.PASS,
                f"${vol_5m:.0f} >= ${min_vol:.0f}", vol_5m, min_vol
            )
        return CheckItem(
            "VolumeActive", CheckStatus.FAIL,
            f"${vol_5m:.0f} < ${min_vol:.0f}", vol_5m, min_vol
        )

    def _check_chart_safe(
        self, change_5m: float, change_1h: float
    ) -> CheckItem:
        # Waterfall detection: both timeframes strongly negative
        if change_5m < -15 and change_1h < -30:
            return CheckItem(
                "ChartSafe", CheckStatus.FAIL,
                f"Waterfall detected (5m={change_5m:.1f}%, 1h={change_1h:.1f}%)",
                change_5m, -15
            )
        if change_5m < -10:
            return CheckItem(
                "ChartSafe", CheckStatus.WARN,
                f"Short-term dip (5m={change_5m:.1f}%)", change_5m, -10
            )
        return CheckItem(
            "ChartSafe", CheckStatus.PASS,
            f"Chart OK (5m={change_5m:+.1f}%, 1h={change_1h:+.1f}%)",
            change_5m, None
        )

    def _check_holders(
        self, count: int, top5_pct: float, strategy: StrategyPreset
    ) -> CheckItem:
        min_holders = strategy.min_holders
        max_top5 = strategy.max_top5_holder_pct

        if count < min_holders:
            return CheckItem(
                "Holders", CheckStatus.FAIL,
                f"{count} < {min_holders} holders", count, min_holders
            )
        if top5_pct > max_top5:
            return CheckItem(
                "Holders", CheckStatus.FAIL,
                f"Top5 hold {top5_pct:.0f}% > {max_top5:.0f}%", top5_pct, max_top5
            )
        return CheckItem(
            "Holders", CheckStatus.PASS,
            f"{count} holders, top5={top5_pct:.0f}%", count, min_holders
        )

    def _check_liquidity(
        self, liq_usd: float, strategy: StrategyPreset
    ) -> CheckItem:
        # Minimum liquidity = at least 5x our position size in USD
        min_liq = strategy.max_position_sol * 170 * 5  # rough estimate
        if liq_usd >= min_liq:
            return CheckItem(
                "Liquidity", CheckStatus.PASS,
                f"${liq_usd:.0f} >= ${min_liq:.0f}", liq_usd, min_liq
            )
        return CheckItem(
            "Liquidity", CheckStatus.FAIL,
            f"${liq_usd:.0f} < ${min_liq:.0f} (too thin)", liq_usd, min_liq
        )

    def _check_fee_tier(
        self, fee_bps: int, strategy: StrategyPreset
    ) -> CheckItem:
        if strategy.min_fee_rate_bps <= fee_bps <= strategy.max_fee_rate_bps:
            return CheckItem(
                "FeeTier", CheckStatus.PASS,
                f"{fee_bps}bps in range [{strategy.min_fee_rate_bps}-{strategy.max_fee_rate_bps}]",
                fee_bps, (strategy.min_fee_rate_bps, strategy.max_fee_rate_bps)
            )
        return CheckItem(
            "FeeTier", CheckStatus.FAIL,
            f"{fee_bps}bps outside [{strategy.min_fee_rate_bps}-{strategy.max_fee_rate_bps}]",
            fee_bps, (strategy.min_fee_rate_bps, strategy.max_fee_rate_bps)
        )

    def _check_pool_age(
        self, age_s: int, strategy: StrategyPreset
    ) -> CheckItem:
        if strategy.min_pool_age_seconds <= age_s <= strategy.max_pool_age_seconds:
            return CheckItem(
                "PoolAge", CheckStatus.PASS,
                f"{age_s}s in [{strategy.min_pool_age_seconds}-{strategy.max_pool_age_seconds}]",
                age_s, None
            )
        return CheckItem(
            "PoolAge", CheckStatus.FAIL,
            f"{age_s}s outside [{strategy.min_pool_age_seconds}-{strategy.max_pool_age_seconds}]",
            age_s, None
        )

    def _check_bin_step(
        self, bin_step: int, strategy: StrategyPreset
    ) -> CheckItem:
        if bin_step in strategy.bin_step_preferred:
            return CheckItem(
                "BinStep", CheckStatus.PASS,
                f"{bin_step} is preferred", bin_step, strategy.bin_step_preferred
            )
        # Not preferred but not a hard fail - warn
        return CheckItem(
            "BinStep", CheckStatus.WARN,
            f"{bin_step} not in preferred {strategy.bin_step_preferred}",
            bin_step, strategy.bin_step_preferred
        )

    def _check_not_duplicate(self, already_in: bool) -> CheckItem:
        if not already_in:
            return CheckItem("NoDuplicate", CheckStatus.PASS, "Not in pool", False, False)
        return CheckItem(
            "NoDuplicate", CheckStatus.FAIL,
            "Already have position in this pool", True, False
        )

    def _check_budget(
        self, available_sol: float, strategy: StrategyPreset
    ) -> CheckItem:
        needed = strategy.max_position_sol
        if available_sol >= needed:
            return CheckItem(
                "Budget", CheckStatus.PASS,
                f"{available_sol:.2f} SOL available >= {needed} needed",
                available_sol, needed
            )
        return CheckItem(
            "Budget", CheckStatus.FAIL,
            f"{available_sol:.2f} SOL < {needed} needed",
            available_sol, needed
        )

    def _check_position_limit(
        self, current: int, strategy: StrategyPreset
    ) -> CheckItem:
        max_pos = strategy.max_concurrent_same_strategy
        if current < max_pos:
            return CheckItem(
                "PosLimit", CheckStatus.PASS,
                f"{current}/{max_pos} positions", current, max_pos
            )
        return CheckItem(
            "PosLimit", CheckStatus.FAIL,
            f"{current}/{max_pos} (limit reached)", current, max_pos
        )

    def _check_daily_loss(
        self, daily_loss: float, max_loss: float
    ) -> CheckItem:
        if daily_loss < max_loss:
            return CheckItem(
                "DailyLoss", CheckStatus.PASS,
                f"{daily_loss:.3f} SOL lost < {max_loss} limit",
                daily_loss, max_loss
            )
        return CheckItem(
            "DailyLoss", CheckStatus.FAIL,
            f"Daily loss {daily_loss:.3f} SOL >= {max_loss} KILL SWITCH",
            daily_loss, max_loss
        )


# =============================================================================
# EXIT DECISION TREE
# =============================================================================

class ExitDecision(Enum):
    """Exit decisions with priority."""
    HOLD = "hold"                         # No exit needed
    CLOSE_FEE_TARGET = "fee_target"       # Take profit
    CLOSE_VOLUME_DECAY = "volume_decay"   # Volume died
    CLOSE_PRICE_CRASH = "price_crash"     # Price breakdown
    CLOSE_DURATION = "max_duration"       # Timed out
    CLOSE_OOR = "out_of_range"            # Out of range too long
    CLOSE_INVENTORY = "inventory_cap"     # Too much meme token
    CLOSE_FEE_DECAY = "fee_per_min_decay" # Fee/min collapsed
    CLOSE_KILL_SWITCH = "kill_switch"     # Daily loss limit hit
    CLOSE_RUG = "rug_detected"            # Rug pull indicators
    CLOSE_MANUAL = "manual"               # Manual close requested


@dataclass
class ExitCheckResult:
    """Result of exit decision tree."""
    decision: ExitDecision = ExitDecision.HOLD
    reason: str = ""
    urgency: int = 0  # 0-10, higher = close faster
    details: Dict[str, Any] = field(default_factory=dict)


class ExitDecisionTree:
    """
    Exit decision tree. Evaluates ALL conditions and returns
    the highest-priority exit signal.
    
    Priority order (highest first):
    1. Kill switch (daily loss)
    2. Rug detected
    3. Price crash
    4. Inventory cap exceeded
    5. Out of range timeout
    6. Fee/min decay
    7. Volume decay
    8. Max duration
    9. Fee target hit (this is a GOOD exit)
    """

    def evaluate(
        self,
        strategy: StrategyPreset,
        # Position state
        entry_time: int,
        entry_price: float,
        entry_volume_5m: float,
        peak_fee_per_min: float,
        # Current state
        current_price: float,
        current_volume_5m: float,
        current_fee_per_min: float,
        fees_earned_pct: float,  # fees as % of capital
        inventory_meme_pct: float,  # % of position now in meme token
        is_in_range: bool,
        out_of_range_since: Optional[int],  # timestamp when went OOR
        # Risk state
        daily_loss_sol: float,
        max_daily_loss_sol: float,
        # Rug indicators
        large_holder_dumping: bool = False,
        liquidity_removed: bool = False,
    ) -> ExitCheckResult:
        """Evaluate exit conditions. Returns highest priority signal."""

        now = int(time.time())
        results: List[ExitCheckResult] = []

        # 1. KILL SWITCH — daily loss exceeded
        if daily_loss_sol >= max_daily_loss_sol:
            results.append(ExitCheckResult(
                decision=ExitDecision.CLOSE_KILL_SWITCH,
                reason=f"Daily loss {daily_loss_sol:.3f} >= {max_daily_loss_sol} SOL",
                urgency=10,
            ))

        # 2. RUG DETECTED — large holder dump or liquidity pull
        if large_holder_dumping or liquidity_removed:
            reasons = []
            if large_holder_dumping:
                reasons.append("top holder dumping")
            if liquidity_removed:
                reasons.append("liquidity removed")
            results.append(ExitCheckResult(
                decision=ExitDecision.CLOSE_RUG,
                reason=f"Rug indicators: {', '.join(reasons)}",
                urgency=9,
            ))

        # 3. PRICE CRASH
        if entry_price > 0 and current_price > 0:
            price_change_pct = (current_price - entry_price) / entry_price * 100
            if price_change_pct <= -strategy.price_drop_exit_pct:
                results.append(ExitCheckResult(
                    decision=ExitDecision.CLOSE_PRICE_CRASH,
                    reason=(
                        f"Price dropped {price_change_pct:.1f}% "
                        f"(threshold: -{strategy.price_drop_exit_pct}%)"
                    ),
                    urgency=8,
                    details={"price_change_pct": price_change_pct},
                ))

        # 4. INVENTORY CAP
        if inventory_meme_pct >= strategy.inventory_meme_max_pct:
            results.append(ExitCheckResult(
                decision=ExitDecision.CLOSE_INVENTORY,
                reason=(
                    f"Meme inventory {inventory_meme_pct:.0f}% "
                    f">= {strategy.inventory_meme_max_pct:.0f}%"
                ),
                urgency=7,
            ))

        # 5. OUT OF RANGE TIMEOUT
        if not is_in_range and out_of_range_since is not None:
            oor_duration = now - out_of_range_since
            if oor_duration >= strategy.out_of_range_max_seconds:
                results.append(ExitCheckResult(
                    decision=ExitDecision.CLOSE_OOR,
                    reason=(
                        f"Out of range for {oor_duration}s "
                        f"(max: {strategy.out_of_range_max_seconds}s)"
                    ),
                    urgency=6,
                    details={"oor_seconds": oor_duration},
                ))

        # 6. FEE/MIN DECAY
        if peak_fee_per_min > 0 and current_fee_per_min >= 0:
            fee_decay_pct = (1 - current_fee_per_min / peak_fee_per_min) * 100
            if fee_decay_pct >= strategy.fee_per_min_decay_pct:
                results.append(ExitCheckResult(
                    decision=ExitDecision.CLOSE_FEE_DECAY,
                    reason=(
                        f"Fee/min decayed {fee_decay_pct:.0f}% from peak "
                        f"(threshold: {strategy.fee_per_min_decay_pct}%)"
                    ),
                    urgency=5,
                    details={"fee_decay_pct": fee_decay_pct},
                ))

        # 7. VOLUME DECAY
        if entry_volume_5m > 0 and current_volume_5m >= 0:
            vol_ratio_pct = (current_volume_5m / entry_volume_5m) * 100
            if vol_ratio_pct < strategy.volume_decay_exit_pct:
                results.append(ExitCheckResult(
                    decision=ExitDecision.CLOSE_VOLUME_DECAY,
                    reason=(
                        f"Volume at {vol_ratio_pct:.0f}% of entry "
                        f"(threshold: {strategy.volume_decay_exit_pct}%)"
                    ),
                    urgency=4,
                    details={"volume_ratio_pct": vol_ratio_pct},
                ))

        # 8. MAX DURATION
        duration = now - entry_time
        if duration >= strategy.max_duration_seconds:
            results.append(ExitCheckResult(
                decision=ExitDecision.CLOSE_DURATION,
                reason=(
                    f"Duration {duration}s >= max {strategy.max_duration_seconds}s"
                ),
                urgency=3,
                details={"duration_seconds": duration},
            ))

        # 9. FEE TARGET HIT (positive exit!)
        if fees_earned_pct >= strategy.fee_target_pct:
            results.append(ExitCheckResult(
                decision=ExitDecision.CLOSE_FEE_TARGET,
                reason=(
                    f"Fee target reached: {fees_earned_pct:.1f}% "
                    f">= {strategy.fee_target_pct}%"
                ),
                urgency=2,  # low urgency, it's a good thing
                details={"fees_earned_pct": fees_earned_pct},
            ))

        # Return highest urgency signal
        if results:
            results.sort(key=lambda r: r.urgency, reverse=True)
            winner = results[0]
            logger.info(
                f"EXIT DECISION: {winner.decision.value} | "
                f"Urgency: {winner.urgency}/10 | {winner.reason}"
            )
            return winner

        # No exit signal
        return ExitCheckResult(decision=ExitDecision.HOLD, reason="All clear")


# =============================================================================
# LIVE MANAGEMENT RULES
# =============================================================================

class LiveManagementAction(Enum):
    """Actions during active position."""
    DO_NOTHING = "do_nothing"
    CLAIM_FEES = "claim_fees"
    NARROW_RANGE = "narrow_range"
    WIDEN_RANGE = "widen_range"
    REBALANCE = "rebalance"


@dataclass
class ManagementAdvice:
    """Advice for managing an active position."""
    action: LiveManagementAction
    reason: str


def get_management_advice(
    strategy: StrategyPreset,
    fees_unclaimed_usd: float,
    time_since_last_claim_seconds: int,
    is_in_range: bool,
    bins_from_edge: int,  # how many bins from going OOR
    total_bins: int,
) -> ManagementAdvice:
    """
    Advise on live position management.
    
    Rules:
    - Claim fees every 5-10 min to lock in profit
    - If approaching edge of range, consider rebalance
    - Never widen range on heart_attack/HFL
    """

    # Claim fees if unclaimed > $1 and > 5 min since last claim
    if fees_unclaimed_usd >= 1.0 and time_since_last_claim_seconds >= 300:
        return ManagementAdvice(
            LiveManagementAction.CLAIM_FEES,
            f"${fees_unclaimed_usd:.2f} unclaimed, {time_since_last_claim_seconds}s since last"
        )

    # Close to edge warning
    if is_in_range and total_bins > 0:
        edge_pct = bins_from_edge / total_bins * 100
        if edge_pct <= 10:  # within 10% of going OOR
            if strategy.strategy_type in (StrategyType.HFL,):
                return ManagementAdvice(
                    LiveManagementAction.REBALANCE,
                    f"Near edge ({bins_from_edge} bins), HFL should rebalance"
                )
            elif strategy.strategy_type not in (
                StrategyType.HEART_ATTACK,
                StrategyType.FRESH_RUNNER,
            ):
                return ManagementAdvice(
                    LiveManagementAction.WIDEN_RANGE,
                    f"Near edge ({bins_from_edge}/{total_bins} bins from OOR)"
                )

    return ManagementAdvice(
        LiveManagementAction.DO_NOTHING,
        "Position healthy, no action needed"
    )
