"""
Risk Management Module
Master Playbook v1.0

Implements:
- Daily loss limit (kill-switch)
- Max open exposure tracking
- Per-strategy allocation limits
- Inventory composition monitoring
- Consecutive loss circuit breaker
- Cooldown after losses
- Position sizing model based on bankroll & Kelly-lite
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .config import CONFIG
from .strategies import DEFAULT_ALLOCATION, StrategyType

logger = logging.getLogger("dlmm_bot.risk")


# =============================================================================
# RISK CONFIGURATION
# =============================================================================

@dataclass
class RiskConfig:
    """Risk management parameters."""

    # --- Kill Switch ---
    max_daily_loss_sol: float = 1.0          # Stop all trading if lost > 1 SOL today
    max_daily_loss_pct: float = 20.0         # Or 20% of bankroll

    # --- Exposure Limits ---
    max_total_exposure_sol: float = 5.0      # Max SOL deployed across all positions
    max_single_position_sol: float = 1.0     # Max per position
    max_open_positions: int = 5              # Max concurrent

    # --- Per-Strategy Limits ---
    max_per_strategy: Dict[StrategyType, int] = field(default_factory=lambda: {
        StrategyType.FRESH_RUNNER: 3,
        StrategyType.HEART_ATTACK: 2,
        StrategyType.AFTER_WAR: 3,
        StrategyType.BID_ASK_FLIP: 2,
        StrategyType.OVERNIGHT: 2,
        StrategyType.BEAR_MARKET: 2,
        StrategyType.HFL: 3,
    })

    # --- Inventory Cap ---
    max_meme_inventory_pct: float = 70.0     # Close if meme > 70% of position
    max_total_meme_value_sol: float = 2.0    # Max total meme token value held

    # --- Circuit Breaker ---
    max_consecutive_losses: int = 3          # Pause after 3 losses in a row
    cooldown_after_losses_seconds: int = 600 # 10 min cooldown
    cooldown_after_kill_switch_seconds: int = 3600  # 1h after kill switch

    # --- Position Sizing ---
    base_position_pct: float = 2.0           # Default: 2% of bankroll
    min_position_sol: float = 0.1            # Minimum 0.1 SOL
    kelly_fraction: float = 0.25             # Quarter Kelly for safety

    # --- Fee Threshold ---
    min_fee_before_close_usd: float = 1.0    # Don't close if earned < $1 (rent loss)

    # --- Time Guards ---
    min_time_between_entries_seconds: int = 10  # Don't spam entries
    max_entries_per_hour: int = 20


# =============================================================================
# RISK STATE
# =============================================================================

@dataclass
class DailyStats:
    """Daily trading statistics."""
    date: str = ""  # YYYY-MM-DD
    positions_opened: int = 0
    positions_closed: int = 0
    wins: int = 0
    losses: int = 0
    total_fees_sol: float = 0.0
    total_losses_sol: float = 0.0
    net_pnl_sol: float = 0.0
    consecutive_losses: int = 0
    peak_exposure_sol: float = 0.0


@dataclass
class RiskState:
    """Current risk state of the bot."""
    # Daily
    daily_stats: DailyStats = field(default_factory=DailyStats)

    # Kill switch
    kill_switch_active: bool = False
    kill_switch_triggered_at: int = 0

    # Cooldown
    in_cooldown: bool = False
    cooldown_until: int = 0
    cooldown_reason: str = ""

    # Exposure tracking
    current_exposure_sol: float = 0.0
    positions_by_strategy: Dict[str, int] = field(default_factory=dict)

    # Inventory
    total_meme_value_sol: float = 0.0

    # Entry timing
    last_entry_time: int = 0
    entries_this_hour: int = 0
    hour_start: int = 0

    # Blocked pools (recently closed with loss)
    blocked_pools: Set[str] = field(default_factory=set)


# =============================================================================
# RISK MANAGER
# =============================================================================

class RiskManager:
    """
    Central risk management engine.
    
    Called before every entry and continuously during operation.
    Can block entries and force exits.
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        self.cfg = config or RiskConfig()
        self.state = RiskState()
        self._init_daily()

    def _init_daily(self):
        """Initialize daily stats for today."""
        today = time.strftime("%Y-%m-%d")
        if self.state.daily_stats.date != today:
            self.state.daily_stats = DailyStats(date=today)
            # Reset kill switch if new day
            if self.state.kill_switch_active:
                logger.info("New day — resetting kill switch")
                self.state.kill_switch_active = False

    # =========================================================================
    # PRE-ENTRY CHECKS
    # =========================================================================

    def can_open_position(
        self,
        strategy_type: StrategyType,
        requested_sol: float,
        pool_address: str,
    ) -> tuple[bool, str]:
        """
        Check if a new position is allowed.
        Returns (allowed, reason).
        """
        self._init_daily()

        # 1. Kill switch
        if self.state.kill_switch_active:
            return False, "KILL SWITCH active — no new positions"

        # 2. Cooldown
        if self.state.in_cooldown:
            now = int(time.time())
            if now < self.state.cooldown_until:
                remaining = self.state.cooldown_until - now
                return False, f"In cooldown ({remaining}s remaining): {self.state.cooldown_reason}"
            else:
                self._end_cooldown()

        # 3. Daily loss limit
        if abs(self.state.daily_stats.net_pnl_sol) >= self.cfg.max_daily_loss_sol:
            if self.state.daily_stats.net_pnl_sol < 0:
                self._trigger_kill_switch("Daily loss limit hit")
                return False, f"Daily loss {self.state.daily_stats.net_pnl_sol:.3f} SOL exceeded limit"

        # 4. Max open positions
        total_open = sum(self.state.positions_by_strategy.values())
        if total_open >= self.cfg.max_open_positions:
            return False, f"Max positions reached ({total_open}/{self.cfg.max_open_positions})"

        # 5. Per-strategy limit
        strategy_key = strategy_type.value
        current_strategy_count = self.state.positions_by_strategy.get(strategy_key, 0)
        max_for_strategy = self.cfg.max_per_strategy.get(strategy_type, 3)
        if current_strategy_count >= max_for_strategy:
            return False, f"Strategy {strategy_key} at limit ({current_strategy_count}/{max_for_strategy})"

        # 6. Total exposure
        if self.state.current_exposure_sol + requested_sol > self.cfg.max_total_exposure_sol:
            return False, (
                f"Would exceed max exposure: "
                f"{self.state.current_exposure_sol:.2f} + {requested_sol:.2f} > "
                f"{self.cfg.max_total_exposure_sol:.2f}"
            )

        # 7. Single position size
        if requested_sol > self.cfg.max_single_position_sol:
            return False, f"Position {requested_sol} SOL > max {self.cfg.max_single_position_sol}"

        # 8. Blocked pool
        if pool_address in self.state.blocked_pools:
            return False, f"Pool {pool_address[:8]}... is blocked (recent loss)"

        # 9. Entry rate limit
        now = int(time.time())
        if now - self.state.last_entry_time < self.cfg.min_time_between_entries_seconds:
            return False, "Too fast — min time between entries not met"

        # 10. Hourly entry limit
        if now - self.state.hour_start >= 3600:
            self.state.entries_this_hour = 0
            self.state.hour_start = now
        if self.state.entries_this_hour >= self.cfg.max_entries_per_hour:
            return False, f"Hourly entry limit ({self.cfg.max_entries_per_hour}) reached"

        # 11. Meme inventory cap
        if self.state.total_meme_value_sol >= self.cfg.max_total_meme_value_sol:
            return False, f"Total meme inventory {self.state.total_meme_value_sol:.2f} SOL at cap"

        return True, "All risk checks passed"

    # =========================================================================
    # POSITION SIZING
    # =========================================================================

    def calculate_position_size(
        self,
        strategy_type: StrategyType,
        bankroll_sol: float,
        pool_score: float,
        win_rate: Optional[float] = None,
    ) -> float:
        """
        Calculate optimal position size.
        
        Model:
        - Base: strategy.position_size_pct_bankroll % of bankroll
        - Adjusted by score (85+ = full size, 70 = 60% size)
        - Adjusted by win rate if available (quarter Kelly)
        - Capped by risk limits
        """
        from .strategies import ALL_STRATEGIES

        preset = ALL_STRATEGIES.get(strategy_type)
        if not preset:
            return self.cfg.min_position_sol

        # Base size from strategy
        base_pct = preset.position_size_pct_bankroll / 100.0
        base_sol = bankroll_sol * base_pct

        # Score adjustment: scale linearly from 60% at score=70 to 100% at score=85+
        if pool_score >= 85:
            score_mult = 1.0
        elif pool_score >= 70:
            score_mult = 0.6 + (pool_score - 70) / 15 * 0.4
        else:
            score_mult = 0.5

        # Win rate adjustment (quarter Kelly)
        kelly_mult = 1.0
        if win_rate is not None and win_rate > 0:
            # Simplified Kelly: f = (p * b - q) / b
            # where p = win_rate, q = 1-p, b = avg_win/avg_loss (assume 1.5)
            b = 1.5
            p = min(win_rate, 0.8)
            q = 1 - p
            kelly = (p * b - q) / b
            kelly = max(0, kelly)
            kelly_mult = self.cfg.kelly_fraction * kelly / base_pct if base_pct > 0 else 1.0
            kelly_mult = max(0.5, min(kelly_mult, 2.0))  # bound 0.5x to 2x

        # Calculate final size
        size_sol = base_sol * score_mult * kelly_mult

        # Apply caps
        size_sol = max(size_sol, self.cfg.min_position_sol)
        size_sol = min(size_sol, self.cfg.max_single_position_sol)
        size_sol = min(size_sol, preset.max_position_sol)

        # Don't exceed remaining budget
        remaining = self.cfg.max_total_exposure_sol - self.state.current_exposure_sol
        size_sol = min(size_sol, remaining)

        logger.info(
            f"Position size: {size_sol:.3f} SOL "
            f"(base={base_sol:.3f}, score_mult={score_mult:.2f}, "
            f"kelly_mult={kelly_mult:.2f})"
        )

        return round(size_sol, 4)

    # =========================================================================
    # STATE UPDATES
    # =========================================================================

    def record_position_opened(
        self, strategy_type: StrategyType, sol_amount: float
    ):
        """Record a new position opening."""
        self.state.current_exposure_sol += sol_amount
        key = strategy_type.value
        self.state.positions_by_strategy[key] = (
            self.state.positions_by_strategy.get(key, 0) + 1
        )
        self.state.daily_stats.positions_opened += 1
        self.state.last_entry_time = int(time.time())
        self.state.entries_this_hour += 1

        # Track peak exposure
        if self.state.current_exposure_sol > self.state.daily_stats.peak_exposure_sol:
            self.state.daily_stats.peak_exposure_sol = self.state.current_exposure_sol

        logger.debug(
            f"Position opened: exposure={self.state.current_exposure_sol:.2f} SOL, "
            f"strategy={key}"
        )

    def record_position_closed(
        self,
        strategy_type: StrategyType,
        sol_returned: float,
        sol_deposited: float,
        pool_address: str,
        fees_earned_sol: float = 0.0,
    ):
        """Record a position closure and update risk state."""
        # Update exposure
        self.state.current_exposure_sol = max(
            0, self.state.current_exposure_sol - sol_deposited
        )
        key = strategy_type.value
        self.state.positions_by_strategy[key] = max(
            0, self.state.positions_by_strategy.get(key, 1) - 1
        )

        # Calculate PnL
        net_pnl = sol_returned + fees_earned_sol - sol_deposited
        self.state.daily_stats.positions_closed += 1
        self.state.daily_stats.net_pnl_sol += net_pnl
        self.state.daily_stats.total_fees_sol += fees_earned_sol

        if net_pnl >= 0:
            self.state.daily_stats.wins += 1
            self.state.daily_stats.consecutive_losses = 0
        else:
            self.state.daily_stats.losses += 1
            self.state.daily_stats.total_losses_sol += abs(net_pnl)
            self.state.daily_stats.consecutive_losses += 1
            # Block this pool temporarily
            self.state.blocked_pools.add(pool_address)

        # Check consecutive loss circuit breaker
        if (
            self.state.daily_stats.consecutive_losses
            >= self.cfg.max_consecutive_losses
        ):
            self._trigger_cooldown(
                self.cfg.cooldown_after_losses_seconds,
                f"{self.cfg.max_consecutive_losses} consecutive losses"
            )

        # Check daily loss kill switch
        if self.state.daily_stats.net_pnl_sol <= -self.cfg.max_daily_loss_sol:
            self._trigger_kill_switch(
                f"Daily loss {self.state.daily_stats.net_pnl_sol:.3f} SOL"
            )

        logger.info(
            f"Position closed: PnL={net_pnl:+.4f} SOL | "
            f"Daily: {self.state.daily_stats.net_pnl_sol:+.4f} SOL | "
            f"W/L: {self.state.daily_stats.wins}/{self.state.daily_stats.losses} | "
            f"Streak: {self.state.daily_stats.consecutive_losses} losses"
        )

    def update_meme_inventory(self, total_meme_value_sol: float):
        """Update total meme token inventory value."""
        self.state.total_meme_value_sol = total_meme_value_sol

    # =========================================================================
    # KILL SWITCH & COOLDOWN
    # =========================================================================

    def _trigger_kill_switch(self, reason: str):
        """Activate kill switch — stop ALL trading."""
        self.state.kill_switch_active = True
        self.state.kill_switch_triggered_at = int(time.time())
        logger.critical(
            f"KILL SWITCH ACTIVATED: {reason} | "
            f"All trading halted until next day or manual reset."
        )

    def _trigger_cooldown(self, duration_seconds: int, reason: str):
        """Enter cooldown period."""
        self.state.in_cooldown = True
        self.state.cooldown_until = int(time.time()) + duration_seconds
        self.state.cooldown_reason = reason
        logger.warning(
            f"COOLDOWN ACTIVATED: {reason} | "
            f"Duration: {duration_seconds}s"
        )

    def _end_cooldown(self):
        """End cooldown period."""
        self.state.in_cooldown = False
        self.state.cooldown_reason = ""
        logger.info("Cooldown ended — trading resumed")

    def reset_kill_switch(self):
        """Manual kill switch reset."""
        self.state.kill_switch_active = False
        logger.warning("Kill switch manually reset")

    # =========================================================================
    # QUERIES
    # =========================================================================

    def is_trading_allowed(self) -> tuple[bool, str]:
        """Quick check if any trading is allowed."""
        if self.state.kill_switch_active:
            return False, "Kill switch active"
        if self.state.in_cooldown and int(time.time()) < self.state.cooldown_until:
            return False, f"In cooldown: {self.state.cooldown_reason}"
        return True, "Trading allowed"

    def get_available_budget(self) -> float:
        """Get remaining SOL budget for new positions."""
        remaining = self.cfg.max_total_exposure_sol - self.state.current_exposure_sol
        return max(0, remaining)

    def get_win_rate(self) -> Optional[float]:
        """Get current session win rate."""
        total = self.state.daily_stats.wins + self.state.daily_stats.losses
        if total < 3:
            return None  # not enough data
        return self.state.daily_stats.wins / total

    def get_risk_summary(self) -> str:
        """Get formatted risk summary."""
        s = self.state.daily_stats
        total_trades = s.wins + s.losses
        win_rate = (s.wins / total_trades * 100) if total_trades > 0 else 0

        return (
            f"Risk Status: {'KILL SWITCH' if self.state.kill_switch_active else 'OK'}\n"
            f"  Exposure: {self.state.current_exposure_sol:.2f}/{self.cfg.max_total_exposure_sol} SOL\n"
            f"  Daily PnL: {s.net_pnl_sol:+.4f} SOL\n"
            f"  W/L: {s.wins}/{s.losses} ({win_rate:.0f}%)\n"
            f"  Consecutive losses: {s.consecutive_losses}\n"
            f"  Meme inventory: {self.state.total_meme_value_sol:.2f} SOL\n"
            f"  Entries today: {s.positions_opened}\n"
            f"  Kill switch: {'YES' if self.state.kill_switch_active else 'no'}\n"
            f"  Cooldown: {'YES' if self.state.in_cooldown else 'no'}"
        )
