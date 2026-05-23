# Claire — Meteora DLMM Memecoin

**Automated short-term market maker on Solana memecoin pools.**  
**Strategy: single-sided SOL, fee-first, inventory-light, exit before the dump.**

> You're not a token holder hoping for moon. You're a liquidity provider renting inventory to FOMO traders. Take the rent. Leave before the building burns.

---

## What Claire Does

Claire scans 1000+ Meteora DLMM pools, scores them on 9 dimensions, selects the optimal strategy from 7 presets, runs a 13-point entry checklist, sizes positions with quarter-Kelly math, monitors with a 9-signal exit decision tree, and sweeps all meme tokens back to SOL automatically.

**Full pipeline every 10 seconds:**

```
Scan → Score (70/100 min) → Strategy Select → Entry Checklist (13/13) → Risk Check
  → Position Size (Kelly) → Open → Monitor (9 exit signals) → Close → Sweep → PnL
```

---

## Key Features

### 1. Pool Scoring Model (0-100)

Every pool is scored before entry. Minimum **70/100** required.

| Dimension | Max Points | What It Measures |
|-----------|-----------|------------------|
| Volume Quality | 20 | Real volume, transaction count, consistency |
| Chart Structure | 15 | Uptrend/sideways, not waterfall |
| Holder Distribution | 15 | Not concentrated, top5 < 40% |
| Fee/min Potential | 15 | Actual fee earning rate |
| Liquidity Depth | 10 | Enough to avoid manipulation |
| Pool/Jupiter Sync | 10 | Pool price matches market |
| Narrative/Social | 5 | Community buzz |
| Freshness Bonus | 5 | Sweet-spot age (2-30 min) |
| Volume Growth | 5 | Volume increasing, not declining |

**Penalties** (auto-reject on any >= 15):
- Dev wallet risk (-20)
- Confirmed downtrend (-20)
- Wash volume (-15)
- Pool desync (-15)
- Honeypot (-20)

---

### 2. Seven Strategy Presets

Each preset has exact parameters for entry conditions, position shape, bin count, duration, exit thresholds, and size limits.

| Strategy | When | Shape | Bins | Duration | Size | Exit Target |
|----------|------|-------|------|----------|------|-------------|
| **Fresh Runner** | New launch, <15 min, high vol | Spot | 50 | 5-30 min | 1% bankroll | 5% fee |
| **Heart Attack** | Post-pump retrace, extreme vol | Curve tight | 25 | 2-10 min | 0.5% | 3% fee |
| **After-War** | Settled range, 30min+ age | Bid-Ask | 80 | 30min-2h | 3% | 8% fee |
| **Bid-Ask Flip** | High conviction, clear S/R | Bid-Ask | 100 | 1-4h | 5% | 10% fee |
| **Overnight** | Proven survivor, stable | Spot wide | 150 | 4-8h | 2% | 5% fee |
| **Bear Market** | SOL/USDC, market dead | Spot | 200 | 1-24h | 10% | 2% fee |
| **HFL** | High-freq with automation | Spot tight | 12 | 1h rebal | 2% | 3% fee |

**Auto-selected** based on pool age, volume, holder count, and market conditions.

---

### 3. Entry Checklist (13 Checks, ALL Must Pass)

No position opens without passing every single check:

```
 1. Score >= 70/100
 2. Pool price synced with Jupiter (< 3-5% deviation)
 3. Volume active (above strategy minimum)
 4. Chart not in waterfall (5m > -15% AND 1h > -30%)
 5. Holder count meets strategy minimum
 6. Top5 holder concentration within limits
 7. Liquidity depth sufficient (> 5x position size)
 8. Fee tier within strategy range
 9. Pool age within strategy window
10. Bin step compatible with strategy
11. Not already in this pool
12. Budget available
13. Daily loss limit not hit
```

---

### 4. Exit Decision Tree (9 Signals, Priority-Ordered)

The monitor evaluates **all** conditions every 5 seconds and exits on the highest-priority signal:

| Priority | Signal | Urgency | Condition |
|----------|--------|---------|-----------|
| 1 | Kill Switch | 10/10 | Daily loss limit breached |
| 2 | Rug Detected | 9/10 | Top holder dumping / liquidity pulled |
| 3 | Price Crash | 8/10 | Price dropped 15-40% (per strategy) |
| 4 | Inventory Cap | 7/10 | Meme tokens > 50-80% of position |
| 5 | Out of Range | 6/10 | OOR for 30s-10min (per strategy) |
| 6 | Fee/min Decay | 5/10 | Fee rate dropped 40-70% from peak |
| 7 | Volume Decay | 4/10 | Volume at 20-50% of entry level |
| 8 | Max Duration | 3/10 | Time limit hit (10min-24h per strategy) |
| 9 | Fee Target | 2/10 | Take profit! (3-10% per strategy) |

---

### 5. Risk Management Engine

| Protection | How |
|-----------|-----|
| **Kill Switch** | Stop ALL trading if daily loss > 1 SOL (configurable) |
| **Circuit Breaker** | 10-min cooldown after 3 consecutive losses |
| **Per-Strategy Limits** | Max 2-3 positions per strategy type |
| **Exposure Cap** | Max 5 SOL total across all positions |
| **Position Sizing** | Quarter-Kelly formula adjusted by score |
| **Pool Blocking** | Block pool after losing trade (no re-entry) |
| **Rate Limiting** | Max 20 entries/hour, 10s between entries |
| **Inventory Cap** | Max 2 SOL total in meme tokens |

**Position Sizing Formula:**
```
size = bankroll × strategy_base% × score_multiplier × kelly_multiplier
       (capped by max_single_position and remaining_budget)
```

- Score 85+ → full size (1.0x)
- Score 70 → 60% size (0.6x)
- Kelly uses actual win rate after 3+ trades

---

### 6. Single-Sided SOL (Zero Meme Exposure at Start)

Default: deposit **only SOL** on the quote side. You never buy memecoin upfront.
- If price drops into your range → you DCA into the token while earning fees
- If price stays above → you earn fees on SOL and never touch the token
- After close → any acquired tokens are auto-swapped back to SOL

---

### 7. Automatic Inventory Sweep

After every position close:
1. Check wallet for meme token balances
2. Get Jupiter quote for best swap route
3. Execute swap back to SOL
4. Log the swap cost in PnL

You always end each cycle in SOL — never bagholding.

---

### 8. PnL Tracking (True Net)

```
Net PnL = Fees Earned
        - Impermanent Loss
        - Rent (bin initialization)
        - Swap Slippage
        - Failed TX Costs
        - Priority Fees
```

Session summary on shutdown:
- Win/loss count and win rate
- Total fees earned
- Total costs
- Best and worst trade
- Net PnL in SOL and USD

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    CLAIRE — Main Orchestrator                      │
├──────────┬──────────┬──────────┬──────────┬──────────┬───────────┤
│ Scanner  │ Scorer   │Strategy  │Checklist │  Risk    │ Inventory │
│          │          │Selector  │  Engine  │ Manager  │  Manager  │
│ Meteora  │ 9 dims   │7 presets │13 entry  │Kill swch │ Jupiter   │
│ DEXScr.  │ +penalty │Decision  │9 exit    │Kelly size│ Auto-swap │
│ Filter   │ 70/100   │tree      │Priority  │Cooldown  │ Sweep     │
└────┬─────┴────┬─────┴────┬─────┴────┬─────┴────┬─────┴─────┬─────┘
     │          │          │          │          │           │
     └──────────┴──────────┴──────────┴──────────┴───────────┘
                              │
                    ┌─────────▼─────────┐
                    │ Position Manager  │
                    │ Open / Monitor /  │
                    │ Close / Claim     │
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │   PnL Tracker     │
                    │  pnl_log.json     │
                    └───────────────────┘
```

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/zean6178/Claire.git
cd Claire

# 2. Install
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit: RPC URL (paid!), wallet path, capital limits

# 4. Run (dry-run by default — no real transactions)
python3 run_bot.py
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SOLANA_RPC_URL` | mainnet public | Your RPC endpoint |
| `WALLET_KEY_PATH` | ./wallet.json | Solana keypair file |
| `MAX_TOTAL_SOL` | 5.0 | Total capital budget |
| `MAX_PER_POSITION_SOL` | 0.5 | Per-position cap |

### Key Config (in `dlmm_bot/config.py`)

| Setting | Default | Purpose |
|---------|---------|---------|
| `dry_run` | True | Simulate without sending TX |
| `scan_interval_seconds` | 10 | How often to scan |
| `max_open_positions` | 5 | Concurrent positions |

### Risk Config (in `dlmm_bot/risk.py`)

| Setting | Default | Purpose |
|---------|---------|---------|
| `max_daily_loss_sol` | 1.0 | Kill switch threshold |
| `max_consecutive_losses` | 3 | Circuit breaker |
| `cooldown_after_losses` | 600s | Pause duration |
| `kelly_fraction` | 0.25 | Quarter Kelly |

---

## File Structure

```
Claire/
├── dlmm_bot/
│   ├── __init__.py
│   ├── __main__.py          # python -m dlmm_bot
│   ├── bot.py               # Main orchestrator (integrates everything)
│   ├── config.py            # Base configuration
│   ├── strategies.py        # 7 strategy presets + selector
│   ├── scoring.py           # Pool scoring model (0-100)
│   ├── checklist.py         # Entry checklist + exit decision tree
│   ├── risk.py              # Risk manager, kill switch, sizing
│   ├── scanner.py           # Pool discovery + filtering
│   ├── position_manager.py  # DLMM position open/close
│   ├── monitor.py           # Position monitoring
│   ├── inventory.py         # Jupiter swap (meme → SOL)
│   ├── pnl.py              # PnL tracking + reporting
│   └── utils.py            # Rate limiters, retry, helpers
├── run_bot.py               # CLI entry point
├── requirements.txt
├── .env.example
└── README.md
```

---

## What Makes Claire Different

| Aspect | Typical LP Bot | Claire |
|--------|---------------|--------|
| Entry | Manual pool pick | Auto-scored (9 dimensions, 70/100 min) |
| Strategy | One-size-fits-all | 7 presets auto-selected per conditions |
| Validation | None or basic | 13-point checklist, ALL must pass |
| Exit | Timer or manual | 9 priority signals, decision tree |
| Risk | Hope for the best | Kill switch + circuit breaker + Kelly sizing |
| Sizing | Fixed amount | Dynamic: score × win-rate × Kelly |
| Inventory | Bags accumulate | Auto-sweep to SOL after every close |
| PnL | Fee APY screenshot | True net: fees - IL - rent - swap - failed TX |
| Shape | Usually Spot | Bid-Ask optimized for memecoin volatility |
| Exposure | 50/50 both tokens | Single-sided SOL only |

---

## Production Checklist

Before running with real funds:

- [ ] Use paid RPC (Helius / Quicknode / Triton)
- [ ] Implement Meteora TX building (SDK sidecar or anchorpy)
- [ ] Add dynamic SOL price feed (Jupiter Price API)
- [ ] Connect holder analysis (Helius DAS API)
- [ ] Add WebSocket for real-time pool updates
- [ ] Test with 0.1 SOL positions first
- [ ] Review strategy parameters for current market
- [ ] Set up monitoring / alerting

---

## The Master Rule

> **You are not a token hunter. You are a liquidity provider who rents inventory to FOMO traders. Collect rent. Exit before the house burns down.**

---

## Risk Disclaimer

This bot interacts with DeFi protocols on Solana mainnet. You can lose **all** deposited capital. Risks include impermanent loss, rug pulls, smart contract bugs, transaction failures, and market crashes.

**Only use funds you can afford to lose entirely. This is not financial advice.**

---

## License

MIT — see [LICENSE](./LICENSE)
