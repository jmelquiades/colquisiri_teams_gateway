import os

def getenv(name: str, default: str | None = None, secret: bool = False):
    val = os.getenv(name, default)
    if secret and val:
        return "****"
    return val

MICROSOFT_APP_ID       = os.getenv("MICROSOFT_APP_ID", "")
MICROSOFT_APP_PASSWORD = os.getenv("MICROSOFT_APP_PASSWORD", "")
N2SQL_URL              = os.getenv("N2SQL_URL", "")
APP_TZ                 = os.getenv("APP_TZ", "America/Lima")

def public_env_snapshot():
    return {
        "MICROSOFT_APP_ID": getenv("MICROSOFT_APP_ID", secret=True),
        "N2SQL_URL": getenv("N2SQL_URL", default="(unset)"),
        "APP_TZ": getenv("APP_TZ", default="America/Lima"),
    }
