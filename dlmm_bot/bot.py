"""
DLMM Bot - Main Orchestrator
Coordinates scanning, position opening, monitoring, and inventory management.
"""

import asyncio
import logging
import signal
import sys
from typing import List

from .config import CONFIG
from .inventory import InventoryManager
from .monitor import ExitSignal, PositionMonitor
from .pnl import PnLTracker
from .position_manager import (
    ActivePosition,
    PositionManager,
    PositionShape,
    PositionSide,
)
from .scanner import PoolCandidate, PoolScanner
from .utils import now_ts, setup_logging

logger = logging.getLogger("dlmm_bot.bot")



class DLMMBot:
    """
    Main bot class.
    
    Flow:
    1. Scan for pool candidates
    2. Filter and rank by score
    3. Open positions on top candidates (single-sided SOL, Bid-Ask)
    4. Monitor positions for exit signals
    5. Close positions when triggered
    6. Swap remaining tokens back to SOL
    7. Log PnL
    8. Repeat
    """

    def __init__(self):
        self.scanner = PoolScanner()
        self.position_manager = PositionManager()
        self.monitor = PositionMonitor(self.position_manager, self.scanner)
        self.inventory = InventoryManager()
        self.pnl = PnLTracker()
        self._running = False
        self._tasks: List[asyncio.Task] = []

    async def initialize(self):
        """Initialize all components."""
        setup_logging()
        logger.info("=" * 50)
        logger.info("  DLMM MEMECOIN FEE-FARMING BOT")
        logger.info("=" * 50)
        logger.info(f"  Mode: {'DRY RUN' if CONFIG.dry_run else 'LIVE'}")
        logger.info(f"  Max positions: {CONFIG.position.max_open_positions}")
        logger.info(f"  Max SOL/position: {CONFIG.wallet.max_per_position_sol}")
        logger.info(f"  Max total SOL: {CONFIG.wallet.max_total_sol}")
        logger.info(f"  Shape: {CONFIG.position.default_shape}")
        logger.info(f"  Bins: {CONFIG.position.default_num_bins}")
        logger.info(f"  Fee target: {CONFIG.monitor.fee_target_pct}%")
        logger.info(f"  Max duration: {CONFIG.monitor.max_position_duration_seconds}s")
        logger.info("=" * 50)

        await self.position_manager.initialize()
        await self.monitor.initialize()
        await self.inventory.initialize()

    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("Shutting down bot...")
        self._running = False
        self.monitor.stop()

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()

        # Close all open positions
        active = self.position_manager.get_active_positions()
        if active:
            logger.info(f"Closing {len(active)} active positions...")
            for pos in active:
                await self.position_manager.close_position(
                    pos.position_pubkey, reason="shutdown"
                )
                self.pnl.record_position_close(pos)

        # Sweep inventory
        mints = list(set(
            pos.mint_x for pos in self.position_manager.positions.values()
        ))
        if mints and CONFIG.inventory.auto_swap_to_sol:
            logger.info("Sweeping remaining tokens to SOL...")
            await self.inventory.sweep_all_meme_tokens(mints)

        # Print summary
        self.pnl.print_summary()

        # Cleanup
        await self.scanner.close()
        await self.position_manager.close()
        await self.monitor.close()
        await self.inventory.close()

        logger.info("Bot shutdown complete.")


    async def _scan_and_enter(self):
        """Scan for pools and enter positions."""
        while self._running:
            try:
                # Check if we can open more positions
                active_count = len(
                    self.position_manager.get_active_positions()
                )
                if active_count >= CONFIG.position.max_open_positions:
                    logger.debug(
                        f"Max positions ({active_count}) reached, "
                        f"waiting..."
                    )
                    await asyncio.sleep(CONFIG.scanner.scan_interval_seconds)
                    continue

                # Check SOL budget
                deployed = self.position_manager.get_total_deployed_sol()
                remaining_budget = (
                    CONFIG.wallet.max_total_sol - deployed
                )
                if remaining_budget <= CONFIG.wallet.reserve_sol:
                    logger.debug("SOL budget exhausted, waiting...")
                    await asyncio.sleep(CONFIG.scanner.scan_interval_seconds)
                    continue

                # Scan for candidates
                candidates = await self.scanner.scan()

                if not candidates:
                    logger.info("No viable candidates found this scan")
                    await asyncio.sleep(CONFIG.scanner.scan_interval_seconds)
                    continue

                # Enter top candidates (up to available slots)
                slots_available = (
                    CONFIG.position.max_open_positions - active_count
                )
                top_candidates = candidates[:slots_available]

                for candidate in top_candidates:
                    # Skip if we already have a position in this pool
                    active_pools = {
                        p.pool_address
                        for p in self.position_manager.get_active_positions()
                    }
                    if candidate.address in active_pools:
                        continue

                    # Calculate position size
                    sol_amount = min(
                        CONFIG.wallet.max_per_position_sol,
                        remaining_budget / slots_available,
                    )

                    # Open position
                    position = await self.position_manager.open_position(
                        candidate=candidate,
                        sol_amount=sol_amount,
                    )

                    if position:
                        self.pnl.record_position_open(position)
                        remaining_budget -= sol_amount

            except Exception as e:
                logger.error(f"Error in scan loop: {e}", exc_info=True)

            await asyncio.sleep(CONFIG.scanner.scan_interval_seconds)


    async def _position_close_handler(self):
        """
        Handle post-close actions: inventory sweep, PnL recording.
        Runs alongside monitor.
        """
        seen_closed = set()

        while self._running:
            try:
                for key, pos in self.position_manager.positions.items():
                    if pos.is_closed and key not in seen_closed:
                        seen_closed.add(key)

                        # Record PnL
                        self.pnl.record_position_close(pos)

                        # Swap tokens back to SOL
                        if CONFIG.inventory.auto_swap_to_sol:
                            await self.inventory.swap_token_to_sol(pos.mint_x)

            except Exception as e:
                logger.error(f"Error in close handler: {e}")

            await asyncio.sleep(2)

    async def run(self):
        """Main bot entry point."""
        await self.initialize()
        self._running = True

        # Start background tasks
        scan_task = asyncio.create_task(self._scan_and_enter())
        monitor_task = asyncio.create_task(self.monitor.monitor_loop())
        close_task = asyncio.create_task(self._position_close_handler())
        self._tasks = [scan_task, monitor_task, close_task]

        logger.info("Bot is RUNNING. Press Ctrl+C to stop.")

        try:
            # Wait for all tasks (they run forever until stopped)
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()


def main():
    """CLI entry point."""
    bot = DLMMBot()

    # Handle graceful shutdown
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, shutting down...")
        bot._running = False
        bot.monitor.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        loop.run_until_complete(bot.shutdown())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
