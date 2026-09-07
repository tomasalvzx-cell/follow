"""
Consulta el estado de vigencia de una cédula en la Registraduría.

Envía correo en dos casos:
  1) El estado ("vigencia") cambió respecto a la última consulta válida.
  2) El servicio falla o responde algo inesperado (con reintentos antes
     de considerarlo una falla real, y sin repetir la alerta todos los
     días si sigue caído: solo al inicio de la falla y luego cada
     ALERTA_FALLA_CADA_N intentos).

Variables de entorno requeridas:
  NUIP                -> número de cédula a consultar (ej: 19305751)
  IP_REPORTADA        -> valor "ip" que espera el payload (puede ser cualquier IP válida)
  SMTP_HOST           -> ej: smtp.gmail.com
  SMTP_PORT           -> ej: 587
  SMTP_USER           -> tu correo remitente
  SMTP_PASS           -> contraseña de aplicación (no la contraseña normal)
  EMAIL_DESTINO       -> a dónde quieres que llegue la alerta
  ESTADO_FILE         -> ruta del archivo donde se guarda el último estado (default: estado.json)
  ALERTA_FALLA_CADA_N -> cada cuántos días consecutivos de falla se vuelve a
                         avisar por correo (default: 3)
"""

import json
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText

import requests

URL = "https://defunciones.registraduria.gov.co:8443/VigenciaCedula/consulta"

REINTENTOS = 3
ESPERA_ENTRE_REINTENTOS_SEG = 5


class RespuestaInvalida(Exception):
    """La API respondió pero no con el formato/código esperado."""


def consultar_estado_una_vez(nuip: int, ip: str) -> dict:
    payload = {"nuip": nuip, "ip": ip}
    resp = requests.post(URL, json=payload, timeout=15)
    resp.raise_for_status()  # lanza si el HTTP status no es 2xx

    data = resp.json()  # lanza si no es JSON válido

    if data.get("codigo") != 200 or "vigencia" not in data:
        raise RespuestaInvalida(f"Respuesta con formato inesperado: {data}")

    return data


def consultar_con_reintentos(nuip: int, ip: str) -> tuple[dict | None, str | None]:
    """Devuelve (data, None) si tuvo éxito, o (None, mensaje_error) si falló
    tras agotar los reintentos."""
    ultimo_error = None
    for intento in range(1, REINTENTOS + 1):
        try:
            return consultar_estado_una_vez(nuip, ip), None
        except Exception as e:  # noqa: BLE001 - queremos capturar cualquier falla de red/parseo
            ultimo_error = f"{type(e).__name__}: {e}"
            print(f"Intento {intento}/{REINTENTOS} falló: {ultimo_error}")
            if intento < REINTENTOS:
                time.sleep(ESPERA_ENTRE_REINTENTOS_SEG)
    return None, ultimo_error


def cargar_ultimo_estado(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_estado(path: str, estado: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def enviar_correo(asunto: str, cuerpo: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    destino = os.environ["EMAIL_DESTINO"]

    msg = MIMEText(cuerpo, "plain", "utf-8")
    msg["Subject"] = asunto
    msg["From"] = user
    msg["To"] = destino

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [destino], msg.as_string())


def main() -> int:
    nuip = int(os.environ["NUIP"])
    ip = os.environ.get("IP_REPORTADA", "190.68.144.124")
    estado_file = os.environ.get("ESTADO_FILE", "estado.json")
    alerta_falla_cada_n = int(os.environ.get("ALERTA_FALLA_CADA_N", "3"))

    guardado = cargar_ultimo_estado(estado_file)
    ultimo_valido = guardado.get("ultimo_valido")  # último dict {codigo,vigencia,fecha} bueno
    racha_fallas = guardado.get("racha_fallas", 0)

    data, error = consultar_con_reintentos(nuip, ip)

    # --- Caso: la consulta falló tras reintentos ---
    if error is not None:
        racha_fallas += 1
        print(f"Falla confirmada tras {REINTENTOS} intentos. Racha de fallas: {racha_fallas}")

        es_primera_falla = racha_fallas == 1
        toca_recordatorio = racha_fallas % alerta_falla_cada_n == 0

        if es_primera_falla or toca_recordatorio:
            enviar_correo(
                asunto="⚠ El servicio de consulta de cédula está fallando",
                cuerpo=(
                    f"No se pudo consultar el estado tras {REINTENTOS} intentos.\n\n"
                    f"Último error: {error}\n"
                    f"Días consecutivos fallando: {racha_fallas}\n\n"
                    f"Último estado válido conocido: {ultimo_valido}\n"
                ),
            )
            print("Correo de alerta de falla enviado.")
        else:
            print("Falla ya notificada antes, no se reenvía correo aún.")

        guardar_estado(estado_file, {"ultimo_valido": ultimo_valido, "racha_fallas": racha_fallas})
        return 0

    # --- Caso: la consulta funcionó ---
    if racha_fallas > 0:
        # Se había estado cayendo y ahora volvió a responder bien
        enviar_correo(
            asunto="✅ El servicio de consulta de cédula volvió a responder",
            cuerpo=(
                f"Después de {racha_fallas} día(s) fallando, el servicio volvió a "
                f"responder normalmente.\n\nEstado actual: {data}\n"
            ),
        )
        print("Correo de recuperación enviado.")

    if ultimo_valido is None:
        # Primera ejecución exitosa: solo guarda línea base
        guardar_estado(estado_file, {"ultimo_valido": data, "racha_fallas": 0})
        print("Primera ejecución exitosa: se guardó el estado base, no se envía correo de cambio.")
        return 0

    if data.get("vigencia") != ultimo_valido.get("vigencia"):
        enviar_correo(
            asunto="🔴 Cambio detectado en estado de cédula",
            cuerpo=(
                f"El estado cambió.\n\n"
                f"Anterior: {ultimo_valido.get('vigencia')} (consultado {ultimo_valido.get('fecha')})\n"
                f"Actual:   {data.get('vigencia')} (consultado {data.get('fecha')})\n"
            ),
        )
        print("Cambio de estado detectado, correo enviado.")
    else:
        print("Sin cambios de estado. No se envía correo.")

    guardar_estado(estado_file, {"ultimo_valido": data, "racha_fallas": 0})
    return 0


if __name__ == "__main__":
    sys.exit(main())
