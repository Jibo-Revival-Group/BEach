"""Portable Python 2.7/3 PM installer; embedded verbatim in all four scripts."""
from __future__ import print_function
import errno
import fcntl
import hashlib
import json
import os
import posixpath
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile

BEAM_ROOT = '/opt/jibo/Jibo/Skills'
BENCH_ROOT = '/usr/local'
BOOT_HOOK = b'''#!/bin/sh
# Local filesystems are mounted before rcS; run after S03fs-resize.
set -e
case "${1:-start}" in
    start|restart) /usr/local/bin/jibo-runtime-bootstrap --knowledge ;;
esac
'''
EXCLUDED = set(('.git', '.github', '__pycache__', 'tests', 'test'))


def mkdir(directory):
    try:
        os.makedirs(directory)
    except OSError as error:
        if error.errno != errno.EEXIST or not os.path.isdir(directory):
            raise


def writable_directory(directory):
    if os.path.isdir(directory):
        return
    writable_directory(os.path.dirname(directory))
    try:
        os.mkdir(directory, 0o777)
        os.chmod(directory, 0o777)
    except OSError as error:
        if error.errno != errno.EEXIST or not os.path.isdir(directory):
            raise


def atomic_json(filename, value):
    fd, temporary = tempfile.mkstemp(prefix='.pm-', dir=os.path.dirname(filename))
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, filename)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def archive_entries(filename, package):
    result = []
    names = set()
    with zipfile.ZipFile(filename) as archive:
        for info in archive.infolist():
            name = info.filename
            parts = name.rstrip('/').split('/')
            if '\\' in name or name.startswith('/') or any(p in ('', '.', '..') for p in parts):
                raise RuntimeError('Unsafe archive member: ' + name)
            if parts[0].lower() != package.lower() + '-master':
                raise RuntimeError('Unexpected archive root: ' + parts[0])
            relative = '/'.join(parts[1:])
            if not relative or name.endswith('/'):
                continue
            if any(p in EXCLUDED for p in parts[1:]) or relative.endswith(('.bak', '.openjibo-orig', '.pre-beam-ota')):
                continue
            if package == 'BEam':
                if parts[1] not in ('@be', 'oobe-config', 'fin-goods-test', 'jibo-diagnostics', 'jibo-tbd'):
                    continue
                destination = BEAM_ROOT + '/' + relative
            else:
                if not relative.startswith('usr/local/'):
                    continue
                destination = '/' + relative
            if destination in names:
                raise RuntimeError('Duplicate archive member: ' + destination)
            names.add(destination)
            mode = info.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFLNK):
                raise RuntimeError('Unsupported archive member: ' + name)
            link = None
            if kind == stat.S_IFLNK:
                link = archive.read(info).decode('utf-8')
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(destination), link))
                boundary = BEAM_ROOT if package == 'BEam' else BENCH_ROOT
                if not link or link.startswith('/') or '\\' in link or not resolved.startswith(boundary + '/'):
                    raise RuntimeError('Unsafe archive symlink: ' + name)
            result.append({'name': name, 'destination': destination, 'size': info.file_size,
                           'mode': stat.S_IMODE(mode) or 0o644, 'link': link})
        required = (BEAM_ROOT + '/@be/be/package.json', BEAM_ROOT + '/@be/be/index.js',
                    BEAM_ROOT + '/@be/be/index.html', BEAM_ROOT + '/@be/be/runtime-state.js') if package == 'BEam' else (
            '/usr/local/bin/jibo-system-manager', '/usr/local/bin/jibo-service-version',
            '/usr/local/bin/jibo-runtime-bootstrap', '/usr/local/bin/jibo-service-start',
            '/usr/local/lib/jibo/runtime-state.js', '/usr/local/lib/jibo/knowledge-bootstrap.js',
            '/usr/local/etc/jibo-system-manager.json')
        if not all(name in names for name in required):
            raise RuntimeError('Incomplete ' + package + ' archive')
        if package == 'BEam':
            entry = next(e for e in result if e['destination'].endswith('/@be/be/package.json'))
            if json.loads(archive.read(entry['name']).decode('utf-8')).get('name') != '@be/be':
                raise RuntimeError('Invalid BEam host package')
        for entry in result:
            # A symlink/file must never serve as a parent for another member.
            parent = posixpath.dirname(entry['destination'])
            while parent not in ('/', '.'):
                if parent in names:
                    raise RuntimeError('Archive member has a file as parent: ' + entry['name'])
                parent = posixpath.dirname(parent)
        bad = archive.testzip()
        if bad:
            raise RuntimeError('Archive CRC failure: ' + bad)
    return result


def checked_target(root, destination):
    target = root + destination
    parent = os.path.dirname(target)
    while parent and parent != root and parent != '/':
        if os.path.islink(parent) or (os.path.exists(parent) and not os.path.isdir(parent)):
            raise RuntimeError('Unsafe destination parent: ' + parent)
        parent = os.path.dirname(parent)
    if os.path.isdir(target) and not os.path.islink(target):
        raise RuntimeError('Expected file at ' + target)
    return target


def free_space(directory):
    while not os.path.exists(directory):
        directory = os.path.dirname(directory)
    value = os.statvfs(directory)
    return os.stat(directory).st_dev, value.f_bavail * value.f_frsize


def check_space(root, stage, entries, available=free_space):
    requirements = {}
    remaining = {}
    def reserve(directory, size):
        device, free = available(directory)
        requirements[device] = requirements.get(device, 0) + size
        remaining[device] = free
        return device
    allocated = lambda size: ((size + 4095) // 4096) * 4096
    parents = set()
    for entry in entries:
        parent = posixpath.dirname(entry['destination'])
        while parent != '/':
            parents.add(parent)
            parent = posixpath.dirname(parent)
    stage_device = reserve(stage, sum(allocated(e['size']) for e in entries) + len(parents) * 4096)
    backup_device = reserve(root + '/opt/jibo/pm-backups', 0)
    target_peaks = {}
    for entry in entries:
        target = checked_target(root, entry['destination'])
        target_device = reserve(os.path.dirname(target), 0)
        old_size = allocated(os.lstat(target).st_size) if os.path.lexists(target) else 0
        new_size = allocated(entry['size'])
        # Same-filesystem promotion/backups use rename, so old files are not
        # duplicated. Across filesystems reserve growth plus one atomic copy.
        if old_size and backup_device != target_device:
            reserve(root + '/opt/jibo/pm-backups', old_size)
        if target_device != stage_device:
            reserve(os.path.dirname(target), max(0, new_size - old_size))
            target_peaks[target_device] = max(target_peaks.get(target_device, 0), new_size)
    for parent in parents:
        if not os.path.isdir(root + parent):
            reserve(root + parent, 4096)
        reserve(root + '/opt/jibo/pm-backups', 4096)
    for source, target in migration_pairs(root):
        if not os.path.isdir(source):
            continue
        for directory, directories, files in os.walk(source, followlinks=False):
            directories[:] = [d for d in directories if not os.path.islink(os.path.join(directory, d))]
            destination = os.path.join(target, os.path.relpath(directory, source))
            for name in files:
                old, new = os.path.join(directory, name), os.path.join(destination, name)
                if not os.path.islink(old) and not os.path.lexists(new):
                    reserve(destination, allocated(os.path.getsize(old)) + 4096)
    for device, size in requirements.items():
        if remaining[device] < size + target_peaks.get(device, 0) + 32 * 1024 * 1024:
            raise RuntimeError('Insufficient disk space for staging, deployment and backups')


def copy_file(source, target):
    mkdir(os.path.dirname(target))
    fd, temporary = tempfile.mkstemp(prefix='.pm-install-', dir=os.path.dirname(target))
    os.close(fd)
    try:
        if os.path.islink(source):
            os.unlink(temporary)
            os.symlink(os.readlink(source), temporary)
        else:
            shutil.copy2(source, temporary)
            if hasattr(os, 'geteuid') and os.geteuid() == 0:
                owner = os.stat(source)
                os.chown(temporary, owner.st_uid, owner.st_gid)
            with open(temporary, 'rb') as handle:
                os.fsync(handle.fileno())
        os.rename(temporary, target)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def transfer(source, target):
    mkdir(os.path.dirname(target))
    try:
        os.rename(source, target)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        copy_file(source, target)
        os.unlink(source)


def restore_files(root, backup, entries):
    for entry in reversed(entries):
        target = checked_target(root, entry['path'])
        saved = backup + entry['path']
        if entry['existed']:
            if os.path.lexists(saved):
                transfer(saved, target)
            elif not os.path.lexists(target):
                raise RuntimeError('Recovery file missing: ' + entry['path'])
        elif os.path.lexists(target):
            os.unlink(target)


def recover(backup, root='', stopper=None):
    root = os.path.abspath(root) if root else ''
    backup = os.path.abspath(backup)
    lock = open(root + '/var/jibo/pm-install.lock', 'a')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Read recovery state only while holding the installation lock. Another
        # recovery may have consumed this backup before we acquired the lock.
        with open(backup + '/manifest.json') as handle:
            manifest = json.load(handle)
        if manifest.get('status') == 'rolled-back':
            print('Installation backup was already restored: ' + backup)
            return
        if manifest['factory_sha256'] != factory_evidence(root):
            raise RuntimeError('Recovery backup belongs to different factory data')
        entries = []
        journal = backup + '/journal.jsonl'
        if os.path.exists(journal):
            with open(journal) as handle:
                for line in handle:
                    if not line.endswith('\n'):
                        break  # incomplete intent was never fsynced before replacement
                    entry = json.loads(line)
                    if not entry['path'].startswith((BEAM_ROOT + '/', BENCH_ROOT + '/', '/etc/init.d/')) or '..' in entry['path'].split('/'):
                        raise RuntimeError('Unsafe recovery path')
                    entries.append(entry)
        # A completed install persisted the full intent list in its manifest.
        # Do not claim rollback success after journal loss or truncation. An
        # interrupted install still relies on its complete journal lines only.
        if manifest.get('status') == 'installed' and entries != manifest['files']:
            raise RuntimeError('Incomplete or inconsistent recovery journal: ' + backup)
        frozen = (stopper or stop_services)(root) or []
        release_overlays(root, mounted_overlays(root, set(e['path'] for e in entries)))
        restore_files(root, backup, entries)
        manifest['status'] = 'rolled-back'
        atomic_json(backup + '/manifest.json', manifest)
        for pid in frozen:
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass
    finally:
        lock.close()


def stop_services(root):
    if root:
        raise RuntimeError('Fixture installation requires a mock service stopper')
    # Freeze supervisors before stopping children, so nothing respawns mid-copy.
    frozen = []
    children = []
    for name in os.listdir('/proc'):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            raw = open('/proc/' + name + '/cmdline', 'rb').read().decode('utf-8', 'replace')
            command = raw.split('\x00')
            executable = os.path.basename(command[0])
            if executable in ('jibo-service-monitor', 'jibo-system-manager'):
                os.kill(int(name), signal.SIGSTOP)
                frozen.append(int(name))
            elif (command[0].startswith('/usr/local/bin/jibo-') or
                  any(arg.startswith(('/usr/local/bin/jibo-ssm/', '/usr/local/bin/jibo-sts/',
                                      BEAM_ROOT + '/')) for arg in command[1:])):
                children.append(int(name))
        except (IOError, OSError, IndexError):
            continue
    for pid in children:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if children:
        time.sleep(2)
    for pid in children:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return frozen


def mounted_overlays(root, destinations):
    import re
    info = root + '/proc/self/mountinfo'
    if root and not os.path.isfile(info):
        return []
    result = []
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), value)
    with open(info) as handle:
        for line in handle:
            fields = line.split()
            if len(fields) < 6:
                continue
            source, target = unescape(fields[3]), unescape(fields[4])
            if target not in destinations:
                continue
            # BEetle's optional startup view is a file bind over an installed
            # service file. Release it after stopping services, preserving the
            # underlying original in the installation backup.
            known_source = re.search(r'/jibo/phoenix/network-status/startup-view(?:-x11-early|-dom)?\.js$', source)
            if target != '/usr/local/bin/jibo-ssm/startup/startup-view.js' or not known_source:
                raise RuntimeError('Refusing to replace unknown mounted file: ' + target)
            result.append(target)
    return result


def release_overlays(root, targets, unmount=None):
    if targets and root and unmount is None:
        raise RuntimeError('Fixture overlay release requires a mock unmount command')
    for target in targets:
        if unmount is not None:
            unmount(target)
        else:
            subprocess.check_call(['/bin/umount', target])


def factory_evidence(root):
    files = ['/var/jibo/identity.json'] + ['/var/jibo/lps/' + name for name in (
        'CameraModelParamsL.json', 'CameraModelParamsR.json', 'InterCameraTransform.json')]
    result = {}
    for filename in files:
        with open(root + filename, 'rb') as handle:
            raw = handle.read()
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict) or not data:
            raise RuntimeError('Missing factory data: ' + filename)
        result[filename] = hashlib.sha256(raw).hexdigest()
    return result


def merge_missing(source, target):
    if not os.path.isdir(source):
        return
    writable_directory(target)
    for name in os.listdir(source):
        old, new = os.path.join(source, name), os.path.join(target, name)
        if os.path.islink(old):
            continue
        if os.path.isdir(old):
            merge_missing(old, new)
        elif not os.path.lexists(new):
            copy_file(old, new)


def migration_pairs(root):
    pairs = [(root + '/opt/tmp/beacon', root + '/opt/jibo/Knowledge/beacon')]
    canonical = root + '/opt/jibo/Knowledge/jukebox/music'
    for relative in ('/opt/tmp/jukebox-music', BEAM_ROOT + '/@be/Skills/Jukebox/Music',
                     BEAM_ROOT + '/@be/be/skills/jukebox/music', BEAM_ROOT + '/@be/skills/jukebox/music',
                     BEAM_ROOT + '/@be/be/node_modules/@be/jukebox/music',
                     BEAM_ROOT + '/@be/jukebox/music', BEAM_ROOT + '/skills/jukebox/music'):
        pairs.append((root + relative, canonical))
    return pairs


def migrate_user_data(root):
    # Merge without replacing current content or deleting the legacy sources.
    for source, target in migration_pairs(root):
        merge_missing(source, target)


def install(beam_archive, bench_archive, stage, root='', stopper=stop_services,
            available=free_space, bootstrap=None, unmount=None):
    root = os.path.abspath(root) if root else ''
    packages = [(beam_archive, archive_entries(beam_archive, 'BEam')),
                (bench_archive, archive_entries(bench_archive, 'BEnch'))]
    entries = [entry for filename, items in packages for entry in items]
    entries.append({'destination': '/etc/init.d/S04jibo-bootstrap', 'size': len(BOOT_HOOK), 'mode': 0o755})
    # Validate everything and reserve space before replacing or stopping anything.
    evidence = factory_evidence(root)
    overlays = mounted_overlays(root, set(e['destination'] for e in entries))
    check_space(root, stage, entries, available)
    mkdir(root + '/var/jibo')
    lock = open(root + '/var/jibo/pm-install.lock', 'a')
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    payload = tempfile.mkdtemp(prefix='payload-', dir=stage)
    backup = root + '/opt/jibo/pm-backups/' + time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    manifest = {'schema': 1, 'status': 'staging', 'files': [], 'factory_sha256': evidence}
    frozen = []
    try:
        for filename, items in packages:
            with zipfile.ZipFile(filename) as archive:
                for entry in items:
                    target = payload + entry['destination']
                    mkdir(os.path.dirname(target))
                    if entry['link'] is not None:
                        os.symlink(entry['link'], target)
                    else:
                        with archive.open(entry['name']) as source, open(target, 'wb') as destination:
                            shutil.copyfileobj(source, destination)
                        mode = entry['mode']
                        with open(target, 'rb') as handle:
                            magic = handle.read(4)
                        if '/bin/' in entry['destination'] and (magic == b'\x7fELF' or magic.startswith(b'#!')):
                            mode |= 0o111
                        os.chmod(target, mode)
        hook = payload + '/etc/init.d/S04jibo-bootstrap'
        mkdir(os.path.dirname(hook))
        with open(hook, 'wb') as handle:
            handle.write(BOOT_HOOK)
        os.chmod(hook, 0o755)
        mkdir(backup)
        atomic_json(backup + '/manifest.json', manifest)
        frozen = stopper(root) or []
        release_overlays(root, overlays, unmount)
        migrate_user_data(root)
        manifest['status'] = 'installing'
        atomic_json(backup + '/manifest.json', manifest)
        for entry in entries:
            target = checked_target(root, entry['destination'])
            existed = os.path.lexists(target)
            manifest['files'].append({'path': entry['destination'], 'existed': existed})
            # Journal before replacement: interrupted installs remain recoverable.
            with open(backup + '/journal.jsonl', 'a') as journal:
                journal.write(json.dumps(manifest['files'][-1]) + '\n')
                journal.flush()
                os.fsync(journal.fileno())
            if existed:
                transfer(target, backup + entry['destination'])
            transfer(payload + entry['destination'], target)
        if bootstrap is None:
            if root:
                raise RuntimeError('Fixture installation requires a mock bootstrap')
            subprocess.check_call(['/usr/local/bin/jibo-runtime-bootstrap', '--knowledge'])
        else:
            bootstrap(root)
        if evidence != factory_evidence(root):
            raise RuntimeError('Factory data changed during installation')
        manifest['status'] = 'installed'
        atomic_json(backup + '/manifest.json', manifest)
        print('BEam and BEnch installed; recovery backup: ' + backup)
        return backup
    except BaseException:
        try:
            restore_files(root, backup, manifest['files'])
        except Exception:
            import sys
            sys.stderr.write('Automatic rollback incomplete; recovery backup: ' + backup + '\n')
            raise
        manifest['status'] = 'rolled-back'
        if os.path.isdir(backup):
            atomic_json(backup + '/manifest.json', manifest)
        for pid in frozen:
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(payload)
        lock.close()


if __name__ == '__main__':
    import sys
    try:
        if len(sys.argv) == 3 and sys.argv[1] == '--rollback':
            recover(sys.argv[2])
        else:
            stage = os.environ['PM_STAGE']
            install(stage + '/beam.zip', stage + '/bench.zip', stage)
    except Exception as error:
        sys.stderr.write('PM installation failed: ' + str(error) + '\n')
        sys.exit(1)
