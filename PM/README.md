# Post-mod scripts

`post-mod.sh` is the existing PM entry point served at `https://scripts.5x1.com/pm`.
`post-oobe-mod.sh` is the OOBE version, kept outside the BEetle installer.

For a robot originally in OOBE that BEetle has prepared in `int-developer` mode:

```sh
jibo-mount --rw
curl -fsSL -k https://raw.githubusercontent.com/Jibo-Revival-Group/BEach/master/PM/post-oobe-mod.sh -o /tmp/pm.sh
chmod +x /tmp/pm.sh
sh /tmp/pm.sh
```

The OOBE script validates and preserves factory identity and camera calibration,
saves the original configuration under `/var/jibo/pm-oobe-before-5x1`, and
preserves existing credential keys. Only absent credentials trigger BEefy's
prepared OOBE protocol. Malformed existing keys or failed provisioning stop
before skills are changed.

PM owns skills installation, TLS and region configuration, OTA endpoint setup,
SSM backup compatibility, body firmware, normal mode and reboot. The script can
run in a non-interactive SSH session without `TERM`. The original normal PM
script is unchanged.

Before reboot, the OOBE script verifies identity/calibration hashes, credentials,
normal mode and hub configuration. It writes
`/var/jibo/5x1-pm-oobe-complete.json` and prints `5X1_OOBE_PM_COMPLETE` so BEetle
can distinguish a successful reboot from an interrupted run. These receipts
contain no credential secrets.

The raw GitHub URL above serves this script from the repository's `master`
branch. BEetle downloads it at setup time rather than bundling a copy.

Run the fixture tests without contacting BEefy or flashing any hardware:

```sh
python3 -m pytest -q tests/test_post_oobe_mod.py
```

Live OOBE installation and BBFW/reboot behavior still require hardware validation.
