"""Configuration module copied from original `user_logictic_assistant`.
No functional changes are required for this example; it's kept for parity.
"""

import logging
import os

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


class AgentModel(BaseModel):
    name: str = Field(default="customer_service_coordinator")
    model: str = Field(default="anthropic/claude-sonnet-4-5-20250929")
    # model: str = Field(default="anthropic/claude-3-haiku-20240307") 


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.env"),
        env_prefix="GOOGLE_",
        case_sensitive=True,
        extra="ignore",
    )
    agent_settings: AgentModel = Field(default=AgentModel())
    app_name: str = "customer_service_app"
    CLOUD_PROJECT: str = Field(default="my_project")
    CLOUD_LOCATION: str = Field(default="us-central1")
    GENAI_USE_VERTEXAI: str = Field(default="1")
    API_KEY: str | None = Field(default="")
    ANTHROPIC_API_KEY: str | None = Field(default="", env_prefix="")
    OPENAI_API_KEY: str | None = Field(default="", env_prefix="")

    def get_model_for_agent(self, model_override: str | None = None):
        from google.adk.models.lite_llm import LiteLlm

        model_name = model_override or self.agent_settings.model
        if model_name.startswith(("anthropic/", "openai/")):
            return LiteLlm(model=model_name)
        return model_name

    def get_check_attachments_response(self):
        return {
            "damage_level": "minor",
            "missing_parts": False,
        }
