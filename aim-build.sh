#!/bin/bash
# Сборка и пуш AIM-форка mcp-atlassian прямо в наш Artifactory
# (mlops-docker-releases), откуда его тянут кластеры. Тег = image.tag в
# mcp-servers/open-source/atlassian-mcp/values/<кластер>.yaml.
# Тег не перезаливать: поменял код — подними суффикс (-aim2, -aim3, ...).
#
#   ./aim-build.sh 0.21.1-aim1
set -euo pipefail
cd "$(dirname "$0")"

IMAGE_NAME="mlops-docker-releases.binary.alfabank.ru/mcp-atlassian"
IMAGE_TAG="${1:?usage: $0 <tag>, например 0.21.1-aim1}"

# Собираем рабочую копию как есть. Незакоммиченное не блокирует, но
# помечается в revision образа суффиксом -dirty.
REVISION="$(git rev-parse HEAD)"
if [ -n "$(git status --porcelain)" ]; then
  echo "внимание: есть незакоммиченные изменения — собираю как есть (-dirty)" >&2
  REVISION="${REVISION}-dirty"
fi

# Кластер amd64 — с Apple Silicon без --platform соберётся arm64
# и под упадёт с "exec format error".
# Пуш — отдельным docker push (его делает демон), а не buildx --push: buildx
# берёт токен Artifactory с самого Mac, а macOS считает сертификат
# *.binary.alfabank.ru отозванным (x509: certificate is revoked).
docker build \
  --platform linux/amd64 \
  --label "org.opencontainers.image.revision=${REVISION}" \
  --label "org.opencontainers.image.source=https://github.com/sav-labs/mcp-atlassian" \
  -t "${IMAGE_NAME}:${IMAGE_TAG}" \
  .
docker push "${IMAGE_NAME}:${IMAGE_TAG}"

echo "pushed ${IMAGE_NAME}:${IMAGE_TAG} (${REVISION})"
