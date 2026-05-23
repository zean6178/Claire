"""
Dynamic Price Feed Module
Fetches SOL/USD price from Jupiter Price API with caching.
Replaces hardcoded $170 throughout the bot.
"""

import asyncio
import logging
import time
from typing import Dict, Optional

import httpx

from .config import CONFIG
from .utils import jupiter_limiter, retry

logger = logging.getLogger("dlmm_bot.price_feed")

# Cache duration in seconds
PRICE_CACHE_TTL = 30  # refresh every 30s


class PriceFeed:
    """
    Dynamic price feed with caching.
    Fetches from Jupiter Price API v2.
    Fallback to CoinGecko if Jupiter fails.
    """

    def __init__(self):
        self._cache: Dict[str, tuple] = {}  # mint -> (price_usd, timestamp)
        self._http: Optional[httpx.AsyncClient] = None
        self._sol_mint = CONFIG.inventory.sol_mint

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15))
        return self._http

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def get_sol_price(self) -> float:
        """
        Get current SOL/USD price.
        Returns cached value if fresh, otherwise fetches.
        Falls back to 170.0 if all sources fail.
        """
        return await self.get_token_price(self._sol_mint, fallback=170.0)

    async def get_token_price(
        self, mint: str, fallback: float = 0.0
    ) -> float:
        """Get USD price for any token mint."""
        # Check cache
        cached = self._cache.get(mint)
        if cached:
            price, ts = cached
            if time.time() - ts < PRICE_CACHE_TTL:
                return price

        # Fetch from Jupiter
        price = await self._fetch_jupiter_price(mint)

        # Fallback to CoinGecko for SOL
        if price is None and mint == self._sol_mint:
            price = await self._fetch_coingecko_sol()

        # Use fallback
        if price is None:
            logger.warning(
                f"Price fetch failed for {mint[:8]}..., using fallback ${fallback}"
            )
            return fallback

        # Update cache
        self._cache[mint] = (price, time.time())
        return price

    @retry(max_retries=2, delay=1.0)
    async def _fetch_jupiter_price(self, mint: str) -> Optional[float]:
        """Fetch price from Jupiter Price API v2."""
        await jupiter_limiter.wait()
        http = await self._get_http()

        try:
            resp = await http.get(
                f"{CONFIG.scanner.jupiter_price_api_url}",
                params={"ids": mint},
            )
            if resp.status_code == 200:
                data = resp.json()
                token_data = data.get("data", {}).get(mint)
                if token_data:
                    price = float(token_data.get("price", 0))
                    if price > 0:
                        logger.debug(f"Jupiter price {mint[:8]}...: ${price:.4f}")
                        return price
            elif resp.status_code == 429:
                logger.warning("Jupiter Price API rate limited (429)")
        except Exception as e:
            logger.debug(f"Jupiter price fetch error: {e}")

        return None

    async def _fetch_coingecko_sol(self) -> Optional[float]:
        """Fallback: fetch SOL price from CoinGecko."""
        http = await self._get_http()
        try:
            resp = await http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
            )
            if resp.status_code == 200:
                data = resp.json()
                price = float(data.get("solana", {}).get("usd", 0))
                if price > 0:
                    logger.debug(f"CoinGecko SOL price: ${price:.2f}")
                    return price
        except Exception as e:
            logger.debug(f"CoinGecko fallback error: {e}")
        return None

    async def get_multiple_prices(
        self, mints: list
    ) -> Dict[str, float]:
        """Batch fetch prices for multiple mints."""
        results = {}
        # Jupiter supports batch via comma-separated IDs
        uncached = []
        for mint in mints:
            cached = self._cache.get(mint)
            if cached and time.time() - cached[1] < PRICE_CACHE_TTL:
                results[mint] = cached[0]
            else:
                uncached.append(mint)

        if uncached:
            await jupiter_limiter.wait()
            http = await self._get_http()
            try:
                resp = await http.get(
                    f"{CONFIG.scanner.jupiter_price_api_url}",
                    params={"ids": ",".join(uncached)},
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    for mint in uncached:
                        token_data = data.get(mint)
                        if token_data:
                            price = float(token_data.get("price", 0))
                            if price > 0:
                                self._cache[mint] = (price, time.time())
                                results[mint] = price
            except Exception as e:
                logger.debug(f"Batch price fetch error: {e}")

        return results


# Global singleton
price_feed = PriceFeed()
