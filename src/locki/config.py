import functools
import logging
import pathlib
import platform
import sys
import tomllib

import pydantic
import tomlkit

from locki.paths import CONFIG, USER_CONFIG

logger = logging.getLogger(__name__)


@functools.cache
def _arch() -> str:
    match platform.machine().lower():
        case "aarch64" | "arm64":
            return "aarch64"
        case "x86_64" | "x64" | "amd64":
            return "x86_64"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (override wins for leaf keys)."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class AiConfig(pydantic.BaseModel):
    harness: str | None = None


class LockiConfig(pydantic.BaseModel):
    incus_image: dict[str, str] = pydantic.Field({"x86_64": "images:fedora/43", "aarch64": "images:fedora/43"})
    ai: AiConfig = AiConfig()

    def get_incus_image(self) -> str:
        if _arch() not in self.incus_image:
            logger.error(
                "No incus_image configured for architecture '%s'. Available: %s",
                _arch(),
                ", ".join(self.incus_image),
            )
            sys.exit(1)
        return self.incus_image[_arch()]


def load_config(git_root: pathlib.Path) -> LockiConfig:
    """Load config from user config and repo locki.toml, repo takes precedence."""
    user_data: dict = {}
    if USER_CONFIG.exists():
        try:
            with open(USER_CONFIG, "rb") as f:
                user_data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            logger.warning("Invalid user config %s: %s", USER_CONFIG, e)

    repo_data: dict = {}
    repo_config_path = git_root / "locki.toml"
    if repo_config_path.exists():
        try:
            with open(repo_config_path, "rb") as f:
                repo_data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            logger.error("Invalid repo config %s: %s", repo_config_path, e)
            sys.exit(1)

    merged = _deep_merge(user_data, repo_data)
    try:
        return LockiConfig.model_validate(merged)
    except pydantic.ValidationError as e:
        logger.error("Invalid config: %s", e)
        sys.exit(1)


def save_user_config(section: str, key: str, value: object) -> None:
    """Write a single key under [section] in the user config file."""
    CONFIG.mkdir(parents=True, exist_ok=True)
    data = tomlkit.loads(USER_CONFIG.read_text()) if USER_CONFIG.exists() else tomlkit.document()
    if section not in data:
        data.add(section, tomlkit.table())
    data[section][key] = value
    USER_CONFIG.write_text(tomlkit.dumps(data))
