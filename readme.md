Estructura

Teams Gateway (servicio A)

Asistente / Backend (servicio B)

Flujo punta a punta

Qué se mantiene en cada capa (reglas de oro)

Dóndé modificar qué (chuleta rápida)

Variables de entorno y endpoints de diagnóstico

Playbooks comunes (paso a paso)

Siguientes mejoras sugeridas

1) Teams Gateway (A)

Rol: Puerta de entrada/salida con Bot Framework (Web Chat/Teams).

Hace:

Autentica y valida el mensaje entrante de Bot Service (token JWT).

Extrae texto, usuario, canal y contexto.

Enruta de forma muy ligera (reglas simples) o delega 100% al backend.

Llama al backend POST /n2sql/run con {user, intent, utterance}.

Formatea la respuesta a Markdown y la envía al canal (ConnectorClient).

Observabilidad: /health, /diag/env, /diag/msal, /diag/sdk-token.

Archivos clave (Gateway):

bot_app.py (todo el flujo del gateway)

requirements.txt (SDK BF 4.14.3, FastAPI, httpx, msal)

(Opcional) pequeñas utilidades de presentación (orden/ocultar columnas en Markdown).

2) Asistente / Backend (B)

Rol: Cerebro de negocio y datos (intenciones, SQL, guardrails, auditoría, DB).

Hace:

Intents & NLU (puede ser simple o avanzado): decide qué consulta correr.

N2SQL: mapea intención/utterance → SQL segura (plantillas).

Guardrails: solo SELECT, vista segura, LIMIT, validaciones.

Ejecución SQL y formateos (símbolo de moneda, miles, fecha…).

Auditoría de consultas (quién, qué, cuánto tardó, errores).

ETL / Vista semántica: el dataset “amigable” para preguntas.

Archivos clave (Backend):

app/main.py y app/routers/*.py (FastAPI y rutas)

bot_backend/n2sql/generator.py (plantillas SQL por intención)

bot_backend/n2sql/guardrails.py (seguridad de SQL)

bot_backend/n2sql/endpoint.py (endpoint /n2sql/run)

bot_backend/intents/* (registro/router de intenciones)

bot_backend/vocabulary/* (sinónimos/lexicón si usas NLU simple)

infra/db.py, infra/audit.py, infra/config.py (DB, logs, settings)

sql/*.sql (schemas auxiliares, docs)

tests/* (pruebas mínimas)

3) Flujo punta a punta

Usuario escribe en Teams (ej. “pásame las facturas que vencen este mes”).

Teams → Bot Service envía POST /api/messages al Gateway.

Gateway valida token entrante, toma text, arma payload → Backend /n2sql/run.

Backend:

decide intención (si le delegas NLU) o usa la que llegó,

genera SQL con generator.py,

pasa por guardrails,

ejecuta en Postgres,

formatea (moneda/fecha),

audita y responde {columns, rows, summary, sql, stats}.

Gateway arma Markdown y responde al canal (ConnectorClient).

Usuario ve la tabla ya formateada.

4) Qué se mantiene en cada capa (reglas de oro)

Gateway = capa del canal

Autenticación BF, recibir y responder mensajes.

Presentación mínima: orden u ocultar columnas “técnicas”.

No mete lógica de negocio ni SQL.

Backend = capa de negocio y datos

Intenciones, sinónimos, plantillas SQL, formateos, seguridad, auditoría.

Aquí está el “qué” y “cómo” obtener/transformar resultados.

5) Dónde modificar qué (chuleta rápida)
Quiero cambiar…	¿Dónde?	Archivo(s) típicos
Que entienda nuevas frases/sinónimos	Backend	bot_backend/vocabulary/lexicon.py o NLU avanzado
Agregar una intención (ej. “facturas del día 13”)	Backend	bot_backend/intents/registry.py + generator.py
Cambiar SQL/orden/agrupación/limit	Backend	bot_backend/n2sql/generator.py
Forzar SELECT seguro, FROM vista segura, LIMIT	Backend	bot_backend/n2sql/guardrails.py
Formato de moneda, miles y decimales	Backend	generator.py (via to_char/CASE)
Formato de fechas	Backend	generator.py (SQL) o salida antes de responder
Columnas visibles en Teams (ocultar _num)	Gateway	bot_app.py (helper de columnas y markdown)
Diags de BF/MSAL	Gateway	/diag/* en bot_app.py
Cambiar vista semántica/ETL	Backend	sql/docs_vista.md, etl/*
Auditoría de consultas	Backend	infra/audit.py, sql/schema_audit.sql
Token entrante/saliente BF	Gateway	bot_app.py (adapter, ConnectorClient)
6) Variables & diags útiles

Gateway (A):

MICROSOFT_APP_ID, MICROSOFT_APP_PASSWORD

MicrosoftAppType (SingleTenant/MultiTenant), MicrosoftAppTenantId

BACKEND_URL (URL del Backend)

Endpoints: /health, /diag/env, /diag/msal, /diag/sdk-token

Backend (B):

PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD, PG_SSLMODE

APP_TZ (ej. America/Lima)

Endpoints: /health, /n2sql/run

7) Playbooks comunes (paso a paso)
A) “Quiero que entienda una nueva manera de pedir lo mismo”

En Backend, añade sinónimos en vocabulary/lexicon.py o ajusta tu NLU.

(Opcional) actualiza intents/registry.py si implica nueva etiqueta de intención.

Redeploy Backend. Gateway queda igual.

B) “Quiero una nueva intención con su SQL”

Backend:

intents/registry.py: registra {"mi_intent": "Descripción"}.

n2sql/generator.py: añade el if i in (...): return """SELECT ...""".

(Opcional) tests en tests/.

Redeploy Backend. Gateway queda igual.

C) “Quiero mejorar la presentación en Teams (ocultar columnas técnicas)”

Gateway: en bot_app.py, ajusta la lista de columnas antes de renderizar Markdown.

Redeploy Gateway. Backend queda igual.

D) “Quiero cambiar formato de moneda/fecha”

Backend: en generator.py, usa to_char() y CASE (como ya hiciste con moneda).

Redeploy Backend.

E) “Problema de tokens/403/401 con Teams”

Gateway: revisa /diag/msal y /diag/sdk-token.

Verifica APP_ID/APP_PWD y tenant/config tipo app.

Reintenta; si el backend responde por curl pero el canal no, el foco es Gateway.

8) Siguientes mejoras sugeridas

Intent NLU backend: pasar a “modo backend” para que B detecte intención y parámetros (día, rango, orden, top N) desde utterance.

Parámetros (ej. “día 13”, “próximas dos semanas”): extraer en backend y aplicar en WHERE y ORDER BY.

Formateo de fechas a DD/MM/YYYY en la salida de SQL.

Resúmenes narrativos (opcional) encima de la tabla (ej. “5 facturas por $4, +2 vencen el 13”).
