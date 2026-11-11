"""Servidor central de prueba (NO forma parte del entregable del grupo).

El servidor central real lo opera el profesor. Este mock existe solo para que
el equipo pueda probar el sistema completo en su computador sin depender de
la red del curso. Implementa el mismo contrato mínimo descrito en el
enunciado:

    POST /register
    POST /messages
    GET  /history?since={lamport}
    GET  /health

Guarda todo en memoria. Si se reinicia, pierde el historial: es suficiente
para desarrollo y para el experimento de caída del servidor de grupo, pero no
para la sesión de interoperabilidad oficial.

Para levantarlo:

    python scripts/mock_central.py --port 9000

Para demostrar reintentos y descartes se pueden inyectar fallos:

    python scripts/mock_central.py --port 9000 --fail-first 2 --reject-after 1

El primer mensaje recibirá dos respuestas 500 y el tercer intento será
aceptado; el siguiente mensaje recibirá 400 y el grupo lo descartará sin
reintentar.
"""

from __future__ import annotations

import argparse
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [central-mock] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
registro = logging.getLogger("mock-central")


class CuerpoRegistro(BaseModel):
    server: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def _validar_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("url debe comenzar por http:// o https://")
        return value


class CuerpoMensaje(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    origin: str
    lamport: int = Field(ge=0)
    sent_at: str
    body: str


class EstadoCentral:
    """Registro de servidores e historial global, protegidos por un candado."""

    def __init__(self) -> None:
        self.candado = threading.RLock()
        self.servidores: dict[str, str] = {}
        self.mensajes: list[dict[str, Any]] = []
        self.identificadores: set[str] = set()

        # Inyección de fallos para la demo de reintentos (0/None = normal).
        self.fallar_primeros = 0
        self.rechazar_despues_de: int | None = None
        self.intentos = 0
        self.aceptados = 0

    def registrar(self, servidor: str, url: str) -> int:
        with self.candado:
            self.servidores[servidor] = url
            return len(self.servidores)

    def agregar_mensaje(self, mensaje: dict[str, Any]) -> bool:
        with self.candado:
            if mensaje["id"] in self.identificadores:
                return False

            self.identificadores.add(mensaje["id"])
            self.mensajes.append(mensaje)
            self.aceptados += 1
            return True

    def destinos_para(self, origen: str) -> list[tuple[str, str]]:
        with self.candado:
            return [
                (servidor, url)
                for servidor, url in self.servidores.items()
                if servidor != origen
            ]

    def historial(self, since: int, limite: int) -> list[dict[str, Any]]:
        with self.candado:
            seleccionados = [
                mensaje for mensaje in self.mensajes if mensaje["lamport"] > since
            ]
            seleccionados.sort(
                key=lambda mensaje: (
                    mensaje["lamport"],
                    mensaje["origin"],
                    mensaje["id"],
                )
            )
            return seleccionados[:limite]


estado = EstadoCentral()
_cliente_http: httpx.AsyncClient | None = None


@asynccontextmanager
async def ciclo_vida(_: FastAPI):
    global _cliente_http
    _cliente_http = httpx.AsyncClient(timeout=3.0)
    registro.info("servidor central de prueba listo")

    try:
        yield
    finally:
        if _cliente_http is not None:
            await _cliente_http.aclose()
        registro.info("servidor central de prueba apagado")


app = FastAPI(
    title="Central de prueba - chat distribuido",
    version="1.0.0",
    lifespan=ciclo_vida,
)


async def _reenviar(mensaje: dict[str, Any]) -> None:
    """Reenvía el mensaje a todos los servidores registrados menos al origen.

    El central del curso hace lo mismo. Si un grupo está caído, el envío
    falla; ese grupo recuperará el mensaje con GET /history al volver.
    """
    if _cliente_http is None:
        return

    destinos = estado.destinos_para(str(mensaje["origin"]))
    if not destinos:
        return

    for servidor, url in destinos:
        try:
            respuesta = await _cliente_http.post(
                f"{url}/fed/messages",
                json=mensaje,
            )
            registro.info(
                "reenvío id=%s lamport=%s -> %s status=%s",
                mensaje["id"],
                mensaje["lamport"],
                servidor,
                respuesta.status_code,
            )
        except httpx.HTTPError as error:
            registro.warning(
                "no se pudo reenviar id=%s a %s: %s",
                mensaje["id"],
                servidor,
                type(error).__name__,
            )


@app.get("/health")
def salud() -> dict[str, Any]:
    with estado.candado:
        return {
            "status": "ok",
            "servers": len(estado.servidores),
            "messages": len(estado.mensajes),
        }


@app.post("/register")
def registrar(datos: CuerpoRegistro) -> dict[str, Any]:
    total = estado.registrar(datos.server, datos.url)
    registro.info(
        "registro server=%s url=%s total=%s",
        datos.server,
        datos.url,
        total,
    )
    return {
        "status": "registered",
        "server": datos.server,
        "servers": total,
    }


@app.post("/messages")
def recibir_mensaje(
    datos: CuerpoMensaje,
    tareas_fondo: BackgroundTasks,
) -> dict[str, Any]:
    contenido = datos.model_dump(by_alias=True)

    # Fallos programados para la demo de reintentos. No forman parte del
    # protocolo: son una herramienta didáctica del central de prueba.
    with estado.candado:
        estado.intentos += 1
        intento = estado.intentos
        fallar_primeros = estado.fallar_primeros
        rechazar_despues_de = estado.rechazar_despues_de
        aceptados = estado.aceptados

    if intento <= fallar_primeros:
        registro.warning(
            "fallo temporal simulado (500) en el intento %s",
            intento,
        )
        raise HTTPException(
            status_code=500,
            detail="fallo temporal simulado",
        )

    if rechazar_despues_de is not None and aceptados >= rechazar_despues_de:
        registro.warning(
            "rechazo permanente simulado (400) en el intento %s",
            intento,
        )
        raise HTTPException(
            status_code=400,
            detail="rechazo permanente simulado",
        )

    # Idempotencia del lado del central: si el grupo reintenta porque se
    # perdió la respuesta, no se guarda dos veces.
    if not estado.agregar_mensaje(contenido):
        registro.info("duplicado descartado id=%s", contenido["id"])
        return {"status": "duplicate", "id": contenido["id"]}

    registro.info(
        "mensaje aceptado id=%s from=%s lamport=%s",
        contenido["id"],
        contenido["from"],
        contenido["lamport"],
    )

    # Se responde al emisor y el reenvío ocurre después (BackgroundTasks):
    # un grupo lento no debe bloquear al emisor.
    tareas_fondo.add_task(_reenviar, contenido)
    return {"status": "accepted", "id": contenido["id"]}


@app.get("/history")
def historial(
    since: int = Query(0, ge=0),
    limit: int = Query(10000, ge=1, le=100000),
) -> dict[str, Any]:
    mensajes = estado.historial(since, limit)
    registro.info("historial since=%s devueltos=%s", since, len(mensajes))
    return {"messages": mensajes}


@app.get("/servers")
def servidores() -> dict[str, str]:
    """Endpoint de depuración, no forma parte del protocolo."""
    with estado.candado:
        return dict(estado.servidores)


def principal() -> None:
    analizador = argparse.ArgumentParser(description="Central de prueba en memoria")
    analizador.add_argument("--host", default="127.0.0.1")
    analizador.add_argument("--port", type=int, default=9000)
    analizador.add_argument(
        "--fail-first",
        type=int,
        default=0,
        help="Devuelve 500 en los primeros N POST /messages (para ver reintentos).",
    )
    analizador.add_argument(
        "--reject-after",
        type=int,
        default=None,
        help="Después de aceptar N mensajes, devuelve 400 (para ver el descarte).",
    )
    argumentos = analizador.parse_args()

    estado.fallar_primeros = max(0, argumentos.fail_first)
    estado.rechazar_despues_de = argumentos.reject_after
    uvicorn.run(
        app,
        host=argumentos.host,
        port=argumentos.port,
        log_level="info",
    )


if __name__ == "__main__":
    principal()
