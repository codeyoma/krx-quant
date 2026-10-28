#!/usr/bin/env bash
# Create the AWS access the server watchdog uses (.github/workflows/server-watchdog.yml, #385, #387).
#
# Run once on your own computer, from the repository checkout, with the administrator profile of
# `aws configure` (the one deploy/create-aws.sh used) and gh logged in to GitHub:
#
#   deploy/create-watchdog-user.sh
#
# It makes, keeping what is there already:
# - the IAM user krx-quant-watchdog, with no console password and no policy but the next one;
# - the user's one inline policy krx-quant-watchdog (deploy/watchdog-policy.json): read the
#   instances' state, metrics and operations in the Region, and reboot the one instance.
#   Lightsail's read actions take no resource ("*" only, Service Authorization Reference);
#   RebootInstance takes the instance's ARN, looked up here;
# - a new access key, which goes from AWS straight into the repository secrets
#   AWS_WATCHDOG_ACCESS_KEY_ID and AWS_WATCHDOG_SECRET_ACCESS_KEY: gh secret set reads it on its
#   standard input. It is never printed, written to a file or put on a command line.
#
# Once both secrets hold the new key, the user's older keys are deleted, so running it again
# rotates the key. AWS allows two keys per user: when two are there already, the older one is
# deleted before the new one is made, and the script says so.
#
# It stops before it makes anything unless gh is installed and logged in, and reaches the
# repository's secrets. It prints no account ID, ARN or key: the repository is public.
#
# It replaced deploy/create-watchdog-role.sh, which took a role through GitHub's OIDC: the
# account's AWS-managed service control policy denies iam:CreateOpenIDConnectProvider (#387).
#
# Defaults, changed by environment variables: AWS_REGION (else the profile's, else
# ap-southeast-2), INSTANCE (krx-quant), WATCHDOG_USER (krx-quant-watchdog), REPO
# (codeyoma/krx-quant).
set -euo pipefail
# Never trace this script, even under bash -x: the trace would show the key.
{ set +x; } 2>/dev/null

region=${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}
region=${region:-ap-southeast-2}
instance=${INSTANCE:-krx-quant}
user=${WATCHDOG_USER:-krx-quant-watchdog}
repo=${REPO:-codeyoma/krx-quant}
here=$(cd "$(dirname "$0")" && pwd)
tab=$'\t'

stop() {
  echo "$1" >&2
  echo "Nothing was made." >&2
  exit 1
}

echo "== gh (it puts the key into $repo's secrets)"
command -v gh >/dev/null 2>&1 || stop "gh is not installed: brew install gh, then gh auth login."
gh auth status >/dev/null 2>&1 || stop "gh is not logged in: gh auth login."
gh secret list --repo "$repo" >/dev/null 2>&1 ||
  stop "gh cannot reach $repo's secrets: log in as an account that administers it (gh auth login)."
echo "logged in"

echo "== instance $instance"
instance_arn=$(aws lightsail get-instance --instance-name "$instance" --region "$region" \
  --query instance.arn --output text)
echo "found in $region"

echo "== IAM user $user"
if aws iam get-user --user-name "$user" >/dev/null 2>&1; then
  echo "already there"
else
  # No login profile: the user has no console password.
  aws iam create-user --user-name "$user" >/dev/null
  echo "created"
fi
aws iam put-user-policy --user-name "$user" --policy-name krx-quant-watchdog \
  --policy-document "$(sed -e "s|@REGION@|$region|g" -e "s|@INSTANCE_ARN@|$instance_arn|g" \
    "$here/watchdog-policy.json")"
echo "policy krx-quant-watchdog set (deploy/watchdog-policy.json)"

echo "== access key"
# The user's keys, oldest first, one ID a line. An ID is not the secret, but it is not printed
# either. --output text puts the IDs on one line, tab-separated.
listed=$(aws iam list-access-keys --user-name "$user" \
  --query 'sort_by(AccessKeyMetadata,&CreateDate)[].AccessKeyId' --output text)
older=$(printf '%s' "$listed" | tr -s '[:blank:]' '\n' | grep -vx -e '' -e None || true)
count=$(printf '%s' "$older" | grep -c . || true)
if ((count >= 2)); then
  oldest=$(printf '%s\n' "$older" | head -n 1)
  aws iam delete-access-key --user-name "$user" --access-key-id "$oldest"
  older=$(printf '%s\n' "$older" | tail -n +2)
  echo "two keys were there, the most AWS allows: deleted the older one first"
fi

# The new key lives only in this variable and in the pipes to gh. printf is a shell builtin, so
# the key is on no command line either.
key=$(aws iam create-access-key --user-name "$user" \
  --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)
key_id=${key%%"$tab"*}
secret_key=${key#*"$tab"}
unset key
if [[ -z $key_id || -z $secret_key || $key_id == "$secret_key" || $secret_key == *[[:space:]]* ]]; then
  unset key_id secret_key
  echo "AWS answered create-access-key with something other than a key ID and a secret." >&2
  echo "Nothing was put into the secrets and the older keys are kept: run this again, it" >&2
  echo "deletes what this run made once the secrets hold a new key." >&2
  exit 1
fi
echo "made a new key"
if ! printf '%s' "$key_id" | gh secret set AWS_WATCHDOG_ACCESS_KEY_ID --repo "$repo" ||
  ! printf '%s' "$secret_key" | gh secret set AWS_WATCHDOG_SECRET_ACCESS_KEY --repo "$repo"; then
  unset key_id secret_key
  echo "Setting the secrets failed, so the older keys are kept. The two secrets may now hold" >&2
  echo "different keys and the watchdog fail: run this again, it makes another key and then" >&2
  echo "deletes the rest." >&2
  exit 1
fi
unset key_id secret_key
echo "put into the secrets AWS_WATCHDOG_ACCESS_KEY_ID and AWS_WATCHDOG_SECRET_ACCESS_KEY"

# Only now that both secrets hold the new key do the older keys go.
deleted=0
for old in $older; do
  aws iam delete-access-key --user-name "$user" --access-key-id "$old"
  deleted=$((deleted + 1))
done
echo "deleted the older keys: $deleted"

cat <<NEXT

Done. Run this again to rotate the key. Next (docs/operations.md 1장 12번, "서버 watchdog"):
  1. Optional, Telegram as well: gh secret set TELEGRAM_BOT_TOKEN --repo $repo
                                 gh secret set TELEGRAM_CHAT_ID --repo $repo
  2. Try it without acting:
       gh workflow run server-watchdog --repo $repo -f dry_run=true
NEXT
