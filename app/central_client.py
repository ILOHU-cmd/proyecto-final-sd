"""Cliente hacia el servidor central.

Este módulo mantiene el enlace entre nuestro servidor de grupo y el servidor
central. Mientras la aplicación está encendida, tres tareas de fondo trabajan
al mismo tiempo:

1. **Registro**: avisa al central cada ciertos segundos dónde estamos
   (``POST /register``). Se repite porque el central puede reiniciarse y
   olvidar la lista de servidores.

2. **Latido**: pregunta al central si está vivo (``GET /health``). Si falla
   varias veces seguidas, se marca como caído y se deja de enviar. Cuando
   vuelve, se recupera el historial que nos perdimos.

3. **Cola de salida**: saca de SQLite los mensajes que todavía no fueron
   aceptados y los envía uno por uno (``POST /messages``).

Conceptos de sistemas distribuidos que aplica este módulo:

- **Entrega al menos una vez**: un mensaje se reintenta hasta que el central
  responde 2xx. Si se pierde la respuesta, se reenvía con el mismo ``id`` y el
  central lo deduplica; por eso reintentar de más no crea duplicados.
- **Espera exponencial con variación aleatoria**: entre un reintento y otro se
  espera cada vez más (1s, 2s, 4s...) con algo de azar, para que varios grupos
  no golpeen al central al mismo tiempo cuando vuelve.
- **Latido consultado (pull)**: somos nosotros quienes preguntamos si el
  central está vivo, en lugar de esperar a que él nos avise.
- **Sincronización de historial**: al arrancar o al volver de una caída se pide
  ``GET /history?since=...`` para recuperar lo que nos perdimos.

Se usa ``httpx.AsyncClient`` para que esperar una respuesta del central no
bloquee el resto del servidor.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import Configuracion
from app.database import Database, OutboxItem
from app.lamport import LamportClock
from app.logger import log_error, log_event
from app.models import Message, RegisterRequest


def calcular_espera_reintento(
    intento_actual: int, segundos_base: float, segundos_maximos: float
) -> float:
    """Devuelve cuántos segundos esperar antes del próximo reintento.

    Recibe:

    - ``intento_actual``: número del intento que acaba de fallar (empieza en 1).
    - ``segundos_base``: espera del primer reintento (por defecto 1 segundo).
    - ``segundos_maximos``: tope máximo permitido (por defecto 30 segundos).

    La espera crece con cada intento para no saturar al central con reintentos
    inmediatos. Además se le suma una variación aleatoria de ±50 % para que
    varios grupos no reintenten exactamente al mismo tiempo.

    Ejemplo con base 1 y máximo 30:

    - intento 1 -> alrededor de 1 segundo,
    - intento 2 -> alrededor de 2 segundos,
    - intento 3 -> alrededor de 4 segundos,
    - ... hasta llegar al tope de 30 segundos.
    """
    # 2 ** (intento - 1) duplica el valor en cada intento: 1, 2, 4, 8...
    # max(0, ...) evita un exponente negativo si llegara un intento inválido.
    # min(..., segundos_maximos) impone el tope desde el principio.
    espera_exponencial = min(
        segundos_maximos,
        segundos_base * (2 ** max(0, intento_actual - 1)),
    )

    # Variación aleatoria ("jitter"): en vez de esperar exactamente el valor
    # calculado, se espera algo entre la mitad y el doble y medio.
    espera_con_azar = random.uniform(
        espera_exponencial * 0.5,
        espera_exponencial * 1.5,
    )

    # El azar tampoco debe superar el tope configurado.
    return min(segundos_maximos, espera_con_azar)


class ClienteCentral:
    """Mantiene el enlace con el servidor central mediante tareas de fondo."""

    # Número máximo de intentos para una sincronización puntual del historial.
    _INTENTOS_SINCRONIZACION = 3

    def __init__(
        self,
        configuracion: Configuracion,
        base_datos: Database,
        reloj: LamportClock,
    ) -> None:
        self.configuracion = configuracion
        self.base_datos = base_datos
        self.reloj = reloj

        # Indica si el central responde. Lo lee el endpoint /status y lo
        # escriben las tareas de fondo. Mientras sea False, la cola de salida
        # no intenta enviar nada.
        self.en_linea = False

        # Cliente HTTP compartido por las tres tareas. Se crea en ``ejecutar``.
        self._cliente_http: httpx.AsyncClient | None = None

        # Bandera para pedir el apagado ordenado de las tareas.
        self._detenido = False

        # Evita que dos sincronizaciones del historial corran a la vez.
        self._candado_sincronizacion = asyncio.Lock()

        # La primera sincronización pide el historial completo (desde 0).
        # Las recuperaciones posteriores, sin reiniciar el proceso, piden solo
        # lo nuevo a partir de la última marca guardada.
        self._sincronizacion_inicial = True

    @property
    def esta_en_linea(self) -> bool:
        """True si el central está disponible. Lo consulta el endpoint /status."""
        return self.en_linea

    def solicitar_apagado(self) -> None:
        """Pide a las tareas que terminen cuando se apague el servidor."""
        self._detenido = True

    # ------------------------------------------------------------------
    # Arranque
    # ------------------------------------------------------------------

    async def ejecutar(self) -> None:
        """Crea el cliente HTTP y lanza las tres tareas de fondo.

        Las tres tareas avanzan en paralelo: mientras una espera la respuesta
        del central, las otras pueden seguir trabajando.
        """
        # Tiempo máximo que se espera cualquier petición al central.
        tiempo_espera = httpx.Timeout(self.configuracion.segundos_espera_peticion)

        # "async with" abre el cliente HTTP y lo cierra al terminar.
        async with httpx.AsyncClient(timeout=tiempo_espera) as cliente:
            self._cliente_http = cliente

            # Cada tarea se guarda en una lista para poder cancelarlas todas
            # cuando el servidor se apague. Sin esto, una tarea podría quedar
            # corriendo sola en segundo plano.
            tareas = [
                asyncio.create_task(self._bucle_registro(), name="registro-central"),
                asyncio.create_task(self._bucle_latido(), name="latido-central"),
                asyncio.create_task(self._bucle_cola_salida(), name="cola-central"),
            ]

            try:
                # Espera a que las tres terminen. Solo terminan cuando se pide
                # el apagado o cuando ocurre un error inesperado.
                await asyncio.gather(*tareas)
            finally:
                # Apagado ordenado: se cancelan y se espera a que suelten.
                for tarea in tareas:
                    tarea.cancel()
                await asyncio.gather(*tareas, return_exceptions=True)
                self._cliente_http = None

    # ------------------------------------------------------------------
    # Registro periódico
    # ------------------------------------------------------------------

    async def _bucle_registro(self) -> None:
        """Registra el servidor en el central cada cierto tiempo.

        El registro es idempotente: repetirlo con el mismo nombre solo
        actualiza la dirección, no crea entradas repetidas. Se repite porque
        el central puede reiniciarse y perder su lista de servidores.
        """
        fallos = 0

        # El bucle se repite mientras no se haya pedido el apagado.
        while not self._detenido:
            exito = await self._registrar_una_vez()
            intervalo = self.configuracion.segundos_entre_registros

            if exito:
                # Todo bien: se espera el intervalo normal, con un pequeño
                # azar para no coincidir exactamente con otros grupos.
                fallos = 0
                espera = intervalo + random.uniform(0.0, intervalo * 0.1)
            else:
                # Falló: se espera un tiempo que crece con cada fallo, pero
                # nunca más que el intervalo normal de registro.
                fallos += 1
                espera = min(
                    intervalo,
                    calcular_espera_reintento(
                        fallos,
                        self.configuracion.segundos_base_reintento,
                        self.configuracion.segundos_maximos_reintento,
                    ),
                )

                log_event(
                    "REINTENTO",
                    operacion="POST /register",
                    intento_fallido=fallos,
                    espera=round(espera, 3),
                )

            # asyncio.sleep no bloquea el programa: mientras esta tarea
            # duerme, las otras dos siguen funcionando.
            await asyncio.sleep(espera)

    async def _registrar_una_vez(self) -> bool:
        """Hace un único ``POST /register`` y dice si el central lo aceptó.

        El cuerpo es exactamente el que pide el enunciado:
        ``{"server": "g3", "url": "http://..."}``.
        """
        if self._cliente_http is None:
            return False

        cuerpo = RegisterRequest(
            server=self.configuracion.nombre_grupo,
            url=self.configuracion.url_publica,
        ).model_dump()

        try:
            respuesta = await self._cliente_http.post(
                f"{self.configuracion.url_central}/register",
                json=cuerpo,
            )
        except httpx.HTTPError as error:
            # No hubo respuesta: red caída, tiempo agotado, etc.
            log_event(
                "REGISTRO_FALLO",
                motivo=type(error).__name__,
                central=self.configuracion.url_central,
            )
            return False

        # Cualquier código entre 200 y 299 significa que fue aceptado.
        if 200 <= respuesta.status_code < 300:
            log_event(
                "REGISTRO_OK",
                servidor=self.configuracion.nombre_grupo,
                url=self.configuracion.url_publica,
            )
            return True

        log_event("REGISTRO_FALLO", status=respuesta.status_code)
        return False

    # ------------------------------------------------------------------
    # Detección de fallos (latido consultado)
    # ------------------------------------------------------------------

    async def _bucle_latido(self) -> None:
        """Pregunta periódicamente si el central está vivo.

        Regla:

        - Si responde -> central arriba. Si estábamos caídos, se recupera.
        - Si no responde -> se suma un fallo. Al llegar a
          ``fallos_latido_para_caida`` fallos seguidos, se marca caído.
        """
        fallos = 0

        while not self._detenido:
            saludable = await self._consultar_salud()

            if saludable:
                fallos = 0

                if not self.en_linea:
                    # Primer contacto o regreso de una caída: antes de volver
                    # a enviar, se recupera lo que nos perdimos.
                    await self._recuperar()
            else:
                fallos += 1

                if (
                    self.en_linea
                    and fallos >= self.configuracion.fallos_latido_para_caida
                ):
                    # Varios fallos seguidos: se declara caído. La cola de
                    # salida dejará de intentar enviar.
                    self.en_linea = False
                    log_event(
                        "CAÍDA_CENTRAL",
                        intentos_fallidos=fallos,
                        mensajes_pendientes=self.base_datos.pending_outbox_count(),
                    )
                elif (
                    not self.en_linea
                    and fallos == self.configuracion.fallos_latido_para_caida
                ):
                    # Ya estaba caído: se registra una sola vez para no llenar
                    # el log con el mismo aviso cada pocos segundos.
                    log_event(
                        "CENTRAL_NO_DISPONIBLE",
                        intentos_fallidos=fallos,
                        nota="se sigue consultando el latido",
                    )

            await asyncio.sleep(self.configuracion.segundos_entre_latidos)

    async def _consultar_salud(self) -> bool:
        """Hace un único ``GET /health`` y devuelve True si respondió bien.

        Esta petición usa un tiempo de espera más corto que las demás, para
        detectar rápido un central colgado.
        """
        if self._cliente_http is None:
            return False

        try:
            respuesta = await self._cliente_http.get(
                f"{self.configuracion.url_central}/health",
                timeout=self.configuracion.segundos_espera_latido,
            )
        except httpx.HTTPError:
            # Si la petición falla, el central no está disponible.
            return False

        return 200 <= respuesta.status_code < 300

    # ------------------------------------------------------------------
    # Recuperación de historial
    # ------------------------------------------------------------------

    async def _recuperar(self) -> None:
        """Renueva el registro y sincroniza el historial antes de volver a enviar.

        El orden es importante:

        1. Renovar el registro (el central pudo reiniciarse y olvidarnos).
        2. Pedir el historial que nos perdimos.
        3. Solo entonces marcar ``en_linea = True``.

        Si la sincronización falla, se sigue "caído" y se intenta de nuevo en
        el siguiente latido. Es mejor no enviar a ciegas y arriesgar un
        historial incompleto.
        """
        # Si ya hay una sincronización en curso, no se lanza otra.
        if self._candado_sincronizacion.locked():
            return

        async with self._candado_sincronizacion:
            if self._detenido:
                return

            log_event("RECUPERACIÓN_INICIO", central=self.configuracion.url_central)

            # 1. Renovar el registro es barato e idempotente.
            await self._registrar_una_vez()

            # 2. Traer el historial perdido.
            if not await self._sincronizar_historial():
                log_event(
                    "RECUPERACIÓN_FALLO",
                    nota="se reintentará en el próximo latido",
                )
                return

            # 3. Recién ahora se permite enviar otra vez.
            self.en_linea = True
            log_event(
                "RECUPERACIÓN_OK",
                lamport=self.reloj.value,
                pendientes=self.base_datos.pending_outbox_count(),
            )

    async def _sincronizar_historial(self) -> bool:
        """Pide al central los mensajes posteriores a nuestra última marca.

        En SQLite se guarda la clave ``last_history_lamport``: la mayor marca
        de Lamport que hemos visto venir del central.

        Al pedir el historial se usa ``desde = max(0, marca - 1)``. Se resta 1
        a propósito para incluir los mensajes que comparten la misma marca,
        porque el orden es ``(lamport, origin)`` y dos grupos pueden coincidir
        en el número. Pedir de más no hace daño: el receptor deduplica por UUID.

        La primera vez (al arrancar) se pide ``desde=0``: el historial completo,
        para no dejar huecos.
        """
        if self._cliente_http is None:
            return False

        # Última marca de historial que quedó guardada.
        marca_guardada = int(
            self.base_datos.get_state("last_history_lamport", "0") or 0
        )

        # En el arranque se pide todo; en una reconexión, solo lo nuevo.
        if self._sincronizacion_inicial:
            desde = 0
            modo = "completa"
        else:
            desde = max(0, marca_guardada - 1)
            modo = "incremental"

        log_event(
            "HISTORIAL_INICIO",
            modo=modo,
            desde=desde,
            ultima_marca=marca_guardada,
        )

        # --- 1. Pedir el historial, con reintentos. ---
        respuesta: httpx.Response | None = None
        ultimo_motivo = "sin respuesta"

        for intento in range(1, self._INTENTOS_SINCRONIZACION + 1):
            try:
                respuesta = await self._cliente_http.get(
                    f"{self.configuracion.url_central}/history",
                    params={"since": desde},
                )
            except httpx.HTTPError as error:
                # Error de red o tiempo agotado: se puede reintentar.
                respuesta = None
                ultimo_motivo = type(error).__name__
            else:
                if 200 <= respuesta.status_code < 300:
                    # Respuesta correcta: se sale del bucle de reintentos.
                    break

                if 400 <= respuesta.status_code < 500:
                    # Un 4xx indica un error en la petición. Reintentarla no
                    # lo va a arreglar.
                    log_event(
                        "HISTORIAL_FALLO",
                        status=respuesta.status_code,
                        motivo="4xx no se reintenta",
                    )
                    return False

                # Un 5xx es un fallo temporal del central: se reintenta.
                ultimo_motivo = f"HTTP {respuesta.status_code}"
                respuesta = None

            # Si todavía quedan intentos, se espera antes del siguiente.
            if intento < self._INTENTOS_SINCRONIZACION:
                espera = calcular_espera_reintento(
                    intento,
                    self.configuracion.segundos_base_reintento,
                    self.configuracion.segundos_maximos_reintento,
                )
                log_event(
                    "REINTENTO",
                    operacion="GET /history",
                    intento_fallido=intento,
                    proximo_intento=intento + 1,
                    espera=round(espera, 3),
                    motivo=ultimo_motivo,
                )
                await asyncio.sleep(espera)

        if respuesta is None:
            log_error("HISTORIAL_FALLO", motivo=ultimo_motivo)
            return False

        # --- 2. Leer el JSON de la respuesta. ---
        try:
            datos = respuesta.json()
        except ValueError:
            log_error("HISTORIAL_FALLO", motivo="la respuesta no es JSON")
            return False

        # El contrato admite una lista directa o un objeto con "messages".
        # Se aceptan las dos formas para no romper la interoperabilidad.
        if isinstance(datos, list):
            mensajes_recibidos: Any = datos
        elif isinstance(datos, dict):
            mensajes_recibidos = datos.get("messages", datos.get("history", []))
        else:
            log_error("HISTORIAL_FALLO", motivo="forma de respuesta no reconocida")
            return False

        if not isinstance(mensajes_recibidos, list):
            log_error("HISTORIAL_FALLO", motivo="el campo messages no es una lista")
            return False

        # --- 3. Guardar los mensajes nuevos y descartar los repetidos. ---
        marca_maxima = marca_guardada
        nuevos = 0

        for mensaje_crudo in mensajes_recibidos:
            if not isinstance(mensaje_crudo, dict):
                log_event(
                    "MENSAJE_INVALIDO",
                    origen="historial",
                    motivo="no es un objeto",
                )
                continue

            try:
                mensaje = Message.model_validate(mensaje_crudo)
            except ValidationError as error:
                log_event(
                    "MENSAJE_INVALIDO",
                    origen="historial",
                    motivo=error.errors()[0].get("msg", "error de validación"),
                )
                continue

            marca_maxima = max(marca_maxima, mensaje.lamport)

            if self.base_datos.insert_message(mensaje):
                # Mensaje nuevo: se aplica la regla de recepción de Lamport,
                # L = max(L, L_recibido) + 1. Solo se hace con mensajes nuevos;
                # si se hiciera con duplicados, cada resincronización inflaría
                # el reloj sin motivo.
                nuevo_valor_reloj = self.reloj.observe(mensaje.lamport)
                nuevos += 1
                log_event(
                    "RECEPCIÓN",
                    origen="historial",
                    id=mensaje.id,
                    lamport=mensaje.lamport,
                    lamport_local=nuevo_valor_reloj,
                )
            else:
                # insert_message devuelve False cuando el UUID ya existía.
                log_event(
                    "DESCARTE_DUPLICADO",
                    origen="historial",
                    id=mensaje.id,
                    lamport=mensaje.lamport,
                )

        # --- 4. Guardar la nueva marca y terminar. ---
        self.base_datos.advance_state_int("last_history_lamport", marca_maxima)
        self._sincronizacion_inicial = False
        log_event(
            "HISTORIAL_SINCRONIZADO",
            desde=desde,
            nuevos=nuevos,
            total_recibidos=len(mensajes_recibidos),
            lamport_historial=marca_maxima,
        )
        return True

    # ------------------------------------------------------------------
    # Cola de salida y reintentos
    # ------------------------------------------------------------------

    async def _bucle_cola_salida(self) -> None:
        """Envía los mensajes pendientes cuando el central está disponible.

        La cola vive en SQLite (tabla ``outbox``). Un mensaje sale de la cola
        SOLO cuando el central confirma con 2xx. Si el servidor se reinicia,
        los pendientes siguen ahí y se reintentan con el mismo UUID.
        """
        while not self._detenido:
            if not self.en_linea or self._cliente_http is None:
                # Central caído: los mensajes se quedan guardados y se espera.
                await asyncio.sleep(0.5)
                continue

            try:
                # Mensajes cuyo próximo intento ya venció.
                pendientes = self.base_datos.get_due_outbox(time.time(), limit=20)
            except Exception as error:  # noqa: BLE001
                # Una tarea de fondo no debe morir por un error de lectura.
                log_error("ERROR", "no se pudo leer la cola", detalle=repr(error))
                await asyncio.sleep(1.0)
                continue

            if not pendientes:
                # No hay nada para enviar: se revisa de nuevo en medio segundo.
                await asyncio.sleep(0.5)
                continue

            for elemento in pendientes:
                # Se comprueba otra vez por si el central se cayó a mitad.
                if self._detenido or not self.en_linea:
                    break
                await self._enviar_elemento_cola(elemento)

            await asyncio.sleep(0.1)

    async def _enviar_elemento_cola(self, elemento: OutboxItem) -> None:
        """Envía un mensaje al central y decide qué hacer según la respuesta.

        El comportamiento ante cada respuesta es la clave de la entrega
        "al menos una vez":

        - **2xx**: el central lo aceptó -> se elimina de la cola.
        - **4xx**: el mensaje tiene un error -> no se reintenta, se descarta.
        - **5xx o error de red**: fallo temporal -> se reprograma el reintento.
        """
        if self._cliente_http is None:
            return

        mensaje = elemento.message
        intento = elemento.attempts + 1

        log_event(
            "ENVÍO",
            destino="central",
            id=mensaje.id,
            lamport=mensaje.lamport,
            intento=intento,
        )

        try:
            respuesta = await self._cliente_http.post(
                f"{self.configuracion.url_central}/messages",
                json=mensaje.to_protocol_dict(),
            )
        except httpx.HTTPError as error:
            # Sin respuesta: el mensaje sigue en la cola y se reintentará más
            # tarde con el MISMO id.
            self._programar_reintento(
                mensaje,
                elemento.attempts,
                type(error).__name__,
            )
            return

        if 200 <= respuesta.status_code < 300:
            # Confirmado. Solo ahora se quita de la cola: si el proceso muere
            # justo antes, al reiniciar se reenvía con el mismo id y el central
            # lo deduplica.
            self.base_datos.remove_outbox(mensaje.id)
            log_event(
                "ENVÍO_OK",
                id=mensaje.id,
                lamport=mensaje.lamport,
                status=respuesta.status_code,
            )
            return

        if 400 <= respuesta.status_code < 500:
            # El protocolo dice que un 4xx no se reintenta: el error está en el
            # mensaje. Se descarta y se deja rastro en el log.
            self.base_datos.remove_outbox(mensaje.id)
            log_event(
                "DESCARTE_4XX",
                id=mensaje.id,
                lamport=mensaje.lamport,
                status=respuesta.status_code,
                detalle=respuesta.text[:160].replace("\n", " "),
            )
            return

        # 5xx: el central falló temporalmente. Se reprograma el reintento.
        self._programar_reintento(
            mensaje,
            elemento.attempts,
            f"HTTP {respuesta.status_code}",
        )

    def _programar_reintento(
        self,
        mensaje: Message,
        intentos_previos: int,
        motivo: str,
    ) -> None:
        """Deja un mensaje en la cola para intentarlo de nuevo más adelante.

        Aumenta el contador de intentos, calcula la espera con la fórmula
        exponencial con azar y guarda en SQLite a qué hora toca el próximo
        intento. Todo queda persistido: sobrevive a un reinicio.
        """
        intentos = intentos_previos + 1

        espera = calcular_espera_reintento(
            intentos,
            self.configuracion.segundos_base_reintento,
            self.configuracion.segundos_maximos_reintento,
        )
        proximo_intento_en = time.time() + espera

        self.base_datos.schedule_outbox_retry(
            mensaje.id,
            intentos,
            proximo_intento_en,
        )

        log_event(
            "REINTENTO",
            id=mensaje.id,
            lamport=mensaje.lamport,
            intento_fallido=intentos,
            proximo_intento=intentos + 1,
            espera=round(espera, 3),
            motivo=motivo,
        )
