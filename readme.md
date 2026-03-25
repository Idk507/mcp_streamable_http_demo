# MCP Production Demo — Complete Guide

This repository demonstrates a production-style MCP architecture with:
- a stdio MCP server,
- an HTTP/SSE transport,
- a resilient MCP client,
- advanced patterns (middleware, tracing, elicitation, tool chains),
- production challenge patterns and tests,
- and one unified runner (`main.py`).

---


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



## 1) Quick Start

### Prerequisites
- Python 3.11+ (project was validated on Python 3.13)
- `pip`

### Install dependencies
```bash
pip install -r requirements.txt
```

### Environment setup (`.env`)
```env
SLACK_API_TOKEN=your-slack-token
```

Notes:
- Slack notifications are implemented.
- Email channel is intentionally disabled in current implementation.

---

## 2) How to Run Everything (Unified Entrypoint)

Use `main.py` for all common tasks:

```bash
python main.py --help
python main.py all
python main.py check
python main.py http
python main.py core
python main.py client-demo
python main.py test
```

### Command behavior
- `all`: import-check all modules, check HTTP health (if server is running), run tests.
- `check`: import-check all numbered module files.
- `http`: run FastAPI HTTP+SSE server from `02_http_transport.py`.
- `core`: run stdio MCP server from `01_core_server.py`.
- `client-demo`: run demo client flow from `03_client.py` (requires HTTP server running).
- `test`: run `pytest 05_challenges.py -v --tb=short`.

---

## 3) High-Level Architecture

- `01_core_server.py`: MCP server logic (tools/resources/prompts) over stdio.
- `02_http_transport.py`: JSON-RPC over HTTP + SSE push transport, JWT auth, tenancy.
- `03_client.py`: robust MCP client (retry, circuit breaker, cache, concurrent calls).
- `04_advanced_concepts.py`: advanced implementation patterns for real-world systems.
- `05_challenges.py`: production challenge solutions + integration tests.
- `main.py`: unified operational CLI.

---

## 4) File-by-File, Function-by-Function Reference

## `01_core_server.py` — Core MCP Server (stdio transport)

### Purpose
Implements the main MCP server with tool registration, resources, prompts, logging, and safety controls.

### Classes and methods

#### `JSONFormatter`
- `format(record)`: emits structured JSON log lines with timestamp, level, message, module, function, and optional exception/extra fields.

Use case: machine-parsable logging for observability pipelines.

#### `RateLimiter`
- `is_allowed(client_id)`: token-bucket-like window check; returns `(allowed, metadata)` including remaining quota or retry-after.

Use case: defend expensive tools from abuse/spikes.

#### `ToolMetadata` (dataclass)
Holds per-tool contract: name, description, JSON schema, handler, timeout, tags, auth/rate options.

#### `ToolRegistry`
- `__init__()`: initializes tool registry and call/error metrics.
- `register(...)`: decorator factory that registers a tool and wraps it with timeout, call/error counting, and structured logs.
	- nested `decorator(func)`: binds metadata and returns wrapper.
	- nested `wrapper(*args, **kwargs)`: runtime execution wrapper with timeout/error handling.
- `get_mcp_tools()`: converts registry metadata to MCP `types.Tool` objects.
- `stats()`: returns per-tool call/error/error-rate summary.

Use case: standardized tool lifecycle with metrics + resilience.

#### `ResourceProvider`
- `get_resource(uri)`: serves virtual resources (`config://server/info`, `config://server/health`, `schema://tools`).
- `_run_health_checks()`: executes health probes (simulated DB + external HTTP check), includes tool stats.

Use case: expose internal server state to MCP clients as readable resources.

### Top-level tool functions

#### `setup_logging(level="INFO")`
Configures `mcp.server` logger to emit JSON logs.

#### `search_documents(query, collection="all", limit=10, filters=None)`
Mock full-text/semantic search returning ranked documents.

Use case: retrieval-style MCP tool.

#### `execute_query(sql, database="analytics", timeout=30, max_rows=1000)`
Read-only SQL execution with guardrails:
- allows only `SELECT`
- blocks dangerous SQL keywords (`DROP`, `DELETE`, etc.)

Use case: safe analytics query tool.

#### `send_notification(channel, destination, subject, body, priority="normal", metadata=None)`
Notification dispatch with channel-specific behavior:
- Slack channel sends `chat.postMessage` using `SLACK_API_TOKEN`.
- Email path intentionally raises `NotImplementedError`.
- PagerDuty/webhook are placeholder/no-op warning path.

Use case: alerting/incident communication tool.

#### `build_prompt_messages(name, args)`
Builds structured prompt messages for:
- `analyze_query_results`
- `incident_response`

Use case: server-provided reusable prompt templates.

#### `create_server()`
Constructs MCP `Server` and registers handlers:
- nested `list_tools()`
- nested `call_tool(name, arguments)`
- nested `list_resources()`
- nested `read_resource(uri)`
- nested `list_prompts()`
- nested `get_prompt(name, arguments)`

Use case: one place wiring all MCP capabilities.

#### `main()`
Starts stdio MCP server using `stdio_server()` context and initialization options.

---

## `02_http_transport.py` — HTTP + SSE MCP Transport

### Purpose
Wraps MCP capabilities behind FastAPI with JWT auth, tenancy context, JSON-RPC endpoint, and SSE events.

### Classes and methods

#### `ServerConfig` (dataclass)
Central server configuration (host/port/JWT/CORS/request limits/SSE timing).

#### `TenantContext` (dataclass)
- `can(permission)`: permission check (`admin` bypass).
- `rate_limit()`: tier-aware request limit (`free/pro/enterprise`).

Use case: per-tenant authorization and policy routing.

#### `MCPRequest` / `MCPResponse` (Pydantic models)
Typed JSON-RPC request/response models.

#### `SSEConnectionManager`
- `__init__()`: initializes per-tenant queue registry.
- `connect(tenant_id)`: allocates queue for one SSE client.
- `disconnect(tenant_id, queue)`: removes queue.
- `broadcast(tenant_id, event)`: fan-out event to one tenant.
- `broadcast_all(event)`: fan-out event to all tenants.
- `connection_count` (property): total active SSE streams.

Use case: scalable server-push notifications.

#### `ToolDispatcher`
- `dispatch(method, params, ctx)`: central JSON-RPC method router.
- `_handle_initialize(...)`: protocol handshake + capabilities.
- `_handle_list_tools(...)`: permission-filtered tool listing.
- `_handle_call_tool(...)`: permission checks, simulated execution, event broadcast.
- `_handle_list_resources(...)`: resource list response.
- `_handle_read_resource(...)`: resource read with tenant isolation.
- `_handle_list_prompts(...)`: prompt listing.
- `_handle_get_prompt(...)`: prompt materialization.

Use case: transport-layer JSON-RPC controller.

### Top-level functions/routes

#### `create_token(tenant_id, user_id, permissions, tier)`
Signs JWT access token with expiry and JTI.

#### `require_auth(request, credentials)`
JWT validation dependency for protected endpoints. Attaches tenant + correlation ID to request state.

#### `lifespan(app)`
FastAPI lifecycle manager:
- startup log,
- background keepalive broadcaster,
- graceful shutdown.

Nested helper:
- `keepalive()`: emits periodic ping events.

#### `security_headers(request, call_next)`
Middleware that applies security headers and propagates correlation ID.

#### Route handlers
- `health()`: unauthenticated liveness endpoint.
- `create_access_token(request)`: issues JWTs from request body fields.
- `mcp_endpoint(request, ctx)`: main MCP JSON-RPC endpoint.
- `sse_events(request, ctx)`: event stream endpoint.
	- nested `event_generator()`: emits initial connect event + tenant events + keepalive comments.
- `server_stats(ctx)`: admin-only operational stats endpoint.

---

## `03_client.py` — Production MCP Client

### Purpose
Provides a robust async MCP client with retries, circuit breaker, caching, concurrency controls, and SSE consumption.

### Classes and methods

#### `CircuitState` (Enum)
`CLOSED`, `OPEN`, `HALF_OPEN`.

#### `CircuitBreaker`
- `record_success()`: updates state and closes breaker after enough half-open successes.
- `record_failure()`: tracks rolling failures, opens breaker at threshold.
- `can_request()`: gatekeeper; opens/half-opens based on recovery timeout.
- `state` (property): current breaker state.
- `stats` (property): diagnostic counters.

Use case: prevent cascading failure during downstream outages.

#### `RetryConfig`
- `delay_for_attempt(attempt)`: exponential backoff + optional jitter.
- `should_retry(status_code, exc)`: status/exception retry policy.

Use case: robust transient-failure handling.

#### `ResponseCache`
- `__init__(maxsize, ttl)`: initializes TTL cache.
- `_key(tool, args)`: stable hash key for tool input.
- `get(tool, args)`: cached lookup for idempotent tools.
- `set(tool, args, value)`: cache insert for cacheable tools.
- `info` (property): cache diagnostics.

Use case: reduce repeated read-only tool load.

#### `MCPClient`
- `__init__(...)`: configure client transport, retry, breaker, cache, correlation ID.
- `__aenter__()`: open HTTP client and run MCP initialize handshake.
- `__aexit__()`: close HTTP connections.
- `_next_id()`: JSON-RPC ID generator.
- `_initialize()`: sends `initialize` method and marks client ready.
- `_rpc(method, params)`: core JSON-RPC call path with retry + circuit-breaker behavior.
- `list_tools()`: calls `tools/list`.
- `call_tool(name, arguments)`: cache-aware tool call + content parsing.
- `call_tools_concurrent(calls, max_concurrency=5)`: bounded parallel calls.
	- nested `bounded_call(name, args)`: semaphore wrapper per call.
- `list_resources()`: calls `resources/list`.
- `read_resource(uri)`: calls `resources/read` and extracts text.
- `list_prompts()`: calls `prompts/list`.
- `get_prompt(name, arguments=None)`: calls `prompts/get`.
- `subscribe_events()`: async SSE consumer yielding JSON events.
- `stats` (property): aggregates breaker/cache/session diagnostics.

### Top-level functions

#### `get_token(base_url, tenant_id, user_id)`
Convenience method to request JWT from `/token`.

#### `demo_client()`
Demonstrates full client lifecycle:
- auth,
- tool discovery,
- single + concurrent tool calls,
- resource/prompt usage,
- SSE event subscription.

---

## `04_advanced_concepts.py` — Advanced MCP Patterns

### Purpose
Showcases higher-level architecture patterns used in production MCP systems.

### 1) Middleware pipeline

#### `ToolCallContext` (dataclass)
Shared mutable request context for middleware/tool execution.

#### `Middleware` (abstract)
- `before(ctx)` / `after(ctx)`: lifecycle hooks.

#### `MiddlewarePipeline`
- `__init__()`: holds middleware list.
- `use(middleware)`: appends middleware.
- `run(ctx, handler)`: executes before hooks, handler, then reverse-order after hooks.

#### Concrete middlewares
- `AuditLogMiddleware.before/after`: audit logging with redaction for body/PII-size fields.
- `PiiRedactionMiddleware.before/after/_redact`: masks known sensitive keys recursively.
- `CostTrackingMiddleware.__init__/before/after/get_usage`: estimates per-call token cost by tenant.
- `ArgumentValidationMiddleware.__init__/before/after`: validates input args against JSON schema.

### 2) Sampling API

#### `SamplingClient`
- `__init__(session)`: stores MCP session.
- `create_message(...)`: asks client-side model for reasoning/analysis.
- `analyze_with_llm(data, task)`: helper for structured analysis prompt.

### 3) Tool chaining

#### `ChainStep` (dataclass)
Defines one step with tool name, input mapper, optional condition, output key, and stop-on-error behavior.

#### `ToolChain`
- `__init__(steps)`: stores ordered chain steps.
- `run(initial_context, tool_executor)`: executes conditional multi-step orchestration with context passing.

### 4) Structured output validation

#### Models
- `DocumentResult`
- `SearchOutput` with validator `sort_by_score(...)`
- `IncidentReport`

#### Utility
- `validate_tool_output(model_class, raw_output)`: validates and returns typed model.

### 5) Telemetry and tracing

#### `MCPTelemetry`
- `__init__(service_name)`: sets service metadata and span store.
- `start_span(name, attributes=None)`: begin logical span.
- `end_span(span, error=None)`: close span and mark status.
- `add_event(span, name, attributes=None)`: attach event to span.
- `export_spans()`: returns collected spans.

#### `with_tracing(tool_name)`
Decorator factory that wraps async tools in telemetry span lifecycle.
- nested `decorator(func)`
- nested `wrapper(*args, **kwargs)`

### 6) Roots protocol

#### `RootsManager`
- `__init__()`: initializes root list and listeners.
- `add_root(uri, name)`: add root and notify listeners.
- `remove_root(uri)`: remove root and notify listeners.
- `on_change(handler)`: subscribe listener.
- `roots` (property): returns root list.
- `resolve_path(relative_path)`: resolve relative path against first file root.

### 7) Elicitation

#### `ElicitationField` (dataclass)
Field definition for user-input requests.

#### `ElicitationRequest`
- `__init__(session)`: stores session.
- `ask(prompt, fields, title="Input Required")`: requests structured user input via MCP elicitation flow.
- `_field_to_schema(field)`: converts field model to JSON schema fragment.

### 8) Dynamic tool loading

#### `DynamicToolLoader`
- `__init__(server)`: holds server and loaded tool map.
- `load_from_config(config)`: creates tools at runtime from config map.
- `call_dynamic_tool(name, arguments)`: executes loaded tool function.
- `unload_tool(name)`: removes loaded tool.
- `loaded_tools` (property): lists currently loaded dynamic tools.

### Advanced server composition

#### `AdvancedMCPServer`
- `__init__()`: assembles server, pipeline, roots, elicitation, dynamic tools.
- `_setup_handlers()`: registers MCP handlers.
	- nested `call_tool(name, arguments)`
	- nested `handler(**kwargs)` used by pipeline execution
	- nested `list_tools()`
- `run_incident_workflow(incident_title, severity, symptoms)`: demonstrates elicitation + tool chain.
- `tool_executor(tool_name, args)`: helper used by workflow to execute chain steps.

---

## `05_challenges.py` — Production Challenges + Tests

### Purpose
Documents common failure modes and practical patterns to handle them, then validates them with pytest.

### Utility loader

#### `_load_module(filename, module_name)`
Loads digit-prefixed modules (e.g., `01_core_server.py`) via `importlib`.

Use case: avoids Python identifier limitations in tests/examples.

### 1) Versioning and schema evolution

#### `ProtocolVersion`
- `negotiate(client_version)`: finds best supported protocol version.
- `feature_supported(version, feature)`: feature-gating by version.

#### `ToolVersionRegistry`
- `__init__()`: initializes schema/version stores.
- `register_version(...)`: registers tool schema version + optional deprecation notice.
- `get_schema(tool_name, client_version)`: selects highest compatible schema.
- `migrate_arguments(tool, args, from_ver, to_ver)`: transforms old payloads to new format.

### 2) Error typing and recovery

#### `MCPErrorCode` (Enum)
JSON-RPC + MCP-specific error codes.

#### `MCPError`
- `to_dict()`: JSON-RPC style error shape.
- `from_exception(exc)`: maps generic exceptions to typed MCP errors.

#### `ErrorRecoveryManager`
- `__init__(alert_webhook=None)`: sets strategy state.
- `handle(error, context)`: applies strategy (backoff/fallback/degrade/alert).
- `_send_alert(error, context)`: optional webhook alerting.

### 3) Connection lifecycle

#### `ConnectionState` (Enum)
State machine for client/server connectivity lifecycle.

#### `ConnectionManager`
- `__init__(reconnect_attempts=5, reconnect_delay=2.0)`
- `on_state_change(state, handler)`
- `_transition(new_state)`
- `connect(connector)`
- `reconnect(connector)`
- `is_active` (property)
- `stats` (property)

### 4) Secrets management

#### `SecretManager`
- `__init__(provider="env")`
- `get(key)`
- `_fetch(key)`
- `load_server_config()`

Use case: central, cache-backed secret retrieval with provider abstraction.

### 5) Integration test harness

#### `MCPTestServer`
- `__init__(server)`
- `call_tool(name, arguments)`
- `recorded_calls` (property)
- `assert_tool_called(name, times=1)`
- `assert_tool_called_with(name, **expected_args)`

### Pytest fixture and tests

#### Fixture
- `mock_tool_registry()` with nested async mocks:
	- `mock_search(**kwargs)`
	- `mock_query(**kwargs)`

#### Tests
- `test_protocol_version_negotiation()`
- `test_argument_migration()`
- `test_sql_injection_prevention()`
- `test_rate_limiter_enforcement()`
- `test_circuit_breaker_state_machine()`
- `test_middleware_pipeline_execution_order()`
	- includes nested `LoggingMiddleware.__init__/before/after`
	- includes nested `handler(**kwargs)`
- `test_tool_chain_condition_skip()`
	- includes nested `executor(tool_name, args)`

---

## `main.py` — Unified Runner (Operational CLI)

### Purpose
Provides one command-line entrypoint to run/check/test the entire repository.

### Functions

#### `load_module(filename, module_name)`
Dynamic import helper for numbered file names.

#### `run_core_server()`
Starts stdio MCP server via `01_core_server.main()`.

#### `run_http_server()`
Starts FastAPI/Uvicorn app from `02_http_transport` configuration.

#### `run_client_demo()`
Runs `03_client.demo_client()`.

#### `run_tests()`
Executes pytest for `05_challenges.py` and returns exit code.

#### `check_all_imports()`
Loads all major modules and prints success lines.

#### `check_http_health(url, timeout)`
Simple HTTP health probe to `/health`.

#### `run_all_flow()`
Composite sanity flow: imports, health probe, tests.

#### `build_parser()`
Defines CLI arguments and command choices.

#### `main()`
Entry dispatch and process exit behavior.

---

## 5) Typical Development Flows

### A) Validate project quickly
```bash
python main.py all
```

### B) Run server + client demo
Terminal 1:
```bash
python main.py http
```

Terminal 2:
```bash
python main.py client-demo
```

### C) Run test suite
```bash
python main.py test
```

---

## 6) Operational Notes

- If `python main.py http` fails with port-in-use on `8080`, stop the previous process or change port in `ServerConfig`.
- Keep secrets in `.env`; never commit real tokens.
- Structured logs are JSON and suitable for ingestion into log management systems.
- SQL tool is intentionally read-only and blocks destructive keywords.

---

## 7) Current Notification Scope

Implemented now:
- Slack via `send_notification(channel="slack", ...)` using `SLACK_API_TOKEN`.

Not implemented now:
- Email (explicit `NotImplementedError`)
- PagerDuty/Webhook full integration (placeholder path)

---
