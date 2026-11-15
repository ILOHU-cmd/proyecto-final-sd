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
from app.database import BaseDatos
from app.lamport import RelojLamport
from app.logger import configurar_registros, registrar_error, registrar_evento
from app.models import (
    EstadoServidor,
    InfoServicio,
    InfoUsuario,
    ListaMensajes,
    ListaUsuarios,
    Mensaje,
    RespuestaRecepcionFederada,
    RespuestaSalud,
    SaludFederada,
    SolicitudCreacionUsuario,
    SolicitudEnvioMensaje,
    ahora_utc_iso,
)

# ---------------------------------------------------------------------------
# Composición de la aplicación
# ---------------------------------------------------------------------------

configuracion = cargar_configuracion()
configurar_registros(configuracion.nivel_registro, configuracion.nombre_grupo)

base_datos = BaseDatos(configuracion.ruta_base_datos)
base_datos.crear_esquema()
reloj = RelojLamport(base_datos)
cliente_central = ClienteCentral(configuracion, base_datos, reloj)


@asynccontextmanager
async def ciclo_vida(_: FastAPI):
    """Arranca y detiene las tareas de fondo junto con el servidor."""
    registrar_evento(
        "ARRANQUE",
        servidor=configuracion.nombre_grupo,
        url_publica=configuracion.url_publica,
        central=configuracion.url_central,
        db=configuracion.ruta_base_datos,
    )

    tarea_central = asyncio.create_task(
        cliente_central.ejecutar(),
        name=f"cliente-central-{configuracion.nombre_grupo}",
    )

    def _reportar_fin_tarea(tarea: asyncio.Task[None]) -> None:
        """Si la tarea muere por un error inesperado, que quede en el log."""
        if tarea.cancelled():
            return

        error = tarea.exception()
        if error is not None:
            registrar_error(
                "ERROR",
                "la tarea del cliente central terminó",
                detalle=repr(error),
            )

    tarea_central.add_done_callback(_reportar_fin_tarea)

    try:
        yield
    finally:
        cliente_central.solicitar_apagado()
        tarea_central.cancel()

        with suppress(asyncio.CancelledError):
            await tarea_central

        base_datos.cerrar()
        registrar_evento("APAGADO", servidor=configuracion.nombre_grupo)


app = FastAPI(
    title=f"Chat distribuido - {configuracion.nombre_grupo}",
    version="1.0.0",
    description=(
        "Servidor de grupo para la sala global #general. "
        "Arquitectura en estrella: todo pasa por el servidor central."
    ),
    lifespan=ciclo_vida,
)


def _identidad(nombre_usuario: str) -> str:
    """Identificador visible del usuario, por ejemplo ana@g3."""
    return f"{nombre_usuario}@{configuracion.nombre_grupo}"


# ---------------------------------------------------------------------------
# Información y salud
# ---------------------------------------------------------------------------


@app.get("/", response_model=InfoServicio, tags=["info"])
def informacion_servicio() -> InfoServicio:
    """Datos útiles para saber a qué servidor estamos conectados."""
    return InfoServicio(
        server=configuracion.nombre_grupo,
        public_url=configuracion.url_publica,
        central_url=configuracion.url_central,
        lamport=reloj.valor,
    )


@app.get("/health", response_model=RespuestaSalud, tags=["info"])
def salud() -> RespuestaSalud:
    """Salud del propio servidor de grupo (la usa el healthcheck de Docker)."""
    return RespuestaSalud(
        status="ok",
        server=configuracion.nombre_grupo,
        lamport=reloj.valor,
    )


@app.get("/status", response_model=EstadoServidor, tags=["info"])
def estado_servidor() -> EstadoServidor:
    """Estado que usa el CLI y el script del experimento."""
    return EstadoServidor(
        server=configuracion.nombre_grupo,
        lamport=reloj.valor,
        central_online=cliente_central.en_linea,
        pending_messages=base_datos.contar_pendientes(),
        users=base_datos.contar_usuarios(),
    )


# ---------------------------------------------------------------------------
# API local: usuarios
# ---------------------------------------------------------------------------


@app.post(
    "/users",
    response_model=InfoUsuario,
    status_code=status.HTTP_201_CREATED,
    tags=["local"],
)
def crear_usuario(datos: SolicitudCreacionUsuario) -> InfoUsuario:
    """Crea un usuario local del grupo."""
    if not base_datos.agregar_usuario(datos.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El usuario {datos.username!r} ya existe",
        )

    registrar_evento("USUARIO_CREADO", usuario=_identidad(datos.username))
    return InfoUsuario(
        username=datos.username,
        identity=_identidad(datos.username),
    )


@app.get("/users", response_model=ListaUsuarios, tags=["local"])
def listar_usuarios() -> ListaUsuarios:
    """Lista los usuarios locales con su identidad completa usuario@gN."""
    return ListaUsuarios(
        users=[
            InfoUsuario(username=nombre, identity=_identidad(nombre))
            for nombre in base_datos.listar_usuarios()
        ]
    )


# ---------------------------------------------------------------------------
# API local: mensajes
# ---------------------------------------------------------------------------


@app.post(
    "/messages",
    response_model=Mensaje,
    status_code=status.HTTP_201_CREATED,
    tags=["local"],
)
def enviar_mensaje(datos: SolicitudEnvioMensaje) -> Mensaje:
    """Un usuario local escribe en #general.

    El mensaje se guarda de inmediato en el historial local y se encola para
    enviarlo al central. Responder rápido permite que el cliente siga
    conversando aunque el central esté caído; la entrega queda a cargo de la
    cola persistente.
    """
    if not base_datos.existe_usuario(datos.user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"El usuario {datos.user!r} no existe. Créalo con POST /users",
        )

    # Regla de Lamport para emisión: incrementar antes de enviar.
    lamport = reloj.avanzar()

    # Se genera el UUID en el origen. Este id viaja sin cambios en todos los
    # reintentos y es la clave que usan los receptores para deduplicar.
    mensaje = Mensaje.model_validate(
        {
            "id": str(uuid.uuid4()),
            "from": _identidad(datos.user),
            "origin": configuracion.nombre_grupo,
            "lamport": lamport,
            "sent_at": ahora_utc_iso(),
            "body": datos.body,
        }
    )

    if not base_datos.insertar_mensaje(mensaje):
        # Con UUIDv4 la colisión es prácticamente imposible; si ocurriera,
        # preferimos fallar de forma visible antes que reutilizar un id.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Colisión de UUID al guardar el mensaje",
        )

    # La cola es persistente: si el central está caído o el proceso se
    # reinicia, el mensaje sigue pendiente y se reintenta más tarde.
    base_datos.encolar_salida(mensaje.id)

    registrar_evento(
        "MENSAJE_LOCAL",
        usuario=mensaje.from_,
        id=mensaje.id,
        lamport=mensaje.lamport,
        pendientes=base_datos.contar_pendientes(),
    )
    return mensaje


@app.get("/messages", response_model=ListaMensajes, tags=["local"])
def listar_mensajes(
    since: int = Query(
        0,
        ge=0,
        description=(
            "Devuelve mensajes con lamport >= since. El cliente pide solapamiento "
            "y descarta ids repetidos."
        ),
    ),
    limit: int = Query(500, ge=1, le=5000),
) -> ListaMensajes:
    """Historial local ordenado por (lamport, origin)."""
    return ListaMensajes(messages=base_datos.listar_mensajes(since, limit))


# ---------------------------------------------------------------------------
# API federada: la llama el servidor central
# ---------------------------------------------------------------------------


@app.get("/fed/health", response_model=SaludFederada, tags=["federado"])
def salud_federada() -> SaludFederada:
    """Latido del servidor de grupo, visto desde el central."""
    return SaludFederada(
        server=configuracion.nombre_grupo,
        lamport=reloj.valor,
    )


@app.post(
    "/fed/messages",
    response_model=RespuestaRecepcionFederada,
    tags=["federado"],
)
def recibir_mensaje_federado(mensaje: Mensaje) -> RespuestaRecepcionFederada:
    """Recibe un mensaje reenviado por el central.

    Es idempotente: si el UUID ya existe, responde 200 con
    duplicate=true y no modifica nada: ni el historial ni el reloj
    de Lamport, porque un reintento no es un evento nuevo.
    Esto es obligatorio porque el emisor no puede distinguir entre
    "no llegó" y "llegó pero se perdió la respuesta"; por eso puede
    reenviar el mismo id.

    Los campos JSON desconocidos se ignoran (extra="ignore").
    Un id que no sea un UUID válido responde 422.
    """
    if not base_datos.insertar_mensaje(mensaje):
        registrar_evento(
            "DESCARTE_DUPLICADO",
            origen="central",
            id=mensaje.id,
            lamport=mensaje.lamport,
        )
        return RespuestaRecepcionFederada(
            status="duplicate",
            id=mensaje.id,
            duplicate=True,
        )

    # Recepción nueva: L = max(L, L_recibido) + 1.
    nuevo_valor_reloj = reloj.observar(mensaje.lamport)

    # Recordamos hasta dónde hemos visto el historial del central. Tras una
    # caída pediremos GET /history?since=esta_marca.
    base_datos.avanzar_estado_entero("last_history_lamport", mensaje.lamport)

    registrar_evento(
        "RECEPCIÓN",
        origen="central",
        usuario=mensaje.from_,
        id=mensaje.id,
        lamport=mensaje.lamport,
        lamport_local=nuevo_valor_reloj,
    )
    return RespuestaRecepcionFederada(
        status="received",
        id=mensaje.id,
        duplicate=False,
    )
