#!/bin/bash
# Export a clean snapshot of the main branch to the public release tree without private history.
#   deploy/release-public.sh vX.Y.Z
# Environment overrides:
#   PB_PUBLIC_TREE        destination public git repository (default: $SRC/../PromptBridge-public)
#   PB_RELEASE_DENYLIST   grep -E pattern file for personal strings (default: $SRC/ai-orchestrator/.system/release-denylist.txt)
# Never pushes to any remote; prints the manual push command after creating the release commit and tag.
set -euo pipefail

VERSION="${1:-}"
SRC="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
DST="${PB_PUBLIC_TREE:-"$SRC/../PromptBridge-public"}"
DENY="${PB_RELEASE_DENYLIST:-"$SRC/ai-orchestrator/.system/release-denylist.txt"}"

FORBIDDEN_PATHS_RE='^_isbirligi/|(^|/)\.system/|\.db$|\.db-(wal|shm)$|\.log$|\.lock$|node_modules/|(^|/)\.venv/|(^|/)\.claude/|\.DS_Store$'
SECRET_PATTERNS_RE='(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{35}|xox[bp]-|BEGIN [A-Z ]*PRIVATE KEY)'

fail() {
    echo "release-public.sh: $*" >&2
    exit 1
}

rollback_fail() {
    git -C "$DST" reset -q --hard HEAD
    fail "$*"
}

# Preconditions
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "invalid version '$VERSION' (expected vX.Y.Z)"

[ -z "$(git -C "$SRC" status --porcelain)" ] || fail "source tree ($SRC) is not clean"
[ "$(git -C "$SRC" rev-parse --abbrev-ref HEAD)" = "main" ] || fail "source tree ($SRC) must be on branch 'main'"

[ -d "$DST" ] && [ -e "$DST/.git" ] && git -C "$DST" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || fail "destination ($DST) is not a git repository"
[ -z "$(git -C "$DST" status --porcelain)" ] || fail "destination tree ($DST) is not clean"

dst_email="$(git -C "$DST" config --get user.email || true)"
[[ "$dst_email" == ?*@users.noreply.github.com ]] \
    || fail "destination user.email ('$dst_email') must end with @users.noreply.github.com"

if git -C "$DST" rev-parse -q --verify "refs/tags/$VERSION" >/dev/null 2>&1; then
    fail "tag '$VERSION' already exists in destination ($DST)"
fi

[ -f "$DENY" ] && [ -s "$DENY" ] && grep -q '[^[:space:]]' "$DENY" \
    || fail "release denylist ($DENY) is missing or empty"

# Refresh destination tree from source HEAD snapshot
git -C "$DST" rm -rq --ignore-unmatch .
git -C "$SRC" archive --format=tar HEAD | tar -x -C "$DST"
git -C "$DST" add -A

# Verification gates (rollback DST on any failure)
forbidden_hits="$(git -C "$DST" ls-files | grep -E "$FORBIDDEN_PATHS_RE" || true)"
if [ -n "$forbidden_hits" ]; then
    printf '%s\n' "$forbidden_hits" >&2
    rollback_fail "forbidden path(s) detected in staged tree"
fi

deny_hits=""
deny_rc=0
deny_hits="$(git -C "$DST" grep -I -n -E -f "$DENY" --cached)" || deny_rc=$?
if [ "$deny_rc" -eq 0 ]; then
    printf '%s\n' "$deny_hits" >&2
    rollback_fail "personal pattern(s) from denylist matched in staged tree"
elif [ "$deny_rc" -ne 1 ]; then
    rollback_fail "denylist grep failed with exit code $deny_rc"
fi

secret_hits=""
secret_rc=0
secret_hits="$(git -C "$DST" grep -I -n -E -e "$SECRET_PATTERNS_RE" --cached)" || secret_rc=$?
if [ "$secret_rc" -eq 0 ]; then
    printf '%s\n' "$secret_hits" >&2
    rollback_fail "secret pattern(s) matched in staged tree"
elif [ "$secret_rc" -ne 1 ]; then
    rollback_fail "secret pattern grep failed with exit code $secret_rc"
fi

# Exit cleanly if nothing changed compared to destination HEAD
if git -C "$DST" diff --cached --quiet; then
    echo "nothing to release"
    exit 0
fi

git -C "$DST" commit -q -m "Release $VERSION"
git -C "$DST" tag -a "$VERSION" -m "PromptBridge $VERSION"

commit_sha="$(git -C "$DST" rev-parse HEAD)"
file_count="$(git -C "$DST" ls-files | wc -l | tr -d ' ')"

echo "commit: $commit_sha ($file_count files)"
echo "To publish (needs the owner's approval): git -C $DST push origin main && git -C $DST push origin $VERSION"
