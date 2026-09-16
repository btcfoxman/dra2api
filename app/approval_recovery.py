"""Conservative evidence for retrying a command that never reached dl.

Agent prose, missing jobs and generic CLI/network errors are not proof that a
generation was not submitted. Only the observed shell parser failure qualifies.
"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_APPROVAL_REPAIRS = 2
_SHELL_PARSE_ERROR = re.compile(
    r"/bin/bash: -c: line \d+: unexpected EOF while looking for matching .+"
    r"\n+Command exited with code 2\s*"
)
_UTILITY = re.compile(
    r"^dl generate-video\s+(?:--help|-h|--print-schema|model|preflight|policy|"
    r"composition|async|publish|examples|errors)(?:\s|$)"
)


def recovery_evidence(history: Any, protocol: dict[str, Any],
                      pending: list[dict[str, Any]]) -> dict[str, Any] | None:
    if (not isinstance(history, dict) or history.get("billingMarkers")
            or not protocol.get("approved_id") or protocol.get("job_ref")
            or len(protocol.get("approval_repairs") or []) >= MAX_APPROVAL_REPAIRS):
        return None
    entries = history.get("entries")
    if not isinstance(entries, list):
        return None
    calls, results = {}, {}
    for entry in entries:
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict):
            continue
        if message.get("role") == "toolResult":
            call_id = message.get("toolCallId")
            if call_id in results:
                return None
            results[call_id] = message
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict) or item.get("type") != "toolCall":
                continue
            command = str((item.get("arguments") or {}).get("command") or "")
            if not re.search(r"\bdl\s+generate-video\b", command):
                continue
            command = command.replace("\\\n", " ").strip()
            # A compound script could have submitted work before a later parse
            # error. Accept only one direct generation command.
            if (item.get("name") != "bash" or not command.startswith("dl generate-video ")
                    or any(token in command for token in ("\n", ";", "&", "|", "`", "$(", "<", ">"))):
                return None
            if _UTILITY.match(command):
                continue
            call_id = item.get("id")
            if not call_id or call_id in calls:
                return None
            calls[call_id] = command
    pending_ids = {item.get("toolCallId") for item in pending if item.get("toolCallId")}
    denied_ids = {item.get("toolCallId") for item in protocol.get("extra_quotes") or []
                  if item.get("approvalId") in (protocol.get("denied_approval_ids") or [])}
    failures, inspected = [], []
    for call_id in calls:
        result = results.get(call_id)
        if call_id in pending_ids and result is None:
            continue
        if not result or result.get("isError") is not True:
            return None
        content = result.get("content") or []
        if len(content) != 1 or content[0].get("type") != "text":
            return None
        text = str(content[0].get("text") or "").strip()
        if _SHELL_PARSE_ERROR.fullmatch(text):
            failures.append(call_id)
        elif call_id in denied_ids:
            try:
                denial = json.loads(text)
            except (TypeError, ValueError):
                return None
            if not isinstance(denial, dict) or denial.get("code") != "E_USER_DENIED_TOOL_APPROVAL":
                return None
        else:
            return None
        inspected.append(call_id)
    approved_call = protocol.get("approved_tool_call_id")
    if not failures or (approved_call and approved_call not in failures):
        return None
    return {"reason": "SHELL_PARSE_ERROR", "failed_tool_call_id": failures[-1],
            "inspected_tool_call_ids": inspected, "previous_approval_id": protocol["approved_id"]}


def same_production_settings(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """The corrected prompt may differ; references and production settings may not."""
    def sections(quote: dict[str, Any]) -> list[Any]:
        preview = (quote.get("raw") or {}).get("approvalPreview") or {}
        return [section for section in preview.get("sections") or []
                if section.get("type") != "text" and section.get("items")]
    before, after = sections(previous), sections(current)
    return bool(before) and before == after
