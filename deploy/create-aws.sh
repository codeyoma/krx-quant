#!/usr/bin/env bash
# Create the AWS side once: the S3 bucket for recorded days, an IAM user that can only
# read and write that bucket, and the Lightsail instance (2 GB, public IPv4) with its
# firewall and daily automatic snapshots, all in one Region.
#
# Run on your own computer after `aws configure` with an administrator profile:
#
#   deploy/create-aws.sh <globally-unique-bucket-name>
#
# The Region is AWS_REGION, else the profile's region. An account made through AWS's new
# sign-up gets a single Region (ours is ap-southeast-2); see docs/operations.md.
#
# Safe to run again: existing resources are kept. It does not create access keys; see
# docs/operations.md for that one-time step.
set -euo pipefail

bucket=${1:?usage: deploy/create-aws.sh <globally-unique-bucket-name>}
region=${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}
if [[ -z "$region" ]]; then
  echo "create-aws: no AWS Region; set AWS_REGION (e.g. ap-southeast-2)" \
    "or a region in aws configure" >&2
  exit 1
fi
zone=${ZONE:-${region}a}
instance=${INSTANCE:-krx-quant}
bundle=${BUNDLE:-}                  # unset: picked below
blueprint=${BLUEPRINT:-ubuntu_24_04}
iam_user=${IAM_USER:-krx-quant-engine}
snapshot_utc=${SNAPSHOT_UTC:-18:00} # 03:00 KST, outside every session
here=$(cd "$(dirname "$0")" && pwd)

echo "== checking the Lightsail plan and image in $region"
if [[ -z "$bundle" ]]; then
  # 2 GB RAM with a public IPv4 (KIS has no IPv6). Bundle IDs differ by Region
  # (small_3_0 in Seoul, small_3_2 in Sydney), so look it up.
  query="bundles[?isActive && ramSizeInGb==\`2\` && publicIpv4AddressCount==\`1\`"
  query+=" && contains(supportedPlatforms, 'LINUX_UNIX')].bundleId"
  found=$(aws lightsail get-bundles --region "$region" --query "$query" --output text)
  read -r -a bundles <<<"$found"
  if [[ ${#bundles[@]} -ne 1 ]]; then
    echo "create-aws: expected one active Linux 2 GB bundle with a public IPv4 in $region," \
      "found ${#bundles[@]} (${bundles[*]:-none}); set BUNDLE, see" \
      "aws lightsail get-bundles --region $region" >&2
    exit 1
  fi
  bundle=${bundles[0]}
elif [[ -z "$(aws lightsail get-bundles --region "$region" \
  --query "bundles[?bundleId=='$bundle'].bundleId" --output text)" ]]; then
  echo "create-aws: Lightsail bundle $bundle not found; list them with" \
    "aws lightsail get-bundles --region $region" >&2
  exit 1
fi
echo "bundle: $bundle"
if [[ -z "$(aws lightsail get-blueprints --region "$region" \
  --query "blueprints[?blueprintId=='$blueprint'].blueprintId" --output text)" ]]; then
  echo "create-aws: Lightsail blueprint $blueprint not found" >&2
  exit 1
fi

echo "== S3 bucket $bucket"
if ! aws s3api head-bucket --bucket "$bucket" --region "$region" 2>/dev/null; then
  aws s3api create-bucket --bucket "$bucket" --region "$region" \
    --create-bucket-configuration LocationConstraint="$region" >/dev/null
fi
aws s3api put-public-access-block --bucket "$bucket" --region "$region" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
# The engine may delete only the models it no longer keeps (engine/models/, #361), and may
# overwrite everything; a delete or an overwrite with a bad file would still lose data: keep
# earlier versions for 30 days (the engine cannot delete those), and drop uploads that died
# half-way.
aws s3api put-bucket-versioning --bucket "$bucket" --region "$region" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" --region "$region" \
  --lifecycle-configuration '{
  "Rules": [{
    "ID": "keep-replaced-versions-30-days",
    "Status": "Enabled",
    "Filter": {},
    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}
  }]
}' >/dev/null

echo "== IAM user $iam_user (this bucket only; deletes only under engine/models/)"
if ! aws iam get-user --user-name "$iam_user" >/dev/null 2>&1; then
  aws iam create-user --user-name "$iam_user" >/dev/null
fi
aws iam put-user-policy --user-name "$iam_user" --policy-name krx-quant-data \
  --policy-document "$(sed "s/BUCKET/$bucket/g" "$here/iam-policy.json")"

echo "== Lightsail instance $instance"
created=
if ! aws lightsail get-instance --instance-name "$instance" --region "$region" >/dev/null 2>&1; then
  aws lightsail create-instances --instance-names "$instance" --region "$region" \
    --availability-zone "$zone" --blueprint-id "$blueprint" --bundle-id "$bundle" \
    --ip-address-type dualstack >/dev/null
  created=yes
fi
for _ in $(seq 60); do
  state=$(aws lightsail get-instance-state --instance-name "$instance" --region "$region" \
    --query state.name --output text)
  [[ "$state" == running ]] && break
  sleep 5
done
echo "instance state: $state"

if [[ -n "$created" ]]; then
  echo "== firewall: browser SSH and Tailscale only"
  # Only for a new instance: a rerun must not reopen port 22 after it was closed.
  # Port 22 is open to the Lightsail browser console only; close it once Tailscale SSH works.
  aws lightsail put-instance-public-ports --instance-name "$instance" --region "$region" \
    --port-infos '[
      {"fromPort": 22, "toPort": 22, "protocol": "tcp", "cidrListAliases": ["lightsail-connect"]},
      {"fromPort": 41641, "toPort": 41641, "protocol": "udp", "cidrs": ["0.0.0.0/0"], "ipv6Cidrs": ["::/0"]}
    ]' >/dev/null
fi

echo "== daily automatic snapshots at $snapshot_utc UTC"
aws lightsail enable-add-on --resource-name "$instance" --region "$region" --add-on-request \
  "{\"addOnType\": \"AutoSnapshot\", \"autoSnapshotAddOnRequest\": {\"snapshotTimeOfDay\": \"$snapshot_utc\"}}" \
  >/dev/null

cat <<NEXT

Done. Next (docs/operations.md):
  1. Once only, create the engine's access key and put it in the server's .env:
       aws iam create-access-key --user-name $iam_user
  2. Set KRX_OBJECT_STORE=s3://$bucket/engine in the server's .env.
  3. Open the Lightsail browser SSH for $instance and run deploy/setup-server.sh.
NEXT
