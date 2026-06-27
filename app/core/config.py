from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    API_SECRET_KEY: str          # used as the admin master key
    ENVIRONMENT: str = "development"

    model_config = {"env_file": ".env"}


settings = Settings()
