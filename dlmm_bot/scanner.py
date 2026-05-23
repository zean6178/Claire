"""
Pool Scanner Module
Discovers and filters Meteora DLMM pools for memecoin fee farming.
Uses Meteora API + DEXScreener for volume/holder data.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from .config import CONFIG
from .utils import (
    dexscreener_limiter,
    get_http_client,
    meteora_limiter,
    now_ts,
    retry,
)

logger = logging.getLogger("dlmm_bot.scanner")


@dataclass
class PoolCandidate:
    """A pool that passed filters and is a candidate for LP."""
    address: str
    name: str
    mint_x: str  # base token (usually memecoin)
    mint_y: str  # quote token (SOL/USDC)
    bin_step: int
    base_fee_bps: int
    current_price: float
    liquidity_usd: float
    volume_5m_usd: float
    volume_1h_usd: float
    fee_rate_5m_usd: float
    holders: int
    pool_age_seconds: int
    active_bin_id: int
    created_at: int  # unix timestamp
    score: float = 0.0  # ranking score
    extra: Dict[str, Any] = field(default_factory=dict)


class PoolScanner:
    """Scans and filters Meteora DLMM pools."""

    def __init__(self):
        self.cfg = CONFIG.scanner
        self._http: Optional[httpx.AsyncClient] = None
        self._known_pools: Dict[str, float] = {}  # address -> last_seen_ts

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(30),
                limits=httpx.Limits(max_connections=20),
            )
        return self._http

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    @retry(max_retries=3, delay=1.0)
    async def fetch_all_pools(self) -> List[Dict[str, Any]]:
        """Fetch all DLMM pools from Meteora API."""
        await meteora_limiter.wait()
        http = await self._get_http()
        resp = await http.get(f"{self.cfg.meteora_api_url}/pair/all")
        resp.raise_for_status()
        return resp.json()

    @retry(max_retries=3, delay=1.0)
    async def fetch_pool_detail(self, pool_address: str) -> Dict[str, Any]:
        """Fetch detailed info for a specific pool."""
        await meteora_limiter.wait()
        http = await self._get_http()
        resp = await http.get(f"{self.cfg.meteora_api_url}/pair/{pool_address}")
        resp.raise_for_status()
        return resp.json()

    @retry(max_retries=2, delay=1.0)
    async def fetch_dexscreener_data(self, token_address: str) -> Optional[Dict[str, Any]]:
        """Fetch token data from DEXScreener for holder/volume info."""
        await dexscreener_limiter.wait()
        http = await self._get_http()
        try:
            resp = await http.get(
                f"{self.cfg.dexscreener_api_url}/tokens/{token_address}"
            )
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get("pairs", [])
                if pairs:
                    return pairs[0]
        except Exception as e:
            logger.debug(f"DEXScreener lookup failed for {token_address}: {e}")
        return None

    def _passes_basic_filters(self, pool: Dict[str, Any]) -> bool:
        """Quick filter pass on raw pool data."""
        try:
            # Must be SOL or USDC pair
            mint_y = pool.get("mint_y", "")
            sol_mint = CONFIG.inventory.sol_mint
            usdc_mint = CONFIG.inventory.usdc_mint
            if mint_y not in (sol_mint, usdc_mint):
                return False

            # Bin step check
            bin_step = int(pool.get("bin_step", 0))
            if bin_step not in self.cfg.preferred_bin_steps:
                return False

            # Fee rate check
            base_fee = int(pool.get("base_fee_percentage", "0").replace(".", ""))
            # This is approximate; Meteora API returns fee differently
            # We'll do a more precise check later

            # Liquidity check
            liquidity = float(pool.get("liquidity", 0))
            if liquidity < self.cfg.min_liquidity_usd:
                return False

            # Volume check (trade_volume_24h as proxy)
            volume_24h = float(pool.get("trade_volume_24h", 0))
            if volume_24h < self.cfg.min_volume_1h_usd:
                return False

            return True
        except (ValueError, TypeError, KeyError):
            return False

    def _calculate_pool_age(self, pool: Dict[str, Any]) -> int:
        """Calculate pool age in seconds."""
        created = pool.get("created_at") or pool.get("open_time")
        if created:
            try:
                created_ts = int(created)
                # Meteora sometimes uses milliseconds
                if created_ts > 1e12:
                    created_ts = created_ts // 1000
                return now_ts() - created_ts
            except (ValueError, TypeError):
                pass
        return 999999  # unknown age = old

    def _score_pool(self, candidate: PoolCandidate) -> float:
        """
        Score a pool candidate. Higher = better opportunity.
        Factors: volume/liquidity ratio, fee rate, freshness, holder count.
        """
        score = 0.0

        # Volume intensity (volume relative to liquidity)
        if candidate.liquidity_usd > 0:
            vol_intensity = candidate.volume_5m_usd / candidate.liquidity_usd
            score += min(vol_intensity * 100, 50)  # cap at 50 points

        # Fee earning potential
        if candidate.liquidity_usd > 0:
            fee_intensity = candidate.fee_rate_5m_usd / candidate.liquidity_usd
            score += min(fee_intensity * 200, 30)  # cap at 30 points

        # Freshness bonus (newer = higher score, but not TOO new)
        age = candidate.pool_age_seconds
        if 120 <= age <= 600:  # 2-10 min = sweet spot
            score += 15
        elif 600 < age <= 1800:  # 10-30 min
            score += 10
        elif 1800 < age <= 3600:  # 30-60 min
            score += 5

        # Holder diversity bonus
        if candidate.holders >= 500:
            score += 10
        elif candidate.holders >= 200:
            score += 5

        return round(score, 2)

    async def scan(self) -> List[PoolCandidate]:
        """
        Main scan loop: fetch pools, filter, enrich, score, return candidates.
        """
        logger.info("Scanning for DLMM pool candidates...")

        # Fetch all pools
        try:
            all_pools = await self.fetch_all_pools()
        except Exception as e:
            logger.error(f"Failed to fetch pools: {e}")
            return []

        logger.info(f"Fetched {len(all_pools)} total DLMM pools")

        # Basic filter pass
        filtered = [p for p in all_pools if self._passes_basic_filters(p)]
        logger.info(f"After basic filters: {len(filtered)} pools")

        # Sort by volume desc, take top N for detailed enrichment
        filtered.sort(key=lambda p: float(p.get("trade_volume_24h", 0)), reverse=True)
        top_pools = filtered[: self.cfg.max_watch_pools]

        # Enrich and create candidates
        candidates = []
        for pool in top_pools:
            try:
                candidate = await self._enrich_pool(pool)
                if candidate and self._passes_detailed_filters(candidate):
                    candidate.score = self._score_pool(candidate)
                    candidates.append(candidate)
            except Exception as e:
                logger.debug(f"Failed to enrich pool {pool.get('address', '?')}: {e}")

        # Sort by score
        candidates.sort(key=lambda c: c.score, reverse=True)

        logger.info(
            f"Found {len(candidates)} viable candidates. "
            f"Top: {candidates[0].name if candidates else 'none'} "
            f"(score={candidates[0].score if candidates else 0})"
        )

        return candidates

    async def _enrich_pool(self, pool: Dict[str, Any]) -> Optional[PoolCandidate]:
        """Enrich raw pool data with additional metrics."""
        address = pool.get("address", "")
        mint_x = pool.get("mint_x", "")
        mint_y = pool.get("mint_y", "")
        name = pool.get("name", f"{mint_x[:8]}...")

        pool_age = self._calculate_pool_age(pool)

        # Get DEXScreener data for volume/holder enrichment
        dex_data = await self.fetch_dexscreener_data(mint_x)

        volume_5m = 0.0
        volume_1h = 0.0
        holders = 0

        if dex_data:
            vol = dex_data.get("volume", {})
            volume_5m = float(vol.get("m5", 0) or 0)
            volume_1h = float(vol.get("h1", 0) or 0)
            # DEXScreener doesn't always have holders; estimate from txns
            txns = dex_data.get("txns", {})
            h1_txns = txns.get("h1", {})
            # Rough holder estimate from unique buyers
            holders = int(h1_txns.get("buys", 0)) + int(h1_txns.get("sells", 0))

        # Fee calculation (approximate from pool data)
        base_fee_pct = float(pool.get("base_fee_percentage", "0") or "0")
        base_fee_bps = int(base_fee_pct * 100)  # convert % to bps

        # Fee earned in 5min (from volume)
        fee_rate_5m = volume_5m * (base_fee_pct / 100)

        return PoolCandidate(
            address=address,
            name=name,
            mint_x=mint_x,
            mint_y=mint_y,
            bin_step=int(pool.get("bin_step", 0)),
            base_fee_bps=base_fee_bps,
            current_price=float(pool.get("current_price", 0) or 0),
            liquidity_usd=float(pool.get("liquidity", 0) or 0),
            volume_5m_usd=volume_5m,
            volume_1h_usd=volume_1h,
            fee_rate_5m_usd=fee_rate_5m,
            holders=holders,
            pool_age_seconds=pool_age,
            active_bin_id=int(pool.get("active_id", 0) or 0),
            created_at=int(pool.get("created_at", 0) or 0),
        )

    def _passes_detailed_filters(self, c: PoolCandidate) -> bool:
        """Detailed filter on enriched candidate."""
        if c.volume_5m_usd < self.cfg.min_volume_5m_usd:
            return False
        if c.volume_1h_usd < self.cfg.min_volume_1h_usd:
            return False
        if c.holders < self.cfg.min_holders:
            return False
        if c.base_fee_bps < self.cfg.min_fee_rate_bps:
            return False
        if c.base_fee_bps > self.cfg.max_fee_rate_bps:
            return False
        if c.pool_age_seconds < self.cfg.min_pool_age_seconds:
            return False
        return True
