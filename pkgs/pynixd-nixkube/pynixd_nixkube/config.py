from pydantic_settings import BaseSettings, SettingsConfigDict


class NixkubeCentralSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PYNIXD_")

    kube_namespace: str | None = None
    builder_max: int = 3
    builder_min: int = 1
    idle_timeout: int = 300
    systems: str = "x86_64-linux"
    # Seconds a builder may take to become Ready before the manager gives up
    # on it. A warm node reaches Ready in 20 to 30 seconds; this is 10 minutes
    # so a cold image pull on a slow node still clears it.
    builder_startup_timeout: int = 600
    # Longest delay between retries after builders of one system fail one
    # after another. The delay doubles from 30 seconds up to this.
    builder_backoff_cap: int = 600
