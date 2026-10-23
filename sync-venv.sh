#!/usr/bin/env bash
# Bring the venv on the vd_config volume to the package versions baked into
# the image compose is about to run. The volume hides the image's own venv, so
# without this a deploy changes the app code and never its packages: prod ran
# the August 27 set until 2026-10-23, 30 packages behind what CI had tested.
#
# pip runs in a one-off container of the NEW image with the volume mounted, as
# vduser, so installed files stay vduser-owned and the UI's yt-dlp update can
# still replace them. Every package is pinned to the image's version, so a
# rollback (TAG=<sha>) syncs back down too. yt-dlp alone is a floor, not a pin:
# one installed from the UI that is newer than the image's is kept. Packages
# the image no longer has are left in place.
#
#   ./sync-venv.sh    # update.sh runs it between `compose pull` and `up -d`
set -euo pipefail
cd "$(dirname "$0")"
export LC_ALL=C   # comm needs both sides sorted the same way

image=$(docker compose config --images app)
volume_pip() {
  docker compose run --rm --no-deps -T --entrypoint /config/venv/bin/pip app "$@"
}

want=$(docker run --rm --entrypoint /config/venv/bin/pip "$image" freeze --all | sort)
before=$(volume_pip freeze --all | sort)

sed 's/^yt-dlp==/yt-dlp>=/' <<<"$want" \
  | volume_pip install --no-cache-dir -q --disable-pip-version-check -r /dev/stdin

after=$(volume_pip freeze --all | sort)
if [ "$before" = "$after" ]; then
  echo "venv already matches $image"
else
  echo "venv synced to $image:"
  diff <(echo "$before") <(echo "$after") | grep '^[<>]' || true
fi

missing=$(comm -13 <(echo "$after") <(grep -v '^yt-dlp==' <<<"$want"))
if [ -n "$missing" ]; then
  echo "venv still differs from $image:" >&2
  echo "$missing" >&2
  exit 1
fi
