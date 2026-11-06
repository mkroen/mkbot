"""管理入口仅调用核心服务，不复制状态转换或任务调度逻辑。"""

import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from mkbot import __version__
from mkbot.domain.errors import AdmissionDenied, Conflict, MkbotError, NotFound
from mkbot.domain.models import (
    ActivityRequest,
    InboundEvent,
    ReviewDecision,
    SleepRequest,
    StrictModel,
)
from mkbot.models.contracts import GatewaySettings
from mkbot.runtime.agent import ExecutionSettings
from mkbot.runtime.clock import Clock
from mkbot.runtime.kernel import Kernel


class ConfigUpdate(StrictModel):
    expected_revision: int = Field(ge=0)
    content: dict[str, Any]


class ActivityTransition(StrictModel):
    status: Literal["paused", "waiting", "completed", "cancelled", "failed"]


class CheckpointUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    data: dict[str, Any]


class WorkResolution(StrictModel):
    outcome: Literal["succeeded", "failed", "cancelled"]
    detail: str = Field(min_length=1, max_length=1000)


class DeliveryResolution(StrictModel):
    status: Literal["sent", "failed"]
    receipt: dict | None = None
    detail: str = Field(min_length=1, max_length=1000)


class BudgetLimit(StrictModel):
    scope: str = Field(min_length=1, max_length=120)
    metric: str = Field(min_length=1, max_length=80)
    limit: int = Field(ge=0)


def create_app(data_dir: Path, token: str, *, clock: Clock | None = None) -> FastAPI:
    if len(token) < 16:
        raise ValueError("管理令牌至少需要 16 个字符")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        kernel = Kernel(data_dir, clock)
        app.state.kernel = kernel
        timer = asyncio.create_task(kernel.run_timers(), name="mkbot-clock")
        try:
            yield
        finally:
            kernel.stop()
            try:
                await timer
            finally:
                kernel.close()

    app = FastAPI(title="mkbot", version=__version__, lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    async def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not secrets.compare_digest(credentials.credentials, token):
            raise HTTPException(401, "管理令牌无效", headers={"WWW-Authenticate": "Bearer"})

    async def kernel(request: Request, _: Annotated[None, Depends(authorize)]) -> Kernel:
        return request.app.state.kernel

    # 保持即时求值，FastAPI 的局部依赖类型不使用 postponed annotations。
    KernelDependency = Annotated[Kernel, Depends(kernel)]

    @app.exception_handler(MkbotError)
    async def domain_error(_: Request, exc: MkbotError):
        code = 404 if isinstance(exc, NotFound) else 409
        if isinstance(exc, AdmissionDenied):
            code = 423
        return JSONResponse({"detail": str(exc), "error": type(exc).__name__}, status_code=code)

    @app.exception_handler(ValueError)
    async def invalid_value(_: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/status")
    async def status(k: KernelDependency):
        return k.lifecycle.state() | {
            "inbox_counts": k.db.rows("SELECT status,count(*) AS count FROM inbox GROUP BY status"),
            "activity_counts": k.db.rows(
                "SELECT status,count(*) AS count FROM activities GROUP BY status"
            ),
        }

    @app.get("/api/inbox")
    async def inbox(k: KernelDependency):
        return k.inbox.list()

    @app.post("/api/inbox", status_code=201)
    async def receive(event: InboundEvent, k: KernelDependency):
        result = k.inbox.receive(event)
        k.notify()
        return result

    @app.post("/api/inbox/review")
    async def review(decision: ReviewDecision, k: KernelDependency):
        k.inbox.resolve(decision)
        return {"ok": True}

    @app.post("/api/lifecycle/sleep")
    async def sleep(request: SleepRequest, k: KernelDependency):
        result = k.lifecycle.request_sleep(request)
        k.notify()
        return result

    @app.post("/api/lifecycle/wake")
    async def wake(k: KernelDependency):
        result = k.lifecycle.wake()
        k.notify()
        return result

    @app.post("/api/lifecycle/awake")
    async def awake(k: KernelDependency):
        k.lifecycle.finish_waking()
        k.notify()
        return k.lifecycle.state()

    @app.post("/api/lifecycle/pause")
    async def pause(k: KernelDependency):
        result = k.lifecycle.pause(True)
        k.notify()
        return result

    @app.post("/api/lifecycle/resume")
    async def resume(k: KernelDependency):
        result = k.lifecycle.pause(False)
        k.notify()
        return result

    @app.get("/api/activities")
    async def activities(k: KernelDependency):
        return k.activities.list()

    @app.post("/api/activities/{activity_id}/run")
    async def run_activity(activity_id: str, k: KernelDependency):
        try:
            return await k.agent.run(activity_id)
        finally:
            k.notify()

    @app.get("/api/plugins")
    async def plugins(k: KernelDependency):
        return k.plugins.catalog()

    @app.post("/api/activities", status_code=201)
    async def create_activity(request: ActivityRequest, k: KernelDependency):
        result = k.activities.create(request)
        k.notify()
        return result

    @app.post("/api/activities/{activity_id}/claim")
    async def claim_activity(activity_id: str, k: KernelDependency):
        return k.activities.claim(activity_id)

    @app.post("/api/activities/{activity_id}/transition")
    async def transition_activity(
        activity_id: str, request: ActivityTransition, k: KernelDependency
    ):
        result = k.activities.transition(activity_id, request.status)
        k.notify()
        return result

    @app.put("/api/activities/{activity_id}/checkpoint")
    async def checkpoint(activity_id: str, request: CheckpointUpdate, k: KernelDependency):
        return k.activities.checkpoint(activity_id, request.expected_version, request.data)

    @app.post("/api/work/{work_id}/resolve")
    async def resolve_work(work_id: str, request: WorkResolution, k: KernelDependency):
        rows = k.db.rows("SELECT status FROM work WHERE id=?", (work_id,))
        if not rows or rows[0]["status"] != "unknown":
            raise Conflict("只能核对状态未知的工作")
        k.lifecycle.finish_work(work_id, request.outcome, detail=request.detail)
        k.notify()
        return {"ok": True}

    @app.get("/api/deliveries")
    async def deliveries(k: KernelDependency):
        return k.outbox.list()

    @app.post("/api/deliveries/{delivery_id}/reconcile")
    async def reconcile_delivery(
        delivery_id: str, request: DeliveryResolution, k: KernelDependency
    ):
        if k.outbox.get(delivery_id)["status"] != "unknown":
            raise Conflict("只能核对状态未知的投递")
        result = k.outbox.settle(
            delivery_id, request.status, receipt=request.receipt, error=request.detail
        )
        k.notify()
        return result

    @app.get("/api/audit")
    async def audit_log(k: KernelDependency):
        return k.audit()

    @app.get("/api/config/{name}")
    async def config(name: str, k: KernelDependency):
        return k.config.get(name)

    @app.put("/api/config/{name}")
    async def update_config(name: str, request: ConfigUpdate, k: KernelDependency):
        if name == "models":
            request.content = GatewaySettings.model_validate(request.content).model_dump(
                mode="json"
            )
        if name == "execution":
            request.content = ExecutionSettings.model_validate(request.content).model_dump(
                mode="json"
            )
        return k.config.put(name, request.content, request.expected_revision)

    @app.get("/api/model-calls")
    async def model_calls(k: KernelDependency):
        return k.models.calls()

    @app.get("/api/config/{name}/history")
    async def config_history(name: str, k: KernelDependency):
        return k.config.history(name)

    @app.get("/api/usage")
    async def usage(k: KernelDependency):
        return k.budgets.usage()

    @app.put("/api/usage/limits")
    async def budget_limit(request: BudgetLimit, k: KernelDependency):
        k.budgets.configure(request.scope, request.metric, request.limit)
        return k.budgets.usage()

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static / "index.html")

    return app
