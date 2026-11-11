"""Shared wording for the exec-approval prompt every messaging surface renders.

The button card (``BasePlatformAdapter._format_exec_approval``) and the plain-text
``/approve`` fallback (``gateway.run._format_exec_approval_fallback``) must tell the user
the same three things: what Hermes wants to run, why it was flagged, and that silence means
the command does NOT run once ``approvals.timeout`` elapses. Keeping the text here means one
edit changes every platform; adapters only wrap these strings in their own markup.

The ``EA_*`` / ``APPROVAL_TIMED_OUT_NOTICE`` module constants are the English source of truth
(catalog keys ``gateway.exec_approval.*``); anything that renders the prompt calls the sibling
accessors (``ea_header_text()`` …) so the active ``display.language`` is honoured at render time.

No imports from ``gateway.platforms.base`` or ``gateway.run`` — both import this module.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from agent.i18n import t

# Bare strings; adapters add their own bold/HTML around them.
EA_HEADER_TEXT = "Hermes wants to run a command that needs your OK"
EA_REASON_LABEL_TEXT = "Why it was flagged"

# Timeout notice posted when nobody answered the prompt (``{window}`` = "5 minutes").
APPROVAL_TIMED_OUT_NOTICE = (
    "⌛ Approval timed out after {window} — the command was NOT run. "
    "Ask me to try again if you still want it, or raise approvals.timeout in config.yaml.")


# ── 本地补丁：危险命令风险短语的中文映射 ─────────────────────────────────
# ``DANGEROUS_PATTERNS`` 给的是英文短语（如 "recursive delete"），它不经过 i18n，
# 会原样进审批卡片与纯文本提示。这里补一张查表；未收录的原样返回。
_REASON_ZH: dict[str, str] = {
    "PowerShell destructive delete (Remove-Item)": "PowerShell 破坏性删除（Remove-Item）",
    "PowerShell destructive delete (alias after pipe)": "PowerShell 破坏性删除（管道后面接了别名）",
    "PowerShell encoded command execution": "PowerShell 执行编码后的命令（疑似混淆）",
    "SQL DELETE without WHERE": "SQL DELETE 没写 WHERE（会清空整表）",
    "SQL DROP": "SQL DROP（删表/删库）",
    "SQL TRUNCATE": "SQL TRUNCATE（清空整表）",
    "Windows PowerShell destructive delete": "PowerShell 破坏性删除",
    "Windows PowerShell destructive delete (later in the script)": "PowerShell 破坏性删除（脚本后段再次出现删除动作）",
    "Windows cmd destructive delete": "Windows cmd 破坏性删除（del/rd）",
    "Windows destructive delete (recursive/quiet switch)": "Windows 破坏性删除（递归/静默开关）",
    "access to Hermes secrets (Windows path)": "访问 Hermes 的密钥文件",
    "access to SSH keys (Windows path)": "访问 SSH 私钥文件",
    "chmod +x followed by immediate execution": "chmod +x 之后立即执行",
    "cloud metadata endpoint access (instance credentials)": "访问云元数据端点（可能泄露实例凭据）",
    "copy/move file into sensitive credential/SSH/shell-rc path": "把文件复制/移动到凭据、SSH、shell 配置路径",
    "copy/move file into system config path": "把文件复制/移动到系统配置目录",
    "dd to raw block device": "dd 直写裸设备（会毁掉数据）",
    "delete backups (wbadmin)": "删除备份（wbadmin）",
    "delete in root path": "在根目录下删除",
    "delete volume shadow copies (vssadmin)": "删除卷影副本/系统还原点（vssadmin）",
    "delete volume shadow copies (wmic)": "删除卷影副本（wmic，会毁掉系统还原点）",
    "disk copy": "磁盘复制（dd）",
    "disk partitioning (diskpart)": "磁盘分区操作（diskpart）",
    "docker compose restart/stop/kill/down (container lifecycle)": "docker compose 重启/停止/销毁容器",
    "docker context use (switches default daemon for future commands)": "docker context use（改变后续命令的默认守护进程）",
    "docker restart/stop/kill (container lifecycle)": "docker 重启/停止/结束容器",
    "docker with daemon redirect (--context: alternate daemon)": "docker 切换守护进程（--context）",
    "docker with remote daemon redirect (-H/--host)": "docker 指向远程守护进程（-H/--host）",
    "docker/podman daemon redirect via environment (DOCKER_HOST/CONTAINER_HOST)": "用环境变量改 docker/podman 的守护进程",
    "dynamic shell word may expand to arbitrary program execution flag": "变量可能展开成任意程序的执行参数",
    "execute remote content via Invoke-Expression": "用 Invoke-Expression 执行远程内容",
    "execute remote content via command substitution": "用命令替换执行远程内容",
    "execute remote script via process substitution": "用进程替换执行远程脚本",
    "find -delete": "find -delete 批量删除文件",
    "find -exec/-execdir rm": "find -exec rm 批量删除",
    "find dynamic shell word may expand to destructive flag": "find 里的变量可能展开成破坏性参数",
    "force kill processes": "强制结束进程",
    "force kill processes (Stop-Process -Force)": "强制结束进程（Stop-Process -Force）",
    "force kill processes (killall -KILL)": "强制结束进程（killall -KILL）",
    "force kill processes (killall -s KILL)": "强制结束进程（killall -s KILL）",
    "force kill processes (taskkill /F)": "强制结束进程（taskkill /F）",
    "force stop service (Stop-Service -Force)": "强制停止服务（Stop-Service -Force）",
    "fork bomb": "fork 炸弹",
    "format drive (format.com)": "格式化驱动器（format.com）",
    "format filesystem": "格式化文件系统",
    "format filesystem (Format-Volume)": "格式化文件系统（Format-Volume）",
    "git branch force delete": "git 强制删除分支",
    "git branch force delete (long flags)": "git 强制删除分支（长参数）",
    "git branch force delete (long flags, force-first)": "git 强制删除分支（长参数，force 在前）",
    "git clean with force (deletes untracked files)": "git clean -f（删除未跟踪的文件）",
    "git force push (rewrites remote history)": "git 强制推送（重写远端历史）",
    "git force push short flag (rewrites remote history)": "git 强制推送 -f（重写远端历史）",
    "git reset --hard (destroys uncommitted changes)": "git reset --hard（丢弃未提交的改动）",
    "grant Everyone access (icacls)": "把访问权限授予 Everyone（icacls）",
    "hermes update (restarts gateway, kills running agents)": "hermes 升级（会重启网关、杀掉正在运行的 agent）",
    "in-place edit of Hermes config/env": "原地修改 Hermes 的配置/env",
    "in-place edit of Hermes config/env (long flag)": "原地修改 Hermes 的配置/env（长参数）",
    "in-place edit of Hermes config/env (perl/ruby)": "原地修改 Hermes 的配置/env（perl/ruby）",
    "in-place edit of sensitive credential/SSH/shell-rc path": "原地修改凭据/SSH/shell 配置",
    "in-place edit of sensitive credential/SSH/shell-rc path (long flag)": "原地修改凭据/SSH/shell 配置（长参数）",
    "in-place edit of sensitive credential/SSH/shell-rc path (perl/ruby)": "原地修改凭据/SSH/shell 配置（perl/ruby）",
    "in-place edit of system config": "原地修改系统配置",
    "in-place edit of system config (long flag)": "原地修改系统配置（长参数）",
    "init 0/6 (shutdown/reboot)": "init 0/6（关机/重启）",
    "invisible or bidirectional Unicode control character (possible obfuscation)": "含不可见字符或双向控制符（可能在混淆命令）",
    "kill all processes": "结束所有进程",
    "kill hermes/gateway process (self-termination)": "杀掉 Hermes/网关进程（自我终止）",
    "kill process via backtick pgrep/pidof expansion (self-termination)": "用反引号 pgrep/pidof 杀进程（自我终止）",
    "kill process via pgrep/pidof expansion (self-termination)": "用 pgrep/pidof 杀进程（自我终止）",
    "kill processes by regex (killall -r)": "按正则批量结束进程（killall -r）",
    "modify boot configuration (bcdedit /set)": "修改启动配置（bcdedit /set）",
    "overwrite project env/config file": "覆盖项目的 env/配置 文件",
    "overwrite project env/config via redirection": "用重定向覆盖项目的 env/配置",
    "overwrite project env/config via tee": "用 tee 覆盖项目的 env/配置",
    "overwrite system config": "覆盖系统配置",
    "overwrite system file via redirection": "用重定向覆盖系统文件",
    "overwrite system file via tee": "用 tee 覆盖系统文件",
    "package manager uninstall": "包管理器卸载",
    "pipe a credential file into a curl/wget upload (possible exfiltration)": "把凭据文件管道输送给 curl/wget 上传（可能外泄）",
    "pipe decoded content to shell (possible command obfuscation)": "把解码后的内容管道给 shell（疑似混淆）",
    "pipe openssl-decoded content to shell (possible command obfuscation)": "把 openssl 解码的内容管道给 shell（疑似混淆）",
    "pipe remote content to PowerShell (iwr | iex)": "把远程内容管道给 PowerShell 执行（iwr | iex）",
    "pipe remote content to shell": "把远程内容管道给 shell 执行",
    "pipe tr-transformed output to shell (possible command obfuscation)": "把 tr 变换后的输出管道给 shell（疑似混淆）",
    "pipe xxd-decoded content to shell (possible command obfuscation)": "把 xxd 解码的内容管道给 shell（疑似混淆）",
    "podman remote mode (-r/--remote: remote daemon)": "podman 远程模式（-r/--remote）",
    "podman with remote daemon redirect (--url/--connection/--identity)": "podman 指向远程守护进程",
    "recursive chown to root": "递归把属主改成 root",
    "recursive chown to root (long flag)": "递归把属主改成 root（长参数）",
    "recursive delete": "递归删除（rm -r）",
    "recursive delete (flags after operands)": "递归删除（参数写在操作数之后）",
    "recursive delete (long flag)": "递归删除（--recursive）",
    "recursive delete of home directory": "递归删除用户主目录",
    "recursive delete of root filesystem": "递归删除根文件系统",
    "recursive delete of system directory": "递归删除系统目录",
    "recursive world/other-writable (long flag)": "递归开放给所有用户（--recursive）",
    "redirect to raw block device": "重定向到裸设备（会毁掉数据）",
    "registry delete (reg delete)": "删除注册表项（reg delete）",
    "registry value delete (Remove-ItemProperty -Force)": "强制删除注册表值",
    "reset ACLs recursively (icacls /reset)": "递归重置访问控制列表（icacls /reset）",
    "shell execution via heredoc": "用 heredoc 执行 shell",
    "start gateway outside systemd (use 'systemctl --user restart hermes-gateway')": "在 systemd 之外启动网关",
    "stop/delete service (sc)": "停止/删除服务（sc）",
    "stop/restart hermes gateway (kills running agents)": "停止/重启 Hermes 网关（会杀掉正在运行的 agent）",
    "stop/restart hermes launchd service (kills running agents)": "停止/重启 Hermes 服务（会杀掉正在运行的 agent）",
    "stop/restart system service": "停止/重启系统服务",
    "sudo with combined-flag privilege escalation": "sudo 组合参数提权",
    "sudo with privilege flag (stdin/askpass/shell/list)": "sudo 提权参数（stdin/askpass/shell/list）",
    "system shutdown/reboot": "系统关机/重启",
    "systemctl poweroff/reboot": "systemctl 关机/重启",
    "telinit 0/6 (shutdown/reboot)": "telinit 0/6（关机/重启）",
    "upload a secret or credential file via curl/wget (possible exfiltration)": "用 curl/wget 上传密钥或凭据文件（可能外泄）",
    "wipe disk (Clear-Disk)": "清空磁盘（Clear-Disk）",
    "wipe free space (cipher /w)": "擦除磁盘空闲空间（cipher /w）",
    "world/other-writable permissions": "把权限开放给所有用户（o+w）",
    "write to block device": "直接写入块设备",
    "xargs with rm": "xargs 配合 rm 批量删除",
}

def translate_reason(reason: str) -> str:
    """把 ``detect_dangerous_command`` 给的英文风险短语换成中文；未收录的原样返回。"""
    if not reason:
        return reason
    key = reason.strip()
    hit = _REASON_ZH.get(key) or _REASON_ZH.get(key.lower())
    if hit:
        return hit
    # 描述可能带括号后缀（如 "... (long flag)"）：去掉括号再试一次
    return _REASON_ZH.get(key.split(" (")[0], reason)


def ea_header_text() -> str:
    """Localized ``EA_HEADER_TEXT``."""
    return t("gateway.exec_approval.header")


def ea_reason_label_text() -> str:
    """Localized ``EA_REASON_LABEL_TEXT``."""
    return t("gateway.exec_approval.reason_label")


def ea_smart_deny_line_text() -> str:
    """Localized smart-deny sentence (bare; adapters add their own line breaks/markup)."""
    return t("gateway.exec_approval.smart_deny_line")


def ea_default_reason_text() -> str:
    """Localized default "why it was flagged" reason when the approval carries none."""
    return t("gateway.exec_approval.default_reason")


def ea_action_labels() -> dict[str, str]:
    """Localized ``choice -> button label`` for the four approval tiers."""
    return {
        "once": t("gateway.exec_approval.action_once"),
        "session": t("gateway.exec_approval.action_session"),
        "always": t("gateway.exec_approval.action_always"),
        "deny": t("gateway.exec_approval.action_deny"),
    }


def approval_timed_out_notice(window: str) -> str:
    """Localized ``APPROVAL_TIMED_OUT_NOTICE`` for an already-formatted ``window``."""
    return t("gateway.exec_approval.timed_out_notice", window=window)


def approval_timeout_seconds() -> int:
    """The configured ``approvals.timeout`` (default 300s); module attribute so tests can pin it."""
    from tools.approval_context import _get_approval_timeout
    return _get_approval_timeout()


def format_approval_window(seconds: int) -> str:
    """Human wording for a timeout (300 → "5 minutes"); one formatter shared with the CLI notice and
    the tool result's ``user_summary`` — see ``tools.approval_context.format_approval_window``."""
    from tools.approval_context import format_approval_window as _shared
    return _shared(seconds)


def format_approval_deadline_line(timeout_s: int) -> str:
    """The last line of every approval prompt: doing nothing is a safe no."""
    return t("gateway.exec_approval.deadline", window=format_approval_window(timeout_s))


def format_approval_timed_out_notice(timeout_s: int) -> str:
    return approval_timed_out_notice(format_approval_window(timeout_s))


def code_fence(command: str) -> str:
    """A backtick fence longer than any backtick run in ``command``, so the command cannot close it."""
    return "`" * max(3, max((len(run) for run in re.findall(r"`+", command)), default=0) + 1)


def fit_fenced(command: str, fit) -> tuple[str, str]:
    """``(shown, fence)``: the fence is sized from the text actually shown, not the whole ``command``, so
    a long backtick run the cut drops cannot inflate the card past the platform cap. ``fit(fence)``
    returns the longest prefix of ``command`` that fits beside ``fence``. A wider fence only shortens
    that prefix, so the smallest fence its own prefix cannot close is found by bisection."""
    lo, hi = 3, len(code_fence(command))
    while lo < hi:
        mid = (lo + hi) // 2
        if len(code_fence(fit("`" * mid))) <= mid:
            hi = mid
        else:
            lo = mid + 1
    shown = fit("`" * lo)
    return shown, code_fence(shown)


# Shortest preview a hard-capped card keeps beside the cut notice; a longer reason is shortened to leave it.
_EA_MIN_PREVIEW = 120


def exec_approval_cmd_budget(adapter: Any, description: str, smart_denied: bool) -> int:
    """Preview units left once everything else on the card is counted against ``adapter._EA_CARD_CAP``
    (``message_len_fn`` units, after ``_ea_escape``); uncapped adapters use the fixed ``_EA_CMD_BUDGET``."""
    if not adapter._EA_CARD_CAP:
        return adapter._EA_CMD_BUDGET
    fixed = adapter.message_len_fn(
        adapter._EA_HEADER + adapter._EA_CODE_OPEN + adapter._EA_CODE_CLOSE + adapter._EA_REASON_LABEL
        + adapter._ea_escape(description) + "..." + adapter._ea_deadline_line()
        + (adapter._EA_SMART_DENY_LINE if smart_denied else ""))
    return max(0, adapter._EA_CARD_CAP - fixed)


def render_exec_approval(adapter: Any, command: str, description: str, smart_denied: bool,
                         full_command_shown: bool) -> tuple[str, bool]:
    """``(card text, truncated)`` for ``adapter``'s markup. A preview that cannot hold the whole command
    says so in the card: where the full command was sent just before it (``full_command_shown``), or
    that only Deny is offered because it could not be (#62359)."""
    # 本地补丁：风险短语是英文枚举（"recursive delete"），过一遍中文映射表。
    description = translate_reason(str(description or ""))
    if adapter._EA_REASON_BUDGET:
        description = adapter._ea_fit(str(description or ""), adapter._EA_REASON_BUDGET)
    key = "truncated_full_above" if full_command_shown else "truncated_deny_only"
    cut_notice = "\n\n" + adapter._ea_escape(t(f"gateway.exec_approval.{key}", total=len(command)))
    capped = bool(adapter._EA_CARD_CAP)
    if capped:  # the reason gives way first: the deadline line and the cut notice always stay on the card
        room = adapter._exec_approval_cmd_budget("", smart_denied) - adapter.message_len_fn(cut_notice)
        description = adapter._ea_fit(str(description or ""), max(0, room - _EA_MIN_PREVIEW - len("...")))
    # A ``` inside the shown preview would close a markdown fence early; a longer fence holds it, and its
    # extra backticks come out of the preview budget.
    base = adapter._exec_approval_cmd_budget(description, smart_denied)

    def fit(fence: str) -> str:
        budget = base - 2 * (len(fence) - 3)
        preview = adapter._ea_fit(command, budget)
        if preview == command:
            return preview
        # The notice shares the cap the budget was sized for; a tiny fixed budget keeps half its preview.
        left = budget - adapter.message_len_fn(cut_notice)
        return adapter._ea_fit(command, max(left, 0) if capped else max(left, budget // 2))

    preview, fence = fit_fenced(command, fit)
    truncated = preview != command
    notice = cut_notice if truncated else ""
    code_open, code_close = (part.replace("```", fence, 1) for part in (adapter._EA_CODE_OPEN, adapter._EA_CODE_CLOSE))
    text = (f"{adapter._EA_HEADER}"
            f"{code_open}{adapter._ea_escape(preview)}{code_close}"
            f"{adapter._EA_REASON_LABEL}{adapter._ea_escape(description)}{notice}"
            f"{adapter._ea_deadline_line()}")
    return (text + adapter._EA_SMART_DENY_LINE if smart_denied else text), truncated


async def card_shows_whole_command(adapter: Any, chat_id: str, session_key: str, request_id: Optional[str],
                                   command: str, metadata: Optional[dict], truncated: bool) -> bool:
    """Lift a request's Deny-only mark once the user can read the whole command: on the card itself, or
    (for a truncated preview) in a full-command send ahead of it. The request was created Deny-only, so a
    tap that lands before this, or during the send, can only deny it; on any failure (or a raise) the
    mark stays."""
    from tools.approval import restrict_gateway_approval_card_to_deny
    if truncated:
        restrict_gateway_approval_card_to_deny(session_key, request_id)  # a re-render starts Deny-only too
        if not await send_full_command(adapter, chat_id, command, metadata):
            return False
    restrict_gateway_approval_card_to_deny(session_key, request_id, restricted=False)
    return True


def send_cannot_prove_whole(adapter: Any, chat_id: str, content: str) -> bool:
    """True when ``adapter.send(chat_id, content)`` can lose the tail and still report success."""
    # ``send`` reports success even when a chunk cap (Discord's) swapped the tail for a notice.
    cap = getattr(adapter, "MAX_SPLIT_MESSAGES", 0)
    if cap and len(adapter.truncate_message(adapter.format_message(content), adapter.MAX_MESSAGE_LENGTH)) > cap:
        return True
    # A send that cannot report a lost chunk (the relay: its connector splits, and the wire result has no
    # partial field) proves delivery only when the content fits one message.
    return not getattr(adapter, "reports_partial_delivery", True) and adapter.message_len_fn_for_chat(chat_id)(
        adapter.format_message(content)) > adapter.max_message_length_for_chat(chat_id)


async def send_full_command(adapter: Any, chat_id: str, command: str, metadata: Optional[dict]) -> bool:
    """Send the whole command as a fenced block (the adapter's ``send`` splits it to fit) before a
    card whose preview is truncated. False when it cannot be shown verbatim and whole, or a chunk failed."""
    if "```" in command:  # it would close the fence early and the rest would render as markdown
        return False
    content = f"```\n{command}\n```"
    if send_cannot_prove_whole(adapter, chat_id, content):
        return False
    # A mid-turn send: unmarked, a stream-is-the-message adapter seals the live response with it.
    result = await adapter.send(chat_id, content, metadata={**(metadata or {}), "_interim_send": True})
    raw = getattr(result, "raw_response", None)
    clipped = isinstance(raw, dict) and bool(raw.get("warnings") or raw.get("partial_overflow"))
    return bool(getattr(result, "success", False)) and not clipped
