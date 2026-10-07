# strands-box-microvm-ctl

[Strands Box](https://github.com/strands-agents/box) inside [AWS Lambda MicroVMs](https://aws.amazon.com/lambda/), run as a fleet with [microvm-ctl](https://github.com/Vivek0712/microvm-ctl).

Lambda MicroVMs give every agent its own Firecracker VM and kernel. Strands Box decides, one operation at a time, what the agent inside that VM may do: every shell command, every file a command touches, and every outbound request goes through Box's trusted process, where a [Dogwood](https://dogwood-policy.github.io/dogwood/) policy permits or forbids it. This repo puts the two together. It has an image with Box 0.1.0 built for aarch64 Linux, a hook runtime that runs each task in a fresh box, fleet scenarios measured on the live service, and a playground web app.

![A kernel for the fleet, a policy for every command](https://raw.githubusercontent.com/Vivek0712/awesome-microvm/main/blog/img/hook-04-strands-box.png)

Part 5 of the series Building on AWS Lambda MicroVMs walks through all of it. The other examples live in [awesome-microvm](https://github.com/Vivek0712/awesome-microvm).

## What is measured

Live in us-east-1 on 2026-10-08, on an account with a RunMicrovm quota of 1 per second and 8 GB of microVM memory. The image is 1 GB with 2 vCPUs, and the task is the scripted agent's five calls: list, read README.md, read .env (denied), delete scratch.txt (denied), count Python lines.

| | |
|---|---|
| Launch to RUNNING | 4.04 s, first byte at 5.91 s |
| One box inside the VM, warm | 83 ms p50 (create, run, record 26 decisions, tear down) |
| One task, laptop to VM and back, warm | 351 ms p50, 378 ms max over 20 |
| Task on a SUSPENDED VM (auto-resume) | 924 ms |
| Boxes per second in one VM | 22.7 at 4 or more boxes at once, flat to 32 at once, 32 of 32 correct |
| 8 VMs, 8 boxes each | scale_to(8) in 15.0 s; 64 of 64 correct per round, 1,664 decisions and 192 denials a round, 45 to 51 boxes/s |
| Fleet.dispatch, 96 tasks over 4 VMs | 7.1 tasks/s at 1 in flight per VM, 14.9 at 8; 96 of 96 correct at each |
| 6 leases of 4 boxes | all RUNNING at 11.1 s, all done at 15.8 s |

The raw JSON is in [results/](results/), and the playground draws it on its Benchmarks tab.

## Layout

```text
image/                 the microVM image: Dockerfile, hook runtime (app.py), task runner (sbx.py)
  agent/agent.py       the Strands Agents SDK agent from Box's examples, unchanged
  agent/scripted.py    the same three tools driven by a fixed plan, for runs without model access
  box/box.toml.tmpl    one box per task, rendered under /work/<task>
  box/policy.dw        the upstream example policy, plus out/ writes, for every task on the VM
  seed/                the project every box starts from
build/build-box.sh     builds Box for aarch64 Linux in amazonlinux:2023 (no Linux release exists yet)
fleet/scenarios.py     single, density, fleet, dispatch, lease, plan-reject, lifecycle, failure
playground/            the web app: server.py (JSON API over microvm-ctl) and static/index.html
infra/                 CloudFront + S3 + Lambda Function URL template and deploy script for the playground
FINDINGS.md            what broke, what I fixed, and what to report upstream
```

## Run it

You need Docker on an arm64 host (Apple silicon or Graviton), Python 3.9 or newer, and an account bootstrapped with `mvm bootstrap` from microvm-ctl 0.4.0 or newer.

```sh
pip install "microvm-ctl>=0.4"
export MVM_PROFILE=... MVM_REGION=us-east-1 MVM_ARTIFACT_BUCKET=... MVM_BUILD_ROLE_ARN=... MVM_EXECUTION_ROLE_ARN=...

./build/build-box.sh                                   # image/box-core/{box, sock-alias, trampoline}
mvm image build strands-box image --memory 1024 --caps-all
mvm run strands-box --wait
mvm call <microvm-id> /task -X POST -d '{"steps": ["list", "read:.env", "run:rm scratch.txt"]}'
mvm dispatch strands-box /task -d '{"steps": ["read:README.md", "read:.env"]}' -n 64 --per-vm 4

python fleet/scenarios.py all                          # every scenario, results/ gets one JSON each
python playground/server.py                            # http://127.0.0.1:8770
```

`--caps-all` matters. Box builds its sandbox from user, mount, PID and network namespaces and mounts a fresh `/proc` for the agent; without `additionalOsCapabilities: ["ALL"]` on the image, the VM refuses that mount and every box fails to start (FINDINGS.md, entry 3). The build's `/validate` hook runs a box and fails the build if the policy does not deny what it should.

## The model-driven agent

`POST /task {"prompt": "..."}` runs the Strands Agents SDK agent. The VM mints a short-lived Bedrock API key from its own execution role for each task with `aws-bedrock-token-generator`. The key goes to `box run` as `AWS_BEARER_TOKEN_BEDROCK`, the box binds it with `secret.ref = "env://AWS_BEARER_TOKEN_BEDROCK"`, the agent sees a stand-in value, and the egress gateway adds the real key to each request the policy permits. For that, the execution role needs `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` and `bedrock:CallWithBearerToken`. Without them, scripted tasks run and model tasks fail with a typed error. The default model is `global.amazon.nova-2-lite-v1:0` in us-west-2, and `MODEL_ID` and `BEDROCK_REGION` in the image environment change it.

## The playground

![The playground running a task](https://raw.githubusercontent.com/Vivek0712/awesome-microvm/main/blog/img/playground-box-run.png)

Seven tabs over one API:

- **Run a task:** compose steps from presets (each labelled permit or deny) or type a command, then read every tool call, every policy decision in order, and the box's startup report.
- **Fleet:** launch, scale, suspend, resume, terminate, drain, and look inside a VM.
- **Fan-out:** `Fleet.dispatch` with a chart of tasks per VM and denials by rule.
- **Leases:** the plan sentence as you type, then `lease_many` and a live job table.
- **Policy:** the two files that define every box.
- **Benchmarks:** the results above, with tooltips and tables.
- **Activity:** every AWS call and endpoint request.

Guardrails apply whatever the page asks for: at most 8 VMs, an idle policy on every launch, and a 30 minute lifetime cap.

`playground/e2e.py` drives the page in Chromium against the live service and asserts on what it shows: two VMs launched, a task with both denials, writes inside and outside `out/`, a 48-task fan-out, a 12-lease plan that needs waves, 2 leases to done, every tab, phone width without horizontal scroll, and a drain at the end. The last run passed with no page errors.

To host it behind CloudFront: `source .env.mvm && ./infra/deploy.sh`. The template keeps the page in a private S3 bucket behind origin access control, and sends `/api/*` to a Lambda Function URL that refuses any request without the secret header CloudFront adds. The API also asks for a playground key.

## License

MIT. Strands Box is Apache-2.0 and is built from source by `build/build-box.sh`; no Box binary is committed here. `image/agent/agent.py` is copied unchanged from Box's `examples/strands-box/strands-sdk-agent`, and `image/box/policy.dw` is adapted from the policy in the same example, both under Apache-2.0 (Copyright Amazon.com, Inc. or its affiliates).
