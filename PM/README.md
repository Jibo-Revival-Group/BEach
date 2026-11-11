# Post-mod scripts

All four entry points (`post-mod.sh`, `post-oobe-mod.sh`, `web-post-mod.sh`, and
`web-post-oobe-mod.sh`) install 5x1 the same way an update does: they queue one OTA
from BEaker (`joap.5x1.com`) with Jibo's own updater. The standard PM entry point is
served at `https://scripts.5x1.com/pm`; the web variants remain separate entry points
for mod.5x1.com.

The regular scripts (`post-mod.sh`, `web-post-mod.sh`) detect an OOBE robot (the
OOBE PM's state file, an `oobe`/`identified` mode, or no `@be` skill and an empty or
missing `/opt/jibo/Knowledge`) and run the matching OOBE script instead, so OOBE
robots can use the regular commands too.

For a robot originally in OOBE that BEetle has prepared in `int-developer` mode
(OOBE images have no `jibo-mount`):

```sh
mount -o remount,rw /
mkdir -p /opt/tmp
curl -fsSL -k https://raw.githubusercontent.com/Jibo-Revival-Group/BEach/master/PM/post-oobe-mod.sh -o /opt/tmp/pm.sh
sh /opt/tmp/pm.sh
```

## What PM does

1. Remounts read-write.
2. Regular: points `/var/jibo/credentials.json` at `http://joap.5x1.com:80` (keys kept).
   OOBE: validates factory identity and camera calibration, saves the original
   configuration under `/var/jibo/pm-oobe-before-5x1`, keeps existing credential keys
   (absent ones get random local values; invalid ones stop PM) and points them at joap.
   No PrepareRobot or SetupRobot request is needed.
3. Sets normal mode. OOBE also flashes BBFW, re-checks the factory files and writes
   `/var/jibo/5x1-pm-oobe-complete.json` / prints `5X1_OOBE_PM_COMPLETE` (no secrets).
4. Asks BEaker for the newest os, services, `@be/be`, `jibo-tbd`, `oobe-config`,
   `jibo-diagnostics` and `fin-goods-test` (as version 0.0.0, filter `fcs`, so the newest
   full package is always offered), checks there is room on `/opt` (all packages plus
   the largest again), sets aside an unfinished update (`/var/jibo/ota.json.old`),
   downloads each to `/opt/ota/pm-<name>.tar` (SHA-1 checked, 5 tries; a finished
   download is reused on a rerun), and queues them all in one `jibo-apply-update`.

Jibo then installs the modded os (BEnch's root filesystem, os 13.0.7, the same version as services: no firewall,
Let's Encrypt root, api.5x1.com, the runtime bootstrap boot hook), BEnch's services
and BEam's skills, rebooting between them with the stock update screens. This works
the same from a factory OOBE os (3.x): the new os's updater finishes the queue. PM no
longer edits the root filesystem by hand, so a later os update keeps the mods.

Any failure before the last step (no offer, too little space, a bad download) exits
before the update is queued; nothing is installed.

The OTA block is the same in all four scripts (between `# >>> 5x1 OTA install` and
`# <<< 5x1 OTA install`); the tests enforce that.

Run fixture tests without BEaker access, firmware flashing, or a real reboot:

```sh
python3 -m pytest -q tests
```

Live OOBE installation, BBFW/reboot behavior, and cloud acceptance of locally
generated credentials still require hardware validation.
