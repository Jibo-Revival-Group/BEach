"""A failed rewrite of credentials.json, the jetstream config or the SSM bundle must leave the original intact."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ["PM/post-mod.sh", "PM/web-post-mod.sh", "PM/post-oobe-mod.sh", "PM/web-post-oobe-mod.sh",
           "Server/5x1-patch.sh", "Server/setup-ota.sh", "Server/point-at-server.sh", "Patch/skip-ota-backup.sh"]
# The robot runs these snippets on Python 2.7.
PYTHON = os.environ.get("PY27") or shutil.which("python2.7") or shutil.which("python2")
NEEDLE = "this._backupHelper((bkError, maxError) => {"


def snippets(script):
    source = (ROOT / script).read_text()
    inline = [s.replace("\\\\", "\\") for s in re.findall(r'python -c "\n(.*?)\n"$', source, re.M | re.S)]
    heredocs = re.findall(r"^python << ?'PY'\n(.*?)^PY$", source, re.M | re.S)
    return [s for s in inline + heredocs if "$CONFIG_FILE" in s or "$CREDS_FILE" in s or NEEDLE in s]


CASES = [(script, n) for script in SCRIPTS for n in range(len(snippets(script)))]


@pytest.mark.skipif(not PYTHON, reason="Python 2.7 not available")
@pytest.mark.parametrize("script,index", CASES)
def test_failed_write_keeps_original_and_success_keeps_mode(script, index, tmp_path):
    code = snippets(script)[index]
    if NEEDLE in code:
        target = tmp_path / "ssm/lib/skills-service-manager.js"
        target.parent.mkdir(parents=True)
        target.write_text("x\n" + NEEDLE + "\n")
        code = code.replace("/usr/local/bin/jibo-ssm", str(tmp_path / "ssm")).replace("/opt/jibo/Jibo/jibo-ssm", str(tmp_path / "none"))
    else:
        target = tmp_path / "file.json"
        target.write_text(json.dumps({"HubClient": {}, "accessKeyId": "AKFIXTURE", "secretAccessKey": "fixture"}))
        for var, value in (("$CONFIG_FILE", target), ("$CREDS_FILE", target), ("$HUB_HOST", "hub.test"),
                           ("$HUB_PORT", "8443"), ("$OTA_ENDPOINT", "http://joap.test:80")):
            code = code.replace(var, str(value))
    target.chmod(0o640)
    original = target.read_bytes()
    (tmp_path / "snippet.py").write_text(code)

    # ulimit -f 0 makes every write fail (EFBIG), like a full disk.
    subprocess.run(["sh", "-c", 'ulimit -f 0; exec "$0" snippet.py', PYTHON], cwd=tmp_path, capture_output=True)
    assert target.read_bytes() == original, "failed rewrite truncated " + target.name
    assert sorted(p.name for p in target.parent.iterdir()) == sorted([target.name] + (["snippet.py"] if target.parent == tmp_path else []))

    done = subprocess.run([PYTHON, "snippet.py"], cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert target.read_bytes() != original
    assert target.stat().st_mode & 0o7777 == 0o640
