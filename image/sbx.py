"""Run one Strands Box task on this machine and return what happened.

A task gets its own directory under /work: the project the agent works on, the box's private state,
and the agent's home and temporary files. The box config is rendered into the task directory, so
tasks on one VM never share a box. The result carries the agent's output, the box's startup report,
and every policy decision the box recorded for the task.
"""

import json
import os
import shutil
import subprocess
import time
import uuid

ROOT = os.environ.get("SBX_ROOT", "/opt/sbx")
WORK = os.environ.get("SBX_WORK", "/work")
BOX = os.path.join(ROOT, "box-core", "box")
PYTHON = os.environ.get("SBX_PYTHON", "/usr/bin/python3.12")
VENV_PY = os.path.join(ROOT, "venv", "bin", "python3")
SEED = os.path.join(ROOT, "seed")
TEMPLATE = os.path.join(ROOT, "box", "box.toml.tmpl")
POLICY = os.path.join(ROOT, "box", "policy.dw")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-west-2")
MODEL_ID = os.environ.get("MODEL_ID", "global.amazon.nova-2-lite-v1:0")
TASK_TIMEOUT_S = int(os.environ.get("SBX_TASK_TIMEOUT_S", "300"))


def box_version() -> str:
    return subprocess.run([BOX, "--version"], capture_output=True, text=True).stdout.strip()


def _render(task_dir: str, name: str, mode: str) -> str:
    script = "agent.py" if mode == "bedrock" else "scripted.py"
    command = json.dumps([VENV_PY if mode == "bedrock" else PYTHON, os.path.join(ROOT, "agent", script)])
    with open(TEMPLATE) as f:
        text = f.read().format(name=name, task_dir=task_dir, policy=POLICY, command=command,
                               bedrock_region=BEDROCK_REGION, model_id=MODEL_ID)
    path = os.path.join(task_dir, "box.toml")
    with open(path, "w") as f:
        f.write(text)
    return path


def _decisions(task_dir: str) -> list:
    """The policy decisions from the box's OTLP JSON records, oldest first."""
    path = os.path.join(task_dir, "state", "private", "telemetry", "records.jsonl")
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            for rl in rec.get("resourceLogs", []):
                for sl in rl.get("scopeLogs", []):
                    if sl.get("scope", {}).get("name") != "strands-box.policy":
                        continue
                    for lr in sl.get("logRecords", []):
                        attrs = {a["key"].removeprefix("strands.box.policy."): next(iter(a["value"].values()), None)
                                 for a in lr.get("attributes", []) if a["key"].startswith("strands.box.policy.")}
                        out.append({"verdict": attrs.get("verdict"), "action": attrs.get("action"),
                                    "resource": attrs.get("resource"), "rule": attrs.get("rule")})
    return out


def run_task(steps=None, prompt=None, files=None, mode=None, task_id=None, keep=False, env=None) -> dict:
    """Run one task in a fresh box. `steps` drives the scripted agent; `prompt` drives the model."""
    mode = mode or ("bedrock" if prompt else "scripted")
    task_id = task_id or uuid.uuid4().hex[:12]
    task_dir = os.path.join(WORK, task_id)
    project = os.path.join(task_dir, "project")
    started = time.time()
    shutil.rmtree(task_dir, ignore_errors=True)
    shutil.copytree(SEED, project)
    for sub in ("tmp", "home", os.path.join("project", "out")):
        os.makedirs(os.path.join(task_dir, sub), exist_ok=True)
    for rel, content in (files or {}).items():
        dest = os.path.normpath(os.path.join(project, rel))
        if not dest.startswith(project + os.sep):
            raise ValueError(f"file path {rel!r} leaves the project")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as f:
            f.write(content)
    config = _render(task_dir, f"sbx-{task_id}", mode)
    args = [prompt] if mode == "bedrock" else list(steps or ["list"])
    proc_env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/root")}
    proc_env.update(env or {})
    if "AWS_BEARER_TOKEN_BEDROCK" not in proc_env:
        proc_env["AWS_BEARER_TOKEN_BEDROCK"] = "unset"  # the route needs a value; scripted runs never call Bedrock
    t0 = time.time()
    try:
        proc = subprocess.run([BOX, "run", "--config", config, "--", *args], capture_output=True, text=True,
                              env=proc_env, timeout=TASK_TIMEOUT_S)
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        code, out, err = 124, e.stdout or "", (e.stderr or "") + f"\nsbx: timed out after {TASK_TIMEOUT_S} s"
    box_ms = round((time.time() - t0) * 1000, 1)
    decisions = _decisions(task_dir)
    agent = None
    if mode == "scripted":
        for line in reversed(out.splitlines()):
            if line.startswith("{"):
                try:
                    agent = json.loads(line)
                except ValueError:
                    pass
                break
    result = {
        "task_id": task_id, "mode": mode, "exit_code": code, "box_ms": box_ms,
        "total_ms": round((time.time() - started) * 1000, 1),
        "stdout": out[-8000:], "stderr": err[-8000:], "agent": agent,
        "decisions": decisions,
        "denied": [d for d in decisions if d["verdict"] == "deny"],
        "project_after": sorted(os.listdir(project)),
    }
    if not keep:
        shutil.rmtree(task_dir, ignore_errors=True)
    return result


if __name__ == "__main__":
    import sys
    print(json.dumps(run_task(steps=sys.argv[1:] or ["list", "read:README.md", "read:.env", "run:rm scratch.txt"],
                              keep=True), indent=2))
