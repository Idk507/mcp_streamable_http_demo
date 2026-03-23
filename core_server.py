"""
MCP Production Server - Core Implementation
=============================================
A production-grade MCP server with:
- Tool registration with proper schemas
- Resource management
- Prompt templates
- Error handling & structured logging
- Authentication middleware
- Rate limiting
- Health checks
"""

import asyncio
import json
import logging
import time
import hashlib
import hmac
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from functools import wraps

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# ─── Structured Logging Setup ──────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
        }
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra"):
            log_obj.update(record.extra)
        return json.dumps(log_obj)

def setup_logging(level: str = "INFO") -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("mcp.server")
    logger.setLevel(getattr(logging, level))
    logger.addHandler(handler)
    return logger

logger = setup_logging()

# rate limitter 

@dataclass
class RateLimiter:
    """Token bucket rate limiter per client."""
    max_requests: int = 100
    window_seconds: int = 60
    _buckets: dict = field(default_factory=lambda: defaultdict(list))

    def is_allowed(self, client_id :str ) -> tuple[bool , dict] :
        now = time.monotonic()
        window_start = now - self.window_seconds
        bucket = self._buckets[client_id]

        # evict old timestamps
        self._buckets[client_id] = [timestamp for timestamp in bucket if timestamp > window_start]

        if len(self._buckets[client_id]) >= self.max_requests:
            oldest = min(self._buckets[client_id])
            retry_after = int(oldest + self.window_seconds - now) + 1
            return False, {"retry_after": retry_after, "limit": self.max_requests}
        
        self._buckets[client_id].append(now)
        remaining = self.max_requests - len(self._buckets[client_id])
        return True, {"remaining": remaining, "limit": self.max_requests}   

rate_limiter = RateLimiter(max_requests=5, window_seconds=60)


# Tool registry with metadata

@dataclass 
class ToolMetadata :
    name : str 
    description : str 
    input_schema : dict 
    handler : Callable
    requires_auth : bool = True 
    rate_limit_override : Optional[int] = None
    timeout_seconds :float = 30.0 
    tags : list[str] = field(default_factory=list)


class ToolRegistry :
    def __init__(self):
        self._tools : dict[str, ToolMetadata] = {}
        self._call_counts : dict[str, int] = defaultdict(int)
        self._error_counts : dict[str, int] = defaultdict(int)
    
    def register(self, name : str, description : str, input_schema : dict, requires_auth :bool = True,
                timeout_seconds : float =30.0, tags : list[str] = None):
                """ Decorator to register a tool with full metadata"""
                def decorator(func : Callable) -> callable :
                    self._tools[name] = ToolMetadata(
                        name=name,
                        description=description,
                        input_schema=input_schema,
                        handler=func,
                        requires_auth=requires_auth,
                        timeout_seconds=timeout_seconds,
                        tags=tags or [],
                    )
                    logger.info(f"Registered tool: {name}", extra={"tool": name})

                    @wraps(func)
                    async def wrapper(*args, **kwargs):
                        self._call_counts[name] += 1
                        start = time.monotonic()
                        try : 
                            result = await asyncio.wait_for(
                                func(*args, **kwargs), timeout=timeout_seconds
                            )
                            elapsed = time.monotonic() - start
                            logger.info(
                                f"Tool executed successfully: {name}",
                                extra={"tool": name, "elapsed_seconds": elapsed},
                            )
                            return result
                        except asyncio.TimeoutError:
                            self._error_counts[name] += 1
                            logger.error(
                                f"Tool execution timed out: {name}",
                                extra={"tool": name, "timeout_seconds": timeout_seconds},
                            )
                            raise RuntimeError(f"Tool '{name}' timed out after {timeout_seconds}s")

                    self._tools[name].handler = wrapper
                    return wrapper
                return decorator

    def get_mcp_tools(self) -> list[types.Tool]:
                return [
                    types.Tool(
                        name = meta.name,
                        description = meta.description,
                        inputSchema = meta.input_schema,

                    ) for meta in self._tools.values()
                ]
            
    def stats(self) -> dict:
                return {
                    name: {
                        "calls": self._call_counts[name],
                        "errors": self._error_counts[name],
                        "error_rate": (
                            self._error_counts[name] / self._call_counts[name]
                            if self._call_counts[name] > 0 else 0.0
                        ),
                    }
                    for name in self._tools
                }
registry = ToolRegistry()
    