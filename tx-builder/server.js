/**
 * Claire TX Builder Sidecar
 * 
 * Express HTTP server that handles Meteora DLMM transaction building.
 * Called by the Python bot via HTTP to:
 * - Add liquidity (single-sided SOL, various strategies)
 * - Remove liquidity (close position)
 * - Claim fees
 * 
 * Runs on port 3456 (configurable via TX_BUILDER_PORT env var)
 */

require('dotenv').config({ path: '../.env' });

const express = require('express');
const { Connection, Keypair, PublicKey, VersionedTransaction } = require('@solana/web3.js');
const { DLMM } = require('@meteora-ag/dlmm');
const { BN } = require('bn.js');
const bs58 = require('bs58');
const fs = require('fs');
const path = require('path');

const app = express();
app.use(express.json());

const PORT = process.env.TX_BUILDER_PORT || 3456;
const RPC_URL = process.env.SOLANA_RPC_URL || 'https://api.mainnet-beta.solana.com';
const WALLET_PATH = process.env.WALLET_KEY_PATH || '../wallet.json';

// =============================================================================
// SETUP
// =============================================================================

let connection;
let keypair;

function loadWallet() {
  const keyPath = path.resolve(__dirname, WALLET_PATH);
  if (!fs.existsSync(keyPath)) {
    throw new Error(`Wallet file not found: ${keyPath}`);
  }
  const keyData = JSON.parse(fs.readFileSync(keyPath, 'utf-8'));
  if (Array.isArray(keyData)) {
    return Keypair.fromSecretKey(Uint8Array.from(keyData));
  }
  // Base58 encoded
  return Keypair.fromSecretKey(bs58.decode(keyData));
}

function init() {
  connection = new Connection(RPC_URL, 'confirmed');
  keypair = loadWallet();
  console.log(`TX Builder initialized`);
  console.log(`  RPC: ${RPC_URL.substring(0, 40)}...`);
  console.log(`  Wallet: ${keypair.publicKey.toBase58()}`);
  console.log(`  Port: ${PORT}`);
}

// =============================================================================
// HEALTH CHECK
// =============================================================================

app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    wallet: keypair.publicKey.toBase58(),
    rpc: RPC_URL.substring(0, 40),
  });
});

// =============================================================================
// ADD LIQUIDITY
// =============================================================================

app.post('/add-liquidity', async (req, res) => {
  try {
    const {
      pool_address,
      amount_sol,        // in SOL (float)
      strategy,          // "spot", "curve", "bid_ask"
      min_bin_id,
      max_bin_id,
      single_sided_sol,  // boolean
    } = req.body;

    console.log(`[AddLiquidity] Pool=${pool_address} | SOL=${amount_sol} | Strategy=${strategy}`);

    // Create DLMM instance
    const poolPubkey = new PublicKey(pool_address);
    const dlmmPool = await DLMM.create(connection, poolPubkey);

    // Get active bin
    const activeBin = await dlmmPool.getActiveBin();
    const activeBinId = activeBin.binId;
    console.log(`  Active bin: ${activeBinId}`);

    // Convert SOL to lamports
    const amountLamports = new BN(Math.floor(amount_sol * 1e9));

    // Determine strategy type
    let strategyType;
    switch (strategy) {
      case 'spot':
        strategyType = { Spot: {} };
        break;
      case 'curve':
        strategyType = { Curve: {} };
        break;
      case 'bid_ask':
        strategyType = { BidAsk: {} };
        break;
      default:
        strategyType = { Spot: {} };
    }

    // Build add liquidity transaction
    let tx;
    if (single_sided_sol) {
      // Single-sided: only deposit SOL (quote token Y)
      const addLiquidityParams = {
        connection,
        position: keypair.publicKey, // Will create new position
        user: keypair.publicKey,
        totalXAmount: new BN(0),      // No token X
        totalYAmount: amountLamports, // SOL amount
        strategy: {
          maxBinId: max_bin_id || activeBinId - 1,
          minBinId: min_bin_id || activeBinId - 69,
          strategyType,
        },
      };

      tx = await dlmmPool.addLiquidityByStrategy(addLiquidityParams);
    } else {
      // Both sides
      const addLiquidityParams = {
        connection,
        position: keypair.publicKey,
        user: keypair.publicKey,
        totalXAmount: new BN(0),
        totalYAmount: amountLamports,
        strategy: {
          maxBinId: max_bin_id || activeBinId + 34,
          minBinId: min_bin_id || activeBinId - 35,
          strategyType,
        },
      };

      tx = await dlmmPool.addLiquidityByStrategy(addLiquidityParams);
    }

    // Sign and send
    const txHash = await sendAndConfirmTx(tx);

    console.log(`  TX confirmed: ${txHash}`);
    res.json({
      success: true,
      signature: txHash,
      active_bin_id: activeBinId,
      position: keypair.publicKey.toBase58(),
    });

  } catch (error) {
    console.error(`[AddLiquidity] Error:`, error.message);
    res.status(500).json({
      success: false,
      error: error.message,
    });
  }
});

// =============================================================================
// REMOVE LIQUIDITY
// =============================================================================

app.post('/remove-liquidity', async (req, res) => {
  try {
    const {
      pool_address,
      position_address,  // Position public key
    } = req.body;

    console.log(`[RemoveLiquidity] Pool=${pool_address} | Position=${position_address}`);

    const poolPubkey = new PublicKey(pool_address);
    const dlmmPool = await DLMM.create(connection, poolPubkey);

    // Get user positions
    const positionPubkey = new PublicKey(position_address);
    const { userPositions } = await dlmmPool.getPositionsByUserAndLbPair(keypair.publicKey);

    // Find the matching position
    const position = userPositions.find(
      p => p.publicKey.toBase58() === position_address
    );

    if (!position) {
      return res.status(404).json({
        success: false,
        error: `Position ${position_address} not found`,
      });
    }

    // Remove all liquidity
    const binIds = position.positionData.positionBinData.map(b => b.binId);
    const removeTx = await dlmmPool.removeLiquidity({
      position: positionPubkey,
      user: keypair.publicKey,
      binIds,
      bps: new BN(10000), // 100% = 10000 bps
      shouldClaimAndClose: true, // Also claim fees and close position
    });

    const txHash = await sendAndConfirmTx(removeTx);

    console.log(`  TX confirmed: ${txHash}`);
    res.json({
      success: true,
      signature: txHash,
    });

  } catch (error) {
    console.error(`[RemoveLiquidity] Error:`, error.message);
    res.status(500).json({
      success: false,
      error: error.message,
    });
  }
});

// =============================================================================
// CLAIM FEES
// =============================================================================

app.post('/claim-fees', async (req, res) => {
  try {
    const {
      pool_address,
      position_address,
    } = req.body;

    console.log(`[ClaimFees] Pool=${pool_address} | Position=${position_address}`);

    const poolPubkey = new PublicKey(pool_address);
    const dlmmPool = await DLMM.create(connection, poolPubkey);
    const positionPubkey = new PublicKey(position_address);

    const claimTx = await dlmmPool.claimAllFees({
      owner: keypair.publicKey,
      positions: [positionPubkey],
    });

    const txHash = await sendAndConfirmTx(claimTx);

    console.log(`  TX confirmed: ${txHash}`);
    res.json({
      success: true,
      signature: txHash,
    });

  } catch (error) {
    console.error(`[ClaimFees] Error:`, error.message);
    res.status(500).json({
      success: false,
      error: error.message,
    });
  }
});

// =============================================================================
// GET POSITION INFO
// =============================================================================

app.get('/positions/:pool_address', async (req, res) => {
  try {
    const { pool_address } = req.params;
    const poolPubkey = new PublicKey(pool_address);
    const dlmmPool = await DLMM.create(connection, poolPubkey);

    const { userPositions } = await dlmmPool.getPositionsByUserAndLbPair(keypair.publicKey);

    const positions = userPositions.map(p => ({
      address: p.publicKey.toBase58(),
      lower_bin_id: p.positionData.lowerBinId,
      upper_bin_id: p.positionData.upperBinId,
      total_fee_x_pending: p.positionData.feeX?.toString() || '0',
      total_fee_y_pending: p.positionData.feeY?.toString() || '0',
    }));

    res.json({ success: true, positions });

  } catch (error) {
    console.error(`[GetPositions] Error:`, error.message);
    res.status(500).json({ success: false, error: error.message });
  }
});

// =============================================================================
// HELPERS
// =============================================================================

async function sendAndConfirmTx(tx) {
  // Handle both single tx and array of txs
  const txs = Array.isArray(tx) ? tx : [tx];
  let lastSig = '';

  for (const transaction of txs) {
    // Sign
    if (transaction instanceof VersionedTransaction) {
      transaction.sign([keypair]);
    } else {
      transaction.sign(keypair);
    }

    // Send with preflight checks
    const rawTx = transaction.serialize();
    const sig = await connection.sendRawTransaction(rawTx, {
      skipPreflight: false,
      maxRetries: 3,
    });

    // Confirm
    const confirmation = await connection.confirmTransaction(sig, 'confirmed');
    if (confirmation.value.err) {
      throw new Error(`TX failed: ${JSON.stringify(confirmation.value.err)}`);
    }
    lastSig = sig;
  }

  return lastSig;
}

// =============================================================================
// START SERVER
// =============================================================================

init();
app.listen(PORT, '127.0.0.1', () => {
  console.log(`\nClaire TX Builder running on http://127.0.0.1:${PORT}`);
  console.log(`Endpoints:`);
  console.log(`  GET  /health`);
  console.log(`  POST /add-liquidity`);
  console.log(`  POST /remove-liquidity`);
  console.log(`  POST /claim-fees`);
  console.log(`  GET  /positions/:pool_address`);
});
