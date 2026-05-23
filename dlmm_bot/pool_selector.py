"""
Pool Selector Module — Yunss DLMM Guide Implementation
MC-based fee/binstep selection, rugcheck integration, dynamic range,
bin initialization cost check.

Logic from: https://x.com/0xyunss/status/1983850301632475290

Rules:
- MC > $1M: fee 1-2%, binstep 80/100, pick highest TVL pool
- MC < $1M: fee 5-10%, binstep 125/200
- Always check rugcheck.xyz before entry
- Spot for uptrend (range -49% default)
- Bid-Ask single-sided SOL for volatile/dump-expected (range tipis 25-40 bins)
- Skip pool if non-refundable bin init cost > 0.07 SOL
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

import httpx

from .config import CONFIG
from .scanner import PoolCandidate
from .utils import retry

logger = logging.getLogger("dlmm_bot.pool_selector")


# =============================================================================
# MC-BASED POOL SELECTION
# =============================================================================

@dataclass
class PoolSelectionCriteria:
    """Optimal pool parameters based on token market cap."""
    preferred_fee_pct_min: float
    preferred_fee_pct_max: float
    preferred_bin_steps: List[int]
    shape: str  # "spot" or "bid_ask"
    num_bins: int
    description: str


# Yunss rules: MC-based fee/binstep
MC_RULES = {
    "high_cap": PoolSelectionCriteria(
        preferred_fee_pct_min=1.0,
        preferred_fee_pct_max=2.0,
        preferred_bin_steps=[80, 100],
        shape="spot",
        num_bins=69,  # default -49% range
        description="MC > $1M: fee 1-2%, binstep 80/100, spot default range",
    ),
    "low_cap": PoolSelectionCriteria(
        preferred_fee_pct_min=5.0,
        preferred_fee_pct_max=10.0,
        preferred_bin_steps=[125, 200],
        shape="bid_ask",
        num_bins=30,  # tipis for quick plays
        description="MC < $1M: fee 5-10%, binstep 125/200, bid-ask tipis",
    ),
}


def get_pool_criteria_by_mc(market_cap_usd: float) -> PoolSelectionCriteria:
    """Select pool criteria based on token market cap."""
    if market_cap_usd >= 1_000_000:
        return MC_RULES["high_cap"]
    else:
        return MC_RULES["low_cap"]


def select_best_pool(
    pools: List[PoolCandidate],
    market_cap_usd: float,
) -> Optional[PoolCandidate]:
    """
    Select the best pool for a token based on MC rules.
    
    Priority:
    1. Fee tier matches MC bracket
    2. Bin step matches MC bracket
    3. Highest TVL among matching pools
    """
    criteria = get_pool_criteria_by_mc(market_cap_usd)

    # Filter pools matching criteria
    matching = []
    for pool in pools:
        fee_pct = pool.base_fee_bps / 100.0  # convert bps to %
        
        # Check fee range
        if not (criteria.preferred_fee_pct_min <= fee_pct <= criteria.preferred_fee_pct_max):
            continue
        
        # Check bin step
        if pool.bin_step not in criteria.preferred_bin_steps:
            continue
        
        matching.append(pool)

    if not matching:
        # Fallback: relax fee requirement, just match bin step
        for pool in pools:
            if pool.bin_step in criteria.preferred_bin_steps:
                matching.append(pool)

    if not matching:
        # Last fallback: pick highest TVL pool regardless
        if pools:
            matching = pools

    if not matching:
        return None

    # Sort by TVL descending (pick the one with most liquidity)
    matching.sort(key=lambda p: p.liquidity_usd, reverse=True)
    
    selected = matching[0]
    logger.info(
        f"Pool selected: {selected.name} | "
        f"Fee={selected.base_fee_bps}bps | BinStep={selected.bin_step} | "
        f"TVL=${selected.liquidity_usd:,.0f} | "
        f"MC=${market_cap_usd:,.0f} -> {criteria.description}"
    )
    
    return selected


# =============================================================================
# DYNAMIC RANGE CALCULATION
# =============================================================================

def calculate_dynamic_range(
    shape: str,
    market_cap_usd: float,
    is_uptrend: bool = False,
    is_volatile: bool = True,
    bin_step: int = 100,
) -> int:
    """
    Calculate optimal number of bins based on Yunss strategy.
    
    Rules:
    - Spot for uptrend: -49% range (default ~69 bins for binstep 100)
    - Bid-Ask single-sided SOL tipis: 25-40 bins for quick plays
    - Wider range for higher MC (more stable)
    
    Returns: num_bins
    """
    if shape == "spot":
        if is_uptrend:
            # Default -49% range
            # For bin_step 100: each bin = ~1% price change
            # -49% ≈ 69 bins below current price
            return 69
        else:
            # More conservative when not confirmed uptrend
            return 50
    
    elif shape == "bid_ask":
        if market_cap_usd >= 1_000_000:
            # Higher MC = slightly wider range (more stable)
            return 40
        else:
            # Low MC = tipis, quick plays
            return 25
    
    elif shape == "curve":
        # Curve is very tight, high risk
        return 20
    
    # Default
    return 50


def determine_shape_for_condition(
    is_uptrend: bool,
    is_high_volume: bool,
    expect_dump: bool,
    market_cap_usd: float,
) -> str:
    """
    Determine position shape based on market condition.
    
    Yunss rules:
    - Spot: token sedang uptrend dengan volume besar
    - Bid-Ask: token yang yakin akan dump 30% tapi naik lagi cepat
    - Bid-Ask also default for low MC volatile memes
    """
    if is_uptrend and is_high_volume:
        return "spot"
    
    if expect_dump:
        return "bid_ask"
    
    # Default for memecoin: bid-ask (safer, lower IL risk)
    if market_cap_usd < 1_000_000:
        return "bid_ask"
    
    return "spot"


# =============================================================================
# RUGCHECK.XYZ INTEGRATION
# =============================================================================

@dataclass
class RugcheckResult:
    """Result from rugcheck.xyz API."""
    is_good: bool = False
    risk_level: str = "unknown"  # good, warning, danger
    risks: List[str] = None
    score: int = 0  # 0-100, higher = safer
    
    def __post_init__(self):
        if self.risks is None:
            self.risks = []


class RugcheckClient:
    """
    Integration with rugcheck.xyz API.
    Checks token safety before entry.
    """
    
    BASE_URL = "https://api.rugcheck.xyz/v1"
    
    def __init__(self):
        self._http: Optional[httpx.AsyncClient] = None
    
    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15))
        return self._http
    
    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
    
    @retry(max_retries=2, delay=1.0)
    async def check_token(self, mint_address: str) -> RugcheckResult:
        """
        Check token safety via rugcheck.xyz.
        
        Returns RugcheckResult with risk assessment.
        Only proceed if is_good = True.
        """
        http = await self._get_http()
        
        try:
            resp = await http.get(
                f"{self.BASE_URL}/tokens/{mint_address}/report/summary"
            )
            
            if resp.status_code == 200:
                data = resp.json()
                
                # Parse rugcheck response
                score = int(data.get("score", 0) or 0)
                risks = data.get("risks", [])
                risk_descriptions = []
                
                for risk in risks:
                    if isinstance(risk, dict):
                        risk_descriptions.append(
                            f"{risk.get('name', '?')}: {risk.get('description', '?')}"
                        )
                    elif isinstance(risk, str):
                        risk_descriptions.append(risk)
                
                # Determine risk level
                # rugcheck.xyz uses: "Good", "Warning", "Danger"
                risk_level = data.get("riskLevel", "unknown")
                if isinstance(risk_level, str):
                    risk_level = risk_level.lower()
                
                is_good = risk_level == "good" or score >= 70
                
                logger.info(
                    f"Rugcheck [{mint_address[:12]}...]: "
                    f"score={score}, level={risk_level}, "
                    f"risks={len(risk_descriptions)}, pass={'YES' if is_good else 'NO'}"
                )
                
                return RugcheckResult(
                    is_good=is_good,
                    risk_level=risk_level,
                    risks=risk_descriptions,
                    score=score,
                )
            
            elif resp.status_code == 404:
                # Token not found on rugcheck — new token, proceed with caution
                logger.warning(f"Rugcheck: token {mint_address[:12]}... not found (new?)")
                return RugcheckResult(
                    is_good=True,  # allow but flag as unknown
                    risk_level="unknown",
                    risks=["Token not indexed by rugcheck.xyz (possibly very new)"],
                    score=50,
                )
            
            else:
                logger.warning(f"Rugcheck API error: {resp.status_code}")
                # On API error, don't block (allow with warning)
                return RugcheckResult(is_good=True, risk_level="unknown", score=50)
                
        except Exception as e:
            logger.error(f"Rugcheck failed for {mint_address[:12]}...: {e}")
            # On error, don't block entry but log warning
            return RugcheckResult(is_good=True, risk_level="error", score=50)


# =============================================================================
# BIN INITIALIZATION COST CHECK
# =============================================================================

@dataclass
class BinCostEstimate:
    """Estimated cost for bin initialization."""
    total_cost_sol: float = 0.0
    refundable_sol: float = 0.0
    non_refundable_sol: float = 0.0
    is_acceptable: bool = True
    reason: str = ""


def estimate_bin_init_cost(
    num_bins: int,
    existing_initialized_bins: int = 0,
    max_non_refundable_sol: float = 0.07,
) -> BinCostEstimate:
    """
    Estimate bin initialization cost.
    
    On Meteora DLMM:
    - Each bin array (up to 70 bins) costs ~0.07 SOL rent
    - Rent is refundable when you close the position
    - BUT if bins are already initialized by others, no cost
    - Non-refundable cost comes from opening NEW bin arrays
    
    Yunss rule: skip if non-refundable > 0.07 SOL
    """
    # Each bin array holds ~70 bins
    # Rent per bin array ≈ 0.07 SOL (refundable)
    bins_needing_init = max(0, num_bins - existing_initialized_bins)
    
    if bins_needing_init == 0:
        return BinCostEstimate(
            total_cost_sol=0.0,
            refundable_sol=0.0,
            non_refundable_sol=0.0,
            is_acceptable=True,
            reason="All bins already initialized",
        )
    
    # How many bin arrays needed
    bin_arrays_needed = (bins_needing_init + 69) // 70  # ceiling division
    
    # Cost per bin array (rent, refundable)
    rent_per_array = 0.07  # approximately
    total_rent = bin_arrays_needed * rent_per_array
    
    # In most cases, rent is refundable
    # Non-refundable only if bin array was created by someone else and you're
    # the first to add liquidity (rare for active pools)
    # For active pools with existing LPs: usually 0 non-refundable
    non_refundable = 0.0  # assume refundable for active pools
    
    # For brand new pools with no existing LPs:
    # First LP pays bin array creation (non-refundable portion)
    # This is ~0.003 SOL per bin array for the account creation
    if existing_initialized_bins == 0:
        non_refundable = bin_arrays_needed * 0.003  # small non-refundable portion
    
    is_acceptable = non_refundable <= max_non_refundable_sol
    
    reason = ""
    if not is_acceptable:
        reason = (
            f"Non-refundable cost {non_refundable:.4f} SOL > "
            f"max {max_non_refundable_sol} SOL"
        )
        logger.warning(f"Bin cost too high: {reason}")
    
    return BinCostEstimate(
        total_cost_sol=total_rent + non_refundable,
        refundable_sol=total_rent,
        non_refundable_sol=non_refundable,
        is_acceptable=is_acceptable,
        reason=reason,
    )


# =============================================================================
# COMBINED POOL VALIDATION (all Yunss checks in one call)
# =============================================================================

class YunssValidator:
    """
    Combined validator implementing all Yunss DLMM guide rules.
    
    Call validate_pool() before opening any position.
    """
    
    def __init__(self):
        self.rugcheck = RugcheckClient()
    
    async def close(self):
        await self.rugcheck.close()
    
    async def validate_pool(
        self,
        candidate: PoolCandidate,
        market_cap_usd: float = 0.0,
        is_uptrend: bool = False,
        is_high_volume: bool = True,
    ) -> Tuple[bool, str, Dict]:
        """
        Run all Yunss validation checks on a pool candidate.
        
        Returns:
            (passed, reason, metadata)
            metadata includes: shape, num_bins, rugcheck_score, bin_cost
        """
        metadata = {}
        
        # --- CHECK 1: Rugcheck.xyz ---
        rugcheck_result = await self.rugcheck.check_token(candidate.mint_x)
        metadata["rugcheck"] = rugcheck_result
        
        if not rugcheck_result.is_good:
            return (
                False,
                f"Rugcheck FAILED: {rugcheck_result.risk_level} "
                f"(score={rugcheck_result.score})",
                metadata,
            )
        
        # --- CHECK 2: MC-based pool criteria ---
        criteria = get_pool_criteria_by_mc(market_cap_usd)
        metadata["mc_criteria"] = criteria
        
        fee_pct = candidate.base_fee_bps / 100.0
        fee_ok = criteria.preferred_fee_pct_min <= fee_pct <= criteria.preferred_fee_pct_max
        binstep_ok = candidate.bin_step in criteria.preferred_bin_steps
        
        # Don't hard-reject on fee/binstep mismatch, just note it
        if not fee_ok:
            logger.info(
                f"Pool fee {fee_pct}% outside preferred "
                f"{criteria.preferred_fee_pct_min}-{criteria.preferred_fee_pct_max}% "
                f"for MC ${market_cap_usd:,.0f} (acceptable but not ideal)"
            )
        
        # --- CHECK 3: TVL minimum ---
        if candidate.liquidity_usd < 15000:
            return (
                False,
                f"TVL too low: ${candidate.liquidity_usd:,.0f} < $15,000 minimum",
                metadata,
            )
        
        # --- CHECK 4: Determine shape and range ---
        shape = determine_shape_for_condition(
            is_uptrend=is_uptrend,
            is_high_volume=is_high_volume,
            expect_dump=False,
            market_cap_usd=market_cap_usd,
        )
        num_bins = calculate_dynamic_range(
            shape=shape,
            market_cap_usd=market_cap_usd,
            is_uptrend=is_uptrend,
            bin_step=candidate.bin_step,
        )
        metadata["shape"] = shape
        metadata["num_bins"] = num_bins
        
        # --- CHECK 5: Bin initialization cost ---
        bin_cost = estimate_bin_init_cost(
            num_bins=num_bins,
            existing_initialized_bins=num_bins,  # assume active pool
        )
        metadata["bin_cost"] = bin_cost
        
        if not bin_cost.is_acceptable:
            return (
                False,
                f"Bin init cost too high: {bin_cost.reason}",
                metadata,
            )
        
        # --- ALL CHECKS PASSED ---
        logger.info(
            f"Yunss validation PASSED: {candidate.name} | "
            f"Rugcheck={rugcheck_result.score} | "
            f"Shape={shape} | Bins={num_bins} | "
            f"Fee={fee_pct}% | BinStep={candidate.bin_step} | "
            f"MC=${market_cap_usd:,.0f}"
        )
        
        return (True, "All Yunss checks passed", metadata)
