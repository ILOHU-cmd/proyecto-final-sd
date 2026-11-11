"""Servidor HTTP del grupo (FastAPI).

Expone dos grupos de endpoints:

* API local (la usa nuestro cliente de consola). Es libre según el enunciado:
  crear usuarios, enviar a #general, leer el historial y consultar el estado.
* API federada (la usa el servidor central del profesor):
  GET /fed/health y POST /fed/messages.

El servidor guarda todo en SQLite, mantiene un reloj de Lamport persistente y
delega el envío/reintento al central en ``ClienteCentral``, que corre como
tarea de fondo desde el arranque.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Query, status

from app.central_client import ClienteCentral
from app.config import cargar_configuracion
from app.database import Database
from app.lamport import LamportClock
from app.logger import log_error, log_event, setup_logging
from app.models import (
    FedHealth,
    FedReceiveResponse,
    HealthResponse,
    Message,
    MessageList,
    SendMessageRequest,
    ServerStatus,
    ServiceInfo,
    UserCreate,
    UserInfo,
    UserList,
    utc_now_iso,
)

# ---------------------------------------------------------------------------
# Composición de la aplicación
# ---------------------------------------------------------------------------

configuracion = cargar_configuracion()
setup_logging(configuracion.nivel_registro, configuracion.nombre_grupo)

database = Database(configuracion.ruta_base_datos)
database.init_schema()
clock = LamportClock(database)
central_client = ClienteCentral(configuracion, database, clock)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Arranca y detiene las tareas de fondo junto con el servidor."""
    log_event(
        "ARRANQUE",
        servidor=configuracion.nombre_grupo,
        url_publica=configuracion.url_publica,
        central=configuracion.url_central,
        db=configuracion.ruta_base_datos,
    )

    central_task = asyncio.create_task(
        central_client.ejecutar(), name=f"central-client-{configuracion.nombre_grupo}"
    )

    def _report_task_end(task: asyncio.Task[None]) -> None:
        """Si la tarea muere por un error inesperado, que quede en el log."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log_error(
                "ERROR",
                "la tarea del cliente central terminó",
                detalle=repr(error),
            )

    central_task.add_done_callback(_report_task_end)

    try:
        yield
    finally:
        central_client.solicitar_apagado()
        central_task.cancel()
        with suppress(asyncio.CancelledError):
            await central_task
        database.close()
        log_event("APAGADO", servidor=configuracion.nombre_grupo)


app = FastAPI(
    title=f"Chat distribuido - {configuracion.nombre_grupo}",
    version="1.0.0",
    description=(
        "Servidor de grupo para la sala global #general. "
        "Arquitectura en estrella: todo pasa por el servidor central."
    ),
    lifespan=lifespan,
)


def _identity(username: str) -> str:
    """Identificador visible del usuario, por ejemplo ana@g3."""
    return f"{username}@{configuracion.nombre_grupo}"


# ---------------------------------------------------------------------------
# Información y salud
# ---------------------------------------------------------------------------


@app.get("/", response_model=ServiceInfo, tags=["info"])
def service_info() -> ServiceInfo:
    """Datos útiles para saber a qué servidor estamos conectados."""
    return ServiceInfo(
        server=configuracion.nombre_grupo,
        public_url=configuracion.url_publica,
        central_url=configuracion.url_central,
        lamport=clock.value,
    )


@app.get("/health", response_model=HealthResponse, tags=["info"])
def health() -> HealthResponse:
    """Salud del propio servidor de grupo (la usa el healthcheck de Docker)."""
    return HealthResponse(
        status="ok", server=configuracion.nombre_grupo, lamport=clock.value
    )


@app.get("/status", response_model=ServerStatus, tags=["info"])
def server_status() -> ServerStatus:
    """Estado que usa el CLI y el script del experimento."""
    return ServerStatus(
        server=configuracion.nombre_grupo,
        lamport=clock.value,
        central_online=central_client.esta_en_linea,
        pending_messages=database.pending_outbox_count(),
        users=database.count_users(),
    )


# ---------------------------------------------------------------------------
# API local: usuarios
# ---------------------------------------------------------------------------


@app.post(
    "/users",
    response_model=UserInfo,
    status_code=status.HTTP_201_CREATED,
    tags=["local"],
)
def create_user(payload: UserCreate) -> UserInfo:
    """Crea un usuario local del grupo."""
    if not database.add_user(payload.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El usuario {payload.username!r} ya existe",
        )

    log_event("USUARIO_CREADO", usuario=_identity(payload.username))
    return UserInfo(username=payload.username, identity=_identity(payload.username))


@app.get("/users", response_model=UserList, tags=["local"])
def list_users() -> UserList:
    """Lista los usuarios locales con su identidad completa usuario@gN."""
    return UserList(
        users=[
            UserInfo(username=name, identity=_identity(name))
            for name in database.list_users()
        ]
    )


# ---------------------------------------------------------------------------
# API local: mensajes
# ---------------------------------------------------------------------------


@app.post(
    "/messages",
    response_model=Message,
    status_code=status.HTTP_201_CREATED,
    tags=["local"],
)
def send_message(payload: SendMessageRequest) -> Message:
    """Un usuario local escribe en #general.

    El mensaje se guarda de inmediato en el historial local y se encola para
    enviarlo al central. Responder rápido permite que el cliente siga
    conversando aunque el central esté caído; la entrega queda a cargo de la
    cola persistente.
    """
    if not database.user_exists(payload.user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"El usuario {payload.user!r} no existe. Créalo con POST /users",
        )

    # Regla de Lamport para emisión: incrementar antes de enviar.
    lamport = clock.tick()

    # Se genera el UUID en el origen. Este id viaja sin cambios en todos los
    # reintentos y es la clave que usan los receptores para deduplicar.
    message = Message.model_validate(
        {
            "id": str(uuid.uuid4()),
            "from": _identity(payload.user),
            "origin": configuracion.nombre_grupo,
            "lamport": lamport,
            "sent_at": utc_now_iso(),
            "body": payload.body,
        }
    )

    if not database.insert_message(message):
        # Con UUIDv4 la colisión es prácticamente imposible; si ocurriera,
        # preferimos fallar de forma visible antes que reutilizar un id.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Colisión de UUID al guardar el mensaje",
        )

    # La cola es persistente: si el central está caído o el proceso se
    # reinicia, el mensaje sigue pendiente y se reintenta más tarde.
    database.enqueue_outbox(message.id)

    log_event(
        "MENSAJE_LOCAL",
        usuario=message.from_,
        id=message.id,
        lamport=message.lamport,
        pendientes=database.pending_outbox_count(),
    )
    return message


@app.get("/messages", response_model=MessageList, tags=["local"])
def list_messages(
    since: int = Query(
        0,
        ge=0,
        description=(
            "Devuelve mensajes con lamport >= since. El cliente pide solapamiento "
            "y descarta ids repetidos."
        ),
    ),
    limit: int = Query(500, ge=1, le=5000),
) -> MessageList:
    """Historial local ordenado por (lamport, origin)."""
    return MessageList(messages=database.list_messages(since, limit))


# ---------------------------------------------------------------------------
# API federada: la llama el servidor central
# ---------------------------------------------------------------------------


@app.get("/fed/health", response_model=FedHealth, tags=["federado"])
def fed_health() -> FedHealth:
    """Heartbeat del servidor de grupo, visto desde el central."""
    return FedHealth(server=configuracion.nombre_grupo, lamport=clock.value)


@app.post("/fed/messages", response_model=FedReceiveResponse, tags=["federado"])
def receive_fed_message(message: Message) -> FedReceiveResponse:
    """Recibe un mensaje reenviado por el central.

    Es idempotente: si el UUID ya existe, responde 200 sin duplicar nada.
    Esto es obligatorio porque el emisor no puede distinguir entre "no llegó"
    y "llegó pero se perdió la respuesta"; por eso puede reenviar el mismo id.
    """
    if not database.insert_message(message):
        log_event(
            "DESCARTE_DUPLICADO",
            origen="central",
            id=message.id,
            lamport=message.lamport,
        )
        return FedReceiveResponse(status="duplicate", id=message.id, duplicate=True)

    # Recepción nueva: L = max(L, L_recibido) + 1.
    new_clock = clock.observe(message.lamport)

    # Recordamos hasta dónde hemos visto el historial del central. Tras una
    # caída pediremos GET /history?since=esta_marca.
    database.advance_state_int("last_history_lamport", message.lamport)

    log_event(
        "RECEPCIÓN",
        origen="central",
        usuario=message.from_,
        id=message.id,
        lamport=message.lamport,
        lamport_local=new_clock,
    )
    return FedReceiveResponse(status="received", id=message.id, duplicate=False)
