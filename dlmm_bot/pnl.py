"""
PnL Tracking Module
Tracks net profit/loss: fees - IL - rent - swap costs - failed tx.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import CONFIG
from .position_manager import ActivePosition

logger = logging.getLogger("dlmm_bot.pnl")



@dataclass
class PositionPnL:
    """PnL record for a single position."""
    position_key: str
    pool_address: str
    pool_name: str
    entry_time: int
    close_time: int = 0
    close_reason: str = ""

    # Revenue
    fees_earned_sol: float = 0.0
    fees_earned_usd: float = 0.0

    # Costs
    sol_deposited: float = 0.0
    sol_recovered: float = 0.0  # SOL back after close
    token_recovered_value_usd: float = 0.0
    rent_cost_sol: float = 0.005  # ~0.005 SOL for position account
    swap_cost_sol: float = 0.0
    tx_fees_sol: float = 0.0
    failed_tx_cost_sol: float = 0.0

    # Impermanent loss (estimated)
    il_estimated_usd: float = 0.0

    # Net
    net_pnl_sol: float = 0.0
    net_pnl_usd: float = 0.0

    # Duration
    duration_seconds: int = 0

    def calculate_net(self, sol_price: float = 170.0):
        """Calculate net PnL."""
        self.duration_seconds = (
            self.close_time - self.entry_time
            if self.close_time > 0
            else 0
        )

        total_costs_sol = (
            self.rent_cost_sol
            + self.swap_cost_sol
            + self.tx_fees_sol
            + self.failed_tx_cost_sol
        )

        # Net SOL = fees + recovered SOL - deposited - costs
        self.net_pnl_sol = (
            self.fees_earned_sol
            + self.sol_recovered
            - self.sol_deposited
            - total_costs_sol
        )

        # Net USD
        self.net_pnl_usd = (
            self.fees_earned_usd
            + self.token_recovered_value_usd
            + (self.sol_recovered - self.sol_deposited) * sol_price
            - total_costs_sol * sol_price
            - self.il_estimated_usd
        )


@dataclass
class SessionPnL:
    """Aggregate PnL for a bot session."""
    session_start: int = field(default_factory=lambda: int(time.time()))
    total_positions_opened: int = 0
    total_positions_closed: int = 0
    total_fees_earned_sol: float = 0.0
    total_fees_earned_usd: float = 0.0
    total_costs_sol: float = 0.0
    total_net_pnl_sol: float = 0.0
    total_net_pnl_usd: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    best_trade_usd: float = 0.0
    worst_trade_usd: float = 0.0



class PnLTracker:
    """Tracks and persists PnL data."""

    def __init__(self):
        self.cfg = CONFIG.pnl
        self.session = SessionPnL()
        self.records: List[PositionPnL] = []
        self._log_path = Path(self.cfg.log_file)
        self._load_history()

    def _load_history(self):
        """Load previous PnL records from file on restart."""
        if self._log_path.exists():
            try:
                with open(self._log_path, "r") as f:
                    data = json.load(f)
                logger.info(f"[PnL] Loaded {len(data)} historical records from {self._log_path}")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"[PnL] Could not load history: {e}")


    def record_position_open(self, position: ActivePosition):
        """Record a new position opening."""
        self.session.total_positions_opened += 1
        logger.info(
            f"[PnL] Position opened: {position.pool_name} | "
            f"Deposited: {position.sol_deposited} SOL | "
            f"Total open: {self.session.total_positions_opened}"
        )

    def record_position_close(
        self,
        position: ActivePosition,
        sol_recovered: float = 0.0,
        token_value_usd: float = 0.0,
        swap_cost: float = 0.0,
        failed_tx_cost: float = 0.0,
    ):
        """Record a position closure and calculate PnL."""
        record = PositionPnL(
            position_key=position.position_pubkey,
            pool_address=position.pool_address,
            pool_name=position.pool_name,
            entry_time=position.entry_time,
            close_time=int(time.time()),
            close_reason=position.close_reason,
            fees_earned_sol=position.fees_earned_sol,
            fees_earned_usd=position.fees_earned_usd,
            sol_deposited=position.sol_deposited,
            sol_recovered=sol_recovered,
            token_recovered_value_usd=token_value_usd,
            swap_cost_sol=swap_cost,
            failed_tx_cost_sol=failed_tx_cost,
        )

        record.calculate_net()
        self.records.append(record)

        # Update session stats
        self.session.total_positions_closed += 1
        self.session.total_fees_earned_sol += record.fees_earned_sol
        self.session.total_fees_earned_usd += record.fees_earned_usd
        self.session.total_net_pnl_sol += record.net_pnl_sol
        self.session.total_net_pnl_usd += record.net_pnl_usd
        self.session.total_costs_sol += (
            record.rent_cost_sol + record.swap_cost_sol
            + record.tx_fees_sol + record.failed_tx_cost_sol
        )

        if record.net_pnl_usd > 0:
            self.session.win_count += 1
        else:
            self.session.loss_count += 1

        if record.net_pnl_usd > self.session.best_trade_usd:
            self.session.best_trade_usd = record.net_pnl_usd
        if record.net_pnl_usd < self.session.worst_trade_usd:
            self.session.worst_trade_usd = record.net_pnl_usd

        logger.info(
            f"[PnL] Position closed: {position.pool_name} | "
            f"Net: {record.net_pnl_sol:+.4f} SOL "
            f"(${record.net_pnl_usd:+.2f}) | "
            f"Fees: ${record.fees_earned_usd:.2f} | "
            f"Reason: {record.close_reason} | "
            f"Duration: {record.duration_seconds}s"
        )

        # Persist
        self._save_record(record)

    def _save_record(self, record: PositionPnL):
        """Append PnL record to log file."""
        try:
            data = []
            if self._log_path.exists():
                with open(self._log_path, "r") as f:
                    data = json.load(f)

            data.append(asdict(record))

            with open(self._log_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save PnL record: {e}")

    def print_summary(self):
        """Print session PnL summary."""
        s = self.session
        total = s.win_count + s.loss_count
        win_rate = (s.win_count / total * 100) if total > 0 else 0

        summary = (
            f"\n{'='*50}\n"
            f" SESSION PnL SUMMARY\n"
            f"{'='*50}\n"
            f" Positions: {s.total_positions_opened} opened, "
            f"{s.total_positions_closed} closed\n"
            f" Win/Loss: {s.win_count}W / {s.loss_count}L "
            f"({win_rate:.0f}% win rate)\n"
            f" Fees earned: {s.total_fees_earned_sol:.4f} SOL "
            f"(${s.total_fees_earned_usd:.2f})\n"
            f" Total costs: {s.total_costs_sol:.4f} SOL\n"
            f" Net PnL: {s.total_net_pnl_sol:+.4f} SOL "
            f"(${s.total_net_pnl_usd:+.2f})\n"
            f" Best trade: ${s.best_trade_usd:+.2f}\n"
            f" Worst trade: ${s.worst_trade_usd:+.2f}\n"
            f"{'='*50}"
        )
        logger.info(summary)
        return summary
