"""
Impermanent Loss Protection Module
3-tier IL protection system:
- Tier 1 (10% IL): Warning alert
- Tier 2 (20% IL): Reduce position size / narrow range
- Tier 3 (35% IL): Force close position

Also includes rebalance engine logic.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .config import CONFIG
from .price_feed import price_feed

logger = logging.getLogger("dlmm_bot.il_protection")


class ILTier(Enum):
    """IL severity tiers."""
    SAFE = "safe"           # < 10% IL
    WARNING = "warning"     # 10-20% IL
    REDUCE = "reduce"       # 20-35% IL
    FORCE_CLOSE = "close"   # > 35% IL


@dataclass
class ILConfig:
    """IL protection configuration."""
    # Tier thresholds (% of deposited value lost to IL)
    tier1_warning_pct: float = 10.0
    tier2_reduce_pct: float = 20.0
    tier3_force_close_pct: float = 35.0

    # Actions
    tier2_reduce_by_pct: float = 50.0  # remove 50% of position on tier 2
    enable_force_close: bool = True

    # Calculation method
    use_actual_vs_hodl: bool = True  # compare LP value vs just holding SOL


@dataclass
class ILState:
    """IL tracking state for a position."""
    position_key: str
    entry_sol_value: float  # total SOL equivalent at entry
    entry_price: float      # token price at entry
    current_il_pct: float = 0.0
    tier: ILTier = ILTier.SAFE
    last_check: int = 0
    tier2_triggered: bool = False  # already reduced?
    warnings_sent: int = 0


class ILProtection:
    """
    Impermanent Loss protection engine.
    
    Calculates IL based on:
    IL% = (LP_value / HODL_value - 1) * 100
    
    For single-sided SOL entry:
    - Entry: X SOL deposited, 0 tokens
    - HODL value = X SOL (you'd still have X SOL if you didn't LP)
    - LP value = current SOL in position + token value in position
    - IL = (LP_value - HODL_value) / HODL_value * 100
    """

    def __init__(self, config: Optional[ILConfig] = None):
        self.cfg = config or ILConfig()
        self.states: Dict[str, ILState] = {}

    def register_position(
        self,
        position_key: str,
        sol_deposited: float,
        entry_price: float,
    ):
        """Register a new position for IL tracking."""
        self.states[position_key] = ILState(
            position_key=position_key,
            entry_sol_value=sol_deposited,
            entry_price=entry_price,
        )
        logger.debug(f"IL tracking registered for {position_key[:16]}...")

    def unregister_position(self, position_key: str):
        """Remove position from IL tracking."""
        self.states.pop(position_key, None)

    def calculate_il(
        self,
        position_key: str,
        current_sol_in_position: float,
        current_token_value_sol: float,
        current_price: float,
    ) -> Tuple[float, ILTier]:
        """
        Calculate current IL percentage and tier.
        
        Args:
            current_sol_in_position: SOL remaining in the position
            current_token_value_sol: Value of tokens in position (in SOL equivalent)
            current_price: Current token price
            
        Returns:
            (il_percentage, tier)
        """
        state = self.states.get(position_key)
        if not state:
            return 0.0, ILTier.SAFE

        # HODL value = what you'd have if you just held SOL
        hodl_value = state.entry_sol_value

        # LP value = current position value in SOL terms
        lp_value = current_sol_in_position + current_token_value_sol

        if hodl_value <= 0:
            return 0.0, ILTier.SAFE

        # IL % (negative means loss)
        il_pct = ((lp_value - hodl_value) / hodl_value) * 100

        # For single-sided SOL, if price drops and we got filled with tokens,
        # IL is the loss vs just holding SOL
        # il_pct will be negative when losing

        actual_il = abs(min(0, il_pct))  # Always positive for loss amount

        # Determine tier
        if actual_il >= self.cfg.tier3_force_close_pct:
            tier = ILTier.FORCE_CLOSE
        elif actual_il >= self.cfg.tier2_reduce_pct:
            tier = ILTier.REDUCE
        elif actual_il >= self.cfg.tier1_warning_pct:
            tier = ILTier.WARNING
        else:
            tier = ILTier.SAFE

        # Update state
        state.current_il_pct = actual_il
        state.tier = tier
        state.last_check = int(time.time())

        if tier != ILTier.SAFE:
            logger.warning(
                f"IL Alert [{position_key[:16]}...]: "
                f"{actual_il:.1f}% IL | Tier: {tier.value} | "
                f"LP={lp_value:.4f} vs HODL={hodl_value:.4f} SOL"
            )

        return actual_il, tier

    def get_action(self, position_key: str) -> Tuple[ILTier, str]:
        """
        Get recommended action for a position based on IL.
        
        Returns:
            (tier, action_description)
        """
        state = self.states.get(position_key)
        if not state:
            return ILTier.SAFE, "No IL data"

        if state.tier == ILTier.FORCE_CLOSE:
            return ILTier.FORCE_CLOSE, (
                f"FORCE CLOSE: IL={state.current_il_pct:.1f}% "
                f">= {self.cfg.tier3_force_close_pct}%"
            )

        if state.tier == ILTier.REDUCE and not state.tier2_triggered:
            state.tier2_triggered = True
            return ILTier.REDUCE, (
                f"REDUCE: IL={state.current_il_pct:.1f}% "
                f">= {self.cfg.tier2_reduce_pct}%. "
                f"Remove {self.cfg.tier2_reduce_by_pct:.0f}% of position."
            )

        if state.tier == ILTier.WARNING:
            state.warnings_sent += 1
            return ILTier.WARNING, (
                f"WARNING: IL={state.current_il_pct:.1f}% "
                f">= {self.cfg.tier1_warning_pct}%. Monitoring closely."
            )

        return ILTier.SAFE, "Position healthy"


# =============================================================================
# REBALANCE ENGINE
# =============================================================================

class RebalanceAction(Enum):
    """Rebalance actions."""
    NONE = "none"
    NARROW_RANGE = "narrow_range"      # Tighten bins around current price
    SHIFT_UP = "shift_up"              # Move range up (price went up)
    SHIFT_DOWN = "shift_down"          # Move range down (price went down)
    RECENTER = "recenter"              # Re-center around active bin
    TRIM_WINNER = "trim_winner"        # Take partial profit on winning side
    CUT_LOSER = "cut_loser"            # Remove liquidity from losing side


@dataclass
class RebalanceDecision:
    """Rebalance decision output."""
    action: RebalanceAction
    reason: str
    new_lower_bin: Optional[int] = None
    new_upper_bin: Optional[int] = None
    remove_pct: float = 0.0  # for trim/cut


class RebalanceEngine:
    """
    Rebalance engine for DLMM positions.
    
    Rules:
    - Only rebalance if position has been OOR for > 30s
    - Prefer up-only rebalance (don't chase downtrend)
    - Trim winners at edge of range (take profit in SOL)
    - Cut losers if IL exceeds threshold
    - Never rebalance heart_attack or fresh_runner (too short)
    """

    def __init__(self):
        self._last_rebalance: Dict[str, int] = {}  # position_key -> timestamp
        self.min_rebalance_interval = 60  # minimum 60s between rebalances

    def evaluate(
        self,
        position_key: str,
        active_bin: int,
        lower_bin: int,
        upper_bin: int,
        entry_bin: int,
        is_in_range: bool,
        out_of_range_seconds: int,
        price_direction: str,  # "up", "down", "sideways"
        il_pct: float,
        fees_earned_pct: float,
        strategy_allows_rebalance: bool = True,
    ) -> RebalanceDecision:
        """
        Evaluate whether to rebalance a position.
        
        Args:
            active_bin: Current active bin in the pool
            lower_bin/upper_bin: Current position range
            entry_bin: Active bin when position was opened
            is_in_range: Whether active bin is within position range
            out_of_range_seconds: How long OOR (0 if in range)
            price_direction: Recent price movement
            il_pct: Current impermanent loss %
            fees_earned_pct: Fees earned as % of capital
            strategy_allows_rebalance: Some strategies don't allow it
        """
        # Don't rebalance if strategy doesn't allow
        if not strategy_allows_rebalance:
            return RebalanceDecision(
                action=RebalanceAction.NONE,
                reason="Strategy does not allow rebalance"
            )

        # Don't rebalance too frequently
        last = self._last_rebalance.get(position_key, 0)
        if time.time() - last < self.min_rebalance_interval:
            return RebalanceDecision(
                action=RebalanceAction.NONE,
                reason="Too soon since last rebalance"
            )

        total_bins = upper_bin - lower_bin
        if total_bins <= 0:
            return RebalanceDecision(
                action=RebalanceAction.NONE,
                reason="Invalid bin range"
            )

        # --- RULE 1: Cut loser if IL too high ---
        if il_pct >= 25:
            return RebalanceDecision(
                action=RebalanceAction.CUT_LOSER,
                reason=f"IL={il_pct:.1f}% too high, cutting losing side",
                remove_pct=50.0,
            )

        # --- RULE 2: Trim winner if fees are good and near edge ---
        if is_in_range and fees_earned_pct >= 3.0:
            bins_from_upper = upper_bin - active_bin
            bins_from_lower = active_bin - lower_bin
            edge_threshold = max(3, total_bins * 0.1)

            if bins_from_upper <= edge_threshold and price_direction == "up":
                return RebalanceDecision(
                    action=RebalanceAction.TRIM_WINNER,
                    reason=f"Price near upper edge ({bins_from_upper} bins), taking partial profit",
                    remove_pct=30.0,
                )

        # --- RULE 3: Recenter if OOR for too long ---
        if not is_in_range and out_of_range_seconds >= 30:
            # Only recenter upward (up-only rebalance)
            if price_direction == "up" and active_bin > upper_bin:
                half_range = total_bins // 2
                new_lower = active_bin - half_range
                new_upper = active_bin + half_range
                self._last_rebalance[position_key] = int(time.time())
                return RebalanceDecision(
                    action=RebalanceAction.SHIFT_UP,
                    reason=f"Price moved above range, shifting up (up-only rebalance)",
                    new_lower_bin=new_lower,
                    new_upper_bin=new_upper,
                )

            # Don't chase downtrend — let position close via exit signals
            if price_direction == "down" and active_bin < lower_bin:
                return RebalanceDecision(
                    action=RebalanceAction.NONE,
                    reason="Price below range but NOT chasing downtrend (let exit signals handle)"
                )

            # Sideways OOR — recenter
            if price_direction == "sideways" and out_of_range_seconds >= 120:
                half_range = total_bins // 2
                new_lower = active_bin - half_range
                new_upper = active_bin + half_range
                self._last_rebalance[position_key] = int(time.time())
                return RebalanceDecision(
                    action=RebalanceAction.RECENTER,
                    reason=f"OOR {out_of_range_seconds}s in sideways, recentering",
                    new_lower_bin=new_lower,
                    new_upper_bin=new_upper,
                )

        return RebalanceDecision(
            action=RebalanceAction.NONE,
            reason="No rebalance needed"
        )
