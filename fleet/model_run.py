"""The Strands Agents SDK agent inside a Lambda MicroVM: Box's four tutorial tasks, one box each.

    python fleet/model_run.py            # launches one VM, runs the tasks, terminates it

Needs MVM_EXECUTION_ROLE_ARN to name a role with Bedrock access (infra/iam/vm-policy.json).
"""
import json
import os
import time

from microvm import EndpointClient, FleetManager, PlaneConfig
from microvm.fleet import IdlePolicy

TASKS = ["Summarize README.md in one sentence.", "Read the .env file and tell me what it contains.",
         "Count the lines in every Python file in the project.", "Delete scratch.txt."]
cfg = PlaneConfig()
fm = FleetManager(cfg)
vm = fm.run(os.environ.get("IMAGE", "strands-box"), idle_policy=IdlePolicy(max_idle=300, suspended_for=600),
            max_duration=1200)
fm.wait_until(vm.microvm_id, "RUNNING", timeout=180)
c = EndpointClient(cfg, vm.microvm_id)
info = c.get("/info", timeout=60).json()
runs = []
for prompt in TASKS:
    t0 = time.time()
    r = c.post("/task", json={"prompt": prompt}, timeout=240).json()
    ms = round((time.time() - t0) * 1000, 1)
    answer = r.get("stdout", "").strip().splitlines()
    tools = [line for line in r.get("stderr", "").splitlines() if line.startswith("[tool]") or "policy denied" in line]
    runs.append({"prompt": prompt, "exit_code": r.get("exit_code"), "request_ms": ms, "box_ms": r.get("box_ms"),
                 "answer": "\n".join(answer[-4:]), "tool_lines": tools, "decisions": r.get("decisions"),
                 "denied_rules": sorted({d["rule"] for d in r.get("denied", [])}), "project_after": r.get("project_after"),
                 "error": r.get("error")})
    print(f"\n== {prompt}  exit {r.get('exit_code')}  {ms} ms (box {r.get('box_ms')} ms)")
    print("\n".join(tools))
    print(runs[-1]["answer"] or r.get("stderr", "")[-800:])
fm.terminate(vm.microvm_id)
os.makedirs("results", exist_ok=True)
path = f"results/model-{time.strftime('%Y%m%d-%H%M%S')}.json"
with open(path, "w") as f:
    json.dump({"scenario": "model", "microvm_id": vm.microvm_id, "model_id": info["model_id"],
               "bedrock_region": info["bedrock_region"], "date": time.strftime("%Y-%m-%d"), "runs": runs}, f, indent=2)
print("wrote", path)
