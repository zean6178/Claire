# Claire - Meteora DLMM

**Automated liquidity provisioning bot for Solana memecoin pools on Meteora DLMM.**

> Be the market maker. Harvest fees. Exit before the dump.

---

## Key Features

### 1. Single-Sided SOL Entry (Zero Meme Exposure at Start)
Unlike traditional LP where you must hold 50/50 of both tokens, Claire deposits **only SOL** on the quote side. You never buy the memecoin upfront — you only accumulate it if price drops into your range, effectively DCA-ing in while earning fees.

### 2. Intelligent Pool Scanner
Scans 1000+ Meteora DLMM pools every 10 seconds and filters them by:
- Volume intensity (5-min & 1-hour USD volume)
- Pool age (sweet spot: 2-30 minutes old)
- Holder distribution (rejects concentrated supply)
- Fee tier (targets 0.5%-10% base fee pools)
- Bin step compatibility
- Liquidity depth

Each pool gets a **composite score** combining volume/liquidity ratio, fee intensity, freshness, and holder diversity.

### 3. Bid-Ask Shape Distribution
Liquidity is distributed with **more weight at the edges** of your range — optimized for volatile memecoin price action. This captures more fees during large swings compared to uniform (Spot) or center-concentrated (Curve) distributions.

### 4. Multi-Signal Exit Engine
The monitor checks every 5 seconds for exit triggers:

| Signal | What It Means |
|--------|---------------|
| Fee Target Hit | Earned 5%+ fee on deployed capital — take profit |
| Volume Decay | Pool volume dropped to <20% of entry — party's over |
| Max Duration | 30 min timeout — don't overstay |
| Price Breakdown | Token crashed 30%+ — cut losses |
| Out of Range | Position not earning for 2+ min — close & redeploy |

### 5. Automatic Inventory Sweep (Jupiter Swap)
After closing a position, any meme tokens accumulated are **automatically swapped back to SOL** via Jupiter aggregator. You always end each cycle in SOL/USDC — never bagholding dead memes.

### 6. Real-Time PnL Tracking
Every position is tracked with:
```
Net PnL = Fees Earned - IL - Rent Cost - Swap Slippage - Failed TX Cost
```
Session summaries with win/loss ratio, best/worst trade, and total net are logged and persisted.

### 7. Concurrent Position Management
Run up to 5 positions simultaneously across different pools. Capital is automatically sized and distributed. No duplicate entries on the same pool.

### 8. Dry-Run Mode (Safe Testing)
Bot runs in simulation mode by default — full scanning, scoring, and decision logging without sending any on-chain transactions. Go live only when you're confident.

### 9. Rate-Limited & Resilient
Built-in rate limiters for Meteora API (3 req/s), DEXScreener (2 req/s), and Jupiter (5 req/s). All API calls have exponential backoff retry logic. Graceful shutdown closes all positions and sweeps inventory on Ctrl+C.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     DLMM Bot (async)                        │
├─────────────┬──────────────┬─────────────┬─────────────────┤
│   Scanner   │  Position    │   Monitor   │   Inventory     │
│             │  Manager     │             │                 │
│ Meteora API │ Open/Close   │ Exit Signal │ Jupiter Swap    │
│ DEXScreener │ Bin Calc     │ Detection   │ Token → SOL     │
│ Filter/Score│ Distribution │ Fee Track   │ Auto-sweep      │
└──────┬──────┴──────┬───────┴──────┬──────┴────────┬────────┘
       │             │              │               │
       └─────────────┴──────────────┴───────────────┘
                            │
                     ┌──────▼──────┐
                     │  PnL Logger │
                     │  pnl_log.json│
                     └─────────────┘
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
# Edit .env with your RPC URL and wallet path

# 4. Run (dry-run by default)
python3 run_bot.py
```

---

## Configuration

All tunable in `dlmm_bot/config.py` or via environment variables:

| Setting | Default | Description |
|---------|---------|-------------|
| `MAX_TOTAL_SOL` | 5.0 | Total capital budget |
| `MAX_PER_POSITION_SOL` | 0.5 | Max per position |
| `dry_run` | True | Simulation mode |
| `default_shape` | bid_ask | Liquidity shape |
| `default_num_bins` | 69 | Bin count |
| `fee_target_pct` | 5% | Take-profit on fees |
| `max_position_duration` | 30 min | Max hold time |
| `volume_decay_threshold` | 20% | Volume exit trigger |
| `auto_swap_to_sol` | True | Sweep tokens post-close |

---

## What Makes Claire Different?

| Feature | Most LP Bots | Claire |
|---------|-------------|--------|
| Entry exposure | 50/50 token pair | Single-sided SOL only |
| Strategy | Set range & forget | Active scan → enter → monitor → exit |
| Exit logic | Manual / time-based | 5 automatic exit signals |
| Post-close | Hold tokens | Auto-swap to SOL |
| PnL | Guess | Fee - IL - Rent - Swap - FailedTx |
| Pool selection | Manual pick | Auto-scored from 1000+ pools |
| Shape | Usually Spot | Bid-Ask (volatility optimized) |

---

## Production Checklist

Before running with real funds:

- [ ] Use paid RPC (Helius / Quicknode / Triton)
- [ ] Implement Meteora transaction building (SDK sidecar or anchorpy)
- [ ] Add dynamic SOL price feed
- [ ] Test with small capital first (0.1 SOL positions)
- [ ] Set up monitoring / alerting
- [ ] Review exit parameters for current market conditions

---

## Risk Disclaimer

This bot interacts with DeFi protocols on Solana mainnet. Risks include:

- **Impermanent loss** exceeding fee income on token dumps
- **Rug pulls** making positions worthless
- **Smart contract risk** on Meteora
- **Transaction failures** that cost SOL
- **Slippage** on volatile meme token swaps
- **Total loss of deposited capital**

**Only use funds you can afford to lose. This is not financial advice.**

---

## License

MIT
