from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # JWT
    secret_key: str = "change-me-before-first-run"
    algorithm: str = "HS256"
    token_expire_minutes: int = 480  # 8 hours

    # Single admin user — set password_hash via .env or environment
    admin_username: str = "admin"
    admin_password_hash: str = ""   # bcrypt hash; see setup.sh

    # Paths (on cam2)
    test_recordings_dir: Path = Path("/media/ab-ivnc/hc2_data/test_recordings")
    recordings_dir:      Path = Path("/media/ab-ivnc/hc2_data/recordings")
    camera_names_file:    Path = Path("/home/ab-ivnc/homecagev3/camera_names.json")
    camera_settings_file: Path = Path("/home/ab-ivnc/homecagev3/camera_settings.json")

    class Config:
        env_file = ".env"


settings = Settings()
