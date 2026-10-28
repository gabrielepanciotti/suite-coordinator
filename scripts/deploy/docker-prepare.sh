#!/usr/bin/env bash
# Vendor the unpublished @suite/design-system package into the frontend build context
# so `docker compose build` can install it via file: (the registry has no "*" and the
# package source lives outside this build context, in $SRV_ROOT/Life/packages).
#
# Mirrors the HomeResearcher pattern, the first Life node to Dockerize a
# design-system consumer. See $SRV_ROOT/Life/docs/design-system-consumption.md.
#
# Run before `docker compose build frontend`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Life sta accanto a questo repo (<SrvRoot>/Life, <SrvRoot>/<questo nodo>): il
# path si deduce da $ROOT invece di essere hardcodato, cosi' lo script vale su
# ogni device. LIFE_FRAMEWORK_DIR lo sovrascrive se il checkout sta altrove.
DS_SRC="${LIFE_FRAMEWORK_DIR:-$ROOT/../Life}/packages/design-system"
VENDOR="$ROOT/frontend/vendor"
TARBALL="$VENDOR/suite-design-system.tgz"

echo "-> Building @suite/design-system dist (tsup)..."
(cd "$DS_SRC" && npm run build >/dev/null)

echo "-> Packing into $VENDOR ..."
mkdir -p "$VENDOR"
rm -f "$VENDOR"/*.tgz
PACKED="$(cd "$DS_SRC" && npm pack --pack-destination "$VENDOR" --silent | tail -1)"
mv -f "$VENDOR/$PACKED" "$TARBALL"

echo "Vendored: ${TARBALL#$ROOT/}"
