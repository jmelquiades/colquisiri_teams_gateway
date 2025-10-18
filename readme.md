# CRITERIA DataTalk — Teams Gateway (Python)

Puerta de entrada/salida con Microsoft Teams (Bot Framework). Recibe el texto del usuario, delega la consulta al servicio N2SQL y responde con tabla en Markdown.

## Requisitos
- Python 3.11+
- Credenciales de Bot Framework (App registration)

## Variables de entorno
- MICROSOFT_APP_ID
- MICROSOFT_APP_PASSWORD
- N2SQL_URL (p.ej. https://n2sql-service.onrender.com)
- APP_TZ=America/Lima

## Desarrollo
```bash
pip install -r requirements.txt
python app.py
# POST /api/messages (lo normal es usar el canal de Teams y el servicio en Render)

ls
q
exit
eof
