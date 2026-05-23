# Claire TX Builder Sidecar

Node.js HTTP server that builds and sends Meteora DLMM transactions.
Called by the Python bot via localhost HTTP.

## Setup

```bash
cd tx-builder
npm install
```

## Run

```bash
# Start sidecar (runs on port 3456)
npm start

# Or with PM2 (recommended)
pm2 start server.js --name claire-tx
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/add-liquidity` | Open DLMM position |
| POST | `/remove-liquidity` | Close position + claim fees |
| POST | `/claim-fees` | Claim fees only |
| GET | `/positions/:pool` | Get all positions for a pool |

## Environment

Uses same `.env` as Claire bot (reads from `../.env`):
- `SOLANA_RPC_URL` — RPC endpoint
- `WALLET_KEY_PATH` — Path to wallet.json
- `TX_BUILDER_PORT` — Server port (default: 3456)
