"""Strands Box playground: one JSON API over microvm-ctl and the strands-box image, and one HTML page.

    python playground/server.py                 # http://127.0.0.1:8770, against the live service
    python playground/server.py --port 9000

The same `Playground.api(method, path, query, body)` serves the local HTTP server and the Lambda
Function URL behind CloudFront (`lambda_handler`). Every call is short: launches return at once and the
page polls, so nothing needs a background thread that outlives a request.

Guardrails, whatever the page asks for: at most SBX_MAX_VMS microVMs of the image at once, every launch
carries an idle policy (suspend after 5 minutes without traffic, terminate after 15 minutes suspended)
and a 30 minute lifetime cap, and a fan-out is capped at SBX_MAX_TASKS tasks.

When PLAYGROUND_KEY is set, every /api call must carry it in the x-playground-key header.
"""

from __future__ import annotations

import base64
import glob
import hmac
import json
import os
import random
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from microvm import EndpointClient, FleetManager, FleetMonitor, ImageBuilder, PlaneConfig
from microvm.fleet import Fleet, IdlePolicy
from microvm.lease import Lease, LeasePlanRejected, LeasePolicy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
IMAGE = os.environ.get("SBX_IMAGE", "strands-box")
MAX_VMS = int(os.environ.get("SBX_MAX_VMS", "8"))
MAX_TASKS = int(os.environ.get("SBX_MAX_TASKS", "256"))
BASELINE_MIB = int(os.environ.get("SBX_BASELINE_MIB", "1024"))
IDLE = IdlePolicy(max_idle=300, suspended_for=900, auto_resume=True)
MAX_DURATION = 1800
STATIC = os.environ.get("SBX_STATIC", os.path.join(HERE, "static"))
BOX_FILES = os.environ.get("SBX_BOX_FILES", os.path.join(REPO, "image", "box"))
RESULTS = os.environ.get("SBX_RESULTS", os.path.join(REPO, "results"))

PRESETS = [
    {"id": "list", "label": "List the project", "step": "list", "expect": "permit"},
    {"id": "readme", "label": "Read README.md", "step": "read:README.md", "expect": "permit"},
    {"id": "env", "label": "Read .env", "step": "read:.env", "expect": "deny: no_env"},
    {"id": "delete", "label": "Delete scratch.txt", "step": "run:rm scratch.txt", "expect": "deny: no_deletes"},
    {"id": "count", "label": "Count Python lines", "step": "run:find . -name '*.py' | xargs wc -l",
     "expect": "permit"},
    {"id": "write", "label": "Write out/report.txt", "step": "run:echo 'built in a box' > out/report.txt",
     "expect": "permit: out_write"},
    {"id": "escape", "label": "Write outside out/", "step": "run:echo pwned > hello.py", "expect": "deny"},
    {"id": "etc", "label": "Read /etc/passwd", "step": "read:/etc/passwd", "expect": "deny"},
]


class Trace:
    """Every AWS API call this process makes: operation, duration, outcome. Kept in memory."""

    def __init__(self, size: int = 200):
        self.items: deque = deque(maxlen=size)
        self._starts: dict = {}
        self._lock = threading.Lock()

    def attach(self, client) -> None:
        events = client.meta.events
        events.register("before-call.*.*", self._before)
        events.register("after-call.*.*", self._after)

    def _before(self, model, context, **_kw):
        context["sbx_t0"] = time.time()

    def _after(self, http_response, parsed, model, context, **_kw):
        t0 = context.get("sbx_t0") or time.time()
        status = getattr(http_response, "status_code", None)
        err = (parsed or {}).get("Error", {}).get("Code") if isinstance(parsed, dict) else None
        with self._lock:
            self.items.appendleft({"at": time.strftime("%H:%M:%S"), "service": model.service_model.service_name,
                                   "op": model.name, "status": status, "error": err,
                                   "ms": round((time.time() - t0) * 1000, 1)})

    def add(self, service: str, op: str, status, ms: float, error: str | None = None) -> None:
        with self._lock:
            self.items.appendleft({"at": time.strftime("%H:%M:%S"), "service": service, "op": op,
                                   "status": status, "error": error, "ms": round(ms, 1)})


class Playground:
    def __init__(self):
        self.cfg = PlaneConfig()
        self.fm = FleetManager(self.cfg)
        self.mon = FleetMonitor(self.cfg)
        self.trace = Trace()
        self.trace.attach(self.fm.api)
        self.fleet = Fleet(self.fm, IMAGE, idle_policy=IDLE, max_duration=MAX_DURATION)
        self._clients: dict = {}
        self._lock = threading.Lock()
        self._rr = 0

    # ── helpers ────────────────────────────────────────────────────────────────────────────────
    def client(self, microvm_id: str) -> EndpointClient:
        with self._lock:
            c = self._clients.get(microvm_id)
            if c is None:
                c = self._clients[microvm_id] = EndpointClient(self.cfg, microvm_id)
            return c

    def call_vm(self, microvm_id: str, method: str, path: str, body=None, timeout: float = 120):
        t0 = time.time()
        resp = self.client(microvm_id).request(method, path, json=body, timeout=timeout)
        ms = (time.time() - t0) * 1000
        self.trace.add("microvm-endpoint", f"{method} {path}", resp.status_code, ms)
        try:
            return resp.status_code, resp.json(), round(ms, 1)
        except ValueError:
            return resp.status_code, {"raw": resp.text[:2000]}, round(ms, 1)

    def vms(self) -> list[dict]:
        out = []
        for vm in self.fm.list(IMAGE):
            if vm.state == "TERMINATED":
                continue
            d = {"microvmId": vm.microvm_id, "state": vm.state, "imageVersion": vm.image_version,
                 "startedAt": str(vm.started_at or ""), "endpoint": vm.endpoint}
            d["age_s"] = round(time.time() - vm.started_epoch) if vm.started_epoch else None
            out.append(d)
        return sorted(out, key=lambda d: d.get("age_s") or 0)

    def running(self) -> list[str]:
        return [v["microvmId"] for v in self.vms() if v["state"] == "RUNNING"]

    def pick(self) -> str:
        ids = self.running()
        if not ids:
            raise ValueError("no RUNNING microVM: launch one on the Fleet tab first")
        with self._lock:
            self._rr += 1
            return ids[self._rr % len(ids)]

    def active_count(self) -> int:
        return sum(1 for v in self.vms() if v["state"] in ("PENDING", "RUNNING", "SUSPENDING", "SUSPENDED"))

    # ── routes ─────────────────────────────────────────────────────────────────────────────────
    def overview(self, _q, _b):
        builder = ImageBuilder(self.cfg)
        versions = []
        try:
            versions = [{"version": v.get("imageVersion"), "state": v.get("state") or v.get("status"),
                         "created": str(v.get("createdAt") or v.get("creationTime") or "")}
                        for v in builder.list_versions(IMAGE)]
        except Exception as e:  # the page still works without the version list
            versions = [{"error": str(e)}]
        vms = self.vms()
        by_state: dict = {}
        for v in vms:
            by_state[v["state"]] = by_state.get(v["state"], 0) + 1
        return 200, {"image": IMAGE, "region": self.cfg.region, "versions": versions, "vms": vms,
                     "by_state": by_state, "max_vms": MAX_VMS, "max_tasks": MAX_TASKS,
                     "launch_tps": self.fm.tps("RunMicrovm"), "memory_quota_gb": self.fm.memory_quota_gb,
                     "baseline_mib": BASELINE_MIB, "presets": PRESETS,
                     "guardrails": {"max_idle_s": IDLE.max_idle, "suspended_for_s": IDLE.suspended_for,
                                    "max_duration_s": MAX_DURATION}}

    def list_vms(self, _q, _b):
        return 200, {"vms": self.vms()}

    def launch(self, _q, body):
        n = max(1, min(int(body.get("count", 1)), MAX_VMS))
        room = MAX_VMS - self.active_count()
        if room <= 0:
            return 409, {"error": f"the playground caps {IMAGE} at {MAX_VMS} microVMs; terminate some first"}
        n = min(n, room)
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=min(n, 8)) as pool:
            vms = list(pool.map(lambda _i: self.fm.run(IMAGE, idle_policy=IDLE, max_duration=MAX_DURATION),
                                range(n)))
        return 200, {"launched": [v.microvm_id for v in vms], "ms": round((time.time() - t0) * 1000, 1),
                     "capped": n < int(body.get("count", 1))}

    def lifecycle(self, microvm_id: str, verb: str):
        t0 = time.time()
        getattr(self.fm, verb)(microvm_id)
        if verb == "terminate":
            self._clients.pop(microvm_id, None)
        return 200, {"ok": True, "verb": verb, "microvm_id": microvm_id, "ms": round((time.time() - t0) * 1000, 1)}

    def fleet_action(self, _q, body):
        action = body.get("action")
        t0 = time.time()
        if action == "scale":
            n = max(0, min(int(body.get("n", 0)), MAX_VMS))
            launched = self.fleet.scale_to(n)
            result = {"desired": n, "launched": [v.microvm_id for v in launched]}
        elif action == "suspend_all":
            result = {"suspended": self.fleet.suspend_all()}
        elif action == "resume_all":
            result = {"resumed": self.fleet.resume_all()}
        elif action == "drain":
            result = {"terminated": self.fleet.drain()}
            self._clients.clear()
        else:
            return 400, {"error": f"unknown fleet action {action!r}"}
        result["ms"] = round((time.time() - t0) * 1000, 1)
        return 200, result

    def vm_info(self, microvm_id: str):
        status, body, ms = self.call_vm(microvm_id, "GET", "/info", timeout=90)
        return status, {"info": body, "ms": ms}

    def vm_status(self, microvm_id: str):
        t0 = time.time()
        snap = self.client(microvm_id).status()
        self.trace.add("microvm-endpoint", "GET /status", 200, (time.time() - t0) * 1000)
        return 200, snap

    def task(self, _q, body):
        microvm_id = body.get("microvm_id") or self.pick()
        payload = {k: body[k] for k in ("steps", "prompt", "files") if body.get(k)}
        if not payload.get("steps") and not payload.get("prompt"):
            return 400, {"error": "add at least one step, or a prompt"}
        status, result, ms = self.call_vm(microvm_id, "POST", "/task", payload, timeout=180)
        return status, {"microvm_id": microvm_id, "request_ms": ms, "result": result}

    def dispatch(self, _q, body):
        n = max(1, min(int(body.get("count", 16)), MAX_TASKS))
        per_vm = max(1, min(int(body.get("per_vm", 4)), 16))
        steps = body.get("steps") or [p["step"] for p in PRESETS[:5]]
        bodies = [{"steps": steps + [f"run:echo task {i}"]} for i in range(n)]
        t0 = time.time()
        results = self.fleet.dispatch("/task", bodies, per_vm=per_vm, timeout=180,
                                      client_factory=lambda vid: self.client(vid))
        wall = (time.time() - t0) * 1000
        self.trace.add("microvm-ctl", f"Fleet.dispatch x{n}", 200, wall)
        rows, by_vm, by_rule = [], {}, {}
        decisions = 0
        for r in results:
            b = r.get("body") if isinstance(r.get("body"), dict) else {}
            denied = b.get("denied", [])
            decisions += len(b.get("decisions", []))
            for d in denied:
                by_rule[d["rule"]] = by_rule.get(d["rule"], 0) + 1
            vm = by_vm.setdefault(r["microvm_id"], {"microvm_id": r["microvm_id"], "tasks": 0, "ms": []})
            vm["tasks"] += 1
            vm["ms"].append(r["ms"])
            rows.append({"index": r["index"], "microvm_id": r["microvm_id"], "status": r["status"], "ms": r["ms"],
                         "box_ms": b.get("box_ms"), "exit_code": b.get("exit_code"),
                         "denied_rules": sorted({d["rule"] for d in denied}), "error": r.get("error")})
        per_vm_rows = [{"microvm_id": v["microvm_id"], "tasks": v["tasks"],
                        "p50_ms": sorted(v["ms"])[len(v["ms"]) // 2]} for v in by_vm.values()]
        ok = sum(1 for r in rows if r["status"] == 200 and r["exit_code"] == 0)
        return 200, {"count": n, "per_vm": per_vm, "wall_ms": round(wall, 1),
                     "tasks_per_s": round(n / (wall / 1000), 1), "ok": ok, "decisions": decisions,
                     "denials_by_rule": by_rule, "per_vm_rows": sorted(per_vm_rows, key=lambda r: r["microvm_id"]),
                     "rows": rows}

    def plan(self, q, _b):
        shards = max(1, int(q.get("shards", ["4"])[0]))
        policy = LeasePolicy(budget_s=600, heartbeat_timeout_s=120, slack_s=120)
        p = self.fm.plan(shards, BASELINE_MIB, policy)
        verdict = "ok"
        try:
            p.check()
        except LeasePlanRejected as e:
            verdict = f"rejected: {e}"
        if verdict == "ok" and shards > p.concurrency:
            verdict = f"{shards} leases exceed the concurrency limit {p.concurrency}; launch in waves"
        return 200, {"summary": p.summary(), "plan": p.to_dict(), "verdict": verdict}

    def lease(self, _q, body):
        shards = max(1, min(int(body.get("shards", 2)), MAX_VMS))
        boxes = max(1, min(int(body.get("boxes", 4)), 32))
        steps = body.get("steps") or [p["step"] for p in PRESETS[:5]]
        room = MAX_VMS - self.active_count()
        if shards > room:
            return 409, {"error": f"{shards} leases need {shards} microVMs and the playground has room for {room}"}
        policy = LeasePolicy(budget_s=600, heartbeat_timeout_s=120, slack_s=120)
        run_id = f"pg-{time.strftime('%H%M%S')}-{random.randint(100, 999)}"
        leases = [Lease(kind="none", token="", region=self.cfg.region, id=f"{run_id}-{i}", heartbeat_s=10)
                  for i in range(shards)]
        tasks = [{"tasks": [{"steps": steps + [f"run:echo shard {i} box {j}"]} for j in range(boxes)],
                  "parallel": min(boxes, 8)} for i in range(shards)]
        try:
            t0 = time.time()
            vms = self.fm.lease_many(IMAGE, leases, tasks, policy, baseline_mib=BASELINE_MIB)
        except LeasePlanRejected as e:
            return 409, {"error": str(e)}
        return 200, {"run_id": run_id, "launched": [v.microvm_id for v in vms],
                     "ms": round((time.time() - t0) * 1000, 1),
                     "plan": self.fm.plan(shards, BASELINE_MIB, policy).summary()}

    def jobs(self, q, _b):
        ids = set((q.get("ids", [""])[0]).split(",")) - {""}
        rows = self.mon.job_status(IMAGE)
        if ids:
            rows = [r for r in rows if r.get("microvm_id") in ids]
        vms = {v["microvmId"]: v["state"] for v in self.vms()}
        for r in rows:
            r["state"] = vms.get(r.get("microvm_id"))
        done = sum(1 for r in rows if r.get("done"))
        return 200, {"rows": rows, "done": done, "total": len(ids) or len(rows)}

    def policy(self, _q, _b):
        def read(name):
            try:
                with open(os.path.join(BOX_FILES, name)) as f:
                    return f.read()
            except OSError:
                return ""
        return 200, {"policy": read("policy.dw"), "box_toml": read("box.toml.tmpl"), "presets": PRESETS}

    def benchmarks(self, _q, _b):
        latest = {}
        for path in sorted(glob.glob(os.path.join(RESULTS, "*.json"))):
            name = os.path.basename(path).rsplit("-", 2)[0]
            try:
                with open(path) as f:
                    latest[name] = json.load(f)
            except (OSError, ValueError):
                continue
        for data in latest.values():  # keep the payload small
            data.pop("job_status", None)
            data.pop("jobs", None)
            data.pop("first_task_decisions", None)
        return 200, latest

    def trace_items(self, _q, _b):
        return 200, {"items": list(self.trace.items)}

    # ── dispatch ───────────────────────────────────────────────────────────────────────────────
    def api(self, method: str, path: str, query: dict, body: dict):
        parts = [p for p in path.split("/") if p][1:]  # drop "api"
        routes = {
            ("GET", "overview"): self.overview, ("GET", "vms"): self.list_vms, ("POST", "vms"): self.launch,
            ("POST", "fleet"): self.fleet_action, ("POST", "task"): self.task, ("POST", "dispatch"): self.dispatch,
            ("GET", "plan"): self.plan, ("POST", "lease"): self.lease, ("GET", "jobs"): self.jobs,
            ("GET", "policy"): self.policy, ("GET", "benchmarks"): self.benchmarks, ("GET", "trace"): self.trace_items,
        }
        try:
            if len(parts) == 1 and (method, parts[0]) in routes:
                return routes[(method, parts[0])](query, body or {})
            if len(parts) == 3 and parts[0] == "vms":
                vid, verb = parts[1], parts[2]
                if method == "POST" and verb in ("suspend", "resume", "terminate"):
                    return self.lifecycle(vid, verb)
                if method == "GET" and verb == "info":
                    return self.vm_info(vid)
                if method == "GET" and verb == "status":
                    return self.vm_status(vid)
            return 404, {"error": f"no route {method} {path}"}
        except ValueError as e:
            return 400, {"error": str(e)}
        except Exception as e:  # surface AWS and endpoint errors to the page as they are
            return 502, {"error": f"{type(e).__name__}: {e}"}


_PLAYGROUND: Playground | None = None


def playground() -> Playground:
    global _PLAYGROUND
    if _PLAYGROUND is None:
        _PLAYGROUND = Playground()
    return _PLAYGROUND


def authorized(headers: dict) -> bool:
    key = os.environ.get("PLAYGROUND_KEY")
    if not key:
        return True
    given = headers.get("x-playground-key") or ""
    return hmac.compare_digest(given.encode(), key.encode())


# ── local server ───────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            path = os.path.normpath(os.path.join(STATIC, name))
            if not path.startswith(STATIC) or not os.path.isfile(path):
                return self._send(404, {"error": "not found"})
            ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript", "css": "text/css",
                     "png": "image/png", "svg": "image/svg+xml"}.get(path.rsplit(".", 1)[-1], "application/octet-stream")
            with open(path, "rb") as f:
                return self._send(200, f.read(), ctype)
        headers = {k.lower(): v for k, v in self.headers.items()}
        if not authorized(headers):
            return self._send(401, {"error": "missing or wrong playground key"})
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            body = json.loads(raw or b"{}")
        status, payload = playground().api(method, u.path, parse_qs(u.query), body)
        self._send(status, payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


# ── Lambda Function URL ────────────────────────────────────────────────────────────────────────

def lambda_handler(event, _context):
    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method", "GET"), event.get("rawPath", "/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    origin_secret = os.environ.get("ORIGIN_SECRET")
    if origin_secret and not hmac.compare_digest((headers.get("x-origin-verify") or "").encode(), origin_secret.encode()):
        return {"statusCode": 403, "headers": {"content-type": "application/json"},
                "body": json.dumps({"error": "call the playground through its CloudFront URL"})}
    if not authorized(headers):
        return {"statusCode": 401, "headers": {"content-type": "application/json"},
                "body": json.dumps({"error": "missing or wrong playground key"})}
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    body = json.loads(raw) if raw else {}
    status, payload = playground().api(method, path, parse_qs(event.get("rawQueryString", "")), body)
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(payload, default=str)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Strands Box playground on http://{a.host}:{a.port} ({IMAGE} in {PlaneConfig().region})", file=sys.stderr)
    srv.serve_forever()
