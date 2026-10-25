# Post-mod scripts

All four entry points (`post-mod.sh`, `post-oobe-mod.sh`, `web-post-mod.sh`, and
`web-post-oobe-mod.sh`) install **BEam and BEnch** from their `master` archives.
The standard PM entry point is served at `https://scripts.5x1.com/pm`; the web
variants remain separate entry points for mod.5x1.com.

For a robot originally in OOBE that BEetle has prepared in `int-developer` mode:

```sh
jibo-mount --rw
curl -fsSL -k https://raw.githubusercontent.com/Jibo-Revival-Group/BEach/master/PM/post-oobe-mod.sh -o /tmp/pm.sh
chmod +x /tmp/pm.sh
sh /tmp/pm.sh
```

The OOBE scripts validate factory identity and camera calibration, save their
original configuration under `/var/jibo/pm-oobe-before-5x1`, and preserve existing
credential keys. Absent credentials get random local values; no PrepareRobot or
SetupRobot request is needed. Invalid existing credentials stop preparation.
Local credentials support robot startup; cloud account pairing/acceptance is a
separate concern.

Both archives are validated and staged before services stop. PM checks space
for staging, filesystem allocation, migration, deployment, and recovery backups.
BEam goes into `/opt/jibo/Jibo/Skills`, and BEnch's `usr/local` payload goes into
`/usr/local`. Repository metadata, tests, and obsolete backup artifacts are
excluded. Executable permissions and relative symlinks are retained; native
binaries, including `jibo-service-version`, are not patched.

The installer freezes Jibo's supervisors and stops their services before file
replacement. BEetle's recognized startup-view bind overlay is released so the
underlying BEnch file can be backed up and updated; unknown mounted files stop
preflight. Files on the same filesystem move into place; replacements are
journaled and original files remain in `/opt/jibo/pm-backups/<run>/`. Legacy music
and BEacon data are merged into Knowledge without overwriting current content
or clearing `/opt/tmp`. Installation failures restore the replaced files and
resume the supervisors. A hard interruption leaves a journal for recovery.

PM installs `/etc/init.d/S04jibo-bootstrap`, runs local runtime/KB initialization,
then applies the existing TLS, hub, region, OTA and SSM compatibility patches.
Managed service starts/restarts use `jibo-service-start` to prepare their local
storage. Only PM changes the robot's mode. A failed installation or initialization
prevents normal-mode selection and reboot. Later configuration failures also
prevent successful completion and reboot; the installation backup is retained.

Before reboot, OOBE PM verifies factory hashes, credentials, normal mode and hub
configuration. It writes `/var/jibo/5x1-pm-oobe-complete.json` and prints
`5X1_OOBE_PM_COMPLETE` so BEetle can distinguish completion from an interrupted
run. Receipts contain no credential secrets.

## Recovery and maintenance

To restore a successful or interrupted package installation, obtain this
repository's `PM/install-runtime.py` and run it on the robot:

```sh
jibo-mount --rw
python /tmp/install-runtime.py --rollback /opt/jibo/pm-backups/RUN_DIRECTORY
```

Recovery verifies factory identity/calibration hashes, stops services, restores
journaled files, and resumes the supervisors. It consumes saved files as they
are restored. Recovery reads the manifest and journal under the installer lock,
so a completed rollback is not replayed against a later installation. For a
completed installation, the journal must match the manifest's full file list;
missing, truncated, or inconsistent journals stop recovery before services or
installed files are touched. Keep the backup intact and investigate the damaged
journal rather than treating the run as restored. Interrupted installations still
use complete journal lines, ignoring an unfinished final intent.

User data under Knowledge and OOBE configuration backups remain.
After a hard interruption/recovery, rerun PM to finish configuration before
rebooting. Keep backups until the installation has been validated on hardware.

`install-runtime.py` is the canonical installer source, embedded verbatim in all
four scripts so a downloaded PM remains self-contained. When changing it, update
all four `PM_INSTALL` blocks; the tests enforce equality.

Run fixture tests without BEefy access, firmware flashing, or a real reboot:

```sh
python3 -m pytest -q tests
```

Live OOBE installation, native services, BBFW/reboot behavior, and cloud acceptance
of locally generated credentials still require hardware validation.
