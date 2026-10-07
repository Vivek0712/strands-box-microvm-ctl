# Findings: Strands Box on Linux and on AWS Lambda MicroVMs

What I hit while running Strands Box 0.1.0 locally on macOS, in an AL2023 arm64 container, and inside Lambda MicroVMs, and while driving it with microvm-ctl. Each entry says what happened, how to reproduce it, and what I did about it. The Strands Box entries are candidates for issues on github.com/strands-agents/box; none of them is a security problem.

Versions: Strands Box 0.1.0 (release tarball for macOS, and built from source at commit 2c874ea for aarch64-unknown-linux-gnu), Lambda MicroVMs base image `al2023-minimal` with kernel 6.1.166, microvm-ctl 0.3.1.

## Strands Box

### 1. A `list` grant cannot be expressed on Linux, so the SDK example's box.toml fails there

`examples/strands-box/strands-sdk-agent/box.toml` grants `list = ["~/box-tutorial/my-project"]`. On Linux the run is refused before the agent starts:

```text
strands-box: error: `[agent]` filesystem entry "/work/<task>/project" is refused: /work/<task>/project cannot be listed without its contents on Linux: the namespace backend has no lowering for `list`; grant `read` instead
```

The message is precise, but following it widens the grant: `read` on the project lets the agent's own process open `.env` with no policy decision, which is the exact thing the example's `no_env` rule exists to stop. The safe fix is to drop the grant, since the agent lists through the box's shell anyway. Suggested: say that in the error ("or remove the entry and list through Strands Shell"), and mark `list` as macOS-only in `config.md`.

### 2. On Linux a `read` grant cannot map code, so Python packages with native extensions fail to import

With the venv under `read` and only the interpreter under `exec`, the Strands Agents SDK fails at import inside the box:

```text
ImportError: /opt/sbx/venv/lib64/python3.12/site-packages/pydantic_core/_pydantic_core.cpython-312-aarch64-linux-gnu.so: failed to map segment from shared object
```

The startup report lists the venv as `read` and gives no hint that compiled extensions under it will not load. The same box.toml shape works on macOS. Fix: add the venv to `exec`. Suggested: a line in the startup disclosure when a `read` tree holds `.so` files, or a sentence in `config.md` next to the existing "a writable bind reads" note.

### 3. Box needs a fresh `/proc` mount, which a default container and a default Lambda MicroVM refuse, and the error does not say what to change

In a default Docker container:

```text
strands-box-contain-trampoline: platform Linux unsupported: backend 'namespace' reports it cannot enforce on this host: Linux namespace launcher unavailable: this identity may not create a user namespace
```

With seccomp relaxed, and in a Lambda MicroVM image built without extra capabilities:

```text
strands-box: namespace reaper could not build the view: backend 'namespace' failed to apply: mounting a fresh proc at '/tmp/.strands-box-view.<id>/proc': Operation not permitted (os error 1)
strands-box: error: containment setup failed during containment apply
```

Box refusing to run without its boundary is the right behaviour. The second message does not tell an operator which host setting to change. What fixed it: in Docker, `--security-opt seccomp=unconfined --security-opt systempaths=unconfined`; on Lambda MicroVMs, build the image with `additionalOsCapabilities: ["ALL"]` (`mvm image build --caps-all`, or `os_capabilities_all=True` in the SDK). Suggested: a short "running Box inside a container or a microVM" page, and a hint after the proc-mount failure.

### 4. No Linux release artifact

`deploy-box-artifact.yml` builds `aarch64-unknown-linux-gnu` and `x86_64-unknown-linux-gnu` tarballs and then publishes only the macOS one, and `download.sh` refuses any OS but Darwin. Running Box in the cloud therefore means a Rust toolchain and a 4 minute build. I built it natively in an `amazonlinux:2023` arm64 container so the glibc matches the Lambda MicroVMs base image (`build/build-box.sh` in this repo). A published Linux tarball, even marked preview, would remove that step.

### 5. `download.sh` can hang forever

`curl -fsSL` has no `--connect-timeout` or `--max-time`, and one run sat on the `SHA256SUMS.txt` fetch for over two minutes with no output. `gh release download v0.1.0 -R strands-agents/box` fetched the same files at once and the checksum verified. Suggested: `--connect-timeout 10 --max-time 300 --retry 3`.

### 6. `aws://PROFILE` signs with static keys only, so a cloud workload needs another route to Bedrock

This is a deliberate choice (`crates/credentials/src/sources/aws_profile.rs` explains why `credential_process`, SSO and role profiles are refused). Inside a microVM, a container or an EC2 instance the identity is a role, so `aws://` does not apply. The route that works is the one the refusal text itself names: mint a short-lived Bedrock API key from the role in the trusted host process (`aws-bedrock-token-generator`), pass it to `box run` as `AWS_BEARER_TOKEN_BEDROCK`, and bind it with `secret.ref = "env://AWS_BEARER_TOKEN_BEDROCK"`. The agent gets a stand-in value and the gateway adds the real key. The image in this repo does that per task. Suggested: document it as the cloud pattern.

### 7. The tutorial's model needs account access the reader may not have

The SDK tutorial pins `global.anthropic.claude-opus-5` in `us-west-2`. On an account without that model the run ends in a 60 line botocore traceback from inside the agent. Box behaved correctly (the gateway forwarded, Bedrock refused). Suggested: one line in "If something goes wrong" for `AccessDeniedException ... is not available for this account`, and a note that any Bedrock model with tool use works (I used `global.amazon.nova-2-lite-v1:0`).

### Observation: `find` raises a denial for a file it only walks past

`find . -name '*.py' | xargs wc -l` records `deny fs:read .../.env no_env` although the command never opens `.env`: Strands Shell's `find` checks each entry it visits. The result is correct, and the denial count per task is one higher than the files the agent asked for. Worth knowing when you alert on denial counts.

## microvm-ctl

### 8. `Fleet.size()` counted a VM it had just terminated (fixed)

`ListMicrovms` keeps reporting a terminated VM in its old state for about a second. In the lifecycle scenario, `scale_to(n - 1)` terminated the SUSPENDED member and a `drain()` one second later counted 4 members instead of 3, and terminated the victim a second time. Harmless, but `size()` was wrong. Fixed in microvm-ctl: `Fleet` remembers the VMs it terminated and leaves them out of `members()`.

### 9. No way to spread many short requests over a warm fleet (added)

`lease_many` launches one VM per shard, which is right for long tasks. A box takes about 100 ms, a VM launch about 4 s, so for Strands Box the useful shape is many boxes over VMs that are already up. I wrote that by hand in the first fleet scenario, then added it to the plane as `Fleet.dispatch(path, bodies, per_vm=K)` and `mvm dispatch IMAGE /path`: every RUNNING member gets K workers pulling from one queue, results come back in order, and a failed request is recorded without stopping the rest.

## AWS deployment of the playground

### 11. A Lambda Function URL also needs `lambda:InvokeFunction`

With `AuthType: NONE` and only the `lambda:InvokeFunctionUrl` permission, every request through CloudFront and every direct call came back 403 from Lambda with "Forbidden. For troubleshooting Function URL authorization issues". Function URLs created after October 2025 also need `lambda:InvokeFunction` granted to `*` with `InvokedViaFunctionUrl: true`. `infra/playground.yaml` carries both.

### 12. `ReservedConcurrentExecutions` fails on a new account

The account's concurrency limit is 10 and Lambda keeps 10 unreserved, so reserving 4 for the playground failed the stack ("decreases account's UnreservedConcurrentExecution below its minimum value of [10]"). The playground is bounded by WAF rate limits and its own hourly launch budget instead.

## Lambda MicroVMs

### 10. The namespace launcher works once the image has the extra capabilities

An image built without `additionalOsCapabilities` boots the app as root with a bounded capability set, and Box fails at the proc mount (entry 3). Built with `["ALL"]`, the VM reports `CapEff 000001ffffffffff`, no seccomp filter, `max_user_namespaces 15985`, and `unshare --user --mount --pid --mount-proc` succeeds. The box inside still drops everything for the agent, so the extra capabilities belong to the trusted host process only.
