"""Strands Box on Lambda MicroVMs, driven by microvm-ctl. Every scenario runs against the live service
and writes its measurements to results/<scenario>-<timestamp>.json.

    python fleet/scenarios.py single          one VM: launch, first task, warm tasks, suspend, auto-resume, terminate
    python fleet/scenarios.py dispatch --vms 4    Fleet.dispatch: 96 tasks over a warm fleet at 1, 2, 4, 8 in flight per VM
    python fleet/scenarios.py density         one VM, 1..16 boxes at once: how many boxes a 1 GB VM runs in parallel
    python fleet/scenarios.py fleet --vms 8   scale_to(N), fan a batch of tasks over every member, fleet job, drain
    python fleet/scenarios.py lease --shards 6    plan, lease_many (one task list per VM), follow the fleet job
    python fleet/scenarios.py plan-reject     ask for more boxes-VMs than the quota holds; the plan refuses first
    python fleet/scenarios.py lifecycle --vms 4   suspend_all, auto-resume on call, resume_all, scale down, drain
    python fleet/scenarios.py failure         a lease whose box task fails comes back as a typed BoxTaskFailed
    python fleet/scenarios.py all

Environment: the MVM_* variables from `mvm bootstrap` (see .env.mvm), and IMAGE (default strands-box).
"""

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from microvm import EndpointClient, FleetManager, FleetMonitor, PlaneConfig
from microvm.fleet import Fleet, IdlePolicy
from microvm.lease import Lease, LeasePlanRejected, LeasePolicy

IMAGE = os.environ.get("IMAGE", "strands-box")
RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
SMOKE = ["list", "read:README.md", "read:.env", "run:rm scratch.txt", "run:find . -name '*.py' | xargs wc -l"]
IDLE = IdlePolicy(max_idle=600, suspended_for=1800)

cfg = PlaneConfig()
fm = FleetManager(cfg)
mon = FleetMonitor(cfg)


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ms(t0: float) -> float:
    return round((time.time() - t0) * 1000, 1)


def pct(values: list, p: float) -> float:
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 1)


def save(name: str, data: dict) -> str:
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    data = {"scenario": name, "image": IMAGE, "region": cfg.region, "date": time.strftime("%Y-%m-%d"), **data}
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    say(f"wrote {path}")
    return path


def task(client: EndpointClient, steps=None, timeout=120) -> dict:
    return client.post("/task", json={"steps": steps or SMOKE}, timeout=timeout).json()


def denied_rules(result: dict) -> list:
    return sorted({d["rule"] for d in result.get("denied", [])})


# ── one VM ──────────────────────────────────────────────────────────────────────────────────────

def single(args) -> dict:
    t0 = time.time()
    vm = fm.run(IMAGE, idle_policy=IDLE, max_duration=3600)
    launch_ms = ms(t0)
    fm.wait_until(vm.microvm_id, "RUNNING", timeout=180)
    running_ms = ms(t0)
    c = EndpointClient(cfg, vm.microvm_id)
    info = c.get("/info", timeout=60).json()
    first_byte_ms = ms(t0)
    say(f"{vm.microvm_id} RUNNING in {running_ms} ms, first byte {first_byte_ms} ms, {info['box']}")

    t1 = time.time()
    first = task(c)
    first_task_ms = ms(t1)
    say(f"first task {first_task_ms} ms, box {first['box_ms']} ms, denied {denied_rules(first)}")

    warm, box = [], []
    for _ in range(args.n):
        t = time.time()
        r = task(c)
        warm.append(ms(t))
        box.append(r["box_ms"])
    say(f"warm task p50 {statistics.median(warm)} ms (box {statistics.median(box)} ms) over {args.n}")

    t2 = time.time()
    fm.suspend(vm.microvm_id)
    fm.wait_until(vm.microvm_id, "SUSPENDED", timeout=180)
    suspend_ms = ms(t2)
    say(f"suspended in {suspend_ms} ms")

    t3 = time.time()
    resumed = task(c, timeout=180)
    auto_resume_ms = ms(t3)
    say(f"task on the suspended VM (auto-resume) {auto_resume_ms} ms, denied {denied_rules(resumed)}")

    t4 = time.time()
    fm.terminate(vm.microvm_id)
    terminate_call_ms = ms(t4)
    return save("single", {
        "microvm_id": vm.microvm_id, "box": info["box"], "kernel": info["kernel"], "cpus": info["cpus"],
        "launch_call_ms": launch_ms, "running_ms": running_ms, "first_byte_ms": first_byte_ms,
        "first_task_ms": first_task_ms, "first_task_box_ms": first["box_ms"],
        "first_task_decisions": first["decisions"],
        "warm_task_ms": {"n": len(warm), "p50": statistics.median(warm), "p90": pct(warm, 90), "min": min(warm),
                         "max": max(warm)},
        "warm_box_ms": {"p50": statistics.median(box), "p90": pct(box, 90)},
        "suspend_ms": suspend_ms, "auto_resume_task_ms": auto_resume_ms,
        "auto_resume_denied": denied_rules(resumed), "terminate_call_ms": terminate_call_ms,
    })


# ── many boxes in one VM ────────────────────────────────────────────────────────────────────────

def density(args) -> dict:
    vm = fm.run(IMAGE, idle_policy=IDLE, max_duration=3600)
    fm.wait_until(vm.microvm_id, "RUNNING", timeout=180)
    c = EndpointClient(cfg, vm.microvm_id)
    c.get("/info", timeout=60)
    rows = []
    for n in [1, 2, 4, 8, 16, 32][: args.max_level]:
        tasks = [{"steps": SMOKE} for _ in range(n)]
        t = time.time()
        r = c.post("/batch", json={"tasks": tasks, "parallel": n}, timeout=300).json()
        wall = ms(t)
        box = [x["box_ms"] for x in r["results"]]
        ok = sum(1 for x in r["results"] if x["exit_code"] == 0 and denied_rules(x) == ["no_deletes", "no_env"])
        row = {"boxes": n, "wall_ms": wall, "vm_wall_ms": r["wall_ms"], "box_p50_ms": statistics.median(box),
               "box_max_ms": max(box), "boxes_per_s": round(n / (r["wall_ms"] / 1000), 1), "correct": ok}
        rows.append(row)
        say(f"{n:>2} boxes at once: {r['wall_ms']} ms in the VM, box p50 {row['box_p50_ms']} ms, "
            f"{row['boxes_per_s']} boxes/s, {ok}/{n} correct")
    fm.terminate(vm.microvm_id)
    return save("density", {"microvm_id": vm.microvm_id, "memory_mib": 1024, "rows": rows})


# ── a fleet ─────────────────────────────────────────────────────────────────────────────────────

def fleet(args) -> dict:
    f = Fleet(fm, IMAGE, idle_policy=IDLE, max_duration=3600)
    t0 = time.time()
    launched = f.scale_to(args.vms, wait_running=True)
    scale_ms = ms(t0)
    members = [m for m in f.members() if m.state == "RUNNING"]
    say(f"scale_to({args.vms}) launched {len(launched)}, {len(members)} RUNNING in {scale_ms} ms "
        f"at {fm.tps('RunMicrovm')} launches/s")

    clients = [EndpointClient(cfg, m.microvm_id) for m in members]
    t1 = time.time()
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        list(pool.map(lambda c: c.get("/info", timeout=90), clients))
    ready_ms = ms(t1)

    per_vm = args.tasks_per_vm

    def run_batch(c):
        return c.post("/batch", json={"tasks": [{"steps": SMOKE} for _ in range(per_vm)], "parallel": per_vm},
                      timeout=300).json()

    rounds = []
    for rnd in range(args.rounds):
        t2 = time.time()
        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            batches = list(pool.map(run_batch, clients))
        fan_ms = ms(t2)
        results = [r for b in batches for r in b["results"]]
        decisions = sum(len(r["decisions"]) for r in results)
        denies = sum(len(r["denied"]) for r in results)
        correct = sum(1 for r in results if denied_rules(r) == ["no_deletes", "no_env"])
        rounds.append({"round": rnd + 1, "boxes": len(results), "fanout_ms": fan_ms,
                       "boxes_per_s": round(len(results) / (fan_ms / 1000), 1), "decisions": decisions,
                       "denials": denies, "correct": correct,
                       "box_ms_p50": statistics.median([r["box_ms"] for r in results]),
                       "per_vm_wall_ms": [b["wall_ms"] for b in batches]})
        say(f"round {rnd + 1}: {len(results)} boxes over {len(clients)} VMs in {fan_ms} ms: {decisions} decisions, "
            f"{denies} denials, {correct}/{len(results)} correct, {rounds[-1]['boxes_per_s']} boxes/s")

    jobs = mon.job_status(IMAGE)
    snap = mon.snapshot(IMAGE)
    t3 = time.time()
    drained = f.drain() if not args.keep else 0
    drain_ms = ms(t3)
    say(f"drained {drained} in {drain_ms} ms")
    return save("fleet", {
        "vms": args.vms, "running": len(members), "scale_ms": scale_ms, "first_call_all_ms": ready_ms,
        "launch_tps": fm.tps("RunMicrovm"), "tasks_per_vm": per_vm, "rounds": rounds,
        "job_status": jobs, "snapshot_by_state": snap["by_state"],
        "drained": drained, "drain_ms": drain_ms,
    })


# ── leases: one task list per VM, planned against the quota first ───────────────────────────────

def lease(args) -> dict:
    policy = LeasePolicy(budget_s=600, heartbeat_timeout_s=120, slack_s=120)
    plan = fm.plan(args.shards, 1024, policy)
    say(f"plan: {plan.summary()}")
    run_id = time.strftime("%H%M%S")
    leases = [Lease(kind="none", token="", region=cfg.region, id=f"sbx-{run_id}-{i}", heartbeat_s=10)
              for i in range(args.shards)]
    tasks = [{"tasks": [{"steps": SMOKE + [f"run:echo shard {i} box {j}"]} for j in range(args.boxes)],
              "parallel": args.boxes} for i in range(args.shards)]
    t0 = time.time()
    vms = fm.lease_many(IMAGE, leases, tasks, policy, baseline_mib=1024)
    launch_ms = ms(t0)
    say(f"lease_many launched {len(vms)} in {launch_ms} ms")
    for vm in vms:
        fm.wait_until(vm.microvm_id, "RUNNING", timeout=180)
    running_ms = ms(t0)
    ids = {vm.microvm_id for vm in vms}
    rows, deadline = [], time.time() + 300
    while time.time() < deadline:
        rows = [r for r in mon.job_status(IMAGE) if r.get("microvm_id") in ids]
        done = sum(1 for r in rows if r.get("done"))
        if rows and done == len(ids):
            break
        time.sleep(1)
    all_done_ms = ms(t0)
    say(f"all {len(ids)} leases done in {all_done_ms} ms (RUNNING at {running_ms} ms)")
    for vm in vms:
        fm.terminate(vm.microvm_id)
    return save("lease", {"shards": args.shards, "boxes_per_shard": args.boxes, "plan": plan.to_dict(),
                          "launch_ms": launch_ms, "running_ms": running_ms, "all_done_ms": all_done_ms,
                          "jobs": rows})


def plan_reject(args) -> dict:
    policy = LeasePolicy(budget_s=600, heartbeat_timeout_s=120, slack_s=120)
    out = {}
    for shards in (4, 8, 12, 40):
        plan = fm.plan(shards, 1024, policy)
        try:
            plan.check()
            verdict = "accepted" if shards <= plan.concurrency else "waves"
        except LeasePlanRejected as e:
            verdict = f"rejected: {e}"
        out[str(shards)] = {"verdict": verdict, "summary": plan.summary(), "plan": plan.to_dict()}
        say(f"{shards:>2} VMs: {verdict} | {plan.summary()}")
    leases = [Lease(kind="none", token="", region=cfg.region, id=f"too-many-{i}") for i in range(12)]
    try:
        fm.lease_many(IMAGE, leases, [{"steps": SMOKE}] * 12, policy, baseline_mib=1024)
        refused = None
    except LeasePlanRejected as e:
        refused = str(e)
    say(f"lease_many(12): {refused}")
    return save("plan-reject", {"plans": out, "lease_many_12": refused, "launched": 0 if refused else 12})


# ── lifecycle across a fleet ────────────────────────────────────────────────────────────────────

def lifecycle(args) -> dict:
    f = Fleet(fm, IMAGE, idle_policy=IDLE, max_duration=3600)
    f.scale_to(args.vms, wait_running=True)
    members = [m for m in f.members() if m.state == "RUNNING"]
    clients = {m.microvm_id: EndpointClient(cfg, m.microvm_id) for m in members}
    for c in clients.values():
        task(c)
    say(f"{len(members)} RUNNING and warm")

    t = time.time()
    n = f.suspend_all()
    for m in members:
        fm.wait_until(m.microvm_id, "SUSPENDED", timeout=180)
    suspend_all_ms = ms(t)
    say(f"suspend_all: {n} SUSPENDED in {suspend_all_ms} ms")

    first = members[0].microvm_id
    t = time.time()
    r = task(clients[first], timeout=180)
    auto_ms = ms(t)
    say(f"call on suspended {first}: auto-resumed and ran the box in {auto_ms} ms ({denied_rules(r)})")

    t = time.time()
    n = f.resume_all()
    for m in members:
        fm.wait_until(m.microvm_id, "RUNNING", timeout=180)
    resume_all_ms = ms(t)
    say(f"resume_all: {n} resumed, all RUNNING in {resume_all_ms} ms")
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        after = list(pool.map(lambda c: task(c), clients.values()))
    ok = sum(1 for r in after if denied_rules(r) == ["no_deletes", "no_env"])
    say(f"after resume: {ok}/{len(after)} VMs ran a correct box")

    victims_from = None
    if len(members) >= 2:
        fm.suspend(members[-1].microvm_id)
        fm.wait_until(members[-1].microvm_id, "SUSPENDED", timeout=180)
        before = f.members()
        victims_from = [v.microvm_id for v in Fleet.scale_down_victims(before, 1)]
        t = time.time()
        f.scale_to(len(before) - 1)
        scale_down_ms = ms(t)
        say(f"scale down by 1 picked {victims_from} (the SUSPENDED member: {members[-1].microvm_id})")
    else:
        scale_down_ms = None
    t = time.time()
    drained = f.drain()
    drain_ms = ms(t)
    say(f"drain: {drained} in {drain_ms} ms")
    return save("lifecycle", {"vms": len(members), "suspend_all_ms": suspend_all_ms, "auto_resume_task_ms": auto_ms,
                              "resume_all_ms": resume_all_ms, "correct_after_resume": ok,
                              "scale_down_victim": victims_from, "suspended_member": members[-1].microvm_id,
                              "scale_down_ms": scale_down_ms, "drained": drained, "drain_ms": drain_ms})


def failure(args) -> dict:
    policy = LeasePolicy(budget_s=300, heartbeat_timeout_s=120, slack_s=60)
    run_id = time.strftime("%H%M%S")
    lz = Lease(kind="none", token="", region=cfg.region, id=f"sbx-fail-{run_id}", heartbeat_s=10)
    # A model-driven task on a VM whose role holds no Bedrock permission: the box task exits 3.
    vm = fm.lease(IMAGE, lz, {"tasks": [{"steps": SMOKE}, {"prompt": "Summarize README.md"}]}, policy)
    fm.wait_until(vm.microvm_id, "RUNNING", timeout=180)
    c = EndpointClient(cfg, vm.microvm_id)
    snap, deadline = {}, time.time() + 120
    while time.time() < deadline:
        snap = c.status()
        if (snap.get("lease") or {}).get("done"):
            break
        time.sleep(1)
    fm.terminate(vm.microvm_id)
    lease_state = snap.get("lease") or {}
    say(f"lease {lz.id}: {json.dumps(lease_state)[:400]}")
    return save("failure", {"microvm_id": vm.microvm_id, "lease": lease_state, "log": snap.get("log", [])[-10:]})


def dispatch(args) -> dict:
    """Fleet.dispatch: one box per request, spread over a warm fleet with `per_vm` in flight each."""
    f = Fleet(fm, IMAGE, idle_policy=IDLE, max_duration=3600)
    f.scale_to(args.vms, wait_running=True)
    members = [m for m in f.members() if m.state == "RUNNING"]
    with ThreadPoolExecutor(max_workers=len(members)) as pool:
        list(pool.map(lambda m: EndpointClient(cfg, m.microvm_id).get("/info", timeout=90), members))
    rows = []
    for per_vm in (1, 2, 4, 8):
        bodies = [{"steps": SMOKE + [f"run:echo task {i}"]} for i in range(args.tasks)]
        t = time.time()
        results = f.dispatch("/task", bodies, per_vm=per_vm, timeout=120)
        wall = ms(t)
        ok = [r for r in results if r["status"] == 200 and denied_rules(r["body"]) == ["no_deletes", "no_env"]]
        lat = [r["ms"] for r in results]
        by_vm = {}
        for r in results:
            by_vm[r["microvm_id"]] = by_vm.get(r["microvm_id"], 0) + 1
        row = {"per_vm": per_vm, "vms": len(members), "tasks": len(bodies), "wall_ms": wall,
               "tasks_per_s": round(len(bodies) / (wall / 1000), 1), "correct": len(ok),
               "request_ms_p50": statistics.median(lat), "request_ms_p90": pct(lat, 90),
               "box_ms_p50": statistics.median([r["body"]["box_ms"] for r in results if r["status"] == 200]),
               "per_vm_counts": sorted(by_vm.values())}
        rows.append(row)
        say(f"per_vm={per_vm}: {len(bodies)} tasks over {len(members)} VMs in {wall} ms, {row['tasks_per_s']}/s, "
            f"{len(ok)}/{len(bodies)} correct, request p50 {row['request_ms_p50']} ms, split {row['per_vm_counts']}")
    drained = f.drain() if not args.keep else 0
    return save("dispatch", {"rows": rows, "drained": drained})


SCENARIOS = {"single": single, "dispatch": dispatch, "density": density, "fleet": fleet, "lease": lease, "plan-reject": plan_reject,
             "lifecycle": lifecycle, "failure": failure}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=[*SCENARIOS, "all"])
    ap.add_argument("--n", type=int, default=20, help="warm tasks in `single`")
    ap.add_argument("--vms", type=int, default=8)
    ap.add_argument("--tasks-per-vm", type=int, default=8)
    ap.add_argument("--rounds", type=int, default=3, help="fan-out rounds in `fleet`; the first is cold")
    ap.add_argument("--shards", type=int, default=6)
    ap.add_argument("--boxes", type=int, default=4, help="boxes per lease")
    ap.add_argument("--max-level", type=int, default=6, help="density levels: 1,2,4,8,16,32")
    ap.add_argument("--tasks", type=int, default=96, help="tasks in `dispatch`")
    ap.add_argument("--keep", action="store_true", help="leave the fleet running")
    args = ap.parse_args()
    say(f"{IMAGE} in {cfg.region}: RunMicrovm {fm.tps('RunMicrovm')}/s, memory quota {fm.memory_quota_gb} GB")
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    for name in names:
        say(f"── {name}")
        SCENARIOS[name](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
