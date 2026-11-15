"""Registros de auditoría del servidor.

El enunciado pide logs legibles de lo que el servidor envía, recibe, reintenta
y descarta. También son la fuente de datos del experimento. Por eso todos los
eventos importantes pasan por ``registrar_evento`` y llevan un nombre en
mayúsculas:

    [ENVÍO] [RECEPCIÓN] [REINTENTO] [DESCARTE_DUPLICADO] [CAÍDA_CENTRAL] ...

No se registra el cuerpo completo de los mensajes (solo su id y su marca de
Lamport): así el log es corto y no expone conversaciones completas.
"""

from __future__ import annotations

import logging
import sys

_NOMBRE_REGISTRADOR = "chat"
_nombre_servidor = "?"
_configurado = False


def configurar_registros(nivel: str = "INFO", nombre_servidor: str = "?") -> None:
    """Configura el registrador una sola vez.

    Se usa ``sys.stdout`` para que ``docker compose logs`` muestre todo sin
    necesidad de configuración adicional.
    """
    global _nombre_servidor, _configurado

    _nombre_servidor = nombre_servidor

    registrador = logging.getLogger(_NOMBRE_REGISTRADOR)
    registrador.setLevel(nivel.upper())
    registrador.propagate = False

    if _configurado:
        return

    manejador = logging.StreamHandler(sys.stdout)
    manejador.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    registrador.addHandler(manejador)
    _configurado = True


def registrar_evento(evento: str, mensaje: str = "", **campos: object) -> None:
    """Escribe un evento legible con la forma ``[g3] [EVENTO] clave=valor``."""
    partes = [f"[{_nombre_servidor}]", f"[{evento}]"]

    if mensaje:
        partes.append(mensaje)

    for clave, valor in campos.items():
        if valor is not None:
            partes.append(f"{clave}={valor}")

    logging.getLogger(_NOMBRE_REGISTRADOR).info(" ".join(partes))


def registrar_error(evento: str, mensaje: str = "", **campos: object) -> None:
    """Igual que ``registrar_evento``, pero con nivel ERROR para buscar mejor."""
    partes = [f"[{_nombre_servidor}]", f"[{evento}]"]

    if mensaje:
        partes.append(mensaje)

    for clave, valor in campos.items():
        if valor is not None:
            partes.append(f"{clave}={valor}")

    logging.getLogger(_NOMBRE_REGISTRADOR).error(" ".join(partes))
