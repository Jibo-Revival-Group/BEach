"""PM installs 5x1 by queueing one OTA from BEaker (joap.5x1.com) with Jibo's own updater.
The real PM scripts run against a disposable robot tree; robot and network commands are stand-ins."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

PM = Path(__file__).resolve().parents[1] / 'PM'
VARIANTS = ('post-mod.sh', 'post-oobe-mod.sh', 'web-post-mod.sh', 'web-post-oobe-mod.sh')
OOBE_VARIANTS = ('post-oobe-mod.sh', 'web-post-oobe-mod.sh')
SUBSYSTEMS = ['os', 'services', '@be/be', 'jibo-tbd', 'oobe-config', 'jibo-diagnostics', 'fin-goods-test']
NEWEST = {'os': '13.0.7', 'services': '13.0.7', '@be/be': '13.0.11', 'jibo-tbd': '3.1.9',
          'oobe-config': '9.0.1', 'jibo-diagnostics': '3.1.2', 'fin-goods-test': '3.0.0'}


def package(subsystem):
    return ('package ' + subsystem).encode()


def write(root, name, data):
    target = root / name.lstrip('/')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data if isinstance(data, bytes) else data.encode())
    return target


def ota_block(source):
    return source[source.index('# >>> 5x1 OTA install'):source.index('# <<< 5x1 OTA install')]


SHIM = r'''import hashlib, json, os, pathlib, re, shutil, sys
root = os.environ['FIXTURE_ROOT']
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
def event(value):
    with open(os.environ['FIXTURE_EVENTS'], 'a') as handle: handle.write(value + '\n')
try:
    if tool == 'wget':
        name = args[0].rsplit('/', 1)[1][:-len('.tar')]
        event('download ' + name)
        data = b'damaged' if name == os.environ.get('BAD_DOWNLOAD') else ('package ' + ('@be/be' if name == '@be-be' else name)).encode()
        pathlib.Path(args[args.index('-O') + 1]).write_bytes(data)
    elif tool == 'curl':
        script = [a for a in args if a.endswith('.sh')][0].rsplit('/', 1)[1]
        event('oobe-redirect ' + script)
        shutil.copyfile(os.environ['OOBE_SCRIPT_DIR'] + '/' + script, args[args.index('-o') + 1])
    elif tool == 'python':
        code = args[args.index('-c') + 1] if '-c' in args else sys.stdin.read()
        if 'def prepare_oobe(' in code or 'def finish_oobe(' in code:
            namespace = {'__name__': 'fixture'}
            exec(compile(code, 'embedded-oobe', 'exec'), namespace)
            if 'prepare_oobe' in namespace:
                event('prepare'); namespace['prepare_oobe'](root)
            else:
                event('receipt'); namespace['finish_oobe'](root)
        else:
            event('credentials')
            sys.argv = ['-c']
            exec(compile(code, 'embedded-patch', 'exec'), {'__name__': '__main__'})
    elif tool == 'jibo-setmode':
        event('normal')
        pathlib.Path(root + '/var/jibo/mode.json').write_text(json.dumps({'mode': args[0]}))
    elif tool == 'jibo-bbfw-update': event('firmware')
    elif tool == 'jibo-mount': event('jibo-mount')
    elif tool == 'mount': event('mount ' + ' '.join(args))
    elif tool == 'reboot': event('reboot')
    elif tool == 'df':
        print('Filesystem           1K-blocks      Used Available Use% Mounted on')
        print('/dev/mmcblk0p6')
        print('                       10570752   7629824   %s  76%% /opt' % os.environ.get('FREE_KB', '2400000'))
    elif tool == 'jibo-version': print('Jibo Release Version: Release-%s-20190225' % os.environ.get('FIXTURE_OS', '13.0.0'))
    elif tool == 'jibo-service-version': print('Jibo Service Version: Release-3.3.2-20170623')
    elif tool == 'jibo-get-update':
        subsystem = args[args.index('--subsystem') + 1]
        event('offer %s %s %s' % (subsystem, args[args.index('--version') + 1], args[args.index('--filter') + 1]))
        assert args[args.index('--credentials') + 1].endswith('/var/jibo/credentials.json')
        if subsystem == os.environ.get('NO_OFFER'):
            print('{"error":{"message":"Update not found","code":"UPDATE_NOT_FOUND","statusCode":404}}')
        else:
            data = ('package ' + subsystem).encode()
            sha = hashlib.sha1(data).hexdigest()
            print(json.dumps({'_id': 'x', 'fromVersion': '0.0.0', 'toVersion': json.loads(os.environ['NEWEST'])[subsystem],
                              'url': 'http://joap.5x1.com:80/packages/%s/%s.tar' % (sha, subsystem.replace('/', '-')),
                              'shaHash': sha, 'length': int(os.environ.get('LENGTH', '1000')), 'subsystem': subsystem,
                              'filter': 'fcs', 'dependencies': {}}))
    elif tool == 'jibo-apply-update':
        event('apply ' + ' '.join(args))
        assert os.environ.get('DISPLAY') == ':0'
except Exception as error:
    print('fixture command failed: ' + repr(error), file=sys.stderr)
    sys.exit(1)
'''


def relocate_shell(source, root):
    delimiter, output = None, []
    for line in source.splitlines(True):
        if delimiter is not None:
            output.append(line)
            if line.strip() == delimiter:
                delimiter = None
            continue
        match = re.search(r"<<\s*'([A-Z_]+)'", line)
        output.append(re.sub(r'/(?:opt/|usr/local/|var/jibo/|etc/|tmp/)', lambda m: str(root) + m.group(0), line))
        if match:
            delimiter = match.group(1)
    return ''.join(output)


@pytest.fixture
def robot(tmp_path):
    root = tmp_path / 'robot'
    (root / 'tmp').mkdir(parents=True)
    write(root, '/var/jibo/identity.json', json.dumps({'name': 'Fixture-Robot', 'serial_number': 'fixture',
                                                       'cpuid': 'fixture', 'wifi_mac': 'fixture'}))
    write(root, '/var/jibo/mode.json', '{"mode":"int-developer"}')
    for name in ('CameraModelParamsL.json', 'CameraModelParamsR.json', 'InterCameraTransform.json'):
        write(root, '/var/jibo/lps/' + name, '{"factory":"preserve"}')
    write(root, '/var/jibo/credentials.json', json.dumps({'accessKeyId': 'fixture-key', 'secretAccessKey': 'fixture-secret',
                                                           'region': 'api', 'endpoint': 'https://api.jibo.com'}))
    write(root, '/opt/jibo/Jibo/Skills/@be/be/package.json', '{\n  "name": "@be/be",\n  "version": "13.0.10"\n}\n')
    write(root, '/opt/jibo/Knowledge/jibo/memo.db', 'kb')
    (root / 'opt/tmp').mkdir(mode=0o700)
    (root / 'opt/tmp').chmod(0o700)  # as a user's own mkdir over SSH leaves it
    return root


def run_pm(root, variant, **env):
    work = root.parent
    tools = work / 'tools'
    tools.mkdir(exist_ok=True)
    shim = write(work, '/tools/shim', '#!' + sys.executable + '\n' + SHIM)
    shim.chmod(0o755)
    for name in ('python', 'wget', 'curl', 'jibo-mount', 'mount', 'jibo-setmode', 'jibo-bbfw-update', 'pkill', 'sleep',
                 'reboot', 'df', 'jibo-version', 'jibo-service-version', 'jibo-get-update', 'jibo-apply-update'):
        if not (tools / name).exists():
            (tools / name).symlink_to(shim)
    script = write(work, '/fixture-pm.sh', relocate_shell((PM / variant).read_text(), root))
    for oobe in OOBE_VARIANTS:  # what the regular PM's OOBE redirect downloads
        write(work, '/oobe-scripts/' + oobe, relocate_shell((PM / oobe).read_text(), root))
    events = work / 'events'
    if events.exists():
        events.unlink()
    environment = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'], FIXTURE_ROOT=str(root),
                       FIXTURE_EVENTS=str(events), OOBE_SCRIPT_DIR=str(work / 'oobe-scripts'),
                       NEWEST=json.dumps(NEWEST), **env)
    environment.pop('DISPLAY', None)
    result = subprocess.run(['sh', str(script)], env=environment, text=True, capture_output=True)
    return result, events.read_text().splitlines() if events.exists() else []


def expected_apply(root, os_from='13.0.0'):
    skills = '%s/opt/jibo/Jibo/Skills/' % root
    froms = {'os': os_from, 'services': '3.3.2', '@be/be': '13.0.10'}
    destinations = {'os': '/', 'services': '/usr/local'}
    return 'apply ' + ' '.join(
        '--source %s/opt/ota/pm-%s.tar --subsystem %s --from %s --to %s --destination %s --filter fcs' % (
            root, s.replace('/', '-'), s, froms.get(s, '0.0.0'), NEWEST[s], destinations.get(s, skills + s))
        for s in SUBSYSTEMS)


@pytest.mark.parametrize('variant', VARIANTS)
def test_every_pm_script_ends_by_queueing_the_same_ota(variant):
    subprocess.run(['sh', '-n', str(PM / variant)], check=True)
    source = (PM / variant).read_text()
    assert ota_block(source) == ota_block((PM / 'post-mod.sh').read_text())
    assert source.rstrip().endswith('# <<< 5x1 OTA install')
    assert 'BEnch/archive' not in source and 'BEam/archive' not in source, 'PM no longer installs from GitHub archives'
    assert 'S21firewall' not in source and 'isrgrootx1' not in source, 'the os package has these now'
    start = source.index('# >>> 5x1 OTA install')
    assert source.index('jibo-setmode normal') < start
    if 'oobe' in variant:
        assert source.index('prepare_oobe()') < source.index('jibo-setmode normal') < source.index('jibo-bbfw-update') \
            < source.index('finish_oobe()') < start
    if variant.startswith('web-'):
        assert source.index('boot.py finish') < start
    for delimiter, block in re.findall(r"^python << ?'([A-Z]+)'\n(.*?)^\1$", source, re.M | re.S):
        compile(block, variant, 'exec')


@pytest.mark.parametrize('variant', VARIANTS)
def test_pm_downloads_everything_then_queues_one_update(robot, variant):
    result, events = run_pm(robot, variant)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'fixture-secret' not in result.stdout + result.stderr
    if 'oobe' in variant:
        assert events[:6] == ['mount -o remount,rw /', 'mount -o remount,rw /usr/local', 'prepare', 'normal',
                              'firmware', 'receipt'], events
        assert (robot / 'var/jibo/5x1-pm-oobe-complete.json').exists()
        start = 6
    else:
        assert events[:3] == ['jibo-mount', 'credentials', 'normal'], events
        start = 3
    assert events[start:start + 7] == ['offer %s 0.0.0 fcs' % s for s in SUBSYSTEMS]
    assert events[start + 7:-1] == ['download ' + s.replace('/', '-') for s in SUBSYSTEMS]
    assert events[-1] == expected_apply(robot), events[-1]
    for s in SUBSYSTEMS:
        assert (robot / ('opt/ota/pm-%s.tar' % s.replace('/', '-'))).read_bytes() == package(s)
    creds = json.loads((robot / 'var/jibo/credentials.json').read_text())
    assert (creds['endpoint'], creds['accessKeyId']) == ('http://joap.5x1.com:80', 'fixture-key')
    assert json.loads((robot / 'var/jibo/mode.json').read_text())['mode'] == 'normal'
    assert (robot / 'opt/tmp').stat().st_mode & 0o777 == 0o777, 'Be (uid 2000) must be able to use /opt/tmp'
    assert 'restarts' in result.stdout


@pytest.mark.parametrize('variant', OOBE_VARIANTS)
def test_old_oobe_os_gets_the_same_single_update(robot, variant):
    (robot / 'var/jibo/credentials.json').unlink()  # factory OOBE: PM makes local keys
    shutil.rmtree(str(robot / 'opt/jibo/Jibo/Skills/@be'))
    result, events = run_pm(robot, variant, FIXTURE_OS='3.3.4')
    assert result.returncode == 0, result.stdout + result.stderr
    assert events[-1] == expected_apply(robot, os_from='3.3.4').replace('--from 13.0.10', '--from 0.0.0')


@pytest.mark.parametrize('variant', ('post-mod.sh', 'post-oobe-mod.sh'))
def test_a_missing_offer_stops_before_any_download(robot, variant):
    result, events = run_pm(robot, variant, NO_OFFER='jibo-tbd')
    assert result.returncode != 0
    assert 'did not offer jibo-tbd' in result.stderr
    assert not any(e.startswith(('download', 'apply')) for e in events), events


def test_too_little_space_stops_before_any_download(robot):
    # 7 packages of 100 MB need 700 MB plus 100 MB for the updater.
    result, events = run_pm(robot, 'post-mod.sh', LENGTH=str(100 * 1024 * 1024), FREE_KB=str(790 * 1024))
    assert result.returncode != 0
    assert 'needs 800 MB free on /opt' in result.stderr, result.stderr
    assert not any(e.startswith(('download', 'apply')) for e in events), events
    result, events = run_pm(robot, 'post-mod.sh', LENGTH=str(100 * 1024 * 1024), FREE_KB=str(820 * 1024))
    assert result.returncode == 0, result.stderr


def test_a_damaged_download_is_retried_then_nothing_is_installed(robot):
    result, events = run_pm(robot, 'post-mod.sh', BAD_DOWNLOAD='services')
    assert result.returncode != 0
    assert events.count('download services') == 5
    assert not any(e.startswith('apply') for e in events)
    assert not (robot / 'opt/ota/pm-services.tar').exists()


def test_a_finished_download_is_reused_when_pm_runs_again(robot):
    write(robot, '/opt/ota/pm-os.tar', package('os'))
    write(robot, '/opt/ota/pm-services.tar', b'half a download')
    result, events = run_pm(robot, 'post-mod.sh')
    assert result.returncode == 0, result.stderr
    assert 'download os' not in events and 'download services' in events
    assert (robot / 'opt/ota/pm-services.tar').read_bytes() == package('services')


def test_an_unfinished_update_is_set_aside(robot):
    write(robot, '/var/jibo/ota.json', '{"stuck": true}')
    result, events = run_pm(robot, 'post-mod.sh')
    assert result.returncode == 0, result.stderr
    assert not (robot / 'var/jibo/ota.json').exists()
    assert (robot / 'var/jibo/ota.json.old').read_text() == '{"stuck": true}'
    assert events[-1].startswith('apply ')


@pytest.mark.parametrize('variant', ('post-mod.sh', 'web-post-mod.sh'))
@pytest.mark.parametrize('oobe', ('state-file', 'oobe-mode', 'no-be-no-kb', 'no-be-empty-kb'))
def test_regular_pm_runs_the_oobe_pm_on_an_oobe_robot(robot, variant, oobe):
    if oobe == 'state-file':  # an OOBE robot after an earlier OOBE PM (normal mode, has jibo-mount)
        write(robot, '/var/jibo/mode.json', '{"mode":"normal"}')
        write(robot, '/var/jibo/5x1-pm-oobe-state.json', json.dumps({'schema': 1, 'robot': 'Fixture-Robot'}))
    elif oobe == 'oobe-mode':
        write(robot, '/var/jibo/mode.json', '{"mode": "oobe"}')
    else:  # int-developer OOBE robot that never had BEam or a Knowledge Base
        shutil.rmtree(str(robot / 'opt/jibo/Jibo/Skills/@be'))
        shutil.rmtree(str(robot / 'opt/jibo/Knowledge/jibo'))
        if oobe == 'no-be-no-kb':
            shutil.rmtree(str(robot / 'opt/jibo/Knowledge'))
    result, events = run_pm(robot, variant)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events[0] == 'oobe-redirect ' + variant.replace('post-mod', 'post-oobe-mod'), events
    assert 'jibo-mount' not in events
    assert events[1:4] == ['mount -o remount,rw /', 'mount -o remount,rw /usr/local', 'prepare'], events
    assert (robot / 'var/jibo/5x1-pm-oobe-complete.json').exists()
    assert events[-1].startswith('apply ')


@pytest.mark.parametrize('variant', ('post-mod.sh', 'web-post-mod.sh'))
def test_regular_pm_stays_regular_on_a_set_up_robot_without_be(robot, variant):
    shutil.rmtree(str(robot / 'opt/jibo/Jibo/Skills/@be'))  # a stock robot's first PM: no @be, but it has a KB
    result, events = run_pm(robot, variant)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events[0] == 'jibo-mount', events
