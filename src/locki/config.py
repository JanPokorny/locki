import dataclasses
import functools
import logging
import pathlib
import platform
import tomllib

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


_ARCH_HINTS: dict[str, list[str]] = {
    "aarch64": ["arm64", "aarch64", "arm"],
    "x86_64": ["x86_64", "amd64", "x64"],
}


_USER_ONLY_KEYS = frozenset({"ide_command"})


@dataclasses.dataclass
class LockiConfig:
    incus_image: str | dict[str, str] = "images:fedora/43"
    ai_command: str = ""
    ide_command: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "LockiConfig":
        """Build a config from merged TOML data; unknown keys are ignored."""
        config = cls()
        for field in dataclasses.fields(cls):
            if field.name not in data:
                continue
            value = data[field.name]
            if not isinstance(value, str) and not (
                field.name == "incus_image"
                and isinstance(value, dict)
                and all(isinstance(v, str) for v in value.values())
            ):
                fail(
                    f"Invalid config: '{field.name}' must be a string{' or a table of strings' if field.name == 'incus_image' else ''}."
                )
            setattr(config, field.name, value)
        return config

    def get_incus_image(self, repo: pathlib.Path) -> str:
        if isinstance(self.incus_image, dict):
            if _arch() not in self.incus_image:
                fail(
                    f"No incus_image configured for architecture '{_arch()}'. Available: {', '.join(self.incus_image)}"
                )
            return self.incus_image[_arch()]

        matches = sorted(repo.glob(self.incus_image))
        if len(matches) <= 1:
            return str(matches[0].relative_to(repo)) if matches else self.incus_image

        for hint in _ARCH_HINTS.get(_arch(), []):
            arch_matches = [m for m in matches if hint in m.name.lower()]
            if len(arch_matches) == 1:
                return str(arch_matches[0].relative_to(repo))

        fail(f"Ambiguous incus_image glob '{self.incus_image}' for {_arch()}: {[m.name for m in matches]}")


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
            for key in _USER_ONLY_KEYS & repo_data.keys():
                logger.warning(
                    "Ignoring '%s' in repo config %s: it may only be set in user config.", key, repo_config_path
                )
                del repo_data[key]

    config = LockiConfig.from_dict(deep_merge(user_data, repo_data))

    if not skip_auto_setup and (not config.ai_command or not config.ide_command):
        from locki.cmd.setup import setup_cmd

        setup_cmd.main([], standalone_mode=False)
        return load_config(git_root, skip_auto_setup=True)

    return config


def save_user_config(key: str, value: object) -> None:
    """Write a top-level key in the user config file."""
    import tomlkit

    CONFIG.mkdir(parents=True, exist_ok=True)
    data = tomlkit.loads(USER_CONFIG.read_text()) if USER_CONFIG.exists() else tomlkit.document()
    data[key] = value  # pyrefly: ignore[unsupported-operation] -- tomlkit Item supports subscript at runtime
    USER_CONFIG.write_text(tomlkit.dumps(data))
