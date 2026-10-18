#!/usr/bin/env bash
set -euo pipefail

storage_root="${1:?Usage: prepare-object-mounts.sh /absolute/storage/path}"
[[ "$storage_root" = /* && "$storage_root" != / ]] || {
    printf '%s\n' 'An absolute storage directory is required.' >&2
    exit 2
}
[[ "$(id -u)" = 0 ]] || {
    printf '%s\n' 'Run this command as root on the Docker host.' >&2
    exit 2
}
storage_root="$(realpath -e -- "$storage_root")"
mount_root="$storage_root/object-mounts"
[[ ! -L "$mount_root" ]] || {
    printf '%s\n' 'The mount directory must not be a symbolic link.' >&2
    exit 2
}
mkdir -p -- "$mount_root"
chmod 0700 -- "$mount_root"
if ! mountpoint -q -- "$mount_root"; then
    mount --bind "$mount_root" "$mount_root"
fi
mount --make-rshared "$mount_root"
findmnt --target "$mount_root" --output TARGET,PROPAGATION
