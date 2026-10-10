#!/usr/bin/env bash
# Build a Keycloak 26.7.4 image that runs on hosts without the x86-64-v2
# instruction set (e.g. Intel Core2 Duo). The upstream image is EL9-based and
# its glibc aborts with "Fatal glibc error: CPU does not support x86-64-v2".
#
# Strategy: keep the upstream distribution files (already pulled from quay.io),
# but run them on eclipse-temurin:17-jdk (Ubuntu baseline glibc, x86-64-v1).
#
# Run on the deploy host from this directory:  bash build-on-host.sh
set -uo pipefail

IMAGE_UPSTREAM=quay.io/keycloak/keycloak:26.7.4
IMAGE_LOCAL=kge-keycloak:26.7.4-v1
WORK=${WORK:-/home/$(id -un)/kc-build}

mkdir -p "$WORK"
cd "$WORK"

if [ ! -d keycloak ]; then
  echo "--- extracting ${IMAGE_UPSTREAM}:/opt/keycloak ---"
  NAME="kc-extract-$(date +%s)"
  docker create --name "$NAME" "$IMAGE_UPSTREAM" >/dev/null || exit 1
  docker cp "$NAME":/opt/keycloak ./keycloak || exit 1
  docker container prune -f >/dev/null 2>&1
fi

# Dockerfile and this script live next to each other in the repo.
cp "$(dirname "$0")/Dockerfile" "$WORK/Dockerfile"

# The host hits a containerd/LVM overlayfs unmount bug intermittently; cleanup +
# retry is the documented workaround (see deploy notes).
for attempt in 1 2 3 4 5 6; do
  for m in /data/containerd/tmpmounts/*; do umount -l "$m" 2>/dev/null; done
  echo "=== build attempt $attempt ==="
  if docker build --network=host -t "$IMAGE_LOCAL" "$WORK" > /tmp/kcbuild.log 2>&1; then
    echo BUILD_OK
    break
  fi
  tail -8 /tmp/kcbuild.log
  docker builder prune -f >/dev/null 2>&1
  sleep 4
done

docker image inspect -f 'image ok: {{.Id}}' "$IMAGE_LOCAL" || exit 1
echo "=== kc.sh version (proves glibc + JVM run on this CPU) ==="
docker run --rm --entrypoint /opt/keycloak/bin/kc.sh "$IMAGE_LOCAL" version 2>&1 | tail -5
