"""OpenAI-compatible shim that forwards Hermes requests to `copilot --acp`.

This adapter lets Hermes treat the GitHub Copilot ACP server as a chat-style
backend. Each request starts a short-lived ACP session, sends the formatted
conversation as a single prompt, collects text chunks, and converts the result
back into the minimal shape Hermes expects from an OpenAI client.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ACP_MARKER_BASE_URL = "acp://copilot"
_DEFAULT_TIMEOUT_SECONDS = 900.0

_ACP_RUNTIME_DEFAULTS: dict[str, dict[str, Any]] = {
    "acp://copilot": {
        "label": "Copilot ACP",
        "command_envs": ("HERMES_COPILOT_ACP_COMMAND", "COPILOT_CLI_PATH"),
        "args_env": "HERMES_COPILOT_ACP_ARGS",
        "default_command": "copilot",
        "default_args": ["--acp", "--stdio"],
        "install_hint": "Install GitHub Copilot CLI or set HERMES_COPILOT_ACP_COMMAND/COPILOT_CLI_PATH.",
    },
    "acp://opencode": {
        "label": "OpenCode ACP",
        "command_envs": ("HERMES_OPENCODE_ACP_COMMAND", "OPENCODE_CLI_PATH"),
        "args_env": "HERMES_OPENCODE_ACP_ARGS",
        "default_command": "opencode",
        "default_args": ["acp"],
        "install_hint": "Install OpenCode CLI or set HERMES_OPENCODE_ACP_COMMAND/OPENCODE_CLI_PATH.",
    },
}

_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TOOL_CALL_JSON_RE = re.compile(r"\{\s*\"id\"\s*:\s*\"[^\"]+\"\s*,\s*\"type\"\s*:\s*\"function\"\s*,\s*\"function\"\s*:\s*\{.*?\}\s*\}", re.DOTALL)


def _runtime_defaults(base_url: str | None = None) -> dict[str, Any]:
    normalized = str(base_url or "").strip().lower()
    for prefix, config in _ACP_RUNTIME_DEFAULTS.items():
        if normalized.startswith(prefix):
            return dict(config)
    return dict(_ACP_RUNTIME_DEFAULTS[ACP_MARKER_BASE_URL])


def _resolve_command(base_url: str | None = None) -> str:
    defaults = _runtime_defaults(base_url)
    for env_var in defaults["command_envs"]:
        value = os.getenv(env_var, "").strip()
        if value:
            return value
    return defaults["default_command"]


def _resolve_args(base_url: str | None = None) -> list[str]:
    defaults = _runtime_defaults(base_url)
    raw = os.getenv(defaults["args_env"], "").strip()
    if not raw:
        return list(defaults["default_args"])
    return shlex.split(raw)


def _jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _format_messages_as_prompt(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> str:
    sections: list[str] = [
        "You are being used as the active ACP agent backend for Hermes.",
        "Use ACP capabilities to complete tasks.",
        "IMPORTANT: If you take an action with a tool, you MUST output tool calls using <tool_call>{...}</tool_call> blocks with JSON exactly in OpenAI function-call shape.",
        "If no tool is needed, answer normally.",
    ]
    if model:
        sections.append(f"Hermes requested model hint: {model}")

    if isinstance(tools, list) and tools:
        tool_specs: list[dict[str, Any]] = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function") or {}
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            tool_specs.append(
                {
                    "name": name.strip(),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                }
            )
        if tool_specs:
            sections.append(
                "Available tools (OpenAI function schema). "
                "When using a tool, emit ONLY <tool_call>{...}</tool_call> with one JSON object "
                "containing id/type/function{name,arguments}. arguments must be a JSON string.\n"
                + json.dumps(tool_specs, ensure_ascii=False)
            )

    if tool_choice is not None:
        sections.append(f"Tool choice hint: {json.dumps(tool_choice, ensure_ascii=False)}")

    transcript: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown").strip().lower()
        if role == "tool":
            role = "tool"
        elif role not in {"system", "user", "assistant"}:
            role = "context"

        content = message.get("content")
        rendered = _render_message_content(content)
        if not rendered:
            continue

        label = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool",
            "context": "Context",
        }.get(role, role.title())
        transcript.append(f"{label}:\n{rendered}")

    if transcript:
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))

    sections.append("Continue the conversation from the latest user request.")
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        if "content" in content and isinstance(content.get("content"), str):
            return str(content.get("content") or "").strip()
        return json.dumps(content, ensure_ascii=True)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content).strip()



# Mapping from OpenCode native tool names -> Hermes equivalents.
# OpenCode executes tools itself (bash, read, glob, grep, web_search), so we
# need to translate its rawInput into the shape Hermes expects.
_OPENCODE_TOOL_MAP: dict[str, str] = {
    "bash": "terminal",
    "read": "read_file",
    "glob": "search_files",
    "grep": "search_files",
    "web_search": "web_search",
}


def _convert_opencode_tool_calls(
    raw_events: list[dict[str, Any]],
) -> list[SimpleNamespace]:
    """Convert OpenCode's structured tool_call events into Hermes SimpleNamespace format.
    
    Detects tool type from rawInput structure since title field can be descriptive.
    """
    result: list[SimpleNamespace] = []
    for entry in raw_events:
        tc_id = str(entry.get("id", ""))
        if not tc_id:
            continue
        # Skip invalid/error entries
        title = str(entry.get("title", ""))
        if title.lower() in ("invalid", "invalid tool"):
            continue
        raw_input = entry.get("rawInput") or {}
        if not isinstance(raw_input, dict):
            raw_input = {}
        # Skip entries with errors
        if "error" in raw_input and not isinstance(raw_input["error"], list):
            continue
        
        # Detect tool type from rawInput structure + title fallback
        hermes_name: str | None = None
        fn_args: dict[str, Any] = {}
        
        if "command" in raw_input:
            # Terminal/bash command (has 'command' key)
            hermes_name = "terminal"
            fn_args = {"command": str(raw_input["command"])}
        elif "path" in raw_input or "paths" in raw_input:
            # File read
            hermes_name = "read_file"
            paths = raw_input.get("paths", []) or []
            path_str = str(paths[0]) if isinstance(paths, list) and paths else str(raw_input.get("path", ""))
            fn_args = {"path": path_str}
        elif "query" in raw_input:
            # Web search
            hermes_name = "web_search"
            fn_args = {"query": str(raw_input["query"])}
        elif "pattern" in raw_input:
            # File search (glob/grep)
            hermes_name = "search_files"
            fn_args = {"pattern": str(raw_input["pattern"])}
        else:
            # Try title-based mapping as fallback
            hermes_name = _OPENCODE_TOOL_MAP.get(title, title)
            fn_args = dict(raw_input)
        
        if not hermes_name:
            continue
        
        result.append(
            SimpleNamespace(
                id=tc_id,
                call_id=tc_id,
                response_item_id=None,
                type="function",
                function=SimpleNamespace(
                    name=hermes_name,
                    arguments=json.dumps(fn_args, ensure_ascii=False),
                ),
            )
        )
    return result


def _extract_tool_calls_from_text(text: str) -> tuple[list[SimpleNamespace], str]:
    if not isinstance(text, str) or not text.strip():
        return [], ""

    extracted: list[SimpleNamespace] = []
    consumed_spans: list[tuple[int, int]] = []

    def _try_add_tool_call(raw_json: str) -> None:
        try:
            obj = json.loads(raw_json)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        fn = obj.get("function")
        if not isinstance(fn, dict):
            return
        fn_name = fn.get("name")
        if not isinstance(fn_name, str) or not fn_name.strip():
            return
        fn_args = fn.get("arguments", "{}")
        if not isinstance(fn_args, str):
            fn_args = json.dumps(fn_args, ensure_ascii=False)
        call_id = obj.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"acp_call_{len(extracted)+1}"

        extracted.append(
            SimpleNamespace(
                id=call_id,
                call_id=call_id,
                response_item_id=None,
                type="function",
                function=SimpleNamespace(name=fn_name.strip(), arguments=fn_args),
            )
        )

    for m in _TOOL_CALL_BLOCK_RE.finditer(text):
        raw = m.group(1)
        _try_add_tool_call(raw)
        consumed_spans.append((m.start(), m.end()))

    # Only try bare-JSON fallback when no XML blocks were found.
    if not extracted:
        for m in _TOOL_CALL_JSON_RE.finditer(text):
            raw = m.group(0)
            _try_add_tool_call(raw)
            consumed_spans.append((m.start(), m.end()))

    if not consumed_spans:
        return extracted, text.strip()

    consumed_spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in consumed_spans:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(text):
        parts.append(text[cursor:])

    cleaned = "\n".join(p.strip() for p in parts if p and p.strip()).strip()
    return extracted, cleaned



def _ensure_path_within_cwd(path_text: str, cwd: str) -> Path:
    candidate = Path(path_text)
    if not candidate.is_absolute():
        raise PermissionError("ACP file-system paths must be absolute.")
    resolved = candidate.resolve()
    root = Path(cwd).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Path '{resolved}' is outside the session cwd '{root}'.") from exc
    return resolved


class _ACPChatCompletions:
    def __init__(self, client: "CopilotACPClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _ACPChatNamespace:
    def __init__(self, client: "CopilotACPClient"):
        self.completions = _ACPChatCompletions(client)


class CopilotACPClient:
    """Minimal OpenAI-client-compatible facade for Copilot ACP."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        acp_command: str | None = None,
        acp_args: list[str] | None = None,
        acp_cwd: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        **_: Any,
    ):
        self.api_key = api_key or "copilot-acp"
        self.base_url = base_url or ACP_MARKER_BASE_URL
        self._runtime_defaults = _runtime_defaults(self.base_url)
        self._default_headers = dict(default_headers or {})
        self._acp_command = acp_command or command or _resolve_command(self.base_url)
        self._acp_args = list(acp_args or args or _resolve_args(self.base_url))
        self._acp_cwd = str(Path(acp_cwd or os.getcwd()).resolve())
        self.chat = _ACPChatNamespace(self)
        self.is_closed = False
        self._active_process: subprocess.Popen[str] | None = None
        self._active_process_lock = threading.Lock()

    def close(self) -> None:
        proc: subprocess.Popen[str] | None
        with self._active_process_lock:
            proc = self._active_process
            self._active_process = None
        self.is_closed = True
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        **_: Any,
    ) -> Any:
        prompt_text = _format_messages_as_prompt(
            messages or [],
            model=model,
            tools=tools,
            tool_choice=tool_choice,
        )
        # Normalise timeout: run_agent.py may pass an httpx.Timeout object
        # (used natively by the OpenAI SDK) rather than a plain float.
        if timeout is None:
            _effective_timeout = _DEFAULT_TIMEOUT_SECONDS
        elif isinstance(timeout, (int, float)):
            _effective_timeout = float(timeout)
        else:
            # httpx.Timeout or similar — pick the largest component so the
            # subprocess has enough wall-clock time for the full response.
            _candidates = [
                getattr(timeout, attr, None)
                for attr in ("read", "write", "connect", "pool", "timeout")
            ]
            _numeric = [float(v) for v in _candidates if isinstance(v, (int, float))]
            _effective_timeout = max(_numeric) if _numeric else _DEFAULT_TIMEOUT_SECONDS

        response_text, reasoning_text, raw_tool_calls = self._run_prompt(
            prompt_text,
            timeout_seconds=_effective_timeout,
        )

        # Merge structured event-based tool calls with XML-block-parsed ones.
        xml_tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)
        event_tool_calls = _convert_opencode_tool_calls(raw_tool_calls)
        tc_ids = {tc.id for tc in event_tool_calls} if event_tool_calls else set()
        tool_calls = list(event_tool_calls) + [tc for tc in xml_tool_calls if tc.id not in tc_ids]

        usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        assistant_message = SimpleNamespace(
            content=cleaned_text,
            tool_calls=tool_calls,
            reasoning=reasoning_text or None,
            reasoning_content=reasoning_text or None,
            reasoning_details=None,
        )
        finish_reason = "tool_calls" if tool_calls else "stop"
        choice = SimpleNamespace(message=assistant_message, finish_reason=finish_reason)
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=model or "copilot-acp",
        )

    def _run_prompt(self, prompt_text: str, *, timeout_seconds: float) -> tuple[str, str, list[dict[str, Any]]]:
        try:
            proc = subprocess.Popen(
                [self._acp_command] + self._acp_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self._acp_cwd,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start {self._runtime_defaults['label']} command '{self._acp_command}'. "
                f"{self._runtime_defaults['install_hint']}"
            ) from exc

        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise RuntimeError(f"{self._runtime_defaults['label']} process did not expose stdin/stdout pipes.")

        self.is_closed = False
        with self._active_process_lock:
            self._active_process = proc

        inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=40)

        def _stdout_reader() -> None:
            for line in proc.stdout:
                try:
                    inbox.put(json.loads(line))
                except Exception:
                    inbox.put({"raw": line.rstrip("\n")})

        def _stderr_reader() -> None:
            if proc.stderr is None:
                return
            for line in proc.stderr:
                stderr_tail.append(line.rstrip("\n"))

        out_thread = threading.Thread(target=_stdout_reader, daemon=True)
        err_thread = threading.Thread(target=_stderr_reader, daemon=True)
        out_thread.start()
        err_thread.start()

        next_id = 0

        def _request(method: str, params: dict[str, Any], *, text_parts: list[str] | None = None, reasoning_parts: list[str] | None = None, tool_calls: list[dict[str, Any]] | None = None) -> Any:
            nonlocal next_id
            next_id += 1
            request_id = next_id
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    msg = inbox.get(timeout=0.1)
                except queue.Empty:
                    continue

                if self._handle_server_message(
                    msg,
                    process=proc,
                    cwd=self._acp_cwd,
                    text_parts=text_parts,
                    reasoning_parts=reasoning_parts,
                    tool_calls=tool_calls,
                ):
                    continue

                if msg.get("id") != request_id:
                    continue
                if "error" in msg:
                    err = msg.get("error") or {}
                    raise RuntimeError(
                        f"Copilot ACP {method} failed: {err.get('message') or err}"
                    )
                return msg.get("result")

            stderr_text = "\n".join(stderr_tail).strip()
            if proc.poll() is not None and stderr_text:
                raise RuntimeError(f"Copilot ACP process exited early: {stderr_text}")
            raise TimeoutError(f"Timed out waiting for Copilot ACP response to {method}.")

        try:
            _request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {
                            "readTextFile": True,
                            "writeTextFile": True,
                        }
                    },
                    "clientInfo": {
                        "name": "hermes-agent",
                        "title": "Hermes Agent",
                        "version": "0.0.0",
                    },
                },
            )
            session = _request(
                "session/new",
                {
                    "cwd": self._acp_cwd,
                    "mcpServers": [],
                },
            ) or {}
            session_id = str(session.get("sessionId") or "").strip()
            if not session_id:
                raise RuntimeError("Copilot ACP did not return a sessionId.")

            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            _raw_tool_calls: list[dict[str, Any]] = []
            _request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [
                        {
                            "type": "text",
                            "text": prompt_text,
                        }
                    ],
                },
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
                tool_calls=_raw_tool_calls,
            )
            return "".join(text_parts), "".join(reasoning_parts), _raw_tool_calls
        finally:
            self.close()

    def _handle_server_message(
        self,
        msg: dict[str, Any],
        *,
        process: subprocess.Popen[str],
        cwd: str,
        text_parts: list[str] | None,
        reasoning_parts: list[str] | None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> bool:
        method = msg.get("method")
        if not isinstance(method, str):
            return False

        if method == "session/update":
            params = msg.get("params") or {}
            update = params.get("update") or {}
            kind = str(update.get("sessionUpdate") or "").strip()
            content = update.get("content") or {}
            chunk_text = ""
            if isinstance(content, dict):
                chunk_text = str(content.get("text") or "")
            if kind == "agent_message_chunk" and chunk_text and text_parts is not None:
                text_parts.append(chunk_text)
            elif kind == "agent_thought_chunk" and chunk_text and reasoning_parts is not None:
                reasoning_parts.append(chunk_text)

            # OpenCode ACP sends tool calls as structured events, not XML blocks.
            elif kind == "tool_call" and tool_calls is not None:
                _tc = {
                    "id": str(update.get("toolCallId") or ""),
                    "name": str(update.get("title") or ""),
                    "kind": str(update.get("kind") or ""),
                    "status": str(update.get("status") or ""),
                    "rawInput": dict(update.get("rawInput") or {}),
                }
                tool_calls.append(_tc)

            elif kind == "tool_call_update" and tool_calls is not None:
                _tc_id = str(update.get("toolCallId") or "")
                for entry in tool_calls:
                    if entry.get("id") == _tc_id:
                        entry["status"] = str(update.get("status") or entry.get("status", ""))
                        entry.update({
                            "title": str(update.get("title") or entry.get("title", "")),
                            "kind": str(update.get("kind") or entry.get("kind", "")),
                            "rawInput": dict(update.get("rawInput") or entry.get("rawInput", {})),
                        })
                        if str(update.get("status")) == "completed" and isinstance(content, list):
                            result_texts = []
                            for item in content:
                                if isinstance(item, dict):
                                    inner = item.get("content", {})
                                    if isinstance(inner, dict) and inner.get("type") == "text":
                                        result_texts.append(str(inner.get("text", "")))
                            entry["result"] = "\n".join(t for t in result_texts if t)

            return True

        if process.stdin is None:
            return True

        message_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "session/request_permission":
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    "outcome": {
                        "outcome": "allow_once",
                    }
                },
            }
        elif method == "fs/read_text_file":
            try:
                path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd)
                content = path.read_text() if path.exists() else ""
                line = params.get("line")
                limit = params.get("limit")
                if isinstance(line, int) and line > 1:
                    lines = content.splitlines(keepends=True)
                    start = line - 1
                    end = start + limit if isinstance(limit, int) and limit > 0 else None
                    content = "".join(lines[start:end])
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "content": content,
                    },
                }
            except Exception as exc:
                response = _jsonrpc_error(message_id, -32602, str(exc))
        elif method == "fs/write_text_file":
            try:
                path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(params.get("content") or ""))
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": None,
                }
            except Exception as exc:
                response = _jsonrpc_error(message_id, -32602, str(exc))
        else:
            response = _jsonrpc_error(
                message_id,
                -32601,
                f"ACP client method '{method}' is not supported by Hermes yet.",
            )

        process.stdin.write(json.dumps(response) + "\n")
        process.stdin.flush()
        return True
