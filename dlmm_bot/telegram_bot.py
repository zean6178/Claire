"""
Telegram Bot Integration for Claire DLMM Bot
Provides:
- Inline keyboard UI buttons for control
- Screening alerts when tokens pass (score >= 70)
- Position open/close notifications with PnL
- Risk alerts (kill switch, cooldown, OOR, volume decay)
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from .config import CONFIG

logger = logging.getLogger("dlmm_bot.telegram")


# Telegram API base URL
TG_API = "https://api.telegram.org/bot{token}/{method}"



# =============================================================================
# ALERT TYPES
# =============================================================================

class AlertType(Enum):
    """Types of Telegram alerts."""
    SCREENING_PASS = "screening_pass"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    FEE_TARGET_HIT = "fee_target_hit"
    VOLUME_DECAY = "volume_decay"
    PRICE_CRASH = "price_crash"
    OUT_OF_RANGE = "out_of_range"
    KILL_SWITCH = "kill_switch"
    COOLDOWN = "cooldown"
    RUG_DETECTED = "rug_detected"
    BOT_STARTED = "bot_started"
    BOT_STOPPED = "bot_stopped"
    DAILY_SUMMARY = "daily_summary"


# =============================================================================
# INLINE KEYBOARD BUTTONS
# =============================================================================

# Callback data prefixes for button actions
CB_STATUS = "cb_status"
CB_POSITIONS = "cb_positions"
CB_PNL = "cb_pnl"
CB_KILL_SWITCH = "cb_kill_switch"
CB_RESUME = "cb_resume"
CB_SCAN_NOW = "cb_scan_now"
CB_CLOSE_ALL = "cb_close_all"
CB_REFRESH = "cb_refresh"



def _build_main_keyboard() -> List[List[Dict[str, str]]]:
    """Build the main control panel inline keyboard."""
    return [
        [
            {"text": "📊 Status", "callback_data": CB_STATUS},
            {"text": "💰 Positions", "callback_data": CB_POSITIONS},
        ],
        [
            {"text": "📈 PnL", "callback_data": CB_PNL},
            {"text": "🔍 Scan Now", "callback_data": CB_SCAN_NOW},
        ],
        [
            {"text": "🛑 Kill Switch", "callback_data": CB_KILL_SWITCH},
            {"text": "▶️ Resume", "callback_data": CB_RESUME},
        ],
        [
            {"text": "❌ Close All", "callback_data": CB_CLOSE_ALL},
            {"text": "🔄 Refresh", "callback_data": CB_REFRESH},
        ],
    ]


def _build_screening_keyboard(pool_address: str) -> List[List[Dict[str, str]]]:
    """Build keyboard for screening alert (enter/skip)."""
    return [
        [
            {"text": "✅ Enter Position", "callback_data": f"enter_{pool_address[:16]}"},
            {"text": "⏭️ Skip", "callback_data": f"skip_{pool_address[:16]}"},
        ],
        [
            {"text": "🔍 Details", "callback_data": f"detail_{pool_address[:16]}"},
        ],
    ]


def _build_position_keyboard(position_key: str) -> List[List[Dict[str, str]]]:
    """Build keyboard for active position (close/claim)."""
    short_key = position_key[:16]
    return [
        [
            {"text": "💸 Claim Fees", "callback_data": f"claim_{short_key}"},
            {"text": "🚪 Close", "callback_data": f"close_{short_key}"},
        ],
    ]



# =============================================================================
# TELEGRAM CLIENT
# =============================================================================

class TelegramClient:
    """Low-level Telegram Bot API client using httpx."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self._http = None

    async def _get_http(self):
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=30)
        return self._http

    async def close(self):
        if self._http:
            await self._http.aclose()
            self._http = None

    def _url(self, method: str) -> str:
        return TG_API.format(token=self.token, method=method)

    async def send_message(
        self,
        text: str,
        reply_markup: Optional[Dict] = None,
        parse_mode: str = "HTML",
        disable_preview: bool = True,
    ) -> Optional[Dict]:
        """Send a text message with optional inline keyboard."""
        http = await self._get_http()
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = await http.post(self._url("sendMessage"), json=payload)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.warning(f"TG send failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"TG send error: {e}")
        return None


    async def edit_message(
        self,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict] = None,
        parse_mode: str = "HTML",
    ) -> Optional[Dict]:
        """Edit an existing message (for button refresh)."""
        http = await self._get_http()
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = await http.post(self._url("editMessageText"), json=payload)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.error(f"TG edit error: {e}")
        return None

    async def answer_callback(
        self, callback_query_id: str, text: str = ""
    ):
        """Answer a callback query (dismiss loading on button press)."""
        http = await self._get_http()
        payload = {
            "callback_query_id": callback_query_id,
            "text": text,
        }
        try:
            await http.post(self._url("answerCallbackQuery"), json=payload)
        except Exception as e:
            logger.debug(f"TG answer_callback error: {e}")

    async def get_updates(self, offset: int = 0, timeout: int = 30) -> List[Dict]:
        """Long-poll for updates (button presses, commands)."""
        http = await self._get_http()
        payload = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        try:
            resp = await http.post(
                self._url("getUpdates"), json=payload, timeout=timeout + 5
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("result", [])
        except Exception as e:
            logger.debug(f"TG get_updates error: {e}")
        return []



# =============================================================================
# ALERT FORMATTER
# =============================================================================

class AlertFormatter:
    """Formats bot events into readable Telegram messages."""

    @staticmethod
    def screening_alert(
        pool_name: str,
        pool_address: str,
        score: float,
        strategy_name: str,
        volume_5m: float,
        fee_rate_bps: int,
        holders: int,
        pool_age_seconds: int,
        liquidity_usd: float,
    ) -> str:
        """Format a screening pass alert."""
        age_min = pool_age_seconds // 60
        return (
            f"🎯 <b>TOKEN PASSED SCREENING</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>{pool_name}</b>\n"
            f"Score: <b>{score:.0f}/100</b> ✅\n"
            f"Strategy: <code>{strategy_name}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Vol 5m: <b>${volume_5m:,.0f}</b>\n"
            f"💰 Fee: <b>{fee_rate_bps/100:.1f}%</b>\n"
            f"👥 Holders: <b>{holders}</b>\n"
            f"⏱️ Age: <b>{age_min}m</b>\n"
            f"🏊 Liquidity: <b>${liquidity_usd:,.0f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{pool_address}</code>"
        )

    @staticmethod
    def position_opened(
        pool_name: str,
        strategy: str,
        sol_amount: float,
        shape: str,
        num_bins: int,
        score: float,
    ) -> str:
        """Format position opened notification."""
        return (
            f"🟢 <b>POSITION OPENED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Pool: <b>{pool_name}</b>\n"
            f"Strategy: <code>{strategy}</code>\n"
            f"Size: <b>{sol_amount:.3f} SOL</b>\n"
            f"Shape: {shape} | Bins: {num_bins}\n"
            f"Score: {score:.0f}/100"
        )


    @staticmethod
    def position_closed(
        pool_name: str,
        reason: str,
        duration_seconds: int,
        fees_earned_sol: float,
        fees_earned_usd: float,
        net_pnl_sol: float,
        net_pnl_usd: float,
    ) -> str:
        """Format position closed notification."""
        duration_min = duration_seconds // 60
        pnl_emoji = "🟢" if net_pnl_sol >= 0 else "🔴"
        reason_emoji = {
            "fee_target": "🎯",
            "volume_decay": "📉",
            "price_crash": "💥",
            "max_duration": "⏰",
            "out_of_range": "↔️",
            "kill_switch": "🛑",
            "rug_detected": "🚨",
            "shutdown": "⚡",
            "manual": "👤",
        }.get(reason, "❓")

        return (
            f"{pnl_emoji} <b>POSITION CLOSED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Pool: <b>{pool_name}</b>\n"
            f"Reason: {reason_emoji} <code>{reason}</code>\n"
            f"Duration: {duration_min}m {duration_seconds % 60}s\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Fees: <b>{fees_earned_sol:.4f} SOL</b> (${fees_earned_usd:.2f})\n"
            f"Net PnL: <b>{net_pnl_sol:+.4f} SOL</b> (${net_pnl_usd:+.2f})\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )

    @staticmethod
    def risk_alert(
        alert_type: AlertType,
        details: str,
    ) -> str:
        """Format risk/warning alerts."""
        emoji_map = {
            AlertType.KILL_SWITCH: "🛑",
            AlertType.COOLDOWN: "⏸️",
            AlertType.OUT_OF_RANGE: "↔️",
            AlertType.VOLUME_DECAY: "📉",
            AlertType.PRICE_CRASH: "💥",
            AlertType.RUG_DETECTED: "🚨",
        }
        emoji = emoji_map.get(alert_type, "⚠️")
        title = alert_type.value.replace("_", " ").upper()

        return (
            f"{emoji} <b>ALERT: {title}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{details}"
        )


    @staticmethod
    def status_message(
        is_running: bool,
        mode: str,
        active_positions: int,
        total_exposure_sol: float,
        daily_pnl_sol: float,
        daily_pnl_usd: float,
        wins: int,
        losses: int,
        kill_switch: bool,
        cooldown: bool,
    ) -> str:
        """Format bot status message."""
        status = "🟢 RUNNING" if is_running else "🔴 STOPPED"
        mode_str = "🧪 DRY RUN" if mode == "dry" else "🔴 LIVE"
        ks = "🛑 ACTIVE" if kill_switch else "✅ Off"
        cd = "⏸️ YES" if cooldown else "✅ No"
        total = wins + losses
        wr = f"{wins/total*100:.0f}%" if total > 0 else "N/A"

        return (
            f"<b>CLAIRE STATUS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Status: {status}\n"
            f"Mode: {mode_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Positions: <b>{active_positions}</b>\n"
            f"💰 Exposure: <b>{total_exposure_sol:.2f} SOL</b>\n"
            f"📈 Daily PnL: <b>{daily_pnl_sol:+.4f} SOL</b> "
            f"(${daily_pnl_usd:+.2f})\n"
            f"🏆 W/L: {wins}/{losses} ({wr})\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Kill Switch: {ks}\n"
            f"Cooldown: {cd}"
        )

    @staticmethod
    def positions_list(positions: List[Dict[str, Any]]) -> str:
        """Format active positions list."""
        if not positions:
            return "📭 <b>No active positions</b>"

        lines = ["<b>💰 ACTIVE POSITIONS</b>\n━━━━━━━━━━━━━━━━━━━━"]
        for i, p in enumerate(positions, 1):
            duration = int(time.time()) - p.get("entry_time", 0)
            dur_min = duration // 60
            lines.append(
                f"\n{i}. <b>{p.get('pool_name', '?')}</b>\n"
                f"   Size: {p.get('sol_deposited', 0):.3f} SOL\n"
                f"   Fees: ${p.get('fees_earned_usd', 0):.2f}\n"
                f"   Duration: {dur_min}m\n"
                f"   Range: {'✅ In' if p.get('in_range', True) else '⚠️ OUT'}"
            )
        return "\n".join(lines)

    @staticmethod
    def pnl_summary(
        session_pnl_sol: float,
        session_pnl_usd: float,
        total_fees_sol: float,
        total_fees_usd: float,
        total_costs_sol: float,
        positions_closed: int,
        wins: int,
        losses: int,
        best_trade_usd: float,
        worst_trade_usd: float,
    ) -> str:
        """Format PnL summary."""
        total = wins + losses
        wr = f"{wins/total*100:.0f}%" if total > 0 else "N/A"

        return (
            f"<b>📈 SESSION PnL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Net: <b>{session_pnl_sol:+.4f} SOL</b> (${session_pnl_usd:+.2f})\n"
            f"Fees: {total_fees_sol:.4f} SOL (${total_fees_usd:.2f})\n"
            f"Costs: -{total_costs_sol:.4f} SOL\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Closed: {positions_closed}\n"
            f"W/L: {wins}/{losses} ({wr})\n"
            f"Best: ${best_trade_usd:+.2f}\n"
            f"Worst: ${worst_trade_usd:+.2f}"
        )



# =============================================================================
# TELEGRAM BOT (main class)
# =============================================================================

class TelegramBot:
    """
    Main Telegram bot integration.
    
    Provides:
    - Inline keyboard control panel (Status, Positions, PnL, Kill Switch, etc.)
    - Screening alerts when pools pass scoring
    - Position open/close notifications
    - Risk alerts
    - Long-polling for button callbacks
    """

    def __init__(self, bot_reference=None):
        """
        Args:
            bot_reference: Reference to the main DLMMBot instance
                           for executing commands from buttons.
        """
        self.enabled = bool(CONFIG.telegram.bot_token and CONFIG.telegram.chat_id)
        self.client: Optional[TelegramClient] = None
        self.formatter = AlertFormatter()
        self.bot_ref = bot_reference  # DLMMBot instance
        self._running = False
        self._update_offset = 0

        if self.enabled:
            self.client = TelegramClient(
                token=CONFIG.telegram.bot_token,
                chat_id=CONFIG.telegram.chat_id,
            )
            logger.info("Telegram bot initialized")
        else:
            logger.info("Telegram bot DISABLED (no token/chat_id configured)")

    async def close(self):
        """Cleanup."""
        if self.client:
            await self.client.close()

    # =========================================================================
    # SEND ALERTS
    # =========================================================================

    async def send_screening_alert(
        self,
        pool_name: str,
        pool_address: str,
        score: float,
        strategy_name: str,
        volume_5m: float,
        fee_rate_bps: int,
        holders: int,
        pool_age_seconds: int,
        liquidity_usd: float,
    ):
        """Send alert when a token passes screening (score >= 70)."""
        if not self.enabled:
            return

        text = self.formatter.screening_alert(
            pool_name=pool_name,
            pool_address=pool_address,
            score=score,
            strategy_name=strategy_name,
            volume_5m=volume_5m,
            fee_rate_bps=fee_rate_bps,
            holders=holders,
            pool_age_seconds=pool_age_seconds,
            liquidity_usd=liquidity_usd,
        )

        keyboard = _build_screening_keyboard(pool_address)
        await self.client.send_message(
            text=text,
            reply_markup={"inline_keyboard": keyboard},
        )

    async def send_position_opened(
        self,
        pool_name: str,
        strategy: str,
        sol_amount: float,
        shape: str,
        num_bins: int,
        score: float,
    ):
        """Send notification when position is opened."""
        if not self.enabled:
            return

        text = self.formatter.position_opened(
            pool_name=pool_name,
            strategy=strategy,
            sol_amount=sol_amount,
            shape=shape,
            num_bins=num_bins,
            score=score,
        )
        await self.client.send_message(text=text)


    async def send_position_closed(
        self,
        pool_name: str,
        reason: str,
        duration_seconds: int,
        fees_earned_sol: float,
        fees_earned_usd: float,
        net_pnl_sol: float,
        net_pnl_usd: float,
    ):
        """Send notification when position is closed."""
        if not self.enabled:
            return

        text = self.formatter.position_closed(
            pool_name=pool_name,
            reason=reason,
            duration_seconds=duration_seconds,
            fees_earned_sol=fees_earned_sol,
            fees_earned_usd=fees_earned_usd,
            net_pnl_sol=net_pnl_sol,
            net_pnl_usd=net_pnl_usd,
        )
        await self.client.send_message(text=text)

    async def send_risk_alert(self, alert_type: AlertType, details: str):
        """Send risk/warning alert."""
        if not self.enabled:
            return

        text = self.formatter.risk_alert(alert_type, details)
        await self.client.send_message(text=text)

    async def send_bot_started(self):
        """Send bot started notification with control panel."""
        if not self.enabled:
            return

        mode = "DRY RUN" if CONFIG.dry_run else "LIVE"
        text = (
            f"🚀 <b>CLAIRE STARTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Mode: <b>{mode}</b>\n"
            f"Max positions: {CONFIG.position.max_open_positions}\n"
            f"Max exposure: {CONFIG.wallet.max_total_sol} SOL\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Use buttons below to control the bot:"
        )
        keyboard = _build_main_keyboard()
        await self.client.send_message(
            text=text,
            reply_markup={"inline_keyboard": keyboard},
        )

    async def send_bot_stopped(self, pnl_summary: str):
        """Send bot stopped notification."""
        if not self.enabled:
            return

        text = f"⚡ <b>CLAIRE STOPPED</b>\n\n{pnl_summary}"
        await self.client.send_message(text=text)

    # =========================================================================
    # BUTTON CALLBACK HANDLER
    # =========================================================================

    async def _handle_callback(self, callback_query: Dict):
        """Handle button press callback."""
        cb_id = callback_query.get("id", "")
        data = callback_query.get("data", "")
        message = callback_query.get("message", {})
        msg_id = message.get("message_id", 0)

        logger.info(f"TG callback: {data}")

        # Acknowledge the button press immediately
        await self.client.answer_callback(cb_id, text="Processing...")

        # Route to handler
        if data == CB_STATUS:
            await self._cmd_status(msg_id)
        elif data == CB_POSITIONS:
            await self._cmd_positions(msg_id)
        elif data == CB_PNL:
            await self._cmd_pnl(msg_id)
        elif data == CB_KILL_SWITCH:
            await self._cmd_kill_switch(msg_id)
        elif data == CB_RESUME:
            await self._cmd_resume(msg_id)
        elif data == CB_SCAN_NOW:
            await self._cmd_scan_now(msg_id)
        elif data == CB_CLOSE_ALL:
            await self._cmd_close_all(msg_id)
        elif data == CB_REFRESH:
            await self._cmd_status(msg_id)
        elif data.startswith("close_"):
            await self._cmd_close_position(data, msg_id)
        elif data.startswith("enter_"):
            await self.client.answer_callback(cb_id, "Manual entry not yet supported")
        elif data.startswith("skip_"):
            await self.client.answer_callback(cb_id, "Skipped ✅")


    async def _cmd_status(self, msg_id: int):
        """Handle Status button."""
        if not self.bot_ref:
            return

        risk = self.bot_ref.risk
        text = self.formatter.status_message(
            is_running=self.bot_ref._running,
            mode="dry" if CONFIG.dry_run else "live",
            active_positions=len(self.bot_ref.position_manager.get_active_positions()),
            total_exposure_sol=risk.state.current_exposure_sol,
            daily_pnl_sol=risk.state.daily_stats.net_pnl_sol,
            daily_pnl_usd=risk.state.daily_stats.net_pnl_sol * 170,  # approx
            wins=risk.state.daily_stats.wins,
            losses=risk.state.daily_stats.losses,
            kill_switch=risk.state.kill_switch_active,
            cooldown=risk.state.in_cooldown,
        )
        keyboard = _build_main_keyboard()
        await self.client.edit_message(
            msg_id, text, reply_markup={"inline_keyboard": keyboard}
        )

    async def _cmd_positions(self, msg_id: int):
        """Handle Positions button."""
        if not self.bot_ref:
            return

        positions = self.bot_ref.position_manager.get_active_positions()
        pos_data = []
        for p in positions:
            pos_data.append({
                "pool_name": p.pool_name,
                "sol_deposited": p.sol_deposited,
                "fees_earned_usd": p.fees_earned_usd,
                "entry_time": p.entry_time,
                "in_range": p.out_of_range_since is None,
            })

        text = self.formatter.positions_list(pos_data)
        keyboard = _build_main_keyboard()
        await self.client.edit_message(
            msg_id, text, reply_markup={"inline_keyboard": keyboard}
        )

    async def _cmd_pnl(self, msg_id: int):
        """Handle PnL button."""
        if not self.bot_ref:
            return

        pnl = self.bot_ref.pnl.session
        risk = self.bot_ref.risk.state.daily_stats

        text = self.formatter.pnl_summary(
            session_pnl_sol=pnl.total_net_pnl_sol,
            session_pnl_usd=pnl.total_net_pnl_usd,
            total_fees_sol=pnl.total_fees_earned_sol,
            total_fees_usd=pnl.total_fees_earned_usd,
            total_costs_sol=pnl.total_costs_sol,
            positions_closed=pnl.total_positions_closed,
            wins=pnl.win_count,
            losses=pnl.loss_count,
            best_trade_usd=pnl.best_trade_usd,
            worst_trade_usd=pnl.worst_trade_usd,
        )
        keyboard = _build_main_keyboard()
        await self.client.edit_message(
            msg_id, text, reply_markup={"inline_keyboard": keyboard}
        )

    async def _cmd_kill_switch(self, msg_id: int):
        """Handle Kill Switch button."""
        if not self.bot_ref:
            return

        self.bot_ref.risk._trigger_kill_switch("Manual via Telegram")
        text = (
            "🛑 <b>KILL SWITCH ACTIVATED</b>\n\n"
            "All trading halted. No new positions will be opened.\n"
            "Active positions remain open (close manually or wait for exit signals).\n\n"
            "Press ▶️ Resume to re-enable trading."
        )
        keyboard = _build_main_keyboard()
        await self.client.edit_message(
            msg_id, text, reply_markup={"inline_keyboard": keyboard}
        )


    async def _cmd_resume(self, msg_id: int):
        """Handle Resume button."""
        if not self.bot_ref:
            return

        self.bot_ref.risk.reset_kill_switch()
        text = (
            "▶️ <b>TRADING RESUMED</b>\n\n"
            "Kill switch deactivated. Bot will resume scanning and entering positions."
        )
        keyboard = _build_main_keyboard()
        await self.client.edit_message(
            msg_id, text, reply_markup={"inline_keyboard": keyboard}
        )

    async def _cmd_scan_now(self, msg_id: int):
        """Handle Scan Now button — trigger immediate scan."""
        if not self.bot_ref:
            return

        text = "🔍 <b>Scanning now...</b>\nResults will appear as screening alerts."
        keyboard = _build_main_keyboard()
        await self.client.edit_message(
            msg_id, text, reply_markup={"inline_keyboard": keyboard}
        )
        # Trigger scan in background (non-blocking)
        asyncio.create_task(self._do_manual_scan())

    async def _do_manual_scan(self):
        """Execute a manual scan and send results."""
        try:
            candidates = await self.bot_ref.scanner.scan()
            if candidates:
                await self.client.send_message(
                    f"🔍 Found {len(candidates)} candidates from manual scan"
                )
            else:
                await self.client.send_message("🔍 No candidates found")
        except Exception as e:
            await self.client.send_message(f"❌ Scan error: {e}")

    async def _cmd_close_all(self, msg_id: int):
        """Handle Close All button."""
        if not self.bot_ref:
            return

        active = self.bot_ref.position_manager.get_active_positions()
        if not active:
            text = "📭 No active positions to close."
        else:
            count = len(active)
            for pos in active:
                await self.bot_ref.position_manager.close_position(
                    pos.position_pubkey, reason="manual_telegram"
                )
            text = f"❌ <b>Closed {count} positions</b> (manual via Telegram)"

        keyboard = _build_main_keyboard()
        await self.client.edit_message(
            msg_id, text, reply_markup={"inline_keyboard": keyboard}
        )

    async def _cmd_close_position(self, data: str, msg_id: int):
        """Handle individual position close."""
        if not self.bot_ref:
            return
        pos_key_prefix = data.replace("close_", "")
        # Find position by prefix match
        for key, pos in self.bot_ref.position_manager.positions.items():
            if key.startswith(pos_key_prefix) and not pos.is_closed:
                await self.bot_ref.position_manager.close_position(
                    key, reason="manual_telegram"
                )
                await self.client.send_message(
                    f"🚪 Closed position: {pos.pool_name}"
                )
                return
        await self.client.send_message("❓ Position not found")

    # =========================================================================
    # COMMAND HANDLER (text commands)
    # =========================================================================

    async def _handle_message(self, message: Dict):
        """Handle incoming text commands."""
        text = message.get("text", "").strip().lower()

        if text in ("/start", "/menu", "/panel"):
            await self.send_bot_started()
        elif text == "/status":
            # Send fresh status with keyboard
            await self._send_fresh_status()
        elif text == "/kill":
            if self.bot_ref:
                self.bot_ref.risk._trigger_kill_switch("Manual /kill command")
            await self.client.send_message("🛑 Kill switch activated")
        elif text == "/resume":
            if self.bot_ref:
                self.bot_ref.risk.reset_kill_switch()
            await self.client.send_message("▶️ Trading resumed")
        elif text == "/help":
            await self._send_help()

    async def _send_fresh_status(self):
        """Send a new status message with full keyboard."""
        if not self.bot_ref:
            return
        risk = self.bot_ref.risk
        text = self.formatter.status_message(
            is_running=self.bot_ref._running,
            mode="dry" if CONFIG.dry_run else "live",
            active_positions=len(self.bot_ref.position_manager.get_active_positions()),
            total_exposure_sol=risk.state.current_exposure_sol,
            daily_pnl_sol=risk.state.daily_stats.net_pnl_sol,
            daily_pnl_usd=risk.state.daily_stats.net_pnl_sol * 170,
            wins=risk.state.daily_stats.wins,
            losses=risk.state.daily_stats.losses,
            kill_switch=risk.state.kill_switch_active,
            cooldown=risk.state.in_cooldown,
        )
        keyboard = _build_main_keyboard()
        await self.client.send_message(
            text=text, reply_markup={"inline_keyboard": keyboard}
        )

    async def _send_help(self):
        """Send help message."""
        text = (
            "<b>Claire Bot Commands</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/start — Show control panel\n"
            "/status — Bot status\n"
            "/kill — Activate kill switch\n"
            "/resume — Deactivate kill switch\n"
            "/help — This message\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Or use the inline buttons below any panel message."
        )
        await self.client.send_message(text=text)


    # =========================================================================
    # POLLING LOOP
    # =========================================================================

    async def polling_loop(self):
        """
        Long-polling loop for receiving button presses and commands.
        Runs as background task alongside the bot.
        """
        if not self.enabled:
            return

        self._running = True
        logger.info("Telegram polling loop started")

        while self._running:
            try:
                updates = await self.client.get_updates(
                    offset=self._update_offset, timeout=30
                )

                for update in updates:
                    self._update_offset = update.get("update_id", 0) + 1

                    # Handle callback query (button press)
                    if "callback_query" in update:
                        await self._handle_callback(update["callback_query"])

                    # Handle text message (commands)
                    elif "message" in update:
                        await self._handle_message(update["message"])

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Telegram polling error: {e}")
                await asyncio.sleep(5)

        logger.info("Telegram polling loop stopped")

    def stop(self):
        """Stop the polling loop."""
        self._running = False
