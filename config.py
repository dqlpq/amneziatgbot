from pydantic_settings import BaseSettings
from pydantic import field_validator


class Settings(BaseSettings):
    # Telegram
    BOT_TOKEN: str
    ADMIN_IDS: list[int]
    BOT_MODE: str = "all"  # "all" или "admin"

    VPN_HOST: str = ""

    # Amnezia API
    AMNEZIA_API_URL: str = "http://127.0.0.1:4001"
    AMNEZIA_API_KEY: str
    AMNEZIA_PROTOCOL: str = "amneziawg2"

    # Database & Security
    DB_PATH: str = "./bot_data.db"
    DB_ENCRYPTION_KEY: str

    # Mini App (Flask)
    MINIAPP_HOST: str = "0.0.0.0"
    MINIAPP_PORT: int = 5000
    MINIAPP_DEV_MODE: bool = False

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admins(cls, v: str | list | int) -> list[int]:
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("BOT_MODE")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        v = v.lower()
        if v not in ("all", "admin"):
            raise ValueError("BOT_MODE должен быть 'all' или 'admin'")
        return v

    @field_validator("AMNEZIA_API_URL")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
