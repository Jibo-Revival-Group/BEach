#!/bin/sh
set -e
if [ -t 1 ]; then clear || true; fi

JUKEBOX_MUSIC="/opt/jibo/Knowledge/jukebox/music"
KNOWLEDGE_BEACON="/opt/jibo/Knowledge/beacon"
CONFIG_FILE="/usr/local/etc/jibo-jetstream-service.json"
HUB_HOST="api.5x1.com"
HUB_PORT="443"
CREDS_FILE="/var/jibo/credentials.json"
OTA_ENDPOINT="http://joap.5x1.com:80"
CA_URL="https://letsencrypt.org/certs/isrgrootx1.pem"
CA="/opt/jibo/openjibo-ca.crt"

echo "=========================================="
echo "  BEam post-mod bring-up"
echo "  (For OOBE robots only)"
echo "=========================================="
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 1: Remount filesystem read-write ==="
jibo-mount --rw
echo "Done."
echo ""

# OOBE setup belongs to this downloaded PM script, never to BEetle.
# Keep factory files and any existing keys; provision only absent credentials.
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

    def request(operation, body):
        fd, request_path = tempfile.mkstemp(prefix=".pm-oobe-request-", dir=path("/var/jibo"))
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(body, handle)
            args = ["curl", "-fsSL", "-k", "--max-time", "60", "-X", "POST",
                    "-H", "Content-Type: application/x-amz-json-1.1",
                    "-H", "X-Amz-Target: OOBE_20161026." + operation,
                    "--data-binary", "@" + request_path, "https://api.5x1.com/"]
            try:
                raw = call(args) if call is not None else subprocess.check_output(args, stderr=subprocess.PIPE)
                value = json.loads(raw)
            except Exception:
                raise RuntimeError("BEefy OOBE provisioning failed; credentials were not replaced.")
            if not isinstance(value, dict):
                raise RuntimeError("BEefy returned an invalid OOBE response.")
            return value
        finally:
            if os.path.exists(request_path):
                os.unlink(request_path)

    creds_path = "/var/jibo/credentials.json"
    if os.path.isfile(path(creds_path)):
        creds = load(creds_path)
        print("Preserving this robot's existing credential keys.")
    else:
        print("Requesting credentials for this OOBE robot from BEefy...")
        body = {"robotId": robot["name"], "deviceId": robot["name"], "targetMode": "open-jibo",
                "targetHost": "api.5x1.com", "rollbackSnapshotId": backup}
        prepared = request("PrepareRobot", body)
        token = prepared.get("token")
        readiness = prepared.get("conversionReadiness", {})
        if not isinstance(token, string_types) or not token or readiness.get("canWriteRobot") is False:
            raise RuntimeError("BEefy did not approve prepared OOBE setup.")
        body["token"] = token
        issued = request("SetupRobot", body)
        creds = dict((k, issued.get(k)) for k in ("accessKeyId", "secretAccessKey"))
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
echo "=== Step 2: Remove firewall init script ==="
rm -f /etc/init.d/S21firewall
echo "Done."
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 3: Persist user data, then clear /opt/tmp ==="
# Custom eyes + music live under Knowledge (survives Skills OTA and tmp wipes).
# Copy legacy /opt/tmp data into Knowledge before clearing tmp.
mkdir -p /opt/jibo/Knowledge/jukebox/music /opt/jibo/Knowledge/beacon
chmod 777 /opt/jibo/Knowledge /opt/jibo/Knowledge/jukebox \
  /opt/jibo/Knowledge/jukebox/music /opt/jibo/Knowledge/beacon 2>/dev/null || true
if [ -d /opt/tmp/beacon ] && [ ! -f /opt/jibo/Knowledge/beacon/eye/custom.png ]; then
    echo "Migrating custom eye from /opt/tmp/beacon to Knowledge..."
    mkdir -p /opt/jibo/Knowledge/beacon
    cp -a /opt/tmp/beacon/. /opt/jibo/Knowledge/beacon/ 2>/dev/null || true
fi
if [ -d /opt/tmp/jukebox-music ] && \
   ! find /opt/jibo/Knowledge/jukebox/music -maxdepth 3 -type f \( \
       -iname '*.mp3' -o -iname '*.opus' -o -iname '*.ogg' -o -iname '*.oga' \
     \) 2>/dev/null | head -n 1 | grep -q .
then
    echo "Migrating jukebox music from /opt/tmp/jukebox-music to Knowledge..."
    rm -rf /opt/jibo/Knowledge/jukebox/music
    mv /opt/tmp/jukebox-music /opt/jibo/Knowledge/jukebox/music
fi
rm -rf /opt/tmp/
mkdir -p /opt/tmp/beacon
chmod 777 /opt/tmp /opt/tmp/beacon
echo "Done."
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 4: Install BEam ==="

echo "Going to skills directory..."
cd /opt/jibo/Jibo/Skills/

echo "Terminate Be via SSM..."
curl -s -X POST http://localhost:8779/terminate \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-raw '{"command":"@be/be"}'

# Durable library under Knowledge — only stash Skills-tree leftovers.
JUKEBOX_MUSIC="/opt/jibo/Knowledge/jukebox/music"
KNOWLEDGE_BEACON="/opt/jibo/Knowledge/beacon"
JUKEBOX_MUSIC_SKILLS="/opt/jibo/Jibo/Skills/@be/Skills/Jukebox/Music"
JUKEBOX_MUSIC_IN_BE="/opt/jibo/Jibo/Skills/@be/be/skills/jukebox/music"
JUKEBOX_MUSIC_LOWER="/opt/jibo/Jibo/Skills/@be/skills/jukebox/music"
JUKEBOX_MUSIC_LEGACY_NESTED="/opt/jibo/Jibo/Skills/@be/be/node_modules/@be/jukebox/music"
JUKEBOX_MUSIC_LEGACY_SIBLING="/opt/jibo/Jibo/Skills/@be/jukebox/music"
JUKEBOX_MUSIC_LEGACY_ROOT="/opt/jibo/Jibo/Skills/skills/jukebox/music"
JUKEBOX_MUSIC_STASH="/opt/tmp/jukebox-music"

jukebox_has_audio() {
    dir="$1"
    [ -d "$dir" ] || return 1
    find "$dir" -maxdepth 3 -type f \( \
        -iname '*.mp3' -o -iname '*.opus' -o -iname '*.ogg' -o -iname '*.oga' \
    \) 2>/dev/null | head -n 1 | grep -q .
}

JUKEBOX_STASH_FROM=""
if jukebox_has_audio "$JUKEBOX_MUSIC"; then
    echo "Jukebox music already in Knowledge ($JUKEBOX_MUSIC); leaving it alone."
else
    for candidate in \
        "$JUKEBOX_MUSIC_SKILLS" \
        "$JUKEBOX_MUSIC_IN_BE" \
        "$JUKEBOX_MUSIC_LOWER" \
        "$JUKEBOX_MUSIC_LEGACY_ROOT" \
        "$JUKEBOX_MUSIC_LEGACY_SIBLING" \
        "$JUKEBOX_MUSIC_LEGACY_NESTED"
    do
        if jukebox_has_audio "$candidate"; then
            JUKEBOX_STASH_FROM="$candidate"
            break
        fi
    done

    if [ -n "$JUKEBOX_STASH_FROM" ]; then
        echo "Stashing jukebox music library from $JUKEBOX_STASH_FROM to $JUKEBOX_MUSIC_STASH..."
        mkdir -p /opt/tmp
        rm -rf "$JUKEBOX_MUSIC_STASH"
        mv "$JUKEBOX_STASH_FROM" "$JUKEBOX_MUSIC_STASH"
    else
        echo "No existing jukebox music library to stash."
    fi
fi

echo "Cleaning up old temporary files..."
rm -rf Beam-master BEam-master master.zip

echo "Preparing backup directory..."
rm -rf old-BEer
mkdir old-BEer

echo "Backing up current skills to old-BEer..."
for dir in */; do
    if [ "$dir" != "old-BEer/" ]; then
        mv "$dir" old-BEer/
    fi
done

echo "Downloading BEam repository..."
wget -q --show-progress https://github.com/Jibo-Revival-Group/Beam/archive/refs/heads/master.zip

echo "Extracting files (this may take a moment)..."
python -c "
import zipfile, sys
z = zipfile.ZipFile('master.zip')
namelist = z.namelist()
total = float(len(namelist))
for i, name in enumerate(namelist):
    z.extract(name)
    percent = ((i + 1) / total) * 100
    sys.stdout.write('\rProgress: %.1f%%' % percent)
    sys.stdout.flush()
print('\nExtraction complete.')
"

echo "Deploying new BEam skills..."
EXTRACT_DIR=""
if [ -d BEam-master ]; then
    EXTRACT_DIR=BEam-master
elif [ -d Beam-master ]; then
    EXTRACT_DIR=Beam-master
else
    echo "ERROR: extracted BEam directory not found after unzip"
    ls -la
    exit 1
fi
# BusyBox ash leaves unmatched globs literal; move items one by one
for item in "$EXTRACT_DIR"/*; do
    [ -e "$item" ] || continue
    mv "$item" .
done
rm -rf "$EXTRACT_DIR" master.zip

if [ -d "$JUKEBOX_MUSIC_STASH" ] && ! jukebox_has_audio "$JUKEBOX_MUSIC"; then
    echo "Restoring jukebox music library to $JUKEBOX_MUSIC..."
    mkdir -p "$(dirname "$JUKEBOX_MUSIC")"
    rm -rf "$JUKEBOX_MUSIC"
    mv "$JUKEBOX_MUSIC_STASH" "$JUKEBOX_MUSIC"
elif jukebox_has_audio "$JUKEBOX_MUSIC"; then
    echo "Jukebox music already in Knowledge; skipping restore."
    rm -rf "$JUKEBOX_MUSIC_STASH" 2>/dev/null || true
else
    echo "No stashed jukebox music library to restore."
fi

echo "Fixing permissions..."
chmod 777 -R /opt/jibo/Jibo/Skills/
chmod +x -R /usr/local/bin
# Jetstream needs to be public
chmod 777 /usr/local/etc/jibo-jetstream-service.json

# Durable user data (music + custom eyes) under Knowledge
mkdir -p "$JUKEBOX_MUSIC" "$KNOWLEDGE_BEACON"
chmod 777 /opt/jibo/Knowledge /opt/jibo/Knowledge/jukebox "$JUKEBOX_MUSIC" "$KNOWLEDGE_BEACON" 2>/dev/null || true
mkdir -p /opt/tmp/beacon
chmod 777 /opt/tmp /opt/tmp/beacon

echo ""
echo "BEam install complete."
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 4.5: Install TLS certificate and patch HTTPS verification ==="
mkdir -p /opt/jibo /etc/ssl/certs
echo "Downloading and installing ISRG Root X1 certificate..."
# Download next to its destination: Jibo's /tmp is a small tmpfs that is often full.
curl -k -fsSL "$CA_URL" -o "$CA.new"
grep -q "BEGIN CERTIFICATE" "$CA.new"
chmod 644 "$CA.new"
mv -f "$CA.new" "$CA"
ln -sf "$CA" /etc/ssl/certs/4042bcee.0
cat "$CA" >> /etc/ssl/certs/ca-certificates.crt

echo "Patching rejectUnauthorized in node modules..."
for f in \
    /opt/jibo/Jibo/Skills/@be/be/node_modules/@jibo/jibo-server-client/lib/http/node.js \
    /opt/jibo/Jibo/Skills/oobe-config/node_modules/@jibo/jibo-server-client/lib/http/node.js \
    /usr/local/bin/jibo-ssm/node_modules/@jibo/jibo-server-client/lib/http/node.js \
    /usr/lib/node_modules/@jibo/jibo-server-client/lib/http/node.js
do
    if [ -f "$f" ]; then
        if python - "$f" <<'PY'
import sys

path = sys.argv[1]
with open(path, 'r+') as f:
    text = f.read()
    patched = text.replace(
        'rejectUnauthorized: true',
        'rejectUnauthorized: false'
    )
    if patched == text:
        if 'rejectUnauthorized: false' in text:
            raise SystemExit(0)
        raise SystemExit('target text not found')
    f.seek(0)
    f.write(patched)
    f.truncate()
PY
        then
            echo "Patched: $f"
        else
            echo "ERROR: could not patch $f." >&2
            exit 1
        fi
    fi
done
echo ""

# ---------------------------------------------------------------------------
# Classic HTTP and notification sockets also need the 5x1 global pattern.
python <<'PYREGION'
import json, os
paths = [
    "/usr/local/bin/jibo-ssm/node_modules/@jibo/jibo-server-client/lib/region_config.json",
    "/usr/lib/node_modules/@jibo/jibo-server-client/lib/region_config.json",
    "/opt/jibo/Jibo/Skills/@be/be/node_modules/@jibo/jibo-server-client/lib/region_config.json",
    "/opt/jibo/Jibo/Skills/oobe-config/node_modules/@jibo/jibo-server-client/lib/region_config.json",
]
for path in paths:
    if not os.path.isfile(path):
        continue
    with open(path) as handle:
        data = json.load(handle)
    data.setdefault("patterns", {})["globalSSL"] = {
        "endpoint": "https://api.5x1.com", "wsendpoint": "wss://api.5x1.com", "globalEndpoint": True,
    }
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
PYREGION

echo "=== Step 5: Point jetstream at $HUB_HOST:$HUB_PORT ==="
echo "Updating $CONFIG_FILE..."

python -c "
import json

host = '$HUB_HOST'
port = int('$HUB_PORT')

with open('$CONFIG_FILE', 'r') as f:
    data = json.load(f)

data['HubClient']['override'] = {
    'hub_port': port,
    'hub_hostname': host,
    'entrypoint_hostname': host
}

with open('$CONFIG_FILE', 'w') as f:
    json.dump(data, f, indent=4)
"

echo "Restarting jetstream service to apply changes..."
kill -9 $(pgrep -f jibo-jetstream-service) || true

echo "Jetstream now pointing to $HUB_HOST:$HUB_PORT"
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 6: OTA credentials were configured during OOBE preparation ==="
echo "Endpoint: $OTA_ENDPOINT (credential keys preserved)"
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 7: Skip SSM pre-OTA backup (so /ota-update is not gated) ==="
python << 'PY'
import os, sys

roots = [
    '/usr/local/bin/jibo-ssm',
    '/opt/jibo/Jibo/jibo-ssm',
]
needle = 'this._backupHelper((bkError, maxError) => {'
marker = "this._doLog('Skipping pre-OTA backup');"
replacement = (
    marker + '\n'
    '            return callback();\n'
    '            ' + needle
)
skip_dirs = set(['node_modules'])
patched = 0
already = 0
searched = []

for root in roots:
    if not os.path.isdir(root):
        continue
    searched.append(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            if not name.endswith('.js'):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, 'r') as f:
                    text = f.read()
            except Exception:
                continue
            if needle not in text:
                continue
            if marker in text:
                already += 1
                print('already patched: ' + path)
                continue
            text = text.replace(needle, replacement)
            try:
                with open(path, 'w') as f:
                    f.write(text)
            except Exception as e:
                sys.stderr.write('WARNING: could not write ' + path + ': ' + str(e) + '\n')
                continue
            patched += 1
            print('patched ' + path)

if patched == 0 and already == 0:
    sys.stderr.write(
        'WARNING: no SSM _backupIfOTA site found.\n'
        '  looked in: %s\n' % (', '.join(searched) if searched else '(none exist)')
    )
else:
    print('SSM backup skip: patched %d, already %d' % (patched, already))
PY
echo ""
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 8: Finish OOBE in normal mode ==="
jibo-setmode normal
echo "Done."
echo ""

# ---------------------------------------------------------------------------
echo "=== Step 9: Flash BBFW ==="
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
    override = load("/usr/local/etc/jibo-jetstream-service.json")["HubClient"]["override"]
    if override != {"hub_port": 443, "hub_hostname": "api.5x1.com", "entrypoint_hostname": "api.5x1.com"}:
        raise RuntimeError("BEefy hub configuration did not persist.")
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

# ---------------------------------------------------------------------------
echo "=== Step 10: Reboot ==="
echo "Mode change requires a reboot. Rebooting now..."
echo ""
echo ""
echo ""
echo "Wait like 2 minutes while the robot reboots... I'm done here."
reboot
