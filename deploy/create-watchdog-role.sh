#!/usr/bin/env bash
# Create the AWS role the server watchdog takes (.github/workflows/server-watchdog.yml, #385).
#
# Run once on your own computer, from the repository checkout, with the administrator profile of
# `aws configure` (the one deploy/create-aws.sh used):
#
#   deploy/create-watchdog-role.sh
#
# It makes, keeping what is there already:
# - GitHub's OIDC provider in IAM (token.actions.githubusercontent.com, audience sts.amazonaws.com);
# - the role krx-quant-watchdog, which only workflows of codeyoma/krx-quant running on main may
#   take: no stored AWS key;
# - the role's one inline policy (deploy/watchdog-policy.json): read the instances' state,
#   metrics and operations in the Region, and reboot the one instance. Lightsail's read actions
#   take no resource ("*" only, Service Authorization Reference); RebootInstance takes the
#   instance's ARN, looked up here;
# - the repository secret AWS_WATCHDOG_ROLE_ARN, with gh when it is logged in.
#
# Safe to run again: it sets the trust and the policy to what this file says. It prints no ARN:
# the ARNs hold the account ID, and the repository is public. Do not paste them anywhere public.
#
# Defaults, changed by environment variables: AWS_REGION (else the profile's, else
# ap-southeast-2), INSTANCE (krx-quant), ROLE (krx-quant-watchdog), REPO (codeyoma/krx-quant),
# BRANCH (main).
set -euo pipefail

region=${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}
region=${region:-ap-southeast-2}
instance=${INSTANCE:-krx-quant}
role=${ROLE:-krx-quant-watchdog}
repo=${REPO:-codeyoma/krx-quant}
branch=${BRANCH:-main}
here=$(cd "$(dirname "$0")" && pwd)
issuer=token.actions.githubusercontent.com
audience=sts.amazonaws.com

echo "== AWS account"
account=$(aws sts get-caller-identity --query Account --output text)
provider="arn:aws:iam::$account:oidc-provider/$issuer"
instance_arn=$(aws lightsail get-instance --instance-name "$instance" --region "$region" \
  --query instance.arn --output text)
echo "instance $instance in $region found"

echo "== GitHub OIDC provider ($issuer)"
if clients=$(aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$provider" \
  --query ClientIDList --output text 2>/dev/null); then
  if ! tr '\t' '\n' <<<"$clients" | grep -qx "$audience"; then
    aws iam add-client-id-to-open-id-connect-provider \
      --open-id-connect-provider-arn "$provider" --client-id "$audience"
  fi
  echo "already there"
else
  # AWS no longer checks these thumbprints for GitHub (it trusts GitHub's certificate authority);
  # older AWS CLIs require the option.
  aws iam create-open-id-connect-provider --url "https://$issuer" --client-id-list "$audience" \
    --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 1c58a3a8518e8759bf075b76b750d4f2df264fcd \
    >/dev/null
  echo "created"
fi

echo "== role $role (workflows of $repo on $branch only)"
trust=$(
  cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GitHubActionsOfThisRepositorysMainOnly",
      "Effect": "Allow",
      "Principal": {"Federated": "$provider"},
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "$issuer:aud": "$audience",
          "$issuer:sub": "repo:$repo:ref:refs/heads/$branch"
        }
      }
    }
  ]
}
JSON
)
if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$role" --policy-document "$trust"
  echo "trust updated"
else
  aws iam create-role --role-name "$role" --assume-role-policy-document "$trust" \
    --description "The krx-quant server watchdog (GitHub Actions): read the server's health, reboot it" \
    >/dev/null
  echo "created"
fi
aws iam put-role-policy --role-name "$role" --policy-name krx-quant-watchdog \
  --policy-document "$(sed -e "s|@REGION@|$region|g" -e "s|@INSTANCE_ARN@|$instance_arn|g" \
    "$here/watchdog-policy.json")"
echo "policy krx-quant-watchdog set (deploy/watchdog-policy.json)"
role_arn=$(aws iam get-role --role-name "$role" --query Role.Arn --output text)

echo "== repository secret AWS_WATCHDOG_ROLE_ARN ($repo)"
if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  printf '%s' "$role_arn" | gh secret set AWS_WATCHDOG_ROLE_ARN --repo "$repo"
else
  echo "gh is not logged in. Set it yourself (it asks for the value):"
  echo "  aws iam get-role --role-name $role --query Role.Arn --output text"
  echo "  gh secret set AWS_WATCHDOG_ROLE_ARN --repo $repo"
fi

cat <<NEXT

Done. Next (docs/operations.md 1장, "서버 watchdog"):
  1. Optional, Telegram as well: gh secret set TELEGRAM_BOT_TOKEN --repo $repo
                                 gh secret set TELEGRAM_CHAT_ID --repo $repo
  2. Once #385 is on main, try it without acting:
       gh workflow run server-watchdog --repo $repo -f dry_run=true
NEXT
