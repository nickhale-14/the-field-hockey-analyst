#!/usr/bin/env bash
# Build the MCP server image and push it to ECR under a tag you can read.
#
# ":latest" tells you nothing once a service is running: you cannot say which
# code is live, and rolling back means guessing. So every build also gets an
# immutable tag of the form
#
#     2026-10-07-9b042fc-db-parts
#     └─ date ──┘ └─sha─┘ └─ what changed ─┘
#
# The date orders them, the commit sha ties the image to exact code, and the
# slug is for you reading the ECR list in three weeks. Point the ECS task
# definition at THAT tag, never at :latest — then rollback is changing a string
# to one you can identify.
#
# Usage (from anywhere):
#   ./mcp/release.sh "db parts"      # build, tag, push
#   ./mcp/release.sh "db parts" --dry-run   # build and tag only, print the commands
set -euo pipefail
cd "$(dirname "$0")"

REGISTRY="<aws-account-id>.dkr.ecr.ap-southeast-2.amazonaws.com"
REPO="hockey-master-mcp"
REGION="ap-southeast-2"

SLUG_RAW="${1:-}"
DRY_RUN="${2:-}"
if [[ -z "$SLUG_RAW" ]]; then
  echo "usage: ./mcp/release.sh \"short description\" [--dry-run]" >&2
  exit 1
fi

# Tags allow letters, digits, and - _ . only. Lowercase, spaces to dashes,
# everything else dropped, capped so the tag stays readable.
SLUG=$(echo "$SLUG_RAW" | tr '[:upper:]' '[:lower:]' | tr ' ' '-' | tr -cd 'a-z0-9-' | cut -c1-30)
SHA=$(git rev-parse --short HEAD)
DATE=$(date +%Y-%m-%d)
TAG="${DATE}-${SHA}-${SLUG}"

# A dirty tree means the sha in the tag does not describe what you are shipping.
if ! git diff --quiet HEAD -- .; then
  echo "WARNING: uncommitted changes in mcp/ — the tag's commit sha will not match the image." >&2
  echo "         Commit first, or accept that $SHA is a lie." >&2
fi

echo "building $TAG (linux/amd64 — Fargate is x86; an arm64 image will not start)"
# --provenance=false: BuildKit otherwise attaches a build-attestation manifest, which
# lands in ECR as a third, 0 MB "Other" row per push and makes the image list unreadable.
# Nothing here consumes attestations.
docker build --platform linux/amd64 --provenance=false \
  -t "hockey-mcp:${TAG}" -t "hockey-mcp:latest" .

docker tag "hockey-mcp:${TAG}" "${REGISTRY}/${REPO}:${TAG}"
docker tag "hockey-mcp:${TAG}" "${REGISTRY}/${REPO}:latest"

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo
  echo "built and tagged, not pushed. To push:"
  echo "  aws ecr get-login-password --region ${REGION} | docker login --username AWS --password-stdin ${REGISTRY}"
  echo "  docker push ${REGISTRY}/${REPO}:${TAG}"
  echo "  docker push ${REGISTRY}/${REPO}:latest"
  exit 0
fi

echo "logging in to ECR"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY"

echo "pushing $TAG"
docker push "${REGISTRY}/${REPO}:${TAG}"
docker push "${REGISTRY}/${REPO}:latest"

echo
echo "pushed. Use this in the ECS task definition:"
echo "  ${REGISTRY}/${REPO}:${TAG}"
