"""
Consulta el estado de vigencia de una cédula en la Registraduría.

Envía correo en dos casos:
  1) El estado ("vigencia") cambió respecto a la última consulta válida.
  2) El servicio falla o responde algo inesperado, con reintentos SOLO para
     errores transitorios (red/HTTP 5xx) y sin repetir la alerta todos los
     días si sigue caído.

Variables de entorno requeridas:
  NUIP                -> número de cédula a consultar (ej: 19305751)
  SMTP_HOST           -> ej: smtp.gmail.com
  SMTP_PORT           -> ej: 587 (STARTTLS) o 465 (SSL directo)
  SMTP_USER           -> tu correo remitente
  SMTP_PASS           -> contraseña de aplicación (no la contraseña normal)
  EMAIL_DESTINO       -> uno o varios correos separados por coma, ej:
                         "correo1@gmail.com, correo2@gmail.com"

Variables opcionales:
  API_URL             -> URL del servicio a consultar (default: endpoint de la Registraduría)
  IP_REPORTADA        -> valor "ip" del payload (default: 190.68.144.124)
  ESTADO_FILE         -> ruta del archivo de estado (default: estado.json)
  HISTORICO_FILE      -> ruta del histórico de llamadas (default: historico.jsonl)
  ALERTA_FALLA_CADA_N -> cada cuántos días de falla consecutiva se reavisa (default: 3)
  REINTENTOS          -> reintentos para errores transitorios (default: 3)
  ESPERA_REINTENTO_SEG-> segundos base de espera entre reintentos (default: 5, con backoff)

Para pruebas locales puedes usar un archivo .env + `python-dotenv`;
en GitHub Actions esto no hace falta porque las variables llegan por `secrets`.
"""

import json
import logging
import os
import smtplib
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests

URL_DEFAULT = "https://defunciones.registraduria.gov.co:8443/VigenciaCedula/consulta"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("seguimiento")


class RespuestaInvalida(Exception):
    """La API respondió pero no con el formato/código esperado (error determinista, no se reintenta)."""


class ConfigError(Exception):
    """Falta o es inválida una variable de entorno requerida."""


@dataclass
class Config:
    nuip: int
    ip: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    email_destino: list[str]
    api_url: str = URL_DEFAULT
    estado_file: str = "estado.json"
    historico_file: str = "historico.jsonl"
    alerta_falla_cada_n: int = 3
    reintentos: int = 3
    espera_reintento_seg: int = 5

    @classmethod
    def from_env(cls) -> "Config":
        def requerida(nombre: str) -> str:
            valor = os.environ.get(nombre)
            if not valor:
                raise ConfigError(f"Falta la variable de entorno requerida: {nombre}")
            return valor

        try:
            nuip = int(requerida("NUIP"))
        except ValueError as e:
            raise ConfigError("NUIP debe ser un número entero") from e

        try:
            smtp_port = int(requerida("SMTP_PORT"))
        except ValueError as e:
            raise ConfigError("SMTP_PORT debe ser un número entero") from e

        return cls(
            nuip=nuip,
            ip=os.environ.get("IP_REPORTADA") or "190.68.144.124",
            smtp_host=requerida("SMTP_HOST"),
            smtp_port=smtp_port,
            smtp_user=requerida("SMTP_USER"),
            smtp_pass=requerida("SMTP_PASS"),
            email_destino=[
                correo.strip() for correo in requerida("EMAIL_DESTINO").split(",") if correo.strip()
            ],
            # Se usa "or" en vez del default de .get(): en GitHub Actions un
            # secreto no definido llega como cadena vacía, no como ausente.
            api_url=os.environ.get("API_URL") or URL_DEFAULT,
            estado_file=os.environ.get("ESTADO_FILE") or "estado.json",
            historico_file=os.environ.get("HISTORICO_FILE") or "historico.jsonl",
            alerta_falla_cada_n=int(os.environ.get("ALERTA_FALLA_CADA_N") or 3),
            reintentos=int(os.environ.get("REINTENTOS") or 3),
            espera_reintento_seg=int(os.environ.get("ESPERA_REINTENTO_SEG") or 5),
        )


def consultar_estado_una_vez(url: str, nuip: int, ip: str) -> dict:
    payload = {"nuip": nuip, "ip": ip}
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()  # error transitorio típico (5xx) -> se reintenta

    try:
        data = resp.json()
    except ValueError as e:
        # JSON corrupto: no es un problema de red, reintentar no ayuda
        raise RespuestaInvalida(f"La respuesta no es JSON válido: {e}") from e

    if data.get("codigo") != 200 or "vigencia" not in data:
        raise RespuestaInvalida(f"Respuesta con formato inesperado: {data}")

    if not isinstance(data["vigencia"], str):
        raise RespuestaInvalida(f"'vigencia' no es texto: {data!r}")

    data["fecha_local"] = datetime.now(timezone.utc).isoformat()
    return data


def consultar_con_reintentos(cfg: Config) -> tuple[dict | None, str | None]:
    """Devuelve (data, None) si tuvo éxito, o (None, mensaje_error) si falló.

    Reintenta con backoff SOLO errores transitorios (red, timeout, HTTP 5xx).
    Los errores deterministas (RespuestaInvalida) fallan de una vez: reintentar
    no cambia una respuesta que ya sabemos que está mal formada.
    """
    ultimo_error = None
    for intento in range(1, cfg.reintentos + 1):
        try:
            return consultar_estado_una_vez(cfg.api_url, cfg.nuip, cfg.ip), None
        except RespuestaInvalida as e:
            log.warning("Respuesta inválida (no se reintenta): %s", e)
            return None, f"RespuestaInvalida: {e}"
        except requests.exceptions.RequestException as e:
            ultimo_error = f"{type(e).__name__}: {e}"
            log.warning("Intento %d/%d falló (error transitorio): %s", intento, cfg.reintentos, ultimo_error)
            if intento < cfg.reintentos:
                espera = cfg.espera_reintento_seg * (2 ** (intento - 1))  # backoff exponencial
                time.sleep(espera)
    return None, ultimo_error


def cargar_estado(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_estado(path: str, estado: dict) -> None:
    """Escritura atómica: escribe a un archivo temporal y luego reemplaza,
    para no dejar el estado corrupto si el proceso se interrumpe a mitad."""
    directorio = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directorio, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def registrar_historico(
    path: str,
    *,
    exito: bool,
    data: dict | None = None,
    error: str | None = None,
    racha_fallas: int = 0,
    cambio_detectado: bool = False,
) -> None:
    """Agrega UNA línea JSON al histórico por cada llamada al servicio.

    Formato .jsonl (append-only): no hay que reescribir el archivo completo
    cada día, así que no se corrompe el historial viejo si el proceso se corta,
    y git genera un diff limpio de una sola línea por ejecución.

    Si falla el registro del histórico NO se propaga la excepción: es
    información complementaria, no debe tumbar el monitoreo principal.
    """
    registro = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "exito": exito,
        "vigencia": (data or {}).get("vigencia"),
        "fecha_api": (data or {}).get("fecha"),
        "codigo": (data or {}).get("codigo"),
        "error": error,
        "racha_fallas": racha_fallas,
        "cambio_detectado": cambio_detectado,
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error("No se pudo escribir en el histórico '%s': %s", path, e)


def enviar_correo(cfg: Config, asunto: str, cuerpo: str) -> None:
    msg = MIMEText(cuerpo, "plain", "utf-8")
    msg["Subject"] = asunto
    msg["From"] = cfg.smtp_user
    msg["To"] = ", ".join(cfg.email_destino)

    if cfg.smtp_port == 465:
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)

    with server:
        if cfg.smtp_port != 465:
            server.starttls()
        server.login(cfg.smtp_user, cfg.smtp_pass)
        server.sendmail(cfg.smtp_user, cfg.email_destino, msg.as_string())


def enviar_correo_seguro(cfg: Config, asunto: str, cuerpo: str) -> bool:
    """No queremos que un fallo de SMTP tumbe el script antes de guardar el estado."""
    try:
        enviar_correo(cfg, asunto, cuerpo)
        return True
    except Exception as e:
        log.error("No se pudo enviar el correo '%s': %s", asunto, e)
        return False


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        log.error("Error de configuración: %s", e)
        return 1

    guardado = cargar_estado(cfg.estado_file)
    ultimo_valido = guardado.get("ultimo_valido")
    racha_fallas = guardado.get("racha_fallas", 0)

    data, error = consultar_con_reintentos(cfg)

    # --- Caso: falló ---
    if error is not None:
        racha_fallas += 1
        log.info("Falla confirmada. Racha de fallas: %d", racha_fallas)

        es_primera_falla = racha_fallas == 1
        toca_recordatorio = racha_fallas % cfg.alerta_falla_cada_n == 0

        if es_primera_falla or toca_recordatorio:
            enviar_correo_seguro(
                cfg,
                asunto="⚠ El servicio de consulta de cédula está fallando",
                cuerpo=(
                    f"No se pudo consultar el estado.\n\n"
                    f"Último error: {error}\n"
                    f"Días consecutivos fallando: {racha_fallas}\n\n"
                    f"Último estado válido conocido: {ultimo_valido}\n"
                ),
            )

        guardar_estado(cfg.estado_file, {"ultimo_valido": ultimo_valido, "racha_fallas": racha_fallas})
        registrar_historico(cfg.historico_file, exito=False, error=error, racha_fallas=racha_fallas)
        # Código 1 -> el job queda en rojo en GitHub Actions, visible de un vistazo.
        return 1

    # --- Caso: funcionó ---
    if racha_fallas > 0:
        enviar_correo_seguro(
            cfg,
            asunto="✅ El servicio de consulta de cédula volvió a responder",
            cuerpo=f"Después de {racha_fallas} día(s) fallando, volvió a responder.\n\nEstado actual: {data}\n",
        )

    if ultimo_valido is None:
        guardar_estado(cfg.estado_file, {"ultimo_valido": data, "racha_fallas": 0})
        registrar_historico(cfg.historico_file, exito=True, data=data)
        log.info("Primera ejecución exitosa: estado base guardado, sin correo de cambio.")
        return 0

    cambio_detectado = data.get("vigencia") != ultimo_valido.get("vigencia")

    if cambio_detectado:
        enviar_correo_seguro(
            cfg,
            asunto="🔴 Cambio detectado en estado de cédula",
            cuerpo=(
                f"El estado cambió.\n\n"
                f"Anterior: {ultimo_valido.get('vigencia')} (consultado {ultimo_valido.get('fecha_local')})\n"
                f"Actual:   {data.get('vigencia')} (consultado {data.get('fecha_local')})\n"
            ),
        )
        log.info("Cambio de estado detectado, correo enviado.")
    else:
        log.info("Sin cambios de estado.")

    guardar_estado(cfg.estado_file, {"ultimo_valido": data, "racha_fallas": 0})
    registrar_historico(cfg.historico_file, exito=True, data=data, cambio_detectado=cambio_detectado)
    return 0


if __name__ == "__main__":
    sys.exit(main())
