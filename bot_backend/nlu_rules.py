import re
from typing import List, Dict
from .nlu_glossary import GLOSSARY

def _rx(words: List[str]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.I)

RX_INVOICE    = _rx(GLOSSARY["invoice"])
RX_PENDING    = _rx(GLOSSARY["pending"])
RX_DUE        = _rx(GLOSSARY["due"])
RX_THIS_MONTH = _rx(GLOSSARY["this_month"])
RX_TODAY      = _rx(GLOSSARY["today"])
RX_TOP        = _rx(GLOSSARY["top"])
RX_CUSTOMER   = _rx(GLOSSARY["customer"])

def detect_intent(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "help"

    has_invoice = bool(RX_INVOICE.search(t))
    has_pending = bool(RX_PENDING.search(t))
    has_due     = bool(RX_DUE.search(t))
    has_month   = bool(RX_THIS_MONTH.search(t))
    has_today   = bool(RX_TODAY.search(t))
    has_top     = bool(RX_TOP.search(t))
    has_client  = bool(RX_CUSTOMER.search(t))

    # Si hablan de pendientes/vencidos/top sin decir "facturas", asumimos facturas
    if not has_invoice and (has_pending or has_due or has_top):
        has_invoice = True

    score = {"invoices_due_this_month":0, "overdue_today":0, "top_clients_overdue":0}
    if has_invoice and (has_due or has_pending) and has_month:
        score["invoices_due_this_month"] += 5
    if has_invoice and has_month:
        score["invoices_due_this_month"] += 2

    if has_invoice and has_today:
        score["overdue_today"] += 5
    if has_invoice and has_due and has_today:
        score["overdue_today"] += 2

    if has_top and has_client and (has_due or has_pending):
        score["top_clients_overdue"] += 6
    if has_top and has_client:
        score["top_clients_overdue"] += 2

    intent, pts = max(score.items(), key=lambda kv: kv[1])
    return intent if pts > 0 else "help"

