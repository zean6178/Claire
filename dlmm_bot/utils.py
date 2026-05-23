"""
Utility functions for the DLMM bot.
Logging, Solana transaction building, retry logic, helpers.
"""

import asyncio
import json
import logging
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional

import base58
import httpx
from solders.keypair import Keypair  # type: ignore
from solders.pubkey import Pubkey  # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed

from .config import CONFIG

logger = logging.getLogger("dlmm_bot")


def setup_logging():
    """Configure logging for the bot."""
    logging.basicConfig(
        level=getattr(logging, CONFIG.log_level),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reduce noise from httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_keypair() -> Keypair:
    """Load wallet keypair from file."""
    key_path = Path(CONFIG.wallet.private_key_path)
    if not key_path.exists():
        raise FileNotFoundError(f"Wallet key file not found: {key_path}")

    with open(key_path, "r") as f:
        key_data = json.load(f)

    if isinstance(key_data, list):
        # Standard Solana CLI format: array of bytes
        return Keypair.from_bytes(bytes(key_data))
    elif isinstance(key_data, str):
        # Base58 encoded private key
        return Keypair.from_bytes(base58.b58decode(key_data))
    else:
        raise ValueError("Unsupported key format")


def get_rpc_client() -> AsyncClient:
    """Create an async Solana RPC client."""
    return AsyncClient(CONFIG.rpc.endpoint, commitment=Confirmed)


def retry(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Retry decorator with exponential backoff."""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries} failed for {func.__name__}: {e}. "
                            f"Retrying in {current_delay:.1f}s..."
                        )
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logger.error(f"All {max_retries} attempts failed for {func.__name__}: {e}")
            raise last_exception
        return wrapper
    return decorator


async def get_http_client() -> httpx.AsyncClient:
    """Create a shared HTTP client."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(CONFIG.rpc.timeout),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


def lamports_to_sol(lamports: int) -> float:
    """Convert lamports to SOL."""
    return lamports / 1_000_000_000


def sol_to_lamports(sol: float) -> int:
    """Convert SOL to lamports."""
    return int(sol * 1_000_000_000)


def now_ts() -> int:
    """Current timestamp in seconds."""
    return int(time.time())


def pubkey(address: str) -> Pubkey:
    """Create a Pubkey from string."""
    return Pubkey.from_string(address)


class RateLimiter:
    """Rate limiter with 429 backoff support."""

    def __init__(self, calls_per_second: float = 5.0):
        self.min_interval = 1.0 / calls_per_second
        self.last_call = 0.0
        self._backoff_until = 0.0  # timestamp when backoff expires
        self._consecutive_429s = 0

    async def wait(self):
        """Wait if needed to respect rate limit + backoff."""
        now = time.time()

        # If in backoff period, wait longer
        if now < self._backoff_until:
            wait_time = self._backoff_until - now
            logger.debug(f"Rate limiter in backoff, waiting {wait_time:.1f}s")
            await asyncio.sleep(wait_time)
            now = time.time()

        # Normal rate limiting
        elapsed = now - self.last_call
        if elapsed < self.min_interval:
            await asyncio.sleep(self.min_interval - elapsed)
        self.last_call = time.time()

    def report_429(self):
        """Called when API returns 429. Increases backoff exponentially."""
        self._consecutive_429s += 1
        # Exponential backoff: 2s, 4s, 8s, 16s, max 60s
        backoff_seconds = min(60.0, 2.0 ** self._consecutive_429s)
        self._backoff_until = time.time() + backoff_seconds
        logger.warning(
            f"429 received (#{self._consecutive_429s}). "
            f"Backing off {backoff_seconds:.0f}s"
        )

    def report_success(self):
        """Called on successful request. Resets 429 counter."""
        if self._consecutive_429s > 0:
            self._consecutive_429s = 0
            logger.debug("Rate limiter: backoff reset (success)")


# Shared rate limiters
meteora_limiter = RateLimiter(calls_per_second=3.0)
dexscreener_limiter = RateLimiter(calls_per_second=2.0)
jupiter_limiter = RateLimiter(calls_per_second=5.0)
