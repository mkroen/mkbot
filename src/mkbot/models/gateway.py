"""所有模型用途共用路由、预算、尝试账本和熔断，缓存只在同一活动内复用。"""

import asyncio
import hashlib
import json
import time
from datetime import timedelta
from uuid import uuid4

from mkbot.accounting.budgets import BudgetManager, Charge
from mkbot.domain.errors import Conflict, ContextInvalidated, NotFound
from mkbot.models.contracts import (
    GatewaySettings,
    Message,
    ModelReply,
    ModelRequest,
    ModelUnavailable,
    Prices,
    Provider,
    ProviderError,
    Route,
    ToolDefinition,
    Usage,
)
from mkbot.models.http_chat import ChatCompletionProvider
from mkbot.runtime.clock import timestamp
from mkbot.runtime.lifecycle import Lifecycle, audit
from mkbot.storage.config import ConfigStore
from mkbot.storage.database import encode


class ModelGateway:
    def __init__(self, lifecycle: Lifecycle, config: ConfigStore, budgets: BudgetManager) -> None:
        self.lifecycle, self.db, self.clock = lifecycle, lifecycle.db, lifecycle.clock
        self.config, self.budgets = config, budgets
        self.providers: dict[str, Provider] = {}
        self.context_validator = None
        self._lock = asyncio.Lock()
        with self.db.transaction() as tx:
            tx.execute("UPDATE model_calls SET status='unknown' WHERE status='running'")

    def register(self, name: str, provider: Provider) -> None:
        if name in self.providers:
            raise Conflict(f"模型供应商 {name} 已注册")
        self.providers[name] = provider

    def _history(self, activity_id: str, key: str) -> list[dict]:
        return self.db.rows(
            "SELECT * FROM model_calls WHERE activity_id=? AND request_key=? ORDER BY attempt",
            (activity_id, key),
        )

    async def call(
        self,
        activity_id: str,
        key: str,
        role: str,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        max_output_tokens: int = 1024,
    ) -> ModelReply:
        if not key or len(key) > 200:
            raise ValueError("模型调用需要稳定且有限长度的请求键")
        snapshot = self.config.get("models")
        settings = GatewaySettings.model_validate(snapshot["content"])
        routes = settings.roles.get(role)
        if not routes:
            raise NotFound(f"尚未配置模型用途 {role}")
        logical = {
            "role": role,
            "messages": [m.model_dump(mode="json") for m in messages],
            "tools": [t.model_dump(mode="json") for t in (tools or [])],
            "max_output_tokens": max_output_tokens,
        }
        encoded = encode(logical)
        if len(encoded.encode("utf-8")) > 256_000:
            raise ValueError("单次模型上下文超过本地大小限制")
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        # 每次调用的配置快照不在 await 中途变化；下次独立调用读取最新路由。
        providers = dict(self.providers)
        providers.update(
            {
                name: ChatCompletionProvider(endpoint)
                for name, endpoint in settings.providers.items()
            }
        )
        for route in routes:
            if route.provider not in providers:
                raise NotFound(f"尚未注册供应商适配器 {route.provider}")
        for route in routes:
            request = ModelRequest(
                model=route.model,
                messages=messages,
                tools=tools or [],
                max_output_tokens=max_output_tokens,
                parameters=route.parameters,
            )
            async with self._lock:
                if self.context_validator:
                    self.context_validator(activity_id)
                with self.db.transaction() as tx:
                    self.lifecycle.check_admission(tx, activity_id)
                history = self._history(activity_id, key)
                if any(row["fingerprint"] != fingerprint for row in history):
                    raise Conflict("相同模型请求键不能变更上下文")
                if history and history[-1]["status"] == "succeeded":
                    return ModelReply.model_validate_json(history[-1]["response"])
                if any(row["status"] in ("running", "unknown") for row in history):
                    raise ModelUnavailable("相同模型请求仍在执行或结果未知，需先核对")
                if len(history) >= settings.max_attempts:
                    raise ModelUnavailable("该请求的累计尝试次数已达上限")
                if history and history[-1]["error_kind"] in ("invalid_request", "auth"):
                    raise ModelUnavailable("请求或凭据需要修正，暂不自动重试")
                # 首条路由是一次新调度；遵守上次保留的退避时间。
                if route is routes[0] and history and history[-1]["retry_at"]:
                    if history[-1]["retry_at"] > timestamp(self.clock.now()):
                        raise ModelUnavailable(
                            "尚未到再次调用时间", retry_at=history[-1]["retry_at"]
                        )
                circuits = self.db.rows(
                    "SELECT * FROM model_circuits WHERE provider=?",
                    (route.provider,),
                )
                if circuits and circuits[0]["open_until"]:
                    if circuits[0]["open_until"] > timestamp(self.clock.now()):
                        continue
                active = self.db.rows(
                    "SELECT count(*) AS n FROM model_calls WHERE status='running'"
                )[0]["n"]
                if active >= settings.max_concurrency:
                    raise ModelUnavailable(
                        "模型并发槽已占用",
                        retry_at=timestamp(self.clock.now() + timedelta(seconds=1)),
                    )
                attempt_id, work_id, scopes = self._start(
                    activity_id,
                    key,
                    role,
                    fingerprint,
                    route,
                    request,
                    settings,
                    snapshot["revision"],
                    len(history) + 1,
                )
            started = time.monotonic()
            try:
                async with asyncio.timeout(settings.timeout_seconds):
                    reply = await providers[route.provider].complete(request)
                    reply = ModelReply.model_validate(reply)
            except (TimeoutError, asyncio.CancelledError) as exc:
                self._finish(
                    attempt_id,
                    work_id,
                    scopes,
                    route,
                    None,
                    "unknown",
                    "timeout",
                    started,
                    settings,
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ModelUnavailable("模型请求超时，用量与结果待核对") from None
            except ProviderError as exc:
                status = "unknown" if exc.kind == "timeout" else "failed"
                self._finish(
                    attempt_id,
                    work_id,
                    scopes,
                    route,
                    exc.usage,
                    status,
                    exc.kind,
                    started,
                    settings,
                    retry_after=exc.retry_after_seconds,
                )
                if exc.kind in ("invalid_request", "auth", "timeout"):
                    raise ModelUnavailable(f"模型调用停止：{exc.kind}") from None
                continue
            except Exception:
                self._finish(
                    attempt_id,
                    work_id,
                    scopes,
                    route,
                    None,
                    "unknown",
                    "adapter_error",
                    started,
                    settings,
                )
                raise ModelUnavailable("适配器未确认结果，需核对该次调用") from None
            else:
                try:
                    if self.context_validator:
                        self.context_validator(activity_id)
                except ContextInvalidated:
                    self._finish(
                        attempt_id,
                        work_id,
                        scopes,
                        route,
                        reply.usage,
                        "invalidated",
                        "context_invalidated",
                        started,
                        settings,
                    )
                    raise
                self._finish(
                    attempt_id,
                    work_id,
                    scopes,
                    route,
                    reply.usage,
                    "succeeded",
                    None,
                    started,
                    settings,
                    reply=reply,
                )
                return reply
        history = self._history(activity_id, key)
        circuit_times = self.db.rows(
            "SELECT open_until FROM model_circuits WHERE open_until>?",
            (timestamp(self.clock.now()),),
        )
        retry_times = [row["open_until"] for row in circuit_times]
        if history and history[-1]["retry_at"]:
            retry_times.append(history[-1]["retry_at"])
        raise ModelUnavailable(
            "可用路由均未完成请求", retry_at=min(retry_times) if retry_times else None
        )

    def _start(
        self, activity_id, key, role, fingerprint, route, request, settings, revision, attempt
    ):
        root = self.db.rows("SELECT root_id FROM activities WHERE id=?", (activity_id,))[0][
            "root_id"
        ]
        day = self.clock.now().date().isoformat()
        scopes = [f"day:{day}", f"root:{root}"]
        self.budgets.configure(scopes[0], "micro_usd", settings.daily_limit_micro_usd)
        self.budgets.configure(scopes[1], "micro_usd", settings.root_limit_micro_usd)
        self.budgets.configure(scopes[1], "model_attempts", settings.root_call_limit)
        # UTF-8 字节数加余量是保守预估，不把它冒充供应商 token 用量。
        upper_input = len(request.model_dump_json().encode()) + 1024
        estimate = route.prices.cost(
            Usage(input_tokens=upper_input, output_tokens=request.max_output_tokens)
        )
        charges = [Charge(scope, "micro_usd", estimate) for scope in scopes]
        charges.append(Charge(scopes[1], "model_attempts", 1))
        attempt_id = str(uuid4())
        work_id = self.lifecycle.start_work("model", activity_id)
        try:
            self.budgets.reserve(attempt_id, charges)
        except BaseException:
            self.lifecycle.finish_work(work_id, "failed", detail="请求尚未发出，预算预留未通过")
            raise
        try:
            with self.db.transaction() as tx:
                tx.execute(
                    "INSERT INTO model_calls(id,activity_id,request_key,fingerprint,role,"
                    "route_revision,provider,model,attempt,work_id,status,prices,started_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        attempt_id,
                        activity_id,
                        key,
                        fingerprint,
                        role,
                        revision,
                        route.provider,
                        route.model,
                        attempt,
                        work_id,
                        "running",
                        route.prices.model_dump_json(),
                        timestamp(self.clock.now()),
                    ),
                )
                audit(
                    tx,
                    "model.started",
                    attempt_id,
                    {
                        "role": role,
                        "provider": route.provider,
                        "model": route.model,
                        "attempt": attempt,
                        "route_revision": revision,
                    },
                    timestamp(self.clock.now()),
                )
        except BaseException:
            self.budgets.release_unstarted(attempt_id)
            self.lifecycle.finish_work(work_id, "failed", detail="尚未发出，未能持久保存调用")
            raise
        return attempt_id, work_id, scopes

    def _finish(
        self,
        attempt_id,
        work_id,
        scopes,
        route: Route,
        usage: Usage | None,
        status,
        error_kind,
        started,
        settings,
        *,
        reply=None,
        retry_after=None,
    ):
        actual = None
        if usage is not None:
            actual = [Charge(scope, "micro_usd", route.prices.cost(usage)) for scope in scopes]
            actual.append(Charge(scopes[1], "model_attempts", 1))
        self.budgets.settle(attempt_id, actual)
        now = timestamp(self.clock.now())
        retry_at = None
        if status == "failed" and error_kind not in ("auth", "invalid_request"):
            delay = max(1, min(retry_after or 10, 3600))
            retry_at = timestamp(self.clock.now() + timedelta(seconds=delay))
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE model_calls SET status=?,error_kind=?,response=?,usage=?,finished_at=?,"
                "elapsed_ms=?,retry_at=? WHERE id=?",
                (
                    status,
                    error_kind,
                    reply.model_dump_json() if reply else None,
                    usage.model_dump_json() if usage else None,
                    now,
                    round((time.monotonic() - started) * 1000),
                    retry_at,
                    attempt_id,
                ),
            )
            tx.execute(
                "INSERT OR IGNORE INTO model_circuits(provider) VALUES(?)", (route.provider,)
            )
            if status == "succeeded":
                tx.execute(
                    "UPDATE model_circuits SET failures=0,open_until=NULL WHERE provider=?",
                    (route.provider,),
                )
            elif error_kind not in ("invalid_request", "context_invalidated"):
                tx.execute(
                    "UPDATE model_circuits SET failures=failures+1 WHERE provider=?",
                    (route.provider,),
                )
                failures = tx.execute(
                    "SELECT failures FROM model_circuits WHERE provider=?", (route.provider,)
                ).fetchone()[0]
                if failures >= settings.circuit_threshold or error_kind in (
                    "auth",
                    "quota",
                    "rate_limit",
                ):
                    until = timestamp(
                        self.clock.now()
                        + timedelta(
                            seconds=max(settings.circuit_cooldown_seconds, retry_after or 0)
                        )
                    )
                    tx.execute(
                        "UPDATE model_circuits SET open_until=? WHERE provider=?",
                        (until, route.provider),
                    )
            audit(
                tx,
                "model.finished",
                attempt_id,
                {"status": status, "error_kind": error_kind, "usage_known": usage is not None},
                now,
            )
        self.lifecycle.finish_work(
            work_id, "failed" if status == "invalidated" else status, detail=error_kind or ""
        )

    def calls(self, limit: int = 100) -> list[dict]:
        rows = self.db.rows(
            "SELECT id,activity_id,request_key,role,route_revision,provider,model,attempt,status,"
            "error_kind,usage,prices,started_at,finished_at,elapsed_ms,retry_at,"
            "json_extract(response,'$.served_model') AS served_model,"
            "(SELECT min(reserved) FROM reservations WHERE id=model_calls.id "
            "AND metric='micro_usd') AS estimated_micro_usd "
            "FROM model_calls ORDER BY started_at DESC,rowid DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [
            row
            | {
                "usage": json.loads(row["usage"]) if row["usage"] else None,
                "prices": json.loads(row["prices"]),
                "calculated_micro_usd": Prices.model_validate_json(row["prices"]).cost(
                    Usage.model_validate_json(row["usage"])
                )
                if row["usage"]
                else None,
            }
            for row in rows
        ]
