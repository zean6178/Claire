"""
DLMM Bot - Main Orchestrator (Master Playbook v1.0)
Integrates: Scanner, Scorer, Strategy Selector, Entry Checklist,
Position Manager, Exit Decision Tree, Risk Manager, Inventory, PnL.
"""

import asyncio
import logging
import signal
import sys
from typing import List, Optional

from .checklist import EntryChecklist, ExitDecision, ExitDecisionTree
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
from .risk import RiskManager
from .scanner import PoolCandidate, PoolScanner
from .scoring import PoolScore, PoolScorer
from .strategies import (
    ALL_STRATEGIES,
    StrategyPreset,
    StrategyType,
    select_strategy_for_pool,
)
from .telegram_bot import AlertType, TelegramBot
from .utils import now_ts, setup_logging

logger = logging.getLogger("dlmm_bot.bot")


class DLMMBot:
    """
    Main bot class — Master Playbook v1.0.

    Full flow:
    1. Scan pools (Scanner)
    2. Score each pool (Scorer, 70/100 threshold)
    3. Select strategy per pool (Strategy Selector)
    4. Run entry checklist (13 checks, ALL must pass)
    5. Calculate position size (Risk Manager / Kelly-lite)
    6. Open position (Position Manager)
    7. Monitor with exit decision tree (9 priority signals)
    8. Close position on exit signal
    9. Sweep meme tokens -> SOL (Inventory Manager)
    10. Log PnL, update risk state
    11. Repeat
    """

    def __init__(self):
        # Core modules
        self.scanner = PoolScanner()
        self.scorer = PoolScorer()
        self.entry_checklist = EntryChecklist()
        self.exit_tree = ExitDecisionTree()
        self.position_manager = PositionManager()
        self.monitor = PositionMonitor(self.position_manager, self.scanner)
        self.risk = RiskManager()
        self.inventory = InventoryManager()
        self.pnl = PnLTracker()
        self.telegram: Optional[TelegramBot] = None

        # State
        self._running = False
        self._tasks: List[asyncio.Task] = []

        # Track peak fee/min per position for decay detection
        self._peak_fee_per_min: dict = {}  # position_key -> peak fee/min

    async def initialize(self):
        """Initialize all components."""
        setup_logging()
        logger.info("=" * 60)
        logger.info("  CLAIRE — DLMM MEMECOIN FEE-FARMING BOT")
        logger.info("  Master Playbook v1.0")
        logger.info("=" * 60)
        logger.info(f"  Mode: {'DRY RUN' if CONFIG.dry_run else '*** LIVE ***'}")
        logger.info(f"  Max positions: {self.risk.cfg.max_open_positions}")
        logger.info(f"  Max exposure: {self.risk.cfg.max_total_exposure_sol} SOL")
        logger.info(f"  Kill switch: {self.risk.cfg.max_daily_loss_sol} SOL daily loss")
        logger.info(f"  Strategies: {len(ALL_STRATEGIES)} loaded")
        logger.info(f"  Score threshold: 70/100")
        logger.info(f"  Entry checks: 13")
        logger.info(f"  Exit signals: 9 (priority-ordered)")
        logger.info("=" * 60)

        await self.position_manager.initialize()
        await self.monitor.initialize()
        await self.inventory.initialize()

        # Initialize Telegram bot
        self.telegram = TelegramBot(bot_reference=self)
        if self.telegram.enabled:
            await self.telegram.send_bot_started()

    async def shutdown(self):
        """Graceful shutdown: close positions, sweep, report."""
        logger.info("Shutting down bot...")
        self._running = False
        self.monitor.stop()

        # Cancel background tasks
        for task in self._tasks:
            task.cancel()

        # Close all open positions
        active = self.position_manager.get_active_positions()
        if active:
            logger.info(f"Closing {len(active)} active positions on shutdown...")
            for pos in active:
                await self.position_manager.close_position(
                    pos.position_pubkey, reason="shutdown"
                )
                self.pnl.record_position_close(pos)
                self.risk.record_position_closed(
                    strategy_type=StrategyType(pos.shape.value)
                    if hasattr(pos, 'strategy_type')
                    else StrategyType.AFTER_WAR,
                    sol_returned=pos.sol_deposited,  # approximate
                    sol_deposited=pos.sol_deposited,
                    pool_address=pos.pool_address,
                    fees_earned_sol=pos.fees_earned_sol,
                )

        # Sweep inventory
        mints = list(set(
            pos.mint_x for pos in self.position_manager.positions.values()
        ))
        if mints and CONFIG.inventory.auto_swap_to_sol:
            logger.info("Sweeping remaining meme tokens to SOL...")
            await self.inventory.sweep_all_meme_tokens(mints)

        # Print summaries
        self.pnl.print_summary()
        logger.info(self.risk.get_risk_summary())

        # Notify Telegram
        if self.telegram and self.telegram.enabled:
            summary = self.pnl.print_summary()
            await self.telegram.send_bot_stopped(summary or "Session ended")
            self.telegram.stop()
            await self.telegram.close()

        # Cleanup
        await self.scanner.close()
        await self.position_manager.close()
        await self.monitor.close()
        await self.inventory.close()

        logger.info("Bot shutdown complete.")

    # =========================================================================
    # MAIN SCAN → SCORE → STRATEGY → CHECKLIST → ENTER LOOP
    # =========================================================================

    async def _scan_and_enter(self):
        """
        Main entry loop:
        Scan → Score → Select Strategy → Entry Checklist → Size → Open
        """
        while self._running:
            try:
                # 0. Check if trading is allowed (kill switch / cooldown)
                allowed, reason = self.risk.is_trading_allowed()
                if not allowed:
                    logger.info(f"Trading paused: {reason}")
                    await asyncio.sleep(30)
                    continue

                # 1. Scan for pool candidates
                candidates = await self.scanner.scan()
                if not candidates:
                    await asyncio.sleep(CONFIG.scanner.scan_interval_seconds)
                    continue

                # 2. Process each candidate through the full pipeline
                for candidate in candidates:
                    await self._process_candidate(candidate)

            except Exception as e:
                logger.error(f"Error in scan loop: {e}", exc_info=True)

            await asyncio.sleep(CONFIG.scanner.scan_interval_seconds)

    async def _process_candidate(self, candidate: PoolCandidate):
        """
        Full pipeline for a single candidate:
        Score → Strategy → Checklist → Size → Enter
        """
        # --- STEP 1: Score the pool ---
        score = self.scorer.score_pool(
            pool_address=candidate.address,
            pool_name=candidate.name,
            volume_5m_usd=candidate.volume_5m_usd,
            volume_1h_usd=candidate.volume_1h_usd,
            volume_prev_5m_usd=0.0,  # TODO: track previous volume
            txn_count_5m=0,  # TODO: get from DEXScreener
            price_change_1h_pct=0.0,  # TODO: compute from data
            price_change_5m_pct=0.0,
            is_downtrend=False,  # TODO: detect from price series
            has_support=True,  # TODO: technical analysis
            holder_count=candidate.holders,
            top5_holder_pct=40.0,  # TODO: get actual data
            top10_holder_pct=60.0,
            dev_wallet_pct=0.0,  # TODO: detect dev wallet
            liquidity_usd=candidate.liquidity_usd,
            fee_rate_bps=candidate.base_fee_bps,
            estimated_fee_per_min_usd=candidate.fee_rate_5m_usd / 5.0,
            pool_price=candidate.current_price,
            jupiter_price=candidate.current_price,  # TODO: fetch Jupiter price
            has_social_buzz=False,
            has_narrative=False,
            pool_age_seconds=candidate.pool_age_seconds,
            is_honeypot=False,
            has_wash_volume=False,
        )

        if not score.passed:
            return  # Score too low

        # --- TELEGRAM: Screening alert ---
        if self.telegram and self.telegram.enabled and CONFIG.telegram.alert_on_screening:
            await self.telegram.send_screening_alert(
                pool_name=candidate.name,
                pool_address=candidate.address,
                score=score.total_score,
                strategy_name="pending",
                volume_5m=candidate.volume_5m_usd,
                fee_rate_bps=candidate.base_fee_bps,
                holders=candidate.holders,
                pool_age_seconds=candidate.pool_age_seconds,
                liquidity_usd=candidate.liquidity_usd,
            )

        # --- STEP 2: Select strategy ---
        strategy = select_strategy_for_pool(
            pool_age_seconds=candidate.pool_age_seconds,
            volume_5m_usd=candidate.volume_5m_usd,
            volume_1h_usd=candidate.volume_1h_usd,
            holders=candidate.holders,
            fee_rate_bps=candidate.base_fee_bps,
            is_fresh_launch=(candidate.pool_age_seconds <= 900),
            is_post_pump=False,  # TODO: detect from price spike
            has_clear_range=(candidate.pool_age_seconds >= 1800),
            is_stable_pair=False,
            has_automation=False,
        )

        if strategy is None:
            return  # No strategy matched

        # --- STEP 3: Risk check ---
        sol_amount = self.risk.calculate_position_size(
            strategy_type=strategy.strategy_type,
            bankroll_sol=CONFIG.wallet.max_total_sol,
            pool_score=score.total_score,
            win_rate=self.risk.get_win_rate(),
        )

        can_open, risk_reason = self.risk.can_open_position(
            strategy_type=strategy.strategy_type,
            requested_sol=sol_amount,
            pool_address=candidate.address,
        )
        if not can_open:
            logger.debug(f"Risk blocked {candidate.name}: {risk_reason}")
            return

        # --- STEP 4: Entry checklist (13 checks) ---
        active_pools = {
            p.pool_address for p in self.position_manager.get_active_positions()
        }

        checklist_result = self.entry_checklist.run(
            pool_score=score,
            strategy=strategy,
            pool_price=candidate.current_price,
            jupiter_price=candidate.current_price,  # TODO: actual Jupiter price
            volume_5m_usd=candidate.volume_5m_usd,
            volume_prev_5m_usd=0.0,
            price_change_5m_pct=0.0,
            price_change_1h_pct=0.0,
            holder_count=candidate.holders,
            top5_holder_pct=40.0,  # TODO: actual data
            liquidity_usd=candidate.liquidity_usd,
            fee_rate_bps=candidate.base_fee_bps,
            pool_age_seconds=candidate.pool_age_seconds,
            bin_step=candidate.bin_step,
            already_in_pool=(candidate.address in active_pools),
            available_sol=self.risk.get_available_budget(),
            current_open_positions=len(self.position_manager.get_active_positions()),
            daily_loss_sol=abs(min(0, self.risk.state.daily_stats.net_pnl_sol)),
            max_daily_loss_sol=self.risk.cfg.max_daily_loss_sol,
        )

        if not checklist_result.passed:
            logger.debug(
                f"Checklist FAILED for {candidate.name}: "
                f"{checklist_result.fail_reasons[:2]}"
            )
            return

        # --- STEP 5: OPEN POSITION ---
        shape = PositionShape(strategy.shape)
        side = (
            PositionSide.SINGLE_SIDED_SOL
            if strategy.side == "single_sided_sol"
            else PositionSide.BOTH_SIDES
        )

        position = await self.position_manager.open_position(
            candidate=candidate,
            sol_amount=sol_amount,
            shape=shape,
            side=side,
            num_bins=strategy.num_bins,
        )

        if position:
            # Record in risk manager and PnL
            self.risk.record_position_opened(
                strategy_type=strategy.strategy_type,
                sol_amount=sol_amount,
            )
            self.pnl.record_position_open(position)
            self._peak_fee_per_min[position.position_pubkey] = 0.0

            logger.info(
                f"ENTERED: {candidate.name} | "
                f"Strategy={strategy.name} | "
                f"Score={score.total_score:.0f} | "
                f"Size={sol_amount:.3f} SOL | "
                f"Shape={strategy.shape} | Bins={strategy.num_bins}"
            )

            # --- TELEGRAM: Position opened alert ---
            if self.telegram and self.telegram.enabled and CONFIG.telegram.alert_on_open:
                await self.telegram.send_position_opened(
                    pool_name=candidate.name,
                    strategy=strategy.name,
                    sol_amount=sol_amount,
                    shape=strategy.shape,
                    num_bins=strategy.num_bins,
                    score=score.total_score,
                )

    # =========================================================================
    # ENHANCED MONITOR WITH EXIT DECISION TREE
    # =========================================================================

    async def _monitor_loop(self):
        """
        Enhanced monitor: uses ExitDecisionTree instead of simple thresholds.
        """
        while self._running:
            try:
                positions = self.position_manager.get_active_positions()

                for position in positions:
                    await self._check_position_exit(position)

            except Exception as e:
                logger.error(f"Error in monitor loop: {e}", exc_info=True)

            await asyncio.sleep(CONFIG.monitor.monitor_interval_seconds)

    async def _check_position_exit(self, position: ActivePosition):
        """Check a single position against the exit decision tree."""

        # Get current market data
        current_volume = 0.0
        current_price = position.entry_price
        current_fee_per_min = 0.0
        is_in_range = True
        active_bin = None

        try:
            dex_data = await self.scanner.fetch_dexscreener_data(position.mint_x)
            if dex_data:
                vol = dex_data.get("volume", {})
                current_volume = float(vol.get("m5", 0) or 0)
        except Exception:
            pass

        try:
            pool_data = await self.scanner.fetch_pool_detail(position.pool_address)
            if pool_data:
                current_price = float(pool_data.get("current_price", 0) or 0)
                active_bin = int(pool_data.get("active_id", 0) or 0)
                if active_bin:
                    is_in_range = (
                        position.lower_bin_id <= active_bin <= position.upper_bin_id
                    )
        except Exception:
            pass

        # Track peak fee/min
        if current_volume > 0:
            # Rough fee/min estimate
            fee_rate = position.entry_price * 0.01 if position.entry_price > 0 else 0
            current_fee_per_min = current_volume / 5.0 * fee_rate
            peak = self._peak_fee_per_min.get(position.position_pubkey, 0)
            if current_fee_per_min > peak:
                self._peak_fee_per_min[position.position_pubkey] = current_fee_per_min

        # Calculate fees earned as % of capital
        sol_price = 170.0  # TODO: dynamic
        capital_usd = position.sol_deposited * sol_price
        fees_earned_pct = (
            (position.fees_earned_usd / capital_usd * 100)
            if capital_usd > 0
            else 0.0
        )

        # Handle OOR tracking
        now = now_ts()
        if not is_in_range:
            if position.out_of_range_since is None:
                position.out_of_range_since = now
        else:
            position.out_of_range_since = None

        # Determine strategy (default to AFTER_WAR for existing positions)
        strategy = ALL_STRATEGIES.get(StrategyType.AFTER_WAR)

        # Run exit decision tree
        exit_result = self.exit_tree.evaluate(
            strategy=strategy,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            entry_volume_5m=position.entry_volume_5m,
            peak_fee_per_min=self._peak_fee_per_min.get(position.position_pubkey, 0),
            current_price=current_price,
            current_volume_5m=current_volume,
            current_fee_per_min=current_fee_per_min,
            fees_earned_pct=fees_earned_pct,
            inventory_meme_pct=0.0,  # TODO: track actual inventory composition
            is_in_range=is_in_range,
            out_of_range_since=position.out_of_range_since,
            daily_loss_sol=abs(min(0, self.risk.state.daily_stats.net_pnl_sol)),
            max_daily_loss_sol=self.risk.cfg.max_daily_loss_sol,
            large_holder_dumping=False,  # TODO: detect
            liquidity_removed=False,  # TODO: detect
        )

        # Act on decision
        if exit_result.decision != ExitDecision.HOLD:
            logger.info(
                f"EXIT: {position.pool_name} | "
                f"Signal={exit_result.decision.value} | "
                f"Urgency={exit_result.urgency}/10 | "
                f"{exit_result.reason}"
            )

            success = await self.position_manager.close_position(
                position.position_pubkey,
                reason=exit_result.decision.value,
            )

            if success:
                self.risk.record_position_closed(
                    strategy_type=StrategyType.AFTER_WAR,  # TODO: track per position
                    sol_returned=position.sol_deposited,  # approximate
                    sol_deposited=position.sol_deposited,
                    pool_address=position.pool_address,
                    fees_earned_sol=position.fees_earned_sol,
                )

                # --- TELEGRAM: Position closed alert ---
                if self.telegram and self.telegram.enabled and CONFIG.telegram.alert_on_close:
                    duration = now_ts() - position.entry_time
                    await self.telegram.send_position_closed(
                        pool_name=position.pool_name,
                        reason=exit_result.decision.value,
                        duration_seconds=duration,
                        fees_earned_sol=position.fees_earned_sol,
                        fees_earned_usd=position.fees_earned_usd,
                        net_pnl_sol=position.fees_earned_sol,  # approximate
                        net_pnl_usd=position.fees_earned_usd,
                    )

                # --- TELEGRAM: Risk alerts ---
                if self.telegram and self.telegram.enabled and CONFIG.telegram.alert_on_risk:
                    if exit_result.decision.value in ("kill_switch", "rug_detected"):
                        await self.telegram.send_risk_alert(
                            AlertType.KILL_SWITCH
                            if exit_result.decision.value == "kill_switch"
                            else AlertType.RUG_DETECTED,
                            exit_result.reason,
                        )

    # =========================================================================
    # POST-CLOSE HANDLER
    # =========================================================================

    async def _position_close_handler(self):
        """Handle post-close actions: PnL recording, inventory sweep."""
        seen_closed = set()

        while self._running:
            try:
                for key, pos in self.position_manager.positions.items():
                    if pos.is_closed and key not in seen_closed:
                        seen_closed.add(key)

                        # Record PnL
                        self.pnl.record_position_close(pos)

                        # Swap meme tokens back to SOL
                        if CONFIG.inventory.auto_swap_to_sol:
                            await self.inventory.swap_token_to_sol(pos.mint_x)

                        # Clean up peak tracking
                        self._peak_fee_per_min.pop(key, None)

            except Exception as e:
                logger.error(f"Error in close handler: {e}")

            await asyncio.sleep(2)

    # =========================================================================
    # PERIODIC RISK REPORT
    # =========================================================================

    async def _risk_report_loop(self):
        """Print risk summary every 5 minutes."""
        while self._running:
            await asyncio.sleep(300)  # every 5 min
            if self._running:
                logger.info(f"\n{self.risk.get_risk_summary()}")

    # =========================================================================
    # MAIN RUN
    # =========================================================================

    async def run(self):
        """Main bot entry point."""
        await self.initialize()
        self._running = True

        # Start background tasks
        scan_task = asyncio.create_task(self._scan_and_enter())
        monitor_task = asyncio.create_task(self._monitor_loop())
        close_task = asyncio.create_task(self._position_close_handler())
        risk_task = asyncio.create_task(self._risk_report_loop())
        self._tasks = [scan_task, monitor_task, close_task, risk_task]

        # Start Telegram polling if enabled
        if self.telegram and self.telegram.enabled:
            tg_task = asyncio.create_task(self.telegram.polling_loop())
            self._tasks.append(tg_task)

        logger.info("Bot is RUNNING. Press Ctrl+C to stop.")

        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()


def main():
    """CLI entry point."""
    bot = DLMMBot()

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
