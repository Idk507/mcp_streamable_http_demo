
---
<img width="928" height="894" alt="image" src="https://github.com/user-attachments/assets/f36e927e-bf21-4142-922a-40ae63f87c30" />


**`01_core_server.py` — The MCP server foundation**

This is where your server lives. It registers tools using a decorator-based `ToolRegistry` that auto-tracks call counts, error rates, and applies per-tool timeouts. Three production tools are fully implemented: `search_documents` (with filters and pagination), `execute_query` (SQL with injection prevention — blocks DROP, DELETE, etc.), and `send_notification` (Slack/email/PagerDuty). A `ResourceProvider` exposes live health checks and tool schemas as MCP resources. Prompt templates for `analyze_query_results` and `incident_response` are included. JSON structured logging wraps everything.

**`02_http_transport.py` — Network-ready HTTP + SSE transport**

When you need multi-client, cloud-hosted MCP (not just stdio), this is your layer. Built on FastAPI with JWT auth middleware, tenant context isolation (each request carries a `TenantContext` with permissions and tier), and an `SSEConnectionManager` for push events. Includes security headers middleware (HSTS, XSS protection), a `/token` endpoint, the main `/mcp` JSON-RPC endpoint, and `/mcp/events` for SSE subscriptions. Graceful shutdown with proper keepalive cleanup.

**`03_client.py` — Production client with resilience**

The hardest part of MCP in production is the client. This implements a full `CircuitBreaker` (CLOSED → OPEN → HALF_OPEN state machine), exponential backoff retry with jitter, TTL response caching for idempotent tools, and `call_tools_concurrent()` for bounded parallel execution via `asyncio.Semaphore`. The SSE `subscribe_events()` is an async generator that yields events as they arrive. HTTP/2 and connection pooling are configured on the `httpx.AsyncClient`.

**`04_advanced_concepts.py` — Eight advanced MCP patterns**

The middleware pipeline runs pre/post hooks around every tool call, in order forward and reversed on the way back — just like Python's `contextmanager` stacking. `SamplingAPI` shows how a server can request LLM reasoning from the client mid-tool. `ToolChain` enables multi-step orchestration with conditional step skipping and context passing between steps. Pydantic models with validators enforce structured output contracts. OpenTelemetry spans wrap tool execution. The `RootsManager` handles workspace filesystem exposure. `ElicitationRequest` allows tools to ask users for more input mid-execution. `DynamicToolLoader` loads tools from config at runtime without a restart.

**`05_challenges.py` — What breaks in production and how to fix it**

Protocol version negotiation handles old clients gracefully. `ToolVersionRegistry` manages schema evolution with argument migration (old `{"q":"...", "max":10}` → new `{"query":"...", "limit":10}`). `MCPError` classifies all errors into typed codes with recovery strategies (rate-limited → exponential backoff, resource not found → cache fallback, circuit open → degrade gracefully). `ConnectionManager` handles reconnection with pending request cleanup. `SecretManager` abstracts env vars / AWS Secrets Manager / Vault behind a cached interface. The test suite covers circuit breaker state machine, middleware execution order, SQL injection, rate limiting, argument migration, and tool chain conditions.

The `requirements.txt` has everything needed to run it: `pip install -r requirements.txt`.
