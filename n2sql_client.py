import os, requests
from typing import Dict, Any

N2SQL_URL = os.getenv("N2SQL_URL", "")

class N2SQLClient:
    def __init__(self, base_url: str | None = None, timeout: int = 15):
        self.base_url = base_url or N2SQL_URL
        self.timeout = timeout
        if not self.base_url:
            raise RuntimeError("N2SQL_URL no está definido")

    def query_from_text(self, text: str) -> Dict[str, Any]:
        t = text.lower()
        if any(k in t for k in ["partner","cliente","proveedor"]):
            payload = {"dataset":"partners","intent":"search","params":{"q": text}}
        else:
            # Demo; luego lo reemplazas con NLU-service
            payload = {"dataset":"moves","intent":"expiring","params":{"start":"2025-10-01","end":"2025-10-31"}}

        r = requests.post(f"{self.base_url}/v1/query", json=payload, timeout=self.timeout)
        r.raise_for_status()
        return r.json()
