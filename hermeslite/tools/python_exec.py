"""Python code execution tool.

Runs Python in a *subprocess*, not in the same interpreter. This is the
single most important safety property of the tool — a misbehaving script
can't crash or hang the agent. The subprocess inherits the parent
environment by default; pass ``env`` to override.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict

from .registry import Tool, ToolResult, registry

logger = logging.getLogger(__name__)


_WRAPPER = """
import json, sys, traceback
code = json.loads(sys.stdin.read())
try:
    exec(code.get("code", ""), {"__name__": "__main__"}, {})
except SystemExit as e:
    print(json.dumps({"exit": int(e.code) if e.code is not None else 0}))
except BaseException:
    print(json.dumps({"traceback": traceback.format_exc()}))
    sys.exit(1)
"""


class PythonExecTool(Tool):
    name = "python_exec"
    description = (
        "Execute Python code in a fresh subprocess (Python " + sys.version.split()[0] + "). "
        "The `code` argument is the source to run. Output (stdout+stderr) is "
        "captured and returned. `timeout` is in seconds (default 30). "
        "The subprocess is killed if it runs longer than `timeout`."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source code to run."},
            "timeout": {"type": "integer", "description": "Timeout in seconds. Default 30."},
            "max_output": {"type": "integer", "description": "Cap on output bytes. Default 20000."},
        },
        "required": ["code"],
    }

    def run(
        self,
        code: str,
        timeout: int = 30,
        max_output: int = 20_000,
        **_: Any,
    ) -> ToolResult:
        if not code or not code.strip():
            return ToolResult.failure("empty code")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-S", "-c", _WRAPPER],
                input=json.dumps({"code": code}),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            return ToolResult.failure(f"timeout after {timeout}s")
        except OSError as exc:
            return ToolResult.failure(f"{type(exc).__name__}: {exc}")

        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out.encode("utf-8")) > max_output:
            out = out.encode("utf-8")[:max_output].decode("utf-8", errors="replace")
            out += f"\n... [truncated, output was {len(out)} bytes]"
        if proc.returncode == 0:
            return ToolResult.success(out or "(no output)")
        return ToolResult(data=out, ok=False, error=f"exit {proc.returncode}")


registry.register(PythonExecTool())
