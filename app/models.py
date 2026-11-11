"""Modelos Pydantic del protocolo y de la API local.

El protocolo es estricto en los nombres de los campos. El único detalle
especial es que ``from`` es una palabra reservada de Python, así que el campo
se llama ``from_`` y se expone como ``"from"`` usando un alias. Con
``populate_by_name=True`` podemos construir el modelo con cualquiera de los
dos nombres y con ``model_dump(by_alias=True)`` recuperamos el JSON exacto
que espera el servidor central.

Los modelos usan ``extra="ignore"`` porque el enunciado dice: "Los campos
desconocidos se ignoran sin error". Eso permite que el protocolo evolucione
sin romper implementaciones viejas.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Timestamp informativo en ISO-8601 UTC, por ejemplo 2026-11-10T14:03:22Z.

    Se usa únicamente para mostrar en pantalla. El orden de los mensajes lo
    define Lamport, nunca este reloj físico: los relojes de las máquinas no
    están sincronizados.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Mensaje del protocolo
# ---------------------------------------------------------------------------


class Message(BaseModel):
    """Mensaje de #general según la especificación del curso."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    origin: str
    lamport: int = Field(ge=0)
    sent_at: str
    body: str

    @field_validator("id")
    @classmethod
    def _validate_uuid(cls, value: str) -> str:
        """El id debe ser un UUID válido; es la clave de deduplicación."""
        try:
            uuid.UUID(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("id debe ser un UUID válido") from exc
        return value

    @field_validator("from_", "origin")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("no puede estar vacío")
        return value

    def to_protocol_dict(self) -> dict[str, Any]:
        """Devuelve el JSON con la clave ``"from"`` (no ``"from_"``)."""
        return self.model_dump(by_alias=True)


# ---------------------------------------------------------------------------
# Endpoints federados (los llama el servidor central)
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Cuerpo de POST /register hacia el servidor central."""

    server: str
    url: str


class FedHealth(BaseModel):
    """Respuesta de GET /fed/health de nuestro servidor."""

    server: str
    lamport: int


class FedReceiveResponse(BaseModel):
    """Respuesta de POST /fed/messages.

    El estado HTTP siempre es 200; ``duplicate`` permite ver en los logs y en
    las pruebas si el mensaje se descartó por idempotencia.
    """

    status: str
    id: str
    duplicate: bool


# ---------------------------------------------------------------------------
# API local (cliente -> servidor del grupo). Esta API es libre: no la fija
# el protocolo del curso, pero mantenerla pequeña y clara ayuda a la demo.
# ---------------------------------------------------------------------------


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=32)

    @field_validator("username")
    @classmethod
    def _clean_username(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("el nombre de usuario no puede estar vacío")
        # Se permiten letras, números, punto, guion y guion bajo. No se
        # permiten '@' ni espacios porque el identificador final es
        # "usuario@gN" y debe ser inequívoco.
        if not all(c.isalnum() or c in "._-" for c in value):
            raise ValueError(
                "el usuario solo puede tener letras, números, '.', '-' y '_'"
            )
        return value


class UserInfo(BaseModel):
    username: str
    identity: str


class UserList(BaseModel):
    users: list[UserInfo]


class SendMessageRequest(BaseModel):
    user: str = Field(min_length=1, max_length=32)
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("user")
    @classmethod
    def _clean_user(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("el usuario no puede estar vacío")
        return value

    @field_validator("body")
    @classmethod
    def _clean_body(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("el mensaje no puede estar vacío")
        return value


class MessageList(BaseModel):
    messages: list[Message]


# ---------------------------------------------------------------------------
# Estado y salud del servidor de grupo
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = "ok"
    server: str
    lamport: int


class ServerStatus(BaseModel):
    """Resumen útil para el cliente CLI y para el script del experimento."""

    server: str
    lamport: int
    central_online: bool
    pending_messages: int
    users: int


class ServiceInfo(BaseModel):
    server: str
    public_url: str
    central_url: str
    lamport: int
