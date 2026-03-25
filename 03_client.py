"""
MCP Production Client
======================
Production-grade client with:
- Connection pooling
- Exponential backoff retry
- Circuit breaker pattern
- Streaming / SSE support
- Context propagation
- Comprehensive error handling
- Response caching (with TTL)
- Concurrent tool calls
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional

import httpx
from cachetools import TTLCache

logger = logging.getLogger("mcp.client")

# ─── Circuit Breaker ──────────────────────────────────────────────────────

class CircuitState(Enum):
    CLOSED = "closed"      # Normal: requests flow through
    OPEN = "open"          # Failing: requests blocked immediately
    HALF_OPEN = "half_open"  # Testing: one request allowed through

@dataclass
class CircuitBreaker:
    """
    Prevents cascading failures by short-circuiting calls to a failing service.
    
    State machine:
      CLOSED → OPEN (after failure_threshold failures in rolling window)
      OPEN → HALF_OPEN (after recovery_timeout seconds)
      HALF_OPEN → CLOSED (on success) | OPEN (on failure)
    """
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    success_threshold: int = 2  # Successes needed in HALF_OPEN to close
    window_seconds: float = 60.0
    
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _failures: list[float] = field(default_factory=list, init=False)
    _last_failure: Optional[float] = field(default=None, init=False)
    _consecutive_successes: int = field(default=0, init=False)
    _total_calls: int = field(default=0, init=False)
    _total_blocked: int = field(default=0, init=False)

    def record_success(self):
        self._total_calls += 1
        if self._state == CircuitState.HALF_OPEN:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failures.clear()
                self._consecutive_successes = 0
                logger.info("Circuit breaker CLOSED (recovered)")
    
    def record_failure(self):
        self._total_calls += 1
        now = time.monotonic()
        
        # Evict old failures
        self._failures = [t for t in self._failures if now - t < self.window_seconds]
        self._failures.append(now)
        self._last_failure = now
        self._consecutive_successes = 0
        
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            logger.warning("Circuit breaker OPEN (failed in half-open)")
        elif len(self._failures) >= self.failure_threshold:
            self._state = CircuitState.OPEN
            logger.error(
                f"Circuit breaker OPEN ({self.failure_threshold} failures in {self.window_seconds}s)"
            )
    
    def can_request(self) -> bool:
        if self._state == CircuitState.CLOSED:
            return True
        
        if self._state == CircuitState.OPEN:
            if self._last_failure and (time.monotonic() - self._last_failure) > self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._consecutive_successes = 0
                logger.info("Circuit breaker HALF_OPEN (trying recovery)")
                return True
            self._total_blocked += 1
            return False
        
        # HALF_OPEN: allow one request
        return True
    
    @property
    def state(self) -> str:
        return self._state.value
    
    @property
    def stats(self) -> dict:
        return {
            "state": self.state,
            "total_calls": self._total_calls,
            "total_blocked": self._total_blocked,
            "recent_failures": len(self._failures),
        }


# ─── Retry Logic ──────────────────────────────────────────────────────────

@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay: float = 0.5       # seconds
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True           # Avoid thundering herd
    retryable_status_codes: tuple = (429, 500, 502, 503, 504)
    retryable_exceptions: tuple = (
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.RemoteProtocolError,
    )

    def delay_for_attempt(self, attempt: int) -> float:
        delay = min(
            self.base_delay * (self.exponential_base ** attempt),
            self.max_delay
        )
        if self.jitter:
            import random
            delay *= (0.5 + random.random() * 0.5)
        return delay
    
    def should_retry(self, status_code: Optional[int], exc: Optional[Exception]) -> bool:
        if exc is not None:
            return isinstance(exc, self.retryable_exceptions)
        if status_code is not None:
            return status_code in self.retryable_status_codes
        return False


# ─── Response Cache ───────────────────────────────────────────────────────

class ResponseCache:
    """
    TTL-based cache for idempotent tool calls.
    Only safe to cache read-only tools.
    """
    CACHEABLE_TOOLS = {"search_documents", "execute_query"}
    
    def __init__(self, maxsize: int = 256, ttl: int = 300):
        self._cache = TTLCache(maxsize=maxsize, ttl=ttl)
    
    def _key(self, tool: str, args: dict) -> str:
        payload = json.dumps({"tool": tool, "args": args}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()
    
    def get(self, tool: str, args: dict) -> Optional[Any]:
        if tool not in self.CACHEABLE_TOOLS:
            return None
        return self._cache.get(self._key(tool, args))
    
    def set(self, tool: str, args: dict, value: Any):
        if tool not in self.CACHEABLE_TOOLS:
            return
        self._cache[self._key(tool, args)] = value
    
    @property
    def info(self) -> dict:
        return {
            "size": len(self._cache),
            "maxsize": self._cache.maxsize,
            "ttl": self._cache.ttl,
        }


# ─── MCP Client ───────────────────────────────────────────────────────────

class MCPClient:
    """
    Production MCP client with connection pooling, retry, circuit breaker.
    
    Usage:
        async with MCPClient(base_url="http://localhost:8080", token="...") as client:
            result = await client.call_tool("search_documents", {"query": "outage"})
            tools = await client.list_tools()
    """
    
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        retry_config: Optional[RetryConfig] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        enable_cache: bool = True,
        correlation_id: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retry_config = retry_config or RetryConfig()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.cache = ResponseCache() if enable_cache else None
        self.correlation_id = correlation_id or str(__import__("uuid").uuid4())
        self._request_id = 0
        self._http: Optional[httpx.AsyncClient] = None
        self._initialized = False
    
    async def __aenter__(self) -> "MCPClient":
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-Correlation-ID": self.correlation_id,
            },
            timeout=httpx.Timeout(
                connect=5.0,
                read=self.timeout,
                write=5.0,
                pool=10.0,
            ),
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=60.0,
            ),
            http2=True,  # Use HTTP/2 if available
        )
        await self._initialize()
        return self
    
    async def __aexit__(self, *args):
        if self._http:
            await self._http.aclose()
    
    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id
    
    async def _initialize(self):
        """Send MCP initialize handshake."""
        result = await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "production-mcp-client", "version": "1.0.0"},
            "capabilities": {
                "sampling": {},
                "roots": {"listChanged": True},
            },
        })
        logger.info(
            "MCP client initialized",
            extra={"server_info": result.get("serverInfo", {})}
        )
        self._initialized = True
    
    async def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        """Execute a JSON-RPC request with retry and circuit breaker."""
        if not self.circuit_breaker.can_request():
            raise RuntimeError(
                f"Circuit breaker OPEN — service unavailable. "
                f"Stats: {self.circuit_breaker.stats}"
            )
        
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        
        last_exc = None
        for attempt in range(self.retry_config.max_attempts):
            try:
                response = await self._http.post("/mcp", json=payload)
                
                if self.retry_config.should_retry(response.status_code, None):
                    delay = self.retry_config.delay_for_attempt(attempt)
                    logger.warning(
                        f"Retrying {method} (attempt {attempt+1}, status {response.status_code})",
                        extra={"delay": delay}
                    )
                    await asyncio.sleep(delay)
                    continue
                
                response.raise_for_status()
                data = response.json()
                
                if "error" in data and data["error"] is not None:
                    raise RuntimeError(f"MCP error: {data['error']}")
                
                self.circuit_breaker.record_success()
                return data.get("result", {})
            
            except httpx.HTTPStatusError as e:
                last_exc = e
                if not self.retry_config.should_retry(e.response.status_code, None):
                    self.circuit_breaker.record_failure()
                    raise
                
            except Exception as e:
                last_exc = e
                if not self.retry_config.should_retry(None, e):
                    self.circuit_breaker.record_failure()
                    raise
                
                delay = self.retry_config.delay_for_attempt(attempt)
                logger.warning(
                    f"Retrying {method} after error (attempt {attempt+1}): {e}",
                    extra={"delay": delay}
                )
                await asyncio.sleep(delay)
        
        self.circuit_breaker.record_failure()
        raise RuntimeError(
            f"All {self.retry_config.max_attempts} attempts failed for {method}"
        ) from last_exc
    
    # ─── Public API ───────────────────────────────────────────────────────

    async def list_tools(self) -> list[dict]:
        result = await self._rpc("tools/list")
        return result.get("tools", [])
    
    async def call_tool(self, name: str, arguments: dict) -> Any:
        """Call a tool with caching for idempotent tools."""
        
        # Try cache first
        if self.cache:
            cached = self.cache.get(name, arguments)
            if cached is not None:
                logger.debug(f"Cache hit for {name}")
                return cached
        
        start = time.monotonic()
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        
        logger.info(
            "Tool called",
            extra={"tool": name, "duration_ms": elapsed_ms, "cached": False}
        )
        
        # Parse content from MCP response
        content = result.get("content", [])
        parsed = []
        for item in content:
            if item.get("type") == "text":
                try:
                    parsed.append(json.loads(item["text"]))
                except json.JSONDecodeError:
                    parsed.append(item["text"])
        
        output = parsed[0] if len(parsed) == 1 else parsed
        
        # Store in cache
        if self.cache:
            self.cache.set(name, arguments, output)
        
        return output
    
    async def call_tools_concurrent(
        self,
        calls: list[tuple[str, dict]],
        max_concurrency: int = 5,
    ) -> list[Any]:
        """
        Execute multiple tool calls concurrently with a semaphore cap.
        
        Args:
            calls: List of (tool_name, arguments) tuples
            max_concurrency: Max parallel calls
        
        Returns:
            List of results in same order as calls
        """
        semaphore = asyncio.Semaphore(max_concurrency)
        
        async def bounded_call(name: str, args: dict) -> Any:
            async with semaphore:
                return await self.call_tool(name, args)
        
        results = await asyncio.gather(
            *[bounded_call(name, args) for name, args in calls],
            return_exceptions=True,
        )
        
        # Surface individual errors but don't fail all
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    f"Concurrent tool call failed",
                    extra={"tool": calls[i][0], "error": str(result)}
                )
        
        return list(results)
    
    async def list_resources(self) -> list[dict]:
        result = await self._rpc("resources/list")
        return result.get("resources", [])
    
    async def read_resource(self, uri: str) -> str:
        result = await self._rpc("resources/read", {"uri": uri})
        contents = result.get("contents", [])
        if contents:
            return contents[0].get("text", "")
        return ""
    
    async def list_prompts(self) -> list[dict]:
        result = await self._rpc("prompts/list")
        return result.get("prompts", [])
    
    async def get_prompt(self, name: str, arguments: Optional[dict] = None) -> dict:
        return await self._rpc("prompts/get", {"name": name, "arguments": arguments or {}})
    
    async def subscribe_events(self) -> AsyncIterator[dict]:
        """
        Subscribe to server-sent events from the MCP server.
        Yields events as they arrive.
        """
        url = f"{self.base_url}/mcp/events"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "text/event-stream",
            "X-Correlation-ID": self.correlation_id,
        }
        
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            event = json.loads(line[6:])
                            yield event
                        except json.JSONDecodeError:
                            logger.warning(f"Malformed SSE data: {line}")
                    # Comments ": keepalive" are skipped
    
    @property
    def stats(self) -> dict:
        return {
            "circuit_breaker": self.circuit_breaker.stats,
            "cache": self.cache.info if self.cache else None,
            "correlation_id": self.correlation_id,
            "initialized": self._initialized,
        }


# ─── Convenience Factory ──────────────────────────────────────────────────

async def get_token(base_url: str, tenant_id: str, user_id: str) -> str:
    """Obtain a JWT token from the MCP server."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base_url}/token",
            json={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "permissions": ["tools:read", "tools:execute"],
                "tier": "pro",
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


# ─── Usage Example ────────────────────────────────────────────────────────

async def demo_client():
    """
    Demonstrates full client lifecycle:
    1. Auth
    2. Tool discovery
    3. Single tool call (with caching)
    4. Concurrent tool calls
    5. Resource reading
    6. SSE subscription (first 3 events)
    """
    base_url = "http://localhost:8080"
    
    # 1. Get token
    token = await get_token(base_url, "acme-corp", "alice@acme.com")
    
    async with MCPClient(
        base_url=base_url,
        token=token,
        retry_config=RetryConfig(max_attempts=3, base_delay=0.5),
        circuit_breaker=CircuitBreaker(failure_threshold=5, recovery_timeout=60),
    ) as client:
        
        # 2. Discover tools
        tools = await client.list_tools()
        logger.info(f"Available tools: {[t['name'] for t in tools]}")
        
        # 3. Single tool call
        result = await client.call_tool("search_documents", {
            "query": "production outage",
            "collection": "runbooks",
            "limit": 5,
        })
        logger.info(f"Search result: {json.dumps(result, indent=2)}")
        
        # Cache hit on same call
        result_cached = await client.call_tool("search_documents", {
            "query": "production outage",
            "collection": "runbooks",
            "limit": 5,
        })
        
        # 4. Concurrent tool calls
        concurrent_results = await client.call_tools_concurrent([
            ("search_documents", {"query": "deployment", "collection": "all"}),
            ("execute_query", {"sql": "SELECT * FROM metrics LIMIT 10"}),
            ("search_documents", {"query": "incident", "collection": "tickets"}),
        ], max_concurrency=3)
        
        for i, r in enumerate(concurrent_results):
            if isinstance(r, Exception):
                logger.error(f"Call {i} failed: {r}")
            else:
                logger.info(f"Call {i} succeeded")
        
        # 5. Read resource
        server_info = await client.read_resource("config://server/info")
        logger.info(f"Server info: {server_info}")
        
        # 6. Subscribe to events (collect 3 then stop)
        event_count = 0
        async for event in client.subscribe_events():
            logger.info(f"SSE event: {event}")
            event_count += 1
            if event_count >= 3:
                break
        
        logger.info(f"Client stats: {json.dumps(client.stats, indent=2)}")


if __name__ == "__main__":
    asyncio.run(demo_client())
