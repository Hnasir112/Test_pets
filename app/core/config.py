from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    API_SECRET_KEY: str
    ENVIRONMENT: str = "development"

    class Config:
        env_file = ".env"


settings = Settings()
