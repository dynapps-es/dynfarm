"""The fake Agent Farm: an in-memory model of the API that the CLI tests run against.

Why it exists: the CLI is only worth as much as its agreement with the real farm, and the real farm cannot be in CI
(no token, no network). So the fake does what the real one does where it matters to a client, and is held to the same
contract by the pinned OpenAPI (`tests/fixtures/openapi.json`):

- every request is routed and validated against the document: an unknown operation, an undocumented query parameter
  or body field, a missing required field, a value outside its bounds are answered like the farm answers them (404,
  422) **and recorded in `violations`**, which the `fake_farm` fixture asserts empty at teardown. A command that sends
  something the API does not accept therefore fails its test even when nobody looked at the response;
- every answer is validated against the document's response schema before it leaves the fake. If the fake itself
  drifts from the contract it raises `FakeFarmBug` (recorded in `bugs`), not a quiet wrong answer;
- identity, ownership and errors follow `docs/farm-api.md`: a token resolves to a principal of a tenant; what belongs to
  another tenant or, inside the tenant, to another person is `404` (not `403`) unless the caller is `farm:tenant:admin`;
  an expired token is `token_expired`, an unknown one `token_invalid` (§3.1); `tenant` in a body is
  `400 tenant_not_accepted`; the error envelope is §15's. Error codes the document does not name are marked `invented`.
- every request is recorded (method, target, headers, body) so a test can assert what did, and did not, go over the
  wire: that a secret appears only in the body of `POST /credentials`, that a token is never in a URL.

It is served two ways: `transport()` is an `httpx.MockTransport` for unit tests, and `serve()` a real
`ThreadingHTTPServer` on `127.0.0.1` for the end-to-end suite that runs the installed binary.

Tokens are fake and fixed: see `TOKENS`.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
import zoneinfo
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .schema import Openapi, Route

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "openapi.json"

#: Host the unit tests give the client when they use `transport()`: any https name works, the transport ignores it.
HOST = "https://farm.test"

TENANT = "dynapps-internal"
OTHER_TENANT = "acme"

TASK_SCOPES = ("farm:tasks:read", "farm:tasks:write", "farm:tasks:cancel")
CREDENTIAL_SCOPES = ("farm:credentials:read", "farm:credentials:write")
TERMINAL = ("succeeded", "failed", "cancelled", "expired")

ALICE, BOB, OTHER, EXPIRED, ADMIN, READONLY = (
    "tok-alice",
    "tok-bob",
    "tok-other-tenant",
    "tok-expired",
    "tok-admin",
    "tok-readonly",
)


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant: str
    scopes: frozenset[str]
    expires_at: str = "2027-01-01T00:00:00Z"

    @property
    def is_admin(self) -> bool:
        return "farm:tenant:admin" in self.scopes


def _principals() -> dict[str, Principal]:
    everything = frozenset((*TASK_SCOPES, *CREDENTIAL_SCOPES))
    return {
        ALICE: Principal("alice", TENANT, everything),
        BOB: Principal("bob", TENANT, everything),
        OTHER: Principal("carol", OTHER_TENANT, everything),
        EXPIRED: Principal("alice", TENANT, everything, expires_at="2026-09-30T08:00:00Z"),
        ADMIN: Principal("dana", TENANT, everything | {"farm:tenant:admin"}),
        READONLY: Principal("erin", TENANT, frozenset(("farm:tasks:read", "farm:credentials:read"))),
    }


class ApiProblem(Exception):
    """An error answer: raised by a handler, rendered as the farm's error envelope (`farm-api.md` §15)."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retryable, self.details, self.headers = retryable, details, headers or {}


class FakeFarmBug(AssertionError):
    """The fake itself broke the contract (an answer that does not fit the OpenAPI): fix the fake, not the CLI."""


@dataclass
class Recorded:
    method: str
    target: str  # raw path and query, as sent
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]  # lower-cased names
    body: bytes
    operation_id: str | None = None
    status: int | None = None

    @property
    def text(self) -> str:
        return f"{self.method} {self.target}\n{self.headers}\n{self.body.decode('utf-8', 'replace')}"


@dataclass
class Call:
    principal: Principal
    route: Route
    path: dict[str, str]
    query: dict[str, Any]
    body: Any


@dataclass
class TaskRow:
    data: dict[str, Any]
    seq: int
    logs: list[str] = field(default_factory=list)
    schedule_id: str | None = None
    result_expired: bool = False


class FakeFarm:
    def __init__(self) -> None:
        self.openapi = Openapi(json.loads(FIXTURE.read_text()))
        self.tokens: dict[str, Principal] = _principals()
        self.now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
        self.requests: list[Recorded] = []
        self.violations: list[str] = []
        self.bugs: list[str] = []
        self.free_prompt = True
        self.limits = {"max_concurrent_tasks": 10, "max_schedules": 20}
        self.base_url = HOST
        self.credential_values: dict[tuple[str, str, str | None, str], str] = {}
        self._failures: list[dict[str, Any]] = []
        self._seq = 0
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskRow] = {}
        self._schedules: dict[str, dict[str, Any]] = {}
        self._credentials: dict[tuple[str, str, str | None, str], dict[str, Any]] = {}
        self._agents: list[dict[str, Any]] = _platform_agents()
        self._plugins = [
            {
                "name": "developer-skills",
                "version": "1.4.0",
                "commit": "0a1b2c3d",
                "agents": [
                    {"name": "code-reviewer", "agent_id": "developer-skills:code-reviewer", "tools": ["Read", "Grep"]}
                ],
            },
            {"name": "common-skills", "version": "2.0.1"},
        ]
        self._handlers: dict[str, Callable[[Call], tuple[int, Any]]] = {
            name.removeprefix("_op_"): getattr(self, name) for name in dir(self) if name.startswith("_op_")
        }

    # -- ways to talk to it -----------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @contextmanager
    def serve(self) -> Iterator[str]:
        """A real HTTP server on 127.0.0.1 (random port) for the duration of the block; yields its base URL."""
        from .server import serve

        with serve(self) as url:
            yield url

    # -- helpers for tests -------------------------------------------------------------------------------------

    def principal(self, token: str) -> Principal:
        return self.tokens[token]

    def override(
        self, operation_id: str, handler: Callable[[Call], tuple[int, Any]]
    ) -> Callable[[Call], tuple[int, Any]]:
        """Answer an operation differently: `handler(call) -> (status, body)` replaces the model's for this farm.

        For the answer a test needs and the model never gives: a field the farm may add, a task that walks through
        states while it is watched, an empty key list. Returns the handler it replaced, so the new one can wrap it:
        `original = farm.override("whoami", lambda call: ...)`. The answer is still checked against the OpenAPI, and
        the farm is per test, so nothing has to be undone. An unknown `operation_id` is an error, not a new route.
        """
        if operation_id not in self._handlers:
            raise KeyError(f"no such operation: {operation_id}")
        previous, self._handlers[operation_id] = self._handlers[operation_id], handler
        return previous

    def take_violations(self) -> list[str]:
        """The recorded violations, cleared: a test that sends something invalid on purpose asserts them here."""
        taken, self.violations = self.violations, []
        return taken

    def tick(self, seconds: float = 60) -> None:
        self.now += timedelta(seconds=seconds)

    def fail_next(
        self,
        operation_id: str | None = None,
        *,
        status: int = 503,
        code: str = "database_busy",
        message: str = "the database is busy",
        retryable: bool = True,
        retry_after: int | None = None,
        times: int = 1,
    ) -> None:
        """Answer the next `times` requests (of one operation, or any) with this error, before authentication."""
        self._failures.append(
            {
                "operation_id": operation_id,
                "status": status,
                "code": code,
                "message": message,
                "retryable": retryable,
                "retry_after": retry_after,
                "times": times,
            }
        )

    def seed_task(self, token: str, *, state: str = "queued", logs: list[str] | None = None, **body: Any) -> str:
        """Create a task owned by the token's person, straight into the model, in the given state."""
        body.setdefault("agent_id", "developer-skills:code-reviewer")
        row = self._new_task(self.tokens[token], body, origin="api")
        self.set_task(row.data["task_id"], state=state)
        row.logs = list(logs or [])
        return row.data["task_id"]

    def set_task(self, task_id: str, **fields: Any) -> None:
        """Change a task as the farm would (state, usage, failure_reason...). A terminal state stamps `finished_at`."""
        row = self._tasks[task_id]
        row.data.update(fields)
        if fields.get("state") in TERMINAL:
            row.data["finished_at"] = row.data["finished_at"] or self._ts()
        if fields.get("state") in ("running", *TERMINAL):
            row.data["started_at"] = row.data["started_at"] or self._ts()
        row.data["updated_at"] = self._ts()

    def set_schedule(self, schedule_id: str, **fields: Any) -> None:
        """Change a schedule as the farm would (state `finished`, `last_fire_state`, `missed_runs`...)."""
        row = self._schedules[schedule_id]
        row.update(fields)
        row["updated_at"] = self._ts()

    def expire_result(self, task_id: str) -> None:
        self._tasks[task_id].result_expired = True
        self.set_task(task_id, state="expired")

    def recorded_text(self) -> str:
        """Everything that crossed the wire, as one string: for asserting that a secret never did."""
        return "\n".join(r.text for r in self.requests)

    # -- the request pipeline ----------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        target = request.url.raw_path.decode()
        path, _, raw_query = target.partition("?")
        query: dict[str, list[str]] = {}
        for name, value in httpx.QueryParams(raw_query).multi_items():
            query.setdefault(name, []).append(value)
        recorded = Recorded(
            request.method, target, path, query, {k.lower(): v for k, v in request.headers.items()}, request.content
        )
        self.requests.append(recorded)
        try:
            response = self._dispatch(request, recorded)
        except FakeFarmBug as bug:
            self.bugs.append(str(bug))
            raise
        recorded.status = response.status_code
        return response

    def _dispatch(self, request: httpx.Request, recorded: Recorded) -> httpx.Response:
        if request.method == "GET" and recorded.path == "/openapi.json":
            return httpx.Response(200, json=self.openapi.document, request=request)
        if request.method == "GET" and recorded.path.startswith("/_s3/"):
            return self._s3(request, recorded)
        matched = self.openapi.match(request.method, recorded.path)
        if matched is None:
            exists = self.openapi.path_exists(recorded.path)
            self.violations.append(f"unknown operation: {request.method} {recorded.path}")
            return self._problem(
                request,
                ApiProblem(
                    405 if exists else 404, "method_not_allowed" if exists else "not_found", "no such operation"
                ),
            )
        route, path_params = matched
        recorded.operation_id = route.operation_id
        failure = self._next_failure(route)
        if failure is not None:
            headers = {"Retry-After": str(failure["retry_after"])} if failure["retry_after"] is not None else {}
            return self._problem(
                request,
                ApiProblem(
                    failure["status"],
                    failure["code"],
                    failure["message"],
                    retryable=failure["retryable"],
                    headers=headers,
                ),
            )
        try:
            principal = self._authenticate(request)
            missing = [s for s in route.scopes if s not in principal.scopes]
            if missing:
                raise ApiProblem(
                    403, "insufficient_scope", f"this key lacks the scope {missing[0]}", details={"required": missing}
                )
            query = self._read_query(route, recorded.query)
            body = self._read_body(route, request)
            status, payload = self._handlers[route.operation_id](Call(principal, route, path_params, query, body))
        except ApiProblem as problem:
            return self._problem(request, problem)
        self._check_response(route, status, payload)
        headers = {"X-Agent-Farm-Token-Expires-At": principal.expires_at}
        if payload is None:
            return httpx.Response(status, headers=headers, request=request)
        return httpx.Response(status, json=payload, headers=headers, request=request)

    def _authenticate(self, request: httpx.Request) -> Principal:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        principal = self.tokens.get(token.strip()) if scheme.lower() == "bearer" else None
        if principal is None:
            raise ApiProblem(
                401,
                "token_invalid",
                "The key is missing, malformed, unknown or revoked. Stop: retrying will not help.",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="invalid"'},
            )
        if principal.expires_at <= self._ts():
            raise ApiProblem(
                401,
                "token_expired",
                "The key is valid but expired. Get a new one and retry.",
                details={"expires_at": principal.expires_at},
                headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="expired"'},
            )
        return principal

    def _read_query(self, route: Route, query: dict[str, list[str]]) -> dict[str, Any]:
        declared = {p["name"]: p for p in route.parameters if p["in"] == "query"}
        problems: list[dict[str, Any]] = []
        values: dict[str, Any] = {}
        for name, items in query.items():
            if name not in declared:
                self.violations.append(f"{route.operation_id}: undocumented query parameter '{name}'")
                continue
            schema = declared[name].get("schema", {})
            value, error = self.openapi.coerce_query(schema, items[-1])
            found = (
                [{"loc": ["query", name], "msg": error, "type": "int_parsing"}]
                if error
                else [p.as_detail() for p in self.openapi.validate(schema, value, ["query", name])]
            )
            problems += found
            values[name] = value
        for name, parameter in declared.items():
            if name not in values and "default" in parameter.get("schema", {}):
                values[name] = parameter["schema"]["default"]
            if name not in values and parameter.get("required"):
                problems.append({"loc": ["query", name], "msg": "Field required", "type": "missing"})
        if problems:
            self.violations.append(f"{route.operation_id}: invalid query: {problems}")
            raise ApiProblem(422, "request_invalid", "invalid request", details={"detail": problems})
        return values

    def _read_body(self, route: Route, request: httpx.Request) -> Any:
        if route.body_schema is None:
            return None
        try:
            body = json.loads(request.content or b"")
        except ValueError:
            self.violations.append(f"{route.operation_id}: body is not JSON")
            raise ApiProblem(400, "malformed_json", "the body is not valid JSON") from None
        if not isinstance(body, dict):
            self.violations.append(f"{route.operation_id}: body is not an object")
            raise ApiProblem(400, "malformed_json", "the body must be a JSON object")
        if "tenant" in body or "tenant_id" in body:
            self.violations.append(f"{route.operation_id}: tenant sent in the body")
            raise ApiProblem(400, "tenant_not_accepted", "the tenant comes from the credential, never from the body")
        problems = self.openapi.validate(route.body_schema, body, ["body"])
        if problems:
            self.violations.append(f"{route.operation_id}: invalid body: {[p.as_detail() for p in problems]}")
            raise ApiProblem(
                422, "request_invalid", "invalid request", details={"detail": [p.as_detail() for p in problems]}
            )
        return body

    def _next_failure(self, route: Route) -> dict[str, Any] | None:
        for failure in self._failures:
            if failure["operation_id"] in (None, route.operation_id):
                failure["times"] -= 1
                if failure["times"] <= 0:
                    self._failures.remove(failure)
                return failure
        return None

    # -- answers ----------------------------------------------------------------------------------------------

    def _problem(self, request: httpx.Request, problem: ApiProblem) -> httpx.Response:
        headers = dict(problem.headers)
        if problem.code == "request_invalid" and problem.details and "detail" in problem.details:
            body: dict[str, Any] = {"detail": problem.details["detail"]}  # FastAPI's own 422 shape
        else:
            error: dict[str, Any] = {"code": problem.code, "message": problem.message, "retryable": problem.retryable}
            if problem.details:
                error["details"] = problem.details
            body = {"error": error}
        schema = {
            "$ref": "#/components/schemas/HTTPValidationError"
            if "detail" in body
            else "#/components/schemas/ErrorResponse"
        }
        found = self.openapi.validate(schema, body, [])
        if found:
            raise FakeFarmBug(f"error answer does not fit the OpenAPI: {[p.as_detail() for p in found]}")
        return httpx.Response(problem.status, json=body, headers=headers, request=request)

    def _check_response(self, route: Route, status: int, payload: Any) -> None:
        declared = route.responses.get(str(status))
        if declared is None:
            raise FakeFarmBug(f"{route.operation_id} answered {status}, which the OpenAPI does not declare")
        schema = declared.get("content", {}).get("application/json", {}).get("schema")
        if schema is None:
            if payload is not None:
                raise FakeFarmBug(f"{route.operation_id} {status} must have no body")
            return
        problems = self.openapi.validate(schema, payload, [])
        if problems:
            raise FakeFarmBug(
                f"{route.operation_id} answer does not fit the OpenAPI: {[p.as_detail() for p in problems]}"
            )

    def _s3(self, request: httpx.Request, recorded: Recorded) -> httpx.Response:
        """The presigned result URL: no authentication, as an object store (the CLI must not send its token here)."""
        match = re.fullmatch(r"/_s3/([^/]+)/result\.json", recorded.path)
        row = self._tasks.get(match.group(1)) if match else None
        if row is None or row.data["state"] != "succeeded":
            return httpx.Response(404, text="NoSuchKey", request=request)
        return httpx.Response(
            200, content=self._result_object(row), headers={"Content-Type": "application/json"}, request=request
        )

    def _result_object(self, row: TaskRow) -> bytes:
        """The stored result file of a succeeded task: what the presigned URL serves, and what `sha256` hashes."""
        document = {
            "task_id": row.data["task_id"],
            "tenant": row.data["tenant"],
            "scope": row.data["scope"] or "_",
            "agent_id": row.data["agent_id"] or "",
            "state": "succeeded",
            "exit_code": 0,
            "result": "The change looks fine.",
            "session_ref": row.data["session_ref"] or "sess_fake",
            "resumable": True,
            "usage": {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.01, "duration_s": 5, "num_turns": 2},
            "started_at": row.data["started_at"],
            "finished_at": row.data["finished_at"],
        }
        problems = self.openapi.validate({"$ref": "#/components/schemas/ResultDocument"}, document, [])
        if problems:
            raise FakeFarmBug(f"result document does not fit the OpenAPI: {[p.as_detail() for p in problems]}")
        return json.dumps(document, sort_keys=True).encode()

    # -- model helpers -----------------------------------------------------------------------------------------

    def _ts(self, offset: float = 0) -> str:
        return (self.now + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _new_id(self, kind: str) -> str:
        self._seq += 1
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"fakefarm/{kind}/{self._seq}"))

    @staticmethod
    def _owns(principal: Principal, tenant: str | None, owner: str | None) -> bool:
        """Ownership as `farm-api.md` §4.1 has it: the tenant, then the person, unless the caller is a tenant admin."""
        if tenant != principal.tenant:
            return False
        return principal.is_admin or owner == principal.subject

    def _task_for(self, call: Call) -> TaskRow:
        row = self._tasks.get(call.path["task_id"])
        if row is None or not self._owns(call.principal, row.data["tenant"], row.data["owner"]):
            raise ApiProblem(404, "not_found", "no such task")
        return row

    def _task_out(self, principal: Principal, row: TaskRow) -> dict[str, Any]:
        out = dict(row.data)
        if not principal.is_admin:
            out.pop("owner", None)
        return out

    def _check_task_body(self, principal: Principal, body: dict[str, Any]) -> None:
        agent_id, prompt = body.get("agent_id"), body.get("prompt")
        if bool(agent_id) == bool(prompt):
            raise ApiProblem(
                422, "agent_or_prompt_required", "send either an agent_id or a prompt, not both and not neither"
            )
        if prompt and not self.free_prompt:
            raise ApiProblem(422, "prompt_not_enabled_for_tenant", "this tenant only accepts catalogue agents")
        if agent_id and self._find_agent(principal, agent_id) is None:
            raise ApiProblem(404, "agent_not_found", f"no active agent '{agent_id}' in the catalogue")

    def _active_tasks(self, principal: Principal) -> int:
        return sum(
            1 for r in self._tasks.values() if r.data["tenant"] == principal.tenant and r.data["state"] not in TERMINAL
        )

    def _new_task(
        self,
        principal: Principal,
        body: dict[str, Any],
        *,
        origin: str,
        trigger: str | None = None,
        schedule_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TaskRow:
        task_id = self._new_id("task")
        workspace = body.get("workspace")
        callback = body.get("callback")
        limits = {"timeout_s": 900, "cpu": "1", "memory": "2Gi", "ttl_s": 3600}
        limits.update({k: v for k, v in (body.get("limits") or {}).items() if v is not None})
        data: dict[str, Any] = {
            "task_id": task_id,
            "tenant": principal.tenant,
            "scope": body.get("scope"),
            "agent_id": body.get("agent_id"),
            "prompt": body.get("prompt"),
            "input": body.get("input"),
            "state": "queued",
            "failure_reason": None,
            "correlation_id": correlation_id or body.get("correlation_id") or f"auto-{task_id[:8]}",
            "origin": origin if origin in ("ui", "api", "agent", "schedule") else "api",
            "limits": limits,
            "marketplace": None,
            "mcp_servers": [
                {
                    "name": s["name"],
                    "url": s.get("url") or f"https://mcp.invalid/{s['name']}",
                    "credential": s["credential"],
                    "header": s.get("header"),
                }
                for s in body.get("mcp_servers", [])
            ],
            "session_ref": body.get("session_ref"),
            "resumable": None,
            "session_agent_changed": None,
            "created_at": self._ts(),
            "started_at": None,
            "finished_at": None,
            "updated_at": self._ts(),
            "owner": principal.subject,
            "image": body.get("image"),
            "work_size": body.get("work_size"),
            "workspace": {
                "repo": workspace["repo"],
                "ref": workspace.get("ref"),
                "credentials_ref": workspace.get("credentials_ref"),
            }
            if workspace
            else None,
            "callback": {
                "url": callback["url"],
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "last_attempt_at": None,
                "next_attempt_at": None,
                "delivered_at": None,
            }
            if callback
            else None,
            "queue_position": None,
            "usage": None,
            "result": None,
            "logs": None,
            "trigger": trigger,
        }
        row = TaskRow(data, seq=self._seq, schedule_id=schedule_id)
        self._tasks[task_id] = row
        return row

    def _find_agent(self, principal: Principal, slug: str, scope: str | None = None) -> dict[str, Any] | None:
        for row in self._agents:
            if row["slug"] != slug or (scope is not None and row["scope"] != scope):
                continue
            if row["scope"] == "platform" or (
                row["_tenant"] == principal.tenant
                and (row["scope"] == "team" or self._owns(principal, row["_tenant"], row["owner"]))
            ):
                return row
        return None

    @staticmethod
    def _agent_out(row: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    # -- operations: identity ---------------------------------------------------------------------------------

    def _op_whoami(self, call: Call) -> tuple[int, Any]:
        p = call.principal
        return 200, {
            "subject": p.subject,
            "tenant": p.tenant,
            "scopes": sorted(p.scopes),
            "is_admin": p.is_admin,
            "expires_at": p.expires_at,
            "free_prompt": self.free_prompt,
        }

    def _op_callback_key(self, call: Call) -> tuple[int, Any]:
        key = {
            "kid": "fake-key-1",
            "kty": "OKP",
            "crv": "Ed25519",
            "alg": "EdDSA",
            "use": "sig",
            "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
            "pem": "-----BEGIN PUBLIC KEY-----\nFAKE\n-----END PUBLIC KEY-----\n",
        }
        return 200, {"keys": [key]}

    def _op_list_plugins(self, call: Call) -> tuple[int, Any]:
        return 200, {"items": self._plugins}

    # -- operations: tasks ------------------------------------------------------------------------------------

    def _op_create_task(self, call: Call) -> tuple[int, Any]:
        p, body = call.principal, call.body
        self._check_task_body(p, body)
        correlation_id = body.get("correlation_id")
        if correlation_id:
            for row in self._tasks.values():
                if (
                    row.data["tenant"] == p.tenant
                    and row.data["owner"] == p.subject
                    and row.data["correlation_id"] == correlation_id
                ):
                    return 200, self._created(row)
        if self._active_tasks(p) >= self.limits["max_concurrent_tasks"]:
            raise ApiProblem(
                429,
                "quota_exceeded",
                "the tenant has reached max_concurrent_tasks",
                retryable=True,
                headers={"Retry-After": "5"},
            )
        row = self._new_task(p, body, origin=body.get("origin") or "api")
        return 201, self._created(row)

    @staticmethod
    def _created(row: TaskRow) -> dict[str, Any]:
        d = row.data
        return {k: d[k] for k in ("task_id", "tenant", "scope", "state", "correlation_id", "created_at")}

    def _op_list_tasks(self, call: Call) -> tuple[int, Any]:
        p, q = call.principal, call.query
        rows = [r for r in self._tasks.values() if self._owns(p, r.data["tenant"], r.data["owner"])]
        states = [s for s in (q.get("state") or "").split(",") if s]
        valid = self.openapi.document["components"]["schemas"]["TaskOut"]["properties"]["state"]["enum"]
        if any(s not in valid for s in states):
            raise ApiProblem(422, "state_invalid", f"state must be one of {valid}")
        owner = q.get("owner")
        if owner:
            wanted = p.subject if owner == "me" else owner
            rows = [r for r in rows if r.data["owner"] == wanted]
        for field_name in ("agent_id", "scope", "session_ref"):
            if q.get(field_name):
                rows = [r for r in rows if r.data[field_name] == q[field_name]]
        if states:
            rows = [r for r in rows if r.data["state"] in states]
        for key, name, keep in (
            ("since", "created_at", lambda a, b: a >= b),
            ("until", "created_at", lambda a, b: a <= b),
            ("updated_since", "updated_at", lambda a, b: a >= b),
        ):
            if q.get(key):
                bound = _instant(q[key])
                rows = [r for r in rows if keep(_instant(r.data[name]), bound)]
        rows.sort(
            key=lambda r: (r.data["updated_at"], r.seq) if q.get("updated_since") else (r.data["created_at"], r.seq),
            reverse=not q.get("updated_since"),
        )
        cursor = q.get("cursor")
        if cursor is not None and not cursor.isdigit():
            raise ApiProblem(422, "cursor_invalid", "the cursor is not one this listing issued")
        start, limit = int(cursor or 0), q["limit"]
        page = rows[start : start + limit]
        more = start + limit < len(rows)
        return 200, {"items": [self._task_out(p, r) for r in page], "next_cursor": str(start + limit) if more else None}

    def _op_get_task(self, call: Call) -> tuple[int, Any]:
        return 200, self._task_out(call.principal, self._task_for(call))

    def _op_cancel_task(self, call: Call) -> tuple[int, Any]:
        row = self._task_for(call)
        if row.data["state"] in TERMINAL:
            raise ApiProblem(409, "task_not_cancellable", f"the task is already {row.data['state']}")
        self.set_task(row.data["task_id"], state="cancelled")
        d = row.data
        return 202, {"task_id": d["task_id"], "tenant": d["tenant"], "scope": d["scope"], "state": "cancelled"}

    def _op_get_logs(self, call: Call) -> tuple[int, Any]:
        row = self._task_for(call)
        d, tail = row.data, call.query.get("tail_lines") or 200
        full = (
            {
                "s3_key": f"{d['tenant']}/tasks/{d['task_id']}/logs/agent.jsonl",
                "url": f"{self.base_url}/_s3/{d['task_id']}/logs",
                "expires_at": self._ts(300),
            }
            if d["state"] in TERMINAL
            else None
        )
        return 200, {
            "task_id": d["task_id"],
            "tenant": d["tenant"],
            "scope": d["scope"],
            "state": d["state"],
            "lines": row.logs[-tail:],
            "truncated": len(row.logs) > tail,
            "full_logs": full,
        }

    def _op_get_result(self, call: Call) -> tuple[int, Any]:
        row = self._task_for(call)
        d = row.data
        if row.result_expired or d["state"] == "expired":
            raise ApiProblem(410, "result_expired", "the result passed its retention")
        if d["state"] not in TERMINAL:
            raise ApiProblem(409, "task_not_terminal", "the task has not finished; wait for it with ?wait=")
        if d["state"] != "succeeded":
            raise ApiProblem(409, "task_has_no_result", f"the task {d['state']} and produced no result")  # invented
        stored = self._result_object(row) if d["state"] == "succeeded" else b""
        result = {
            "s3_key": f"{d['tenant']}/tasks/{d['task_id']}/result.json",
            "sha256": hashlib.sha256(stored).hexdigest(),
            "size_bytes": len(stored),
            "content_type": "application/json",
            "url": f"{self.base_url}/_s3/{d['task_id']}/result.json?X-Signature=fake",
            "expires_at": self._ts(300),
        }
        usage = {"input_tokens": 100, "output_tokens": 20, "cost_eur": 0.01, "duration_s": 5}
        return 200, {
            "task_id": d["task_id"],
            "tenant": d["tenant"],
            "scope": d["scope"],
            "state": d["state"],
            "result": result,
            "summary": "The change looks fine.",
            "usage": usage,
            "session_ref": d["session_ref"],
            "resumable": True,
        }

    # -- operations: agents -----------------------------------------------------------------------------------

    def _op_list_agents(self, call: Call) -> tuple[int, Any]:
        p, q = call.principal, call.query
        rows = [r for r in self._agents if self._find_agent(p, r["slug"], r["scope"]) is r]
        if q.get("scope"):
            rows = [r for r in rows if r["scope"] == q["scope"]]
        if q.get("owner"):
            wanted = p.subject if q["owner"] == "me" else q["owner"]
            rows = [r for r in rows if r["owner"] == wanted]
        return 200, {"items": [self._agent_out(r) for r in sorted(rows, key=lambda r: (r["scope"], r["slug"]))]}

    def _op_get_agent(self, call: Call) -> tuple[int, Any]:
        slug = call.path["slug"]
        for scope in ("personal", "team", "platform"):
            row = self._find_agent(call.principal, slug, scope)
            if row is not None:
                return 200, self._agent_out(row)
        raise ApiProblem(404, "agent_not_found", f"no agent '{slug}'")

    def _agent_fields(self, body: dict[str, Any]) -> dict[str, Any]:
        """The part of an AgentOut that comes from an AgentCreate/AgentUpdate body."""
        out: dict[str, Any] = {}
        for name in (
            "description",
            "instructions",
            "plugin_agent",
            "env_credentials",
            "requires_workspace",
            "image",
            "profile",
            "work_size",
        ):
            if name in body:
                out[name] = body[name]
        if "mcp_servers" in body:
            out["mcp_servers"] = [
                {
                    "name": s["name"],
                    "url": s.get("url") or f"https://mcp.invalid/{s['name']}",
                    "credential": s["credential"],
                    "header": s.get("header"),
                }
                for s in body["mcp_servers"]
            ]
        if "limits" in body:
            out["default_limits"] = {k: v for k, v in (body["limits"] or {}).items() if v is not None}
        return out

    def _op_create_agent(self, call: Call) -> tuple[int, Any]:
        p, body = call.principal, call.body
        if body["scope"] == "team" and not p.is_admin:
            raise ApiProblem(
                403,
                "insufficient_scope",
                "a team agent needs farm:tenant:admin",
                details={"required": ["farm:tenant:admin"]},
            )
        if any(
            r["slug"] == body["slug"]
            and (
                r["scope"] == "platform"
                or r["_tenant"] == p.tenant
                and (r["scope"] == "team" or r["owner"] == p.subject)
            )
            for r in self._agents
        ):
            raise ApiProblem(409, "agent_slug_taken", f"the slug '{body['slug']}' is already used")
        row: dict[str, Any] = {
            "slug": body["slug"],
            "scope": body["scope"],
            "owner": p.subject if body["scope"] == "personal" else None,
            "description": None,
            "instructions": None,
            "plugin_agent": None,
            "mcp_servers": [],
            "env_credentials": {},
            "harness": "claude-code",
            "requires_workspace": False,
            "capabilities": {
                "credentials": list(body.get("credentials") or []),
                "egress": list(body.get("egress") or ["model-gateway", "object-store"]),
            },
            "default_limits": {},
            "model_alias": "sonnet",
            "_tenant": p.tenant,
        }
        row.update(self._agent_fields(body))
        self._agents.append(row)
        return 201, self._agent_out(row)

    def _agent_target(self, call: Call) -> dict[str, Any]:
        """The agent a PATCH or DELETE means: `?scope=` picks one, else the personal one then the team's, as the farm
        resolves a slug (`farm-api.md` §7.6). A platform agent is read-only (403), a team one needs a tenant admin."""
        slug, scope = call.path["slug"], call.query.get("scope")
        row = None
        for candidate in (scope,) if scope else ("personal", "team", "platform"):
            row = self._find_agent(call.principal, slug, candidate)
            if row is not None:
                break
        if row is None:
            raise ApiProblem(404, "agent_not_found", f"no {scope + ' ' if scope else ''}agent '{slug}'")
        if row["scope"] == "platform":
            raise ApiProblem(403, "agent_read_only", f"'{slug}' is a platform agent and cannot be changed")
        if row["scope"] == "team" and not call.principal.is_admin:
            raise ApiProblem(
                403,
                "insufficient_scope",
                "a team agent needs farm:tenant:admin",
                details={"required": ["farm:tenant:admin"]},
            )
        return row

    def _op_update_agent(self, call: Call) -> tuple[int, Any]:
        row = self._agent_target(call)
        row.update(self._agent_fields(call.body))
        if "credentials" in call.body:
            row["capabilities"]["credentials"] = list(call.body["credentials"] or [])
        if "egress" in call.body and call.body["egress"] is not None:
            row["capabilities"]["egress"] = list(call.body["egress"])
        return 200, self._agent_out(row)

    def _op_delete_agent(self, call: Call) -> tuple[int, Any]:
        row = self._agent_target(call)
        for schedule in self._schedules.values():
            if (
                schedule["tenant"] == call.principal.tenant
                and schedule["state"] != "finished"
                and schedule["task"].get("agent_id") == row["slug"]
            ):
                raise ApiProblem(
                    409, "agent_in_use_by_schedule", "a schedule that is not finished still launches this agent"
                )
        self._agents.remove(row)
        return 204, None

    # -- operations: schedules --------------------------------------------------------------------------------

    def _schedule_for(self, call: Call) -> dict[str, Any]:
        row = self._schedules.get(call.path["schedule_id"])
        if row is None or not self._owns(call.principal, row["tenant"], row["owner"]):
            raise ApiProblem(404, "not_found", "schedule not found")
        return row

    @staticmethod
    def _schedule_out(row: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    def _op_create_schedule(self, call: Call) -> tuple[int, Any]:
        p, body = call.principal, call.body
        task_problems = self.openapi.validate(
            {"$ref": "#/components/schemas/TaskCreate"}, body["task"], ["body", "task"]
        )
        if task_problems:
            detail = [x.as_detail() for x in task_problems]
            self.violations.append(f"create_schedule: invalid task: {detail}")
            raise ApiProblem(422, "request_invalid", "invalid request", details={"detail": detail})
        self._check_task_body(p, body["task"])
        recurrence = body["recurrence"]
        if not re.fullmatch(r"\S+( \S+){4}", recurrence["cron"].strip()):
            raise ApiProblem(422, "cron_invalid", "cron must have exactly 5 fields: minute hour day month weekday")
        try:
            zoneinfo.ZoneInfo(recurrence["timezone"])
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            raise ApiProblem(422, "timezone_invalid", "unknown IANA timezone") from None
        if sum(1 for s in self._schedules.values() if s["tenant"] == p.tenant) >= self.limits["max_schedules"]:
            raise ApiProblem(
                429,
                "quota_exceeded",
                "the tenant has reached max_schedules",
                retryable=True,
                headers={"Retry-After": "5"},
            )
        schedule_id = self._new_id("schedule")
        row = {
            "schedule_id": schedule_id,
            "tenant": p.tenant,
            "owner": p.subject,
            "task": body["task"],
            "recurrence": {k: recurrence.get(k) for k in ("cron", "timezone", "starts_at", "ends_at", "max_runs")},
            "overlap": body.get("overlap", "skip"),
            "state": "active",
            "next_fire_at": self._ts(3600),
            "missed_runs": 0,
            "runs_count": 0,
            "last_fired_at": None,
            "last_fire_state": None,
            "last_fire_error": None,
            "created_at": self._ts(),
            "updated_at": self._ts(),
        }
        self._schedules[schedule_id] = row
        return 201, self._schedule_out(row)

    def _op_list_schedules(self, call: Call) -> tuple[int, Any]:
        p, q = call.principal, call.query
        rows = [r for r in self._schedules.values() if self._owns(p, r["tenant"], r["owner"])]
        if q.get("owner"):
            wanted = p.subject if q["owner"] == "me" else q["owner"]
            rows = [r for r in rows if r["owner"] == wanted]
        return 200, {
            "items": [
                self._schedule_out(r)
                for r in sorted(rows, key=lambda r: (r["created_at"], r["schedule_id"]), reverse=True)
            ]
        }

    def _op_get_schedule(self, call: Call) -> tuple[int, Any]:
        return 200, self._schedule_out(self._schedule_for(call))

    def _op_delete_schedule(self, call: Call) -> tuple[int, Any]:
        row = self._schedule_for(call)
        del self._schedules[row["schedule_id"]]
        return 204, None

    def _set_schedule_state(self, call: Call, state: str) -> tuple[int, Any]:
        row = self._schedule_for(call)
        if row["state"] == "finished":
            raise ApiProblem(409, "schedule_finished", "a finished schedule cannot be paused or resumed")
        row["state"], row["updated_at"] = state, self._ts()
        return 200, self._schedule_out(row)

    def _op_pause_schedule(self, call: Call) -> tuple[int, Any]:
        return self._set_schedule_state(call, "paused")

    def _op_resume_schedule(self, call: Call) -> tuple[int, Any]:
        return self._set_schedule_state(call, "active")

    def _op_list_schedule_tasks(self, call: Call) -> tuple[int, Any]:
        row = self._schedule_for(call)
        fired = [r for r in self._tasks.values() if r.schedule_id == row["schedule_id"]]
        fired.sort(key=lambda r: (r.data["created_at"], r.seq), reverse=True)
        return 200, {"items": [self._task_out(call.principal, r) for r in fired], "next_cursor": None}

    def _op_fire_schedule(self, call: Call) -> tuple[int, Any]:
        schedule = self._schedule_for(call)
        if schedule["state"] != "active":
            raise ApiProblem(409, "schedule_not_active", f"the schedule is {schedule['state']}")
        if schedule["overlap"] == "skip" and any(
            r.schedule_id == schedule["schedule_id"] and r.data["state"] not in TERMINAL for r in self._tasks.values()
        ):
            raise ApiProblem(409, "schedule_overlap", "the previous task of this schedule is still running")
        owner = Principal(schedule["owner"], schedule["tenant"], call.principal.scopes)
        self._seq += 1
        row = self._new_task(
            owner,
            schedule["task"],
            origin="schedule",
            trigger="manual",
            schedule_id=schedule["schedule_id"],
            correlation_id=f"sched-manual-{self._seq}",
        )
        schedule["last_fired_at"], schedule["last_fire_state"] = self._ts(), "ok"
        return 201, self._task_out(call.principal, row)

    # -- operations: credentials ------------------------------------------------------------------------------

    def _credential_key(self, principal: Principal, scope: str, name: str) -> tuple[str, str, str | None, str]:
        return (principal.tenant, scope, principal.subject if scope == "personal" else None, name)

    @staticmethod
    def _credential_out(row: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in row.items() if not k.startswith("_")}

    def _op_write_credential(self, call: Call) -> tuple[int, Any]:
        p, body = call.principal, call.body
        scope = body.get("scope", "personal")
        if scope == "shared" and not p.is_admin:
            raise ApiProblem(
                403,
                "insufficient_scope",
                "a shared credential needs farm:tenant:admin",
                details={"required": ["farm:tenant:admin"]},
            )
        name = body["name"]
        if "/" in name or ".." in name:
            raise ApiProblem(422, "credentials_ref_out_of_tenant", "a credential name cannot contain '/' or '..'")
        if body["type"] == "mcp_url" and not body["value"].startswith("https://"):
            raise ApiProblem(422, "credential_value_invalid", "an mcp_url credential must be an https URL")
        key = self._credential_key(p, scope, name)
        existing = self._credentials.get(key)
        row = existing or {
            "name": name,
            "scope": scope,
            "tenant": p.tenant,
            "metadata": {},
            "description": None,
            "expires_at": None,
            "created_at": self._ts(),
        }
        row.update(
            type=body["type"],
            fingerprint="sha256:" + hashlib.sha256(body["value"].encode()).hexdigest()[:8],
            updated_at=self._ts(),
        )
        if "metadata" in body:
            row["metadata"] = body["metadata"]
        if "description" in body:
            row["description"] = body["description"] or None
        if "expires_at" in body:
            row["expires_at"] = body["expires_at"]
        self._credentials[key] = row
        self.credential_values[key] = body["value"]
        return (200 if existing else 201), self._credential_out(row)

    def _op_list_credentials(self, call: Call) -> tuple[int, Any]:
        p, scope = call.principal, call.query["scope"]
        rows = [
            r
            for k, r in self._credentials.items()
            if k[0] == p.tenant and k[1] == scope and (scope == "shared" or self._owns(p, k[0], k[2]))
        ]
        return 200, {"items": [self._credential_out(r) for r in sorted(rows, key=lambda r: r["name"])]}

    def _credential_for(self, call: Call, *, writing: bool) -> tuple[tuple[str, str, str | None, str], dict[str, Any]]:
        p, scope, name = call.principal, call.query["scope"], call.path["name"]
        if writing and scope == "shared" and not p.is_admin:
            raise ApiProblem(
                403,
                "insufficient_scope",
                "a shared credential needs farm:tenant:admin",
                details={"required": ["farm:tenant:admin"]},
            )
        key = self._credential_key(p, scope, name)
        row = self._credentials.get(key)
        if row is None:
            raise ApiProblem(404, "credentials_not_found", f"no credential '{name}'")
        return key, row

    def _op_get_credential(self, call: Call) -> tuple[int, Any]:
        return 200, self._credential_out(self._credential_for(call, writing=False)[1])

    def _op_delete_credential(self, call: Call) -> tuple[int, Any]:
        key, _ = self._credential_for(call, writing=True)
        del self._credentials[key]
        del self.credential_values[key]
        return 204, None


def _instant(text: str) -> datetime:
    when = datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _platform_agents() -> list[dict[str, Any]]:
    def agent(slug: str, description: str, *, credentials: list[str], workspace: bool) -> dict[str, Any]:
        return {
            "slug": slug,
            "scope": "platform",
            "owner": None,
            "description": description,
            "instructions": None,
            "plugin_agent": None,
            "mcp_servers": [],
            "env_credentials": {},
            "harness": "claude-code",
            "requires_workspace": workspace,
            "capabilities": {
                "credentials": credentials,
                "egress": ["model-gateway", "object-store", "gitlab.dynapps.be"],
            },
            "default_limits": {"timeout_s": 900, "cpu": "1", "memory": "2Gi"},
            "model_alias": "sonnet",
            "_tenant": None,
        }

    return [
        agent(
            "developer-skills:code-reviewer", "Reviews a merge request", credentials=["git-readonly"], workspace=True
        ),
        agent(
            "repo-helper",
            "Reads a repository and answers questions about it",
            credentials=["git-readonly"],
            workspace=True,
        ),
    ]
