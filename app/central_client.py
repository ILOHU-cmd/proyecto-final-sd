"""Cliente hacia el servidor central.

Este módulo es el encargado de mantener el enlace entre nuestro servidor de
grupo y el servidor central. Mientras el servidor está encendido, tres tareas
de fondo corren al mismo tiempo:

1. **Registro** (``_registration_loop``): envía ``POST /register`` cada ciertos
   segundos para que el central sepa nuestra URL. Se renueva periódicamente
   porque el central pudo reiniciarse y perder su lista de servidores.

2. **Heartbeat** (``_heartbeat_loop``): consulta ``GET /health`` del central.
   Si falla varias veces seguidas, marcamos el central como "caído" y dejamos
   de intentar enviar. Al volver, recuperamos el historial perdido.

3. **Cola de salida** (``_outbox_loop``): saca de SQLite los mensajes locales
   que aún no fueron aceptados por el central y los envía uno por uno con
   ``POST /messages``.

Estas tres tareas usan conceptos de sistemas distribuidos:

- **Entrega al menos una vez**: un mensaje se reintenta hasta que el central
  responde ``2xx``. Si se pierde la respuesta, se reenvía con el mismo ``id``
  y el central lo deduplica (por eso no importa reintentar de más).
- **Espera exponencial con jitter**: entre un reintento y otro se espera un
  tiempo que crece (1s, 2s, 4s, ...) con una variación aleatoria, para que
  todos los grupos no reintenten a la vez cuando el central vuelve.
- **Heartbeat en modo "pull"**: somos nosotros quienes preguntamos al central
  si está vivo, en lugar de esperar que él nos avise. Así detectamos la caída
  por nuestra cuenta.
- **Sincronización de historial**: al arrancar o al volver de una caída
  pedimos ``GET /history?since=...`` para recuperar los mensajes que nos
  perdimos mientras estábamos desconectados.

Se usa ``httpx.AsyncClient`` para no bloquear el servidor mientras se espera
la respuesta del central.
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
    """Calcula cuántos segundos esperar antes del siguiente reintento.

    Recibe:

    - ``attempt``: número del intento que acaba de fallar (empieza en 1).
    - ``base``: espera inicial (por defecto 1 segundo).
    - ``cap``: espera máxima permitida (por defecto 30 segundos).

    La idea es que la espera **crezca** con cada intento para no saturar al
    central con reintentos inmediatos. Sin embargo, si todos los grupos
    reintentaran exactamente a los mismos tiempos, golpearían al central a la
    vez: eso se llama *thundering herd* (manada desbocada). Por eso se añade
    **jitter**, una variación aleatoria de ±50 %, que desincroniza a todos.

    Ejemplo con ``base=1`` y ``cap=30``:

    - intento 1 -> alrededor de 1 segundo,
    - intento 2 -> alrededor de 2 segundos,
    - intento 3 -> alrededor de 4 segundos,
    - ... hasta que llega al tope de 30 segundos y no sube más.
    """
    # Parte exponencial: se duplica en cada intento y se limita con `cap`.
    exponential = min(cap, base * (2 ** max(0, attempt - 1)))

    # Jitter: variación aleatoria de ±50 % sobre ese valor.
    jittered = random.uniform(exponential * 0.5, exponential * 1.5)

    # Se vuelve a limitar con `cap`: el jitter no debe romper el tope.
    return min(cap, jittered)


class CentralClient:
    """Tareas de fondo que mantienen vivo el enlace con el servidor central."""

    #: Número máximo de intentos para una sincronización puntual de historial.
    _SYNC_ATTEMPTS = 3

    def __init__(
        self, settings: Settings, database: Database, clock: LamportClock
    ) -> None:
        self.settings = settings
        self.db = database
        self.clock = clock

        # ``online`` indica si el central está disponible. Lo leen los
        # endpoints (desde los hilos de FastAPI) y lo escriben las tareas de
        # fondo. Un booleano es atómico en CPython, por eso no necesita
        # candado. Mientras sea False, la cola de salida no intenta enviar.
        self.online = False

        # Cliente HTTP compartido por las tres tareas. Se crea en ``run``.
        self._client: httpx.AsyncClient | None = None

        # Bandera para pedir el apagado de las tareas desde fuera.
        self._stopped = False

        # Evita que dos sincronizaciones de historial corran a la vez.
        self._sync_lock = asyncio.Lock()

        # La primera sincronización tras arrancar pide el historial completo
        # (since=0). Así no quedan huecos aunque una entrega anterior haya
        # llegado fuera de orden. Las recuperaciones posteriores, sin reiniciar
        # el proceso, usan la marca incremental (since=última_marca - 1).
        self._startup_sync = True

    @property
    def is_online(self) -> bool:
        """Verdadero si el central está disponible. Lo consulta el endpoint /status."""
        return self.online

    def request_stop(self) -> None:
        """Pide a las tareas que terminen; el apagado real lo hace ``lifespan``."""
        self._stopped = True

    # ------------------------------------------------------------------
    # Arranque
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Crea el cliente HTTP y lanza las tres tareas de fondo.

        Las tres corren de forma concurrente: mientras el heartbeat comprueba
        si el central está vivo, la cola de salida puede estar enviando un
        mensaje. Comparten el mismo ``httpx.AsyncClient``, que es seguro para
        hacer varias peticiones a la vez.
        """
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            self._client = client

            # Se crean como tareas explícitas para poder cancelarlas todas si
            # una falla o si la aplicación se apaga. Sin esto, una excepción en
            # una tarea dejaría a las otras corriendo "huérfanas" en segundo
            # plano.
            tasks = [
                asyncio.create_task(self._registration_loop(), name="central-register"),
                asyncio.create_task(self._heartbeat_loop(), name="central-heartbeat"),
                asyncio.create_task(self._outbox_loop(), name="central-outbox"),
            ]
            try:
                # Espera a que las tres terminen. Solo terminan cuando se pide
                # el apagado (request_stop) o cuando ocurre un error inesperado.
                await asyncio.gather(*tasks)
            finally:
                # Al salir, se cancelan las tres y se cierra el cliente HTTP.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self._client = None

    # ------------------------------------------------------------------
    # Registro periódico
    # ------------------------------------------------------------------

    async def _registration_loop(self) -> None:
        """Renueva el registro cada ``REGISTER_INTERVAL_SECONDS``.

        El registro en el central es idempotente: registrarse varias veces con
        el mismo nombre solo actualiza la URL, no crea entradas repetidas. Por
        eso se puede renovar sin miedo.

        Si una renovación falla (central caído), se reintenta con backoff
        exponencial + jitter, pero nunca se espera más que el intervalo normal
        de renovación.
        """
        failures = 0  # fallos de registro consecutivos

        while not self._stopped:
            success = await self._register_once()
            interval = self.settings.register_interval_seconds

            if success:
                failures = 0
                # Pequeño jitter también en la renovación: si todos los grupos
                # arrancan a la vez, no queremos que todos registren en el
                # mismo instante.
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
        """Hace una sola petición ``POST /register`` y devuelve si funcionó.

        El cuerpo es exactamente el que pide la especificación:
        ``{"server": "g3", "url": "http://..."}``.
        """
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
            # Error de red o timeout: el central no respondió.
            log_event(
                "REGISTRO_FALLO",
                motivo=type(exc).__name__,
                central=self.settings.central_url,
            )
            return False

        # Cualquier respuesta 2xx significa que el registro fue aceptado.
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
        """Consulta ``GET /health`` y decide si el central está arriba o abajo.

        Regla simple:

        - Si responde -> central arriba. Si estábamos caídos, nos recuperamos.
        - Si no responde -> sumamos un fallo. Al llegar a
          ``HEARTBEAT_FAILURES`` fallos seguidos, marcamos el central caído.
        """
        failures = 0  # fallos de salud consecutivos

        while not self._stopped:
            healthy = await self._check_health()

            if healthy:
                failures = 0
                if not self.online:
                    # Primer contacto o vuelta de una caída: antes de volver a
                    # enviar, recuperamos los mensajes que nos perdimos.
                    await self._recover()
            else:
                failures += 1
                if self.online and failures >= self.settings.heartbeat_failures:
                    # El central lleva varios fallos seguidos: lo marcamos
                    # caído. La cola de salida dejará de intentar enviar.
                    self.online = False
                    log_event(
                        "CAÍDA_CENTRAL",
                        intentos_fallidos=failures,
                        mensajes_pendientes=self.db.pending_outbox_count(),
                    )
                elif not self.online and failures == self.settings.heartbeat_failures:
                    # Ya estábamos caídos; solo registramos que seguimos sin
                    # central, una vez, para no llenar el log.
                    log_event(
                        "CENTRAL_NO_DISPONIBLE",
                        intentos_fallidos=failures,
                        nota="se sigue consultando el heartbeat",
                    )

            await asyncio.sleep(self.settings.heartbeat_interval_seconds)

    async def _check_health(self) -> bool:
        """Hace una sola petición ``GET /health`` y devuelve si respondió bien.

        El timeout de esta petición es más corto que el general, para que un
        central colgado se detecte rápido.
        """
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
        """Renueva el registro y sincroniza el historial antes de volver a enviar.

        El orden importa:

        1. Renovar el registro (el central pudo reiniciarse y olvidarnos).
        2. Pedir el historial que nos perdimos.
        3. Solo entonces marcar ``online = True``.

        Si la sincronización falla, nos mantenemos caídos y lo intentamos de
        nuevo en el siguiente heartbeat. Es mejor no enviar a ciegas y
        arriesgar un historial incompleto.
        """
        # Evita que dos heartbeats exitosos lancen dos sincronizaciones a la
        # vez. Como el bucle es secuencial, esto casi nunca pasa, pero el
        # candado deja la intención clara.
        if self._sync_lock.locked():
            return

        async with self._sync_lock:
            if self._stopped:
                return

            log_event("RECUPERACIÓN_INICIO", central=self.settings.central_url)

            # Renovar el registro es barato e idempotente.
            await self._register_once()

            if not await self._sync_history():
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

        Llevamos en SQLite la clave ``last_history_lamport``: la mayor marca de
        Lamport que hemos visto venir del central. Al pedir historial usamos
        ``since = max(0, marca - 1)``: restar 1 a propósito incluye mensajes
        que comparten la misma marca, porque el orden es ``(lamport, origin)``
        y dos orígenes pueden coincidir en el número. Pedir de más no hace daño
        porque el receptor deduplica por UUID.

        La primera vez (al arrancar) se pide ``since=0``: el historial
        completo, para no dejar huecos.
        """
        if self._client is None:
            return False

        # Marca desde la que pedimos. `_startup_sync` distingue el arranque
        # (completa) de una reconexión (incremental).
        since = int(self.db.get_state("last_history_lamport", "0") or 0)
        if self._startup_sync:
            request_since = 0
            modo = "completa"
        else:
            request_since = max(0, since - 1)
            modo = "incremental"

        log_event(
            "HISTORIAL_INICIO",
            modo=modo,
            desde=request_since,
            ultima_marca=since,
        )

        # --- 1. Pedir el historial, con reintentos y backoff. ---
        response: httpx.Response | None = None
        last_reason = "sin respuesta"

        for attempt in range(1, self._SYNC_ATTEMPTS + 1):
            try:
                response = await self._client.get(
                    f"{self.settings.central_url}/history",
                    params={"since": request_since},
                )
            except httpx.HTTPError as exc:
                # Error de red o timeout: se podrá reintentar.
                response = None
                last_reason = type(exc).__name__
            else:
                if 200 <= response.status_code < 300:
                    # Respuesta buena: salimos del bucle de reintentos.
                    break
                if 400 <= response.status_code < 500:
                    # Un 4xx es un error de protocolo: reintentar no lo arregla.
                    log_event(
                        "HISTORIAL_FALLO",
                        status=response.status_code,
                        motivo="4xx no se reintenta",
                    )
                    return False
                # Un 5xx es un fallo temporal: se reintenta.
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

        # --- 2. Leer el JSON de la respuesta. ---
        try:
            payload: Any = response.json()
        except ValueError:
            log_error("HISTORIAL_FALLO", motivo="respuesta no es JSON")
            return False

        # Aceptamos las dos formas más habituales del contrato para no romper
        # la interoperabilidad: una lista directa o {"messages": [...]}.
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

        # --- 3. Guardar cada mensaje nuevo (y descartar los ya conocidos). ---
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
                # Es un mensaje nuevo: aplicamos la regla de recepción de
                # Lamport, L = max(L, L_recibido) + 1. Solo lo hacemos para
                # mensajes nuevos; si lo hiciéramos también con duplicados,
                # cada resincronización inflaría el reloj sin motivo.
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
                # insert_message devuelve False cuando el UUID ya existía:
                # deduplicación. No hacemos nada más con ese mensaje.
                log_event(
                    "DESCARTE_DUPLICADO",
                    origen="historial",
                    id=message.id,
                    lamport=message.lamport,
                )

        # --- 4. Avanzar la marca de historial y terminar. ---
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
        """Envía los mensajes pendientes cuando el central está disponible.

        La cola vive en SQLite (tabla ``outbox``). Un mensaje sale de la cola
        SOLO cuando el central confirma con ``2xx``. Si el servidor se cae con
        mensajes pendientes, al reiniciar los encuentra y los reintenta con el
        mismo UUID.
        """
        while not self._stopped:
            if not self.online or self._client is None:
                # Central caído: los mensajes se quedan guardados y esperamos.
                await asyncio.sleep(0.5)
                continue

            try:
                # "due" = mensajes cuyo próximo intento ya venció.
                due = self.db.get_due_outbox(time.time(), limit=20)
            except Exception as exc:  # noqa: BLE001 - una tarea no debe morir
                log_error("ERROR", "no se pudo leer la cola", detalle=repr(exc))
                await asyncio.sleep(1.0)
                continue

            if not due:
                # Nada pendiente: esperamos un poco y volvemos a mirar.
                await asyncio.sleep(0.5)
                continue

            for item in due:
                if self._stopped or not self.online:
                    break
                await self._send_outbox_item(item)

            await asyncio.sleep(0.1)

    async def _send_outbox_item(self, item: OutboxItem) -> None:
        """Envía un mensaje al central y decide qué hacer según la respuesta.

        El comportamiento ante cada respuesta es la clave de la entrega "al
        menos una vez":

        - ``2xx``: el central lo aceptó -> se elimina de la cola.
        - ``4xx``: error de protocolo nuestro -> no se reintenta, se descarta.
        - ``5xx`` o error de red: fallo temporal -> se reprograma un reintento.
        """
        if self._client is None:
            return

        message = item.message
        attempt = item.attempts + 1  # este envío es el intento número attempt

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
            # Sin respuesta o timeout: el mensaje sigue en la cola y se
            # reintentará más tarde con el MISMO id.
            self._schedule_retry(message, item.attempts, type(exc).__name__)
            return

        if 200 <= response.status_code < 300:
            # Confirmado. Solo ahora se quita de la cola: si el proceso muere
            # justo antes, al reiniciar se reenviará con el mismo id y el
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
            # El protocolo dice que un 4xx no se reintenta: reintentarlo no lo
            # va a arreglar. Se descarta y se deja rastro en el log.
            self.db.remove_outbox(message.id)
            log_event(
                "DESCARTE_4XX",
                id=message.id,
                lamport=message.lamport,
                status=response.status_code,
                detalle=response.text[:160].replace("\n", " "),
            )
            return

        # 5xx: el central falló temporalmente. Se reprograma con backoff.
        self._schedule_retry(message, item.attempts, f"HTTP {response.status_code}")

    def _schedule_retry(
        self, message: Message, previous_attempts: int, reason: str
    ) -> None:
        """Reprograma un mensaje para reintentarlo más tarde.

        Aumenta el contador de intentos, calcula la espera con backoff
        exponencial + jitter y guarda en SQLite a qué hora toca el siguiente
        intento. Todo queda persistido: sobrevive a un reinicio.
        """
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
