# Teams Gateway (aiohttp + BotBuilder 4.14.x)

Gateway HTTP para Microsoft Teams (endpoint `/api/messages`) usando `aiohttp` y
`BotFrameworkAdapter` **estable**.

## Despliegue en Render

1. **Start Command**: `python app.py`
2. **Environment**:
   - `MICROSOFT_APP_ID` (o `MicrosoftAppId`)
   - `MICROSOFT_APP_PASSWORD` (o `MicrosoftAppPassword`)
   - `MICROSOFT_APP_TENANT_ID` (o `MicrosoftAppTenantId`) — usa tu tenant GUID si es SingleTenant.
   - `MICROSOFT_APP_TYPE` (o `MicrosoftAppType`) — `SingleTenant` o `MultiTenant`
   - (Opcional) `PORT` — Render lo inyecta automáticamente; el código lo respeta.

3. **Rutas de diagnóstico**:
   - `/health` — ping
   - `/diag/env` — snapshot de variables
   - `/diag/msal` — prueba client_credentials contra **tu tenant**
   - `/diag/msal-bf` — prueba client_credentials contra **botframework.com**

## Notas importantes

- Si recibes 401 en el **reply** (durante `send_activity`) pero `/diag/msal-bf` es OK,
  revisa **AppId/Password** y que el **manifest de Teams** apunte exactamente al `MICROSOFT_APP_ID` de este servicio.

- El log `[JWT] iss=... aud=... appid=...` se imprime **solo** para diagnóstico:
  si `appid` viene `None`, el channel normalmente usa `azp` en su lugar. El SDK valida la firma,
  pero el print te ayuda a detectar tokens raros.

- Para cambiar a `uvicorn`, puedes envolver `app` con `uvicorn`:
  `uvicorn app:app --host 0.0.0.0 --port $PORT`. Con esta pila no es necesario; `aiohttp` corre solo.
