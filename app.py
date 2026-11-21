from __future__ import annotations

import base64
import html
import os
import pickle
import sqlite3
from datetime import datetime, timedelta, timezone
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
NOTIFICATION_DB_PATH = os.getenv(
    "NOTIFICATION_DB_PATH", os.path.join(CREDENTIALS_DIR, "notifications.db")
)
PROCESSING_TTL_SECONDS = int(os.getenv("NOTIFICATION_PROCESSING_TTL_SECONDS", "300"))
SENDER_EMAIL = os.getenv(
    "GMAIL_SENDER_EMAIL", "sergio.bedoya46815@ucaldas.edu.co"
)

app = Flask(__name__)
_service: Resource | None = None
_service_lock = Lock()
_db_lock = Lock()
_db_ready = False


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def init_notification_db() -> None:
    global _db_ready
    if _db_ready:
        return
    with _db_lock:
        if _db_ready:
            return
        db_dir = os.path.dirname(os.path.abspath(NOTIFICATION_DB_PATH))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(NOTIFICATION_DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_notifications (
                    idempotency_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('processing','sent','failed')),
                    message_id TEXT,
                    recipient TEXT,
                    subject TEXT,
                    processing_started_at TEXT,
                    updated_at TEXT,
                    created_at TEXT,
                    last_error TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        _db_ready = True


def _db_conn() -> sqlite3.Connection:
    init_notification_db()
    conn = sqlite3.connect(NOTIFICATION_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def claim_notification(key: str, recipient: str, subject: str) -> tuple[str, str | None]:
    """Claim a notification key. Returns (state, message_id). state in {claimed,sent,processing}."""
    now = _utc_now_iso()
    stale_before = (
        datetime.now(timezone.utc) - timedelta(seconds=max(PROCESSING_TTL_SECONDS, 1))
    ).replace(microsecond=0).isoformat()
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, message_id, processing_started_at FROM processed_notifications WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO processed_notifications
                        (idempotency_key, status, recipient, subject, processing_started_at, created_at, updated_at)
                    VALUES (?, 'processing', ?, ?, ?, ?, ?)
                    """,
                    (key, recipient, subject, now, now, now),
                )
                conn.commit()
                return ("claimed", None)

            status = row["status"]
            if status == "sent":
                conn.commit()
                return ("sent", row["message_id"])

            if status == "processing":
                started = row["processing_started_at"] or ""
                if started and started < stale_before:
                    cur = conn.execute(
                        """
                        UPDATE processed_notifications
                        SET status='processing', recipient=?, subject=?, processing_started_at=?, updated_at=?, last_error=NULL
                        WHERE idempotency_key=? AND status='processing'
                        """,
                        (recipient, subject, now, now, key),
                    )
                    if cur.rowcount:
                        conn.commit()
                        return ("claimed", None)
                conn.commit()
                return ("processing", None)

            if status == "failed":
                cur = conn.execute(
                    """
                    UPDATE processed_notifications
                    SET status='processing', recipient=?, subject=?, processing_started_at=?, updated_at=?, last_error=NULL
                    WHERE idempotency_key=? AND status='failed'
                    """,
                    (recipient, subject, now, now, key),
                )
                if cur.rowcount:
                    conn.commit()
                    return ("claimed", None)

            conn.commit()
            return ("processing", None)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()


def mark_notification_sent(key: str, message_id: str) -> None:
    now = _utc_now_iso()
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute(
                "UPDATE processed_notifications SET status='sent', message_id=?, updated_at=?, last_error=NULL WHERE idempotency_key=?",
                (message_id, now, key),
            )
            conn.commit()
        finally:
            conn.close()


def mark_notification_failed(key: str, error_text: str) -> None:
    now = _utc_now_iso()
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute(
                "UPDATE processed_notifications SET status='failed', updated_at=?, last_error=? WHERE idempotency_key=?",
                (now, str(error_text)[:1000], key),
            )
            conn.commit()
        finally:
            conn.close()


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


def get_notification_presentation(subject: str) -> dict[str, str]:
    normalized = subject.strip().lower()
    if "recuper" in normalized or "contraseña" in normalized or "password" in normalized:
        return {
            "accent": "#2563eb",
            "eyebrow": "Recuperación de contraseña",
            "title": "Recupera el acceso a tu cuenta",
            "intro": "Usa el código siguiente para completar la recuperación de tu contraseña.",
        }
    if "dos factores" in normalized or "2fa" in normalized or "verificación" in normalized:
        return {
            "accent": "#18a66a",
            "eyebrow": "Verificación de dos factores",
            "title": "Verifica tu identidad",
            "intro": "Usa el siguiente código para acceder de forma segura a Sports System.",
        }
    if "apuesta cancelada" in normalized or "cancelada" in normalized:
        return {
            "accent": "#64748b",
            "eyebrow": "Resultado de la apuesta",
            "title": "Tu apuesta fue cancelada",
            "intro": "La apuesta quedó cancelada y se realizó el ajuste correspondiente.",
        }
    if "apost" in normalized and ("ganada" in normalized or "ganó" in normalized or "won" in normalized):
        return {
            "accent": "#16a34a",
            "eyebrow": "Resultado de la apuesta",
            "title": "¡Ganaste tu apuesta!",
            "intro": "Tu selección fue acertada y se calculó el valor recibido.",
        }
    if "apost" in normalized and ("perdida" in normalized or "perdió" in normalized or "lost" in normalized):
        return {
            "accent": "#dc2626",
            "eyebrow": "Resultado de la apuesta",
            "title": "Tu apuesta no fue ganadora",
            "intro": "La selección no coincidió con el resultado del partido.",
        }
    if "apuesta registrada" in normalized or ("apuesta" in normalized and "registrada" in normalized):
        return {
            "accent": "#7c3aed",
            "eyebrow": "Apuesta registrada",
            "title": "Tu apuesta quedó registrada",
            "intro": "Estos son los detalles confirmados de tu apuesta.",
        }
    return {
        "accent": "#18a66a",
        "eyebrow": "Sports System",
        "title": "Nueva notificación",
        "intro": "Tienes una nueva actualización en Sports System.",
    }


def build_notification_html(content: str, subject: str) -> str:
    """Construye la plantilla del correo usando contenido seguro y contexto específico."""
    presentation = get_notification_presentation(subject)
    accent = presentation["accent"]
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
                                    <td style="height: 6px; background-color: {accent}; font-size: 0; line-height: 0;">&nbsp;</td>
                                </tr>
                                <tr>
                                    <td style="padding: 38px 42px 12px;">
                                        <p style="margin: 0 0 10px; color: {accent}; font-size: 12px; font-weight: 800; letter-spacing: 1.4px; text-transform: uppercase;">
                                            {html.escape(presentation["eyebrow"])}
                                        </p>
                                        <h1 style="margin: 0; color: #17212b; font-size: 28px; line-height: 1.25; letter-spacing: -0.6px;">
                                            {html.escape(presentation["title"])}
                                        </h1>
                                    </td>
                                </tr>
                                <tr>
                                    <td style="padding: 0 42px 26px; color: #52616d; font-size: 15px; line-height: 1.6;">
                                        {html.escape(presentation["intro"])}
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
    html_content = build_notification_html(content, subject)
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
    idempotency_key = data.get("idempotency_key")

    if not is_valid_email(recipient):
        return jsonify(error="El campo 'para' debe ser un correo válido."), 400
    if not isinstance(content, str) or not content.strip():
        return jsonify(error="El campo 'contenido' es obligatorio."), 400
    if not isinstance(subject, str) or not subject.strip():
        return jsonify(error="El campo 'asunto' debe ser un texto no vacío."), 400
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        return jsonify(
            error="El campo 'idempotency_key' debe ser un texto no vacío.",
            estado="failed",
            procesada=False,
        ), 400
    if isinstance(idempotency_key, str):
        idempotency_key = idempotency_key.strip()

    if idempotency_key:
        try:
            state, existing_message_id = claim_notification(
                idempotency_key, recipient.strip(), subject.strip()
            )
        except Exception:
            app.logger.exception("No fue posible registrar la clave de idempotencia")
            return (
                jsonify(
                    error="No fue posible registrar la solicitud de notificación.",
                    estado="failed",
                    procesada=False,
                ),
                500,
            )
        if state == "sent":
            return (
                jsonify(
                    mensaje="Notificación ya procesada.",
                    id=existing_message_id,
                    duplicado=True,
                    estado="sent",
                    procesada=True,
                ),
                200,
            )
        if state == "processing":
            return (
                jsonify(
                    error="Notificación en procesamiento.",
                    estado="processing",
                    procesada=False,
                    duplicado=True,
                ),
                409,
            )
        # state == 'claimed': continuar con el envío controlado

    try:
        message_id = send_notification(
            get_gmail_service(), recipient.strip(), content, subject.strip()
        )
    except FileNotFoundError:
        app.logger.exception("No fue posible enviar la notificación por Gmail")
        if idempotency_key:
            try:
                mark_notification_failed(idempotency_key, "credentials_missing")
            except Exception:
                app.logger.exception("No fue posible marcar la notificación como fallida")
        response = {
            "error": "No se encontró el archivo credentials/credentials.json.",
        }
        if idempotency_key:
            response.update({"estado": "failed", "procesada": False})
        return jsonify(response), 500
    except (GoogleAuthError, HttpError):
        app.logger.exception("No fue posible enviar la notificación por Gmail")
        if idempotency_key:
            try:
                mark_notification_failed(idempotency_key, "gmail_error")
            except Exception:
                app.logger.exception("No fue posible marcar la notificación como fallida")
        response = {"error": "No fue posible enviar el correo mediante Gmail."}
        if idempotency_key:
            response.update({"estado": "failed", "procesada": False})
        return jsonify(response), 502
    except (OSError, pickle.UnpicklingError):
        app.logger.exception("No fue posible leer o guardar las credenciales de Gmail")
        if idempotency_key:
            try:
                mark_notification_failed(idempotency_key, "credentials_io_error")
            except Exception:
                app.logger.exception("No fue posible marcar la notificación como fallida")
        response = {
            "error": "No fue posible leer o guardar las credenciales de Gmail.",
        }
        if idempotency_key:
            response.update({"estado": "failed", "procesada": False})
        return jsonify(response), 500

    if idempotency_key:
        try:
            mark_notification_sent(idempotency_key, message_id)
        except Exception:
            app.logger.exception(
                "Correo enviado pero no fue posible registrar la entrega idempotente"
            )
            try:
                mark_notification_failed(idempotency_key, "persist_sent_failed")
            except Exception:
                app.logger.exception(
                    "No fue posible marcar la notificación como fallida"
                )
            return (
                jsonify(
                    error="No fue posible registrar la entrega de la notificación.",
                    estado="failed",
                    procesada=False,
                ),
                500,
            )
        return (
            jsonify(
                mensaje="Correo enviado correctamente.",
                id=message_id,
                duplicado=False,
                estado="sent",
                procesada=True,
            ),
            200,
        )

    return jsonify(mensaje="Correo enviado correctamente.", id=message_id), 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
