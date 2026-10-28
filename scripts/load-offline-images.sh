#!/usr/bin/env sh
set -eu
bundle_dir=${1:?usage: load-offline-images.sh BUNDLE_DIR}
python3 "$bundle_dir/load-offline-images.py" "$bundle_dir"
