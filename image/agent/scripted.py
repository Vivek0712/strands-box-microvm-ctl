"""A scripted agent: the same three tools as agent.py, driven by a fixed plan instead of a model.

Each argument after `--` is one tool call, spelled `list`, `read:<path>`, or `run:<command>`. Every
call goes through the box's shell exactly as the model-driven agent's do, so the policy decides each
command and each file it touches. Use it to exercise a policy, or a fleet, without model access.
"""

import json
import subprocess
import sys
import time


def shell(command: str) -> dict:
    started = time.time()
    done = subprocess.run(["zsh", "-c", command], capture_output=True, text=True)
    print(f"[tool] {command} (exit {done.returncode})", file=sys.stderr)
    if done.stderr:
        print(done.stderr.rstrip(), file=sys.stderr)
    return {"command": command, "exit": done.returncode, "stdout": done.stdout, "stderr": done.stderr,
            "ms": round((time.time() - started) * 1000, 1)}


def call(step: str) -> dict:
    kind, _, arg = step.partition(":")
    if kind == "list":
        return shell("ls -a")
    if kind == "read":
        return shell(f"cat '{arg}'")
    if kind == "run":
        return shell(arg)
    return {"command": step, "exit": 2, "stdout": "", "stderr": f"unknown step {step!r}", "ms": 0}


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    steps = sys.argv[1:]
    if not steps:
        print("usage: box run --config box.toml -- list read:README.md 'run:wc -l *.py'", file=sys.stderr)
        return 2
    results = [call(s) for s in steps]
    print(json.dumps({"agent": "scripted", "steps": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
