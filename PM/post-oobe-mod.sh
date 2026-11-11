#!/bin/sh
set -e
if [ -t 1 ]; then clear || true; fi

CREDS_FILE="/var/jibo/credentials.json"
OTA_ENDPOINT="http://joap.5x1.com:80"

echo "=========================================="
echo "  5x1 post-mod bring-up"
echo "  (For OOBE robots only)"
echo "=========================================="
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 1: Remount filesystem read-write ==="
# OOBE images have no jibo-mount; remount what it would (/usr/local is read-only on the 13.0.0 os).
mount -o remount,rw /
mount -o remount,rw /usr/local
echo "Done."
echo ""

# OOBE setup belongs to this downloaded PM script, never to BEetle.
# Keep factory files and existing keys; create absent credentials locally.
python <<'PY'
from __future__ import print_function
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

try:
    string_types = (basestring,)
except NameError:
    string_types = (str,)


def prepare_oobe(root="", call=None):
    def path(p):
        return root + p
    def load(p):
        with open(path(p)) as handle:
            return json.load(handle)
    def save(p, data, mode=0o600):
        target = path(p)
        fd, tmp = tempfile.mkstemp(prefix=".pm-oobe-", dir=os.path.dirname(target))
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, mode)
            os.rename(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    robot = load("/var/jibo/identity.json")
    if not all(isinstance(robot.get(k), string_types) and robot[k]
               for k in ("name", "serial_number", "cpuid", "wifi_mac")):
        raise RuntimeError("Factory identity is incomplete; no OOBE changes were made.")
    mode = load("/var/jibo/mode.json").get("mode")
    state_path = "/var/jibo/5x1-pm-oobe-state.json"
    resumed = (os.path.isfile(path(state_path)) and load(state_path).get("robot") == robot["name"])
    if mode not in ("oobe", "identified", "int-developer") and not (mode == "normal" and resumed):
        raise RuntimeError("Use this PM script only for OOBE robots prepared in int-developer mode.")
    factory = ("/var/jibo/identity.json", "/var/jibo/lps/CameraModelParamsL.json",
               "/var/jibo/lps/CameraModelParamsR.json", "/var/jibo/lps/InterCameraTransform.json")
    evidence = {}
    for p in factory:
        with open(path(p), "rb") as handle:
            raw = handle.read()
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict) or not parsed:
            raise RuntimeError("Factory calibration is incomplete: " + p)
        evidence[p] = hashlib.sha256(raw).hexdigest()
    backup = "/var/jibo/pm-oobe-before-5x1"
    if not os.path.isdir(path(backup)):
        os.makedirs(path(backup), 0o700)
    originals = factory + ("/var/jibo/mode.json", "/var/jibo/credentials.json",
                           "/usr/local/etc/jibo-jetstream-service.json")
    for p in originals:
        if os.path.isfile(path(p)):
            destination = path(backup) + p
            if not os.path.isdir(os.path.dirname(destination)):
                os.makedirs(os.path.dirname(destination), 0o700)
            if not os.path.exists(destination):
                shutil.copy2(path(p), destination)
    save(state_path, {"schema": 1, "robot": robot["name"], "factory_sha256": evidence})
    receipt = path("/var/jibo/5x1-pm-oobe-complete.json")
    if os.path.exists(receipt):
        os.unlink(receipt)

    creds_path = "/var/jibo/credentials.json"
    if not os.path.isfile(path(creds_path)):
        import uuid
        # Match the runtime bootstrap: local values satisfy robot-side clients.
        # Publish without replacing a concurrent initializer's credentials.
        fd, temporary = tempfile.mkstemp(prefix=".pm-local-credentials-", dir=path("/var/jibo"))
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"accessKeyId": "local-" + uuid.uuid4().hex,
                           "secretAccessKey": uuid.uuid4().hex + uuid.uuid4().hex,
                           "region": "api", "endpoint": "http://joap.5x1.com:80"}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, path(creds_path))
            except OSError as error:
                import errno
                if error.errno != errno.EEXIST:
                    raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    creds = load(creds_path)
    print("Using this robot's local credential keys; no cloud provisioning required.")
    if not isinstance(creds, dict) or not all(isinstance(creds.get(k), string_types) and creds[k].strip()
                                            for k in ("accessKeyId", "secretAccessKey")):
        raise RuntimeError("Robot credentials are incomplete; refusing to overwrite them.")
    creds.update(region="api", endpoint="http://joap.5x1.com:80")
    save(creds_path, creds)
    print("OOBE credentials configured; factory identity and calibration retained.")


if __name__ == "__main__":
    try:
        prepare_oobe()
    except Exception as error:
        import sys
        sys.stderr.write("OOBE preparation failed: " + str(error) + "\n")
        sys.exit(1)
PY

# ---------------------------------------------------------------------------
echo "=== Step 2: Finish OOBE in normal mode ==="
jibo-setmode normal
echo "Done."
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 3: Flash BBFW ==="
pkill -f "jibo" || true
echo ""
echo "Flashing BBFW... You may see a lot of random symbols"
echo ""
sleep 2 # wait for jibo to die
jibo-bbfw-update
echo "Done."
echo ""

# Confirm the factory data survived, then leave a receipt for interrupted SSH.
python <<'PY'
from __future__ import print_function
import hashlib
import json
import os
import tempfile


def finish_oobe(root=""):
    def load(p):
        with open(root + p) as handle:
            return json.load(handle)
    state = load("/var/jibo/5x1-pm-oobe-state.json")
    for p, expected in state["factory_sha256"].items():
        with open(root + p, "rb") as handle:
            actual = hashlib.sha256(handle.read()).hexdigest()
        if actual != expected:
            raise RuntimeError("Factory identity/calibration changed: " + p)
    if load("/var/jibo/mode.json").get("mode") != "normal":
        raise RuntimeError("OOBE completion did not persist normal mode.")
    creds = load("/var/jibo/credentials.json")
    if not creds.get("accessKeyId") or not creds.get("secretAccessKey") or creds.get("endpoint") != "http://joap.5x1.com:80":
        raise RuntimeError("OOBE credentials are not configured.")
    target = root + "/var/jibo/5x1-pm-oobe-complete.json"
    fd, tmp = tempfile.mkstemp(prefix=".pm-complete-", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"schema": 1, "status": "complete", "robot": state["robot"]}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print("5X1_OOBE_PM_COMPLETE")


if __name__ == "__main__":
    try:
        finish_oobe()
    except Exception as error:
        import sys
        sys.stderr.write("OOBE verification failed: " + str(error) + "\n")
        sys.exit(1)
PY

# Be (uid 2000) needs /opt/tmp; a root shell's own "mkdir -p /opt/tmp" makes it root-only (700).
mkdir -p /opt/tmp
chmod 777 /opt/tmp

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
