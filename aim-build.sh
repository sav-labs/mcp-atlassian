#!/bin/bash
# Сборка и пуш AIM-форка mcp-atlassian на Docker Hub (кластер тянет через
# зеркало docker-hub.binary.alfabank.ru). Тег = image.tag в
# mcp-servers/open-source/atlassian-mcp/values/<кластер>.yaml.
# Тег не перезаливать: поменял код — подними суффикс (-aim2, -aim3, ...).
#
#   ./aim-build.sh 0.21.1-aim1
set -euo pipefail
cd "$(dirname "$0")"

IMAGE_NAME="sav116/mcp-atlassian"
IMAGE_TAG="${1:?usage: $0 <tag>, например 0.21.1-aim1}"

# Незакоммиченное в образ не тащим — тег должен соответствовать коммиту.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "есть незакоммиченные изменения — сначала коммит" >&2
  exit 1
fi

if docker manifest inspect "${IMAGE_NAME}:${IMAGE_TAG}" >/dev/null 2>&1; then
  echo "${IMAGE_NAME}:${IMAGE_TAG} уже есть на Docker Hub — возьми новый тег" >&2
  exit 1
fi

# Кластер amd64 — с Apple Silicon без --platform соберётся arm64
# и под упадёт с "exec format error".
docker buildx build \
  --platform linux/amd64 \
  --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
  --label "org.opencontainers.image.source=https://github.com/sav-labs/mcp-atlassian" \
  -t "${IMAGE_NAME}:${IMAGE_TAG}" \
  --push \
  .

echo "pushed ${IMAGE_NAME}:${IMAGE_TAG} ($(git rev-parse --short HEAD))"
