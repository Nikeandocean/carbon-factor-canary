#!/usr/bin/env python3
"""User-journey smoke test: launch the MCP server exactly as a client would,
then exercise it over raw stdio JSON-RPC.

Stdlib only -- deliberately no MCP SDK dependency, so the canary stays
independent of the very ecosystem it monitors. The server command is passed
after ``--`` so the workflow decides how a "user" launches it (npm wrapper,
python -m, etc.).

Exit code 0 = all assertions passed; 1 = failure (with diagnostics).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import NoReturn

INITIALIZE_TIMEOUT_S = 900  # cold start: the npm wrapper may pip-install torch
CALL_TIMEOUT_S = 1800  # first Pro call may download the embedding model (~400 MB)

EXPECTED_TOOLS = {"factor_match", "factor_search", "factor_detail"}
ACTIVITY = "cement production, rotary kiln"


def log(msg: str) -> None:
    print(f"[canary] {msg}", flush=True)


class StdioMcpClient:
    """Minimal newline-delimited JSON-RPC client over a subprocess' stdio."""

    def __init__(self, cmd: list[str]):
        self._stderr_file = tempfile.TemporaryFile(mode="w+")
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next_id = 1
        # Blocking readline() never honors a deadline, so read lines on a
        # thread and get() them with a timeout instead.
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader = threading.Thread(target=self._read_lines, daemon=True)
        self._reader.start()

    def _read_lines(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            self._lines.put(line)
        # EOF sentinel: lets request() fail fast when the server dies instead
        # of blocking until the full timeout (a server that crashes at startup
        # emits nothing, so only this sentinel reveals it).
        self._lines.put(None)

    def request(self, method: str, params: dict, timeout_s: float) -> dict:
        if self._proc.poll() is not None:
            raise ConnectionError(
                f"Server exited (code {self._proc.returncode}) before '{method}' was sent. "
                f"Server stderr tail:\n{self._stderr_tail()}"
            )
        req_id = self._next_id
        self._next_id += 1
        line = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        assert self._proc.stdin is not None
        self._proc.stdin.write(line + "\n")
        self._proc.stdin.flush()

        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"No response to '{method}' within {timeout_s:.0f}s. "
                    f"Server stderr tail:\n{self._stderr_tail()}"
                )
            try:
                raw = self._lines.get(timeout=min(remaining, 5))
            except queue.Empty:
                if self._proc.poll() is not None:
                    raise ConnectionError(
                        f"Server exited (code {self._proc.returncode}) while waiting for '{method}'. "
                        f"Server stderr tail:\n{self._stderr_tail()}"
                    ) from None
                continue
            if raw is None:
                try:
                    self._proc.wait(timeout=10)  # reap so poll() reports the real code
                except subprocess.TimeoutExpired:
                    pass
                raise ConnectionError(
                    f"Server closed stdout while waiting for '{method}' "
                    f"(exit code {self._proc.poll()}). Server stderr tail:\n{self._stderr_tail()}"
                )
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log(f"ignoring non-JSON stdout line: {raw[:120]!r}")
                continue
            if msg.get("id") == req_id and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError(f"'{method}' returned JSON-RPC error: {msg['error']}")
                return msg["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self._proc.stdin is not None
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def _stderr_tail(self, limit: int = 2000) -> str:
        self._stderr_file.seek(0)
        return self._stderr_file.read()[-limit:]

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=10)
        except Exception:
            self._proc.kill()
        self._stderr_file.close()


def fail(msg: str, client: StdioMcpClient | None = None) -> NoReturn:
    log(f"FAIL: {msg}")
    if client is not None:
        client.close()
    sys.exit(1)


def parse_tool_payload(result: dict, tool: str) -> dict:
    """Extract the ToolResponse dict from a tools/call result."""
    if result.get("isError"):
        raise AssertionError(f"{tool} reported isError: {result.get('content')}")
    content = result.get("content") or []
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for item in content:
        if item.get("type") == "text":
            return json.loads(item["text"])
    raise AssertionError(f"{tool} returned no text content: {result}")


def resolve_windows_command(cmd: list[str]) -> list[str]:
    """Make a bare command like 'npx' executable via Popen on Windows.

    CreateProcess cannot find or execute 'npx' directly: it is npx.cmd, and
    .cmd/.bat files must go through the command interpreter.
    """
    if os.name != "nt":
        return cmd
    resolved = shutil.which(cmd[0])
    if resolved is None:
        return cmd  # let Popen raise with its usual diagnostic
    cmd = [resolved] + cmd[1:]
    if resolved.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c"] + cmd
    return cmd


def main() -> None:
    if "--" not in sys.argv:
        sys.exit(f"usage: {Path(sys.argv[0]).name} -- <server launch command...>\n"
                 "example: python smoke_user_journey.py -- npx -y @nikeandocean/carbon-factor-matcher")
    cmd = sys.argv[sys.argv.index("--") + 1:]
    if not cmd:
        sys.exit("server command is required after --")
    cmd = resolve_windows_command(cmd)

    log(f"launching server as a user would: {cmd}")
    t_start = time.monotonic()
    client = StdioMcpClient(cmd)

    # -- Phase 1: initialize handshake -------------------------------------
    try:
        init = client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "carbon-factor-canary", "version": "1.0.0"},
            },
            timeout_s=INITIALIZE_TIMEOUT_S,
        )
    except (TimeoutError, ConnectionError, RuntimeError) as e:
        fail(str(e), client)
    t_init = time.monotonic() - t_start
    server_info = init.get("serverInfo", {})
    log(f"initialized in {t_init:.1f}s: serverInfo={server_info}")

    client.notify("notifications/initialized")

    # -- Phase 2: tools/list -------------------------------------------------
    tools_result = client.request("tools/list", {}, timeout_s=60)
    tool_names = {t["name"] for t in tools_result.get("tools", [])}
    missing = EXPECTED_TOOLS - tool_names
    if missing:
        fail(f"missing expected tools: {sorted(missing)} (got {sorted(tool_names)})", client)
    log(f"tools/list OK: {sorted(tool_names)}")

    # -- Phase 3: factor_search (free path, live API) ------------------------
    try:
        search = parse_tool_payload(
            client.request(
                "tools/call",
                {"name": "factor_search", "arguments": {"query": "electricity", "limit": 3}},
                timeout_s=CALL_TIMEOUT_S,
            ),
            "factor_search",
        )
    except (TimeoutError, ConnectionError, RuntimeError, AssertionError, json.JSONDecodeError) as e:
        fail(f"factor_search failed: {e}", client)
    search_data = search.get("data") or {}
    if not search_data.get("factors"):
        fail(f"factor_search returned no factors: {json.dumps(search)[:300]}", client)
    log(f"factor_search OK: {len(search_data['factors'])} factors via live API")

    # -- Phase 4: factor_match (Pro path: hybrid + quality ratings) ----------
    # Asserts the paid features are actually engaged, catching silent
    # degradation to keyword-only matching. Only runs when a PRO license key
    # is configured: keys are validated remotely against the key registry, so
    # a fake/placeholder PRO key crashes the server at startup. Local dev runs
    # without a key stop here; the scheduled workflow supplies the real key.
    license_key = os.environ.get("CARBON_FACTOR_LICENSE_KEY", "")
    if not license_key.upper().startswith("PRO"):
        client.close()
        log("CARBON_FACTOR_LICENSE_KEY not set to a PRO key -- "
            "skipping Pro assertions (Free-path checks passed).")
        log(f"ALL CHECKS PASSED (partial, Free tier) in {time.monotonic() - t_start:.1f}s")
        return
    t_match = time.monotonic()
    try:
        match = parse_tool_payload(
            client.request(
                "tools/call",
                {"name": "factor_match",
                 "arguments": {"activity_data": ACTIVITY, "top_k": 5}},
                timeout_s=CALL_TIMEOUT_S,
            ),
            "factor_match",
        )
    except (TimeoutError, ConnectionError, RuntimeError, AssertionError, json.JSONDecodeError) as e:
        fail(f"factor_match failed: {e}", client)
    t_match = time.monotonic() - t_match

    data = match.get("data") or {}
    if "error" in data:
        fail(f"factor_match returned error: {data['error']}", client)
    candidates = data.get("candidates")
    if not candidates:
        fail(
            "factor_match returned no 'candidates' -- Pro hybrid path did not engage "
            f"(free tier returns 'factors' instead). Payload: {json.dumps(data)[:300]}",
            client,
        )
    top = candidates[0]
    for field in ("hybrid_score", "final_score", "quality_ratings"):
        if field not in top:
            fail(f"Pro candidate missing '{field}' -- quality ranking may have degraded. "
                 f"Top candidate keys: {sorted(top)}", client)
    if match.get("upgrade_hint"):
        fail(f"upgrade_hint present for a Pro key: {match['upgrade_hint']!r}", client)
    log(f"factor_match OK (Pro path engaged): {len(candidates)} candidates in {t_match:.1f}s, "
        f"top hybrid_score={top['hybrid_score']}")

    client.close()
    log(f"ALL CHECKS PASSED in {time.monotonic() - t_start:.1f}s")


if __name__ == "__main__":
    main()
