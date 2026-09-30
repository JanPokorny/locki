"""Sandbox tools: AI harnesses and CLIs, installed once in the VM and shared read-only.

The VM installs every tool with mise (data/tools-sync.sh) into TOOLS_ROOT, mise's default
system data dir, which every sandbox mounts read-only at the same path, and writes TOOLS_CONFIG:
the sandboxes' mise system config, pinning the exact versions installed. There are no shims:
every bash in a sandbox, the one running the entry command included, loads the mise environment
of its working directory (container-setup.sh), so the tools are on PATH, and a repo's own pins
take precedence. mise checks TOOLS_ROOT/installs by itself, so a pin matching a VM install uses
it in place; a pin of anything else installs into the sandbox on first use.

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

TOOLS_ROOT = "/usr/local/share/mise"
# the sandboxes' MISE_SYSTEM_CONFIG_FILE, rewritten by every sync (read on each prompt, so always current)
TOOLS_CONFIG = f"{TOOLS_ROOT}/locki/tools.toml"
# holds just `mise`, at the end of the sandbox PATH
TOOLS_BIN = f"{TOOLS_ROOT}/locki/bin"

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

# Pinned resolutions (URL + checksum) for tools the GitHub API keeps from installing, see
# tools-sync.sh. Regenerate with `mise run lock-tools`; versions are as old as that run.
_FALLBACK_LOCK = PACKAGE_DATA / "tools.lock"


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
        script = (PACKAGE_DATA / "tools-sync.sh").read_bytes() + _FALLBACK_LOCK.read_bytes()
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
                    base64.b64encode(_FALLBACK_LOCK.read_bytes()).decode(),
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
            for line in result.stderr.decode(errors="replace").splitlines():
                if line.startswith("Installed from Locki pinned versions"):
                    click.echo(f"{WARNING} {line}; they update once the API works again.", err=True)
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
