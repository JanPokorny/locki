"""Sandbox tools: AI harnesses and CLIs, installed once in the VM and shared read-only.

The VM installs every tool with mise (data/tools-sync.sh) under TOOLS_ROOT, which every
sandbox mounts read-only at the same path. The tools' bin folders (`mise bin-paths`) are
appended to the sandbox PATH on each entry, from TOOLS_PATH_FILE. A binary the image ships
itself therefore wins, and repo-level mise inside the sandbox still takes precedence for
anything the repo pins.

Sandboxes never install or update these tools themselves: the host syncs the VM before
entering a sandbox, installing what is missing and upgrading to the newest releases at
most every UPGRADE_INTERVAL, with the host's GitHub token so mise's API calls are not
throttled by the anonymous rate limit.
"""

import base64
import hashlib
import json
import os
import shutil
import time

import click

from locki.paths import LIMA, PACKAGE_DATA, STATE
from locki.runes import SUCCESS, WARNING
from locki.services.daemon import VERSION
from locki.services.vm import vm
from locki.utils import file_lock, run_command

TOOLS_ROOT = "/var/lib/locki/tools"
# `:<dir>...`, appended to the sandbox PATH (read in the VM on each entry, so always current)
TOOLS_PATH_FILE = f"{TOOLS_ROOT}/path"

UPGRADE_INTERVAL = 3600
RETRY_INTERVAL = 300

# The AI harnesses: new models often need their newest release, so they skip mise's default
# 24h minimum_release_age (a supply-chain hold on fresh releases), which every other tool keeps.
HARNESSES = [
    "claude",
    "github:anomalyco/opencode",
    "github:github/copilot-cli",
    "github:google-antigravity/antigravity-cli",
    "npm:@mariozechner/pi-coding-agent",
    "npm:@openai/codex",
]

# mise tool specs, all tracking their newest release
TOOLS = [
    *HARNESSES,
    "node",
    "npm:agent-browser",
    "npm:corepack",
    "bun",
    "fd",
    "github:keilerkonzept/dockerfile-json",
    "jq",
    "k9s",
    "kubectl",
    "pipx:poetry",
    "python",
    "rg",
    "uv",
    "yq",
]

_SYNC_STATE = STATE / "tools-sync.json"


def _mise_toml() -> str:
    return f"[settings]\nminimum_release_age_excludes = {json.dumps(HARNESSES)}\n\n[tools]\n" + "".join(
        f'{json.dumps(spec)} = "latest"\n' for spec in TOOLS
    )


def github_token() -> str:
    """The host's GitHub token, for mise's API calls in the VM: GITHUB_TOKEN / GH_TOKEN, else `gh auth token`."""
    if token := os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        return token.strip()
    if shutil.which("gh"):
        result = run_command(["gh", "auth", "token"], "Reading GitHub token", check=False, quiet=True)
        if result.returncode == 0:
            return result.stdout.decode().strip()
    return ""


class ToolsService:
    def _fingerprint(self) -> str:
        """Changes whenever the VM needs a sync regardless of the upgrade interval: a new tool
        list or sync script (e.g. a Locki upgrade), or a recreated VM."""
        lima_yaml = LIMA / "locki" / "lima.yaml"
        vm_id = str(lima_yaml.stat().st_mtime_ns) if lima_yaml.exists() else ""
        script = (PACKAGE_DATA / "tools-sync.sh").read_bytes()
        return hashlib.sha256(json.dumps([VERSION, vm_id, TOOLS]).encode() + script).hexdigest()

    def _state(self) -> dict:
        try:
            return json.loads(_SYNC_STATE.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def sync(self, force_upgrade: bool = False) -> None:
        """Bring the VM's tools up to date; a no-op (no VM roundtrip) when nothing is due.
        The VM must be running. Failures warn but never block entering a sandbox."""
        with file_lock("tools", "Waiting for another sandbox tools update"):
            fingerprint = self._fingerprint()
            state = self._state()
            now = time.time()
            # a first install already fetches the newest releases
            fresh = not state.get("fingerprint")
            upgrade = force_upgrade or (not fresh and now - state.get("upgraded", 0) >= UPGRADE_INTERVAL)
            if state.get("fingerprint") == fingerprint and not upgrade:
                return

            result = vm.run(
                [
                    "sh",
                    "-c",
                    (PACKAGE_DATA / "tools-sync.sh").read_text(),
                    "tools-sync",
                    base64.b64encode(_mise_toml().encode()).decode(),
                    "upgrade" if upgrade else "",
                ],
                "Installing sandbox tools" if fresh else "Updating sandbox tools",
                input=f"{github_token()}\n".encode(),
                check=False,
                print_success=False,
            )
            upgraded = now if upgrade or fresh else state.get("upgraded", 0)
            if "GitHub rejected the token" in result.stderr.decode(errors="replace"):
                click.echo(
                    f"{WARNING} GitHub rejected your token (GITHUB_TOKEN / GH_TOKEN / `gh auth token`);"
                    " sandbox tools were updated without it, at GitHub's anonymous rate limit.",
                    err=True,
                )
            if result.returncode == 0 and fresh:
                click.echo(f"{SUCCESS} Installed sandbox tools", err=True)
            if result.returncode != 0:
                # Retrying on every entry would stall it while e.g. offline; retry soon instead.
                # A partial failure still leaves the other tools usable.
                upgraded = now - UPGRADE_INTERVAL + RETRY_INTERVAL
                lines = result.stderr.decode(errors="replace").splitlines()
                failed = [line for line in lines if "✗" in line or line.startswith("Sandbox tool folder missing")]
                click.echo(f"{WARNING} Some sandbox tools could not be installed or updated:", err=True)
                for line in dict.fromkeys(failed or lines[-3:]):
                    click.echo(f"     {line.strip()}", err=True)
            _SYNC_STATE.parent.mkdir(parents=True, exist_ok=True)
            _SYNC_STATE.write_text(json.dumps({"fingerprint": fingerprint, "upgraded": upgraded}))


tools = ToolsService()
