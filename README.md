# Claire — DLMM Memecoin Fee-Farming Bot

Automated Meteora DLMM liquidity provider bot for Solana memecoin pools.  
**Strategy: single-sided SOL, Bid-Ask shape, fee-first, inventory-light.**

> ⚠️ **This is not financial advice.** DLMM memecoin LP is high-risk. You can lose your entire deposit. Use at your own risk.

---

## Strategy Overview

Claire acts as a **short-term market maker** on Meteora DLMM memecoin pools. The goal is to harvest trading fees from high-volume pools, not to hold meme tokens.

### Core Principles

| # | Principle | Implementation |
|---|-----------|---------------|
| 1 | **Fee-first** | Target pools with high volume relative to liquidity |
| 2 | **Single-sided SOL** | Deposit only SOL (quote side) to avoid immediate meme exposure |
| 3 | **Bid-Ask shape** | More liquidity at edges for volatility capture |
| 4 | **Fast exit** | Close on volume decay, fee target hit, price breakdown, or timeout |
| 5 | **Inventory sweep** | Swap any acquired meme tokens back to SOL via Jupiter |

### How It Works

```
┌─────────┐     ┌──────────────┐     ┌─────────────┐
│ Scanner │────▶│ Position Mgr │────▶│   Monitor   │
│         │     │              │     │             │
│ Meteora │     │ Open DLMM    │     │ Fee target? │
│ DEXScr. │     │ Single-sided │     │ Vol decay?  │
│ Filter  │     │ Bid-Ask      │     │ Price drop? │
│ Score   │     │              │     │ OOR timeout?│
└─────────┘     └──────────────┘     └──────┬──────┘
                                             │
                                     ┌───────▼───────┐
                                     │  Close + Swap │
                                     │  Jupiter → SOL│
                                     │  Log PnL      │
                                     └───────────────┘
```

---

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

**Requirements:** Python 3.10+

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```env
SOLANA_RPC_URL=https://your-paid-rpc.com    # Helius/Quicknode/Triton recommended
WALLET_KEY_PATH=./wallet.json               # Solana CLI keypair format
MAX_TOTAL_SOL=5.0                           # Total capital budget
MAX_PER_POSITION_SOL=0.5                    # Per-position limit
```

### 3. Run (Dry-Run Mode)

```bash
python3 run_bot.py
# or
python3 -m dlmm_bot
```

By default, the bot runs in **dry-run mode** — it scans, scores, and logs decisions without sending any transactions. To go live, set `dry_run = False` in `dlmm_bot/config.py`.

---

## Project Structure

```
Claire/
├── dlmm_bot/
│   ├── __init__.py          # Package metadata
│   ├── __main__.py          # python -m dlmm_bot entry
│   ├── config.py            # All tunable parameters
│   ├── scanner.py           # Pool discovery & filtering
│   ├── position_manager.py  # Open/close DLMM positions
│   ├── monitor.py           # Exit signal detection
│   ├── inventory.py         # Token → SOL swap via Jupiter
│   ├── pnl.py              # PnL tracking & reporting
│   ├── bot.py              # Main orchestrator
│   └── utils.py            # Helpers, rate limiters, retry logic
├── run_bot.py               # CLI entry point
├── requirements.txt         # Python dependencies
├── .env.example             # Environment template
└── README.md
```

---

## Configuration

All parameters are in `dlmm_bot/config.py`. Key settings:

### Scanner Filters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_pool_age_seconds` | 1800 | Max pool age (30 min) |
| `min_volume_5m_usd` | $5,000 | Min 5-minute volume |
| `min_volume_1h_usd` | $50,000 | Min 1-hour volume |
| `min_holders` | 200 | Min holder count |
| `max_top5_holder_pct` | 40% | Max concentration |
| `preferred_bin_steps` | [100,125,150,200,250] | Acceptable bin steps |

### Position Settings

| Parameter | Default | Description |
|-----------|---------|-------------|
| `default_shape` | bid_ask | Liquidity distribution shape |
| `default_num_bins` | 69 | Number of bins |
| `prefer_single_sided_sol` | true | Only deposit SOL |
| `max_open_positions` | 5 | Concurrent positions |
| `max_slippage_bps` | 300 | 3% slippage tolerance |

### Exit Rules (Monitor)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `fee_target_pct` | 5% | Close when fees reach 5% of capital |
| `volume_decay_threshold_pct` | 20% | Close if volume drops to 20% of entry |
| `max_position_duration_seconds` | 1800 | 30 min max hold |
| `price_drop_exit_pct` | 30% | Close on 30% price drop |
| `out_of_range_max_seconds` | 120 | Close if OOR for 2 min |

### Inventory

| Parameter | Default | Description |
|-----------|---------|-------------|
| `auto_swap_to_sol` | true | Auto-swap meme → SOL after close |
| `swap_slippage_bps` | 500 | 5% slippage for meme swaps |

---

## Exit Signals

The monitor checks every 5 seconds and closes positions when:

1. **Fee Target Hit** — earned enough fees relative to capital
2. **Volume Decay** — pool volume collapsed vs entry
3. **Max Duration** — position held too long
4. **Price Breakdown** — token price crashed
5. **Out of Range Timeout** — position not earning for too long
6. **Rug Detected** — abnormal holder/liquidity changes

---

## PnL Tracking

Net PnL is calculated as:

```
Net = Fees Earned - Impermanent Loss - Rent - Swap Costs - Failed TX Costs
```

Session summaries are printed on shutdown and persisted to `pnl_log.json`.

---

## Production TODOs

Before going live, you need to implement:

- [ ] **Meteora transaction building** — AddLiquidity/RemoveLiquidity/ClaimFee instructions. Options:
  - TypeScript sidecar using `@meteora-ag/dlmm` SDK
  - Direct instruction building with `anchorpy` + Meteora IDL
  - Meteora API transaction endpoint (if available)
- [ ] **Dynamic SOL price** — fetch from Jupiter Price API instead of hardcoded value
- [ ] **Holder distribution** — use Helius DAS API for accurate top-holder checks
- [ ] **WebSocket subscriptions** — subscribe to pool updates for faster reactions
- [ ] **Transaction confirmation** — proper tx send + confirm loop with retries

---

## Recommended RPC Providers

| Provider | Why |
|----------|-----|
| [Helius](https://helius.dev) | Fast, DAS API for holders, priority fee API |
| [Quicknode](https://quicknode.com) | Reliable, good for high-frequency |
| [Triton](https://triton.one) | Low latency Solana-focused |

Public RPC (`api.mainnet-beta.solana.com`) is **too slow** for memecoin LP.

---

## Risk Warnings

- **Impermanent loss** on memecoin dumps can exceed fee income
- **Rug pulls** can make your position worthless
- **Failed transactions** still cost SOL
- **Rent** for bin initialization is non-trivial at scale
- **Slippage** on meme token swaps can be very high
- **This bot does not guarantee profit**

---

## License

MIT — see [LICENSE](./LICENSE)
