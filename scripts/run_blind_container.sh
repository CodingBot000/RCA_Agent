#!/bin/sh
# Only pass a trusted, pre-existing local image. No host or network access is granted.
set -eu
if [ "$#" -lt 3 ]; then
    echo "Usage: $0 BLIND_DIRECTORY LOCAL_IMAGE COMMAND [ARG ...]" >&2
    exit 2
fi
blind_dir=$1
image=$2
shift 2
uv run --locked --no-sync python -m rca_agent.phase1 validate "$blind_dir"
blind_dir=$(cd "$blind_dir" && pwd -P)
exec docker run --rm --pull=never --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges \
    --mount "type=bind,src=$blind_dir,dst=/task,readonly" "$image" "$@"
