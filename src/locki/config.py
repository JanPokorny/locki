import functools
import logging
import pathlib
import platform
import tomllib

import pydantic
import tomlkit

from locki.paths import CONFIG, USER_CONFIG
from locki.utils import deep_merge, fail

logger = logging.getLogger(__name__)


@functools.cache
def _arch() -> str:
    match platform.machine().lower():
        case "aarch64" | "arm64":
            return "aarch64"
        case "x86_64" | "x64" | "amd64":
            return "x86_64"
        case arch:
            fail(f"Unsupported architecture: {arch}")


class LockiConfig(pydantic.BaseModel):
    incus_image: dict[str, str] = pydantic.Field({"x86_64": "images:fedora/43", "aarch64": "images:fedora/43"})
    ai_command: str = ""
    ide_command: str = ""

    def get_incus_image(self) -> str:
        if _arch() not in self.incus_image:
            fail(f"No incus_image configured for architecture '{_arch()}'. Available: {', '.join(self.incus_image)}")
        return self.incus_image[_arch()]


def load_config(git_root: pathlib.Path | None, *, skip_auto_setup: bool = False) -> LockiConfig:
    """Load config from user config and repo locki.toml. Repo config wins on conflict.
    *git_root=None* skips repo-specific config (useful when running outside a git repo)."""
    user_data: dict = {}
    if USER_CONFIG.exists():
        try:
            with open(USER_CONFIG, "rb") as f:
                user_data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            logger.warning("Invalid user config %s: %s", USER_CONFIG, e)

    repo_data: dict = {}
    if git_root is not None:
        repo_config_path = git_root / "locki.toml"
        if repo_config_path.exists():
            try:
                with open(repo_config_path, "rb") as f:
                    repo_data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                fail(f"Invalid repo config {repo_config_path}: {e}")

    merged = deep_merge(user_data, repo_data)
    try:
        config = LockiConfig.model_validate(merged)
    except pydantic.ValidationError as e:
        fail(f"Invalid config: {e}")

    if not skip_auto_setup and (not config.ai_command or not config.ide_command):
        from locki.cmd.setup import setup_cmd

        setup_cmd.main([], standalone_mode=False)
        return load_config(git_root, skip_auto_setup=True)

    return config


def save_user_config(key: str, value: object) -> None:
    """Write a top-level key in the user config file."""
    CONFIG.mkdir(parents=True, exist_ok=True)
    data = tomlkit.loads(USER_CONFIG.read_text()) if USER_CONFIG.exists() else tomlkit.document()
    data[key] = value  # pyrefly: ignore[unsupported-operation] -- tomlkit Item supports subscript at runtime
    USER_CONFIG.write_text(tomlkit.dumps(data))
