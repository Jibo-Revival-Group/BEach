"""etc/update-os.sh with fake Jibo updater tools: always takes the newest os offered and installs it."""
import hashlib
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'etc', 'update-os.sh')
PKG = b'fake os package'


def offer(ver, sha):
    return '{"url": "http://beaker/packages/x/os-%s.tar", "shaHash": "%s", "toVersion": "%s"}' % (ver, sha, ver)


def run(tmp_path, offers, bad_downloads=0):
    bin_ = tmp_path / 'bin'
    bin_.mkdir()
    sha = hashlib.sha1(PKG).hexdigest()
    cases = ''.join('  *"--filter %s"*) echo \'%s\';;\n' % (f, offer(v, sha)) for f, v in offers.items())
    tools = {
        # Answers per filter; records the --version it was asked with.
        'jibo-get-update': '#!/bin/sh\necho "$*" >> %s/asked\ncase "$*" in\n%s  *) echo UPDATE_NOT_FOUND;;\nesac\n' % (tmp_path, cases),
        # The first N downloads are damaged.
        'wget': '#!/bin/sh\nn=$(cat %s/wgets 2>/dev/null || echo 0); echo $((n + 1)) > %s/wgets\n'
                'if [ "$n" -lt %d ]; then printf broken > "$3"; else printf "%s" > "$3"; fi\n' % (tmp_path, tmp_path, bad_downloads, PKG.decode()),
        'jibo-apply-update': '#!/bin/sh\necho "$*" > %s/applied\n' % tmp_path,
        # Jibo's sort is BusyBox's; check the version comparison with it.
        'sort': '#!/bin/sh\nexec %s sort "$@"\n' % (shutil.which('busybox') or 'sort'),
        'jibo-version': '#!/bin/sh\necho "Jibo Release Version: Release-13.0.0-20190225"\n',
    }
    for name, body in tools.items():
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    (tmp_path / 'creds.json').write_text('{}')
    (tmp_path / 'ota.json').write_text('{"stuck": true}')
    env = dict(os.environ, PATH='%s:%s' % (bin_, os.environ['PATH']), CREDS=str(tmp_path / 'creds.json'),
               OTA_DIR=str(tmp_path / 'ota'), QUEUE=str(tmp_path / 'ota.json'))
    return subprocess.run(['sh', SCRIPT], env=env, capture_output=True, text=True)


def test_installs_the_newest_os_whatever_the_robot_is_on(tmp_path):
    r = run(tmp_path, {'oobe': '13.0.10', 'fcs': '13.0.9'}, bad_downloads=2)
    assert r.returncode == 0, r.stdout + r.stderr
    applied = (tmp_path / 'applied').read_text()
    assert '--subsystem os --from 13.0.0 --to 13.0.10 --destination / --filter oobe' in applied  # 13.0.10 > 13.0.9
    assert (tmp_path / 'ota' / 'os-13.0.10.tar').read_bytes() == PKG  # retried until the checksum matched
    assert '--version 0.0.0' in (tmp_path / 'asked').read_text()  # same version is still offered
    assert not (tmp_path / 'ota.json').exists() and (tmp_path / 'ota.json.old').exists()  # stuck queue set aside


def test_stock_os_only(tmp_path):
    r = run(tmp_path, {'oobe': '13.0.0'})
    assert r.returncode == 0, r.stdout + r.stderr
    assert '--to 13.0.0 --destination / --filter oobe' in (tmp_path / 'applied').read_text()


def test_no_offer_is_an_error(tmp_path):
    r = run(tmp_path, {})
    assert r.returncode == 1 and 'did not offer' in r.stderr
    assert not (tmp_path / 'applied').exists()


def test_gives_up_after_five_bad_downloads(tmp_path):
    r = run(tmp_path, {'oobe': '13.0.0'}, bad_downloads=9)
    assert r.returncode == 1 and 'kept failing' in r.stderr
    assert not (tmp_path / 'applied').exists()
