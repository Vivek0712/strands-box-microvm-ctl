#!/bin/sh
# Package the playground API and page, and deploy them behind CloudFront.
#
#   source .env.mvm && ./infra/deploy.sh [stack-name]
#
# Needs the MVM_* variables (bucket, profile, region, execution role). Generates the playground key and
# the origin secret on first deploy and keeps them in .playground-secrets (git-ignored).
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
stack=${1:-strands-box-playground}
region=${MVM_REGION:-us-east-1}
profile=${MVM_PROFILE:-${AWS_PROFILE:-default}}
aws="aws --profile $profile --region $region"
secrets="$here/.playground-secrets"
[ -f "$secrets" ] || printf 'PLAYGROUND_KEY=%s\nORIGIN_SECRET=%s\n' "$(openssl rand -hex 16)" "$(openssl rand -hex 24)" > "$secrets"
. "$secrets"

build=$(mktemp -d)
pip install --quiet --target "$build" --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.12 \
  "microvm-ctl @ file://${MICROVM_CTL_SRC:-$HOME/microvm-ctl}" >/dev/null
rm -rf "$build"/boto3 "$build"/botocore "$build"/s3transfer   # the runtime ships them
cp "$here/playground/server.py" "$build/"
mkdir -p "$build/static" "$build/box" "$build/results"
cp "$here/playground/static/index.html" "$build/static/"
cp "$here/image/box/policy.dw" "$here/image/box/box.toml.tmpl" "$build/box/"
for f in single density fleet dispatch lease lifecycle plan-reject failure; do
  latest=$(ls "$here"/results/$f-*.json 2>/dev/null | tail -1); [ -n "$latest" ] && cp "$latest" "$build/results/"
done
key="playground/api-$(date +%s).zip"
(cd "$build" && zip -qr "$build.zip" .)
$aws s3 cp --quiet "$build.zip" "s3://$MVM_ARTIFACT_BUCKET/$key"

$aws cloudformation deploy --stack-name "$stack" --template-file "$here/infra/playground.yaml" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset --parameter-overrides \
  PlaygroundKey="$PLAYGROUND_KEY" OriginSecret="$ORIGIN_SECRET" CodeBucket="$MVM_ARTIFACT_BUCKET" CodeKey="$key" \
  VmExecutionRoleArn="$MVM_EXECUTION_ROLE_ARN"
out() { $aws cloudformation describe-stacks --stack-name "$stack" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
$aws s3 cp --quiet "$here/playground/static/index.html" "s3://$(out SiteBucket)/index.html" --content-type "text/html; charset=utf-8" --cache-control "no-cache"
$aws cloudfront create-invalidation --distribution-id "$(out DistributionId)" --paths "/*" >/dev/null
echo "playground: $(out Url)   key: in $secrets"
