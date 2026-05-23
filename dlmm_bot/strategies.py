"""
Strategy Presets Module
Master Playbook v1.0 — 7 strategy presets with exact parameters.

Each preset defines: shape, bins, duration, size, fee tier, exit rules.
The bot selects a strategy based on market conditions and pool characteristics.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("dlmm_bot.strategies")


class StrategyType(Enum):
    FRESH_RUNNER = "fresh_runner"
    HEART_ATTACK = "heart_attack"
    AFTER_WAR = "after_war"
    BID_ASK_FLIP = "bid_ask_flip"
    OVERNIGHT = "overnight"
    BEAR_MARKET = "bear_market"
    HFL = "hfl"  # High Frequency Liquidity


@dataclass
class StrategyPreset:
    """Complete strategy preset with execution parameters."""

    name: str
    strategy_type: StrategyType
    description: str

    # --- Entry Conditions ---
    min_pool_age_seconds: int = 60
    max_pool_age_seconds: int = 1800
    min_volume_5m_usd: float = 5000.0
    min_volume_1h_usd: float = 20000.0
    min_holders: int = 100
    max_top5_holder_pct: float = 50.0
    min_fee_rate_bps: int = 50
    max_fee_rate_bps: int = 1000
    require_price_sync: bool = True  # pool price must match Jupiter
    price_sync_tolerance_pct: float = 3.0  # max 3% deviation

    # --- Position Parameters ---
    shape: str = "bid_ask"  # spot, curve, bid_ask
    side: str = "single_sided_sol"  # single_sided_sol, single_sided_token, both
    num_bins: int = 69
    bin_step_preferred: List[int] = field(default_factory=lambda: [100, 125, 150])
    position_size_pct_bankroll: float = 2.0  # % of total bankroll
    max_position_sol: float = 0.5

    # --- Duration & Timing ---
    min_duration_seconds: int = 60
    max_duration_seconds: int = 1800  # 30 min
    monitor_interval_seconds: int = 5

    # --- Exit Rules ---
    fee_target_pct: float = 5.0  # close when earned 5% fee on capital
    volume_decay_exit_pct: float = 50.0  # close if vol drops to 50% of peak
    price_drop_exit_pct: float = 30.0  # close on 30% price crash
    out_of_range_max_seconds: int = 120  # close if OOR for 2 min
    inventory_meme_max_pct: float = 70.0  # close if >70% inventory is meme
    fee_per_min_decay_pct: float = 50.0  # close if fee/min drops 50% from peak

    # --- Risk ---
    max_concurrent_same_strategy: int = 3
    require_monitoring: bool = True  # can't leave unattended

    def __post_init__(self):
        if isinstance(self.bin_step_preferred, int):
            self.bin_step_preferred = [self.bin_step_preferred]


# =============================================================================
# STRATEGY PRESETS
# =============================================================================

FRESH_RUNNER = StrategyPreset(
    name="Fresh Runner Sniper",
    strategy_type=StrategyType.FRESH_RUNNER,
    description="Capture fees from newly launched/migrated tokens in first 5-30 min",

    # Entry: very fresh, high volume, spreading holders
    min_pool_age_seconds=60,       # at least 1 min (avoid instant rug)
    max_pool_age_seconds=900,      # max 15 min old
    min_volume_5m_usd=10000.0,     # $10k+ in 5 min
    min_volume_1h_usd=0.0,         # N/A for fresh pools
    min_holders=50,                # at least some distribution
    max_top5_holder_pct=60.0,      # allow higher concentration early
    min_fee_rate_bps=200,          # 2%+ fee tier
    max_fee_rate_bps=1000,         # up to 10%
    require_price_sync=True,
    price_sync_tolerance_pct=5.0,  # wider tolerance for new pools

    # Position: medium range, single-sided SOL
    shape="spot",                  # spot for uncertainty
    side="single_sided_sol",
    num_bins=50,                   # medium range
    bin_step_preferred=[150, 200, 250],
    position_size_pct_bankroll=1.0,  # small: 1% bankroll
    max_position_sol=0.5,

    # Duration: very short
    min_duration_seconds=30,
    max_duration_seconds=1800,     # 30 min max
    monitor_interval_seconds=3,    # check every 3s

    # Exit: aggressive
    fee_target_pct=5.0,            # 5% = take profit
    volume_decay_exit_pct=30.0,    # volume drops to 30% = exit
    price_drop_exit_pct=25.0,      # 25% crash = exit
    out_of_range_max_seconds=60,   # 1 min OOR = exit
    inventory_meme_max_pct=60.0,   # 60% meme = exit
    fee_per_min_decay_pct=50.0,

    # Risk
    max_concurrent_same_strategy=3,
    require_monitoring=True,
)

HEART_ATTACK = StrategyPreset(
    name="Heart Attack",
    strategy_type=StrategyType.HEART_ATTACK,
    description="Ultra-short fee capture from first retrace after aggressive bullish candle",

    # Entry: right after big pump, first healthy pullback
    min_pool_age_seconds=120,
    max_pool_age_seconds=3600,     # can be slightly older
    min_volume_5m_usd=20000.0,     # $20k+ (extreme volume)
    min_volume_1h_usd=100000.0,    # $100k+ hourly
    min_holders=150,
    max_top5_holder_pct=50.0,
    min_fee_rate_bps=200,
    max_fee_rate_bps=1000,
    require_price_sync=True,
    price_sync_tolerance_pct=3.0,

    # Position: TIGHT range, quick in/out
    shape="curve",                 # curve tight for chop capture
    side="single_sided_sol",
    num_bins=25,                   # very tight
    bin_step_preferred=[100, 125],
    position_size_pct_bankroll=0.5,  # tiny size: max risk
    max_position_sol=0.25,         # absolute max 0.25 SOL

    # Duration: minutes only
    min_duration_seconds=15,
    max_duration_seconds=600,      # 10 min max!
    monitor_interval_seconds=2,    # check every 2s

    # Exit: ultra aggressive
    fee_target_pct=3.0,            # 3% = done
    volume_decay_exit_pct=40.0,
    price_drop_exit_pct=15.0,      # 15% drop = instant exit
    out_of_range_max_seconds=30,   # 30s OOR = exit
    inventory_meme_max_pct=50.0,
    fee_per_min_decay_pct=40.0,

    # Risk
    max_concurrent_same_strategy=2,
    require_monitoring=True,       # MUST be watching
)

AFTER_WAR = StrategyPreset(
    name="After-War Range",
    strategy_type=StrategyType.AFTER_WAR,
    description="Calmer fee farming after launch chaos settles, token forms range",

    # Entry: survived initial period, volume still decent
    min_pool_age_seconds=1800,     # at least 30 min old
    max_pool_age_seconds=86400,    # up to 24h
    min_volume_5m_usd=3000.0,      # lower threshold ok
    min_volume_1h_usd=30000.0,     # $30k/h minimum
    min_holders=200,               # good distribution
    max_top5_holder_pct=40.0,      # tighter concentration check
    min_fee_rate_bps=100,          # 1%+ fee
    max_fee_rate_bps=500,          # up to 5%
    require_price_sync=True,
    price_sync_tolerance_pct=2.0,

    # Position: wider range at support area
    shape="bid_ask",               # bid-ask for DCA effect
    side="single_sided_sol",
    num_bins=80,                   # wider range
    bin_step_preferred=[100, 125, 150],
    position_size_pct_bankroll=3.0,  # can deploy more
    max_position_sol=1.0,

    # Duration: longer hold
    min_duration_seconds=300,
    max_duration_seconds=7200,     # up to 2 hours
    monitor_interval_seconds=10,   # check every 10s

    # Exit: more patient
    fee_target_pct=8.0,            # higher target ok
    volume_decay_exit_pct=20.0,    # volume must really die
    price_drop_exit_pct=35.0,      # wider stop
    out_of_range_max_seconds=180,  # 3 min tolerance
    inventory_meme_max_pct=70.0,
    fee_per_min_decay_pct=60.0,

    # Risk
    max_concurrent_same_strategy=3,
    require_monitoring=False,      # can leave briefly
)

BID_ASK_FLIP = StrategyPreset(
    name="Bid-Ask Flip",
    strategy_type=StrategyType.BID_ASK_FLIP,
    description="Swing trade via DLMM: enter near support, collect fees, exit near resistance",

    # Entry: clear S/R structure, high conviction token
    min_pool_age_seconds=3600,     # at least 1h old
    max_pool_age_seconds=604800,   # up to 7 days
    min_volume_5m_usd=2000.0,
    min_volume_1h_usd=20000.0,
    min_holders=300,
    max_top5_holder_pct=35.0,      # well distributed
    min_fee_rate_bps=50,
    max_fee_rate_bps=500,
    require_price_sync=True,
    price_sync_tolerance_pct=2.0,

    # Position: bid-ask, can be both sides for high conviction
    shape="bid_ask",
    side="single_sided_sol",       # start SOL, flip when filled
    num_bins=100,                  # wide
    bin_step_preferred=[80, 100, 125],
    position_size_pct_bankroll=5.0,  # higher conviction = bigger
    max_position_sol=2.0,

    # Duration: hours
    min_duration_seconds=600,
    max_duration_seconds=14400,    # up to 4 hours
    monitor_interval_seconds=15,

    # Exit: swing-trade style
    fee_target_pct=10.0,           # bigger target
    volume_decay_exit_pct=15.0,
    price_drop_exit_pct=40.0,      # wider stop, higher conviction
    out_of_range_max_seconds=300,  # 5 min tolerance
    inventory_meme_max_pct=80.0,   # ok to hold more (high conviction)
    fee_per_min_decay_pct=70.0,

    # Risk
    max_concurrent_same_strategy=2,
    require_monitoring=False,
)

OVERNIGHT = StrategyPreset(
    name="Overnight Hold",
    strategy_type=StrategyType.OVERNIGHT,
    description="Wide range, small size, for pools stable enough to leave unattended",

    # Entry: proven survivor, stable range
    min_pool_age_seconds=14400,    # at least 4h old
    max_pool_age_seconds=604800,   # up to 7 days
    min_volume_5m_usd=1000.0,      # lower threshold
    min_volume_1h_usd=15000.0,
    min_holders=400,               # well distributed
    max_top5_holder_pct=30.0,      # strict
    min_fee_rate_bps=50,
    max_fee_rate_bps=300,
    require_price_sync=True,
    price_sync_tolerance_pct=1.5,

    # Position: very wide, small size
    shape="spot",                  # spot wide for safety
    side="single_sided_sol",
    num_bins=150,                  # very wide
    bin_step_preferred=[80, 100, 125],
    position_size_pct_bankroll=2.0,
    max_position_sol=0.5,

    # Duration: hours (overnight)
    min_duration_seconds=3600,
    max_duration_seconds=28800,    # up to 8 hours
    monitor_interval_seconds=60,   # check every minute

    # Exit: conservative
    fee_target_pct=5.0,
    volume_decay_exit_pct=10.0,    # only exit if volume REALLY dies
    price_drop_exit_pct=40.0,      # wide stop
    out_of_range_max_seconds=600,  # 10 min tolerance
    inventory_meme_max_pct=60.0,
    fee_per_min_decay_pct=70.0,

    # Risk
    max_concurrent_same_strategy=2,
    require_monitoring=False,      # designed for unattended
)

BEAR_MARKET = StrategyPreset(
    name="Bear Market SOL/USDC",
    strategy_type=StrategyType.BEAR_MARKET,
    description="Safe SOL/USDC or SOL/LST farming when meme market is dead",

    # Entry: large/stable pairs only
    min_pool_age_seconds=86400,    # at least 1 day old
    max_pool_age_seconds=99999999, # no max
    min_volume_5m_usd=500.0,       # lower for stable pairs
    min_volume_1h_usd=5000.0,
    min_holders=0,                 # N/A for SOL/USDC
    max_top5_holder_pct=100.0,     # N/A
    min_fee_rate_bps=1,            # even 0.01% is fine
    max_fee_rate_bps=100,          # low fee tiers
    require_price_sync=True,
    price_sync_tolerance_pct=0.5,  # very tight for stables

    # Position: wide, larger size
    shape="spot",
    side="single_sided_sol",       # accumulate SOL on dips
    num_bins=200,
    bin_step_preferred=[1, 2, 5, 10, 15, 20],
    position_size_pct_bankroll=10.0,  # can deploy more in safe pairs
    max_position_sol=5.0,

    # Duration: very long
    min_duration_seconds=3600,
    max_duration_seconds=86400,    # up to 24h
    monitor_interval_seconds=120,  # check every 2 min

    # Exit: very patient
    fee_target_pct=2.0,            # lower target for stable pairs
    volume_decay_exit_pct=5.0,     # nearly never exit on volume
    price_drop_exit_pct=50.0,      # SOL can move a lot
    out_of_range_max_seconds=1800, # 30 min tolerance
    inventory_meme_max_pct=100.0,  # N/A
    fee_per_min_decay_pct=80.0,

    # Risk
    max_concurrent_same_strategy=2,
    require_monitoring=False,
)

HFL = StrategyPreset(
    name="High Frequency Liquidity",
    strategy_type=StrategyType.HFL,
    description="Ultra-tight range with auto-rebalance, requires automation (HawkFi-style)",

    # Entry: high volume chop or uptrend
    min_pool_age_seconds=300,
    max_pool_age_seconds=86400,
    min_volume_5m_usd=15000.0,     # high volume required
    min_volume_1h_usd=80000.0,
    min_holders=200,
    max_top5_holder_pct=45.0,
    min_fee_rate_bps=100,
    max_fee_rate_bps=1000,
    require_price_sync=True,
    price_sync_tolerance_pct=2.0,

    # Position: ultra tight, auto-rebalance
    shape="spot",                  # tight spot
    side="single_sided_sol",
    num_bins=12,                   # 6-18 bins
    bin_step_preferred=[100, 125, 150, 200],
    position_size_pct_bankroll=2.0,
    max_position_sol=0.5,

    # Duration: short with rebalance
    min_duration_seconds=60,
    max_duration_seconds=3600,     # 1h between rebalances
    monitor_interval_seconds=3,    # very frequent

    # Exit: tight
    fee_target_pct=3.0,
    volume_decay_exit_pct=40.0,
    price_drop_exit_pct=20.0,
    out_of_range_max_seconds=30,   # immediate rebalance on OOR
    inventory_meme_max_pct=50.0,
    fee_per_min_decay_pct=40.0,

    # Risk
    max_concurrent_same_strategy=3,
    require_monitoring=True,       # needs automation
)


# =============================================================================
# STRATEGY REGISTRY
# =============================================================================

ALL_STRATEGIES: Dict[StrategyType, StrategyPreset] = {
    StrategyType.FRESH_RUNNER: FRESH_RUNNER,
    StrategyType.HEART_ATTACK: HEART_ATTACK,
    StrategyType.AFTER_WAR: AFTER_WAR,
    StrategyType.BID_ASK_FLIP: BID_ASK_FLIP,
    StrategyType.OVERNIGHT: OVERNIGHT,
    StrategyType.BEAR_MARKET: BEAR_MARKET,
    StrategyType.HFL: HFL,
}

# Allocation targets (% of bankroll allocated to each strategy)
DEFAULT_ALLOCATION = {
    StrategyType.AFTER_WAR: 50,       # 50% — safest meme farming
    StrategyType.FRESH_RUNNER: 20,    # 20% — high risk, high reward
    StrategyType.BEAR_MARKET: 15,     # 15% — safe pairs fallback
    StrategyType.BID_ASK_FLIP: 10,    # 10% — swing high conviction
    StrategyType.HEART_ATTACK: 5,     # 5%  — max risk, tiny size
    StrategyType.OVERNIGHT: 0,        # allocated from after_war when sleeping
    StrategyType.HFL: 0,              # only if automation available
}


def select_strategy_for_pool(
    pool_age_seconds: int,
    volume_5m_usd: float,
    volume_1h_usd: float,
    holders: int,
    fee_rate_bps: int,
    is_fresh_launch: bool = False,
    is_post_pump: bool = False,
    has_clear_range: bool = False,
    is_stable_pair: bool = False,
    has_automation: bool = False,
) -> Optional[StrategyPreset]:
    """
    Select the best strategy for a given pool based on conditions.
    
    Decision tree:
    1. Stable pair (SOL/USDC)? -> BEAR_MARKET
    2. Has automation + high vol + tight chop? -> HFL
    3. Fresh launch (<15 min)? -> FRESH_RUNNER
    4. Post-pump retrace + extreme vol? -> HEART_ATTACK
    5. Survived + forming range? -> AFTER_WAR
    6. High conviction + clear S/R? -> BID_ASK_FLIP
    7. Default fallback -> AFTER_WAR
    """

    # 1. Stable pair
    if is_stable_pair:
        logger.info("Strategy selected: BEAR_MARKET (stable pair)")
        return BEAR_MARKET

    # 2. HFL with automation
    if has_automation and volume_5m_usd >= 15000 and pool_age_seconds >= 300:
        logger.info("Strategy selected: HFL (automation available + high vol)")
        return HFL

    # 3. Fresh launch
    if is_fresh_launch or pool_age_seconds <= 900:
        if volume_5m_usd >= FRESH_RUNNER.min_volume_5m_usd:
            logger.info("Strategy selected: FRESH_RUNNER (new pool + volume)")
            return FRESH_RUNNER

    # 4. Heart attack (post-pump retrace)
    if is_post_pump and volume_5m_usd >= 20000:
        logger.info("Strategy selected: HEART_ATTACK (post-pump + extreme vol)")
        return HEART_ATTACK

    # 5. After-war (settled range)
    if pool_age_seconds >= 1800 and has_clear_range:
        if volume_1h_usd >= AFTER_WAR.min_volume_1h_usd:
            logger.info("Strategy selected: AFTER_WAR (settled + range)")
            return AFTER_WAR

    # 6. Bid-ask flip (high conviction swing)
    if (
        pool_age_seconds >= 3600
        and holders >= 300
        and has_clear_range
    ):
        logger.info("Strategy selected: BID_ASK_FLIP (high conviction)")
        return BID_ASK_FLIP

    # 7. Default: after_war if qualifies, else fresh_runner
    if pool_age_seconds >= 1800 and volume_1h_usd >= 20000:
        logger.info("Strategy selected: AFTER_WAR (default fallback)")
        return AFTER_WAR

    if volume_5m_usd >= 5000:
        logger.info("Strategy selected: FRESH_RUNNER (default fallback)")
        return FRESH_RUNNER

    logger.debug("No strategy matched pool conditions")
    return None
