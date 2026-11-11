#!/bin/sh
# Dedicated PM for mod.5x1.com; the standard PM entry point stays separate.
set -e
if [ -t 1 ]; then clear || true; fi

CREDS_FILE="/var/jibo/credentials.json"
OTA_ENDPOINT="http://joap.5x1.com:80"

# An OOBE robot needs the OOBE PM: its state file (an earlier OOBE run), an OOBE mode, or (it is usually
# int-developer) no @be skill and no Knowledge Base yet. Not jibo-mount: installing services/BEnch adds it.
if [ -f /var/jibo/5x1-pm-oobe-state.json ] || grep -qE '"mode" *: *"(oobe|identified)"' /var/jibo/mode.json 2>/dev/null \
        || { [ ! -d /opt/jibo/Jibo/Skills/@be ] && [ -z "$(ls -A /opt/jibo/Knowledge 2>/dev/null)" ]; }; then
    echo "This Jibo is running OOBE: running the OOBE PM instead."
    curl -fsSL -k https://raw.githubusercontent.com/Jibo-Revival-Group/BEach/master/PM/web-post-oobe-mod.sh -o /tmp/5x1-oobe-pm.sh
    exec sh /tmp/5x1-oobe-pm.sh
fi

echo "=========================================="
echo "  5x1 post-mod bring-up"
echo "=========================================="
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 1: Remount filesystem read-write ==="
jibo-mount --rw
echo "Done."
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 2: Point OTA at $OTA_ENDPOINT (BEaker) ==="
if [ ! -f "$CREDS_FILE" ]; then
    echo "ERROR: $CREDS_FILE is missing; this Jibo was never set up." >&2
    exit 1
else
    echo "Updating endpoint in $CREDS_FILE (keys preserved)..."
    python -c "
import contextlib, os, tempfile
@contextlib.contextmanager
def atomic(path):
    # Temp file + fsync + rename: a failed write never leaves a truncated original.
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or '.')
    try:
        with os.fdopen(fd, 'w') as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.rename(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
import json, sys

path = '$CREDS_FILE'
endpoint = '$OTA_ENDPOINT'

with open(path, 'r') as f:
    data = json.load(f)

if not data.get('accessKeyId') or not data.get('secretAccessKey'):
    sys.stderr.write('ERROR: credentials missing keys; refusing to rewrite\n')
    sys.exit(1)

next = dict(data)
next.update(region='api', endpoint=endpoint)

with atomic(path) as f:
    json.dump(next, f)
    f.write('\n')
print('endpoint -> ' + endpoint)
"
fi
echo ""


# ---------------------------------------------------------------------------
echo "=== Step 3: Set mode to normal ==="
jibo-setmode normal
echo "Done."
echo ""

# Be (uid 2000) needs /opt/tmp; a root shell's own "mkdir -p /opt/tmp" makes it root-only (700).
mkdir -p /opt/tmp
chmod 777 /opt/tmp

# The WebUSB site installs only this one-time launcher, never PM itself.
# SSH/manual PM retains its existing behavior when this flag is absent.
if [ "${MOD_5X1_BOOT:-}" = "1" ]; then
    /usr/bin/python /var/jibo/5x1-mod/boot.py finish
fi

# ---------------------------------------------------------------------------
# >>> 5x1 OTA install (the same in every PM script)
echo "=== Step 4: Install 5x1 with Jibo's own updater ==="
# Jibo's own updater installs everything from BEaker (joap.5x1.com): the modded os, services
# (BEnch) and skills (BEam), in that order, rebooting by himself between them.
OTA_DIR="/opt/ota"
QUEUE="/var/jibo/ota.json"
SKILLS="/opt/jibo/Jibo/Skills"
PM_ITEMS="os:/ services:/usr/local @be/be:$SKILLS/@be/be jibo-tbd:$SKILLS/jibo-tbd oobe-config:$SKILLS/oobe-config jibo-diagnostics:$SKILLS/jibo-diagnostics fin-goods-test:$SKILLS/fin-goods-test"
PM_LIST="$OTA_DIR/pm-packages.txt"

installed_version() {
    case "$1" in
        os) jibo-version 2>/dev/null | sed -n 's/^Jibo Release Version: Release-\([0-9][0-9.]*\).*/\1/p' ;;
        services) jibo-service-version 2>/dev/null | sed -n 's/.*Release-\([0-9][0-9.]*\).*/\1/p' ;;
        *) sed -n 's/^ *"version" *: *"\([^"]*\)".*/\1/p' "$SKILLS/$1/package.json" 2>/dev/null | head -n 1 ;;
    esac
}

mkdir -p "$OTA_DIR"
: > "$PM_LIST"
need_kb=0
largest_kb=0
for item in $PM_ITEMS; do
    subsystem=${item%%:*}
    destination=${item#*:}
    # Asking as version 0.0.0 always gets the newest full package, even if Jibo already has it.
    offer=$(jibo-get-update --credentials "$CREDS_FILE" --subsystem "$subsystem" --version 0.0.0 --filter fcs 2>/dev/null) || true
    url=$(echo "$offer" | sed -n 's/.*"url" *: *"\([^"]*\)".*/\1/p')
    sha=$(echo "$offer" | sed -n 's/.*"shaHash" *: *"\([0-9a-f]*\)".*/\1/p')
    to=$(echo "$offer" | sed -n 's/.*"toVersion" *: *"\([^"]*\)".*/\1/p')
    length=$(echo "$offer" | sed -n 's/.*"length" *: *\([0-9][0-9]*\).*/\1/p')
    if [ -z "$url" ] || [ -z "$sha" ] || [ -z "$to" ] || [ -z "$length" ]; then
        echo "ERROR: BEaker (joap.5x1.com) did not offer $subsystem. Check Jibo's Wi-Fi and run PM again." >&2
        exit 1
    fi
    pkg="$OTA_DIR/pm-$(echo "$subsystem" | tr / -).tar"
    from=$(installed_version "$subsystem")
    echo "$subsystem $to $sha $url $pkg $destination ${from:-0.0.0}" >> "$PM_LIST"
    kb=$((length / 1024 + 1))
    [ -f "$pkg" ] || need_kb=$((need_kb + kb))
    [ "$kb" -le "$largest_kb" ] || largest_kb=$kb
    echo "  $subsystem $to"
done

# The downloads stay on /opt, and the updater needs room to unpack the largest one.
free_kb=$(df -k "$OTA_DIR" | awk 'END { print $(NF - 2) }')
if [ "$free_kb" -lt $((need_kb + largest_kb)) ]; then
    echo "ERROR: Jibo needs $(((need_kb + largest_kb) / 1024)) MB free on /opt for the update and has $((free_kb / 1024)) MB." >&2
    echo "Free some space (for example music in the jukebox) and run PM again." >&2
    exit 1
fi

# A stuck or half-finished update would block this one: set it aside.
if [ -f "$QUEUE" ]; then
    echo "Setting aside an unfinished update ($QUEUE.old)."
    mv -f "$QUEUE" "$QUEUE.old"
fi
for d in /proc/[0-9]*; do
    if tr '\0' ' ' < "$d/cmdline" 2>/dev/null | grep -q '^[^ ]*jibo-apply-update'; then kill "${d#/proc/}" 2>/dev/null || true; fi
done

apply_args=""
while read -r subsystem to sha url pkg destination from; do
    tries=0
    until [ -f "$pkg" ] && [ "$(sha1sum "$pkg" | cut -d' ' -f1)" = "$sha" ]; do
        tries=$((tries + 1))
        if [ "$tries" -gt 5 ]; then
            echo "ERROR: the $subsystem download kept failing its check. Check Jibo's Wi-Fi and run PM again." >&2
            rm -f "$pkg" "$pkg.part"
            exit 1
        fi
        rm -f "$pkg" "$pkg.part"
        # wget writes to .part first; it is renamed only after the whole file arrives.
        echo "Downloading $subsystem $to (try $tries of 5)..."
        if wget "$url" -O "$pkg.part" < /dev/null; then mv -f "$pkg.part" "$pkg"; fi
    done
    apply_args="$apply_args --source $pkg --subsystem $subsystem --from $from --to $to --destination $destination --filter fcs"
done < "$PM_LIST"

echo ""
echo "PM is done. Jibo now installs 5x1 with his own updater: he shows update screens and restarts"
echo "a few times by himself. Leave him plugged in until he is back to his normal self (about 30 minutes)."
set -f
DISPLAY=:0 XAUTHORITY=/tmp/.Xauthority jibo-apply-update $apply_args
set +f
# <<< 5x1 OTA install
