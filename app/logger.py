"""Logs de auditoría del servidor.

El enunciado pide logs legibles de lo que el servidor envía, recibe, reintenta
y descarta. También son la fuente de datos del experimento. Por eso todos los
eventos importantes pasan por ``log_event`` y llevan un nombre en mayúsculas:

    [ENVÍO] [RECEPCIÓN] [REINTENTO] [DESCARTE_DUPLICADO] [CAÍDA_CENTRAL] ...

No se registra el cuerpo completo de los mensajes (solo su id y su marca de
Lamport): así el log es corto y no expone conversaciones completas.
"""

from __future__ import annotations

import logging
import sys

_LOGGER_NAME = "chat"
_server_name = "?"
_configured = False


def setup_logging(level: str = "INFO", server_name: str = "?") -> None:
    """Configura el logger una sola vez.

    Se usa ``sys.stdout`` para que ``docker compose logs`` muestre todo sin
    necesidad de configuración adicional.
    """
    global _server_name, _configured
    _server_name = server_name

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False

    if _configured:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    _configured = True


def log_event(event: str, message: str = "", **fields: object) -> None:
    """Escribe un evento legible con la forma ``[g3] [EVENTO] clave=valor``."""
    parts = [f"[{_server_name}]", f"[{event}]"]
    if message:
        parts.append(message)
    for key, value in fields.items():
        if value is not None:
            parts.append(f"{key}={value}")
    logging.getLogger(_LOGGER_NAME).info(" ".join(parts))


def log_error(event: str, message: str = "", **fields: object) -> None:
    """Igual que ``log_event``, pero con nivel ERROR para facilitar búsquedas."""
    parts = [f"[{_server_name}]", f"[{event}]"]
    if message:
        parts.append(message)
    for key, value in fields.items():
        if value is not None:
            parts.append(f"{key}={value}")
    logging.getLogger(_LOGGER_NAME).error(" ".join(parts))
