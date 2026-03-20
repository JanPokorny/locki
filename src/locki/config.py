import pathlib
import platform
import sys
import tomllib

import pydantic

from locki.console import console

DEFAULT_INCUS_IMAGES: dict[str, str] = {
    "arm64": "locki-base",
    "amd64": "locki-base",
}


class LockiConfig(pydantic.BaseModel):
    incus_image: dict[str, str] = pydantic.Field(default_factory=lambda: dict(DEFAULT_INCUS_IMAGES))

    def get_incus_image(self) -> str:
        arch = platform.machine()
        if arch not in self.incus_image:
            console.error(
                f"No incus_image configured for architecture '{arch}'. Available: {', '.join(self.incus_image)}"
            )
            sys.exit(1)
        return self.incus_image[arch]


def load_config(git_root: pathlib.Path) -> LockiConfig:
    config_path = git_root / "locki.toml"
    if not config_path.exists():
        return LockiConfig()
    try:
        with open(config_path, "rb") as f:
            return LockiConfig.model_validate(tomllib.load(f))
    except (tomllib.TOMLDecodeError, pydantic.ValidationError) as e:
        console.error(f"Invalid locki.toml: {e}")
        sys.exit(1)
