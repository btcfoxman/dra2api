from __future__ import annotations

import logging
import random
import re
import threading
import time
import uuid
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from app.browser_assist import browser_action, browser_snapshot, capture_browser_session
from app.browser_context import (
    DramaBrowserAccountSuspended,
    DramaBrowserChallengeError,
    DramaBrowserError,
    DramaBrowserTransportError,
    delete_managed_profile,
    refresh_account_context,
    reset_managed_profile,
    shutdown_managed_browsers,
    stop_managed_browser,
)
from app.config import normalize_proxy_url
from app.approval_recovery import recovery_evidence, same_production_settings
from app.db import Database, now_ts
from app.drama_client import (
    DramaAccountSuspended,
    DramaAuthError,
    DramaClient,
    DramaRateLimited,
    DramaRiskBlocked,
    DramaUpstreamError,
    validate_approval,
    failure_reason,
    is_rate_limit_message,
    result_urls,
)
from app.model_catalog import (
    CREDIT_RATES,
    MEDIA_LIMITS,
    model_map_json,
    normalize_generation_request,
    public_models,
)
from app.task_errors import public_failure, public_failure_message


LOGGER = logging.getLogger("dra2api.service")
TERMINAL_STATUSES = {"succeeded", "failed", "expired"}


class _DynamicSlots:
    def __init__(self, limit: int):
        self.limit = max(int(limit), 1)
        self.active = 0
        self.condition = threading.Condition()
        self.waiters: deque[object] = deque()

    def set_limit(self, limit: int) -> None:
        with self.condition:
            self.limit = max(int(limit), 1)
            self.condition.notify_all()

    def reserve(self) -> object:
        token = object()
        with self.condition:
            self.waiters.append(token)
            self.condition.notify_all()
        return token

    def cancel(self, token: object) -> None:
        with self.condition:
            try:
                self.waiters.remove(token)
            except ValueError:
                return
            self.condition.notify_all()

    def acquire(self, token: object | None = None) -> None:
        with self.condition:
            if token is None:
                token = object()
                self.waiters.append(token)
            while self.active >= self.limit or self.waiters[0] is not token:
                self.condition.wait(1)
            self.waiters.popleft()
            self.active += 1
            self.condition.notify_all()

    def release(self) -> None:
        with self.condition:
            self.active = max(self.active - 1, 0)
            self.condition.notify_all()


class DRAService:
    runtime_fields = (
        "task_workers",
        "task_queue_capacity",
        "poll_interval_seconds",
        "task_timeout_seconds",
        "request_timeout_seconds",
        "request_retries",
        "account_maintenance_interval_seconds",
        "account_maintenance_workers",
        "daily_checkin_enabled",
        "browser_recovery_enabled",
        "browser_timeout_seconds",
        "browser_login_workers",
        "browser_login_stagger_seconds",
        "browser_challenge_grace_seconds",
        "chrome_executable",
        "chrome_user_data_root",
        "chrome_headless",
        "proxy_host_override",
        "proxy_pool_enabled",
        "proxy_pool",
        "low_balance_disable_threshold",
        "excess_media_policy",
        "allow_video_reference_inputs",
        "prompt_media_reference_cleanup_enabled",
        "model_map",
    )

    def __init__(self, db: Database, settings: Any):
        self.db = db
        self.settings = settings
        self._load_runtime_settings()
        self._tasks = ThreadPoolExecutor(max_workers=50, thread_name_prefix="dra-task")
        self._logins = ThreadPoolExecutor(max_workers=10, thread_name_prefix="dra-login")
        self._maintenance = ThreadPoolExecutor(
            max_workers=20,
            thread_name_prefix="dra-maintenance",
        )
        self._slots = _DynamicSlots(int(settings.task_workers))
        self._login_slots = _DynamicSlots(int(settings.browser_login_workers))
        self._maintenance_slots = _DynamicSlots(int(settings.account_maintenance_workers))
        self._futures: dict[str, Future[Any]] = {}
        self._future_lock = threading.RLock()
        self._task_submit_lock = threading.RLock()
        self._account_guard = threading.RLock()
        self._running_logins: set[int] = set()
        self._running_maintenance: set[int] = set()
        self._active_maintenance: set[int] = set()
        self._resetting_profiles: set[int] = set()
        self._claiming_rewards: set[int] = set()
        self._manual_browser_leases: dict[int, float] = {}
        self._text_video_image_locks: dict[int, threading.Lock] = {}
        self._stop = threading.Event()
        self._maintenance_wakeup = threading.Event()
        self._maintenance_thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._maintenance_wakeup.clear()
        for task in self.db.recoverable_tasks():
            self._schedule(str(task["id"]))
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop,
            name="dra-account-maintenance",
            daemon=True,
        )
        self._maintenance_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._maintenance_wakeup.set()
        if self._maintenance_thread:
            self._maintenance_thread.join(timeout=3)
        self._tasks.shutdown(wait=False, cancel_futures=False)
        self._logins.shutdown(wait=False, cancel_futures=False)
        self._maintenance.shutdown(wait=False, cancel_futures=True)
        shutdown_managed_browsers()

    def _load_runtime_settings(self) -> None:
        for field in self.runtime_fields:
            raw = self.db.get_setting(field, "")
            if not raw:
                continue
            current = getattr(self.settings, field)
            if isinstance(current, bool):
                value: Any = raw.lower() in {"1", "true", "yes", "on"}
            elif isinstance(current, int):
                value = int(raw)
            elif isinstance(current, float):
                value = float(raw)
            else:
                value = raw
            setattr(self.settings, field, value)

    def runtime_settings(self) -> dict[str, Any]:
        value = {field: getattr(self.settings, field) for field in self.runtime_fields}
        value["ignore_excess_media"] = value["excess_media_policy"] == "ignore"
        value["media_limits"] = dict(MEDIA_LIMITS)
        if not bool(value["allow_video_reference_inputs"]):
            value["media_limits"]["videos"] = 0
        return value

    def update_runtime_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        values = {key: value for key, value in changes.items() if value is not None}
        alias = values.pop("ignore_excess_media", None)
        if alias is not None and "excess_media_policy" not in values:
            values["excess_media_policy"] = "ignore" if alias else "strict"
        if "model_map" in values:
            values["model_map"] = model_map_json(values["model_map"])
        if "excess_media_policy" in values and values["excess_media_policy"] not in {
            "ignore",
            "strict",
        }:
            raise ValueError("excess_media_policy must be ignore or strict")
        persisted: dict[str, Any] = {}
        for field, value in values.items():
            if field not in self.runtime_fields:
                continue
            setattr(self.settings, field, value)
            persisted[field] = value
        self.db.set_settings(persisted)
        self._slots.set_limit(int(self.settings.task_workers))
        self._login_slots.set_limit(int(self.settings.browser_login_workers))
        self._maintenance_slots.set_limit(
            int(self.settings.account_maintenance_workers)
        )
        if {
            "account_maintenance_interval_seconds",
            "account_maintenance_workers",
            "daily_checkin_enabled",
        } & values.keys():
            self._maintenance_wakeup.set()
        return self.runtime_settings()

    def models(self) -> list[dict[str, Any]]:
        models = public_models(self.settings.model_map)
        if bool(self.settings.allow_video_reference_inputs):
            return models
        for model in models:
            capabilities = model.get("capabilities") or {}
            limits = capabilities.get("media_limits") or {}
            limits["videos"] = 0
        return models

    def _proxy_values(self) -> list[str]:
        raw = str(self.settings.proxy_pool or "")
        values: list[str] = []
        for item in raw.replace(";", "\n").replace(",", "\n").splitlines():
            proxy = normalize_proxy_url(item.strip())
            if proxy and proxy not in values:
                values.append(proxy)
        return values

    def _assign_proxy(self, *, exclude_proxy: str = "") -> str:
        if not bool(self.settings.proxy_pool_enabled):
            return ""
        excluded = normalize_proxy_url(exclude_proxy)
        proxies = [item for item in self._proxy_values() if item != excluded]
        if not proxies:
            return ""
        counts = self.db.proxy_assignment_counts()
        minimum = min(counts.get(proxy, 0) for proxy in proxies)
        candidates = [proxy for proxy in proxies if counts.get(proxy, 0) == minimum]
        return random.choice(candidates)

    @staticmethod
    def _identity_key(payload: dict[str, Any]) -> str:
        return str(payload.get("email") or payload.get("name") or "").strip().lower()

    @staticmethod
    def _looks_like_proxy(value: str) -> bool:
        return bool(
            re.fullmatch(r"(?:\[[^]]+\]|[^\s:/]+):\d+", value)
            or re.match(r"^(?:https?|socks4|socks5h?)://", value, re.IGNORECASE)
        )

    def _parse_batch_text(
        self, text: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        accounts: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for line_number, raw in enumerate(str(text or "").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            for separator in ("\t", "----", "|", ","):
                if separator in line:
                    parts = [part.strip() for part in line.split(separator)]
                    break
            else:
                parts = [line]
            email = parts[0] if parts else ""
            password = parts[1] if len(parts) > 1 else ""
            proxy_url = parts[2] if len(parts) > 2 else ""
            if len(parts) == 2 and self._looks_like_proxy(password):
                proxy_url, password = password, ""
            if not email:
                errors.append({"line": str(line_number), "message": "email is required"})
                continue
            if len(parts) > 3:
                errors.append(
                    {
                        "line": str(line_number),
                        "message": "too many fields; expected email, password, proxy",
                    }
                )
                continue
            accounts.append(
                {
                    "name": email,
                    "email": email,
                    "password": password,
                    "proxy_url": proxy_url,
                    "enabled": True,
                    "auto_login": True,
                    "max_concurrency": int(self.settings.account_default_concurrency),
                }
            )
        return accounts, errors

    @staticmethod
    def _account_payload(payload: dict[str, Any]) -> dict[str, Any]:
        value = dict(payload)
        if "access_token" in value or "token" in value:
            value["access_token"] = str(
                value.get("access_token") or value.get("token") or ""
            )
        if "user_id" in value or "uid" in value:
            value["user_id"] = str(value.get("user_id") or value.get("uid") or "")
        if "team_id" in value or "team" in value:
            value["team_id"] = str(value.get("team_id") or value.get("team") or "")
        records = value.get("cookie_records")
        if records is not None:
            value["cookie_records"] = [
                item.model_dump() if hasattr(item, "model_dump") else dict(item)
                for item in records
            ]
        return value

    def upsert_account(self, payload: dict[str, Any], *, start_login: bool = False) -> dict[str, Any]:
        value = self._account_payload(payload)
        with self._account_guard:
            if not str(value.get("proxy_url") or "").strip() and bool(
                value.pop("use_proxy_pool", True)
            ):
                value["proxy_url"] = self._assign_proxy()
            account = self.db.upsert_account(value)
        if start_login:
            self.schedule_login(int(account["id"]))
        return self.db.get_account(int(account["id"]), include_secrets=False) or account

    def sync_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = self._account_payload(payload)
        value["name"] = str(value.get("name") or value.get("email") or "").strip()
        account = self.upsert_account(value, start_login=False)
        try:
            return self.check_account(int(account["id"]), recover=False)
        except Exception:
            return self.db.get_account(int(account["id"]), include_secrets=False) or account

    def batch_import(
        self,
        source: str | list[dict[str, Any]],
        *,
        start_login: bool,
        use_proxy_pool: bool,
    ) -> dict[str, Any]:
        if isinstance(source, list):
            raw_accounts = [self._account_payload(item) for item in source]
            errors: list[dict[str, str]] = []
        else:
            raw_accounts, errors = self._parse_batch_text(source)
        if not raw_accounts:
            raise ValueError("no accounts were provided")
        if len(raw_accounts) > 1000:
            raise ValueError("at most 1000 accounts can be imported at once")

        values: list[dict[str, Any]] = []
        identity_indexes: dict[str, int] = {}
        for item in raw_accounts:
            identity = self._identity_key(item)
            if not identity:
                errors.append({"line": "", "message": "account name or email is required"})
                continue
            if identity in identity_indexes:
                current = values[identity_indexes[identity]]
                for key, value in item.items():
                    if key in {"name", "email"}:
                        continue
                    if value not in (None, "", [], {}):
                        current[key] = value
                continue
            identity_indexes[identity] = len(values)
            values.append(dict(item))

        pool = self._proxy_values() if use_proxy_pool and self.settings.proxy_pool_enabled else []
        counts = Counter(self.db.proxy_assignment_counts())
        existing_count = 0
        for item in values:
            explicit_proxy = normalize_proxy_url(str(item.get("proxy_url") or ""))
            existing = self.db.find_account_by_identity(item, include_secrets=False)
            if existing:
                existing_count += 1
                item["name"] = existing["name"]
            if explicit_proxy:
                item["proxy_url"] = explicit_proxy
                if not existing:
                    counts[explicit_proxy] += 1
                continue
            if existing:
                continue
            if pool:
                selected = min(pool, key=lambda proxy: (counts[proxy], pool.index(proxy)))
                item["proxy_url"] = selected
                counts[selected] += 1

        accounts: list[dict[str, Any]] = []
        login_started_count = 0
        for index, payload in enumerate(values, 1):
            try:
                account = self.upsert_account(payload, start_login=False)
                accounts.append(account)
                if start_login and self.schedule_login(int(account["id"])):
                    login_started_count += 1
            except Exception as exc:
                errors.append({"line": str(index), "message": str(exc)})
        return {
            "accounts": accounts,
            "count": len(accounts),
            "input_count": len(raw_accounts),
            "duplicate_count": len(raw_accounts) - len(values) + existing_count,
            "existing_count": existing_count,
            "login_started": bool(start_login),
            "login_started_count": login_started_count,
            "errors": errors,
        }

    def update_account(self, account_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        before = self.db.get_account(account_id)
        if not before:
            raise KeyError("account not found")
        value = self._account_payload(changes)
        if value.get("enabled") is False:
            stop_managed_browser(account_id, int(before.get("cdp_port") or 0))
            value.setdefault("status", "disabled")
        elif value.get("enabled") is True and before.get("status") in {
            "disabled",
            "disabled_low_balance",
            "suspended",
        }:
            value.setdefault("status", "pending")
            value.setdefault("last_error", "")
        account = self.db.update_account(account_id, value)
        if not account:
            raise KeyError("account not found")
        return self.db.get_account(account_id, include_secrets=False) or account

    def delete_account(self, account_id: int) -> bool:
        account = self.db.get_account(account_id)
        if not account:
            return False
        if account.get("enabled"):
            raise ValueError("account must be disabled before deletion")
        if int(account.get("active_tasks") or 0):
            raise ValueError("account has active tasks")
        with self._account_guard:
            if self._manual_browser_active(account_id):
                raise ValueError("请先关闭该账号的人工验证窗口")
            if int(account_id) in self._running_logins:
                raise ValueError("account login is in progress")
            if int(account_id) in self._running_maintenance:
                raise ValueError("account maintenance is in progress")
            if int(account_id) in self._resetting_profiles:
                raise ValueError("account profile reset is in progress")
            if int(account_id) in self._claiming_rewards:
                raise ValueError("奖励领取正在进行，请稍后重试")
        if not account.get("external_cdp_url"):
            delete_managed_profile(account, self.settings)
        return self.db.delete_account(account_id)

    def reset_account_profile(
        self,
        account_id: int,
        *,
        proxy_url: str | None,
        use_proxy_pool: bool,
        start_login: bool,
    ) -> dict[str, Any]:
        account_id = int(account_id)
        with self._account_guard:
            account = self.db.get_account(account_id)
            if not account:
                raise KeyError("account not found")
            if int(account.get("active_tasks") or 0):
                raise ValueError("account has active tasks")
            if self._manual_browser_active(account_id):
                raise ValueError("请先关闭该账号的人工验证窗口")
            if account_id in self._running_logins:
                raise ValueError("account login is already in progress")
            if account_id in self._running_maintenance:
                raise ValueError("account maintenance is already in progress")
            if account_id in self._resetting_profiles:
                raise ValueError("account profile reset is already in progress")
            if account_id in self._claiming_rewards:
                raise ValueError("奖励领取正在进行，请稍后重试")
            self._resetting_profiles.add(account_id)
        previous_status = str(account.get("status") or "login_required")
        self.db.update_account(account_id, {"status": "profile_resetting", "last_error": ""})
        try:
            current_proxy = normalize_proxy_url(str(account.get("proxy_url") or ""))
            if use_proxy_pool:
                selected_proxy = self._assign_proxy(exclude_proxy=current_proxy)
                if not selected_proxy:
                    raise ValueError("proxy pool is disabled, empty, or has no alternate proxy")
            elif proxy_url is None:
                selected_proxy = current_proxy
            else:
                selected_proxy = normalize_proxy_url(proxy_url)
            profile = reset_managed_profile(account, self.settings)
            updated = self.db.update_account(
                account_id,
                {
                    **profile,
                    "proxy_url": selected_proxy,
                    "access_token": "",
                    "user_id": "",
                    "team_id": "",
                    "cookie_header": "",
                    "cookie_records": [],
                    "user_agent": "",
                    "sec_ch_ua": "",
                    "sec_ch_ua_platform": "",
                    "status": "login_pending" if start_login else "login_required",
                    "last_error": "",
                    "last_login_at": None,
                },
            )
        except Exception as exc:
            self.db.update_account(
                account_id,
                {"status": previous_status, "last_error": f"profile reset failed: {exc}"},
            )
            raise
        finally:
            with self._account_guard:
                self._resetting_profiles.discard(account_id)
        login_started = self.schedule_login(account_id) if start_login else False
        return {
            "account": self.db.get_account(account_id, include_secrets=False) or updated or {},
            "profile_reset": profile,
            "login_started": login_started,
        }

    def _manual_browser_active(self, account_id: int) -> bool:
        with self._account_guard:
            deadline = self._manual_browser_leases.get(int(account_id), 0)
            if deadline > time.monotonic():
                return True
            self._manual_browser_leases.pop(int(account_id), None)
            return False

    def open_manual_browser(self, account_id: int) -> dict[str, Any]:
        with self._account_guard:
            account = self.db.get_account(account_id)
            if not account:
                raise KeyError("account not found")
            if account_id in self._running_logins:
                raise ValueError("登录仍在进行，出现需验证或登录结束后可打开人工验证")
            if (account_id in self._running_maintenance or account_id in self._resetting_profiles
                    or account_id in self._claiming_rewards
                    or account.get("active_tasks")):
                raise ValueError("账号正在执行任务或维护，请稍后打开人工验证")
            self._manual_browser_leases[account_id] = time.monotonic() + 120
        try:
            return browser_snapshot(account, self.settings, start=True)
        except Exception:
            self.close_manual_browser(account_id)
            raise

    def _manual_browser_account(self, account_id: int) -> dict[str, Any]:
        with self._account_guard:
            if not self._manual_browser_active(account_id):
                raise ValueError("人工验证窗口已关闭或超时，请重新打开")
            account = self.db.get_account(account_id)
            if not account:
                raise KeyError("account not found")
            self._manual_browser_leases[account_id] = time.monotonic() + 120
            return account

    def manual_browser_snapshot(self, account_id: int) -> dict[str, Any]:
        return browser_snapshot(self._manual_browser_account(account_id), self.settings)

    def manual_browser_action(self, account_id: int, action: dict[str, Any]) -> dict[str, Any]:
        return browser_action(self._manual_browser_account(account_id), self.settings, action)

    def complete_manual_browser(self, account_id: int) -> dict[str, Any]:
        account = self._manual_browser_account(account_id)
        context = capture_browser_session(account, self.settings)
        self.db.update_account(account_id, context)
        result = self.check_account(account_id, recover=False)
        self.close_manual_browser(account_id)
        return result

    def close_manual_browser(self, account_id: int) -> None:
        with self._account_guard:
            self._manual_browser_leases.pop(account_id, None)

    def schedule_login(self, account_id: int) -> bool:
        account_id = int(account_id)
        with self._account_guard:
            if (account_id in self._running_logins or account_id in self._claiming_rewards
                    or self._manual_browser_active(account_id)):
                return False
            if not self.db.get_account(account_id, include_secrets=False):
                return False
            self._running_logins.add(account_id)
            self.db.update_account(account_id, {"status": "login_pending", "last_error": ""})
            try:
                future = self._logins.submit(self._login_account, account_id)
            except Exception:
                self._running_logins.discard(account_id)
                raise
        future.add_done_callback(lambda _future, value=account_id: self._finish_login(value))
        return True

    def _finish_login(self, account_id: int) -> None:
        with self._account_guard:
            self._running_logins.discard(int(account_id))

    def _login_account(self, account_id: int) -> dict[str, Any]:
        self._login_slots.acquire()
        try:
            stagger = float(self.settings.browser_login_stagger_seconds)
            if stagger > 0:
                time.sleep(random.uniform(0, stagger))
            account = self.db.get_account(account_id)
            if not account:
                raise KeyError("account not found")
            self.db.update_account(account_id, {"status": "logging_in", "last_error": ""})
            context = refresh_account_context(account, self.settings)
            self.db.update_account(account_id, context)
            return self.check_account(account_id, recover=False)
        except Exception as exc:
            if isinstance(exc, (DramaAccountSuspended, DramaBrowserAccountSuspended)):
                self._mark_account_suspended(account_id)
                raise
            if isinstance(exc, (DramaBrowserChallengeError, DramaRiskBlocked)):
                status = "challenge_required"
            elif isinstance(exc, DramaBrowserTransportError):
                status = "network_error"
            elif isinstance(exc, DramaAuthError):
                status = "login_failed"
            elif isinstance(exc, DramaBrowserError):
                status = "login_failed"
            else:
                status = "network_error"
            self.db.update_account(
                account_id,
                {"status": status, "last_error": str(exc), "last_checked_at": now_ts()},
            )
            raise
        finally:
            self._login_slots.release()

    def _recover_client(self, account: dict[str, Any]) -> DramaClient:
        if self._manual_browser_active(int(account["id"])):
            raise DramaBrowserChallengeError("人工验证正在进行，请在验证窗口保存会话")
        if not bool(self.settings.browser_recovery_enabled):
            raise DramaAuthError("Drama session recovery is disabled")
        try:
            context = refresh_account_context(account, self.settings)
        except DramaBrowserAccountSuspended as exc:
            raise DramaAccountSuspended() from exc
        updated = self.db.update_account(int(account["id"]), context) or account
        return self._client(updated)

    def _with_recovery(
        self,
        account: dict[str, Any],
        client: DramaClient,
        operation: Callable[[DramaClient], Any],
    ) -> tuple[Any, DramaClient]:
        try:
            return operation(client), client
        except (DramaAuthError, DramaRiskBlocked):
            recovered = self._recover_client(account)
            return operation(recovered), recovered

    def check_account(
        self,
        account_id: int,
        *,
        recover: bool = True,
        enforce_low_balance: bool = True,
    ) -> dict[str, Any]:
        if recover and self._manual_browser_active(account_id):
            raise ValueError("人工验证正在进行，请在验证窗口保存会话")
        account = self.db.get_account(account_id)
        if not account:
            raise KeyError("account not found")
        client = self._client(account)
        try:
            if recover:
                state, client = self._with_recovery(account, client, lambda item: item.account_state())
            else:
                state = client.account_state()
            updated = self._store_account_state(account_id, account, state)
            if (
                enforce_low_balance
                and updated
                and float(updated.get("last_balance") or 0)
                < float(self.settings.low_balance_disable_threshold)
            ):
                self.db.disable_account_for_low_balance_if_idle(
                    account_id, float(self.settings.low_balance_disable_threshold)
                )
            return self.db.get_account(account_id, include_secrets=False) or updated or {}
        except Exception as exc:
            if isinstance(exc, (DramaAccountSuspended, DramaBrowserAccountSuspended)):
                self._mark_account_suspended(account_id)
                raise
            status = "login_required" if isinstance(
                exc, (DramaAuthError, DramaRiskBlocked, DramaBrowserError)
            ) else "network_error"
            self.db.update_account(
                account_id,
                {"status": status, "last_error": str(exc), "last_checked_at": now_ts()},
            )
            raise

    def _mark_account_suspended(self, account_id: int) -> dict[str, Any] | None:
        account = self.db.get_account(account_id, include_secrets=False)
        if not account:
            return None
        stop_managed_browser(account_id, int(account.get("cdp_port") or 0))
        return self.db.update_account(
            account_id,
            {
                "enabled": False,
                "status": "suspended",
                "last_error": "Drama account has been suspended",
                "last_checked_at": now_ts(),
            },
        )

    def _store_account_state(
        self,
        account_id: int,
        account: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        with self._account_guard:
            current = self.db.get_account(account_id, include_secrets=False) or {}
            details = dict(state.get("buckets") or {})
            for key in ("sign_reward_stats", "reward_claim_stats"):
                if key in (current.get("balance_details") or {}):
                    details[key] = current["balance_details"][key]
            return self.db.update_account(account_id, {
                "email": state.get("email") or account.get("email") or "",
                "user_id": state.get("user_id") or account.get("user_id") or "",
                "team_id": state.get("team_id") or account.get("team_id") or "",
                "access_token": state.get("token") or account.get("access_token") or "",
                "last_balance": state.get("available_balance"),
                "balance_details": details,
                "plan": state.get("plan") or "",
                "status": "active",
                "last_error": "",
                "last_checked_at": now_ts(),
            })

    def claim_account_rewards(self, account_id: int) -> dict[str, Any]:
        with self._account_guard:
            if not self.db.get_account(account_id):
                raise KeyError("account not found")
            if (account_id in self._claiming_rewards or account_id in self._running_maintenance
                    or account_id in self._running_logins or account_id in self._resetting_profiles
                    or self._manual_browser_active(account_id)):
                raise DramaUpstreamError("账号正在领取奖励、登录或维护，请稍后重试", code="ACCOUNT_BUSY", status_code=409)
            self._claiming_rewards.add(account_id)
        try:
            return self._claim_account_rewards(account_id)
        finally:
            with self._account_guard:
                self._claiming_rewards.discard(account_id)

    def _claim_account_rewards(self, account_id: int) -> dict[str, Any]:
        account = self.db.get_account(account_id)
        client = self._client(account)
        # Validate the Firebase identity before any reward write, including disabled accounts.
        state, client = self._with_recovery(account, client, lambda item: item.account_state())
        self._store_account_state(account_id, account, state)
        tasks = client.reward_tasks()
        eligible = {str(item["id"]): item for item in tasks
                    if item.get("status") == "completed" and not item.get("claimed_at")
                    and item.get("reward_eligible") is True
                    and (item.get("reward") or {}).get("type") == "credits"}
        results: list[dict[str, Any]] = []
        for task_id, task in eligible.items():
            entry = {"task_id": task_id, "name": str(task.get("name") or task_id), "credits": 0}
            try:
                entry.update(client.claim_reward(task_id))
            except Exception as exc:
                code = getattr(exc, "code", "REWARD_CLAIM_UNCERTAIN")
                uncertain = (code in {"SUBMISSION_UNCERTAIN", "REWARD_CLAIM_UNCERTAIN"}
                             or getattr(exc, "status_code", 500) >= 500)
                entry.update(status="unknown" if uncertain else "failed", code=code, message=str(exc)[:500])
                if uncertain:
                    # A lost POST response must never trigger an automatic second claim.
                    try:
                        latest = next((item for item in client.reward_tasks() if item["id"] == task_id), {})
                        if latest.get("status") == "claimed" or latest.get("claimed_at"):
                            entry.update(status="reconciled", message="上游已确认领取；本次新增积分以余额查询为准")
                    except Exception:
                        LOGGER.info("account %s reward %s reconciliation unavailable", account_id, task_id)
                results.append(entry)
                if uncertain or isinstance(exc, (DramaAuthError, DramaRiskBlocked, DramaAccountSuspended, DramaRateLimited)):
                    break
                continue
            results.append(entry)
        refreshed, refresh_error = False, ""
        try:
            self._store_account_state(account_id, account, client.account_state())
            refreshed = True
        except Exception as exc:
            refresh_error = str(exc)[:500]
        claimed = [item for item in results if item["status"] == "claimed"]
        report = {"checked_at": now_ts(), "eligible_count": len(eligible), "results": results,
                  "claimed_count": len(claimed), "claimed_credits": sum(item["credits"] for item in claimed),
                  "already_claimed_count": sum(item["status"] == "already_claimed" for item in results),
                  "reconciled_count": sum(item["status"] == "reconciled" for item in results),
                  "failed_count": sum(item["status"] == "failed" for item in results),
                  "unknown_count": sum(item["status"] == "unknown" for item in results),
                  "unattempted_count": len(eligible) - len(results),
                  "balance_refreshed": refreshed, "refresh_error": refresh_error}
        parts = ([f"已领取 {len(claimed)} 项，+{report['claimed_credits']:g} 积分"] if claimed else [])
        if not eligible:
            parts.append("暂无可领取奖励")
        for key, label in (("already_claimed_count", "项已领取，已跳过"), ("reconciled_count", "项已核实领取"),
                           ("failed_count", "项领取失败"), ("unknown_count", "项结果待确认"),
                           ("unattempted_count", "项未尝试")):
            if report[key]:
                parts.append(f"{report[key]} {label}")
        parts.append("额度已刷新" if refreshed else "额度刷新失败，请稍后检测账号")
        report["message"] = "；".join(parts)
        report["status"] = "partial" if (report["failed_count"] or report["unknown_count"]
                                        or report["unattempted_count"] or not refreshed) else "success"
        with self._account_guard:
            current = self.db.get_account(account_id, include_secrets=False) or {}
            details = dict(current.get("balance_details") or {})
            details["reward_claim_stats"] = report
            self.db.update_account(account_id, {"balance_details": details})
        return {**report, "account": self.db.get_account(account_id, include_secrets=False)}

    def create_task(self, payload: dict[str, Any], *, caller_request: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._task_submit_lock:
            capacity = max(int(self.settings.task_queue_capacity), 0)
            maximum_active = int(self.settings.task_workers) + capacity
            if self.db.active_task_count() >= maximum_active:
                raise DramaUpstreamError(
                    "Drama task queue is full",
                    code="TASK_QUEUE_FULL",
                    status_code=429,
                )
            normalized = normalize_generation_request(
                payload,
                self.settings.model_map,
                self.settings.excess_media_policy,
                bool(self.settings.prompt_media_reference_cleanup_enabled),
            )
            if (
                normalized.get("_videos")
                and not bool(self.settings.allow_video_reference_inputs)
            ):
                raise ValueError(
                    "video reference inputs are disabled by the channel setting"
                )
            normalized["_requested_model"] = str(
                payload.get("model") or "doubao-seedance-2-0-mini-260615"
            )
            normalized["_estimated_cost"] = self.db.estimate_cost(normalized) or float(
                CREDIT_RATES.get(normalized["upstream_model"], {}).get(normalized["resolution"], 0)
                * normalized["duration"]
            )
            task_id = f"gen_{uuid.uuid4().hex[:16]}"
            task = self.db.create_task(
                task_id,
                normalized,
                caller_request=caller_request or payload,
            )
            self._schedule(task_id)
            return task

    def _schedule(self, task_id: str) -> None:
        with self._future_lock:
            existing = self._futures.get(task_id)
            if existing and not existing.done():
                return
            slot_token = self._slots.reserve()
            try:
                future = self._tasks.submit(self._run_guarded, task_id, slot_token)
            except Exception:
                self._slots.cancel(slot_token)
                raise
            self._futures[task_id] = future

    def _run_guarded(self, task_id: str, slot_token: object) -> None:
        self._slots.acquire(slot_token)
        try:
            self._run_task(task_id)
        except Exception:
            LOGGER.exception("task %s crashed", task_id)
        finally:
            self._slots.release()
            with self._future_lock:
                self._futures.pop(task_id, None)

    def _acquire_task_account(
        self,
        task: dict[str, Any],
        deadline: float,
        exclude_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        payload = task.get("request") or {}
        recovering = bool(task.get("generation_id"))
        preferred = task.get("account_id") if recovering else payload.get("account_id")
        excluded = {int(value) for value in (exclude_ids or set())}
        if preferred and int(preferred) in excluded:
            preferred = None
        reservation = 0 if recovering else float(task.get("estimated_cost") or 0)
        while time.monotonic() < deadline:
            with self._account_guard:
                manual = {key for key in list(self._manual_browser_leases)
                          if self._manual_browser_active(key)}
                unavailable = excluded | set(self._active_maintenance) | manual
                account = self.db.acquire_account(
                    int(preferred) if preferred else None,
                    exclude_ids=unavailable,
                    kind="video",
                    minimum_balance=reservation,
                    task_id=str(task["id"]),
                    recovering=recovering,
                    reservation_cost=reservation,
                )
            if account:
                return account
            available = self.db.available_account_count(excluded)
            if available <= 0:
                if excluded:
                    raise DramaUpstreamError(
                        "no remaining Drama account has enough credit for this task",
                        code="INSUFFICIENT_CREDITS",
                        status_code=409,
                    )
                raise DramaUpstreamError(
                    "no active Drama account is available",
                    code="NO_AVAILABLE_ACCOUNT",
                    status_code=503,
                )
            if reservation > 0 and self.db.available_account_count(
                excluded,
                minimum_balance=reservation,
            ) <= 0:
                raise DramaUpstreamError(
                    "no active Drama account has enough available credit for this task",
                    code="INSUFFICIENT_CREDITS",
                    status_code=409,
                )
            time.sleep(1)
        raise TimeoutError("timed out waiting for an available Drama account")

    def _release_insufficient_credit_account(
        self,
        *,
        task_id: str,
        account: dict[str, Any],
        client: DramaClient,
        error: DramaUpstreamError,
        phase: str,
        attempts: list[dict[str, Any]],
    ) -> None:
        account_id = int(account["id"])
        attempts.append(
            {
                f"{phase}_error": {
                    "account_id": account_id,
                    "code": error.code,
                    "message": str(error),
                    "response": error.details or {},
                }
            }
        )
        refreshed_balance = False
        try:
            self.db.update_task(
                task_id,
                upstream_response={"attempts": attempts[-30:]},
                status="preparing",
                progress=35,
            )
            try:
                state, _ = self._with_recovery(
                    account, client, lambda current: current.account_state()
                )
                self.db.update_account(
                    account_id,
                    {
                        "last_balance": state.get("available_balance"),
                        "balance_details": state.get("buckets") or {},
                        "plan": state.get("plan") or "",
                        "status": "active",
                        "last_error": str(error),
                        "last_checked_at": now_ts(),
                    },
                )
                refreshed_balance = True
            except Exception as refresh_exc:
                self.db.update_account(
                    account_id,
                    {
                        "last_error": (
                            f"{error}; balance refresh failed: {refresh_exc}"
                        ),
                        "last_checked_at": now_ts(),
                    },
                )
        finally:
            self.db.release_account(account_id, task_id=task_id)
        if refreshed_balance:
            self.db.disable_account_for_low_balance_if_idle(
                account_id,
                float(self.settings.low_balance_disable_threshold),
            )

    def _release_suspended_account(
        self,
        *,
        task_id: str,
        account: dict[str, Any],
        error: DramaUpstreamError,
        phase: str,
        attempts: list[dict[str, Any]],
    ) -> None:
        account_id = int(account["id"])
        attempts.append(
            {
                f"{phase}_error": {
                    "account_id": account_id,
                    "code": error.code,
                    "message": "Drama account has been suspended",
                }
            }
        )
        self.db.update_task(
            task_id,
            upstream_response={"attempts": attempts[-30:]},
            status="preparing",
        )
        self._mark_account_suspended(account_id)
        self.db.release_account(account_id, task_id=task_id)

    def _client(self, account: dict[str, Any]) -> DramaClient:
        return DramaClient(account, self.settings,
                           on_auth_update=lambda changes: self.db.update_account(int(account["id"]), changes))

    def _run_task(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if not task or task.get("status") in TERMINAL_STATUSES:
            return
        payload = task["request"]
        protocol = dict((task.get("upstream_response") or {}).get("protocol") or {})
        account = None
        deadline = time.monotonic() + int(self.settings.task_timeout_seconds)
        balance_fresh = False
        preserve_slot = False
        failure_outcome = "unknown" if (
            task.get("generation_id") or (task.get("upstream_request") or {}).get("submit")
            or any(protocol.get(key) for key in ("project_create_started", "prompt_started", "approved_id"))
        ) else "rejected"
        def save(**changes: Any) -> None:
            self.db.update_task(task_id, upstream_response={"protocol": protocol}, **changes)

        def inspect_recovery(pending: list[dict[str, Any]]) -> dict[str, Any] | None:
            if protocol.get("job_ref"):
                return None
            try:
                evidence = recovery_evidence(client.project_history(project_id), protocol, pending)
                # Recheck after reading history: absence alone never authorizes
                # a retry, and any newly visible job vetoes the evidence.
                if evidence and not client.project_jobs(project_id):
                    return evidence
            except DramaUpstreamError as exc:
                protocol["last_recovery_error"] = {"code": exc.code, "message": str(exc)}
                save()
            return None
        try:
            account = self._acquire_task_account(task, deadline)
            account_id = int(account["id"])
            client = self._client(account)
            state = client.account_state()
            self._store_account_state(account_id, account, state)
            project_id = str(task.get("generation_id") or "")
            if not project_id:
                protocol.clear()
                sources = [(kind, item) for kind, key in (("image", "_images"), ("video", "_videos"), ("audio", "_audio")) for item in payload.get(key) or []]
                uploads = []
                save(status="preparing", channel="draapi", progress=5)
                for index, (kind, item) in enumerate(sources):
                    try:
                        uploads.append(client.upload_media(str(item["value"]), kind, str(item.get("name") or f"{kind}{index + 1}")))
                    except DramaUpstreamError as exc:
                        if exc.code in {"MEDIA_DOWNLOAD_FAILED", "MEDIA_UPLOAD_FAILED"}:
                            key = "media_download_error" if exc.code == "MEDIA_DOWNLOAD_FAILED" else "media_upload_error"
                            protocol[key] = {"reference_index": index + 1, "kind": kind, **(exc.details or {})}
                        elif exc.code in {"MEDIA_FORMAT_UNSUPPORTED", "MEDIA_LIMIT_EXCEEDED", "MEDIA_DURATION_UNSUPPORTED"}:
                            protocol["media_validation_error"] = {"reference_index": index + 1, "kind": kind, **(exc.details or {})}
                        raise
                    save(progress=5 + int(20 * (index + 1) / max(len(sources), 1)))
                request = client.build_generation_request(payload, uploads)
                save(upstream_request={"uploads": [item.audit_view() for item in uploads],
                                       "submit": {"method": "POST", "url": client.base + "/api/v1/hosted/create", "body": request}},
                     estimated_cost=client.estimate_cost(payload))
                # Persist the boundary before a write whose response can be lost.
                failure_outcome = "unknown"
                protocol["project_create_started"] = True
                save()
                project_id = client.create_project(request)
                protocol["project_id"] = project_id
                save(generation_id=project_id, progress=30, status="submitted")
            if not protocol.get("prompt_started"):
                # Persist before the mutation: a restart must never submit the prompt twice.
                protocol["prompt_started"] = True
                save()
                try:
                    protocol["prompt_response"] = client.start_generation(project_id, str(payload.get("language") or "zh-cn"))
                except DramaUpstreamError as exc:
                    if exc.code != "SUBMISSION_UNCERTAIN":
                        raise
                    protocol["prompt_uncertain"] = True
                save()
            poll_errors = 0
            idle_since = None
            while time.monotonic() < deadline:
                if self._stop.is_set():
                    # Keep the upstream identity and balance reservation for startup recovery.
                    preserve_slot = True
                    return
                try:
                    detail = client.generation_detail(project_id, payload)
                except DramaUpstreamError as exc:
                    if exc.code not in {"NETWORK_ERROR", "RATE_LIMITED", "DRAMA_HTTP_ERROR"} or (exc.code == "DRAMA_HTTP_ERROR" and exc.status_code < 500):
                        raise
                    poll_errors += 1
                    delay = min(120, max(self.settings.poll_interval_seconds, 2 ** min(poll_errors, 6)))
                    delay = max(delay, float(getattr(exc, "retry_after", None) or 0))
                    protocol["last_poll_error"] = {"code": exc.code, "message": str(exc)}
                    save()
                    self._stop.wait(min(delay, max(deadline - time.monotonic(), 0)))
                    continue
                poll_errors = 0
                if detail.get("job_ref"):
                    protocol["job_ref"] = detail["job_ref"]
                save(raw_status=detail)
                approvals = detail.get("pendingApprovals") or []
                for approval in approvals:
                    approval_id = str(approval.get("approvalId") or approval.get("id") or "")
                    if not approval_id:
                        raise DramaUpstreamError("报价缺少审批 ID", code="INVALID_QUOTE")
                    approved = protocol.get("approved_id")
                    if approved == approval_id:
                        continue
                    if approval_id in (protocol.get("denied_approval_ids") or []):
                        continue
                    evidence = inspect_recovery(approvals) if approved and not protocol.get("job_ref") else None
                    if protocol.get("job_ref") or (approved and not evidence):
                        denied = protocol.setdefault("denied_approval_ids", [])
                        if approval_id not in denied:
                            # Keep tracking the paid job when the site agent
                            # proposes another generation. Never fund a second.
                            denied.append(approval_id)
                            protocol.setdefault("extra_quotes", []).append(approval)
                            save()
                            try:
                                client.approve(project_id, approval_id, decision="denied")
                            except DramaUpstreamError as exc:
                                if exc.code != "SUBMISSION_UNCERTAIN":
                                    raise
                                protocol["denial_uncertain"] = True
                                save()
                        continue
                    credits = validate_approval(approval, payload)
                    if evidence:
                        if (credits > float(protocol.get("quoted_credits") or 0)
                                or (approval.get("dryRun") or {}).get("status") != "passed"
                                or not same_production_settings(protocol.get("quote") or {}, approval.get("quote") or {})):
                            raise DramaUpstreamError("续交报价的素材、参数或费用与原请求不一致", code="APPROVAL_MISMATCH")
                    if not self.db.reserve_task_balance(task_id, account_id, credits):
                        raise DramaUpstreamError("账号余额不足以支付当前报价", code="INSUFFICIENT_CREDITS", status_code=402)
                    if evidence:
                        protocol.setdefault("approval_repairs", []).append({**evidence, "approval_id": approval_id})
                    protocol.pop("approval_uncertain", None)
                    protocol.update(approved_id=approval_id, approved_tool_call_id=approval.get("toolCallId"),
                                    quoted_credits=credits, quote=approval.get("quote"))
                    save(estimated_cost=credits, progress=40)
                    try:
                        protocol["approval_response"] = client.approve(project_id, approval_id)
                    except DramaUpstreamError as exc:
                        if exc.code != "SUBMISSION_UNCERTAIN":
                            raise
                        protocol["approval_uncertain"] = True
                    save()
                save(status="running", progress=70 if detail.get("job_ref") else 35,
                     raw_status=detail)
                if detail["status"] == "COMPLETE":
                    urls = result_urls(detail)
                    if not urls:
                        raise DramaUpstreamError("任务完成但未返回视频地址", code="RESULT_URL_MISSING")
                    cost = float(detail.get("actual_cost") or protocol.get("quoted_credits") or 0)
                    try:
                        self._store_account_state(account_id, account, client.account_state())
                        balance_fresh = True
                    except DramaUpstreamError:
                        pass
                    save(status="succeeded", progress=100, result_urls=urls, actual_cost=cost,
                         completed_at=now_ts(), error_code="", error_message="")
                    self.db.record_model_cost(task_id, payload, cost)
                    self.db.settle_task_balance(task_id, account_id, actual_cost=cost, balance_snapshot_fresh=balance_fresh)
                    return
                if detail["status"] == "FAILED":
                    failure_outcome = "failed"
                    raise DramaUpstreamError(failure_reason(detail), code=str(detail.get("error_code") or "GENERATION_FAILED"), details=detail)
                if not detail.get("job_ref") and not detail.get("isStreaming") and not approvals:
                    evidence = inspect_recovery([]) if protocol.get("approved_id") else None
                    continuations = protocol.setdefault("continuations", [])
                    if evidence and not any(item.get("failed_tool_call_id") == evidence["failed_tool_call_id"] for item in continuations):
                        continuation = {**evidence, "started_at": now_ts()}
                        continuations.append(continuation)
                        # Durable intent precedes the write. A lost response or
                        # restart must not send the same continuation twice.
                        save()
                        try:
                            continuation["response"] = client.continue_generation(project_id, str(payload.get("language") or "zh-cn"))
                        except DramaUpstreamError as exc:
                            if exc.code != "SUBMISSION_UNCERTAIN":
                                raise
                            continuation["uncertain"] = True
                        save()
                        idle_since = None
                        self._stop.wait(min(self.settings.poll_interval_seconds, max(deadline - time.monotonic(), 0)))
                        continue
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since > 90:
                        raise DramaUpstreamError("上游代理已暂停，请在网站检查项目是否需要补充输入", code="AGENT_INPUT_REQUIRED")
                else:
                    idle_since = None
                self._stop.wait(min(self.settings.poll_interval_seconds, max(deadline - time.monotonic(), 0)))
            raise TimeoutError("Drama.Land generation timed out; the project can be queried again")
        except Exception as exc:
            code = "TASK_TIMEOUT" if isinstance(exc, TimeoutError) else str(getattr(exc, "code", "TASK_FAILED"))
            protocol["failure_outcome"] = failure_outcome
            save(status="expired" if code == "TASK_TIMEOUT" else "failed", progress=100,
                 error_code=code, error_message=str(exc), completed_at=now_ts())
            if account:
                if isinstance(exc, DramaAccountSuspended):
                    self._mark_account_suspended(int(account["id"]))
                elif isinstance(exc, (DramaAuthError, DramaRiskBlocked)):
                    status = "challenge_required" if isinstance(exc, DramaRiskBlocked) else "login_required"
                    self.db.update_account(int(account["id"]), {"status": status, "last_error": str(exc)})
                else:
                    try:
                        self._store_account_state(int(account["id"]), account, self._client(account).account_state())
                    except DramaUpstreamError:
                        pass
        finally:
            if account and not preserve_slot:
                self.db.release_account(int(account["id"]), task_id=task_id)

    def retry_task(self, task_id: str) -> dict[str, Any]:
        task = self.db.get_task(task_id)
        if not task:
            raise KeyError("task not found")
        if task.get("status") not in TERMINAL_STATUSES:
            raise ValueError("task is still active")
        changes: dict[str, Any] = {
            "status": "queued",
            "progress": 0,
            "account_id": None,
            "generation_id": "",
            "error_code": "",
            "error_message": "",
            "completed_at": None,
        }
        if (
            task.get("generation_id")
            and task.get("account_id")
            and (
                task.get("error_code") in {"RATE_LIMITED", "TASK_TIMEOUT", "NETWORK_ERROR", "SUBMISSION_UNCERTAIN", "AGENT_INPUT_REQUIRED", "DRAMA_AUTH_REQUIRED", "DRAMA_RISK_BLOCKED", "DRAMA_HTTP_ERROR", "RESULT_URL_MISSING"}
                or is_rate_limit_message(task.get("error_message"))
            )
        ):
            changes.update(
                status="submitted",
                progress=40,
                account_id=task["account_id"],
                generation_id=task["generation_id"],
            )
        self.db.update_task(
            task_id,
            **changes,
        )
        if changes["generation_id"]:
            self.db.acquire_account(task_id=task_id, recovering=True)
        self._schedule(task_id)
        return self.db.get_task(task_id) or {}

    def wait_task(self, task_id: str, timeout: int | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + int(timeout or self.settings.synchronous_timeout_seconds)
        while time.monotonic() < deadline:
            task = self.db.get_task(task_id)
            if not task:
                raise KeyError("task not found")
            if task.get("status") in TERMINAL_STATUSES:
                return task
            time.sleep(1)
        # Keep the identifier available when the HTTP wait ends before generation.
        return self.db.get_task(task_id) or task

    @staticmethod
    def public_failure_message(task: dict[str, Any]) -> str:
        return public_failure_message(task)

    def admin_task(self, task: dict[str, Any]) -> dict[str, Any]:
        return {**task, "public_error_message": self.public_failure_message(task)
                if task.get("status") in {"failed", "expired"} else ""}

    def public_task(self, task: dict[str, Any]) -> dict[str, Any]:
        result = {
            "id": task["id"],
            "object": "video.generation",
            "created": task.get("created_at"),
            "updated": task.get("updated_at"),
            "status": task.get("status"),
            "progress": task.get("progress", 0),
            "model": task.get("model"),
            "data": [
                {"url": url}
                for url in task.get("result_urls") or []
                if str(url or "").startswith("http")
            ],
        }
        if task.get("status") in {"failed", "expired"}:
            result["error"] = public_failure(task)
        return result

    def task_media_source(self, task_id: str, index: int) -> str:
        task = self.db.get_task(task_id)
        if not task:
            raise KeyError("task not found")
        payload = task.get("request") or {}
        items = (
            list(payload.get("_images") or [])
            + list(payload.get("_videos") or [])
            + list(payload.get("_audio") or [])
        )
        if index < 0 or index >= len(items):
            raise IndexError("media not found")
        return str(items[index].get("value") or "")

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _daily_checkin_due(self, account: dict[str, Any]) -> bool:
        if not bool(self.settings.daily_checkin_enabled):
            return False
        details = account.get("balance_details") or {}
        stats = details.get("sign_reward_stats") or {}
        return str(stats.get("last_sign") or "") != self._today_utc()

    def _store_daily_checkin_stats(
        self,
        account_id: int,
        stats: dict[str, Any],
    ) -> None:
        with self._account_guard:
            account = self.db.get_account(account_id, include_secrets=False) or {}
            details = dict(account.get("balance_details") or {})
            details["sign_reward_stats"] = dict(stats)
            self.db.update_account(account_id, {"balance_details": details})

    def _run_daily_checkin(self, account_id: int) -> None:
        with self._account_guard:
            if account_id in self._claiming_rewards:
                return
            self._claiming_rewards.add(account_id)
        try:
            self._run_daily_checkin_locked(account_id)
        finally:
            with self._account_guard:
                self._claiming_rewards.discard(account_id)

    def _run_daily_checkin_locked(self, account_id: int) -> None:
        account = self.db.get_account(account_id, include_secrets=True)
        if not account or not account.get("enabled"):
            return
        client = self._client(account)
        stats = client.daily_checkin_status()
        self._store_daily_checkin_stats(account_id, stats)
        if bool(stats.get("today_signed")):
            return
        if not bool(stats.get("can_checkedin")):
            LOGGER.info(
                "account %s daily check-in is unavailable: %s",
                account_id,
                stats.get("error_msg") or stats.get("error_code") or "unknown reason",
            )
            return

        reward = client.daily_checkin()
        signed_stats = {
            **stats,
            **reward,
            "last_sign": self._today_utc(),
            "today_signed": True,
            "can_checkedin": False,
        }
        self._store_daily_checkin_stats(account_id, signed_stats)
        LOGGER.info(
            "account %s daily check-in completed, reward credits=%s",
            account_id,
            reward.get("credits"),
        )
        try:
            state = client.account_state()
            self._store_account_state(account_id, account, state)
        except Exception as exc:
            LOGGER.warning(
                "account %s daily check-in balance refresh failed: %s",
                account_id,
                exc,
            )

    def _maintenance_loop(self) -> None:
        first_pass = True
        while not self._stop.is_set():
            if not (first_pass and bool(self.settings.daily_checkin_enabled)):
                self._maintenance_wakeup.wait(
                    int(self.settings.account_maintenance_interval_seconds)
                )
            first_pass = False
            self._maintenance_wakeup.clear()
            if self._stop.is_set():
                break
            for account in self.db.list_accounts(include_secrets=False):
                if (
                    account.get("enabled")
                    and not int(account.get("active_tasks") or 0)
                    and (
                        account.get("auto_login")
                        or self._daily_checkin_due(account)
                    )
                ):
                    account_id = int(account["id"])
                    with self._account_guard:
                        if (
                            account_id in self._running_logins
                            or account_id in self._running_maintenance
                            or account_id in self._resetting_profiles
                            or account_id in self._claiming_rewards
                            or self._manual_browser_active(account_id)
                        ):
                            continue
                        self._running_maintenance.add(account_id)
                    future = self._maintenance.submit(self._maintenance_check, account_id)
                    future.add_done_callback(
                        lambda _future, value=account_id: self._finish_maintenance(value)
                    )

    def _maintenance_check(self, account_id: int) -> None:
        self._maintenance_slots.acquire()
        with self._account_guard:
            self._active_maintenance.add(int(account_id))
        try:
            current = self.db.get_account(account_id, include_secrets=False) or {}
            if not current.get("enabled") or int(current.get("active_tasks") or 0):
                return
            account = self.check_account(
                account_id,
                recover=False,
                enforce_low_balance=False,
            )
            if self._daily_checkin_due(account):
                self._run_daily_checkin(account_id)
            current = self.db.get_account(account_id, include_secrets=False) or {}
            if (
                current.get("enabled")
                and float(current.get("last_balance") or 0)
                < float(self.settings.low_balance_disable_threshold)
            ):
                self.db.disable_account_for_low_balance_if_idle(
                    account_id,
                    float(self.settings.low_balance_disable_threshold),
                )
        except Exception as exc:
            LOGGER.info("account %s maintenance check failed: %s", account_id, exc)
            if isinstance(
                exc,
                (DramaAccountSuspended, DramaBrowserAccountSuspended),
            ):
                self._mark_account_suspended(account_id)
                return
            account = self.db.get_account(account_id, include_secrets=False) or {}
            if account.get("enabled") and account.get("auto_login") and account.get(
                "status"
            ) in {"login_required", "network_error"}:
                self.schedule_login(account_id)
        finally:
            with self._account_guard:
                self._active_maintenance.discard(int(account_id))
            self._maintenance_slots.release()

    def _finish_maintenance(self, account_id: int) -> None:
        with self._account_guard:
            self._running_maintenance.discard(int(account_id))
            self._active_maintenance.discard(int(account_id))

    def status(self) -> dict[str, Any]:
        tasks = self.db.list_task_summaries(100)
        return {
            "service": "dra2api",
            "schema_version": self.settings.schema_version,
            "accounts": len(self.db.list_accounts(include_secrets=False)),
            "available_accounts": self.db.available_account_count(),
            "running_tasks": sum(1 for item in tasks if item.get("status") not in TERMINAL_STATUSES),
            "task_workers": int(self.settings.task_workers),
        }
