import functools
import logging
import pathlib
import platform
import sys
import tomllib

import pydantic

logger = logging.getLogger(__name__)

LOCKI_HOME = pathlib.Path.home() / ".locki"
LIMA_HOME = LOCKI_HOME / "lima"
WORKTREES_HOME = LOCKI_HOME / "worktrees"
WORKTREES_META = LOCKI_HOME / "worktrees-meta"
LOG_DIR = LOCKI_HOME / "logs"


@functools.cache
def _arch() -> str:
    match platform.machine().lower():
        case "aarch64" | "arm64":
            return "aarch64"
        case "x86_64" | "x64" | "amd64":
            return "x86_64"


class LockiConfig(pydantic.BaseModel):
    incus_image: dict[str, str] = pydantic.Field({"x86_64": "images:fedora/43", "aarch64": "images:fedora/43"})
    branch_prefix: str = "locki/"

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
    config_path = git_root / "locki.toml"
    if not config_path.exists():
        return LockiConfig()
    try:
        with open(config_path, "rb") as f:
            return LockiConfig.model_validate(tomllib.load(f))
    except (tomllib.TOMLDecodeError, pydantic.ValidationError) as e:
        logger.error("Invalid locki.toml: %s", e)
        sys.exit(1)
