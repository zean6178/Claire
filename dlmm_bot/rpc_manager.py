"""
RPC Failover Manager
Supports multiple RPC endpoints with automatic failover.
Tracks health, latency, and rotates on failure.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed

from .config import CONFIG

logger = logging.getLogger("dlmm_bot.rpc_manager")


@dataclass
class RpcEndpoint:
    """Single RPC endpoint with health tracking."""
    url: str
    name: str = "unknown"
    ws_url: str = ""
    is_healthy: bool = True
    last_error_time: float = 0.0
    error_count: int = 0
    avg_latency_ms: float = 0.0
    total_requests: int = 0
    # Cooldown: don't retry failed endpoint for this many seconds
    cooldown_seconds: float = 30.0

    @property
    def is_available(self) -> bool:
        """Check if endpoint is available (healthy or cooldown expired)."""
        if self.is_healthy:
            return True
        return time.time() - self.last_error_time > self.cooldown_seconds


class RpcManager:
    """
    Manages multiple RPC endpoints with automatic failover.
    
    Features:
    - Round-robin with health checks
    - Auto-failover on error
    - Latency tracking
    - Cooldown for failed endpoints
    - Auto-recover when endpoint comes back
    
    Setup:
        Set multiple RPCs via env vars:
        SOLANA_RPC_URL=https://primary.rpc.com
        SOLANA_RPC_BACKUP_1=https://backup1.rpc.com
        SOLANA_RPC_BACKUP_2=https://backup2.rpc.com
    """

    def __init__(self):
        self.endpoints: List[RpcEndpoint] = []
        self._current_index: int = 0
        self._clients: dict = {}  # url -> AsyncClient
        self._setup_endpoints()

    def _setup_endpoints(self):
        """Load endpoints from environment."""
        # Primary
        primary_url = CONFIG.rpc.endpoint
        primary_ws = CONFIG.rpc.ws_endpoint
        self.endpoints.append(RpcEndpoint(
            url=primary_url,
            name="primary",
            ws_url=primary_ws,
        ))

        # Backup endpoints from env
        for i in range(1, 5):
            backup_url = os.getenv(f"SOLANA_RPC_BACKUP_{i}", "")
            if backup_url:
                self.endpoints.append(RpcEndpoint(
                    url=backup_url,
                    name=f"backup_{i}",
                    ws_url=backup_url.replace("https://", "wss://").replace("http://", "ws://"),
                ))

        # Always add public as last resort
        public = "https://api.mainnet-beta.solana.com"
        if not any(e.url == public for e in self.endpoints):
            self.endpoints.append(RpcEndpoint(
                url=public,
                name="public_fallback",
                ws_url="wss://api.mainnet-beta.solana.com",
                cooldown_seconds=60.0,  # longer cooldown for public
            ))

        logger.info(
            f"RPC Manager initialized with {len(self.endpoints)} endpoints: "
            f"{[e.name for e in self.endpoints]}"
        )

    async def get_client(self) -> AsyncClient:
        """
        Get the best available RPC client.
        Tries current endpoint first, fails over to next healthy one.
        """
        for _ in range(len(self.endpoints)):
            endpoint = self.endpoints[self._current_index]
            if endpoint.is_available:
                return await self._get_or_create_client(endpoint)
            self._rotate()

        # All endpoints down — force use primary regardless
        logger.error("ALL RPC endpoints unavailable! Forcing primary.")
        return await self._get_or_create_client(self.endpoints[0])

    async def _get_or_create_client(self, endpoint: RpcEndpoint) -> AsyncClient:
        """Get or create AsyncClient for endpoint."""
        if endpoint.url not in self._clients:
            self._clients[endpoint.url] = AsyncClient(
                endpoint.url, commitment=Confirmed
            )
        return self._clients[endpoint.url]

    def _rotate(self):
        """Move to next endpoint."""
        self._current_index = (self._current_index + 1) % len(self.endpoints)

    def report_success(self, latency_ms: float = 0.0):
        """Report successful request to current endpoint."""
        endpoint = self.endpoints[self._current_index]
        endpoint.is_healthy = True
        endpoint.total_requests += 1
        if latency_ms > 0:
            # Running average
            endpoint.avg_latency_ms = (
                endpoint.avg_latency_ms * 0.9 + latency_ms * 0.1
            )

    def report_error(self, error: Exception):
        """Report failed request — may trigger failover."""
        endpoint = self.endpoints[self._current_index]
        endpoint.error_count += 1
        endpoint.last_error_time = time.time()

        # Mark unhealthy after 3 consecutive errors
        if endpoint.error_count >= 3:
            endpoint.is_healthy = False
            logger.warning(
                f"RPC endpoint [{endpoint.name}] marked UNHEALTHY "
                f"({endpoint.error_count} errors). Failing over..."
            )
            self._rotate()
            # Reset error count for next time
            endpoint.error_count = 0

    async def execute_with_failover(self, method: str, *args, **kwargs):
        """
        Execute an RPC method with automatic failover.
        Tries up to len(endpoints) times.
        """
        last_error = None
        for attempt in range(len(self.endpoints)):
            client = await self.get_client()
            start = time.time()
            try:
                func = getattr(client, method)
                result = await func(*args, **kwargs)
                latency = (time.time() - start) * 1000
                self.report_success(latency)
                return result
            except Exception as e:
                last_error = e
                self.report_error(e)
                logger.warning(
                    f"RPC call {method} failed on "
                    f"[{self.endpoints[self._current_index].name}]: {e}"
                )

        raise last_error or Exception("All RPC endpoints failed")

    async def get_balance(self, pubkey) -> int:
        """Get SOL balance with failover."""
        result = await self.execute_with_failover("get_balance", pubkey)
        return result.value

    async def close_all(self):
        """Close all RPC clients."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    def get_health_report(self) -> str:
        """Get formatted health report."""
        lines = ["RPC Health:"]
        for i, ep in enumerate(self.endpoints):
            status = "OK" if ep.is_healthy else "DOWN"
            marker = " <--" if i == self._current_index else ""
            lines.append(
                f"  [{ep.name}] {status} | "
                f"latency={ep.avg_latency_ms:.0f}ms | "
                f"errors={ep.error_count} | "
                f"reqs={ep.total_requests}{marker}"
            )
        return "\n".join(lines)


# Global singleton
rpc_manager = RpcManager()
