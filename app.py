import os
import re
import logging
from typing import Any, Dict, Optional

import aiohttp
from fastapi import FastAPI, Body, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("teams-gateway-lite")

app = FastAPI(title="Gateway (NLU/N2SQL mode)", version="1.0.0")

# ---------- utils ----------
SENSITIVE = {"OPENAI_API_KEY", "N2SQL_API_KEY"}

def _mask(v: Optional[str], key: str) -> str:
    if v is None:
        return "MISSING"
    if key in SENSITIVE:
        return f"SET({v[:3]}***{v[-2:]})" if len(v) >= 6 else "SET(***masked***)"
    return v

def diag_env() -> Dict[str, str]:
    keys = [
        "APP_TZ",
        "N2SQL_URL",
        "N2SQL_API_KEY",
        "OPENAI_API_KEY",
        "PORT",
    ]
    return {k: _mask(os.getenv(k), k) for k in keys}

# ---------- health/diag ----------
@app.get("/health")
def health():
    log.info("Health check solicitado.")
    return {"ok": True}

@app.get("/diag/env")
def diag_env_route():
    return diag_env()

@app.get("/")
def root():
    return {"service": "Gateway NLU/N2SQL", "ok": True}

# ---------- NLU (reglas simples) ----------
class NLUIn(BaseModel):
    text: str

def simple_nlu(text: str) -> Dict[str, Any]:
    t = text.strip()
    low = t.lower()

    # intent heurístico
    if re.search(r"\b(hola|hello|buenas)\b", low):
        intent, conf = "greet", 0.95
    elif re.search(r"\b(incidente|ticket|reporte|case)\b", low):
        intent, conf = "create_incident", 0.85
    elif re.search(r"\b(saldo|balance|cuenta)\b", low):
        intent, conf = "check_balance", 0.80
    elif re.search(r"\b(ayuda|help|soporte)\b", low):
        intent, conf = "help", 0.80
    else:
        intent, conf = "fallback", 0.20

    # entidades heurísticas
    entities: Dict[str, Any] = {}
    nums = re.findall(r"\b\d{5,}\b", t)
    if nums:
        entities["numbers"] = nums
    emails = re.findall(r"[\w\.-]+@[\w\.-]+\.\w+", t)
    if emails:
        entities["emails"] = emails
    dates = re.findall(r"\b(?:\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b", t)
    if dates:
        entities["dates"] = dates

    return {
        "text": t,
        "intent": intent,
        "confidence": conf,
        "entities": entities,
    }

@app.post("/nlu/parse")
async def nlu_parse(inp: NLUIn):
    return {"engine": "rule-based", "result": simple_nlu(inp.text)}

@app.get("/nlu/demo")
def nlu_demo(q: str = "hola, quiero abrir un incidente 12345 para juan@acme.com"):
    return {"engine": "rule-based", "result": simple_nlu(q)}

# ---------- N2SQL ----------
class N2SQLIn(BaseModel):
    question: str
    passthrough: bool = True  # si True y hay N2SQL_URL, reenvía

def heuristic_sql(q: str) -> str:
    l = q.lower()
    if "incidente" in l or "ticket" in l or "reporte" in l:
        return (
            "SELECT id, title, status, created_at "
            "FROM incidents "
            "WHERE created_at >= CURRENT_DATE - INTERVAL '30 days' "
            "ORDER BY created_at DESC LIMIT 50;"
        )
    if "saldo" in l or "balance" in l:
        return (
            "SELECT account_id, balance FROM accounts "
            "WHERE account_id = :account_id;  -- TODO: pasar account_id"
        )
    if "usuarios" in l and ("activos" in l or "actives" in l):
        return "SELECT id, email, last_login FROM users WHERE is_active = true;"
    return "-- sin mapeo heurístico; amplía reglas o usa N2SQL_URL"

@app.post("/n2sql/run")
async def n2sql_run(inp: N2SQLIn):
    url = os.getenv("N2SQL_URL")
    api_key = os.getenv("N2SQL_API_KEY")

    if inp.passthrough and url:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {"question": inp.question}

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload, headers=headers, timeout=20) as r:
                    # no asumimos content-type exacto
                    try:
                        data = await r.json(content_type=None)
                    except Exception:
                        data = {"raw": await r.text()}
                    return {
                        "mode": "passthrough",
                        "upstream_status": r.status,
                        "data": data,
                    }
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"n2sql upstream error: {e}")

    # fallback heurístico si no hay N2SQL_URL o passthrough=False
    return {"mode": "heuristic", "sql": heuristic_sql(inp.question)}

@app.get("/n2sql/demo")
def n2sql_demo(q: str = "listar incidentes abiertos este mes"):
    return {"question": q, "sql": heuristic_sql(q)}
