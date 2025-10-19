# conectores/bf_msft_comandos.py
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("asistente_sdp.bf_msft")

# MSAL (opcional para diagnósticos)
try:
    import msal  # type: ignore
except Exception:  # pragma: no cover
    msal = None  # type: ignore

# Confiar serviceUrl para enviar mensajes salientes
try:
    from botframework.connector.auth import MicrosoftAppCredentials  # type: ignore
except Exception:  # pragma: no cover
    class MicrosoftAppCredentials:  # type: ignore
        @staticmethod
        def trust_service_url(url: str) -> None:
            pass

SCOPE = ["https://api.botframework.com/.default"]

def acquire_bf_token(app_id: str, app_secret: str, tenant: str) -> Dict[str, Any]:
    """Obtiene un token para Bot Framework con MSAL (client credentials)."""
    if not msal:
        return {"has_access_token": False, "error": "msal_not_available"}
    authority = f"https://login.microsoftonline.com/{tenant}"
    cca = msal.ConfidentialClientApplication(client_id=app_id, client_credential=app_secret, authority=authority)
    res = cca.acquire_token_for_client(scopes=SCOPE)
    out: Dict[str, Any] = {k: v for k, v in res.items() if k != "access_token"}
    out["has_access_token"] = "access_token" in res
    return out

def trust_service_url(url: Optional[str]) -> None:
    if url:
        try:
            MicrosoftAppCredentials.trust_service_url(url)
            logger.info("bf_msft: trusted serviceUrl=%s", url)
        except Exception as e:  # pragma: no cover
            logger.warning("bf_msft: trust_service_url error: %s", e)

def diagnose_activity(activity, env_app_id_tail: Optional[str], token_info: Dict[str, Any]) -> Dict[str, Any]:
    ch = getattr(activity, "channel_id", None)
    su = getattr(activity, "service_url", None)
    recipient = getattr(getattr(activity, "recipient", None), "id", None)
    env_tail = env_app_id_tail
    diag = {
        "channelId": ch,
        "serviceUrl": su,
        "recipientId": recipient,
        "env_app_id_tail": env_tail,
        "token_has_access": token_info.get("has_access_token"),
    }
    return diag

