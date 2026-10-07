# Strands Box on AWS Lambda MicroVMs: the box engine, a Strands Agents SDK agent, and the hook
# runtime that turns each HTTP request or lease into one task in a fresh box.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip zsh util-linux && dnf clean all
# The model-driven agent runs from its own virtual environment, which the box grants read on.
RUN python3.12 -m venv /opt/sbx/venv && /opt/sbx/venv/bin/pip install --no-cache-dir "strands-agents>=1.57,<2" \
    && python3.12 -m pip install --no-cache-dir "boto3>=1.43" aws-bedrock-token-generator

# Box 0.1.0 built for aarch64-unknown-linux-gnu: the CLI, the shell alias, and the trampoline
# ship together, because the box resolves the trampoline from beside its own executable.
COPY box-core/ /opt/sbx/box-core/
COPY agent/ /opt/sbx/agent/
COPY box/ /opt/sbx/box/
COPY seed/ /opt/sbx/seed/
COPY sbx.py microvm_hooks.py app.py /opt/sbx/
RUN chmod 0755 /opt/sbx/box-core/* && mkdir -p /work

WORKDIR /opt/sbx
EXPOSE 8080
ENTRYPOINT ["python3.12", "/opt/sbx/app.py"]
