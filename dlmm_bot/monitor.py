"""
Position Monitor Module
Tracks active positions: fee accrual, volume decay, price movement.
Triggers exit when conditions are met.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx

from .config import CONFIG
from .position_manager import ActivePosition, PositionManager
from .scanner import PoolScanner
from .utils import now_ts, retry

logger = logging.getLogger("dlmm_bot.monitor")



class ExitSignal:
    """Reasons to exit a position."""
    NONE = "none"
    VOLUME_DECAY = "volume_decay"
    FEE_TARGET_HIT = "fee_target_hit"
    MAX_DURATION = "max_duration"
    PRICE_BREAKDOWN = "price_breakdown"
    OUT_OF_RANGE = "out_of_range_timeout"
    MANUAL = "manual"
    RUG_DETECTED = "rug_detected"


class PositionMonitor:
    """Monitors active positions and generates exit signals."""

    def __init__(
        self,
        position_manager: PositionManager,
        scanner: PoolScanner,
    ):
        self.cfg = CONFIG.monitor
        self.pm = position_manager
        self.scanner = scanner
        self._running = False
        self._http: Optional[httpx.AsyncClient] = None

    async def initialize(self):
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30))

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()


    async def check_position(
        self, position: ActivePosition
    ) -> str:
        """
        Check a single position for exit signals.
        Returns ExitSignal constant.
        """
        now = now_ts()

        # 1. Max duration check
        duration = now - position.entry_time
        max_dur = self.cfg.max_position_duration_seconds
        if duration >= max_dur:
            logger.info(
                f"[{position.pool_name}] Max duration reached "
                f"({duration}s >= {max_dur}s)"
            )
            return ExitSignal.MAX_DURATION

        # 2. Fee target check
        if position.sol_deposited > 0:
            fee_pct = (
                position.fees_earned_usd
                / (position.sol_deposited * self._get_sol_price())
                * 100
            )
            if fee_pct >= self.cfg.fee_target_pct:
                logger.info(
                    f"[{position.pool_name}] Fee target hit! "
                    f"{fee_pct:.2f}% >= {self.cfg.fee_target_pct}%"
                )
                return ExitSignal.FEE_TARGET_HIT

        # 3. Volume decay check
        current_volume = await self._get_current_volume(position)
        if position.entry_volume_5m > 0 and current_volume is not None:
            volume_ratio = (current_volume / position.entry_volume_5m) * 100
            if volume_ratio < self.cfg.volume_decay_threshold_pct:
                logger.info(
                    f"[{position.pool_name}] Volume decay! "
                    f"Current={current_volume:.0f} vs Entry="
                    f"{position.entry_volume_5m:.0f} "
                    f"({volume_ratio:.1f}%)"
                )
                return ExitSignal.VOLUME_DECAY

        # 4. Price breakdown check
        current_price = await self._get_current_price(position)
        if current_price and position.entry_price > 0:
            price_change_pct = (
                (current_price - position.entry_price)
                / position.entry_price
                * 100
            )
            if price_change_pct <= -self.cfg.price_drop_exit_pct:
                logger.info(
                    f"[{position.pool_name}] Price breakdown! "
                    f"{price_change_pct:.1f}% drop"
                )
                return ExitSignal.PRICE_BREAKDOWN

        # 5. Out of range check
        active_bin = await self._get_active_bin(position)
        if active_bin is not None:
            in_range = (
                position.lower_bin_id <= active_bin <= position.upper_bin_id
            )
            if not in_range:
                if position.out_of_range_since is None:
                    position.out_of_range_since = now
                    logger.warning(
                        f"[{position.pool_name}] Position OUT OF RANGE "
                        f"(active_bin={active_bin}, "
                        f"range={position.lower_bin_id}..{position.upper_bin_id})"
                    )
                elif (now - position.out_of_range_since) >= self.cfg.out_of_range_max_seconds:
                    logger.info(
                        f"[{position.pool_name}] Out of range too long "
                        f"({now - position.out_of_range_since}s)"
                    )
                    return ExitSignal.OUT_OF_RANGE
            else:
                # Reset out-of-range timer if back in range
                if position.out_of_range_since is not None:
                    logger.info(f"[{position.pool_name}] Back in range!")
                    position.out_of_range_since = None

        return ExitSignal.NONE


    async def _get_current_volume(
        self, position: ActivePosition
    ) -> Optional[float]:
        """Get current 5min volume for the pool's token."""
        try:
            dex_data = await self.scanner.fetch_dexscreener_data(
                position.mint_x
            )
            if dex_data:
                vol = dex_data.get("volume", {})
                return float(vol.get("m5", 0) or 0)
        except Exception as e:
            logger.debug(f"Failed to get volume: {e}")
        return None

    async def _get_current_price(
        self, position: ActivePosition
    ) -> Optional[float]:
        """Get current price from pool."""
        try:
            pool_data = await self.scanner.fetch_pool_detail(
                position.pool_address
            )
            return float(pool_data.get("current_price", 0))
        except Exception as e:
            logger.debug(f"Failed to get price: {e}")
        return None

    async def _get_active_bin(
        self, position: ActivePosition
    ) -> Optional[int]:
        """Get current active bin of the pool."""
        try:
            pool_data = await self.scanner.fetch_pool_detail(
                position.pool_address
            )
            return int(pool_data.get("active_id", 0))
        except Exception as e:
            logger.debug(f"Failed to get active bin: {e}")
        return None

    def _get_sol_price(self) -> float:
        """
        Get current SOL price in USD.
        Cached/approximate for quick calculations.
        """
        # In production, fetch from Jupiter price API
        # For now, use a reasonable default
        return 170.0  # TODO: fetch dynamically

    async def update_fees(self, position: ActivePosition):
        """
        Update fee earnings for a position.
        In production, query on-chain position data for accrued fees.
        """
        # TODO: Query Meteora position account for fee_x, fee_y fields
        # For now, estimate from volume
        try:
            current_vol = await self._get_current_volume(position)
            if current_vol and current_vol > 0:
                # Rough estimate: our share of fees
                # fee = volume * fee_rate * (our_liq / total_liq)
                # This is very approximate
                pool_data = await self.scanner.fetch_pool_detail(
                    position.pool_address
                )
                total_liq = float(pool_data.get("liquidity", 1))
                our_liq = position.sol_deposited * self._get_sol_price()
                fee_rate = float(
                    pool_data.get("base_fee_percentage", "0") or "0"
                ) / 100
                
                our_share = our_liq / max(total_liq, 1)
                estimated_fee = current_vol * fee_rate * our_share
                position.fees_earned_usd += estimated_fee * (
                    CONFIG.monitor.monitor_interval_seconds / 300
                )  # scale to interval vs 5min
                position.fees_earned_sol = (
                    position.fees_earned_usd / self._get_sol_price()
                )
        except Exception as e:
            logger.debug(f"Fee estimation failed: {e}")

    async def monitor_loop(self):
        """Main monitoring loop - checks all positions periodically."""
        self._running = True
        logger.info("Position monitor started")

        while self._running:
            positions = self.pm.get_active_positions()
            
            for position in positions:
                try:
                    # Update fee estimates
                    await self.update_fees(position)

                    # Check for exit signals
                    signal = await self.check_position(position)

                    if signal != ExitSignal.NONE:
                        logger.info(
                            f"EXIT SIGNAL: {signal} for "
                            f"{position.pool_name}"
                        )
                        await self.pm.close_position(
                            position.position_pubkey, reason=signal
                        )

                except Exception as e:
                    logger.error(
                        f"Error monitoring {position.pool_name}: {e}"
                    )

            await asyncio.sleep(self.cfg.monitor_interval_seconds)

    def stop(self):
        """Stop the monitor loop."""
        self._running = False
        logger.info("Position monitor stopping")
