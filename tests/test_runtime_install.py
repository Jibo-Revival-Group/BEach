"""PM installation on disposable robot trees; never stops host services."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import zipfile

import pytest

PM = Path(__file__).resolve().parents[1] / 'PM'
SPEC = importlib.util.spec_from_file_location('runtime_install', PM / 'install-runtime.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
VARIANTS = ('post-mod.sh', 'post-oobe-mod.sh', 'web-post-mod.sh', 'web-post-oobe-mod.sh')


def write(root, name, data):
    target = root / name.lstrip('/')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data if isinstance(data, bytes) else data.encode())
    return target


def make_zip(filename, prefix, files):
    with zipfile.ZipFile(filename, 'w') as archive:
        for name, value in files.items():
            info = zipfile.ZipInfo(prefix + '/' + name)
            info.create_system = 3
            if isinstance(value, tuple):
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                value = value[0]
            else:
                info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, value)
    return filename


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / 'robot'
    root.mkdir()
    (root / 'tmp').mkdir()
    identity = {'name': 'Fixture-Robot', 'serial_number': 'fixture', 'cpuid': 'fixture', 'wifi_mac': 'fixture'}
    write(root, '/var/jibo/identity.json', json.dumps(identity))
    write(root, '/var/jibo/mode.json', '{"mode":"int-developer"}')
    for name in ('CameraModelParamsL.json', 'CameraModelParamsR.json', 'InterCameraTransform.json'):
        write(root, '/var/jibo/lps/' + name, '{"factory":"preserve"}')
    write(root, '/opt/jibo/Jibo/Skills/@be/be/index.js', 'old skill')
    write(root, '/usr/local/bin/jibo-system-manager', 'old system manager')
    write(root, '/opt/tmp/beacon/eye/custom.png', 'custom eye')
    write(root, '/opt/tmp/jukebox-music/Album/track.mp3', 'music')
    beam = {
        '@be/be/package.json': '{"name":"@be/be","version":"13.0.7"}',
        '@be/be/index.js': 'new skill',
        '@be/be/index.html': '<html>fixture</html>',
        '@be/be/runtime-state.js': 'runtime helper',
        '@be/be/tests/ignore.js': 'test',
        'tests/ignore.py': 'test',
        'README.md': 'docs',
    }
    bench = {
        'usr/local/bin/jibo-system-manager': b'\x7fELFfixture-manager',
        'usr/local/bin/jibo-service-version': b'\x7fELFfixture-version-do-not-rewrite',
        'usr/local/bin/jibo-runtime-bootstrap': '#!/usr/bin/node\n',
        'usr/local/bin/jibo-service-start': '#!/bin/sh\n',
        'usr/local/lib/jibo/runtime-state.js': 'runtime helper',
        'usr/local/lib/jibo/knowledge-bootstrap.js': 'kb helper',
        'usr/local/etc/jibo-system-manager.json': '{}',
        'usr/local/etc/jibo-jetstream-service.json': '{"HubClient":{}}',
        'usr/local/bin/jibo-ssm/lib/skills-service-manager.js': "this._backupHelper((bkError, maxError) => {",
        'usr/local/bin/jibo-ssm/lib/ignored.js.openjibo-orig': 'old backup',
        'usr/local/lib/libfixture.so': b'fixture library',
        'usr/local/lib/libfixture-current.so': ('libfixture.so',),
        'usr/local/share/tests/ignore': 'test',
        'tests/ignore': 'test',
        'README.md': 'docs',
    }
    archives = (make_zip(tmp_path / 'beam.zip', 'BEam-master', beam),
                make_zip(tmp_path / 'bench.zip', 'BEnch-master', bench))
    return root, tmp_path, archives, beam, bench


def install_fixture(fixture, **kwargs):
    root, stage, archives, _, _ = fixture
    return installer.install(str(archives[0]), str(archives[1]), str(stage), root=str(root),
                             stopper=kwargs.pop('stopper', lambda root: []),
                             bootstrap=kwargs.pop('bootstrap', lambda root: None), **kwargs)


def test_install_both_packages_preserving_user_factory_and_binary_data(fixture):
    root, stage, archives, beam, bench = fixture
    evidence = installer.factory_evidence(str(root))
    events = []
    def stop(root):
        events.append('stop')
    def bootstrap(value):
        assert value == str(root)
        assert (root / 'opt/jibo/Jibo/Skills/@be/be/index.js').read_text() == 'new skill'
        assert (root / 'usr/local/bin/jibo-system-manager').read_bytes() == bench['usr/local/bin/jibo-system-manager']
        assert (root / 'opt/jibo/Knowledge/beacon/eye/custom.png').read_text() == 'custom eye'
        events.append('bootstrap')
    backup = Path(install_fixture(fixture, stopper=stop, bootstrap=bootstrap))
    assert events == ['stop', 'bootstrap']
    assert installer.factory_evidence(str(root)) == evidence
    assert json.loads((root / 'var/jibo/mode.json').read_text())['mode'] == 'int-developer'
    assert (root / 'usr/local/bin/jibo-service-version').read_bytes() == bench['usr/local/bin/jibo-service-version']
    assert (root / 'usr/local/bin/jibo-service-version').stat().st_mode & 0o111
    assert (root / 'usr/local/lib/libfixture-current.so').is_symlink()
    assert os.readlink(root / 'usr/local/lib/libfixture-current.so') == 'libfixture.so'
    assert (backup / 'usr/local/bin/jibo-system-manager').read_text() == 'old system manager'
    assert (backup / 'opt/jibo/Jibo/Skills/@be/be/index.js').read_text() == 'old skill'
    assert json.loads((backup / 'manifest.json').read_text())['status'] == 'installed'
    assert (root / 'opt/jibo/Knowledge/jukebox/music/Album/track.mp3').read_text() == 'music'
    assert (root / 'opt/jibo/Knowledge/beacon/eye').stat().st_mode & 0o777 == 0o777
    assert (root / 'opt/tmp/jukebox-music/Album/track.mp3').exists()
    assert not (root / 'usr/local/bin/jibo-ssm/lib/ignored.js.openjibo-orig').exists()
    assert not (root / 'opt/jibo/Jibo/Skills/README.md').exists()
    assert not (root / 'usr/local/share/tests').exists()
    assert not (root / 'opt/jibo/Jibo/Skills/@be/be/tests').exists()
    assert '--knowledge' in (root / 'etc/init.d/S04jibo-bootstrap').read_text()
    installer.recover(str(backup), root=str(root), stopper=lambda root: [])
    assert (root / 'usr/local/bin/jibo-system-manager').read_text() == 'old system manager'
    assert not (root / 'usr/local/bin/jibo-runtime-bootstrap').exists()
    assert (root / 'opt/jibo/Knowledge/beacon/eye/custom.png').read_text() == 'custom eye'
    write(root, '/usr/local/bin/jibo-runtime-bootstrap', 'new installation after recovery')
    installer.recover(str(backup), root=str(root), stopper=lambda root: pytest.fail('Already restored'))
    assert (root / 'usr/local/bin/jibo-runtime-bootstrap').read_text() == 'new installation after recovery'


@pytest.mark.parametrize('failure', ('bad-zip', 'missing-package', 'traversal', 'symlink', 'duplicate', 'disk', 'factory'))
def test_preflight_failures_do_not_stop_or_replace_services(fixture, failure):
    root, stage, archives, beam, bench = fixture
    kwargs = {}
    if failure == 'bad-zip':
        archives[1].write_bytes(b'not a zip')
    elif failure == 'missing-package':
        del bench['usr/local/bin/jibo-service-version']
        make_zip(archives[1], 'BEnch-master', bench)
    elif failure in ('traversal', 'symlink'):
        bench['usr/local/lib/../../../../escape' if failure == 'traversal' else 'usr/local/lib/escape'] = (
            b'escape' if failure == 'traversal' else ('../../../../escape',))
        make_zip(archives[1], 'BEnch-master', bench)
    elif failure == 'duplicate':
        with pytest.warns(UserWarning, match='Duplicate name'):
            with zipfile.ZipFile(archives[1], 'a') as archive:
                archive.writestr('BEnch-master/usr/local/bin/jibo-system-manager', 'duplicate')
    elif failure == 'disk':
        kwargs['available'] = lambda directory: (1, 1024)
    elif failure == 'factory':
        (root / 'var/jibo/lps/CameraModelParamsL.json').unlink()
    def unexpected(root):
        pytest.fail('Preflight must finish before services stop')
    with pytest.raises((RuntimeError, zipfile.BadZipFile, FileNotFoundError)):
        install_fixture(fixture, stopper=unexpected, **kwargs)
    assert (root / 'usr/local/bin/jibo-system-manager').read_text() == 'old system manager'
    assert (root / 'opt/jibo/Jibo/Skills/@be/be/index.js').read_text() == 'old skill'
    assert not (root / 'opt/jibo/pm-backups').exists()


def test_failed_bootstrap_rolls_back_and_does_not_change_mode(fixture):
    root = fixture[0]
    def fail(root):
        raise RuntimeError('unwritable runtime state')
    with pytest.raises(RuntimeError, match='unwritable'):
        install_fixture(fixture, bootstrap=fail)
    assert (root / 'usr/local/bin/jibo-system-manager').read_text() == 'old system manager'
    assert (root / 'opt/jibo/Jibo/Skills/@be/be/index.js').read_text() == 'old skill'
    assert not (root / 'usr/local/bin/jibo-runtime-bootstrap').exists()
    assert json.loads((root / 'var/jibo/mode.json').read_text())['mode'] == 'int-developer'
    assert not (root / 'var/jibo/5x1-pm-oobe-complete.json').exists()
    manifest = next((root / 'opt/jibo/pm-backups').glob('*/manifest.json'))
    assert json.loads(manifest.read_text())['status'] == 'rolled-back'


def test_recovery_rechecks_completed_rollback_after_acquiring_lock(fixture, monkeypatch):
    root = fixture[0]
    backup = Path(install_fixture(fixture))
    replacement = root / 'usr/local/bin/jibo-runtime-bootstrap'
    real_flock = installer.fcntl.flock
    events = []

    def finish_other_recovery_before_lock(fd, operation):
        # Another recovery finishes after this call starts, before it gets the
        # lock. Its consumed backup must not be replayed against a later install.
        monkeypatch.setattr(installer.fcntl, 'flock', real_flock)
        installer.recover(str(backup), root=str(root), stopper=lambda root: [])
        replacement.write_text('later installation')
        return real_flock(fd, operation)

    monkeypatch.setattr(installer.fcntl, 'flock', finish_other_recovery_before_lock)
    installer.recover(str(backup), root=str(root), stopper=lambda root: events.append('stop'))
    assert replacement.exists(), 'Stale recovery deleted a later installation file'
    assert replacement.read_text() == 'later installation'
    assert events == []
    assert json.loads((backup / 'manifest.json').read_text())['status'] == 'rolled-back'


@pytest.mark.parametrize('damage', ('missing', 'torn-tail', 'shortened', 'mismatched'))
def test_installed_backup_with_incomplete_journal_is_not_reported_restored(fixture, damage):
    root = fixture[0]
    backup = Path(install_fixture(fixture))
    journal = backup / 'journal.jsonl'
    lines = journal.read_text().splitlines(True)
    if damage == 'missing':
        journal.unlink()
    elif damage == 'torn-tail':
        journal.write_text(''.join(lines[:-1]) + lines[-1].rstrip('\n'))
    elif damage == 'shortened':
        journal.write_text(''.join(lines[:-1]))
    else:
        entry = json.loads(lines[0])
        entry['existed'] = not entry['existed']
        journal.write_text(json.dumps(entry) + '\n' + ''.join(lines[1:]))
    events = []
    with pytest.raises(RuntimeError, match='Incomplete or inconsistent recovery journal'):
        installer.recover(str(backup), root=str(root), stopper=lambda root: events.append('stop'))
    assert events == []
    assert json.loads((backup / 'manifest.json').read_text())['status'] == 'installed'
    assert (root / 'opt/jibo/Jibo/Skills/@be/be/index.js').read_text() == 'new skill'
    assert (backup / 'opt/jibo/Jibo/Skills/@be/be/index.js').read_text() == 'old skill'
    assert (root / 'usr/local/bin/jibo-runtime-bootstrap').exists()


def test_partial_recovery_can_be_retried_after_saved_original_was_consumed(fixture, monkeypatch):
    root = fixture[0]
    backup = Path(install_fixture(fixture))
    real_transfer = installer.transfer

    def interrupt_after_restore(source, target):
        real_transfer(source, target)
        raise RuntimeError('fixture recovery interrupted after restoring an original')

    with monkeypatch.context() as patch:
        patch.setattr(installer, 'transfer', interrupt_after_restore)
        with pytest.raises(RuntimeError, match='fixture recovery interrupted'):
            installer.recover(str(backup), root=str(root), stopper=lambda root: [])
    assert json.loads((backup / 'manifest.json').read_text())['status'] == 'installed'
    installer.recover(str(backup), root=str(root), stopper=lambda root: [])
    assert (root / 'usr/local/bin/jibo-system-manager').read_text() == 'old system manager'
    assert (root / 'opt/jibo/Jibo/Skills/@be/be/index.js').read_text() == 'old skill'
    assert not (root / 'usr/local/bin/jibo-runtime-bootstrap').exists()
    assert json.loads((backup / 'manifest.json').read_text())['status'] == 'rolled-back'


def test_interrupted_install_can_be_recovered_from_journal(fixture):
    root, stage, archives, _, _ = fixture
    program = '''import importlib.util, os, sys
s=importlib.util.spec_from_file_location('pm',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
original=m.transfer
count=[0]
def transfer(source,target):
    original(source,target)
    count[0]+=1
    if count[0]==4: os._exit(91)
m.transfer=transfer
m.install(sys.argv[2],sys.argv[3],sys.argv[4],root=sys.argv[5],stopper=lambda root: [],bootstrap=lambda root: None)
'''
    import sys
    result = subprocess.run([sys.executable, '-c', program, str(PM / 'install-runtime.py'),
                             str(archives[0]), str(archives[1]), str(stage), str(root)])
    assert result.returncode == 91
    backup = next((root / 'opt/jibo/pm-backups').iterdir())
    assert json.loads((backup / 'manifest.json').read_text())['status'] == 'installing'
    # An interrupted installation has no final file list; a torn, uncommitted
    # intent must still be ignored rather than blocking recovery of prior files.
    with (backup / 'journal.jsonl').open('a') as journal:
        journal.write('{"path":')
    installer.recover(str(backup), root=str(root), stopper=lambda root: [])
    assert (root / 'opt/jibo/Jibo/Skills/@be/be/index.js').read_text() == 'old skill'
    assert (root / 'usr/local/bin/jibo-system-manager').read_text() == 'old system manager'


@pytest.mark.parametrize('variant', VARIANTS)
def test_every_variant_embeds_the_same_installer_and_orders_success_steps(variant):
    source = (PM / variant).read_text()
    embedded = source.split("python <<'PM_INSTALL'\n", 1)[1].split('\nPM_INSTALL\n', 1)[0]
    assert embedded + '\n' == (PM / 'install-runtime.py').read_text()
    assert source.index('BEnch/archive/') < source.index("python <<'PM_INSTALL'")
    assert source.index('\nPM_INSTALL\n') < source.index('=== Step 4.5:') < source.index('jibo-setmode normal') < source.rindex('\nreboot')
    assert 'PrepareRobot' not in source and 'SetupRobot' not in source
    subprocess.run(['sh', '-n', str(PM / variant)], check=True)

# Execute the actual shell stages against a relocated fixture filesystem. Only
# hardware/network commands and the Python entrypoint are replaced by this shim.
SHIM = r'''import importlib.util, json, os, pathlib, re, shutil, sys
root = os.environ['FIXTURE_ROOT']
events = os.environ['FIXTURE_EVENTS']
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
def event(value):
    with open(events, 'a') as handle: handle.write(value + '\n')
def local_bootstrap(value):
    event('bootstrap')
    if os.environ.get('FAIL_AT') == 'bootstrap': raise RuntimeError('fixture initialization failed')
    target = pathlib.Path(value + '/var/jibo/credentials.json')
    if not target.exists():
        target.write_text(json.dumps({'accessKeyId':'fixture-local', 'secretAccessKey':'fixture-secret',
                                     'region':'api', 'endpoint':'http://joap.5x1.com:80'}))
try:
    if tool == 'wget':
        event('download')
        if os.environ.get('FAIL_AT') == 'download': raise RuntimeError('fixture download failed')
        source = os.environ['BENCH_ARCHIVE' if any('BEnch/archive' in a for a in args) else 'BEAM_ARCHIVE']
        shutil.copyfile(source, args[args.index('-O') + 1])
    elif tool == 'curl':
        event('certificate')
        pathlib.Path(args[args.index('-o') + 1]).write_text('-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n')
    elif tool == 'python':
        code = args[args.index('-c') + 1] if '-c' in args else sys.stdin.read()
        if 'def archive_entries(' in code:
            event('install')
            namespace = {'__name__':'fixture'}
            exec(compile(code, 'embedded-installer', 'exec'), namespace)
            stage = os.environ['PM_STAGE']
            namespace['install'](stage+'/beam.zip', stage+'/bench.zip', stage, root=root,
                                 stopper=lambda root: event('stop'), bootstrap=local_bootstrap)
        elif 'def prepare_oobe(' in code or 'def finish_oobe(' in code:
            namespace = {'__name__':'fixture'}
            exec(compile(code, 'embedded-oobe', 'exec'), namespace)
            if 'prepare_oobe' in namespace:
                event('prepare'); namespace['prepare_oobe'](root)
            else:
                event('receipt'); namespace['finish_oobe'](root)
        else:
            if '-c' not in args:
                code = re.sub(r'/(?:opt/|usr/local/|var/jibo/|etc/ssl/|tmp/)', lambda match: root + match.group(0), code)
            sys.argv = ['-'] + args[1:] if args and args[0] == '-' else ['-c']
            exec(compile(code, 'embedded-patch', 'exec'), {'__name__':'__main__'})
    elif tool == 'jibo-setmode':
        event('normal')
        assert pathlib.Path(root + '/usr/local/bin/jibo-system-manager').read_bytes().startswith(b'\x7fELF')
        assert pathlib.Path(root + '/opt/jibo/Jibo/Skills/@be/be/index.js').read_text() == 'new skill'
        pathlib.Path(root + '/var/jibo/mode.json').write_text(json.dumps({'mode':args[0]}))
    elif tool == 'jibo-bbfw-update': event('firmware')
    elif tool == 'reboot': event('reboot')
except Exception as error:
    print('fixture command failed: '+str(error), file=sys.stderr)
    sys.exit(1)
'''


def relocate_shell(source, root):
    import re
    delimiter = None
    output = []
    for line in source.splitlines(True):
        if delimiter is not None:
            output.append(line)
            if line.strip() == delimiter:
                delimiter = None
            continue
        match = re.search(r"<<\s*'([A-Z_]+)'", line)
        line = re.sub(r'/(?:opt/|usr/local/|var/jibo/|etc/|tmp/)', lambda match: str(root) + match.group(0), line)
        output.append(line)
        if match:
            delimiter = match.group(1)
    return ''.join(output)


@pytest.mark.parametrize('variant', VARIANTS)
@pytest.mark.parametrize('failure', (None, 'download', 'bootstrap'))
def test_all_pm_shell_flows_with_mocked_hardware_and_network(fixture, variant, failure):
    import sys
    root, stage, archives, _, _ = fixture
    tools = stage / 'tools'
    tools.mkdir()
    shim = write(stage, '/tools/shim', '#!' + sys.executable + '\n' + SHIM)
    shim.chmod(0o755)
    for name in ('python', 'wget', 'curl', 'jibo-mount', 'jibo-setmode', 'jibo-bbfw-update', 'pkill', 'sleep', 'reboot'):
        (tools / name).symlink_to(shim)
    script = write(stage, '/fixture-pm.sh', relocate_shell((PM / variant).read_text(), root))
    event_file = stage / 'events'
    environment = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'],
                       FIXTURE_ROOT=str(root), FIXTURE_EVENTS=str(event_file),
                       BEAM_ARCHIVE=str(archives[0]), BENCH_ARCHIVE=str(archives[1]),
                       FAIL_AT=failure or '')
    result = subprocess.run(['sh', str(script)], env=environment, text=True, capture_output=True)
    events = event_file.read_text().splitlines() if event_file.exists() else []
    assert 'fixture-secret' not in result.stdout + result.stderr
    receipt = root / 'var/jibo/5x1-pm-oobe-complete.json'
    if failure:
        assert result.returncode != 0, result.stdout + result.stderr
        assert 'normal' not in events and 'reboot' not in events
        assert not receipt.exists()
        assert (root / 'usr/local/bin/jibo-system-manager').read_text() == 'old system manager'
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert events.count('download') == 2
        assert events.index('stop') < events.index('bootstrap') < events.index('normal') < events.index('reboot')
        assert json.loads((root / 'var/jibo/mode.json').read_text())['mode'] == 'normal'
        if 'oobe' in variant:
            assert events.index('normal') < events.index('firmware') < events.index('receipt') < events.index('reboot')
            assert receipt.exists()


def test_known_beetle_startup_overlay_is_released_after_services_stop(fixture):
    root, stage, archives, beam, bench = fixture
    destination = '/usr/local/bin/jibo-ssm/startup/startup-view.js'
    bench[destination.lstrip('/')] = 'new startup view'
    make_zip(archives[1], 'BEnch-master', bench)
    write(root, '/proc/self/mountinfo', '91 1 8:1 /jibo/phoenix/network-status/startup-view-dom.js ' + destination + ' rw - ext4 /dev/fixture rw\n')
    events = []
    install_fixture(fixture, stopper=lambda root: events.append('stop'),
                    unmount=lambda target: events.append(target))
    assert events == ['stop', destination]


def test_unknown_mounted_service_files_are_not_replaced(fixture):
    root = fixture[0]
    destination = '/usr/local/bin/jibo-system-manager'
    write(root, '/proc/self/mountinfo', '91 1 8:1 /unknown ' + destination + ' rw - ext4 /dev/fixture rw\n')
    with pytest.raises(RuntimeError, match='unknown mounted file'):
        install_fixture(fixture, stopper=lambda root: pytest.fail('Must fail before service shutdown'))
    assert (root / destination.lstrip('/')).read_text() == 'old system manager'
