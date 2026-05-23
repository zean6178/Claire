"""
DLMM Position Manager
Opens and closes positions on Meteora DLMM pools.
Handles single-sided SOL, Bid-Ask shape, bin calculation.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import base58
import httpx
from solders.keypair import Keypair  # type: ignore
from solders.pubkey import Pubkey  # type: ignore
from solana.rpc.async_api import AsyncClient

from .config import CONFIG
from .scanner import PoolCandidate
from .utils import (
    get_rpc_client,
    lamports_to_sol,
    load_keypair,
    now_ts,
    pubkey,
    retry,
    sol_to_lamports,
)

logger = logging.getLogger("dlmm_bot.position_manager")



class PositionShape(Enum):
    SPOT = "spot"
    CURVE = "curve"
    BID_ASK = "bid_ask"


class PositionSide(Enum):
    SINGLE_SIDED_SOL = "single_sided_sol"
    SINGLE_SIDED_TOKEN = "single_sided_token"
    BOTH_SIDES = "both_sides"


@dataclass
class ActivePosition:
    """Represents an active DLMM position."""
    position_pubkey: str
    pool_address: str
    pool_name: str
    mint_x: str  # memecoin
    mint_y: str  # SOL/USDC
    shape: PositionShape
    side: PositionSide
    bin_ids: List[int]
    lower_bin_id: int
    upper_bin_id: int
    active_bin_at_entry: int
    entry_price: float
    sol_deposited: float
    token_deposited: float
    entry_time: int
    entry_volume_5m: float  # volume at entry for decay comparison
    fees_earned_usd: float = 0.0
    fees_earned_sol: float = 0.0
    last_fee_check: int = 0
    out_of_range_since: Optional[int] = None
    is_closed: bool = False
    close_reason: str = ""



class PositionManager:
    """Manages DLMM positions: open, monitor bins, close."""

    def __init__(self):
        self.cfg = CONFIG.position
        self.positions: Dict[str, ActivePosition] = {}
        self._keypair: Optional[Keypair] = None
        self._rpc: Optional[AsyncClient] = None
        self._http: Optional[httpx.AsyncClient] = None

    async def initialize(self):
        """Load wallet and connect to RPC."""
        self._keypair = load_keypair()
        self._rpc = get_rpc_client()
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30))
        logger.info(
            f"Position manager initialized. "
            f"Wallet: {self._keypair.pubkey()}"
        )

    async def close(self):
        """Clean up connections."""
        if self._rpc:
            await self._rpc.close()
        if self._http and not self._http.is_closed:
            await self._http.aclose()


    def _calculate_bin_range(
        self, active_bin_id: int, num_bins: int, side: PositionSide
    ) -> Tuple[int, int]:
        """
        Calculate bin range for position.
        Single-sided SOL: bins BELOW active bin (we provide SOL, 
        get filled with token as price drops into our range).
        """
        if side == PositionSide.SINGLE_SIDED_SOL:
            # Place bins below current price
            # SOL is quote (mint_y), so we put liquidity below active bin
            lower = active_bin_id - num_bins
            upper = active_bin_id - 1  # just below current
            return (lower, upper)
        elif side == PositionSide.SINGLE_SIDED_TOKEN:
            # Place bins above current price
            lower = active_bin_id + 1
            upper = active_bin_id + num_bins
            return (lower, upper)
        else:
            # Both sides: center around active bin
            half = num_bins // 2
            lower = active_bin_id - half
            upper = active_bin_id + half
            return (lower, upper)


    def _calculate_distribution(
        self, shape: PositionShape, num_bins: int
    ) -> List[Dict[str, Any]]:
        """
        Calculate liquidity distribution across bins based on shape.
        Returns list of {bin_id_offset, weight} for the Meteora instruction.
        """
        weights = []

        if shape == PositionShape.SPOT:
            # Uniform distribution
            w = 1.0 / num_bins
            weights = [w] * num_bins

        elif shape == PositionShape.CURVE:
            # Gaussian-like: more in center
            import math
            center = num_bins / 2.0
            sigma = num_bins / 4.0
            raw = []
            for i in range(num_bins):
                val = math.exp(-0.5 * ((i - center) / sigma) ** 2)
                raw.append(val)
            total = sum(raw)
            weights = [r / total for r in raw]

        elif shape == PositionShape.BID_ASK:
            # More weight at edges (good for volatility capture)
            import math
            center = num_bins / 2.0
            sigma = num_bins / 6.0
            raw = []
            for i in range(num_bins):
                # Inverse gaussian: high at edges, low at center
                gaussian = math.exp(-0.5 * ((i - center) / sigma) ** 2)
                val = 1.0 - gaussian + 0.1  # floor so no bin is zero
                raw.append(val)
            total = sum(raw)
            weights = [r / total for r in raw]

        return weights


    async def open_position(
        self,
        candidate: PoolCandidate,
        sol_amount: float,
        shape: Optional[PositionShape] = None,
        side: Optional[PositionSide] = None,
        num_bins: Optional[int] = None,
    ) -> Optional[ActivePosition]:
        """
        Open a DLMM position on the given pool.
        
        In production, this sends the actual Meteora AddLiquidity 
        instruction. For now, builds the params and logs/simulates.
        """
        shape = shape or PositionShape(self.cfg.default_shape)
        side = side or (
            PositionSide.SINGLE_SIDED_SOL
            if self.cfg.prefer_single_sided_sol
            else PositionSide.BOTH_SIDES
        )
        num_bins = num_bins or self.cfg.default_num_bins

        # Check limits
        if len(self.positions) >= self.cfg.max_open_positions:
            logger.warning("Max open positions reached. Cannot open new.")
            return None

        if sol_amount > CONFIG.wallet.max_per_position_sol:
            sol_amount = CONFIG.wallet.max_per_position_sol
            logger.info(f"Capped position size to {sol_amount} SOL")

        # Calculate bin range
        lower_bin, upper_bin = self._calculate_bin_range(
            candidate.active_bin_id, num_bins, side
        )
        bin_ids = list(range(lower_bin, upper_bin + 1))

        # Calculate distribution weights
        weights = self._calculate_distribution(shape, len(bin_ids))

        logger.info(
            f"Opening position on {candidate.name} | "
            f"Shape={shape.value} | Side={side.value} | "
            f"Bins={lower_bin}..{upper_bin} ({len(bin_ids)} bins) | "
            f"SOL={sol_amount} | ActiveBin={candidate.active_bin_id}"
        )

        if CONFIG.dry_run:
            logger.info("[DRY RUN] Would send AddLiquidity tx")
            position_key = f"dry_{candidate.address}_{now_ts()}"
        else:
            position_key = await self._send_add_liquidity_tx(
                candidate, sol_amount, bin_ids, weights, side
            )
            if not position_key:
                return None

        # Create position record
        position = ActivePosition(
            position_pubkey=position_key,
            pool_address=candidate.address,
            pool_name=candidate.name,
            mint_x=candidate.mint_x,
            mint_y=candidate.mint_y,
            shape=shape,
            side=side,
            bin_ids=bin_ids,
            lower_bin_id=lower_bin,
            upper_bin_id=upper_bin,
            active_bin_at_entry=candidate.active_bin_id,
            entry_price=candidate.current_price,
            sol_deposited=sol_amount,
            token_deposited=0.0,
            entry_time=now_ts(),
            entry_volume_5m=candidate.volume_5m_usd,
        )

        self.positions[position_key] = position
        logger.info(f"Position opened: {position_key}")
        return position


    async def _send_add_liquidity_tx(
        self,
        candidate: PoolCandidate,
        sol_amount: float,
        bin_ids: List[int],
        weights: List[float],
        side: PositionSide,
    ) -> Optional[str]:
        """
        Build and send the AddLiquidity transaction to Meteora DLMM.
        
        NOTE: This is the integration point with Meteora's on-chain program.
        In production, you would use:
        1. meteora-dlmm-sdk (TypeScript) via a sidecar, OR
        2. Direct instruction building with anchor IDL, OR
        3. Meteora's transaction API if available.
        
        The actual instruction structure:
        - Program: LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo
        - Instruction: AddLiquidityByStrategy or AddLiquidityOneSide
        - Accounts: position, lb_pair, bin_arrays, user token accounts, etc.
        """
        try:
            # Build instruction params
            program_id = pubkey(CONFIG.meteora_dlmm_program_id)
            pool_pubkey = pubkey(candidate.address)
            
            amount_lamports = sol_to_lamports(sol_amount)

            # In production: derive position PDA, create position account,
            # build AddLiquidityByStrategy instruction with:
            # - strategy_type: Bid-Ask / Spot / Curve
            # - min_bin_id, max_bin_id
            # - amount_x (token), amount_y (SOL)
            # - For single-sided SOL: amount_x = 0, amount_y = lamports

            logger.info(
                f"TX: AddLiquidity to {candidate.address} | "
                f"amount_y={amount_lamports} lamports | "
                f"bins={bin_ids[0]}..{bin_ids[-1]}"
            )

            # TODO: Implement actual transaction building
            # This requires either:
            # 1. Porting Meteora SDK to Python (complex), or
            # 2. Using a TypeScript sidecar process, or  
            # 3. Using Meteora's API endpoint for tx construction

            # Placeholder: return a mock position key
            # In production, this would be the actual position PDA
            return f"pos_{candidate.address[:8]}_{now_ts()}"

        except Exception as e:
            logger.error(f"Failed to send AddLiquidity tx: {e}")
            return None


    async def close_position(
        self, position_key: str, reason: str = "manual"
    ) -> bool:
        """
        Close (remove liquidity from) a position.
        Claims fees + removes all liquidity.
        """
        position = self.positions.get(position_key)
        if not position or position.is_closed:
            logger.warning(f"Position {position_key} not found or already closed")
            return False

        logger.info(
            f"Closing position {position_key} | "
            f"Pool={position.pool_name} | Reason={reason}"
        )

        if CONFIG.dry_run:
            logger.info("[DRY RUN] Would send RemoveLiquidity + ClaimFee tx")
            success = True
        else:
            success = await self._send_remove_liquidity_tx(position)

        if success:
            position.is_closed = True
            position.close_reason = reason
            logger.info(
                f"Position closed. Fees earned: "
                f"${position.fees_earned_usd:.2f} / "
                f"{position.fees_earned_sol:.4f} SOL"
            )

        return success

    async def _send_remove_liquidity_tx(
        self, position: ActivePosition
    ) -> bool:
        """
        Build and send RemoveAllLiquidity + ClaimFee transaction.
        """
        try:
            # In production:
            # 1. ClaimFee instruction (claim accrued fees)
            # 2. RemoveAllLiquidity instruction
            # 3. ClosePosition instruction (reclaim rent)
            # All can be bundled in one transaction

            logger.info(
                f"TX: RemoveAllLiquidity from {position.pool_address} | "
                f"Position: {position.position_pubkey}"
            )

            # TODO: Implement actual transaction
            return True

        except Exception as e:
            logger.error(f"Failed to close position: {e}")
            return False

    async def claim_fees(self, position_key: str) -> Tuple[float, float]:
        """Claim fees from a position without closing it."""
        position = self.positions.get(position_key)
        if not position or position.is_closed:
            return (0.0, 0.0)

        if CONFIG.dry_run:
            logger.debug(f"[DRY RUN] Would claim fees for {position_key}")
            return (0.0, 0.0)

        # TODO: Send ClaimFee instruction and parse results
        # Returns (sol_fees, token_fees)
        return (0.0, 0.0)

    def get_active_positions(self) -> List[ActivePosition]:
        """Get all non-closed positions."""
        return [p for p in self.positions.values() if not p.is_closed]

    def get_total_deployed_sol(self) -> float:
        """Get total SOL deployed across active positions."""
        return sum(
            p.sol_deposited
            for p in self.positions.values()
            if not p.is_closed
        )
