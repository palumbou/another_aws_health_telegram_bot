"""Environment variable loading and validation."""

import pytest

from src.config import Config, ConfigError
from src.health_client import DEFAULT_ENDPOINT
from tests.conftest import RULES_JSON

VALID_ENV = {
    "TABLE_NAME": "health-bot-state",
    "TELEGRAM_CHAT_ID": "-1001234567890",
    "BOT_TOKEN_SECRET_ARN": "arn:aws:secretsmanager:eu-west-1:123456789012:secret:x",
    "ROUTING_RULES": RULES_JSON,
}


def test_valid_environment():
    config = Config.from_env(VALID_ENV)
    assert config.table_name == "health-bot-state"
    assert config.chat_id == "-1001234567890"
    assert len(config.rules) == 2
    assert config.max_messages_per_run == 15
    assert config.state_ttl_days == 90
    assert config.health_endpoint == DEFAULT_ENDPOINT


def test_overrides():
    env = dict(VALID_ENV, MAX_MESSAGES_PER_RUN="5", STATE_TTL_DAYS="30", BOT_NAME="x")
    config = Config.from_env(env)
    assert config.max_messages_per_run == 5
    assert config.state_ttl_days == 30
    assert config.bot_name == "x"


@pytest.mark.parametrize("missing", ["TABLE_NAME", "TELEGRAM_CHAT_ID", "BOT_TOKEN_SECRET_ARN"])
def test_missing_required_variable(missing):
    env = {key: value for key, value in VALID_ENV.items() if key != missing}
    with pytest.raises(ConfigError, match=missing):
        Config.from_env(env)


def test_invalid_routing_rules():
    with pytest.raises(ConfigError, match="ROUTING_RULES"):
        Config.from_env(dict(VALID_ENV, ROUTING_RULES="not json"))


def test_invalid_numbers_are_reported():
    with pytest.raises(ConfigError, match="MAX_MESSAGES_PER_RUN"):
        Config.from_env(dict(VALID_ENV, MAX_MESSAGES_PER_RUN="zero"))
