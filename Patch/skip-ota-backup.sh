#!/bin/sh
set -e
clear

echo "=========================================="
echo "  Skip SSM pre-OTA backup"
echo "=========================================="
echo ""
echo "Patches jibo-ssm so POST /ota-update does not"
echo "run systemManager.backup() before download."
echo "Safe to re-run (idempotent)."
echo ""

echo "=== Remount filesystem read-write ==="
jibo-mount --rw
echo "Done."
echo ""

echo "=== Patch SSM ==="
python << 'PY'
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
import os, sys

# Live SSM is under /usr/local/bin/jibo-ssm (see service args), not Skills.
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
                with atomic(path) as f:
                    f.write(text)
            except Exception as e:
                sys.stderr.write('WARNING: could not write ' + path + ': ' + str(e) + '\n')
                continue
            patched += 1
            print('patched ' + path)

if patched == 0 and already == 0:
    sys.stderr.write(
        'ERROR: no SSM _backupIfOTA site found.\n'
        '  looked in: %s\n' % (', '.join(searched) if searched else '(none exist)')
    )
    sys.exit(1)
print('SSM backup skip: patched %d, already %d' % (patched, already))
PY
echo ""

echo "=== Restart SSM so the patch takes effect ==="
kill -9 $(pgrep -f jibo-ssm) || true
echo "Done."
echo ""
echo "Settings / sleep /ota-update will skip the cloud backup stage."
echo "Watchdog will bring SSM back up in a few seconds."
