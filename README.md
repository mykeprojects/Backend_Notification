# API de notificaciones por correo

La API reutiliza la autenticación OAuth de Gmail del script `2_test_html.py`.

## Instalación

Desde la carpeta del proyecto:

```powershell
.\venv\Scripts\python.exe -m pip install -r requeriments.txt
```

Coloca el archivo `credentials.json` de Google OAuth dentro de `credentials/`.
La primera solicitud abrirá el navegador para autorizar el envío y guardará el
token en `credentials/token.pickle`.

El remitente puede cambiarse con la variable de entorno `GMAIL_SENDER_EMAIL`.
Por defecto se conserva el remitente configurado en el script original.

## Ejecución

```powershell
.\venv\Scripts\python.exe app.py
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
  "contenido": "Hola\nEste es el contenido del correo."
}
```

`asunto` es opcional; si se omite, se usa `Notificación`. `contenido` debe ser
texto plano: la aplicación lo inserta en una plantilla HTML predeterminada.
Los saltos de línea se conservan y cualquier etiqueta HTML enviada como parte
del contenido se muestra como texto, no se interpreta como HTML.

Respuesta exitosa:

```json
{
  "mensaje": "Correo enviado correctamente.",
  "id": "id-del-mensaje-de-gmail"
}
```
