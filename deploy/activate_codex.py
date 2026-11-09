"""Activate the tested Codex adapter, retaining a rollback copy."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import subprocess

root = Path("/opt/leadbot")
stage = root / "codex-staging-20261108"
expected = {
    "classifier.py": "fa9dc9a59bf95fa14d9b107bb01ccf47e630db41699e366d0f44297ceea7deef",
    "pending.py": "7597a40f4b81570dc5f247ba302e99b25bf9d640923cb5f790976a37776db091",
}
for name, digest in expected.items():
    assert hashlib.sha256((root / "leadbot" / name).read_bytes()).hexdigest() == digest, name + " changed since staging"
status = subprocess.run(["runuser", "-u", "leadbot", "--", "codex", "login", "status"], capture_output=True, text=True)
assert status.returncode == 0 and "ChatGPT" in status.stdout + status.stderr, "ChatGPT login required"
backup = root / ("backup-before-codex-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
backup.mkdir(mode=0o700)
shutil.copy2(root / ".env", backup / ".env")
for name in expected:
    shutil.copy2(root / "leadbot" / name, backup / name)
units = ["leadbot.timer", "leadbot-retry.timer", "leadbot-radar.service", "leadbot-feedback.service"]
active = [unit for unit in units if subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0]
subprocess.run(["systemctl", "stop", *units, "leadbot.service", "leadbot-retry.service"], check=True)
try:
    for name in [*expected, "codex_backend.py"]:
        shutil.copy2(stage / "leadbot" / name, root / "leadbot" / name)
        shutil.chown(root / "leadbot" / name, user="leadbot", group="leadbot")
    env = root / ".env"
    settings = {"LLM_BACKEND": "codex_cli", "CODEX_CLI": "/usr/bin/codex", "CODEX_CLI_TIMEOUT": "180"}
    lines = [line for line in env.read_text().splitlines() if line.partition("=")[0].strip() not in settings]
    env.write_text("\n".join(lines + [key + "=" + value for key, value in settings.items()]) + "\n")
    subprocess.run([str(root / "venv/bin/python"), "-c", "from leadbot.classifier import provider_list; assert provider_list()[0][0] == 'codex_cli'; print(provider_list())"], cwd=root, check=True)
    subprocess.run(["systemctl", "start", *active], check=True)
except BaseException:
    shutil.copy2(backup / ".env", root / ".env")
    for name in expected:
        shutil.copy2(backup / name, root / "leadbot" / name)
    subprocess.run(["systemctl", "start", *active], check=True)
    raise
print("Activated Codex; backup: " + str(backup))
