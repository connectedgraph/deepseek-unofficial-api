from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "log"


@dataclass(frozen=True)
class ProxySettings:
    fixed_timeout_enabled: bool = False
    fixed_timeout_seconds: int = 60

    cloudflare_wait_enabled: bool = True
    cloudflare_wait_seconds: int = 1

    char_count_enabled: bool = False

    debug_mode_enabled: bool = False

    @property
    def effective_headless(self) -> bool:
        return not self.debug_mode_enabled

    @property
    def effective_verbose(self) -> bool:
        return True


SETTINGS = ProxySettings(
    fixed_timeout_enabled=False,
    fixed_timeout_seconds=60,
    cloudflare_wait_enabled=True,
    cloudflare_wait_seconds=1,
    char_count_enabled=False,
    debug_mode_enabled=False,
)
