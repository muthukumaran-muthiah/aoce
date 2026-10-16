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


def _require(env_var: str, secret_of: Callable[[Settings], SecretStr | None]):
    async def check(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
        expected = secret_of(get_settings())
        if expected is None or not expected.get_secret_value():
            # Name the setting only in our log, not to an unauthenticated caller.
            log.warning("rejected a request: %s is not set, so this endpoint is turned off", env_var)
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="disabled")
        given = credentials.credentials if credentials else ""
        if not secrets.compare_digest(given.encode(), expected.get_secret_value().encode()):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, detail="missing or wrong bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return check


require_webhook_token = _require("AOCE_WEBHOOK_TOKEN", lambda s: s.webhook_token)
require_api_key = _require("AOCE_API_KEY", lambda s: s.api_key)
