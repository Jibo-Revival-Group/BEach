"""Exercise the external OOBE PM's Python stages on a disposable robot tree."""
import json
import os
from pathlib import Path
import re
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "PM/post-oobe-mod.sh"


@pytest.fixture(autouse=True, params=("post-oobe-mod.sh", "web-post-oobe-mod.sh"))
def pm_variant(request, monkeypatch):
    monkeypatch.setitem(globals(), "SCRIPT", Path(__file__).resolve().parents[1] / "PM" / request.param)


def stages():
    source = SCRIPT.read_text()
    blocks = re.findall(r"^python <<'PY'\n(.*?)^PY$", source, re.M | re.S)
    namespace = {"__name__": "pm_test"}
    for block in blocks:
        exec(compile(block, str(SCRIPT), "exec"), namespace)
    assert "prepare_oobe" in namespace and "finish_oobe" in namespace
    return namespace


@pytest.fixture
def robot(tmp_path):
    def write(p, value):
        target = tmp_path / p.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))
        return target
    # The installer has already changed the original OOBE mode by this point.
    identity = {"name": "Fixture-Robot-Only", "serial_number": "BOJW000000000", "cpuid": "fixture", "wifi_mac": "00:11:22:33:44:55"}
    write("/var/jibo/identity.json", identity)
    write("/var/jibo/mode.json", {"mode": "int-developer"})
    for name in ("CameraModelParamsL.json", "CameraModelParamsR.json", "InterCameraTransform.json"):
        write("/var/jibo/lps/" + name, {"fixture": name})
    write("/usr/local/etc/jibo-jetstream-service.json", {"HubClient": {"override": {
        "hub_port": 443, "hub_hostname": "api.5x1.com", "entrypoint_hostname": "api.5x1.com"}}})
    return tmp_path, write


def no_network(args):
    pytest.fail("Local OOBE preparation must not contact BEefy")


def test_missing_oobe_credentials_are_created_locally_without_changing_factory_files(robot, capsys):
    root, write = robot
    before = {p: p.read_bytes() for p in (root / "var/jibo").rglob("*.json") if p.name != "mode.json"}
    stages()["prepare_oobe"](str(root), no_network)
    creds_path = root / "var/jibo/credentials.json"
    creds = json.loads(creds_path.read_text())
    assert creds["endpoint"] == "http://joap.5x1.com:80" and len(creds["secretAccessKey"]) == 64
    assert creds["accessKeyId"].startswith("local-") and creds["region"] == "api"
    assert creds_path.stat().st_mode & 0o777 == 0o600
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert not list((root / "var/jibo").glob(".pm-local-credentials-*"))
    assert creds["secretAccessKey"] not in capsys.readouterr().out
    stages()["prepare_oobe"](str(root), no_network)
    assert json.loads(creds_path.read_text()) == creds


def test_existing_credentials_are_preserved_without_registration(robot):
    root, write = robot
    write("/var/jibo/credentials.json", {"accessKeyId": "existing", "secretAccessKey": "existing-secret", "keep": "extra"})
    def unexpected(args):
        pytest.fail("Existing credentials must not trigger provisioning")
    stages()["prepare_oobe"](str(root), unexpected)
    value = json.loads((root / "var/jibo/credentials.json").read_text())
    assert value["accessKeyId"] == "existing" and value["secretAccessKey"] == "existing-secret" and value["keep"] == "extra"
    saved = root / "var/jibo/pm-oobe-before-5x1/var/jibo/credentials.json"
    assert json.loads(saved.read_text())["secretAccessKey"] == "existing-secret"


@pytest.mark.parametrize("value", [{}, {"accessKeyId": "key"}, {"accessKeyId": "key", "secretAccessKey": ""}, []])
def test_malformed_existing_credentials_are_not_overwritten(robot, value):
    root, write = robot
    p = write("/var/jibo/credentials.json", value)
    original = p.read_bytes()
    with pytest.raises(RuntimeError, match="incomplete"):
        stages()["prepare_oobe"](str(root), no_network)
    assert p.read_bytes() == original


def test_missing_factory_calibration_blocks_local_preparation(robot):
    root, write = robot
    (root / "var/jibo/lps/CameraModelParamsL.json").unlink()
    with pytest.raises(FileNotFoundError):
        stages()["prepare_oobe"](str(root), no_network)
    assert not (root / "var/jibo/credentials.json").exists()


def test_completion_receipt_requires_normal_mode_and_preserved_calibration(robot, capsys):
    root, write = robot
    functions = stages()
    functions["prepare_oobe"](str(root), no_network)
    with pytest.raises(RuntimeError, match="normal mode"):
        functions["finish_oobe"](str(root))
    write("/var/jibo/mode.json", {"mode": "normal"})
    functions["finish_oobe"](str(root))
    receipt = root / "var/jibo/5x1-pm-oobe-complete.json"
    assert json.loads(receipt.read_text()) == {"schema": 1, "status": "complete", "robot": "Fixture-Robot-Only"}
    assert "5X1_OOBE_PM_COMPLETE" in capsys.readouterr().out
    receipt.unlink()
    write("/var/jibo/lps/CameraModelParamsL.json", {"changed": True})
    with pytest.raises(RuntimeError, match="changed"):
        functions["finish_oobe"](str(root))
    assert not receipt.exists()


def test_local_preparation_does_not_depend_on_network_availability(robot):
    root, write = robot
    def offline(args):
        raise RuntimeError("Network unavailable")
    stages()["prepare_oobe"](str(root), offline)
    assert (root / "var/jibo/credentials.json").exists()


def test_shell_and_embedded_python_stages_are_valid_and_ordered():
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True)
    text = SCRIPT.read_text()
    for delimiter, block in re.findall(r"^python << ?'([A-Z]+)'\n(.*?)^\1$", text, re.M | re.S):
        compile(block, str(SCRIPT), "exec")
    assert text.index("prepare_oobe()") < text.index('rm -f /etc/init.d/S21firewall')
    assert text.index("jibo-setmode normal") < text.index("jibo-bbfw-update") < text.index("finish_oobe()")
    assert text.index('print("5X1_OOBE_PM_COMPLETE")') < text.rindex("\nreboot")
    assert 'if [ -t 1 ]; then clear || true; fi' in text
