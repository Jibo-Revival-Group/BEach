#!/bin/sh
# Downloads and installs the newest os package BEaker has, whatever this robot is on now.
# Usage (on Jibo, as root):  sh update-os.sh
#
# It asks BEaker as if the robot were on 0.0.0, so the newest os is always offered (stock os rows
# are filter "oobe"; any normal os release would be "fcs"), checks the SHA-1, and installs it with
# Jibo's own updater, which reboots a few times by itself.
# An os install reformats the root partition: edits made there by PM (CA certificates, region
# config, boot hooks) are replaced by the package's own files.

CREDS="${CREDS:-/var/jibo/credentials.json}"
OTA_DIR="${OTA_DIR:-/opt/ota}"
QUEUE="${QUEUE:-/var/jibo/ota.json}"

if [ ! -f "$CREDS" ]; then
    echo "ERROR: credentials missing at $CREDS" >&2
    exit 1
fi

# Keep the offer with the highest toVersion across the filters.
best="" best_ver=""
for filter in oobe fcs; do
    offer=$(jibo-get-update --credentials "$CREDS" --subsystem os --version 0.0.0 --filter "$filter" 2>/dev/null)
    ver=$(echo "$offer" | sed -n 's/.*"toVersion" *: *"\([^"]*\)".*/\1/p')
    [ -n "$ver" ] || continue
    if [ -z "$best_ver" ] || [ "$(printf '%s\n%s\n' "$best_ver" "$ver" | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1)" = "$ver" ]; then
        best="$offer" best_ver="$ver" best_filter="$filter"
    fi
done
url=$(echo "$best" | sed -n 's/.*"url" *: *"\([^"]*\)".*/\1/p')
sha=$(echo "$best" | sed -n 's/.*"shaHash" *: *"\([0-9a-f]*\)".*/\1/p')
if [ -z "$url" ] || [ -z "$sha" ]; then
    echo "ERROR: BEaker did not offer an os package." >&2
    exit 1
fi
echo "Newest os: $best_ver"

# A stuck or half-finished update would block this one: set it aside.
if [ -f "$QUEUE" ]; then
    echo "Setting aside an unfinished update ($QUEUE.old)."
    mv -f "$QUEUE" "$QUEUE.old"
fi
for d in /proc/[0-9]*; do
    if tr '\0' ' ' < "$d/cmdline" 2>/dev/null | grep -q '^[^ ]*jibo-apply-update'; then kill "${d#/proc/}" 2>/dev/null; fi
done

mkdir -p "$OTA_DIR"
pkg="$OTA_DIR/os-$best_ver.tar"
tries=0
until [ -f "$pkg" ] && [ "$(sha1sum "$pkg" | cut -d' ' -f1)" = "$sha" ]; do
    tries=$((tries + 1))
    if [ "$tries" -gt 5 ]; then
        echo "ERROR: the os download kept failing its checksum. Check Jibo's Wi-Fi and space on /opt, then run this again." >&2
        exit 1
    fi
    rm -f "$pkg" "$pkg.part"
    echo "Downloading os $best_ver (try $tries of 5)..."
    wget "$url" -O "$pkg.part" && mv -f "$pkg.part" "$pkg"
done

from=$(jibo-version 2>/dev/null | sed -n 's/^Jibo Release Version: Release-\([0-9][0-9.]*\).*/\1/p')
echo "Installing os $best_ver with Jibo's own updater. He will reboot a few times on his own."
DISPLAY=:0 XAUTHORITY=/tmp/.Xauthority jibo-apply-update \
    --source "$pkg" --subsystem os --from "${from:-0.0.0}" --to "$best_ver" --destination / --filter "$best_filter"
