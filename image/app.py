"""Strands Box on a Lambda MicroVM.

Every task runs in a fresh box on this VM: its own project copy, its own box directory, and its own
decision log. Three ways in:

    POST /task      {"steps": ["list", "read:README.md", "run:rm scratch.txt"]}      scripted agent
                    {"prompt": "Summarize README.md in one sentence."}                  model-driven agent
                    optional: "files": {"path": "content"}, "keep": false
    POST /batch     {"tasks": [<task>, ...], "parallel": 4}                             several boxes at once
    a lease         runHookPayload {"lease": {...}, "task": <task> | {"tasks": [...]}}  microvm-ctl leases

GET /info reports the box version, the kernel, and whether the namespace launcher can run here.

For the model-driven agent the VM mints a short-lived Bedrock API key from its own execution role
for each task. The key lives in the box's trusted process; the agent gets a stand-in value and the
egress gateway adds the real key to each Bedrock request. The execution role therefore needs
bedrock:InvokeModel* and bedrock:CallWithBearerToken; without them only scripted tasks run.
"""

import os
import platform
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from microvm_hooks import HookApp, LeaseError

import sbx

app = HookApp()
STATE = {"tasks": 0, "denied": 0, "booted_at": time.time(), "probe": None}
SMOKE = ["list", "read:README.md", "read:.env", "run:rm scratch.txt"]


def _probe() -> dict:
    """One scripted task that must permit a read and deny .env and the delete."""
    r = sbx.run_task(steps=SMOKE, task_id="probe")
    rules = sorted({d["rule"] for d in r["denied"]})
    ok = r["exit_code"] == 0 and rules == ["no_deletes", "no_env"] and "scratch.txt" in r["project_after"]
    return {"ok": ok, "exit_code": r["exit_code"], "box_ms": r["box_ms"], "denied_rules": rules,
            "stderr_tail": "" if ok else r["stderr"][-1500:]}


def _bedrock_key() -> str | None:
    try:
        from aws_bedrock_token_generator import provide_token
        return provide_token(region=sbx.BEDROCK_REGION)
    except Exception as e:  # no role, or the role cannot sign: scripted tasks still run
        app.job.log(f"no Bedrock key: {e}", level="warn")
        return None


def _one(task: dict) -> dict:
    env = {}
    if task.get("prompt"):
        key = _bedrock_key()
        if not key:
            return {"exit_code": 3, "error": "the model-driven agent needs a Bedrock key from the execution role"}
        env["AWS_BEARER_TOKEN_BEDROCK"] = key
    r = sbx.run_task(steps=task.get("steps"), prompt=task.get("prompt"), files=task.get("files"),
                     task_id=task.get("id"), keep=bool(task.get("keep")), env=env)
    STATE["tasks"] += 1
    STATE["denied"] += len(r["denied"])
    app.job.counter("tasks", STATE["tasks"])
    app.job.counter("denied", STATE["denied"])
    return r


def _many(tasks: list, parallel: int, on_done=None) -> list:
    results = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {pool.submit(_one, t): i for i, t in enumerate(tasks)}
        for done, fut in enumerate(as_completed(futures), 1):
            i = futures[fut]
            results[i] = fut.result()
            if on_done:
                on_done(done, len(tasks), results[i])
    return results


@app.on_ready
def ready(_ctx):
    # Warm the box binary, Python, and the SDK import into the snapshot.
    sbx.box_version()
    subprocess.run([sbx.VENV_PY, "-c", "import strands, boto3"], check=False)
    STATE["probe"] = _probe()
    return True


@app.on_validate
def validate(_ctx):
    # Exercise the whole path on the restored clone, so Lambda prefetches it on every launch.
    p = _probe()
    STATE["probe_at_validate"] = p
    if not p["ok"] and os.environ.get("SBX_REQUIRE_PROBE", "1") == "1":
        raise RuntimeError(f"box probe failed: {p}")


@app.on_run
def run(ctx):
    STATE["tasks"] = STATE["denied"] = 0
    STATE["run_at"] = time.time()


@app.route("GET", "/info")
def info(_body, _headers):
    return 200, {"box": sbx.box_version(), "kernel": platform.release(), "arch": platform.machine(),
                 "model_id": sbx.MODEL_ID, "bedrock_region": sbx.BEDROCK_REGION,
                 "tasks": STATE["tasks"], "denied": STATE["denied"], "probe_at_build": STATE["probe"],
                 "probe_at_validate": STATE.get("probe_at_validate"),
                 "cpus": os.cpu_count()}


@app.route("GET", "/diag")
def diag(_body, _headers):
    """What the namespace launcher depends on: identity, capabilities, the proc mounts, userns knobs."""
    def read(path):
        try:
            with open(path) as f:
                return f.read()
        except OSError as e:
            return f"<{e}>"
    status = read("/proc/self/status")
    caps = {line.split(":")[0]: line.split(":")[1].strip() for line in status.splitlines() if line.startswith("Cap")}
    mounts = [line for line in read("/proc/self/mountinfo").splitlines() if " /proc" in line or "proc " in line]
    knobs = {k: read(k).strip() for k in ("/proc/sys/user/max_user_namespaces",
                                          "/proc/sys/kernel/unprivileged_userns_clone",
                                          "/proc/sys/kernel/apparmor_restrict_unprivileged_userns")}
    unshare = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", "--pid", "--fork", "--mount-proc",
                              "true"], capture_output=True, text=True)
    return 200, {"uid": os.getuid(), "caps": caps, "proc_mounts": mounts, "knobs": knobs,
                 "unshare_mount_proc": {"exit": unshare.returncode, "stderr": unshare.stderr.strip()},
                 "seccomp": [line for line in status.splitlines() if line.startswith("Seccomp")]}


@app.route("POST", "/probe")
def probe(_body, _headers):
    return 200, _probe()


@app.route("POST", "/task")
def task(body, _headers):
    app.job.phase("task")
    r = _one(body)
    app.job.phase("idle")
    return 200, r


@app.route("POST", "/batch")
def batch(body, _headers):
    tasks = body.get("tasks") or []
    t0 = time.time()
    results = _many(tasks, int(body.get("parallel", os.cpu_count() or 2)))
    return 200, {"results": results, "wall_ms": round((time.time() - t0) * 1000, 1)}


@app.on_lease
def lease_work(task_spec, lease):
    tasks = task_spec.get("tasks") or [task_spec]
    lease.job.phase(f"boxes 0/{len(tasks)}")
    lease.job.log(f"running {len(tasks)} box task(s)")

    def done(n, total, r):
        lease.job.progress(n, total)
        lease.job.phase(f"boxes {n}/{total}")
        lease.job.log(f"box {r.get('task_id')} exit {r.get('exit_code')}, {len(r.get('denied', []))} denied",
                      box_ms=r.get("box_ms"))

    lease.check()
    results = _many(tasks, int(task_spec.get("parallel", 1)), on_done=done)
    failed = [r for r in results if r.get("exit_code") != 0]
    summary = [{"task_id": r.get("task_id"), "exit_code": r.get("exit_code"), "box_ms": r.get("box_ms"),
                "denied": r.get("denied", []), "decisions": len(r.get("decisions", []))} for r in results]
    if failed:
        raise LeaseError("BoxTaskFailed", f"{len(failed)}/{len(results)} box task(s) failed", retryable=False,
                         data={"results": summary})
    return {"results": summary}


if __name__ == "__main__":
    app.serve(port=8080)
