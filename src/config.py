"""Read and validate configuration from environment variables.

All configuration is injected by CloudFormation as Lambda environment
variables, except the bot token which lives in Secrets Manager and is
fetched at runtime (never through an environment variable).
"""

import os
from dataclasses import dataclass

from src import routing
from src.health_client import DEFAULT_ENDPOINT


class ConfigError(Exception):
    """One or more environment variables are missing or malformed."""


@dataclass(frozen=True)
class Config:
    table_name: str
    chat_id: str
    rules: tuple  # tuple[RoutingRule, ...]
    bot_token_secret_arn: str
    max_messages_per_run: int = 15
    state_ttl_days: int = 90
    bot_name: str = "another-aws-health-telegram-bot"
    health_endpoint: str = DEFAULT_ENDPOINT

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        errors = []

        table_name = env.get("TABLE_NAME", "").strip()
        if not table_name:
            errors.append("TABLE_NAME is required")

        chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
        if not chat_id:
            errors.append("TELEGRAM_CHAT_ID is required")

        secret_arn = env.get("BOT_TOKEN_SECRET_ARN", "").strip()
        if not secret_arn:
            errors.append("BOT_TOKEN_SECRET_ARN is required")

        rules = ()
        try:
            rules = routing.parse_rules(env.get("ROUTING_RULES", ""))
        except ValueError as exc:
            errors.append(str(exc))

        max_messages = _positive_int(env, "MAX_MESSAGES_PER_RUN", 15, errors)
        ttl_days = _positive_int(env, "STATE_TTL_DAYS", 90, errors)

        if errors:
            raise ConfigError("; ".join(errors))

        return cls(
            table_name=table_name,
            chat_id=chat_id,
            rules=rules,
            bot_token_secret_arn=secret_arn,
            max_messages_per_run=max_messages,
            state_ttl_days=ttl_days,
            bot_name=env.get("BOT_NAME", "").strip() or cls.bot_name,
            health_endpoint=env.get("HEALTH_ENDPOINT", "").strip() or DEFAULT_ENDPOINT,
        )


def _positive_int(env, key, default, errors):
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        errors.append(f"{key} must be a positive integer, got {raw!r}")
        return default
