#!/usr/bin/env bash
# Answer one review comment and resolve its thread (#326).
#
#   scripts/reply-resolve.sh <owner/repo> <pr> <comment_id> <message>
#
# Posts <message> as a reply in the comment's thread, with the "Addressed by" trailer, then
# resolves that thread. <comment_id> is the review comment's numeric id (the `databaseId` of
# the thread's first comment). Needs `gh` signed in with access to the repository.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 <owner/repo> <pr> <comment_id> <message>" >&2
  exit 2
fi
repo=$1 pr=$2 comment=$3 message=$4
owner=${repo%%/*} name=${repo##*/}

trailer='_🤖 Addressed by [Claude Code](https://claude.com/claude-code)_'
gh api "repos/${repo}/pulls/${pr}/comments/${comment}/replies" \
  -f body="${message}"$'\n\n'"${trailer}" --jq '"replied: \(.html_url)"'

# shellcheck disable=SC2016  # $owner and the like are GraphQL variables, not shell ones
thread=$(gh api graphql -F owner="$owner" -F name="$name" -F pr="$pr" -f query='
  query($owner: String!, $name: String!, $pr: Int!) {
    repository(owner: $owner, name: $name) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100) { nodes { id comments(first: 1) { nodes { databaseId } } } }
      }
    }
  }' --jq ".data.repository.pullRequest.reviewThreads.nodes[] | select(.comments.nodes[0].databaseId == ${comment}) | .id")

if [[ -z "$thread" ]]; then
  echo "no review thread starts with comment ${comment} on ${repo}#${pr}" >&2
  exit 1
fi
# shellcheck disable=SC2016
gh api graphql -F thread="$thread" -f query='
  mutation($thread: ID!) { resolveReviewThread(input: {threadId: $thread}) { thread { isResolved } } }' \
  --jq '"resolved: \(.data.resolveReviewThread.thread.isResolved)"'
