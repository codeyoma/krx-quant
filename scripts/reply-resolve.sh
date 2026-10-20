#!/usr/bin/env bash
# Answer one review comment and resolve its thread (#326).
#
#   scripts/reply-resolve.sh <owner/repo> <pr> <comment_id> <message>
#
# Finds the review thread that starts with <comment_id> (the numeric id, the `databaseId` of the
# thread's first comment), posts <message> there as a reply with the "Addressed by" trailer, then
# resolves the thread. Nothing is posted when the thread is not found, so a retry never answers
# twice. Needs `gh` signed in with access to the repository.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 <owner/repo> <pr> <comment_id> <message>" >&2
  exit 2
fi
repo=$1 pr=$2 comment=$3 message=$4
owner=${repo%%/*} name=${repo##*/}

# Every page of the PR's review threads, then the one whose first comment is <comment_id>.
# shellcheck disable=SC2016  # $owner and the like are GraphQL variables, not shell ones
thread=$(gh api graphql --paginate -F owner="$owner" -F name="$name" -F pr="$pr" -f query='
  query($owner: String!, $name: String!, $pr: Int!, $endCursor: String) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100, after: $endCursor) {
          nodes { id comments(first: 1) { nodes { databaseId } } }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }' --jq ".data.repository.pullRequest.reviewThreads.nodes[] | select(.comments.nodes[0].databaseId == ${comment}) | .id")

if [[ -z "$thread" ]]; then
  echo "no review thread starts with comment ${comment} on ${repo}#${pr}; nothing posted" >&2
  exit 1
fi

trailer='_🤖 Addressed by [Claude Code](https://claude.com/claude-code)_'
gh api "repos/${repo}/pulls/${pr}/comments/${comment}/replies" \
  -f body="${message}"$'\n\n'"${trailer}" --jq '"replied: \(.html_url)"'

# shellcheck disable=SC2016
gh api graphql -F thread="$thread" -f query='
  mutation($thread: ID!) { resolveReviewThread(input: {threadId: $thread}) { thread { isResolved } } }' \
  --jq '"resolved: \(.data.resolveReviewThread.thread.isResolved)"'
