from typing import Dict, List, Any

def to_markdown_table(result: Dict[str, Any], max_rows: int = 10) -> str:
    cols: List[str] = result.get("columns", [])
    rows: List[List[Any]] = result.get("rows", [])[:max_rows]
    meta = result.get("meta", {})
    elapsed = meta.get("elapsed_ms", "?")
    if not cols:
        return "No se obtuvieron columnas."

    header = " | ".join(cols)
    body = "\n".join(" | ".join(str(v) for v in r) for r in rows) if rows else "(sin resultados)"

    return f"**Resultado**\n```\n{header}\n{body}\n```\n_(~{elapsed} ms)_"
