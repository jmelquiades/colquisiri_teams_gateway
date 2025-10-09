# bot_backend/n2sql/generator.py
import re

SAFE_VIEW = "odoo_replica.vw_invoices_semantic"

# --- Util: números en español a entero simple
SPANISH_NUM = {
    "una": 1, "un": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10
}

# --- Día dentro del mes o ventana
DAY_PATTERNS = [
    r"\bd[ií]a\s+(\d{1,2})\b",
    r"\bel\s+(\d{1,2})\b(?:\s+de\s+este\s+mes)?",
    r"\b(\d{1,2})\s+de\s+este\s+mes\b",
    r"\b(\d{1,2})\s+del\s+presente\s+mes\b",
]

# --- Ventanas en semanas: "próximas dos semanas", "siguientes 3 semanas"
WEEKS_PATTERNS = [
    r"\bpr[oó]xim[oa]s?\s+(\d+)\s+semanas?\b",
    r"\bpr[oó]xim[oa]s?\s+(" + "|".join(SPANISH_NUM.keys()) + r")\s+semanas?\b",
    r"\bsiguientes?\s+(\d+)\s+semanas?\b",
    r"\bsiguientes?\s+(" + "|".join(SPANISH_NUM.keys()) + r")\s+semanas?\b",
    r"\bpr[oó]xim[oa]s?\s+semanas?\b",  # sin número => 2
]

# --- Monedas: USD, PEN, EUR (+ sinónimos)
CURRENCY_MAP = {
    "usd": "USD", "dolar": "USD", "dólar": "USD", "dolares": "USD", "dólares": "USD", "us$": "USD",
    "pen": "PEN", "sol": "PEN", "soles": "PEN", "s/": "PEN", "penes": "PEN",  # cuidado con plurales
    "eur": "EUR", "euro": "EUR", "euros": "EUR", "€": "EUR",
}
CURRENCY_PATTERNS = [
    r"\ben\s+(usd|us\$|d[oó]lares?)\b",
    r"\ben\s+(pen|s/|sol(?:es)?)\b",
    r"\ben\s+(eur|euros?)\b",
    r"\b(moneda|currency)\s*=\s*(usd|pen|eur)\b",
    r"\b(usd|pen|eur)\b",
]

# --- Orden: monto/fecha asc/desc
SORT_DESC_PATTERNS = [
    r"\bde\s+mayor\s+a\s+menor\b",
    r"\bdesc(endente)?\b",
    r"\b(m[aá]s\s+altas|mayores)\b",
]
SORT_ASC_PATTERNS = [
    r"\bde\s+menor\s+a\s+mayor\b",
    r"\basc(endente)?\b",
    r"\b(m[aá]s\s+bajas|menores)\b",
]
SORT_FIELD_AMOUNT = [r"\bmonto\b", r"\bimporte\b", r"\bsaldo\b", r"\bresidual\b"]
SORT_FIELD_DATE   = [r"\bfecha\b", r"\bvencimiento\b", r"\bvencen\b", r"\bdue\b"]

# --- Símbolos por ISO
CURRENCY_SYMBOL = {
    "USD": "$",
    "PEN": "S/",
    "EUR": "€",
}

def _search_any(patterns: list[str], text: str):
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE)
        if m:
            return m
    return None

def _extract_day(utterance: str) -> int | None:
    t = (utterance or "").lower()
    for pat in DAY_PATTERNS:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if not m:
            continue
        for g in m.groups():
            if g and g.isdigit():
                d = int(g)
                if 1 <= d <= 31:
                    return d
    return None

def _extract_weeks(utterance: str) -> int | None:
    t = (utterance or "").lower()
    for pat in WEEKS_PATTERNS:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if not m:
            continue
        for g in m.groups():
            if not g:
                continue
            if g.isdigit():
                return max(1, min(12, int(g)))
            g = g.strip().lower()
            if g in SPANISH_NUM:
                return SPANISH_NUM[g]
        return 2  # sin número explícito
    return None

def _extract_sort(utterance: str) -> tuple[str, str] | None:
    t = (utterance or "").lower()
    field = None
    if _search_any(SORT_FIELD_AMOUNT, t):
        field = "amount"
    elif _search_any(SORT_FIELD_DATE, t):
        field = "date"
    if not field:
        return None

    direction = None
    if _search_any(SORT_DESC_PATTERNS, t):
        direction = "DESC"
    elif _search_any(SORT_ASC_PATTERNS, t):
        direction = "ASC"
    else:
        direction = "DESC" if field == "amount" else "ASC"
    return (field, direction)

def _order_by_clause(sort_pair: tuple[str, str] | None) -> str:
    if not sort_pair:
        return "ORDER BY due_date, customer"
    field, direction = sort_pair
    if field == "amount":
        # usamos el alias numérico para ordenar correctamente
        return f"ORDER BY amount_residual_num {direction}, due_date"
    if field == "date":
        return f"ORDER BY due_date {direction}, customer"
    return "ORDER BY due_date, customer"

def _extract_currency(utterance: str) -> str | None:
    t = (utterance or "").lower()
    m = _search_any(CURRENCY_PATTERNS, t)
    if not m:
        return None
    for g in m.groups():
        if not g:
            continue
        key = g.strip().lower()
        if key in CURRENCY_MAP:
            return CURRENCY_MAP[key]
    return None

def _fmt_amount_expr() -> str:
    """
    Devuelve la expresión SQL para mostrar:
    <símbolo> <monto formateado con coma de miles y punto decimal>
    """
    # to_char con patrón US: 999,999,999,990.00
    fmt = "FM999,999,999,990.00"
    # prefijo símbolo según moneda
    symbol_case = (
        "CASE currency "
        "WHEN 'USD' THEN '$' "
        "WHEN 'PEN' THEN 'S/' "
        "WHEN 'EUR' THEN '€' "
        "ELSE currency END"
    )
    return f"{symbol_case} || ' ' || to_char(amount_residual, '{fmt}')"

def generate_sql(intent: str, utterance: str) -> str:
    i = (intent or "").lower()

    # --- Modificadores
    day     = _extract_day(utterance)
    weeks   = _extract_weeks(utterance)          # si está -> ventana relativa
    sorting = _extract_sort(utterance)
    orderby = _order_by_clause(sorting)
    curr    = _extract_currency(utterance)       # filtro por moneda

    # --- Filtros base
    where_parts = ["is_pending"]

    if weeks:  # ventana relativa desde hoy
        where_parts.append("due_date >= CURRENT_DATE")
        where_parts.append(f"due_date < CURRENT_DATE + INTERVAL '{weeks * 7} days'")
    else:
        # por defecto, mes actual
        where_parts.append("date_trunc('month', due_date) = date_trunc('month', CURRENT_DATE)")

    if day:
        where_parts.append(f"EXTRACT(DAY FROM due_date) = {day}")

    if curr:
        where_parts.append(f"currency = '{curr}'")

    where_sql = " AND ".join(where_parts)
    amount_fmt = _fmt_amount_expr()

    if i in ("invoices_due_this_month", "vencen_mes", "facturas_vencen_mes"):
        # NOTA: exponemos ambas: numérica (para ordenar) y formateada (para mostrar)
        return f"""
        SELECT
          invoice_number,
          customer,
          due_date,
          amount_residual AS amount_residual_num,
          {amount_fmt} AS amount_residual,
          currency
        FROM {SAFE_VIEW}
        WHERE {where_sql}
        {orderby}
        """

    if i in ("overdue_today","vencidas_hoy"):
        # vencidas (no aplicamos weeks/day aquí)
        return f"""
        SELECT
          invoice_number,
          customer,
          due_date,
          days_overdue,
          amount_residual AS amount_residual_num,
          {amount_fmt} AS amount_residual,
          currency
        FROM {SAFE_VIEW}
        WHERE is_overdue
        ORDER BY days_overdue DESC
        """

    if i in ("top_clients_overdue", "top_clientes_vencido"):
        # top por saldo vencido (sumado y formateado)
        # Si filtran por moneda, aplica aquí también.
        where_over = "is_overdue" + (f" AND currency = '{curr}'" if curr else "")
        return f"""
        SELECT
          customer,
          SUM(amount_residual) AS overdue_balance_num,
          (CASE
             WHEN COALESCE(MAX(currency),'') IN ('USD','PEN','EUR') THEN
               (CASE COALESCE(MAX(currency),'')
                 WHEN 'USD' THEN '$' WHEN 'PEN' THEN 'S/' WHEN 'EUR' THEN '€' ELSE COALESCE(MAX(currency),'') END)
             ELSE COALESCE(MAX(currency),'')
           END)
           || ' ' || to_char(SUM(amount_residual), 'FM999,999,999,990.00') AS overdue_balance,
          COALESCE(MAX(currency),'') AS currency
        FROM {SAFE_VIEW}
        WHERE {where_over}
        GROUP BY customer
        ORDER BY overdue_balance_num DESC
        LIMIT 10
        """

    # fallback seguro
    return f"SELECT * FROM {SAFE_VIEW} LIMIT 50"

