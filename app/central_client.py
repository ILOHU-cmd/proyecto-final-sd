"""Cliente hacia el servidor central.

Este módulo concentra la parte de "sistemas distribuidos" del servidor:

1. Registro periódico (POST /register) para que el central sepa nuestra URL.
2. Heartbeat pull (GET /health). Tras ``HEARTBEAT_FAILURES`` fallos seguidos
   marcamos el central como caído y dejamos de enviar.
3. Cola de salida persistente con entrega al menos una vez. Cada mensaje se
   reintenta con espera exponencial + jitter. Los 4xx no se reintentan.
4. Recuperación: al volver el central se renueva el registro y se pide
   GET /history?since={lamport} para recuperar lo perdido durante la caída.

Se usa httpx.AsyncClient para no bloquear el servidor mientras se espera la
respuesta del central. Las tres tareas (registro, heartbeat y envío) corren
concurrentemente en el mismo bucle de asyncio.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.database import Database, OutboxItem
from app.lamport import LamportClock
from app.logger import log_error, log_event
from app.models import Message, RegisterRequest


def exponential_backoff(attempt: int, base: float, cap: float) -> float:
    """Espera exponencial con jitter.

    ``attempt`` empieza en 1. El valor base se duplica en cada intento y se
    limita con ``cap``. El jitter (variación aleatoria de ±50 %) evita que
    todos los servidores reintenten exactamente al mismo tiempo cuando el
    central vuelve: es el problema de la "manada desbocada" o thundering herd.

    El cálculo es determinista en su parte exponencial, pero aleatorio en la
    espera final, así que dos mensajes que fallaron juntos no se reintentan
    sincronizados. El resultado se vuelve a limitar con ``cap`` para que el
    tope sea real incluso después de aplicar el jitter.
    """
    exponential = min(cap, base * (2 ** max(0, attempt - 1)))
    jittered = random.uniform(exponential * 0.5, exponential * 1.5)
    return min(cap, jittered)


class CentralClient:
    """Tareas de fondo que mantienen vivo el enlace con el servidor central."""

    #: Intentos máximos para una sincronización puntual de historial.
    _SYNC_ATTEMPTS = 3

    def __init__(
        self, settings: Settings, database: Database, clock: LamportClock
    ) -> None:
        self.settings = settings
        self.db = database
        self.clock = clock

        # ``online`` lo leen los endpoints (en hilos del threadpool) y lo
        # escriben las tareas de fondo. Un bool es atómico en CPython, así que
        # no hace falta un candado para este indicador.
        self.online = False

        self._client: httpx.AsyncClient | None = None
        self._stopped = False
        self._sync_lock = asyncio.Lock()

        # La primera sincronización tras arrancar pide el historial completo
        # (since=0). Así, aunque una entrega anterior haya llegado fuera de
        # orden, no dejamos huecos: la deduplicación por UUID hace que volver
        # a pedir mensajes ya conocidos sea inofensivo. Las recuperaciones
        # posteriores (sin reiniciar el proceso) usan la marca incremental.
        self._startup_sync = True

    @property
    def is_online(self) -> bool:
        return self.online

    def request_stop(self) -> None:
        """Pide a las tareas que terminen; el apagado real lo hace lifespan."""
        self._stopped = True

    # ------------------------------------------------------------------
    # Arranque
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Crea el cliente HTTP y lanza las tres tareas de fondo."""
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            self._client = client

            # Se crean tareas explícitas para poder cancelarlas todas si una
            # falla o si la aplicación se apaga. Sin esto, una excepción en
            # una tarea dejaría a las otras corriendo como huérfanas.
            tasks = [
                asyncio.create_task(self._registration_loop(), name="central-register"),
                asyncio.create_task(self._heartbeat_loop(), name="central-heartbeat"),
                asyncio.create_task(self._outbox_loop(), name="central-outbox"),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._client = None

    # ------------------------------------------------------------------
    # Registro periódico
    # ------------------------------------------------------------------

    async def _registration_loop(self) -> None:
        """Renueva el registro cada ``REGISTER_INTERVAL_SECONDS``.

        El registro es idempotente en el central (upsert por nombre de
        servidor), así que renovarlo no crea duplicados. Si falla, se
        reintenta con espera exponencial + jitter, sin pasar del intervalo
        normal de renovación.
        """
        failures = 0
        while not self._stopped:
            success = await self._register_once()
            interval = self.settings.register_interval_seconds

            if success:
                failures = 0
                # Pequeño jitter también en la renovación: si todos los
                # grupos arrancan al mismo tiempo, no queremos que todos
                # golpeen el central en el mismo instante.
                delay = interval + random.uniform(0.0, interval * 0.1)
            else:
                failures += 1
                delay = min(
                    interval,
                    exponential_backoff(
                        failures,
                        self.settings.retry_base_seconds,
                        self.settings.retry_max_seconds,
                    ),
                )
                log_event(
                    "REINTENTO",
                    operacion="POST /register",
                    intento_fallido=failures,
                    espera=round(delay, 3),
                )

            await asyncio.sleep(delay)

    async def _register_once(self) -> bool:
        if self._client is None:
            return False

        payload = RegisterRequest(
            server=self.settings.group_name, url=self.settings.public_url
        ).model_dump()

        try:
            response = await self._client.post(
                f"{self.settings.central_url}/register", json=payload
            )
        except httpx.HTTPError as exc:
            log_event(
                "REGISTRO_FALLO",
                motivo=type(exc).__name__,
                central=self.settings.central_url,
            )
            return False

        if 200 <= response.status_code < 300:
            log_event(
                "REGISTRO_OK",
                servidor=self.settings.group_name,
                url=self.settings.public_url,
            )
            return True

        log_event("REGISTRO_FALLO", status=response.status_code)
        return False

    # ------------------------------------------------------------------
    # Detección de fallos (heartbeat pull)
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Consulta GET /health y decide si el central está arriba o abajo."""
        failures = 0

        while not self._stopped:
            healthy = await self._check_health()

            if healthy:
                failures = 0
                if not self.online:
                    # Primer contacto o vuelta de una caída: antes de enviar
                    # nada, recuperamos el historial que nos perdimos.
                    await self._recover()
            else:
                failures += 1
                if self.online and failures >= self.settings.heartbeat_failures:
                    self.online = False
                    log_event(
                        "CAÍDA_CENTRAL",
                        intentos_fallidos=failures,
                        mensajes_pendientes=self.db.pending_outbox_count(),
                    )
                elif not self.online and failures == self.settings.heartbeat_failures:
                    log_event(
                        "CENTRAL_NO_DISPONIBLE",
                        intentos_fallidos=failures,
                        nota="se sigue consultando el heartbeat",
                    )

            await asyncio.sleep(self.settings.heartbeat_interval_seconds)

    async def _check_health(self) -> bool:
        if self._client is None:
            return False
        try:
            response = await self._client.get(
                f"{self.settings.central_url}/health",
                timeout=self.settings.heartbeat_timeout_seconds,
            )
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 300

    # ------------------------------------------------------------------
    # Recuperación de historial
    # ------------------------------------------------------------------

    async def _recover(self) -> None:
        """Renueva el registro y sincroniza el historial antes de volver a enviar."""
        # Evita que dos heartbeats exitosos lancen dos sincronizaciones.
        if self._sync_lock.locked():
            return

        async with self._sync_lock:
            if self._stopped:
                return

            log_event("RECUPERACIÓN_INICIO", central=self.settings.central_url)

            # Si el central se reinició, pudo perder su registro en memoria.
            # Renovar el registro es barato y idempotente.
            await self._register_once()

            if not await self._sync_history():
                # No marcamos online: el próximo heartbeat volverá a intentar
                # la recuperación. Es mejor no enviar a ciegas y arriesgar
                # perder el orden del historial.
                log_event(
                    "RECUPERACIÓN_FALLO",
                    nota="se reintentará en el próximo heartbeat",
                )
                return

            self.online = True
            log_event(
                "RECUPERACIÓN_OK",
                lamport=self.clock.value,
                pendientes=self.db.pending_outbox_count(),
            )

    async def _sync_history(self) -> bool:
        """Pide al central los mensajes posteriores a nuestra última marca.

        ``last_history_lamport`` es la mayor marca de Lamport que hemos visto
        procedente del central. Pedimos desde ``max(0, marca - 1)`` para
        incluir mensajes que compartan la misma marca: el historial se ordena
        por (lamport, origin) y varios orígenes pueden coincidir en lamport.
        Pedir de más es inofensivo gracias a la deduplicación por UUID.
        """
        if self._client is None:
            return False

        since = int(self.db.get_state("last_history_lamport", "0") or 0)
        if self._startup_sync:
            request_since = 0
            modo = "completa"
        else:
            # Se pide desde una marca anterior para incluir mensajes que
            # compartan el mismo lamport. Pedir de más es inofensivo porque
            # el receptor deduplica por UUID.
            request_since = max(0, since - 1)
            modo = "incremental"

        log_event(
            "HISTORIAL_INICIO",
            modo=modo,
            desde=request_since,
            ultima_marca=since,
        )

        response: httpx.Response | None = None
        last_reason = "sin respuesta"

        for attempt in range(1, self._SYNC_ATTEMPTS + 1):
            try:
                response = await self._client.get(
                    f"{self.settings.central_url}/history",
                    params={"since": request_since},
                )
            except httpx.HTTPError as exc:
                response = None
                last_reason = type(exc).__name__
            else:
                if 200 <= response.status_code < 300:
                    break
                if 400 <= response.status_code < 500:
                    # Un 4xx es un error del cliente/protocolo: reintentarlo
                    # no lo va a arreglar.
                    log_event(
                        "HISTORIAL_FALLO",
                        status=response.status_code,
                        motivo="4xx no se reintenta",
                    )
                    return False
                last_reason = f"HTTP {response.status_code}"
                response = None

            if attempt < self._SYNC_ATTEMPTS:
                delay = exponential_backoff(
                    attempt,
                    self.settings.retry_base_seconds,
                    self.settings.retry_max_seconds,
                )
                log_event(
                    "REINTENTO",
                    operacion="GET /history",
                    intento_fallido=attempt,
                    proximo_intento=attempt + 1,
                    espera=round(delay, 3),
                    motivo=last_reason,
                )
                await asyncio.sleep(delay)

        if response is None:
            log_error("HISTORIAL_FALLO", motivo=last_reason)
            return False

        try:
            payload: Any = response.json()
        except ValueError:
            log_error("HISTORIAL_FALLO", motivo="respuesta no es JSON")
            return False

        # La especificación publicada manda; se aceptan las dos formas más
        # habituales para no romper la interoperabilidad: lista directa o
        # {"messages": [...]}.
        if isinstance(payload, list):
            raw_messages = payload
        elif isinstance(payload, dict):
            raw_messages = payload.get("messages", payload.get("history", []))
        else:
            log_error("HISTORIAL_FALLO", motivo="forma de respuesta no reconocida")
            return False

        if not isinstance(raw_messages, list):
            log_error("HISTORIAL_FALLO", motivo="campo messages no es una lista")
            return False

        max_lamport = since
        new_count = 0

        for raw in raw_messages:
            if not isinstance(raw, dict):
                log_event("MENSAJE_INVALIDO", origen="historial", motivo="no es objeto")
                continue
            try:
                message = Message.model_validate(raw)
            except ValidationError as exc:
                log_event(
                    "MENSAJE_INVALIDO",
                    origen="historial",
                    motivo=exc.errors()[0].get("msg", "error de validación"),
                )
                continue

            max_lamport = max(max_lamport, message.lamport)

            if self.db.insert_message(message):
                # Solo los mensajes nuevos mueven el reloj. Si moviéramos el
                # reloj con cada duplicado, una simple resincronización
                # inflaría el contador sin motivo.
                new_clock = self.clock.observe(message.lamport)
                new_count += 1
                log_event(
                    "RECEPCIÓN",
                    origen="historial",
                    id=message.id,
                    lamport=message.lamport,
                    lamport_local=new_clock,
                )
            else:
                log_event(
                    "DESCARTE_DUPLICADO",
                    origen="historial",
                    id=message.id,
                    lamport=message.lamport,
                )

        self.db.advance_state_int("last_history_lamport", max_lamport)
        self._startup_sync = False
        log_event(
            "HISTORIAL_SINCRONIZADO",
            desde=request_since,
            nuevos=new_count,
            total_recibidos=len(raw_messages),
            lamport_historial=max_lamport,
        )
        return True

    # ------------------------------------------------------------------
    # Cola de salida y reintentos
    # ------------------------------------------------------------------

    async def _outbox_loop(self) -> None:
        """Envía los mensajes pendientes cuando el central está disponible."""
        while not self._stopped:
            if not self.online or self._client is None:
                # Mientras el central está caído, los mensajes se acumulan en
                # SQLite. No se pierden si el servidor se reinicia.
                await asyncio.sleep(0.5)
                continue

            try:
                due = self.db.get_due_outbox(time.time(), limit=20)
            except Exception as exc:  # noqa: BLE001 - una tarea no debe morir
                log_error("ERROR", "no se pudo leer la cola", detalle=repr(exc))
                await asyncio.sleep(1.0)
                continue

            if not due:
                await asyncio.sleep(0.5)
                continue

            for item in due:
                if self._stopped or not self.online:
                    break
                await self._send_outbox_item(item)

            await asyncio.sleep(0.1)

    async def _send_outbox_item(self, item: OutboxItem) -> None:
        if self._client is None:
            return

        message = item.message
        attempt = item.attempts + 1

        log_event(
            "ENVÍO",
            destino="central",
            id=message.id,
            lamport=message.lamport,
            intento=attempt,
        )

        try:
            response = await self._client.post(
                f"{self.settings.central_url}/messages",
                json=message.to_protocol_dict(),
            )
        except httpx.HTTPError as exc:
            self._schedule_retry(message, item.attempts, type(exc).__name__)
            return

        if 200 <= response.status_code < 300:
            # El central confirmó. Solo ahora quitamos el mensaje de la cola:
            # si el proceso muere antes, se reenviará con el mismo id y el
            # central lo deduplicará.
            self.db.remove_outbox(message.id)
            log_event(
                "ENVÍO_OK",
                id=message.id,
                lamport=message.lamport,
                status=response.status_code,
            )
            return

        if 400 <= response.status_code < 500:
            # El protocolo dice que un 4xx no se reintenta. Se descarta y se
            # deja rastro para poder explicarlo en la bitácora.
            self.db.remove_outbox(message.id)
            log_event(
                "DESCARTE_4XX",
                id=message.id,
                lamport=message.lamport,
                status=response.status_code,
                detalle=response.text[:160].replace("\n", " "),
            )
            return

        # 5xx: el central falló temporalmente. Se reintenta.
        self._schedule_retry(message, item.attempts, f"HTTP {response.status_code}")

    def _schedule_retry(
        self, message: Message, previous_attempts: int, reason: str
    ) -> None:
        attempts = previous_attempts + 1
        delay = exponential_backoff(
            attempts,
            self.settings.retry_base_seconds,
            self.settings.retry_max_seconds,
        )
        next_retry_at = time.time() + delay
        self.db.schedule_outbox_retry(message.id, attempts, next_retry_at)
        log_event(
            "REINTENTO",
            id=message.id,
            lamport=message.lamport,
            intento_fallido=attempts,
            proximo_intento=attempts + 1,
            espera=round(delay, 3),
            motivo=reason,
        )
