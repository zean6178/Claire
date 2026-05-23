"""
DLMM Bot Configuration
All tunable parameters for the Meteora DLMM memecoin fee-farming bot.
"""

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class RpcConfig:
    """Solana RPC configuration."""
    endpoint: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    ws_endpoint: str = os.getenv("SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com")
    commitment: str = "confirmed"
    max_retries: int = 3
    timeout: int = 30


@dataclass
class WalletConfig:
    """Wallet configuration."""
    private_key_path: str = os.getenv("WALLET_KEY_PATH", "wallet.json")
    # Max SOL to deploy across all active positions
    max_total_sol: float = float(os.getenv("MAX_TOTAL_SOL", "5.0"))
    # Max SOL per single position
    max_per_position_sol: float = float(os.getenv("MAX_PER_POSITION_SOL", "0.5"))
    # Reserve SOL for tx fees (never touch this)
    reserve_sol: float = 0.05


@dataclass
class ScannerConfig:
    """Pool scanner filters."""
    # Pool age filters (seconds)
    max_pool_age_seconds: int = 1800  # 30 min for fresh launches
    min_pool_age_seconds: int = 60   # avoid instant rug (at least 1 min old)

    # Volume filters
    min_volume_5m_usd: float = 5000.0    # min $5k volume in last 5 min
    min_volume_1h_usd: float = 50000.0   # min $50k volume in last hour

    # Holder filters
    min_holders: int = 200
    max_top5_holder_pct: float = 40.0  # top 5 wallets hold max 40%

    # Fee tier filter
    min_fee_rate_bps: int = 50   # 0.5% minimum base fee
    max_fee_rate_bps: int = 1000  # 10% maximum

    # Liquidity
    min_liquidity_usd: float = 10000.0

    # Bin step preferences
    preferred_bin_steps: List[int] = field(default_factory=lambda: [100, 125, 150, 200, 250])

    # Scan interval
    scan_interval_seconds: int = 10

    # Meteora API (new endpoint as of 2025)
    meteora_api_url: str = "https://dlmm.datapi.meteora.ag"
    dexscreener_api_url: str = "https://api.dexscreener.com/latest/dex"
    jupiter_price_api_url: str = "https://api.jup.ag/price/v2"

    # Max concurrent pools to track
    max_watch_pools: int = 20


@dataclass
class PositionConfig:
    """DLMM position parameters."""
    # Shape: "bid_ask", "spot", "curve"
    default_shape: str = "bid_ask"

    # Number of bins for the position
    default_num_bins: int = 69  # ~nice range for memes
    min_bins: int = 20
    max_bins: int = 250

    # Single-sided preference
    prefer_single_sided_sol: bool = True

    # Slippage for adding liquidity (bps)
    max_slippage_bps: int = 300  # 3%

    # Priority fee (microlamports)
    priority_fee_microlamports: int = 100_000

    # Compute unit limit
    compute_unit_limit: int = 400_000

    # Max positions open simultaneously
    max_open_positions: int = 5


@dataclass
class MonitorConfig:
    """Position monitoring and exit rules."""
    # Check interval
    monitor_interval_seconds: int = 5

    # Volume decay exit: close if 5min volume drops below this % of entry volume
    volume_decay_threshold_pct: float = 20.0  # close if volume < 20% of when we entered

    # Fee target: close if we've earned this % fee on capital
    fee_target_pct: float = 5.0  # 5% fee on deployed capital = TP

    # Max position duration (seconds)
    max_position_duration_seconds: int = 1800  # 30 min max for fresh memes
    max_position_duration_afterwar: int = 7200  # 2 hours for settled pools

    # Price breakdown exit
    price_drop_exit_pct: float = 30.0  # exit if price drops 30% from entry

    # Out of range tolerance (seconds) - close if out of range for too long
    out_of_range_max_seconds: int = 120  # 2 minutes out of range = close

    # Minimum fee earned before allowing close (to avoid rent loss)
    min_fee_before_close_usd: float = 1.0


@dataclass
class InventoryConfig:
    """Inventory management (swap back to SOL/USDC)."""
    # Auto-swap meme tokens after closing position
    auto_swap_to_sol: bool = True

    # Jupiter swap API
    jupiter_swap_api_url: str = "https://quote-api.jup.ag/v6"

    # Max slippage for swaps
    swap_slippage_bps: int = 500  # 5% for meme tokens (they're volatile)

    # SOL mint
    sol_mint: str = "So11111111111111111111111111111111111111112"
    # USDC mint
    usdc_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    # Minimum token value to bother swapping (USD)
    min_swap_value_usd: float = 0.50


@dataclass
class PnlConfig:
    """PnL tracking configuration."""
    # Log file
    log_file: str = "pnl_log.json"
    # Include rent costs in PnL
    track_rent: bool = True
    # Include failed tx costs
    track_failed_tx: bool = True


@dataclass
class TelegramConfig:
    """Telegram bot configuration."""
    # Bot token from @BotFather
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    # Chat ID to send alerts to (your user ID or group ID)
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Alert settings
    alert_on_screening: bool = True    # Alert when token passes screening
    alert_on_open: bool = True         # Alert when position opens
    alert_on_close: bool = True        # Alert when position closes
    alert_on_risk: bool = True         # Alert on kill switch / cooldown
    alert_on_oor: bool = True          # Alert on out-of-range

    # Minimum score to trigger screening alert
    min_score_for_alert: float = 70.0

    # Rate limit alerts (max per minute)
    max_alerts_per_minute: int = 10

    # Quiet hours (no screening alerts, only risk alerts)
    quiet_hours_start: int = -1        # -1 = disabled. Use 0-23 for hour
    quiet_hours_end: int = -1


@dataclass
class BotConfig:
    """Master bot configuration."""
    rpc: RpcConfig = field(default_factory=RpcConfig)
    wallet: WalletConfig = field(default_factory=WalletConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    position: PositionConfig = field(default_factory=PositionConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    inventory: InventoryConfig = field(default_factory=InventoryConfig)
    pnl: PnlConfig = field(default_factory=PnlConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    # Logging
    log_level: str = "INFO"

    # Dry run mode (simulate without sending transactions)
    dry_run: bool = True

    # Meteora DLMM Program ID
    meteora_dlmm_program_id: str = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"


# Global config instance
CONFIG = BotConfig()
