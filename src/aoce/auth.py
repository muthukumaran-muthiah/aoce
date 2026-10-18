"""Bearer-token checks for the agent API (AOCE-61).

Two separate secrets: Alertmanager holds only the webhook token, so it can open incidents but not read
them; people and scripts use the API key for /incidents and /tools. An unset secret turns its
endpoints off rather than leaving them open.
"""

import logging
import secrets
from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from aoce.config import Settings, get_settings

log = logging.getLogger("aoce")
_bearer = HTTPBearer(auto_error=False)


SecretOf = Callable[[Settings], SecretStr | None]
SECRETS: dict[str, SecretOf] = {"AOCE_WEBHOOK_TOKEN": lambda s: s.webhook_token, "AOCE_API_KEY": lambda s: s.api_key}


def _require(env_var: str):
    secret_of = SECRETS[env_var]

    async def check(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
        expected = secret_of(get_settings())
        if expected is None or not expected.get_secret_value():
            # warn_unset_secrets names the setting once at startup; nothing here for an unauthenticated caller.
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="disabled")
        given = credentials.credentials if credentials else ""
        if not secrets.compare_digest(given.encode(), expected.get_secret_value().encode()):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail="missing or wrong bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return check


def warn_unset_secrets(settings: Settings) -> None:
    """Log each unset secret once at startup rather than on every rejected request (AOCE-69)."""
    for env_var, secret_of in SECRETS.items():
        secret = secret_of(settings)
        if secret is None or not secret.get_secret_value():
            log.warning("%s is not set, so its endpoints are turned off", env_var)


require_webhook_token = _require("AOCE_WEBHOOK_TOKEN")
require_api_key = _require("AOCE_API_KEY")
