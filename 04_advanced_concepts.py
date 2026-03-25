"""
MCP Advanced Concepts
======================
1. Middleware Pipeline (pre/post processing of every tool call)
2. Sampling API (server-side LLM calls)
3. Tool Chaining & Orchestration
4. Structured Output Validation (Pydantic)
5. Observability: OpenTelemetry spans
6. Roots Protocol
7. Elicitation (multi-turn structured input)
8. Dynamic Tool Registration (runtime tool loading)
"""

import asyncio
import json
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Type
from enum import Enum

from pydantic import BaseModel, Field, field_validator
from mcp.server import Server
from mcp import types

logger = logging.getLogger("mcp.advanced")

# ═══════════════════════════════════════════════════════════════════════════
# 1. MIDDLEWARE PIPELINE
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ToolCallContext:
    """Mutable context passed through the middleware chain."""
    tool_name: str
    arguments: dict
    tenant_id: str
    user_id: str
    correlation_id: str
    request_time: float = field(default_factory=time.monotonic)
    metadata: dict = field(default_factory=dict)
    result: Optional[Any] = None
    error: Optional[Exception] = None
    aborted: bool = False

class Middleware(ABC):
    """Base class for all middleware."""
    
    @abstractmethod
    async def before(self, ctx: ToolCallContext) -> None:
        """Called before tool execution. Set ctx.aborted=True to short-circuit."""
        ...
    
    @abstractmethod
    async def after(self, ctx: ToolCallContext) -> None:
        """Called after tool execution (even on error)."""
        ...

class MiddlewarePipeline:
    """Executes middleware in order, handles rollback on error."""
    
    def __init__(self):
        self._middlewares: list[Middleware] = []
    
    def use(self, middleware: Middleware) -> "MiddlewarePipeline":
        self._middlewares.append(middleware)
        return self
    
    async def run(self, ctx: ToolCallContext, handler: Callable) -> Any:
        executed = []
        
        # Run pre-hooks
        for mw in self._middlewares:
            await mw.before(ctx)
            executed.append(mw)
            if ctx.aborted:
                logger.warning(f"Pipeline aborted by {type(mw).__name__}")
                break
        
        # Execute tool handler (if not aborted)
        if not ctx.aborted:
            try:
                ctx.result = await handler(**ctx.arguments)
            except Exception as e:
                ctx.error = e
        
        # Run post-hooks (in reverse order — like finally blocks)
        for mw in reversed(executed):
            try:
                await mw.after(ctx)
            except Exception as e:
                logger.error(f"Middleware post-hook error: {e}")
        
        if ctx.error:
            raise ctx.error
        return ctx.result


# ─── Concrete Middlewares ─────────────────────────────────────────────────

class AuditLogMiddleware(Middleware):
    """Logs every tool call with input/output to audit trail."""
    
    async def before(self, ctx: ToolCallContext) -> None:
        logger.info(
            "AUDIT: Tool call started",
            extra={
                "audit": True,
                "tool": ctx.tool_name,
                "tenant": ctx.tenant_id,
                "user": ctx.user_id,
                "correlation_id": ctx.correlation_id,
                "arguments_hash": hash(json.dumps(ctx.arguments, sort_keys=True)),
            }
        )
    
    async def after(self, ctx: ToolCallContext) -> None:
        duration_ms = round((time.monotonic() - ctx.request_time) * 1000, 2)
        logger.info(
            "AUDIT: Tool call completed",
            extra={
                "audit": True,
                "tool": ctx.tool_name,
                "tenant": ctx.tenant_id,
                "success": ctx.error is None,
                "duration_ms": duration_ms,
                "error": str(ctx.error) if ctx.error else None,
            }
        )

class PiiRedactionMiddleware(Middleware):
    """Redacts PII from arguments before logging and execution."""
    
    PII_FIELDS = {"password", "token", "secret", "ssn", "credit_card", "api_key"}
    
    async def before(self, ctx: ToolCallContext) -> None:
        ctx.arguments = self._redact(ctx.arguments)
    
    async def after(self, ctx: ToolCallContext) -> None:
        pass  # No post-processing needed
    
    def _redact(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: "[REDACTED]" if k.lower() in self.PII_FIELDS else self._redact(v)
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [self._redact(i) for i in obj]
        return obj

class CostTrackingMiddleware(Middleware):
    """Tracks API cost per tenant."""
    
    TOOL_COSTS = {
        "search_documents": 0.001,
        "execute_query": 0.005,
        "send_notification": 0.002,
    }
    
    def __init__(self):
        self._usage: dict[str, float] = {}
    
    async def before(self, ctx: ToolCallContext) -> None:
        pass
    
    async def after(self, ctx: ToolCallContext) -> None:
        if ctx.error is None:
            cost = self.TOOL_COSTS.get(ctx.tool_name, 0.001)
            tenant = ctx.tenant_id
            self._usage[tenant] = self._usage.get(tenant, 0.0) + cost
            logger.debug(
                f"Cost tracked",
                extra={"tenant": tenant, "tool": ctx.tool_name, "cost": cost}
            )
    
    def get_usage(self, tenant_id: str) -> float:
        return self._usage.get(tenant_id, 0.0)

class ArgumentValidationMiddleware(Middleware):
    """Validates tool arguments against JSON Schema before execution."""
    
    def __init__(self, schemas: dict[str, dict]):
        self._schemas = schemas
    
    async def before(self, ctx: ToolCallContext) -> None:
        schema = self._schemas.get(ctx.tool_name)
        if not schema:
            return
        
        # In production: use jsonschema.validate()
        required = schema.get("required", [])
        missing = [f for f in required if f not in ctx.arguments]
        if missing:
            ctx.aborted = True
            ctx.error = ValueError(f"Missing required arguments: {missing}")
    
    async def after(self, ctx: ToolCallContext) -> None:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# 2. SAMPLING API (Server-side LLM calls)
# ═══════════════════════════════════════════════════════════════════════════

class SamplingClient:
    """
    MCP Sampling allows the server to call the LLM on behalf of the client.
    This is useful for tools that need AI reasoning mid-execution.
    
    Real implementation would use the MCP session's sampling capability.
    This shows the pattern.
    """
    
    def __init__(self, session):  # mcp.server.Session in real impl
        self._session = session
    
    async def create_message(
        self,
        messages: list[dict],
        system_prompt: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        model_preferences: Optional[dict] = None,
    ) -> str:
        """
        Request the client to sample from its LLM.
        The server doesn't control which model is used — the client does.
        """
        
        sampling_params = types.CreateMessageRequest(
            messages=[
                types.SamplingMessage(
                    role=msg["role"],
                    content=types.TextContent(type="text", text=msg["content"])
                )
                for msg in messages
            ],
            systemPrompt=system_prompt,
            maxTokens=max_tokens,
            modelPreferences=types.ModelPreferences(
                hints=[
                    types.ModelHint(name="claude-3-5-sonnet"),
                ],
                intelligencePriority=0.8,
                speedPriority=0.2,
            ) if model_preferences is None else None,
            includeContext="thisServer",  # Include current context
        )
        
        # In real implementation:
        # result = await self._session.create_message(sampling_params)
        # return result.content.text
        
        # Mock for demonstration
        return f"AI analysis of: {messages[-1]['content'][:100]}..."
    
    async def analyze_with_llm(self, data: Any, task: str) -> str:
        """Helper: analyze arbitrary data using the client's LLM."""
        return await self.create_message(
            messages=[{
                "role": "user",
                "content": f"Task: {task}\n\nData: {json.dumps(data, indent=2)}",
            }],
            system_prompt=(
                "You are an expert analyst. Provide concise, actionable insights. "
                "Format your response as bullet points."
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3. TOOL CHAINING & ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ChainStep:
    tool: str
    input_mapper: Callable[[dict], dict]  # Maps previous results to tool args
    output_key: str                        # Key to store result under
    condition: Optional[Callable[[dict], bool]] = None  # Skip step if False
    retry_on_failure: bool = True

class ToolChain:
    """
    Sequential tool orchestration with context passing between steps.
    Enables complex multi-step workflows.
    
    Example:
        chain = ToolChain([
            ChainStep("search_documents", 
                      lambda ctx: {"query": ctx["incident_type"]},
                      "runbooks"),
            ChainStep("execute_query",
                      lambda ctx: {"sql": f"SELECT * FROM incidents WHERE type='{ctx['incident_type']}'"},
                      "history",
                      condition=lambda ctx: len(ctx.get("runbooks", [])) > 0),
        ])
        result = await chain.run({"incident_type": "OOM"}, tool_executor)
    """
    
    def __init__(self, steps: list[ChainStep]):
        self.steps = steps
    
    async def run(
        self,
        initial_context: dict,
        executor: Callable,  # async (tool_name, args) -> result
    ) -> dict:
        ctx = dict(initial_context)
        ctx["_step_results"] = []
        
        for i, step in enumerate(self.steps):
            # Check condition
            if step.condition and not step.condition(ctx):
                logger.info(f"Skipping step {i}: {step.tool} (condition=False)")
                continue
            
            # Map inputs from context
            try:
                args = step.input_mapper(ctx)
            except Exception as e:
                raise ValueError(f"Input mapping failed at step {i} ({step.tool}): {e}")
            
            logger.info(f"Executing chain step {i}: {step.tool}", extra={"args_keys": list(args)})
            
            try:
                result = await executor(step.tool, args)
                ctx[step.output_key] = result
                ctx["_step_results"].append({
                    "step": i,
                    "tool": step.tool,
                    "status": "success",
                })
            except Exception as e:
                ctx["_step_results"].append({
                    "step": i,
                    "tool": step.tool,
                    "status": "failed",
                    "error": str(e),
                })
                if not step.retry_on_failure:
                    raise
                logger.error(f"Step {i} failed: {e}")
        
        return ctx


# ═══════════════════════════════════════════════════════════════════════════
# 4. STRUCTURED OUTPUT VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

class DocumentResult(BaseModel):
    id: str
    title: str
    score: float = Field(ge=0.0, le=1.0)
    snippet: str
    url: str
    metadata: dict = Field(default_factory=dict)

class SearchOutput(BaseModel):
    query: str
    results: list[DocumentResult]
    total_found: int
    search_time_ms: float

    @field_validator("results")
    @classmethod
    def sort_by_score(cls, v: list[DocumentResult]) -> list[DocumentResult]:
        return sorted(v, key=lambda r: r.score, reverse=True)

class IncidentReport(BaseModel):
    severity: str = Field(pattern="^P[1-4]$")
    affected_service: str
    symptom_summary: str
    recommended_actions: list[str]
    escalation_required: bool
    estimated_impact: str

def validate_tool_output(model_class: Type[BaseModel], raw_output: dict) -> BaseModel:
    """
    Validates raw tool output against a Pydantic model.
    Raises ValidationError with clear messages on schema violations.
    """
    try:
        return model_class.model_validate(raw_output)
    except Exception as e:
        logger.error(f"Output validation failed for {model_class.__name__}: {e}")
        raise


# ═══════════════════════════════════════════════════════════════════════════
# 5. OBSERVABILITY — OpenTelemetry
# ═══════════════════════════════════════════════════════════════════════════

class MCPTelemetry:
    """
    OpenTelemetry instrumentation for MCP server.
    Provides distributed tracing across tool calls.
    
    In production, configure an OTLP exporter to Jaeger/Tempo/Datadog.
    """
    
    def __init__(self, service_name: str = "mcp-server"):
        self.service_name = service_name
        self._spans: list[dict] = []  # Mock span storage
        
        # Real implementation:
        # from opentelemetry import trace
        # from opentelemetry.sdk.trace import TracerProvider
        # from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        # provider = TracerProvider()
        # provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        # trace.set_tracer_provider(provider)
        # self.tracer = trace.get_tracer(service_name)
    
    def start_span(self, name: str, attributes: dict = None) -> dict:
        """Create a new trace span."""
        span = {
            "name": name,
            "trace_id": __import__("uuid").uuid4().hex,
            "span_id": __import__("uuid").uuid4().hex[:16],
            "start_time": time.monotonic_ns(),
            "attributes": attributes or {},
            "events": [],
            "status": "OK",
        }
        self._spans.append(span)
        return span
    
    def end_span(self, span: dict, error: Optional[Exception] = None):
        span["end_time"] = time.monotonic_ns()
        span["duration_ms"] = (span["end_time"] - span["start_time"]) / 1_000_000
        if error:
            span["status"] = "ERROR"
            span["error"] = str(error)
    
    def add_event(self, span: dict, name: str, attributes: dict = None):
        span["events"].append({
            "name": name,
            "timestamp": time.monotonic_ns(),
            "attributes": attributes or {},
        })
    
    def export_spans(self) -> list[dict]:
        return self._spans.copy()

telemetry = MCPTelemetry(service_name="mcp-production")

def with_tracing(tool_name: str):
    """Decorator that wraps a tool handler with OpenTelemetry spans."""
    def decorator(func: Callable) -> Callable:
        async def wrapper(*args, **kwargs):
            span = telemetry.start_span(
                f"mcp.tool.{tool_name}",
                attributes={"tool.name": tool_name, "arg.count": len(kwargs)}
            )
            try:
                result = await func(*args, **kwargs)
                telemetry.add_event(span, "tool.success")
                return result
            except Exception as e:
                telemetry.end_span(span, error=e)
                raise
            finally:
                telemetry.end_span(span)
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════════
# 6. ROOTS PROTOCOL
# ═══════════════════════════════════════════════════════════════════════════

class RootsManager:
    """
    MCP Roots allows the client to expose filesystem/workspace roots to the server.
    The server can then read files relative to these roots.
    
    Enables: code analysis, document processing, config reading.
    """
    
    def __init__(self):
        self._roots: list[types.Root] = []
        self._change_handlers: list[Callable] = []
    
    def add_root(self, uri: str, name: str):
        """Register a new root (e.g. workspace directory)."""
        root = types.Root(uri=uri, name=name)
        self._roots.append(root)
        logger.info(f"Root added: {name} -> {uri}")
        for handler in self._change_handlers:
            asyncio.create_task(handler(self._roots))
    
    def remove_root(self, uri: str):
        before = len(self._roots)
        self._roots = [r for r in self._roots if r.uri != uri]
        if len(self._roots) < before:
            logger.info(f"Root removed: {uri}")
    
    def on_change(self, handler: Callable):
        self._change_handlers.append(handler)
    
    @property
    def roots(self) -> list[types.Root]:
        return self._roots.copy()
    
    def resolve_path(self, relative_path: str) -> Optional[str]:
        """Find which root a relative path belongs to."""
        for root in self._roots:
            candidate = f"{root.uri}/{relative_path}"
            return candidate  # Simplified; in prod: check actual filesystem
        return None


# ═══════════════════════════════════════════════════════════════════════════
# 7. ELICITATION (Multi-turn Structured Input)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ElicitationField:
    name: str
    type: str  # "string" | "number" | "boolean" | "enum"
    label: str
    required: bool = True
    options: Optional[list[str]] = None  # For enum type
    default: Optional[Any] = None

class ElicitationRequest:
    """
    MCP Elicitation allows a tool to request additional structured input from the user
    mid-execution, rather than failing with an error.
    
    Pattern: Tool starts → realizes it needs more info → elicits from user → continues.
    """
    
    def __init__(self, session):
        self._session = session
    
    async def ask(
        self,
        message: str,
        fields: list[ElicitationField],
    ) -> Optional[dict]:
        """
        Request structured input from the user.
        Returns the filled-in values, or None if user cancelled.
        """
        
        # Build JSON schema from fields
        schema = {
            "type": "object",
            "properties": {
                f.name: self._field_to_schema(f)
                for f in fields
            },
            "required": [f.name for f in fields if f.required],
        }
        
        # In real MCP implementation:
        # result = await self._session.elicit(
        #     message=message,
        #     requestedSchema=schema,
        # )
        # if result.action == "cancel":
        #     return None
        # return result.content
        
        # Mock: return defaults
        return {
            f.name: f.default or (f.options[0] if f.options else None)
            for f in fields
        }
    
    def _field_to_schema(self, f: ElicitationField) -> dict:
        if f.type == "enum":
            return {"type": "string", "enum": f.options, "title": f.label}
        elif f.type == "number":
            return {"type": "number", "title": f.label}
        elif f.type == "boolean":
            return {"type": "boolean", "title": f.label}
        return {"type": "string", "title": f.label}


# ═══════════════════════════════════════════════════════════════════════════
# 8. DYNAMIC TOOL REGISTRATION
# ═══════════════════════════════════════════════════════════════════════════

class DynamicToolLoader:
    """
    Loads tools from external sources at runtime.
    Supports: Python plugins, config-driven tools, A/B testing variants.
    """
    
    def __init__(self, server: Server):
        self._server = server
        self._dynamic_tools: dict[str, dict] = {}
    
    async def load_from_config(self, config: dict) -> int:
        """Load tools from a configuration dict."""
        loaded = 0
        for tool_def in config.get("tools", []):
            name = tool_def["name"]
            
            self._dynamic_tools[name] = {
                "description": tool_def["description"],
                "schema": tool_def["input_schema"],
                "endpoint": tool_def.get("endpoint"),  # External API endpoint
                "transform": tool_def.get("transform"),  # Response transformation
            }
            loaded += 1
            logger.info(f"Dynamically loaded tool: {name}")
        
        # Notify clients of tool list change
        # In production: await self._server.send_tool_list_changed()
        return loaded
    
    async def call_dynamic_tool(self, name: str, arguments: dict) -> Any:
        """Execute a dynamically loaded tool."""
        if name not in self._dynamic_tools:
            raise ValueError(f"Dynamic tool not found: {name}")
        
        tool_def = self._dynamic_tools[name]
        
        if "endpoint" in tool_def and tool_def["endpoint"]:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(tool_def["endpoint"], json=arguments)
                response.raise_for_status()
                return response.json()
        
        raise NotImplementedError(f"No handler for dynamic tool: {name}")
    
    def unload_tool(self, name: str):
        """Remove a dynamically loaded tool."""
        if name in self._dynamic_tools:
            del self._dynamic_tools[name]
            logger.info(f"Unloaded dynamic tool: {name}")
    
    @property
    def loaded_tools(self) -> list[str]:
        return list(self._dynamic_tools.keys())


# ═══════════════════════════════════════════════════════════════════════════
# PUTTING IT ALL TOGETHER — Advanced Server with all patterns
# ═══════════════════════════════════════════════════════════════════════════

class AdvancedMCPServer:
    """
    Demonstrates all advanced patterns integrated in one server.
    """
    
    def __init__(self):
        self.app = Server("advanced-mcp-server")
        self.telemetry = MCPTelemetry()
        self.roots_manager = RootsManager()
        self.cost_tracker = CostTrackingMiddleware()
        
        # Build middleware pipeline
        self.pipeline = MiddlewarePipeline()
        self.pipeline.use(AuditLogMiddleware())
        self.pipeline.use(PiiRedactionMiddleware())
        self.pipeline.use(self.cost_tracker)
        self.pipeline.use(ArgumentValidationMiddleware({
            "search_documents": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            }
        }))
        
        # Tool chains for complex workflows
        self.incident_chain = ToolChain([
            ChainStep(
                tool="search_documents",
                input_mapper=lambda ctx: {
                    "query": ctx["incident_description"],
                    "collection": "runbooks",
                    "limit": 5,
                },
                output_key="runbooks",
            ),
            ChainStep(
                tool="execute_query",
                input_mapper=lambda ctx: {
                    "sql": f"SELECT * FROM incidents WHERE service='{ctx.get('service', 'unknown')}' ORDER BY created_at DESC LIMIT 10",
                },
                output_key="incident_history",
                condition=lambda ctx: ctx.get("service") is not None,
            ),
            ChainStep(
                tool="send_notification",
                input_mapper=lambda ctx: {
                    "channel": "slack",
                    "destination": "#incidents",
                    "subject": f"Incident: {ctx['incident_description'][:50]}",
                    "body": f"Runbooks found: {len(ctx.get('runbooks', []))}\nHistory entries: {len(ctx.get('incident_history', {}).get('rows', []))}",
                    "priority": "high",
                },
                output_key="notification",
                condition=lambda ctx: len(ctx.get("runbooks", [])) == 0,  # Notify only if no runbooks
            ),
        ])
        
        self._setup_handlers()
    
    def _setup_handlers(self):
        
        @self.app.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
            span = self.telemetry.start_span(
                f"mcp.tool.{name}",
                attributes={"tool.name": name}
            )
            
            ctx = ToolCallContext(
                tool_name=name,
                arguments=arguments,
                tenant_id="demo",
                user_id="system",
                correlation_id=span["trace_id"],
            )
            
            async def handler(**kwargs):
                # Simulated tool execution
                await asyncio.sleep(0.01)
                return {"tool": name, "status": "ok", "args": kwargs}
            
            try:
                result = await self.pipeline.run(ctx, handler)
                self.telemetry.end_span(span)
                
                return [types.TextContent(
                    type="text",
                    text=json.dumps(result, indent=2),
                )]
            except Exception as e:
                self.telemetry.end_span(span, error=e)
                raise
        
        @self.app.list_tools()
        async def list_tools():
            return [
                types.Tool(
                    name="incident_workflow",
                    description="Run full incident response workflow: search runbooks, query history, notify team",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "incident_description": {"type": "string"},
                            "service": {"type": "string"},
                        },
                        "required": ["incident_description"],
                    },
                )
            ]
    
    async def run_incident_workflow(
        self,
        incident_description: str,
        service: Optional[str],
        tool_executor: Callable,
    ) -> dict:
        """Example of running the tool chain for incident response."""
        return await self.incident_chain.run(
            initial_context={
                "incident_description": incident_description,
                "service": service,
            },
            executor=tool_executor,
        )


# ═══════════════════════════════════════════════════════════════════════════
# DEMO: Advanced usage
# ═══════════════════════════════════════════════════════════════════════════

async def demo_advanced():
    
    # Mock tool executor
    async def tool_executor(tool_name: str, args: dict) -> Any:
        await asyncio.sleep(0.05)
        if tool_name == "search_documents":
            return [{"id": "doc_1", "title": "OOM Runbook", "score": 0.95}]
        elif tool_name == "execute_query":
            return {"rows": [["2025-03-18", "OOM", "P2"]], "row_count": 1}
        elif tool_name == "send_notification":
            return {"status": "sent", "notification_id": "notif_123"}
        return {}
    
    server = AdvancedMCPServer()
    
    # Run incident workflow
    result = await server.run_incident_workflow(
        incident_description="Out of memory error on payment service",
        service="payment-service",
        tool_executor=tool_executor,
    )
    
    print("Incident workflow result:")
    print(json.dumps({k: v for k, v in result.items() if not k.startswith("_")}, indent=2, default=str))
    
    # Show telemetry spans
    spans = server.telemetry.export_spans()
    print(f"\nTelemetry spans collected: {len(spans)}")
    
    # Show cost tracking
    cost = server.cost_tracker.get_usage("demo")
    print(f"Tenant 'demo' cost: ${cost:.4f}")
    
    # Demonstrate dynamic tool loading
    loader = DynamicToolLoader(Server("test"))
    count = await loader.load_from_config({
        "tools": [
            {
                "name": "weather_api",
                "description": "Get weather data",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
                "endpoint": "https://api.weather.internal/v1/current",
            }
        ]
    })
    print(f"\nDynamically loaded {count} tools: {loader.loaded_tools}")
    
    # Demonstrate structured output validation
    raw = {
        "id": "doc_1",
        "title": "Test Document",
        "score": 0.85,
        "snippet": "Sample text...",
        "url": "https://docs.internal/doc_1",
    }
    validated = validate_tool_output(DocumentResult, raw)
    print(f"\nValidated output: {validated.model_dump()}")

if __name__ == "__main__":
    asyncio.run(demo_advanced())
