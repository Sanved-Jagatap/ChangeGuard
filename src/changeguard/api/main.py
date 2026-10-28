"""FastAPI service. Thin HTTP layer: every rule lives in ChangeGuardService (tested without HTTP)."""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..auth import Principal, authenticate
from ..config import Settings
from ..db import Database
from ..domain import quality
from ..errors import ApprovalError, BudgetExceeded, PermissionDenied, ValidationError
from ..scenarios import SCENARIOS
from ..seed import AS_OF
from ..service import ChangeGuardService

settings = Settings.from_env()
service = ChangeGuardService(Database.from_settings(settings), settings)
PUBLISHED = Path(__file__).resolve().parents[3] / "benchmarks" / "published"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    service.db.migrate()
    with service.db.connect("admin") as c:
        row = c.query_one("SELECT COUNT(*) AS n FROM components")
    if row is None:
        raise RuntimeError(
            "Unable to determine whether the components table is empty; API startup aborted."
        )
    empty = row["n"] == 0
    if empty:
        from ..seed import build_seed, load_seed
        with service.db.connect("admin") as c:
            load_seed(c, build_seed())
    yield


app = FastAPI(title="ChangeGuard API", version=__version__, lifespan=lifespan)


class _RateLimiter:
    """Tiny in-memory per-IP limiter for the expensive endpoint (single-process demo use)."""

    def __init__(self, n: int, per: float):
        self.n, self.per = n, per
        self.hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now, q = time.time(), self.hits[key]
        while q and now - q[0] > self.per:
            q.popleft()
        if len(q) >= self.n:
            raise HTTPException(429, "too many investigations; try again in a minute")
        q.append(now)


limiter = _RateLimiter(6, 60.0)


def principal(x_api_token: str | None = Header(default=None)) -> Principal:
    try:
        return authenticate(settings, x_api_token)
    except PermissionDenied as e:
        raise HTTPException(401, str(e)) from e


@app.exception_handler(ValidationError)
async def _validation(_r: Request, e: ValidationError):
    return JSONResponse({"error": "invalid_input", "details": e.errors}, status_code=422)


@app.exception_handler(PermissionDenied)
async def _denied(_r: Request, e: PermissionDenied):
    return JSONResponse({"error": "permission_denied", "message": str(e)}, status_code=403)


@app.exception_handler(ApprovalError)
async def _approval(_r: Request, e: ApprovalError):
    code = 404 if e.kind == "not_found" else 409
    return JSONResponse({"error": e.kind, "message": e.message}, status_code=code)


@app.exception_handler(BudgetExceeded)
async def _budget(_r: Request, e: BudgetExceeded):
    return JSONResponse({"error": "budget_exceeded", "message": str(e)}, status_code=429)


class ChangeIn(BaseModel):
    component_id: str
    change_type: str
    description: str
    urgency: str = "normal"
    target_market: str | None = None
    constraints: dict = Field(default_factory=dict)


class RunIn(BaseModel):
    runner: str = "agent"


class DecisionIn(BaseModel):
    decision: str
    payload_hash: str
    reason: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__, "database": service.db.dialect,
            "llm_provider": settings.llm_provider, "llm_model": settings.llm_model or None}


@app.get("/whoami")
def whoami(p: Principal = Depends(principal)):
    return {"user": p.user, "role": p.role}


@app.get("/scenarios")
def scenarios():
    return SCENARIOS


@app.post("/changes", status_code=201)
def create_change(body: ChangeIn, p: Principal = Depends(principal), idempotency_key: str | None = Header(default=None)):
    return service.submit_change(p, body.model_dump(), idempotency_key or "")


@app.get("/changes")
def list_changes(_p: Principal = Depends(principal)):
    return service.list_changes()


@app.get("/changes/{change_id}")
def get_change(change_id: str, _p: Principal = Depends(principal)):
    return service.get_change(change_id)


@app.post("/changes/{change_id}/runs")
def start_run(change_id: str, body: RunIn, request: Request, p: Principal = Depends(principal)):
    limiter.check(request.client.host if request.client else "unknown")
    res = service.investigate(p, change_id, body.runner)
    return service.get_run(res.run_id)


@app.get("/changes/{change_id}/runs")
def list_runs(change_id: str, _p: Principal = Depends(principal)):
    return service.list_runs(change_id)


@app.get("/runs/{run_id}")
def get_run(run_id: str, _p: Principal = Depends(principal)):
    return service.get_run(run_id)


@app.get("/runs/{run_id}/steps")
def run_steps(run_id: str, _p: Principal = Depends(principal)):
    return service.run_steps(run_id)


@app.get("/changes/{change_id}/actions")
def actions(change_id: str, _p: Principal = Depends(principal)):
    return service.approvals.list_actions(change_id)


@app.post("/actions/{action_id}/decision")
def decide(action_id: str, body: DecisionIn, p: Principal = Depends(principal)):
    return service.decide(p, action_id, body.decision, body.payload_hash, body.reason)


@app.post("/actions/{action_id}/execute")
def execute(action_id: str, p: Principal = Depends(principal)):
    return service.execute(p, action_id)


@app.get("/audit")
def audit_log(change_id: str | None = None, _p: Principal = Depends(principal)):
    return service.audit_log(change_id)


@app.get("/quality")
def data_quality(_p: Principal = Depends(principal)):
    with service.db.connect("reader") as r:
        return quality.run_checks(r, AS_OF)


@app.get("/bench/published")
def bench_published():
    out = []
    for f in sorted(PUBLISHED.glob("*.json")) if PUBLISHED.exists() else []:
        doc = json.loads(f.read_text(encoding="utf-8"))
        out.append({"file": f.name, "meta": doc["meta"], "aggregate": doc["aggregate"]})
    return out


@app.post("/admin/reset")
def reset(p: Principal = Depends(principal)):
    service.reset_demo(p)
    return {"status": "reset"}
