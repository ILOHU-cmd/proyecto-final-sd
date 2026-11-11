"""Configuración del servidor de grupo.

Todo lo configurable entra por variables de entorno. La regla más importante
es que la URL del servidor central NUNCA está escrita en el código: el mismo
contenedor debe poder conectarse al central de prueba en casa y al central
real del profesor en la sesión de interoperabilidad, sin cambiar una línea.

Las variables se leen y se validan al arrancar (en ``load_settings``). Si una
variable viene mal, el servidor se detiene con un mensaje claro en lugar de
fallar a mitad de la ejecución. Así un error de configuración se ve enseguida
en el log, no minutos después en forma de un fallo raro.

Variables que exige el enunciado:
    GROUP_NAME  -> nombre corto del grupo, por ejemplo "g3"
    PUBLIC_URL  -> URL con la que el servidor central puede alcanzarnos
    CENTRAL_URL -> URL base del servidor central del profesor
    PORT        -> puerto en el que escucha uvicorn

Variables opcionales (controlan el comportamiento ante fallos):
    DB_PATH, REGISTER_INTERVAL_SECONDS, HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS, HEARTBEAT_FAILURES, REQUEST_TIMEOUT_SECONDS,
    RETRY_BASE_SECONDS, RETRY_MAX_SECONDS, LOG_LEVEL
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Nombre de grupo permitido: letras, números, punto, guion y guion bajo.
# Se aceptan "g3", "g3a" o "grupo-03" y se rechazan espacios y símbolos raros,
# porque ese nombre luego forma la identidad visible "usuario@gN".
_GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


@dataclass(frozen=True)
class Settings:
    """Valores de configuración ya validados y listos para usar.

    ``frozen=True`` hace el objeto inmutable: una vez creado, nadie puede
    cambiar la URL del central o el nombre del grupo "por accidente" a mitad
    de la ejecución. Es seguro compartirlo entre las tareas de fondo.
    """

    group_name: str  # identidad del servidor, p. ej. "g3"
    public_url: str  # URL con la que el central nos llama
    central_url: str  # URL base del central del profesor
    port: int  # puerto de uvicorn (lo usa el contenedor, no la app)
    db_path: str  # ruta del archivo SQLite
    register_interval_seconds: float  # cada cuánto se renueva el registro
    heartbeat_interval_seconds: float  # cada cuánto se consulta GET /health
    heartbeat_timeout_seconds: float  # cuánto se espera cada GET /health
    heartbeat_failures: int  # fallos seguidos para marcar el central caído
    request_timeout_seconds: float  # timeout general de las peticiones
    retry_base_seconds: float  # base del backoff exponencial
    retry_max_seconds: float  # tope del backoff exponencial
    log_level: str  # nivel de logs: INFO, DEBUG, WARNING...


def _env_text(name: str, default: str) -> str:
    """Lee una variable de texto y elimina espacios accidentales."""
    value = os.getenv(name, default)
    return value.strip()


def _env_float(name: str, default: float) -> float:
    """Lee una variable numérica decimal.

    Devuelve el valor por defecto si la variable no existe. Si existe pero no
    es un número positivo, lanza ``ValueError`` con un mensaje que indica qué
    variable está mal: fallar pronto es mejor que dejar un valor absurdo.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser un número; se recibió {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} debe ser mayor que cero; se recibió {raw!r}")
    return value


def _env_int(name: str, default: int) -> int:
    """Lee una variable numérica entera positiva (igual política que la decimal)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser un entero; se recibió {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} debe ser mayor que cero; se recibió {raw!r}")
    return value


def _normalize_url(value: str, name: str) -> str:
    """Quita la barra final y comprueba que sea una URL HTTP(S).

    Se quita la barra final para que al concatenar ``/register``, ``/health``
    o ``/history`` no queden rutas con doble barra (``//register``).
    """
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError(
            f"{name} debe comenzar por http:// o https://; se recibió {value!r}"
        )
    return value


def load_settings() -> Settings:
    """Construye y valida la configuración a partir del entorno.

    Se llama una sola vez, al importar la aplicación. A partir de ahí todo el
    código usa el objeto ``Settings`` y no vuelve a leer variables de entorno.
    """
    # Nombre del grupo. Define la identidad "usuario@gN" de nuestros usuarios.
    group_name = _env_text("GROUP_NAME", "g3")
    if not _GROUP_NAME_RE.fullmatch(group_name):
        raise ValueError(
            "GROUP_NAME solo admite letras, números, '.', '-' y '_' "
            f"(máximo 32 caracteres); se recibió {group_name!r}"
        )

    # Puerto y URLs. PUBLIC_URL es la dirección que el central usará para
    # alcanzarnos: debe ser una IP/URL pública, no "localhost".
    port = _env_int("PORT", 8000)
    public_url = _normalize_url(
        _env_text("PUBLIC_URL", f"http://localhost:{port}"), "PUBLIC_URL"
    )
    central_url = _normalize_url(
        _env_text("CENTRAL_URL", "http://localhost:9000"), "CENTRAL_URL"
    )

    # El tope del backoff no puede ser menor que su base; no tendría sentido.
    retry_base = _env_float("RETRY_BASE_SECONDS", 1.0)
    retry_max = _env_float("RETRY_MAX_SECONDS", 30.0)
    if retry_max < retry_base:
        raise ValueError(
            "RETRY_MAX_SECONDS debe ser mayor o igual a RETRY_BASE_SECONDS"
        )

    return Settings(
        group_name=group_name,
        public_url=public_url,
        central_url=central_url,
        port=port,
        db_path=_env_text("DB_PATH", "./data/chat.db"),
        register_interval_seconds=_env_float("REGISTER_INTERVAL_SECONDS", 10.0),
        heartbeat_interval_seconds=_env_float("HEARTBEAT_INTERVAL_SECONDS", 5.0),
        heartbeat_timeout_seconds=_env_float("HEARTBEAT_TIMEOUT_SECONDS", 3.0),
        heartbeat_failures=_env_int("HEARTBEAT_FAILURES", 3),
        request_timeout_seconds=_env_float("REQUEST_TIMEOUT_SECONDS", 5.0),
        retry_base_seconds=retry_base,
        retry_max_seconds=retry_max,
        log_level=_env_text("LOG_LEVEL", "INFO").upper(),
    )
