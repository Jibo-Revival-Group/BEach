#!/bin/sh
set -e
clear

CONFIG_FILE="/usr/local/etc/jibo-jetstream-service.json"
CREDS_FILE="/var/jibo/credentials.json"
OTA_ENDPOINT="https://api.5x1.com"

echo "--- Jibo BEefy server configurator ---"
echo "Hub: api.5x1.com (BEefy, port 443 in front of the server)"
echo "OTA/loop endpoint: $OTA_ENDPOINT (BEefy, updates forwarded to joap)"
echo "------------------------------------------"

# 1. Remount filesystem
echo "Remounting as Read-Write..."
jibo-mount --rw

# 2. Get User Input
read -p "Enter Server IP or Domain (e.g., api.5x1.com): " HUB_HOST
read -p "Enter Port (e.g., 443): " HUB_PORT

# 3. Update the JSON file using Python
# We use Python to inject the specific values into the template
echo "Updating $CONFIG_FILE..."

python -c "
import json
import sys

host = '$HUB_HOST'
port = int('$HUB_PORT')

with open('$CONFIG_FILE', 'r') as f:
    data = json.load(f)

# Update the override section
data['HubClient']['override'] = {
    'hub_port': port,
    'hub_hostname': host,
    'entrypoint_hostname': host
}

with open('$CONFIG_FILE', 'w') as f:
    json.dump(data, f, indent=4)
"

# Jetstream needs to be public
chmod 777 /usr/local/etc/jibo-jetstream-service.json

# 4. Point OTA/loop credentials at JOAP (keys preserved)
echo "Pointing OTA/loop at $OTA_ENDPOINT..."
if [ ! -f "$CREDS_FILE" ]; then
    echo "WARNING: $CREDS_FILE missing — skipping OTA endpoint (create credentials first)."
else
    python -c "
import json, sys

path = '$CREDS_FILE'
endpoint = '$OTA_ENDPOINT'

with open(path, 'r') as f:
    data = json.load(f)

if not data.get('accessKeyId') or not data.get('secretAccessKey'):
    sys.stderr.write('WARNING: credentials missing keys; refusing to rewrite\n')
    sys.exit(0)

next = {
    'secretAccessKey': data['secretAccessKey'],
    'region': 'api',
    'endpoint': endpoint,
    'accessKeyId': data['accessKeyId']
}

with open(path, 'w') as f:
    json.dump(next, f)
    f.write('\n')
print('endpoint -> ' + endpoint)
"
fi

echo "Pointing region config at api.5x1.com..."
python << 'PY'
import json, os
paths = [
    "/usr/local/bin/jibo-ssm/node_modules/@jibo/jibo-server-client/lib/region_config.json",
    "/usr/lib/node_modules/@jibo/jibo-server-client/lib/region_config.json",
]
pattern = {
    "endpoint": "https://api.5x1.com",
    "wsendpoint": "wss://api.5x1.com",
    "globalEndpoint": True,
}
for path in paths:
    if not os.path.isfile(path):
        continue
    with open(path, "r") as handle:
        data = json.load(handle)
    data.setdefault("patterns", {})["globalSSL"] = pattern
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    print("region_config -> api.5x1.com " + path)
PY

# 5. Skip SSM pre-OTA cloud backup so /ota-update is not gated by JOAP backup
echo "Patching SSM to skip pre-OTA backup..."
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

# 6. Finalize
echo "Configuration complete."
echo "Restarting jetstream service to apply changes..."
kill -9 $(pgrep -f jibo-jetstream-service) || true
echo "Restarting SSM so OTA/loop credentials and backup skip take effect..."
kill -9 $(pgrep -f jibo-ssm) || true

echo "Done! Jibo is now pointing to $HUB_HOST:$HUB_PORT"
echo "OTA/loop endpoint: $OTA_ENDPOINT"
