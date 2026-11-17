#!/bin/sh
# Prove Council seat isolation in a real container (src/seat_user.py,
# THREAT_MODEL.md): build the image, start a throwaway container on a
# temporary data folder, and run scripts/seat_isolation_probe.py inside it as
# Odysseus's own user, the app running as PID 1 next to it.
#
#   scripts/check_seat_isolation.sh                  build, check, clean up
#   SKIP_BUILD=1 scripts/check_seat_isolation.sh     reuse the built image
#   KEEP=1 scripts/check_seat_isolation.sh           leave the container running
#
# IMAGE (odysseus-seat-isolation-test) and NAME (seatiso-check-<pid>) pick the
# image tag and the container name. No port is published and nothing but the
# temporary folder is mounted. Exits non-zero when a check fails.
set -eu

cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-odysseus-seat-isolation-test}"
NAME="${NAME:-seatiso-check-$$}"

if [ -z "${SKIP_BUILD:-}" ]; then
    docker build -t "$IMAGE" .
fi

tmp="$(mktemp -d "${TMPDIR:-/tmp}/seatiso.XXXXXX")"
mkdir -p "$tmp/data" "$tmp/logs"
cleanup() {
    if [ -z "${KEEP:-}" ]; then
        docker rm -f "$NAME" >/dev/null 2>&1 || true
        rm -rf "$tmp" 2>/dev/null || echo "left $tmp behind (files the container wrote)" >&2
    fi
}
trap cleanup EXIT INT TERM

# The data and logs folders are bind mounts, as in docker-compose.yml.
# /council-runs stays the image's own folder here, a real Linux filesystem as
# the compose volume is. NET_ADMIN as in docker-compose.yml.
# Without NET_ADMIN the entrypoint cannot filter the seats' network: the app
# must notice, so that work runs do not start.
echo "Without the NET_ADMIN capability:"
docker run --rm "$IMAGE" python -c "from src import seat_user; why = seat_user.unfiltered(); print('  unfiltered:', why); raise SystemExit(0 if why else 1)"

docker run -d --name "$NAME" --cap-add NET_ADMIN \
    -v "$tmp/data:/app/data" -v "$tmp/logs:/app/logs" \
    -e AUTH_ENABLED=true -e LOCALHOST_BYPASS=false \
    "$IMAGE" >/dev/null

echo "Waiting for Odysseus to start in $NAME ..."
i=0
until docker exec "$NAME" python -c "import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://127.0.0.1:7000/', timeout=2)
except urllib.error.HTTPError:
    pass" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 90 ]; then
        docker logs --tail 40 "$NAME" >&2
        echo "Odysseus did not start." >&2
        exit 1
    fi
    sleep 2
done

echo "The seats' network rules:"
docker exec "$NAME" iptables -S ODYSSEUS_SEATS
docker exec "$NAME" iptables -S OUTPUT
docker exec -i -w /app "$NAME" gosu odysseus python - < scripts/seat_isolation_probe.py
