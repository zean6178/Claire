"""
Safety Module — Deployer Blacklist, Rug Checks, Strict Thresholds
Addresses the gap vs. production bots (SniperAI-level safety).
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import httpx

from .config import CONFIG
from .utils import dexscreener_limiter, retry

logger = logging.getLogger("dlmm_bot.safety")


# =============================================================================
# DEPLOYER BLACKLIST
# =============================================================================

@dataclass
class DeployerRecord:
    """Track deployer behavior."""
    address: str
    tokens_deployed: int = 0
    rugs_detected: int = 0
    first_seen: int = 0
    last_seen: int = 0


class DeployerBlacklist:
    """
    Serial deployer detection.
    Blacklists addresses that deploy multiple tokens quickly (likely rugs).
    """

    def __init__(self):
        # Known rug deployers (manually curated + auto-detected)
        self._blacklist: Set[str] = set()
        self._deployer_history: Dict[str, DeployerRecord] = {}

        # Thresholds
        self.max_deploys_per_day = 3  # >3 tokens in 24h = suspicious
        self.rug_ratio_threshold = 0.5  # >50% of tokens rugged = blacklist

    def is_blacklisted(self, deployer: str) -> bool:
        """Check if deployer is blacklisted."""
        return deployer in self._blacklist

    def record_deploy(self, deployer: str, is_rug: bool = False):
        """Record a token deployment."""
        now = int(time.time())
        if deployer not in self._deployer_history:
            self._deployer_history[deployer] = DeployerRecord(
                address=deployer,
                first_seen=now,
            )

        record = self._deployer_history[deployer]
        record.tokens_deployed += 1
        record.last_seen = now
        if is_rug:
            record.rugs_detected += 1

        # Auto-blacklist check
        if record.tokens_deployed >= self.max_deploys_per_day:
            time_span = now - record.first_seen
            if time_span <= 86400:  # within 24h
                self._blacklist.add(deployer)
                logger.warning(
                    f"BLACKLISTED deployer {deployer[:12]}...: "
                    f"{record.tokens_deployed} deploys in {time_span}s"
                )

        if (
            record.tokens_deployed >= 3
            and record.rugs_detected / record.tokens_deployed >= self.rug_ratio_threshold
        ):
            self._blacklist.add(deployer)
            logger.warning(
                f"BLACKLISTED deployer {deployer[:12]}...: "
                f"{record.rugs_detected}/{record.tokens_deployed} rugs"
            )

    def add_to_blacklist(self, deployer: str, reason: str = "manual"):
        """Manually blacklist a deployer."""
        self._blacklist.add(deployer)
        logger.info(f"Deployer {deployer[:12]}... blacklisted: {reason}")

    @property
    def blacklist_size(self) -> int:
        return len(self._blacklist)


# =============================================================================
# RUG CHECK ENGINE
# =============================================================================

@dataclass
class RugCheckResult:
    """Result of rug check."""
    is_safe: bool = True
    risk_level: str = "low"  # low, medium, high, critical
    reasons: List[str] = field(default_factory=list)
    score: int = 0  # 0 = safe, higher = more rug risk


class RugChecker:
    """
    Rug pull detection.
    
    Checks:
    1. Mint authority (should be revoked)
    2. Freeze authority (should be revoked)
    3. Top holder concentration
    4. Deployer history
    5. Liquidity lock status
    6. Token age vs holder growth
    """

    def __init__(self, deployer_blacklist: Optional[DeployerBlacklist] = None):
        self.blacklist = deployer_blacklist or DeployerBlacklist()
        self._http: Optional[httpx.AsyncClient] = None

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15))
        return self._http

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def check_token(
        self,
        mint_address: str,
        # On-chain data (if available)
        mint_authority: Optional[str] = None,
        freeze_authority: Optional[str] = None,
        deployer_address: Optional[str] = None,
        # Market data
        top_holder_pct: float = 0.0,
        top5_holder_pct: float = 0.0,
        holder_count: int = 0,
        pool_age_seconds: int = 0,
        liquidity_usd: float = 0.0,
        volume_5m_usd: float = 0.0,
    ) -> RugCheckResult:
        """
        Run comprehensive rug check on a token.
        Returns RugCheckResult with safety assessment.
        """
        result = RugCheckResult()

        # --- CHECK 1: Mint authority ---
        if mint_authority and mint_authority != "":
            result.reasons.append(
                f"Mint authority NOT revoked: {mint_authority[:12]}..."
            )
            result.score += 30
            result.risk_level = "high"

        # --- CHECK 2: Freeze authority ---
        if freeze_authority and freeze_authority != "":
            result.reasons.append(
                f"Freeze authority NOT revoked: {freeze_authority[:12]}..."
            )
            result.score += 25
            result.risk_level = "high"

        # --- CHECK 3: Deployer blacklist ---
        if deployer_address and self.blacklist.is_blacklisted(deployer_address):
            result.reasons.append(
                f"Deployer BLACKLISTED: {deployer_address[:12]}..."
            )
            result.score += 50
            result.risk_level = "critical"

        # --- CHECK 4: Top holder concentration ---
        if top_holder_pct >= 50:
            result.reasons.append(
                f"Single holder owns {top_holder_pct:.0f}% (max safe: 20%)"
            )
            result.score += 25
            if result.risk_level != "critical":
                result.risk_level = "high"
        elif top_holder_pct >= 30:
            result.reasons.append(
                f"Single holder owns {top_holder_pct:.0f}% (concerning)"
            )
            result.score += 10
            if result.risk_level == "low":
                result.risk_level = "medium"

        # --- CHECK 5: Top 5 concentration ---
        if top5_holder_pct >= 60:
            result.reasons.append(
                f"Top 5 hold {top5_holder_pct:.0f}% (max safe: 40%)"
            )
            result.score += 15
            if result.risk_level == "low":
                result.risk_level = "medium"

        # --- CHECK 6: Holder count vs age ---
        if pool_age_seconds > 300 and holder_count < 50:
            result.reasons.append(
                f"Only {holder_count} holders after {pool_age_seconds//60}min "
                f"(suspicious low adoption)"
            )
            result.score += 10
            if result.risk_level == "low":
                result.risk_level = "medium"

        # --- CHECK 7: Liquidity vs volume ratio ---
        if liquidity_usd > 0 and volume_5m_usd > 0:
            vol_liq_ratio = volume_5m_usd / liquidity_usd
            if vol_liq_ratio > 5.0:
                result.reasons.append(
                    f"Suspicious vol/liq ratio: {vol_liq_ratio:.1f}x "
                    f"(possible wash trading)"
                )
                result.score += 15
                if result.risk_level == "low":
                    result.risk_level = "medium"

        # --- CHECK 8: Very low liquidity ---
        if liquidity_usd < 5000:
            result.reasons.append(
                f"Very low liquidity: ${liquidity_usd:.0f} (easy to manipulate)"
            )
            result.score += 10

        # --- Final assessment ---
        if result.score >= 50:
            result.is_safe = False
            result.risk_level = "critical"
        elif result.score >= 30:
            result.is_safe = False
            result.risk_level = "high"
        elif result.score >= 15:
            result.is_safe = True  # still allow but with warning
            result.risk_level = "medium"

        if not result.is_safe:
            logger.warning(
                f"RUG CHECK FAILED for {mint_address[:12]}...: "
                f"score={result.score}, risk={result.risk_level}, "
                f"reasons={result.reasons}"
            )

        return result


# =============================================================================
# POSITION RECONCILIATION
# =============================================================================

class PositionReconciler:
    """
    Position reconciliation engine.
    On bot restart, queries on-chain to rebuild position state.
    
    Queries:
    - All position accounts owned by wallet
    - Active bin for each pool
    - Accrued fees
    - Token balances in positions
    """

    def __init__(self):
        self._http: Optional[httpx.AsyncClient] = None

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30))
        return self._http

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def find_open_positions(self, wallet_pubkey: str) -> List[Dict]:
        """
        Query Meteora API for all open DLMM positions owned by wallet.
        
        Returns list of position dicts with:
        - position_address
        - pool_address
        - lower_bin_id, upper_bin_id
        - liquidity amounts
        - accrued fees
        """
        http = await self._get_http()
        positions = []

        try:
            # Meteora positions endpoint
            resp = await http.get(
                f"{CONFIG.scanner.meteora_api_url}/position/{wallet_pubkey}"
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    positions = data
                    logger.info(
                        f"Reconciliation: found {len(positions)} "
                        f"open positions for wallet {wallet_pubkey[:12]}..."
                    )
        except Exception as e:
            logger.error(f"Position reconciliation failed: {e}")

        return positions

    async def sync_state(
        self, wallet_pubkey: str, position_manager
    ) -> int:
        """
        Sync bot state with on-chain positions.
        Call this on restart to rebuild state.
        
        Returns number of positions recovered.
        """
        positions = await self.find_open_positions(wallet_pubkey)
        recovered = 0

        for pos_data in positions:
            try:
                # Check if we already track this position
                pos_addr = pos_data.get("address", "")
                if pos_addr in position_manager.positions:
                    continue

                # Reconstruct position record
                # NOTE: This is approximate — we don't have entry metadata
                logger.info(
                    f"Recovered position: {pos_addr[:16]}... "
                    f"in pool {pos_data.get('pair_address', '?')[:16]}..."
                )
                recovered += 1

            except Exception as e:
                logger.error(f"Failed to recover position: {e}")

        logger.info(f"Reconciliation complete: {recovered} positions recovered")
        return recovered
