from __future__ import annotations

import base64
import html
import os
import pickle
from email.mime.text import MIMEText
from email.utils import parseaddr
from threading import Lock
from typing import Any

from flask import Flask, jsonify, request
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError


SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_DIR = os.path.join(BASE_DIR, "credentials")
CLIENT_SECRET_FILE = os.path.join(CREDENTIALS_DIR, "credentials.json")
TOKEN_FILE = os.path.join(CREDENTIALS_DIR, "token.pickle")
SENDER_EMAIL = os.getenv(
    "GMAIL_SENDER_EMAIL", "michael.ramirez63421@autonoma.edu.co"
)

app = Flask(__name__)
_service: Resource | None = None
_service_lock = Lock()


def authenticate_gmail() -> Any:
    """Obtiene y persiste las credenciales OAuth de Gmail."""
    credentials = None

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "rb") as token_file:
            credentials = pickle.load(token_file)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_FILE):
                raise FileNotFoundError(
                    f"No se encontró el archivo de credenciales: {CLIENT_SECRET_FILE}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET_FILE, SCOPES
            )
            credentials = flow.run_local_server(port=0)

        os.makedirs(CREDENTIALS_DIR, exist_ok=True)
        with open(TOKEN_FILE, "wb") as token_file:
            pickle.dump(credentials, token_file)

    return credentials


def get_gmail_service() -> Resource:
    """Inicializa Gmail una sola vez y reutiliza el servicio entre solicitudes."""
    global _service

    if _service is None:
        with _service_lock:
            if _service is None:
                _service = build("gmail", "v1", credentials=authenticate_gmail())

    return _service


def create_message_html(
    sender: str, recipient: str, subject: str, html_content: str
) -> dict[str, str]:
    message = MIMEText(html_content, "html", "utf-8")
    message["to"] = recipient
    message["from"] = sender
    message["subject"] = subject
    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {"raw": encoded_message}


def build_notification_html(content: str) -> str:
    """Construye la plantilla del correo usando contenido de texto seguro."""
    escaped_content = html.escape(content).replace("\n", "<br>\n")
    return f"""\
<!DOCTYPE html>
<html lang="es" xmlns="http://www.w3.org/1999/xhtml">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="x-apple-disable-message-reformatting">
    <title>Sports System</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f3f6f8; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; color: #17212b;">
    <div style="display: none; max-height: 0; overflow: hidden; opacity: 0;">
        Tu código de verificación de Sports System está listo.
    </div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f3f6f8; margin: 0; padding: 28px 12px;">
        <tr>
            <td align="center">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width: 600px;">
                    <tr>
                        <td style="padding: 0 0 18px 4px;">
                            <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                                <tr>
                                    <td style="width: 42px; height: 42px; border-radius: 12px; background-color: #18a66a; color: #ffffff; font-size: 22px; font-weight: 800; line-height: 42px; text-align: center;">
                                        S
                                    </td>
                                    <td style="padding-left: 11px; color: #17212b; font-size: 18px; font-weight: 750; letter-spacing: -0.2px;">
                                        Sports System
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    <tr>
                        <td style="background-color: #ffffff; border: 1px solid #dfe7ec; border-radius: 14px; box-shadow: 0 6px 20px rgba(23, 33, 43, 0.06); overflow: hidden;">
                            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                                <tr>
                                    <td style="height: 6px; background-color: #18a66a; font-size: 0; line-height: 0;">&nbsp;</td>
                                </tr>
                                <tr>
                                    <td style="padding: 38px 42px 12px;">
                                        <p style="margin: 0 0 10px; color: #18a66a; font-size: 12px; font-weight: 800; letter-spacing: 1.4px; text-transform: uppercase;">
                                            Seguridad de tu cuenta
                                        </p>
                                        <h1 style="margin: 0; color: #17212b; font-size: 28px; line-height: 1.25; letter-spacing: -0.6px;">
                                            Verifica tu identidad
                                        </h1>
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding: 0 42px 26px; color: #52616d; font-size: 15px; line-height: 1.6;">
                                        Usa el siguiente código para continuar de forma segura en Sports System. Este código es personal y tiene una vigencia limitada.
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding: 0 42px 30px;">
                                        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f6f9fa; border: 1px solid #dfe7ec; border-radius: 10px;">
                                            <tr>
                                                <td style="padding: 24px 18px; color: #17212b; font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', monospace; font-size: 24px; font-weight: 700; letter-spacing: 4px; line-height: 1.4; text-align: center; word-break: break-word;">
                                                    {escaped_content}
                                                </td>
                                            </tr>
                                        </table>
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding: 0 42px 36px; border-bottom: 1px solid #edf1f3; color: #71808b; font-size: 13px; line-height: 1.6;">
                                        Si no solicitaste este código, puedes ignorar este mensaje. Tu cuenta permanece protegida.
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding: 22px 42px 26px; color: #9aa6ae; font-size: 12px; line-height: 1.5;">
                                        Este es un mensaje automático de Sports System. Por favor, no respondas a este correo.
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 20px 8px 0; color: #9aa6ae; font-size: 11px; line-height: 1.5; text-align: center;">
                            © Sports System · Organiza tu juego, alcanza tus metas.
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>
"""


def send_notification(
    service: Resource,
    recipient: str,
    content: str,
    subject: str,
) -> str:
    html_content = build_notification_html(content)
    message = create_message_html(SENDER_EMAIL, recipient, subject, html_content)
    sent_message = (
        service.users()
        .messages()
        .send(userId="me", body=message)
        .execute()
    )
    return str(sent_message["id"])


def is_valid_email(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    _, address = parseaddr(value)
    return address == value.strip() and "@" in address and "." in address.rsplit("@", 1)[-1]


@app.post("/notificar")
def notify() -> tuple[Any, int]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="El cuerpo debe ser un objeto JSON."), 400

    recipient = data.get("para")
    content = data.get("contenido")
    subject = data.get("asunto", "Notificación")

    if not is_valid_email(recipient):
        return jsonify(error="El campo 'para' debe ser un correo válido."), 400
    if not isinstance(content, str) or not content.strip():
        return jsonify(error="El campo 'contenido' es obligatorio."), 400
    if not isinstance(subject, str) or not subject.strip():
        return jsonify(error="El campo 'asunto' debe ser un texto no vacío."), 400

    try:
        message_id = send_notification(
            get_gmail_service(), recipient.strip(), content, subject.strip()
        )
    except (FileNotFoundError, GoogleAuthError, HttpError, OSError, pickle.UnpicklingError):
        app.logger.exception("No fue posible enviar la notificación")
        return jsonify(error="No fue posible enviar el correo."), 500

    return jsonify(mensaje="Correo enviado correctamente.", id=message_id), 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
