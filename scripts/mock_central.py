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
logger = logging.getLogger("mock-central")


class RegisterPayload(BaseModel):
    server: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("url debe comenzar por http:// o https://")
        return value


class MessagePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    origin: str
    lamport: int = Field(ge=0)
    sent_at: str
    body: str


class CentralState:
    """Registro de servidores e historial global, protegidos por un candado."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.servers: dict[str, str] = {}
        self.messages: list[dict[str, Any]] = []
        self.ids: set[str] = set()

        # Inyección de fallos para la demo de reintentos (0/None = normal).
        self.fail_first = 0
        self.reject_after: int | None = None
        self.attempts = 0
        self.accepted = 0

    def register(self, server: str, url: str) -> int:
        with self.lock:
            self.servers[server] = url
            return len(self.servers)

    def add_message(self, message: dict[str, Any]) -> bool:
        with self.lock:
            if message["id"] in self.ids:
                return False
            self.ids.add(message["id"])
            self.messages.append(message)
            self.accepted += 1
            return True

    def targets_for(self, origin: str) -> list[tuple[str, str]]:
        with self.lock:
            return [
                (server, url)
                for server, url in self.servers.items()
                if server != origin
            ]

    def history(self, since: int, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            selected = [m for m in self.messages if m["lamport"] > since]
            selected.sort(key=lambda m: (m["lamport"], m["origin"], m["id"]))
            return selected[:limit]


state = CentralState()
_http: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _http
    _http = httpx.AsyncClient(timeout=3.0)
    logger.info("servidor central de prueba listo")
    try:
        yield
    finally:
        if _http is not None:
            await _http.aclose()
        logger.info("servidor central de prueba apagado")


app = FastAPI(
    title="Central de prueba - chat distribuido",
    version="1.0.0",
    lifespan=lifespan,
)


async def _forward(message: dict[str, Any]) -> None:
    """Reenvía el mensaje a todos los servidores registrados menos al origen.

    El central del curso hace lo mismo. Si un grupo está caído, el envío
    falla; ese grupo recuperará el mensaje con GET /history al volver.
    """
    if _http is None:
        return

    targets = state.targets_for(str(message["origin"]))
    if not targets:
        return

    for server, url in targets:
        try:
            response = await _http.post(f"{url}/fed/messages", json=message)
            logger.info(
                "reenvío id=%s lamport=%s -> %s status=%s",
                message["id"],
                message["lamport"],
                server,
                response.status_code,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "no se pudo reenviar id=%s a %s: %s",
                message["id"],
                server,
                type(exc).__name__,
            )


@app.get("/health")
def health() -> dict[str, Any]:
    with state.lock:
        return {
            "status": "ok",
            "servers": len(state.servers),
            "messages": len(state.messages),
        }


@app.post("/register")
def register(payload: RegisterPayload) -> dict[str, Any]:
    total = state.register(payload.server, payload.url)
    logger.info(
        "registro server=%s url=%s total=%s", payload.server, payload.url, total
    )
    return {"status": "registered", "server": payload.server, "servers": total}


@app.post("/messages")
def post_message(
    payload: MessagePayload, background: BackgroundTasks
) -> dict[str, Any]:
    data = payload.model_dump(by_alias=True)

    # Fallos programados para la demo de reintentos. No forman parte del
    # protocolo: son una herramienta didáctica del central de prueba.
    with state.lock:
        state.attempts += 1
        attempt = state.attempts
        fail_first = state.fail_first
        reject_after = state.reject_after
        accepted = state.accepted

    if attempt <= fail_first:
        logger.warning("fallo temporal simulado (500) en el intento %s", attempt)
        raise HTTPException(status_code=500, detail="fallo temporal simulado")

    if reject_after is not None and accepted >= reject_after:
        logger.warning("rechazo permanente simulado (400) en el intento %s", attempt)
        raise HTTPException(status_code=400, detail="rechazo permanente simulado")

    # Idempotencia del lado del central: si el grupo reintenta porque se
    # perdió la respuesta, no se guarda dos veces.
    if not state.add_message(data):
        logger.info("duplicado descartado id=%s", data["id"])
        return {"status": "duplicate", "id": data["id"]}

    logger.info(
        "mensaje aceptado id=%s from=%s lamport=%s",
        data["id"],
        data["from"],
        data["lamport"],
    )

    # Se responde al emisor y el reenvío ocurre después (BackgroundTasks):
    # un grupo lento no debe bloquear al emisor.
    background.add_task(_forward, data)
    return {"status": "accepted", "id": data["id"]}


@app.get("/history")
def history(
    since: int = Query(0, ge=0), limit: int = Query(10000, ge=1, le=100000)
) -> dict[str, Any]:
    messages = state.history(since, limit)
    logger.info("historial since=%s devueltos=%s", since, len(messages))
    return {"messages": messages}


@app.get("/servers")
def servers() -> dict[str, str]:
    """Endpoint de depuración, no forma parte del protocolo."""
    with state.lock:
        return dict(state.servers)


def main() -> None:
    parser = argparse.ArgumentParser(description="Central de prueba en memoria")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--fail-first",
        type=int,
        default=0,
        help="Devuelve 500 en los primeros N POST /messages (para ver reintentos).",
    )
    parser.add_argument(
        "--reject-after",
        type=int,
        default=None,
        help="Después de aceptar N mensajes, devuelve 400 (para ver el descarte).",
    )
    args = parser.parse_args()

    state.fail_first = max(0, args.fail_first)
    state.reject_after = args.reject_after
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
