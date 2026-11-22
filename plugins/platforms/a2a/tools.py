"""A2A client tools (``a2a`` toolset): a2a_discover/call/list/history/orchestrate talk to *other*
agents. Peers come from config.yaml ``a2a_agents: {name: {url, auth: {type: bearer, token}, timeout,
capabilities}}``. Stdlib urllib; wire format is A2A v1.0 ``SendMessage`` (v0.3 replies still parse)."""

from __future__ import annotations

import contextlib
import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from gateway.platforms._shared import coerce_port as _coerce_int, get_scoped_secret as _get_scoped_secret

from . import protocol, security

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120
_ORCHESTRATE_MAX_WORKERS = 6  # max parallel peers for fan-out


def _load_config() -> dict:
    """Read-only view of config.yaml; peers are only read, never mutated (cache-safe)."""
    from hermes_cli.config import load_config_readonly
    return load_config_readonly() or {}


def _configured_peers() -> dict:
    return _load_config().get("a2a_agents") or {}


def _peer_from_entry(entry: dict, **extra: Any) -> dict:
    return {"url": entry.get("url", ""), "auth": entry.get("auth", {}) or {},
            "timeout": int(entry.get("timeout", _DEFAULT_TIMEOUT)), **extra}


def _resolve_peer(agent: str) -> Optional[dict]:
    """Peer name -> {url, auth, timeout, capabilities, tenant}, or treat ``agent`` as a URL."""
    if agent.startswith(("http://", "https://")):
        return {"url": agent, "auth": {}, "timeout": _DEFAULT_TIMEOUT, "capabilities": []}
    entry = _configured_peers().get(agent)
    return _peer_from_entry(entry, capabilities=entry.get("capabilities", []) or [], tenant=entry.get("tenant", "")) if entry else None


def _auth_header(auth: dict) -> dict:
    return {"Authorization": f"Bearer {auth['token']}"} if auth and auth.get("type") == "bearer" and auth.get("token") else {}


def _http_json(url: str, headers: dict, timeout: int, method: str, data: Optional[bytes] = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_json(url: str, headers: dict, timeout: int) -> dict:
    return _http_json(url, headers, timeout, "GET")


def _http_post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    hdrs = {"Content-Type": "application/json", "A2A-Version": protocol.PROTOCOL_VERSION, **headers}
    return _http_json(url, hdrs, timeout, "POST", json.dumps(body).encode("utf-8"))


def _http_post_json_conditional(url: str, body: dict, headers: dict, timeout: int,
                                etag: str = "") -> tuple[Optional[dict], str, bool]:
    """[本地补丁 v3] 发一个 JSON POST，可选带 ``If-None-Match`` 做条件请求。

    返回 ``(payload, 新 etag, not_modified)``。``not_modified=True`` 表示对端回 HTTP 304 且
    没有响应体 —— DSH 的 ``tasks/get`` 对已终态任务就是这样省流量的（实测 30 次轮询
    30 MB → 0 B）。对端不支持 ETag 时行为与普通 POST 完全一致（回 200 + 全量 body）。
    """
    hdrs = {"Content-Type": "application/json", "A2A-Version": protocol.PROTOCOL_VERSION, **headers}
    if etag:
        hdrs["If-None-Match"] = etag
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            new_etag = resp.headers.get("ETag") or ""
            return (json.loads(raw.decode("utf-8")) if raw else None), new_etag, False
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, (e.headers.get("ETag") if e.headers else "") or etag, True
        raise



def _fetch_card(base_url: str, headers: dict, timeout: int) -> dict:
    """GET the v1.0 agent-card.json; on 404 fall back to the v0.2 agent.json alias."""
    base = base_url.rstrip("/")
    try:
        return _http_get_json(base + "/.well-known/agent-card.json", headers, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    return _http_get_json(base + "/.well-known/agent.json", headers, timeout)


def _select_jsonrpc_interface(card: Optional[dict]) -> Optional[dict]:
    if isinstance(card, dict):
        for iface in card.get("supportedInterfaces", []) or []:
            if isinstance(iface, dict) and iface.get("protocolBinding") == "JSONRPC" and iface.get("url"):
                return iface
    return None


def _rpc_url(base_url: str, card: Optional[dict]) -> str:
    """Card's JSONRPC interface (v1.0 supportedInterfaces) > card's legacy top-level url > base."""
    if iface := _select_jsonrpc_interface(card):
        return str(iface["url"])
    if isinstance(card, dict) and isinstance(card.get("url"), str) and card["url"]:
        return card["url"]
    return base_url.rstrip("/")


def _send_message(agent_label: str, peer: dict, message: str, context_id: str,
                  *, return_immediately: bool = False) -> dict:
    """One SendMessage to a peer -> ``{reply, context_id, state, task_id}``. Raises urllib
    errors / ValueError for the caller to format; handles redaction, audit, persistence, metrics.

    ``return_immediately`` sends A2A ``configuration.returnImmediately: true``: a peer that
    supports async dispatch answers at once with a pending task (submitted/working) instead of
    blocking on the whole turn; poll it with ``tasks/get``. Peers that ignore the hint simply
    reply synchronously, so this degrades gracefully.
    """
    base_url = peer.get("url", "")
    headers = _auth_header(peer.get("auth", {}) or {})
    timeout = int(peer.get("timeout", _DEFAULT_TIMEOUT))
    try:
        card = _fetch_card(base_url, headers, min(timeout, 30))  # best-effort, to learn the rpc URL
    except Exception:
        card = None
    ctx = context_id or protocol.new_context_id()
    safe_message = security.redact_outbound(message)
    # v1.0: contextId lives inside the Message, not at the params top level.
    params: dict[str, Any] = {"message": protocol.text_message(protocol.ROLE_USER, safe_message, context_id=ctx)}
    if return_immediately:
        params["configuration"] = {"returnImmediately": True}
    rpc_body = {"jsonrpc": "2.0", "id": protocol.new_task_id(), "method": "SendMessage", "params": params}
    iface = _select_jsonrpc_interface(card)
    tenant = str(iface["tenant"]) if iface and iface.get("tenant") else str(peer.get("tenant") or "")
    if tenant:
        rpc_body["params"]["tenant"] = tenant
    security.audit("outbound", agent_label, rpc_body["id"], safe_message)
    protocol.persist_message(ctx, "user", safe_message, rpc_body["id"])
    protocol.metrics.outbound_total += 1
    resp = _http_post_json(_rpc_url(base_url, card), rpc_body, headers, timeout)
    if "error" in resp:
        raise ValueError(_rpc_error_text(agent_label, resp.get("error") or {}, "returned an error"))
    payload = protocol.unwrap_send_message_response(resp.get("result", {}))
    reply = _reply_text_from_result(payload)
    reply_ctx, state, task_id = ctx, "", ""
    if isinstance(payload, dict):
        reply_ctx = payload.get("contextId") or ctx
        state = (payload.get("status") or {}).get("state", "")
        task_id = str(payload.get("id") or "")
    if reply:
        protocol.persist_message(reply_ctx, "agent", reply, rpc_body["id"])
        protocol.metrics.inbound_total += 1
    return {"reply": reply, "context_id": reply_ctx, "state": state, "task_id": task_id}


def _send_task(agent_label: str, peer: dict, message: str, context_id: str) -> tuple[str, str, str]:
    """Back-compat synchronous wrapper -> (reply_text, context_id, state)."""
    out = _send_message(agent_label, peer, message, context_id)
    return out["reply"], out["context_id"], out["state"]



def _reply_text_from_result(result: Any) -> str:
    result = protocol.unwrap_send_message_response(result)
    if not isinstance(result, dict):
        return str(result)
    # Artifacts first (final output), then status message (interim/clarify), else bare Message.
    for artifact in result.get("artifacts", []) or []:
        txt = protocol.extract_text(artifact)
        if txt:
            return txt
    return protocol.extract_text((result.get("status", {}) or {}).get("message") or result)


_AUTH_ERR = "Error: peer '{agent}' rejected auth (HTTP {code}). Check the configured token."
_HTTP_CALL_ERRORS = {401: _AUTH_ERR, 403: _AUTH_ERR, 429: "Error: peer '{agent}' rate limited us (HTTP 429). Retry later."}

# [本地补丁 v3] DSH 侧自定义错误码（README §3 / §4.9）→ 一句「为什么 + 下一步怎么做」。
#   纯文案增强：码本身照旧原样显示，不改变任何请求、重试或降级行为。
#   ★ -32001/-32002 是**方言冲突位**：A2A 规范是 TaskNotFound / TaskNotCancelable
#     （本仓库 inbound 也按规范发，见 protocol.py:38 与 adapter.py），DSH 侧却把它们
#     定义成「鉴权失败 / 找不到 taskId」。所以这两条要两个语义都说，否则拿 Hermes 自己
#     或任何 spec-conformant 第三方当对端时，会输出「去查 token」这种误导排障建议。
_RPC_ERROR_HINTS: dict[int, str] = {
    -32001: "dialect-dependent: auth failed on DSH-style peers, task-not-found on A2A-spec peers — "
            "check the token AND whether that task id still exists.",
    -32002: "dialect-dependent: unknown task id on DSH-style peers, task-not-cancelable on A2A-spec peers.",
    -32003: "that task already reached a terminal state — nothing to cancel.",
    -32010: "the peer's run failed; error.data.contextId still continues that exchange.",
    -32011: "the peer's turn hit its own timeout (turnTimeoutMs, ~15 min) — split the work into smaller turns.",
    -32012: "the peer's async concurrency limit is full — wait for a slot, or use another peer.",
    -32013: "one task per context — poll the running one with a2a_task, or pass new_context=true.",
    -32014: "the peer's global foreground quota is full — retry in a moment.",
    -32015: "too many queued requests on that context — let the running one finish first.",
    -32016: "the peer's task table is full — retry in a moment.",
    -32601: "that peer does not implement this JSON-RPC method.",
}


def _rpc_error_text(agent_label: str, err: dict, action: str) -> str:
    """[本地补丁 v3] 把 JSON-RPC error 对象渲染成一行可读文本（含错误码与处置建议）。"""
    err = err or {}
    code = err.get("code", "?")
    message = err.get("message", err)
    text = f"Peer '{agent_label}' {action}: {message} (code {code})"
    hint = _RPC_ERROR_HINTS.get(code) if isinstance(code, int) else None
    if hint:
        text += f" — {hint}"
    data = err.get("data")
    if isinstance(data, dict) and data.get("contextId"):
        text += f" [context {data['contextId']}]"
    return text

def a2a_discover(args: dict, **_: Any) -> str:
    """Fetch and summarize the Agent Card at ``url``."""
    url = str(args.get("url") or "").strip()
    if not url:
        return "Error: 'url' is required (e.g. http://localhost:9999)."
    try:
        card = _fetch_card(url, {}, _DEFAULT_TIMEOUT)
    except urllib.error.HTTPError as e:
        return f"Error: discovery failed — HTTP {e.code} from {url}."
    except Exception as e:
        return f"Error: could not reach {url} — {e}."
    caps = card.get("capabilities", {}) or {}
    skills = card.get("skills", []) or []
    auth = "yes" if card.get("security") else "no"
    proto = ", ".join(
        f"{i.get('protocolBinding', '?')} v{i.get('protocolVersion', '?')}"
        for i in (card.get("supportedInterfaces", []) or []) if isinstance(i, dict)
    ) or f"v{card.get('protocolVersion', '?')} (pre-1.0 card)"
    lines = [f"Agent: {card.get('name', '?')}", f"Description: {card.get('description', '')}", f"URL: {_rpc_url(url, card)}",
             f"Protocol: {proto}",
             f"Streaming: {bool(caps.get('streaming'))}  Push: {bool(caps.get('pushNotifications'))}  Auth required: {auth}",
             f"Skills ({len(skills)}):"]
    lines.extend(f"  - {s.get('name', s.get('id', '?'))}: {s.get('description', '')}" for s in skills[:20])
    return "\n".join(lines)


_TRUE_FLAGS = frozenset({"1", "true", "yes", "y", "on"})
_TERMINAL_SHORT = frozenset({"completed", "failed", "canceled", "cancelled", "rejected"})
# States that will not change by waiting: the peer needs something from the caller (or auth).
_PAUSED_SHORT = frozenset({"input-required", "auth-required"})
_POLL_INTERVAL, _MAX_POLL_WAIT = 2, 600


def _as_bool(value: Any) -> bool:
    """Tolerant flag parse — models pass true/\"true\"/1/\"yes\" interchangeably."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in _TRUE_FLAGS


def _flag(args: dict, *names: str) -> bool:
    """First present alias wins (``background=True`` and ``return_immediately: false`` both work)."""
    for name in names:
        if name in args and args[name] is not None:
            return _as_bool(args[name])
    return False


def _state_short(state: str) -> str:
    """TASK_STATE_INPUT_REQUIRED / input-required / input_required -> 'input-required'."""
    return (state or "").replace("TASK_STATE_", "").replace("_", "-").lower()


def a2a_call(args: dict, **_: Any) -> str:
    """Send a task to a peer (configured name or direct URL); ``context_id`` continues a prior exchange.

    Continuity defaults: with no ``context_id`` the call reuses the context last used with that
    peer (kept in ``a2a_last_context.json``), so a peer remembers the running project. Pass
    ``new_context`` to force a fresh exchange. ``return_immediately`` asks a capable peer to
    accept the task and return at once; poll it with ``a2a_task``.
    """
    # Accept common aliases models reach for (observed live: 'agent_name').
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    message = str(args.get("message") or args.get("text") or args.get("task") or "").strip()
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    return_immediately = _flag(args, "return_immediately", "returnImmediately", "immediate",
                               "background", "async_mode", "async")
    new_context = _flag(args, "new_context", "newContext", "fresh_context")
    if not agent or not message:
        return "Error: both 'agent' and 'message' are required."
    peer = _resolve_peer(agent)
    if not peer or not peer.get("url"):
        return f"Error: unknown agent '{agent}'. Configure it under 'a2a_agents' in config.yaml or pass a full http(s):// URL."
    reused = False
    if not context_id and not new_context:
        if remembered := protocol.load_last_context(agent):
            context_id, reused = remembered, True
    try:
        out = _send_message(agent, peer, message, context_id, return_immediately=return_immediately)
    except urllib.error.HTTPError as e:
        return _HTTP_CALL_ERRORS.get(e.code, "Error: call to '{agent}' failed — HTTP {code}.").format(agent=agent, code=e.code)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error: call to '{agent}' failed — {e}."
    reply_ctx, state, task_id = out["context_id"], out["state"], out["task_id"]
    try:
        protocol.save_last_context(agent, reply_ctx)  # next bare call continues this exchange
    except Exception:  # a corrupt bookkeeping file must never mask a successful call
        logger.warning("A2A: could not remember the last context for %s", agent, exc_info=True)
    short = _state_short(state)
    header = f"[{agent}" + (f" · task {task_id}" if task_id else "") + f" · context {reply_ctx}" \
             + (f" · {short}" if short else "") + (" · continued" if reused else "") + "]"
    body = out["reply"] or "(no text reply)"
    if short in _PAUSED_SHORT or state == protocol.STATE_INPUT_REQUIRED:
        body += f"\n\n(The peer needs more input — answer by calling a2a_call again with context_id '{reply_ctx}'.)"
    elif return_immediately and task_id and short not in _TERMINAL_SHORT:
        body = (out["reply"] or "(accepted — the peer runs this in the background; this call did not wait)") \
               + f"\n\nPoll for the result with a2a_task(agent='{agent}', task_id='{task_id}', wait=120)."
    elif return_immediately and not task_id:
        body = "(the peer answered synchronously and ignored returnImmediately)\n" + body
    elif short and short not in _TERMINAL_SHORT and task_id:
        body += f"\n\n(Task {task_id} is still {short} — poll with a2a_task(agent='{agent}', task_id='{task_id}', wait=120).)"
    return f"{header}\n{body}"


# --------------------------------------------------------------------------
# tasks/get — poll a task an async SendMessage accepted (or any live task id)
# --------------------------------------------------------------------------


def _peer_endpoint(peer: dict) -> tuple[str, dict, int]:
    """(jsonrpc url, auth headers, timeout) for a peer — the card is fetched once per call site."""
    base_url = peer.get("url", "")
    headers = _auth_header(peer.get("auth", {}) or {})
    timeout = int(peer.get("timeout", _DEFAULT_TIMEOUT))
    try:
        card = _fetch_card(base_url, headers, min(timeout, 30))
    except Exception:
        card = None
    return _rpc_url(base_url, card), headers, timeout


def _get_task(agent_label: str, rpc_url: str, headers: dict, timeout: int, task_id: str,
              etag: str = "") -> tuple[dict, str, bool]:
    """One ``tasks/get`` round-trip -> ``(task, etag, not_modified)``. Raises for the caller.

    [本地补丁 v3] ``etag`` 非空时带 ``If-None-Match``：DSH 对已终态任务会回 HTTP 304 + 空 body，
    此时 ``not_modified=True`` 且 ``task`` 为空 —— 调用方沿用上一次读到的 Task 即可。
    """
    # v1.0 canonical method is ``GetTask``; ``tasks/get`` is the widely-implemented slash alias.
    # Try the alias first and fall back on METHOD_NOT_FOUND so either kind of peer answers.
    for method in ("tasks/get", "GetTask"):
        body = {"jsonrpc": "2.0", "id": protocol.new_task_id(), "method": method,
                "params": {"id": task_id, "taskId": task_id}}
        resp, new_etag, not_modified = _http_post_json_conditional(
            rpc_url, body, headers, max(10, min(timeout, 60)), etag)
        if not_modified:
            return {}, new_etag or etag, True
        resp = resp or {}
        if "error" in resp:
            err = resp["error"] or {}
            if method == "tasks/get" and err.get("code") == protocol.ERR_METHOD_NOT_FOUND:
                continue
            raise ValueError(_rpc_error_text(agent_label, err, f"could not return task {task_id}"))
        task = protocol.unwrap_send_message_response(resp.get("result") or {})
        return (task if isinstance(task, dict) else {}), new_etag, False
    return {}, etag, False


def _poll_task(agent_label: str, peer: dict, task_id: str, wait: int, interval: int) -> dict:
    """Poll ``tasks/get`` until terminal, or ``wait`` seconds elapse (0 = single read)."""
    rpc_url, headers, timeout = _peer_endpoint(peer)  # resolve the card once, not per poll
    deadline = time.monotonic() + max(0, wait)
    task: dict = {}
    etag = ""  # [本地补丁 v3] 终态 Task 带稳定 ETag；命中 304 就不再拉那份 1 MB body
    while True:
        # 请求前先看 deadline：否则边界上会多发一次空转（wait=3/interval=1 实测 4 次而非 3 次）。
        if task and wait > 0 and time.monotonic() >= deadline:
            return task
        fresh, etag, not_modified = _get_task(agent_label, rpc_url, headers, timeout, task_id, etag)
        if not_modified:
            # 对端说「没变」：沿用上一次的 Task（首次轮询不会 304，因为还没带 etag）。
            if not task:
                raise ValueError(f"Peer '{agent_label}' answered 304 Not Modified for task {task_id} "
                                 "before any full payload had been read — retry the poll.")
        elif fresh:
            task = fresh
        state = _state_short((task.get("status") or {}).get("state", ""))
        # input-required/auth-required never resolve by waiting — report them immediately.
        if wait <= 0 or state in _TERMINAL_SHORT or state in _PAUSED_SHORT:
            return task
        # ★ deadline 判断放在 sleep 前：原写法把 min(interval, ...) 夹了 0.2s 下限，
        #   在 wait=3/interval=1 的边界上会越过 deadline 再发一次空转请求（实测 4 次而非 3 次）。
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return task
        time.sleep(min(interval, remaining))


def _render_task(agent_label: str, task: dict, waited: int = 0) -> str:
    """Human-readable view of a tasks/get payload."""
    if not task:
        return f"[{agent_label}] (no task payload returned)"
    task_id = str(task.get("id") or "?")
    context_id = str(task.get("contextId") or "")
    state = _state_short((task.get("status") or {}).get("state", ""))
    header = f"[{agent_label} · task {task_id}" + (f" · context {context_id}" if context_id else "") \
             + (f" · {state}" if state else "") + "]"
    text = _reply_text_from_result(task)
    body = text or ("(no reply text yet)" if state not in _TERMINAL_SHORT else "(no text reply)")
    if state in _PAUSED_SHORT:
        body += (f"\n\n(The peer is waiting for you — answer with a2a_call(agent='{agent_label}', message=..., "
                 + (f"context_id='{context_id}')" if context_id else "new_context=false)") + ".)")
    elif state and state not in _TERMINAL_SHORT:
        timing = f"Waited {waited}s. " if waited else ""
        body += (f"\n\n(Still {state}. {timing}"
                 f"Poll again with a2a_task(agent='{agent_label}', task_id='{task_id}', wait=120).)")
    return f"{header}\n{body}"


def a2a_task(args: dict, **_: Any) -> str:
    """Read or poll one task over ``tasks/get`` (the companion of ``return_immediately``)."""
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    task_id = str(args.get("task_id") or args.get("taskId") or args.get("id") or "").strip()
    if not agent or not task_id:
        return "Error: both 'agent' and 'task_id' are required."
    wait = max(0, min(_coerce_int(args.get("wait") or 0, 0), _MAX_POLL_WAIT))
    interval = max(1, min(_coerce_int(args.get("interval") or _POLL_INTERVAL, _POLL_INTERVAL), 30))
    peer = _resolve_peer(agent)
    if not peer or not peer.get("url"):
        return f"Error: unknown agent '{agent}'. Configure it under 'a2a_agents' in config.yaml or pass a full http(s):// URL."
    try:
        task = _poll_task(agent, peer, task_id, wait, interval)
    except urllib.error.HTTPError as e:
        return _HTTP_CALL_ERRORS.get(e.code, "Error: tasks/get on '{agent}' failed — HTTP {code}.").format(agent=agent, code=e.code)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error: tasks/get on '{agent}' failed — {e}."
    return _render_task(agent, task, waited=wait)



def a2a_cancel(args: dict, **_: Any) -> str:
    """Ask a peer to cancel a running task over ``tasks/cancel``.

    Companion of ``a2a_call(return_immediately=true)``: without this, a task that runs away
    can only be waited out (DSH's own turn timeout is ~15 min). Cancellation is *best effort* —
    the peer decides; a task already in a terminal state is reported as such, not as an error.

    Tries the A2A 1.0 canonical ``CancelTask`` after the widely-implemented slash alias
    ``tasks/cancel``, mirroring how ``tasks/get`` handles the two dialects.
    """
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    task_id = str(args.get("task_id") or args.get("taskId") or args.get("id") or "").strip()
    if not agent or not task_id:
        return "Error: both 'agent' and 'task_id' are required."
    peer = _resolve_peer(agent)
    if not peer or not peer.get("url"):
        return (f"Error: unknown agent '{agent}'. Configure it under 'a2a_agents' in config.yaml "
                "or pass a full http(s):// URL.")
    rpc_url, headers, timeout = _peer_endpoint(peer)
    call_timeout = max(10, min(timeout, 60))

    last_err = None
    for method in ("tasks/cancel", "CancelTask"):
        body = {"jsonrpc": "2.0", "id": protocol.new_task_id(), "method": method,
                "params": {"id": task_id, "taskId": task_id}}
        try:
            resp = _http_post_json(rpc_url, body, headers, call_timeout)
        except urllib.error.HTTPError as e:
            return _HTTP_CALL_ERRORS.get(
                e.code, "Error: tasks/cancel on '{agent}' failed — HTTP {code}."
            ).format(agent=agent, code=e.code)
        except Exception as e:
            return f"Error: tasks/cancel on '{agent}' failed — {e}."

        if "error" in resp:
            err = resp.get("error") or {}
            code = err.get("code")
            # 方法不存在：换下一个方言重试，别直接失败
            if method == "tasks/cancel" and code == protocol.ERR_METHOD_NOT_FOUND:
                last_err = err
                continue
            # ★ 已终态不是错误：任务本来就不用取消了，如实回报即可
            if code == -32003:
                return (f"Task {task_id} on '{agent}' already reached a terminal state — "
                        "nothing to cancel.")
            raise ValueError(_rpc_error_text(agent, err, f"could not cancel task {task_id}"))

        task = protocol.unwrap_send_message_response(resp.get("result") or {})
        task = task if isinstance(task, dict) else {}
        state = _state_short((task.get("status") or {}).get("state", ""))
        head = f"Cancelled task {task_id} on '{agent}'" + (f" (state: {state})" if state else "")
        body_text = _render_task(agent, task) if task else ""
        return (head + "\n\n" + body_text) if body_text else head

    if last_err:
        raise ValueError(_rpc_error_text(agent, last_err, f"could not cancel task {task_id}"))
    return (f"Error: peer '{agent}' does not implement tasks/cancel (no method alias answered). "
            "The task will keep running until the peer's own turn timeout.")


def a2a_reset(args: dict, **_: Any) -> str:
    """Forget the remembered conversation so the next ``a2a_call`` starts a fresh exchange.

    Why this exists: without it the only way to start over is ``a2a_call(new_context=true)``,
    which leaves the previous context registered on the peer — every reset burns one of the
    peer's ``maxSessions`` slots until it idles out.

    What this can and cannot do (be honest about the second half):

    - **Can**: drop the local ``a2a_last_context.json`` mapping, so a bare ``a2a_call`` no
      longer silently continues the old exchange.
    - **Cannot**: force the peer to free its session. A2A has no "close context" RPC — only
      ``tasks/cancel`` for running tasks. The peer reclaims the slot on its own idle timeout
      (DSH: ``sessionIdleTimeoutMs`` = 30 min). Report that plainly instead of implying the
      slot is freed immediately.
    """
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()

    # ① 先试着让对端真正释放会话。
    #    A2A 协议本身没有"关闭 context"的 RPC，但 DSH 的 a2a-server 自写 fork 提供了
    #    非标准扩展 ResetContext：取消该 context 下所有任务 → 排空 → 重置 bridge session，
    #    返回 {canceled:[taskId...], forced:n, dropped}。这比自己造 new_context 更彻底。
    #    对端没有这个方法（-32601）就静默跳过，退回纯本地清理。
    peer_released = ""
    if agent:
        peer = _resolve_peer(agent)
        if peer and peer.get("url"):
            target_ctx = context_id or protocol.load_last_context(agent) or ""
            if target_ctx:
                try:
                    rpc_url, headers, timeout = _peer_endpoint(peer)
                    body = {"jsonrpc": "2.0", "id": protocol.new_task_id(), "method": "ResetContext",
                            "params": {"contextId": target_ctx}}
                    resp = _http_post_json(rpc_url, body, headers, max(10, min(timeout, 60)))
                    err = resp.get("error") or {}
                    if not err:
                        got = resp.get("result") or {}
                        canceled = got.get("canceled") or []
                        forced = got.get("forced")
                        peer_released = (f"Peer released context {target_ctx}"
                                         f" (canceled {len(canceled)} task(s)"
                                         + (f", {forced} forced" if forced else "") + ").")
                except Exception:
                    # 释放是加分项，失败不能盖掉本地清理的结果。
                    peer_released = ""

    cleared = protocol.clear_last_context(agent)
    if not cleared and not peer_released:
        scope = f"peer '{agent}'" if agent else "any peer"
        return (f"Nothing to reset — no remembered context for {scope}.\n"
                "Note: this only clears the LOCAL continuation mapping; a peer-side session "
                "(if any) still follows its own idle timeout.")

    # 回报用调用者给的原始名字（_peer_key 会转小写，直接回显 cleared 会显示成 peera）
    shown = agent or ", ".join(cleared)
    lines = []
    if peer_released:
        lines.append(peer_released)
    if cleared:
        lines.append(f"Cleared the remembered context for: {shown}.")
        lines.append("The next bare a2a_call will start a fresh exchange (no need to pass new_context).")
    if context_id:
        lines.append(f"Local transcript history for context '{context_id}' is untouched — "
                     "it is read-only via a2a_history.")
    if not peer_released:
        lines.append("Peer-side session slots are NOT freed by this call: A2A has no 'close "
                     "context' RPC, so the peer reclaims them on its own idle timeout "
                     "(DSH: 30 minutes).")
    return "\n".join(lines)


def a2a_list_tasks(args: dict, **_: Any) -> str:
    """List a peer's tasks over ``tasks/list`` (A2A v1.0) — filtering and pagination included.

    Companion of ``a2a_call(return_immediately=true)``: an async task id you failed to note
    down is otherwise unrecoverable unless you remember the context id. ``a2a_list`` lists
    peers and persisted conversations, not tasks — this is the missing half.

    Filters: ``context_id`` (one conversation) and ``status`` (working / completed / ...).
    Pagination: ``page_size`` (max 100) + ``page_token`` (pass back the returned next token).

    Falls back to the A2A 1.0 canonical ``ListTasks`` when a peer only implements that name,
    mirroring how ``tasks/get`` and ``tasks/cancel`` handle the two dialects.
    """
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    if not agent:
        return "Error: 'agent' is required (a configured peer name or a full http(s):// URL)."
    peer = _resolve_peer(agent)
    if not peer or not peer.get("url"):
        return (f"Error: unknown agent '{agent}'. Configure it under 'a2a_agents' in config.yaml "
                "or pass a full http(s):// URL.")
    rpc_url, headers, timeout = _peer_endpoint(peer)
    call_timeout = max(10, min(timeout, 60))

    page_size = max(1, min(_coerce_int(args.get("page_size") or args.get("pageSize") or 20, 20), 100))
    page_token = str(args.get("page_token") or args.get("pageToken") or "").strip()
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    status = str(args.get("status") or args.get("state") or "").strip()

    params: dict[str, Any] = {"pageSize": page_size}
    if page_token:
        params["pageToken"] = page_token
    if context_id:
        params["contextId"] = context_id
    if status:
        params["status"] = status

    last_err = None
    for method in ("tasks/list", "ListTasks"):
        body = {"jsonrpc": "2.0", "id": protocol.new_task_id(), "method": method, "params": params}
        try:
            resp = _http_post_json(rpc_url, body, headers, call_timeout)
        except urllib.error.HTTPError as e:
            return _HTTP_CALL_ERRORS.get(
                e.code, "Error: tasks/list on '{agent}' failed — HTTP {code}."
            ).format(agent=agent, code=e.code)
        except Exception as e:
            return f"Error: tasks/list on '{agent}' failed — {e}."

        if "error" in resp:
            err = resp.get("error") or {}
            if method == "tasks/list" and err.get("code") == protocol.ERR_METHOD_NOT_FOUND:
                last_err = err
                continue
            raise ValueError(_rpc_error_text(agent, err, "could not list tasks"))

        result = resp.get("result") or {}
        tasks = result.get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []
        total = result.get("totalSize")
        next_token = str(result.get("nextPageToken") or "")

        scope = []
        if context_id:
            scope.append(f"context {context_id}")
        if status:
            scope.append(f"status {status}")
        head = f"[{agent} · {len(tasks)} task(s)"
        if isinstance(total, int):
            head += f" of {total}"
        head += (" · " + ", ".join(scope) if scope else "") + "]"
        if not tasks:
            return head + "\n(no tasks matched)"

        lines = [head]
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "?")
            st = _state_short((t.get("status") or {}).get("state", "")) or "?"
            ctx = str(t.get("contextId") or "")
            ts = str((t.get("status") or {}).get("timestamp") or "")
            lines.append(f"  {tid}  · {st}" + (f" · {ctx}" if ctx else "") + (f" · {ts}" if ts else ""))
        if next_token:
            lines.append(f"\nMore available — pass page_token='{next_token}' to continue.")
        return "\n".join(lines)

    if last_err:
        raise ValueError(_rpc_error_text(agent, last_err, "could not list tasks"))
    return f"Error: peer '{agent}' does not implement tasks/list."


def a2a_list(args: dict | None = None, **_: Any) -> str:
    """List configured A2A peers, persisted conversations, and metrics."""
    peers = _configured_peers()
    lines = []
    if peers:
        lines.append(f"Configured peers ({len(peers)}):")
        for name, entry in peers.items():
            caps = entry.get("capabilities", [])
            lines.append(f"  - {name}: {entry.get('url', '?')} (auth: {(entry.get('auth') or {}).get('type', 'none')})"
                         + (f" caps: {', '.join(caps)}" if caps else ""))
    else:
        lines.append("No peers configured. Add them under 'a2a_agents' in config.yaml.")
    if convos := protocol.list_conversations():
        lines.append("")
        lines.append(f"Persisted conversations ({len(convos)}) — recall with a2a_history:")
        lines.extend(f"  - {c}" for c in convos[:25])
    m = protocol.metrics.snapshot()
    lines.append("")
    lines.append(f"Metrics: {m['inbound_total']} in / {m['outbound_total']} out, {m['tasks_completed']} completed, "
                 f"{m['tasks_failed']} failed, {m['streams_started']} streams, {m['push_sent']} push sent, "
                 f"{m['anti_loop_triggers']} anti-loop, {m['rate_limit_triggers']} rate-limited, avg {m['avg_latency_ms']}ms")
    return "\n".join(lines)


def a2a_history(args: dict, **_: Any) -> str:
    """Recall a persisted A2A conversation (survives compaction/restarts)."""
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    if not context_id:
        return "Error: 'context_id' is required (see a2a_list for known conversations)."
    limit = max(1, min(_coerce_int(args.get("limit") or 50, 50), 200))
    messages = protocol.load_conversation(context_id, limit=limit)
    if not messages:
        return f"No persisted conversation for context '{context_id}'."
    lines = [f"Conversation {context_id} (last {len(messages)} messages):"]
    for m in messages:
        text = (m.get("text") or "").strip()
        lines.append(f"[{m.get('role', '?')}] {text[:1000] + ' …[truncated]' if len(text) > 1000 else text}")
    return "\n".join(lines)


def _match_peers_by_capability(capability: str) -> list[tuple[str, dict]]:
    """Configured peers that advertise the capability ('*' matches all)."""
    return [(name, entry) for name, entry in _configured_peers().items()
            if capability in (entry.get("capabilities", []) or []) or capability == "*"]


def _call_peer_sync(agent_name: str, peer_entry: dict, message: str, context_id: str = "") -> tuple[str, str]:
    """Call a single peer synchronously -> (agent_name, reply_text)."""
    try:
        reply, _ctx, _state = _send_task(agent_name, _peer_from_entry(peer_entry), message, context_id)
        return (agent_name, reply or "(no reply)")
    except Exception as e:
        return (agent_name, f"Error: {e}")


def a2a_orchestrate(args: dict, **_: Any) -> str:
    """Fan-out a task to peers matching a capability. Modes: ``all``, ``first`` (first successful),
    ``best`` (longest successful — coarse; use ``all`` to judge yourself)."""
    capability = str(args.get("capability") or "").strip()
    message = str(args.get("message") or args.get("task") or "").strip()
    mode = str(args.get("mode") or "all").strip().lower()
    mode = mode if mode in ("all", "first", "best") else "all"
    context_id = str(args.get("context_id") or "").strip()
    if not message:
        return "Error: 'message' is required."
    if not capability:
        return "Error: 'capability' is required (or use '*' for all peers)."
    if not (matches := _match_peers_by_capability(capability)):
        return f"Error: no configured peers advertise capability '{capability}'."
    results: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(len(matches), _ORCHESTRATE_MAX_WORKERS)) as pool:
        futures = {pool.submit(_call_peer_sync, name, entry, message, context_id): name for name, entry in matches}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results.append(fut.result())
                if mode == "first" and not results[-1][1].startswith("Error:"):
                    for f in futures:  # good reply; cancel peers that haven't started
                        f.cancel()
                    break
            except Exception as e:
                results.append((name, f"Error: {e}"))
    results.sort(key=lambda r: r[0])  # deterministic output
    successes = [(name, reply) for name, reply in results if not reply.startswith("Error:")]
    if mode in ("best", "first"):
        if not successes:
            return "\n".join(["All peers failed:"] + [f"  {name}: {reply}" for name, reply in results])
        name, reply = max(successes, key=lambda r: len(r[1])) if mode == "best" else successes[0]
        return f"[{mode}: {name}]\n{reply}"
    return "\n".join([f"Orchestrated '{capability}' to {len(matches)} peer(s):"]
                     + [line for name, reply in results for line in (f"\n--- {name} ---", reply)])


def _str(description: str) -> dict:
    return {"type": "string", "description": description}


# name -> (handler, description, properties, required)
_TOOLS: dict[str, tuple[Any, str, dict, list[str]]] = {
    "a2a_discover": (a2a_discover,
                     "Fetch a remote agent's A2A Agent Card (name, capabilities, skills) from a URL. "
                     "Use this to find out what a remote agent can do before calling it.",
                     {"url": _str("Base URL of the remote A2A agent, e.g. http://localhost:9999")}, ["url"]),
    "a2a_call": (a2a_call,
                 "Send a natural-language task to a remote A2A agent and return its reply. The agent is a peer "
                 "(any A2A-compliant framework), not a sub-agent you control. With no 'context_id' it reuses "
                 "the context last used with that peer, so the exchange continues (pass new_context=true to "
                 "start fresh). Set 'return_immediately' for long jobs: the peer accepts the task and returns "
                 "a task id at once, which you then poll with a2a_task.",
                 {"agent": _str("Configured peer name (from a2a_agents) or a full http(s):// URL."),
                  "message": _str("The task / message to send the peer, in natural language."),
                  "context_id": _str("Optional: context id from a prior reply, to continue the conversation. "
                                     "Defaults to the context last used with this peer."),
                  "new_context": {"type": "boolean",
                                  "description": "Start a fresh context instead of continuing the last one with this peer."},
                  "return_immediately": {"type": "boolean",
                                         "description": "Async dispatch: return as soon as the peer accepts the task "
                                                        "(submitted/working + task id) instead of waiting for the reply; "
                                                        "poll with a2a_task. Peers without async support answer synchronously."}},
                 ["agent", "message"]),
    "a2a_task": (a2a_task,
                 "Read one A2A task via 'tasks/get' — the companion of a2a_call(return_immediately=true). "
                 "With wait=0 it is a single read; with wait>0 it polls until the task is completed/failed/"
                 "canceled or the wait elapses (max 600s per call; call again to keep waiting).",
                 {"agent": _str("Configured peer name (from a2a_agents) or a full http(s):// URL that owns the task."),
                  "task_id": _str("Task id returned by an async a2a_call (e.g. 'task-1a2b3c')."),
                  "wait": {"type": "integer",
                           "description": "Seconds to poll for a terminal state (default 0 = read once, max 600)."},
                  "interval": {"type": "integer", "description": "Poll interval in seconds (default 2)."}},
                 ["agent", "task_id"]),
    "a2a_cancel": (a2a_cancel,
                   "Cancel a running A2A task via 'tasks/cancel' — the escape hatch for "
                   "a2a_call(return_immediately=true) tasks that ran away. Without it the only "
                   "option is waiting out the peer's turn timeout (~15 min on DSH). Cancellation "
                   "is best effort: a task already in a terminal state is reported as such.",
                   {"agent": _str("Configured peer name (from a2a_agents) or a full http(s):// URL that owns the task."),
                    "task_id": _str("Task id returned by an async a2a_call (e.g. 'task-1a2b3c').")},
                   ["agent", "task_id"]),
    "a2a_reset": (a2a_reset,
                  "Forget the remembered conversation with a peer so the next bare a2a_call starts "
                  "a fresh exchange — replaces a2a_call(new_context=true), which left the previous "
                  "context occupying one of the peer's maxSessions slots. Clears the LOCAL mapping "
                  "only; peer-side sessions follow their own idle timeout (DSH: 30 min).",
                  {"agent": _str("Peer name to reset. Omit to clear every remembered peer."),
                   "context_id": _str("Optional: the context id you believe is stale (for the report only).")},
                  []),
    "a2a_list_tasks": (a2a_list_tasks,
                       "List a peer's tasks via 'tasks/list' (A2A v1.0) — the way to recover an async "
                       "task id you lost track of. Filters by context_id / status, paginates with "
                       "page_size (max 100) + page_token. Distinct from a2a_list, which lists peers "
                       "and conversations.",
                       {"agent": _str("Configured peer name (from a2a_agents) or a full http(s):// URL."),
                        "context_id": _str("Optional: only tasks in this conversation."),
                        "status": _str("Optional: filter by state, e.g. 'working' or 'completed'."),
                        "page_size": _str("Page size, 1-100 (default 20)."),
                        "page_token": _str("Optional: nextPageToken from a previous call.")},
                       ["agent"]),
    "a2a_list": (a2a_list, "List configured A2A peer agents, persisted A2A conversations, and metrics.", {}, []),
    "a2a_history": (a2a_history,
                    "Recall a persisted A2A conversation transcript by context_id. It survives restarts and "
                    "context compaction; use a2a_list to see known context ids.",
                    {"context_id": _str("Context id of the conversation to recall."),
                     "limit": {"type": "integer", "description": "Max messages to return (default 50, max 200)."}},
                    ["context_id"]),
    "a2a_orchestrate": (a2a_orchestrate,
                        "Fan-out a task to multiple peer agents by capability. Peers are matched from config.yaml "
                        "a2a_agents.*.capabilities. Modes: 'all' (return all replies), 'first' (first successful), "
                        "'best' (longest successful reply).",
                        {"capability": _str("Capability to match (e.g. 'research', 'code') or '*' for all peers."),
                         "message": _str("The task to send to all matching peers."),
                         "mode": {"type": "string", "enum": ["all", "first", "best"], "description": "How to aggregate results. Default: 'all'."},
                         "context_id": _str("Optional: shared context id for all peers.")},
                        ["capability", "message"]),
}


def _a2a_tools_available() -> bool:
    """check_fn: serve the client tools ONLY when the operator opted into A2A (peers under
    ``a2a_agents``, inbound platform enabled, or A2A_PORT set). Fail closed.

    Maintainer-directed (#95681): these registered unconditionally, so every session on every install paid
    ~561 tok/call for tools whose only possible output without config is 'no peers configured'. A2A is
    unrelated to Bot Mode (bots talk over gateway RPCs) — for most installs this toolset is foreign-agent
    plumbing they never enabled. Config adds mid-session surface at the next compaction (#97073).
    """
    cfg = {}
    with contextlib.suppress(Exception):
        cfg = _load_config()
        if cfg.get("a2a_agents"):
            return True
    try:
        # Scoped like the platform gate: os.environ is the launch profile's under multiplexing (#122126).
        if _get_scoped_secret("A2A_PORT"):
            return True
        a2a_cfg = (cfg.get("platforms") or {}).get("a2a") or {}
        return bool(isinstance(a2a_cfg, dict) and a2a_cfg.get("enabled"))
    except Exception:
        return False


def register_tools(ctx) -> None:
    """Register the client tools in the ``a2a`` toolset (config-gated)."""
    for name, (handler, description, properties, required) in _TOOLS.items():
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        ctx.register_tool(name=name, toolset="a2a", handler=handler, description=description,
                          schema={"name": name, "description": description, "parameters": parameters},
                          emoji="\U0001f9e9", check_fn=_a2a_tools_available)  # puzzle piece
