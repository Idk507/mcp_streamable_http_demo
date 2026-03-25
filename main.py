"""Unified entrypoint for the MCP production demo project.

This file provides one place to run the different demo components in this
repository, even though source files are prefixed with numbers (for example
`01_core_server.py`), which are not directly importable as normal Python
modules.

Usage examples:
    python main.py all
    python main.py check
    python main.py core
    python main.py http
    python main.py client-demo
    python main.py test

Design goals:
- Keep the project easy to run from one command.
- Provide clear docstrings for learning/understanding.
- Avoid changing existing module filenames.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).parent.resolve()


def load_module(filename: str, module_name: str) -> ModuleType:
    """Load a Python file as a module using a custom name.

    Why this helper exists:
        Files like `01_core_server.py` cannot be imported with regular syntax
        because Python identifiers cannot start with digits.

    Args:
        filename: The Python filename inside the project root.
        module_name: The runtime-safe module name to register in `sys.modules`.

    Returns:
        The imported module object.

    Raises:
        FileNotFoundError: If the target file does not exist.
        ImportError: If import metadata cannot be created.
        RuntimeError: If execution of the module fails.
    """
    module_path = PROJECT_ROOT / filename
    if not module_path.exists():
        raise FileNotFoundError(f"Module file not found: {module_path}")

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to create import spec for {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RuntimeError(f"Error importing {filename}: {exc}") from exc

    return module


async def run_core_server() -> None:
    """Run the stdio-based MCP core server (`01_core_server.py`).

    This starts the server that communicates over standard input/output, which
    is the transport typically used by MCP host applications that spawn a local
    subprocess.
    """
    core = load_module("01_core_server.py", "core_server")
    await core.main()


def run_http_server() -> None:
    """Run the HTTP + SSE MCP server (`02_http_transport.py`).

    This starts a FastAPI/Uvicorn server on the configured host/port
    (default: 0.0.0.0:8080).
    """
    transport = load_module("02_http_transport.py", "http_transport")

    import uvicorn

    uvicorn.run(
        transport.app,
        host=transport.config.host,
        port=transport.config.port,
        log_config=None,
        access_log=False,
    )


async def run_client_demo() -> None:
    """Run the production client demo from `03_client.py`.

    Note:
        The demo expects the HTTP transport server to already be running on
        `http://localhost:8080` so it can get a token and call MCP methods.
    """
    client = load_module("03_client.py", "mcp_client")
    await client.demo_client()


def run_tests() -> int:
    """Run the repository's integration-focused test file.

    Returns:
        The subprocess return code from pytest.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "05_challenges.py", "-v", "--tb=short"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    return int(result.returncode)


def check_all_imports() -> None:
    """Import all numbered modules to verify they load without errors."""
    modules = [
        ("01_core_server.py", "core_server"),
        ("02_http_transport.py", "http_transport"),
        ("03_client.py", "mcp_client"),
        ("04_advanced_concepts.py", "advanced_concepts"),
        ("05_challenges.py", "challenges"),
    ]

    for filename, module_name in modules:
        load_module(filename, module_name)
        print(f"OK: {filename}")


def check_http_health(url: str = "http://localhost:8080/health", timeout: float = 3.0) -> bool:
    """Check whether the HTTP server health endpoint is reachable.

    Args:
        url: Health endpoint URL.
        timeout: Timeout in seconds for the HTTP request.

    Returns:
        True if reachable and returns a response, otherwise False.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            _ = response.read()
        return True
    except Exception:
        return False


def run_all_flow() -> int:
    """Run a practical "everything" verification flow.

    Flow:
    1. Import-check all major modules.
    2. Verify HTTP server health endpoint if server is already running.
    3. Run test suite.

    Why this shape:
        Fully launching both long-running servers and client demo in one command
        is not ideal in a single foreground process. This flow gives a reliable
        full-project sanity check while keeping the command practical.

    Returns:
        0 if successful, non-zero otherwise.
    """
    print("[1/3] Checking module imports...")
    check_all_imports()

    print("[2/3] Checking HTTP health endpoint (if server is running)...")
    healthy = check_http_health()
    if healthy:
        print("HTTP server health: OK")
    else:
        print("HTTP server health: NOT RUNNING (start with: python main.py http)")

    print("[3/3] Running tests...")
    test_code = run_tests()
    if test_code != 0:
        print("Tests failed")
        return test_code

    print("All checks passed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line interface parser for this entrypoint."""
    parser = argparse.ArgumentParser(
        description="Unified runner for MCP production demo",
    )

    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "check", "core", "http", "client-demo", "test"],
        help=(
            "Command to run: "
            "all (default), check (imports only), core (stdio server), "
            "http (FastAPI server), client-demo, test"
        ),
    )

    return parser


def main() -> int:
    """Program entrypoint.

    Returns:
        Process exit code (0 for success, non-zero for failures).
    """
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "all":
            return run_all_flow()

        if args.command == "check":
            check_all_imports()
            print("Import check passed")
            return 0

        if args.command == "core":
            asyncio.run(run_core_server())
            return 0

        if args.command == "http":
            run_http_server()
            return 0

        if args.command == "client-demo":
            if not check_http_health():
                print("HTTP server is not running. Start it first with: python main.py http")
                return 2
            asyncio.run(run_client_demo())
            return 0

        if args.command == "test":
            return run_tests()

        parser.print_help()
        return 1

    except KeyboardInterrupt:
        print("Interrupted by user")
        return 130
    except Exception as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
