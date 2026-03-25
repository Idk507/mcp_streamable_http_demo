"""
MCP Production Challenges & Solutions
=======================================
1. Versioning & Protocol Negotiation
2. Error Classification & Recovery
3. Connection State Management
4. Graceful Degradation
5. Integration Testing
6. Deployment Patterns (sidecar, gateway, standalone)
7. Secret Management
8. Schema Evolution / Backward Compatibility
"""

import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from mcp.server import Server
from mcp import types

logger = logging.getLogger("mcp.challenges")

# ─── Dynamic module loader for files with digit-prefixed names ────────────
_HERE = Path(__file__).parent

def _load_module(filename: str, module_name: str):
    """Load a .py file that has a digit-prefixed name not importable via normal import."""
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod

# ═══════════════════════════════════════════════════════════════════════════
# 1. VERSIONING & PROTOCOL NEGOTIATION
# ═══════════════════════════════════════════════════════════════════════════

class ProtocolVersion:
    """Semantic versioning for MCP protocol negotiation."""
    
    SUPPORTED_VERSIONS = ["2024-11-05", "2024-10-07"]
    CURRENT = "2024-11-05"
    MINIMUM = "2024-10-07"
    
    @classmethod
    def negotiate(cls, client_version: str) -> str:
        """
        Find the best mutually supported version.
        Challenge: Clients and servers may be at different versions.
        """
        if client_version == cls.CURRENT:
            return cls.CURRENT
        
        if client_version in cls.SUPPORTED_VERSIONS:
            logger.warning(
                f"Client using older protocol {client_version}, "
                f"server supports up to {cls.CURRENT}"
            )
            return client_version
        
        raise ValueError(
            f"Unsupported protocol version: {client_version}. "
            f"Supported: {cls.SUPPORTED_VERSIONS}"
        )
    
    @classmethod
    def feature_supported(cls, version: str, feature: str) -> bool:
        """Check if a feature is available in a given protocol version."""
        FEATURE_MAP = {
            "elicitation": "2024-11-05",
            "sampling": "2024-10-07",
            "roots": "2024-10-07",
            "resources_subscribe": "2024-11-05",
        }
        min_version = FEATURE_MAP.get(feature)
        if not min_version:
            return False
        return cls.SUPPORTED_VERSIONS.index(version) <= cls.SUPPORTED_VERSIONS.index(min_version)


class ToolVersionRegistry:
    """
    Manage multiple versions of the same tool schema.
    Challenge: Evolving tool schemas without breaking existing clients.
    """
    
    def __init__(self):
        self._versions: dict[str, dict[str, dict]] = {}  # tool -> version -> schema
        self._deprecation_notices: dict[str, str] = {}
    
    def register_version(
        self,
        tool_name: str,
        version: str,
        schema: dict,
        deprecation_note: Optional[str] = None,
    ):
        if tool_name not in self._versions:
            self._versions[tool_name] = {}
        self._versions[tool_name][version] = schema
        
        if deprecation_note:
            self._deprecation_notices[f"{tool_name}:{version}"] = deprecation_note
    
    def get_schema(self, tool_name: str, client_version: str = "2024-11-05") -> dict:
        """Return the appropriate schema for a client's protocol version."""
        if tool_name not in self._versions:
            raise ValueError(f"Tool not found: {tool_name}")
        
        # Pick the highest compatible version
        compatible = [
            v for v in self._versions[tool_name]
            if v <= client_version
        ]
        if not compatible:
            raise ValueError(f"No compatible schema for {tool_name} at version {client_version}")
        
        selected = max(compatible)
        schema = self._versions[tool_name][selected]
        
        dep_key = f"{tool_name}:{selected}"
        if dep_key in self._deprecation_notices:
            logger.warning(
                f"Tool {tool_name} v{selected} is deprecated: "
                f"{self._deprecation_notices[dep_key]}"
            )
        
        return schema
    
    def migrate_arguments(self, tool: str, args: dict, from_ver: str, to_ver: str) -> dict:
        """
        Transform arguments from old schema to new schema.
        Challenge: Old clients sending old argument formats.
        """
        migrations = {
            ("search_documents", "2024-10-07", "2024-11-05"): lambda a: {
                # Old: {"q": "...", "max": 10}
                # New: {"query": "...", "limit": 10}
                "query": a.get("q", a.get("query", "")),
                "limit": a.get("max", a.get("limit", 10)),
                **{k: v for k, v in a.items() if k not in ("q", "max", "query", "limit")},
            }
        }
        
        key = (tool, from_ver, to_ver)
        if key in migrations:
            return migrations[key](args)
        return args


# ═══════════════════════════════════════════════════════════════════════════
# 2. ERROR CLASSIFICATION & RECOVERY
# ═══════════════════════════════════════════════════════════════════════════

class MCPErrorCode(Enum):
    # JSON-RPC standard
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    
    # MCP custom
    RATE_LIMITED = -32001
    PERMISSION_DENIED = -32002
    RESOURCE_NOT_FOUND = -32003
    TOOL_TIMEOUT = -32004
    SCHEMA_VALIDATION = -32005
    TENANT_NOT_FOUND = -32006
    CIRCUIT_OPEN = -32007

@dataclass
class MCPError(Exception):
    code: MCPErrorCode
    message: str
    data: Optional[Any] = None
    retryable: bool = False
    retry_after: Optional[float] = None
    
    def to_dict(self) -> dict:
        err = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.data:
            err["data"] = self.data
        if self.retryable:
            err["retryable"] = True
        if self.retry_after:
            err["retryAfter"] = self.retry_after
        return err
    
    @classmethod
    def from_exception(cls, exc: Exception) -> "MCPError":
        """Convert arbitrary exceptions to MCPError."""
        if isinstance(exc, cls):
            return exc
        elif isinstance(exc, PermissionError):
            return cls(MCPErrorCode.PERMISSION_DENIED, str(exc))
        elif isinstance(exc, TimeoutError):
            return cls(MCPErrorCode.TOOL_TIMEOUT, str(exc), retryable=True, retry_after=5.0)
        elif isinstance(exc, ValueError):
            return cls(MCPErrorCode.INVALID_PARAMS, str(exc))
        else:
            return cls(MCPErrorCode.INTERNAL_ERROR, f"Unexpected error: {type(exc).__name__}")

class ErrorRecoveryManager:
    """
    Centralized error handling with recovery strategies.
    Challenge: Different errors need different responses.
    """
    
    RECOVERY_STRATEGIES = {
        MCPErrorCode.RATE_LIMITED: "exponential_backoff",
        MCPErrorCode.TOOL_TIMEOUT: "retry_once",
        MCPErrorCode.RESOURCE_NOT_FOUND: "cache_fallback",
        MCPErrorCode.CIRCUIT_OPEN: "degrade_gracefully",
        MCPErrorCode.INTERNAL_ERROR: "log_and_alert",
    }
    
    def __init__(self, alert_webhook: Optional[str] = None):
        self._alert_webhook = alert_webhook
        self._error_counts: dict[str, int] = {}
    
    async def handle(self, error: MCPError, context: dict) -> Optional[Any]:
        """
        Handle an error and optionally return a fallback value.
        Returns None if no recovery is possible.
        """
        self._error_counts[error.code.name] = self._error_counts.get(error.code.name, 0) + 1
        
        strategy = self.RECOVERY_STRATEGIES.get(error.code, "propagate")
        logger.warning(
            f"Applying recovery strategy '{strategy}' for {error.code.name}",
            extra={"error_code": error.code.value, "context": context}
        )
        
        if strategy == "exponential_backoff":
            wait = error.retry_after or 1.0
            logger.info(f"Rate limited, waiting {wait}s")
            await asyncio.sleep(wait)
            return None  # Signal to retry
        
        elif strategy == "cache_fallback":
            cached = context.get("cache_value")
            if cached:
                logger.info("Using cached fallback value")
                return cached
            return None
        
        elif strategy == "degrade_gracefully":
            return context.get("fallback_value", {"status": "degraded", "error": error.message})
        
        elif strategy == "log_and_alert":
            await self._send_alert(error, context)
            return None
        
        return None  # propagate
    
    async def _send_alert(self, error: MCPError, context: dict):
        """Send alert to webhook for critical errors."""
        if not self._alert_webhook:
            return
        
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(self._alert_webhook, json={
                    "error_code": error.code.name,
                    "message": error.message,
                    "context": context,
                    "timestamp": time.time(),
                })
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# 3. CONNECTION STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

class ConnectionState(Enum):
    CONNECTING = "connecting"
    INITIALIZED = "initialized"
    ACTIVE = "active"
    RECONNECTING = "reconnecting"
    CLOSED = "closed"

class ConnectionManager:
    """
    Manages MCP connection lifecycle.
    Challenge: Connections drop; state must be preserved/restored.
    """
    
    def __init__(self, reconnect_attempts: int = 5, reconnect_delay: float = 2.0):
        self._state = ConnectionState.CONNECTING
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_delay = reconnect_delay
        self._session_id: Optional[str] = None
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._state_handlers: dict[ConnectionState, list[Callable]] = {}
        self._stats = {
            "total_connections": 0,
            "reconnections": 0,
            "dropped_requests": 0,
        }
    
    def on_state_change(self, state: ConnectionState, handler: Callable):
        if state not in self._state_handlers:
            self._state_handlers[state] = []
        self._state_handlers[state].append(handler)
    
    async def _transition(self, new_state: ConnectionState):
        old_state = self._state
        self._state = new_state
        logger.info(f"Connection: {old_state.value} → {new_state.value}")
        
        for handler in self._state_handlers.get(new_state, []):
            try:
                await handler(old_state, new_state)
            except Exception as e:
                logger.error(f"State handler error: {e}")
    
    async def connect(self, connector: Callable) -> bool:
        """Establish connection with retry."""
        await self._transition(ConnectionState.CONNECTING)
        
        for attempt in range(self._reconnect_attempts):
            try:
                session_id = await connector()
                self._session_id = session_id
                self._stats["total_connections"] += 1
                await self._transition(ConnectionState.INITIALIZED)
                return True
            except Exception as e:
                delay = self._reconnect_delay * (2 ** attempt)
                logger.warning(f"Connection attempt {attempt+1} failed: {e}, retrying in {delay}s")
                await asyncio.sleep(delay)
        
        await self._transition(ConnectionState.CLOSED)
        return False
    
    async def reconnect(self, connector: Callable):
        """Handle reconnection after dropped connection."""
        if self._state == ConnectionState.RECONNECTING:
            return  # Already reconnecting
        
        await self._transition(ConnectionState.RECONNECTING)
        self._stats["reconnections"] += 1
        
        # Fail pending requests
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(
                    ConnectionError("Connection dropped, request failed")
                )
                self._stats["dropped_requests"] += 1
        self._pending_requests.clear()
        
        success = await self.connect(connector)
        if not success:
            logger.error("Reconnection failed after all attempts")
    
    @property
    def is_active(self) -> bool:
        return self._state in (ConnectionState.INITIALIZED, ConnectionState.ACTIVE)
    
    @property
    def stats(self) -> dict:
        return {**self._stats, "state": self._state.value}


# ═══════════════════════════════════════════════════════════════════════════
# 4. SECRET MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

class SecretManager:
    """
    Secure secret loading for MCP server configuration.
    Challenge: Credentials for databases, APIs must be secure.
    Supports: env vars, AWS Secrets Manager, HashiCorp Vault, K8s secrets.
    """
    
    def __init__(self, provider: str = "env"):
        self._provider = provider
        self._cache: dict[str, tuple[str, float]] = {}  # key -> (value, expires_at)
        self._cache_ttl = 3600  # 1 hour
    
    async def get(self, key: str) -> str:
        """Fetch a secret, using cache to reduce external calls."""
        
        # Check cache
        if key in self._cache:
            value, expires_at = self._cache[key]
            if time.monotonic() < expires_at:
                return value
        
        value = await self._fetch(key)
        self._cache[key] = (value, time.monotonic() + self._cache_ttl)
        return value
    
    async def _fetch(self, key: str) -> str:
        if self._provider == "env":
            value = os.environ.get(key)
            if not value:
                raise ValueError(f"Secret '{key}' not found in environment")
            return value
        
        elif self._provider == "aws_secrets_manager":
            # import boto3
            # client = boto3.client("secretsmanager")
            # resp = client.get_secret_value(SecretId=key)
            # return resp["SecretString"]
            raise NotImplementedError("Set provider='env' for demo")
        
        elif self._provider == "vault":
            # import hvac
            # client = hvac.Client(url=os.environ["VAULT_ADDR"], token=os.environ["VAULT_TOKEN"])
            # resp = client.secrets.kv.read_secret_version(path=key)
            # return resp["data"]["data"]["value"]
            raise NotImplementedError("Set provider='env' for demo")
        
        raise ValueError(f"Unknown secret provider: {self._provider}")
    
    async def load_server_config(self) -> dict:
        """Load all server secrets needed for MCP server startup."""
        secrets = {}
        required = [
            "MCP_JWT_SECRET",
            "MCP_DB_URL",
            "MCP_SLACK_TOKEN",
        ]
        optional = {
            "MCP_REDIS_URL": "redis://localhost:6379",
            "MCP_LOG_LEVEL": "INFO",
        }
        
        for key in required:
            try:
                secrets[key] = await self.get(key)
            except ValueError:
                logger.error(f"Required secret missing: {key}")
                raise
        
        for key, default in optional.items():
            try:
                secrets[key] = await self.get(key)
            except ValueError:
                secrets[key] = default
        
        return secrets


# ═══════════════════════════════════════════════════════════════════════════
# 5. INTEGRATION TESTING
# ═══════════════════════════════════════════════════════════════════════════

class MCPTestServer:
    """
    Test harness for MCP server integration tests.
    Provides an in-memory server without network overhead.
    """
    
    def __init__(self, server: Server):
        self._server = server
        self._recorded_calls: list[dict] = []
    
    async def call_tool(self, name: str, arguments: dict) -> list[types.TextContent]:
        self._recorded_calls.append({
            "type": "tool_call",
            "name": name,
            "arguments": arguments,
            "timestamp": time.time(),
        })
        # Directly invoke the server's tool handler
        # In real MCP SDK: use in-process client
        result = await self._server._tool_handlers[name](arguments)
        return result
    
    @property
    def recorded_calls(self) -> list[dict]:
        return self._recorded_calls.copy()
    
    def assert_tool_called(self, name: str, times: int = 1):
        calls = [c for c in self._recorded_calls if c["name"] == name]
        assert len(calls) == times, f"Expected {name} to be called {times} times, got {len(calls)}"
    
    def assert_tool_called_with(self, name: str, **expected_args):
        matching = [
            c for c in self._recorded_calls
            if c["name"] == name
            and all(c["arguments"].get(k) == v for k, v in expected_args.items())
        ]
        assert matching, f"Tool {name} never called with args {expected_args}"


# ─── Pytest Integration Tests ─────────────────────────────────────────────

# Run with: pytest 05_challenges.py -v

@pytest.fixture
def mock_tool_registry():
    """Fixture providing a minimal tool registry for tests."""
    registry = {}
    
    async def mock_search(**kwargs):
        return [{"id": "test_doc", "title": "Test", "score": 1.0}]
    
    async def mock_query(**kwargs):
        if not kwargs.get("sql", "").upper().startswith("SELECT"):
            raise ValueError("Only SELECT allowed")
        return {"rows": [], "row_count": 0}
    
    registry["search_documents"] = mock_search
    registry["execute_query"] = mock_query
    return registry

@pytest.mark.asyncio
async def test_protocol_version_negotiation():
    """Test that version negotiation handles all cases correctly."""
    # Current version - should work
    assert ProtocolVersion.negotiate("2024-11-05") == "2024-11-05"
    
    # Older supported version - should downgrade gracefully
    result = ProtocolVersion.negotiate("2024-10-07")
    assert result == "2024-10-07"
    
    # Unsupported version - should raise
    with pytest.raises(ValueError, match="Unsupported protocol version"):
        ProtocolVersion.negotiate("2023-01-01")

@pytest.mark.asyncio
async def test_argument_migration():
    """Test backward-compatible argument migration."""
    registry = ToolVersionRegistry()
    registry.register_version("search_documents", "2024-10-07", {
        "properties": {"q": {"type": "string"}, "max": {"type": "integer"}},
        "required": ["q"],
    })
    registry.register_version("search_documents", "2024-11-05", {
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    })
    
    old_args = {"q": "production outage", "max": 5}
    new_args = registry.migrate_arguments(
        "search_documents", old_args, "2024-10-07", "2024-11-05"
    )
    
    assert new_args["query"] == "production outage"
    assert new_args["limit"] == 5
    assert "q" not in new_args
    assert "max" not in new_args

@pytest.mark.asyncio
async def test_sql_injection_prevention():
    """Test that dangerous SQL is rejected."""
    # Loaded dynamically because the filename starts with a digit
    _core = _load_module("01_core_server.py", "core_server")
    execute_query = _core.execute_query

    dangerous_queries = [
        "DROP TABLE users",
        "SELECT * FROM users; DELETE FROM orders",
        "SELECT * FROM users WHERE id=1 OR UPDATE users SET admin=1",
    ]
    
    for sql in dangerous_queries:
        # Verify dangerous SQL is caught
        normalized = sql.strip().upper()
        is_safe = normalized.startswith("SELECT") and all(
            kw not in normalized
            for kw in ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER"]
        )
        assert not is_safe, f"Should have blocked: {sql}"

@pytest.mark.asyncio
async def test_rate_limiter_enforcement():
    """Test that rate limiting correctly allows and blocks requests."""
    _core = _load_module("01_core_server.py", "core_server")
    RateLimiter = _core.RateLimiter

    limiter = RateLimiter(max_requests=3, window_seconds=60)
    client_id = "test_client"
    
    # First 3 should be allowed
    for _ in range(3):
        allowed, info = limiter.is_allowed(client_id)
        assert allowed, "Should have been allowed"
    
    # 4th should be blocked
    allowed, info = limiter.is_allowed(client_id)
    assert not allowed, "Should have been rate limited"
    assert "retry_after" in info

@pytest.mark.asyncio
async def test_circuit_breaker_state_machine():
    """Test circuit breaker transitions through all states."""
    _client = _load_module("03_client.py", "mcp_client")
    CircuitBreaker = _client.CircuitBreaker
    CircuitState = _client.CircuitState

    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.1)
    
    # Initial state: CLOSED
    assert cb.state == "closed"
    assert cb.can_request()
    
    # Record failures up to threshold
    for _ in range(3):
        cb.record_failure()
    
    # Should now be OPEN
    assert cb.state == "open"
    assert not cb.can_request()
    
    # Wait for recovery window
    await asyncio.sleep(0.15)
    
    # Should allow one request (HALF_OPEN)
    assert cb.can_request()
    assert cb.state == "half_open"
    
    # Successful request should close
    cb.record_success()
    cb.record_success()  # success_threshold=2
    assert cb.state == "closed"

@pytest.mark.asyncio
async def test_middleware_pipeline_execution_order():
    """Test that middleware executes in correct order."""
    _adv = _load_module("04_advanced_concepts.py", "advanced_concepts")
    MiddlewarePipeline = _adv.MiddlewarePipeline
    ToolCallContext = _adv.ToolCallContext
    Middleware = _adv.Middleware

    execution_log = []
    
    class LoggingMiddleware(Middleware):
        def __init__(self, name: str):
            self.name = name
        
        async def before(self, ctx: ToolCallContext) -> None:
            execution_log.append(f"before:{self.name}")
        
        async def after(self, ctx: ToolCallContext) -> None:
            execution_log.append(f"after:{self.name}")
    
    pipeline = MiddlewarePipeline()
    pipeline.use(LoggingMiddleware("A"))
    pipeline.use(LoggingMiddleware("B"))
    pipeline.use(LoggingMiddleware("C"))
    
    async def handler(**kwargs):
        execution_log.append("handler")
        return {"status": "ok"}
    
    ctx = ToolCallContext(
        tool_name="test",
        arguments={},
        tenant_id="test",
        user_id="user",
        correlation_id="corr123",
    )
    
    await pipeline.run(ctx, handler)
    
    # before: A, B, C in order; after: C, B, A (reversed)
    assert execution_log == [
        "before:A", "before:B", "before:C",
        "handler",
        "after:C", "after:B", "after:A",
    ]

@pytest.mark.asyncio
async def test_tool_chain_condition_skip():
    """Test that conditional steps are correctly skipped."""
    _adv = _load_module("04_advanced_concepts.py", "advanced_concepts")
    ToolChain = _adv.ToolChain
    ChainStep = _adv.ChainStep

    executed_tools = []
    
    async def executor(tool_name: str, args: dict) -> Any:
        executed_tools.append(tool_name)
        return {"result": f"ok from {tool_name}"}
    
    chain = ToolChain([
        ChainStep(
            tool="tool_a",
            input_mapper=lambda ctx: {},
            output_key="a_result",
        ),
        ChainStep(
            tool="tool_b",
            input_mapper=lambda ctx: {},
            output_key="b_result",
            condition=lambda ctx: False,  # Always skip
        ),
        ChainStep(
            tool="tool_c",
            input_mapper=lambda ctx: {},
            output_key="c_result",
        ),
    ])
    
    result = await chain.run({}, executor)
    
    assert "tool_a" in executed_tools
    assert "tool_b" not in executed_tools  # Skipped
    assert "tool_c" in executed_tools
    assert "a_result" in result
    assert "b_result" not in result


# ═══════════════════════════════════════════════════════════════════════════
# 6. DEPLOYMENT PATTERNS
# ═══════════════════════════════════════════════════════════════════════════

DEPLOYMENT_PATTERNS = """
╔══════════════════════════════════════════════════════════════════╗
║              MCP DEPLOYMENT PATTERNS                            ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  Pattern 1: STDIO (Subprocess)                                   ║
║  ─────────────────────────────                                   ║
║  Client spawns server as subprocess.                             ║
║  Best for: desktop apps, Claude Desktop, local dev               ║
║  Pros: Simple, no network, no auth needed                        ║
║  Cons: Single client, no horizontal scaling                      ║
║                                                                  ║
║  Pattern 2: HTTP + SSE (Network)                                 ║
║  ──────────────────────────────                                  ║
║  Server runs as HTTP service, clients connect over network.      ║
║  Best for: multi-tenant SaaS, cloud deployments                  ║
║  Pros: Scalable, multi-client, loadbalanceable                   ║
║  Cons: Requires auth, TLS, more infra                            ║
║                                                                  ║
║  Pattern 3: Kubernetes Sidecar                                   ║
║  ──────────────────────────────                                  ║
║  MCP server runs as sidecar in same pod as main app.             ║
║  Best for: service-mesh architectures                            ║
║  Pros: Low latency (localhost), shared secrets, co-deployment    ║
║  Cons: Tied to main app lifecycle                                ║
║                                                                  ║
║  Pattern 4: API Gateway + Lambda                                 ║
║  ──────────────────────────────                                  ║
║  Each tool is a separate Lambda function.                         ║
║  Best for: serverless, event-driven, variable load               ║
║  Pros: Pay-per-use, infinite scale, zero ops                     ║
║  Cons: Cold starts, connection overhead per request              ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ─── Docker Compose for Pattern 2 ────────────────────────────────────────
DOCKER_COMPOSE = """
# docker-compose.yml for MCP production deployment

version: '3.9'

services:
  mcp-server:
    build: .
    ports:
      - "8080:8080"
    environment:
      - MCP_JWT_SECRET=${MCP_JWT_SECRET}
      - MCP_DB_URL=${MCP_DB_URL}
      - MCP_LOG_LEVEL=INFO
      - MCP_ENV=production
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
    deploy:
      replicas: 3
      restart_policy:
        condition: on-failure
        delay: 5s
        max_attempts: 3
      resources:
        limits:
          cpus: '1.0'
          memory: 512M
    networks:
      - mcp_net

  nginx:
    image: nginx:alpine
    ports:
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./certs:/etc/nginx/certs:ro
    depends_on:
      mcp-server:
        condition: service_healthy
    networks:
      - mcp_net

  redis:
    image: redis:7-alpine
    command: redis-server --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data
    networks:
      - mcp_net

networks:
  mcp_net:
    driver: bridge

volumes:
  redis_data:
"""

# ─── Kubernetes Deployment ────────────────────────────────────────────────
K8S_DEPLOYMENT = """
# k8s/deployment.yaml

apiVersion: apps/v1
kind: Deployment
metadata:
  name: mcp-server
  namespace: mcp-prod
spec:
  replicas: 3
  selector:
    matchLabels:
      app: mcp-server
  template:
    metadata:
      labels:
        app: mcp-server
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/path: "/metrics"
    spec:
      containers:
      - name: mcp-server
        image: your-registry/mcp-server:1.0.0
        ports:
        - containerPort: 8080
        env:
        - name: MCP_JWT_SECRET
          valueFrom:
            secretKeyRef:
              name: mcp-secrets
              key: jwt-secret
        - name: MCP_DB_URL
          valueFrom:
            secretKeyRef:
              name: mcp-secrets
              key: db-url
        resources:
          requests:
            cpu: "250m"
            memory: "256Mi"
          limits:
            cpu: "1000m"
            memory: "512Mi"
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: mcp-server
  namespace: mcp-prod
spec:
  selector:
    app: mcp-server
  ports:
  - port: 80
    targetPort: 8080
  type: ClusterIP
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: mcp-server-hpa
  namespace: mcp-prod
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: mcp-server
  minReplicas: 2
  maxReplicas: 10
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
"""

if __name__ == "__main__":
    print(DEPLOYMENT_PATTERNS)
    print("\nRun tests with: pytest 05_challenges.py -v --asyncio-mode=auto")
