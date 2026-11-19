# API de notificaciones por correo

La API reutiliza la autenticación OAuth de Gmail del script `2_test_html.py`.

## Instalación

Desde la carpeta del proyecto:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requeriments.txt
```

Coloca el archivo `credentials.json` de Google OAuth dentro de `credentials/`.
La primera solicitud abrirá el navegador para autorizar el envío y guardará el
token en `credentials/token.pickle`.

El remitente puede cambiarse con la variable de entorno `GMAIL_SENDER_EMAIL`.
Por defecto se usa `sergio.bedoya46815@ucaldas.edu.co`.

## Ejecución

```powershell
.\.venv\Scripts\python.exe app.py
```

## Endpoint

`POST http://127.0.0.1:5000/notificar`

Encabezado:

```text
Content-Type: application/json
```

Cuerpo:

```json
{
  "para": "destinatario@ejemplo.com",
  "asunto": "Asunto opcional",
  "contenido": "Hola\nEste es el contenido del correo.",
  "idempotency_key": "clave-unica-opcional"
}
```

- `asunto` es opcional; si se omite, se usa `Notificación`.
- `contenido` debe ser texto plano: se inserta en una plantilla HTML predeterminada (saltos de línea conservados, HTML escapado).
- `idempotency_key` es opcional y permite que el servicio evite procesar/enviar la misma notificación más de una vez (idempotencia de extremo a extremo). Debe ser única por solicitud lógica (p. ej. partido+aficionado+ventana de recordatorio).

### Respuestas e idempotencia

- **Éxito (nuevo envío, con clave):** `200`
  ```json
  { "mensaje": "Correo enviado correctamente.", "id": "id-de-gmail", "duplicado": false, "estado": "sent", "procesada": true }
  ```
- **Duplicada ya procesada/enviada (`sent`):** `200` sin reenviar
  ```json
  { "mensaje": "Notificación ya procesada.", "id": "id-de-gmail", "duplicado": true, "estado": "sent", "procesada": true }
  ```
- **En procesamiento concurrente (`processing`):** `409` (no reenviar, no marcar como procesada)
  ```json
  { "error": "Notificación en procesamiento.", "estado": "processing", "procesada": false, "duplicado": true }
  ```
- **Intento fallido (`failed`):** la clave puede reintentarse de forma controlada; ante fallo responde `500/502` con `estado:"failed"` y `procesada:false`.
- **Sin clave:** mantiene la respuesta/contrato original (`{"mensaje":"Correo enviado correctamente.","id":...}` en éxito).
- **Inválidas:** `400` con `error`. Faltan credenciales OAuth: `500`; fallo de Gmail: `502` (no se reporta como enviada).

> `procesada:true` (HTTP 2xx con `estado:"sent"`) indica que la solicitud fue aceptada/procesada para esa clave (enviada o reconocida como ya enviada), evitando reenvíos. No equivale a una garantía absoluta de entrega efectiva al buzón del destinatario.

## Idempotencia y persistencia (SQLite)

Las claves procesadas se registran en SQLite para evitar duplicados incluso ante reinicios o concurrencia.

- `NOTIFICATION_DB_PATH` (opcional): ruta del archivo SQLite. Por defecto `credentials/notifications.db`. **Debe ser una ubicación persistente y configurable** (p. ej. un volumen montado en despliegues/contenedores); no use rutas temporales.
- `NOTIFICATION_PROCESSING_TTL_SECONDS` (opcional, por defecto `300`): tiempo tras el cual una marca `processing` obsoleta (p. ej. por caída) puede reclamarse para reintento seguro.
