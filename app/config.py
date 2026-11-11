"""Configuración del servidor de grupo.

Toda la configuración entra por variables de entorno. En particular, la URL
del servidor central NUNCA está escrita en el código: el mismo contenedor debe
servir para trabajar en el computador del grupo y para la sesión de
interoperabilidad del curso.

Variables principales (las exige el enunciado):
    GROUP_NAME  -> nombre corto del grupo, por ejemplo "g3"
    PUBLIC_URL  -> URL con la que el servidor central puede alcanzarnos
    CENTRAL_URL -> URL base del servidor central del profesor
    PORT        -> puerto en el que escucha uvicorn

Variables opcionales (sirven para el experimento de fallos):
    DB_PATH, REGISTER_INTERVAL_SECONDS, HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS, HEARTBEAT_FAILURES, REQUEST_TIMEOUT_SECONDS,
    RETRY_BASE_SECONDS, RETRY_MAX_SECONDS, LOG_LEVEL
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Un nombre de grupo razonable: letras, números, punto, guion y guion bajo.
# Se permiten nombres como g3, g3a o grupo-03.
_GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


@dataclass(frozen=True)
class Settings:
    """Valores de configuración ya validados."""

    group_name: str
    public_url: str
    central_url: str
    port: int
    db_path: str
    register_interval_seconds: float
    heartbeat_interval_seconds: float
    heartbeat_timeout_seconds: float
    heartbeat_failures: int
    request_timeout_seconds: float
    retry_base_seconds: float
    retry_max_seconds: float
    log_level: str


def _env_text(name: str, default: str) -> str:
    """Lee una variable de texto y elimina espacios accidentales."""
    value = os.getenv(name, default)
    return value.strip()


def _env_float(name: str, default: float) -> float:
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
    """Quita la barra final y comprueba que sea una URL HTTP(S)."""
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError(
            f"{name} debe comenzar por http:// o https://; se recibió {value!r}"
        )
    return value


def load_settings() -> Settings:
    """Construye y valida la configuración a partir del entorno."""
    group_name = _env_text("GROUP_NAME", "g3")
    if not _GROUP_NAME_RE.fullmatch(group_name):
        raise ValueError(
            "GROUP_NAME solo admite letras, números, '.', '-' y '_' "
            f"(máximo 32 caracteres); se recibió {group_name!r}"
        )

    port = _env_int("PORT", 8000)
    public_url = _normalize_url(
        _env_text("PUBLIC_URL", f"http://localhost:{port}"), "PUBLIC_URL"
    )
    central_url = _normalize_url(
        _env_text("CENTRAL_URL", "http://localhost:9000"), "CENTRAL_URL"
    )

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
