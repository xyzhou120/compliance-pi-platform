#!/usr/bin/env sh
set -eu
bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 "$bundle_dir/verify-offline-bundle.py" "$bundle_dir"
test -f "$bundle_dir/.env" || { echo 'Copy .env.example to .env and provide deployment secrets/endpoints.' >&2; exit 1; }
docker compose --env-file "$bundle_dir/.env" -f "$bundle_dir/compose.offline.yml" config --quiet
docker compose --env-file "$bundle_dir/.env" -f "$bundle_dir/compose.offline.yml" up -d --pull never --no-build
