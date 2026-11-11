"""Modelos Pydantic del protocolo y de la API local.

Los nombres de los campos JSON son el contrato del curso y no se traducen:
``id``, ``from``, ``origin``, ``lamport``, ``sent_at`` y ``body``. Lo que sí
está en español son los nombres de las clases y de las funciones internas.

``from`` es una palabra reservada de Python, así que el atributo se llama
``from_`` y se expone como ``"from"`` mediante un alias. Con
``populate_by_name=True`` el modelo se puede construir con cualquiera de los
dos nombres, y con ``model_dump(by_alias=True)`` se recupera el JSON exacto
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


def ahora_utc_iso() -> str:
    """Marca de tiempo informativa en ISO-8601 UTC, por ejemplo 2026-11-10T14:03:22Z.

    Se usa únicamente para mostrar en pantalla. El orden de los mensajes lo
    define Lamport, nunca este reloj físico: los relojes de las máquinas no
    están sincronizados.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Mensaje del protocolo
# ---------------------------------------------------------------------------


class Mensaje(BaseModel):
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
    def _validar_uuid(cls, value: str) -> str:
        """El id debe ser un UUID válido; es la clave de deduplicación."""
        try:
            uuid.UUID(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("id debe ser un UUID válido") from exc
        return value

    @field_validator("from_", "origin")
    @classmethod
    def _no_vacio(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("no puede estar vacío")
        return value

    def a_diccionario_protocolo(self) -> dict[str, Any]:
        """Devuelve el JSON con la clave ``"from"`` (no ``"from_"``)."""
        return self.model_dump(by_alias=True)


# ---------------------------------------------------------------------------
# Endpoints federados (los llama el servidor central)
# ---------------------------------------------------------------------------


class SolicitudRegistro(BaseModel):
    """Cuerpo de POST /register hacia el servidor central."""

    server: str
    url: str


class SaludFederada(BaseModel):
    """Respuesta de GET /fed/health de nuestro servidor."""

    server: str
    lamport: int


class RespuestaRecepcionFederada(BaseModel):
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


class SolicitudCreacionUsuario(BaseModel):
    username: str = Field(min_length=1, max_length=32)

    @field_validator("username")
    @classmethod
    def _limpiar_usuario(cls, value: str) -> str:
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


class InfoUsuario(BaseModel):
    username: str
    identity: str


class ListaUsuarios(BaseModel):
    users: list[InfoUsuario]


class SolicitudEnvioMensaje(BaseModel):
    user: str = Field(min_length=1, max_length=32)
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("user")
    @classmethod
    def _limpiar_usuario(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("el usuario no puede estar vacío")
        return value

    @field_validator("body")
    @classmethod
    def _limpiar_cuerpo(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("el mensaje no puede estar vacío")
        return value


class ListaMensajes(BaseModel):
    messages: list[Mensaje]


# ---------------------------------------------------------------------------
# Estado y salud del servidor de grupo
# ---------------------------------------------------------------------------


class RespuestaSalud(BaseModel):
    status: str = "ok"
    server: str
    lamport: int


class EstadoServidor(BaseModel):
    """Resumen útil para el cliente CLI y para el script del experimento."""

    server: str
    lamport: int
    central_online: bool
    pending_messages: int
    users: int


class InfoServicio(BaseModel):
    server: str
    public_url: str
    central_url: str
    lamport: int
