"""
Inventory Management Module
Swaps meme tokens back to SOL/USDC after closing positions.
Uses Jupiter Swap API.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import httpx
from solders.keypair import Keypair  # type: ignore
from solders.pubkey import Pubkey  # type: ignore

from .config import CONFIG
from .utils import (
    jupiter_limiter,
    lamports_to_sol,
    load_keypair,
    now_ts,
    pubkey,
    retry,
    sol_to_lamports,
)

logger = logging.getLogger("dlmm_bot.inventory")



class InventoryManager:
    """Manages token inventory: swaps meme tokens back to SOL."""

    def __init__(self):
        self.cfg = CONFIG.inventory
        self._http: Optional[httpx.AsyncClient] = None
        self._keypair: Optional[Keypair] = None
        # Track pending swaps
        self._pending_swaps: Dict[str, float] = {}  # mint -> amount

    async def initialize(self):
        """Initialize HTTP client and wallet."""
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30))
        self._keypair = load_keypair()
        logger.info("Inventory manager initialized")

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    @retry(max_retries=3, delay=1.0)
    async def get_token_balance(self, mint: str) -> Tuple[float, int]:
        """
        Get token balance for our wallet.
        Returns (ui_amount, raw_amount).
        """
        from solana.rpc.async_api import AsyncClient
        from solana.rpc.commitment import Confirmed

        async with AsyncClient(
            CONFIG.rpc.endpoint, commitment=Confirmed
        ) as client:
            wallet_pubkey = self._keypair.pubkey()
            # Get token accounts for this mint
            resp = await client.get_token_accounts_by_owner_json_parsed(
                wallet_pubkey,
                opts={"mint": pubkey(mint)},
            )
            
            if resp.value:
                for account in resp.value:
                    parsed = account.account.data.parsed
                    info = parsed.get("info", {})
                    token_amount = info.get("tokenAmount", {})
                    ui_amount = float(token_amount.get("uiAmount", 0) or 0)
                    raw = int(token_amount.get("amount", "0"))
                    return (ui_amount, raw)
        
        return (0.0, 0)


    @retry(max_retries=2, delay=1.5)
    async def get_jupiter_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: Optional[int] = None,
    ) -> Optional[Dict]:
        """Get a swap quote from Jupiter."""
        await jupiter_limiter.wait()
        slippage = slippage_bps or self.cfg.swap_slippage_bps

        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage,
            "swapMode": "ExactIn",
        }

        resp = await self._http.get(
            f"{self.cfg.jupiter_swap_api_url}/quote",
            params=params,
        )

        if resp.status_code == 200:
            return resp.json()
        else:
            logger.warning(
                f"Jupiter quote failed: {resp.status_code} {resp.text}"
            )
            return None

    @retry(max_retries=2, delay=2.0)
    async def execute_jupiter_swap(
        self, quote: Dict
    ) -> Optional[str]:
        """
        Execute a swap via Jupiter.
        Returns transaction signature on success.
        """
        if CONFIG.dry_run:
            out_amount = quote.get("outAmount", "0")
            logger.info(
                f"[DRY RUN] Would swap -> output: {out_amount}"
            )
            return "dry_run_sig"

        payload = {
            "quoteResponse": quote,
            "userPublicKey": str(self._keypair.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": CONFIG.position.priority_fee_microlamports,
        }

        resp = await self._http.post(
            f"{self.cfg.jupiter_swap_api_url}/swap",
            json=payload,
        )

        if resp.status_code != 200:
            logger.error(f"Jupiter swap failed: {resp.status_code} {resp.text}")
            return None

        swap_data = resp.json()
        swap_tx = swap_data.get("swapTransaction")

        if not swap_tx:
            logger.error("No swap transaction in Jupiter response")
            return None

        # Decode, sign, and send the transaction
        # In production: deserialize VersionedTransaction, sign, send
        logger.info("Swap transaction received, would sign and send")
        
        # TODO: Implement actual tx signing and sending
        return "mock_signature"


    async def swap_token_to_sol(self, mint: str) -> Optional[str]:
        """
        Swap all holdings of a meme token back to SOL.
        Returns tx signature or None.
        """
        ui_amount, raw_amount = await self.get_token_balance(mint)

        if raw_amount == 0:
            logger.debug(f"No balance for {mint}, nothing to swap")
            return None

        # Check if worth swapping (get USD value)
        quote = await self.get_jupiter_quote(
            input_mint=mint,
            output_mint=self.cfg.sol_mint,
            amount=raw_amount,
        )

        if not quote:
            logger.warning(f"Could not get quote for {mint}")
            return None

        out_amount = int(quote.get("outAmount", "0"))
        out_sol = lamports_to_sol(out_amount)

        # Check minimum value
        sol_price_usd = 170.0  # TODO: get dynamically
        value_usd = out_sol * sol_price_usd

        if value_usd < self.cfg.min_swap_value_usd:
            logger.debug(
                f"Token {mint[:8]}... value ${value_usd:.2f} "
                f"below min ${self.cfg.min_swap_value_usd}. Skipping."
            )
            return None

        logger.info(
            f"Swapping {ui_amount} of {mint[:8]}... "
            f"-> ~{out_sol:.4f} SOL (${value_usd:.2f})"
        )

        sig = await self.execute_jupiter_swap(quote)
        if sig:
            logger.info(f"Swap successful: {sig}")
        return sig

    async def sweep_all_meme_tokens(
        self, mints: List[str]
    ) -> Dict[str, Optional[str]]:
        """
        Sweep all meme token holdings back to SOL.
        Returns dict of {mint: tx_signature_or_None}.
        """
        results = {}
        for mint in mints:
            # Skip SOL and USDC
            if mint in (self.cfg.sol_mint, self.cfg.usdc_mint):
                continue
            try:
                sig = await self.swap_token_to_sol(mint)
                results[mint] = sig
                # Small delay between swaps
                await asyncio.sleep(1.0)
            except Exception as e:
                logger.error(f"Failed to swap {mint[:8]}...: {e}")
                results[mint] = None
        return results
