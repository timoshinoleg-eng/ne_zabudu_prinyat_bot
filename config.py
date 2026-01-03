"""
Конфигурация бота с валидацией
"""
import os
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field, validator

class Settings(BaseSettings):
    """Настройки приложения"""
    
    # Bot
    BOT_TOKEN: str = Field(..., env="BOT_TOKEN")
    MANAGER_ID: int = Field(834868627, env="MANAGER_ID")
    TIMEZONE: str = Field("Europe/Moscow", env="TIMEZONE")
    
    # OpenRouter AI
    OPENROUTER_API_KEY: str = Field("", env="OPENROUTER_API_KEY")
    AI_TEXT_MODEL: str = "qwen/qwen-3-coder-480b-instruct:free"
    AI_IMAGE_MODEL: str = "google/gemini-2.5-flash-image-preview:free"
    
    # Redis
    REDIS_HOST: str = Field("localhost", env="REDIS_HOST")
    REDIS_PORT: int = Field(6379, env="REDIS_PORT")
    REDIS_DB: int = Field(0, env="REDIS_DB")
    
    # Features
    ENABLE_AI_FEATURES: bool = Field(True, env="ENABLE_AI_FEATURES")
    ENABLE_MORNING_MOTIVATION: bool = Field(True, env="ENABLE_MORNING_MOTIVATION")
    
    # Rate Limits
    AI_DAILY_LIMIT_ASK: int = Field(5, env="AI_DAILY_LIMIT_ASK")
    AI_DAILY_LIMIT_IMAGE: int = Field(3, env="AI_DAILY_LIMIT_IMAGE")
    
    # Scheduler Settings
    MORNING_MOTIVATION_TIME: str = "08:00"
    REPORT_DAY: int = 0  # 0 = Понедельник
    REPORT_HOUR: int = 9
    REPORT_MINUTE: int = 0
    SOS_ALERT_HOURS: int = 2
    
    @validator("BOT_TOKEN")
    def validate_bot_token(cls, v):
        if not v or v == "your_telegram_bot_token_here":
            raise ValueError("BOT_TOKEN must be set in environment variables")
        return v
    
    @validator("OPENROUTER_API_KEY")
    def validate_openrouter_key(cls, v, values):
        if values.get("ENABLE_AI_FEATURES") and not v:
            raise ValueError("OPENROUTER_API_KEY required when AI features enabled")
        return v
    
    class Config:
        env_file = ".env"
        case_sensitive = False

# Singleton
config = Settings()
