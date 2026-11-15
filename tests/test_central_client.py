"""Pruebas del cliente del servidor central (módulo de Emerson).

Se prueba la lógica de resiliencia sin levantar un servidor real: se reemplaza
``httpx.AsyncClient`` por un doble de prueba que devuelve respuestas
controladas. Así se pueden verificar los casos difíciles (500, 400, timeout,
historial con duplicados) de forma rápida y determinista.

Cubre:

- Cola de salida: 2xx quita, 4xx descarta, 5xx y error de red reprograman.
- Registro: cuerpo exacto del contrato y manejo de fallos.
- Latido: éxito, error de red y tiempo de espera.
- Historial: sincronización completa e incremental, deduplicación, respuestas
  inválidas y la regla de Lamport al recibir.
- Recuperación: el orden registro -> historial -> ``en_linea``.
"""

import os
import tempfile
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx

from app.central_client import ClienteCentral
from app.config import Configuracion
from app.database import BaseDatos, ElementoColaSalida
from app.lamport import RelojLamport
from app.models import Mensaje


# ---------------------------------------------------------------------------
# Dobles de prueba
# ---------------------------------------------------------------------------


class RespuestaFalsa:
    """Imita lo mínimo de ``httpx.Response`` que usa el cliente."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("la respuesta no trae JSON")
        return self._payload


class ClienteHTTPFalso:
    """Registra las llamadas y devuelve respuestas preparadas en orden."""

    def __init__(self, respuestas_post=None, respuestas_get=None):
        self.respuestas_post = list(respuestas_post or [])
        self.respuestas_get = list(respuestas_get or [])
        self.llamadas = []

    async def post(self, url, json=None):
        self.llamadas.append(("POST", url, json))
        respuesta = self.respuestas_post.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    async def get(self, url, params=None, timeout=None):
        self.llamadas.append(("GET", url, params))
        respuesta = self.respuestas_get.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def configuracion_de_prueba() -> Configuracion:
    return Configuracion(
        nombre_grupo="g3",
        url_publica="http://localhost:8001",
        url_central="http://central.prueba",
        puerto=8001,
        ruta_base_datos=":memory:",
        segundos_entre_registros=10.0,
        segundos_entre_latidos=5.0,
        segundos_espera_latido=3.0,
        fallos_latido_para_caida=3,
        segundos_espera_peticion=5.0,
        segundos_base_reintento=1.0,
        segundos_maximos_reintento=30.0,
        nivel_registro="INFO",
    )


def crear_mensaje(
    lamport: int = 1,
    identificador: str | None = None,
    origin: str = "g3",
    from_: str = "ana@g3",
    body: str = "hola",
) -> Mensaje:
    return Mensaje.model_validate(
        {
            "id": identificador or str(uuid.uuid4()),
            "from": from_,
            "origin": origin,
            "lamport": lamport,
            "sent_at": "2026-01-01T00:00:00Z",
            "body": body,
        }
    )


# ---------------------------------------------------------------------------
# Base de las pruebas
# ---------------------------------------------------------------------------


class BasePruebaCliente(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.base = BaseDatos(os.path.join(self.directorio.name, "prueba.db"))
        self.base.crear_esquema()
        self.reloj = RelojLamport(self.base)
        self.cliente = ClienteCentral(configuracion_de_prueba(), self.base, self.reloj)
        self.cliente._cliente_http = ClienteHTTPFalso()

    def tearDown(self):
        self.base.cerrar()
        self.directorio.cleanup()

    def encolar(self, mensaje: Mensaje, intentos: int = 0) -> ElementoColaSalida:
        self.base.insertar_mensaje(mensaje)
        self.base.encolar_salida(mensaje.id)
        return ElementoColaSalida(mensaje=mensaje, intentos=intentos)


# ---------------------------------------------------------------------------
# Cola de salida
# ---------------------------------------------------------------------------


class PruebasColaSalida(BasePruebaCliente):
    async def test_2xx_quita_el_mensaje_de_la_cola(self):
        mensaje = crear_mensaje()
        elemento = self.encolar(mensaje)
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_post=[RespuestaFalsa(201, {"status": "accepted"})]
        )

        await self.cliente._enviar_elemento_cola(elemento)

        self.assertEqual(self.base.contar_pendientes(), 0)
        metodo, url, cuerpo = self.cliente._cliente_http.llamadas[0]
        self.assertEqual(metodo, "POST")
        self.assertTrue(url.endswith("/messages"))
        self.assertEqual(cuerpo["id"], mensaje.id)

    async def test_4xx_descarta_y_no_reintenta(self):
        elemento = self.encolar(crear_mensaje())

        with self.assertLogs("chat", level="INFO") as registro:
            self.cliente._cliente_http = ClienteHTTPFalso(
                respuestas_post=[RespuestaFalsa(400, text="rechazado")]
            )
            await self.cliente._enviar_elemento_cola(elemento)

        self.assertEqual(self.base.contar_pendientes(), 0)
        self.assertEqual(len(self.cliente._cliente_http.llamadas), 1)
        self.assertTrue(any("[DESCARTE_4XX]" in linea for linea in registro.output))

    async def test_5xx_reprograma_con_intentos_y_espera(self):
        mensaje = crear_mensaje()
        elemento = self.encolar(mensaje)

        with self.assertLogs("chat", level="INFO") as registro:
            self.cliente._cliente_http = ClienteHTTPFalso(
                respuestas_post=[RespuestaFalsa(500)]
            )
            await self.cliente._enviar_elemento_cola(elemento)

        self.assertEqual(self.base.contar_pendientes(), 1)
        pendiente = self.base.obtener_cola_pendiente(time.time() + 3600)[0]
        self.assertEqual(pendiente.mensaje.id, mensaje.id)
        self.assertEqual(pendiente.intentos, 1)
        self.assertTrue(any("[REINTENTO]" in linea for linea in registro.output))

    async def test_error_de_red_reprograma(self):
        elemento = self.encolar(crear_mensaje())
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_post=[httpx.ConnectError("sin red")]
        )

        await self.cliente._enviar_elemento_cola(elemento)

        self.assertEqual(self.base.contar_pendientes(), 1)
        self.assertEqual(
            self.base.obtener_cola_pendiente(time.time() + 3600)[0].intentos, 1
        )

    async def test_el_reintento_conserva_el_mismo_id(self):
        mensaje = crear_mensaje(identificador=str(uuid.uuid4()))
        elemento = self.encolar(mensaje)
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_post=[RespuestaFalsa(503), RespuestaFalsa(200)]
        )

        await self.cliente._enviar_elemento_cola(elemento)

        # Se reprograma y se vuelve a enviar el mismo mensaje.
        pendiente = self.base.obtener_cola_pendiente(time.time() + 3600)[0]
        await self.cliente._enviar_elemento_cola(pendiente)

        _, _, primero = self.cliente._cliente_http.llamadas[0]
        _, _, segundo = self.cliente._cliente_http.llamadas[1]
        self.assertEqual(primero["id"], segundo["id"])
        self.assertEqual(segundo["id"], mensaje.id)

    async def test_el_cuerpo_usa_la_clave_from_del_protocolo(self):
        elemento = self.encolar(crear_mensaje())
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_post=[RespuestaFalsa(200)]
        )

        await self.cliente._enviar_elemento_cola(elemento)

        _, _, cuerpo = self.cliente._cliente_http.llamadas[0]
        self.assertIn("from", cuerpo)
        self.assertNotIn("from_", cuerpo)


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------


class PruebasRegistro(BasePruebaCliente):
    async def test_registro_envia_el_contrato_exacto(self):
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_post=[RespuestaFalsa(200, {"status": "registered"})]
        )

        resultado = await self.cliente._registrar_una_vez()

        self.assertTrue(resultado)
        metodo, url, cuerpo = self.cliente._cliente_http.llamadas[0]
        self.assertEqual(metodo, "POST")
        self.assertTrue(url.endswith("/register"))
        self.assertEqual(
            cuerpo, {"server": "g3", "url": "http://localhost:8001"}
        )

    async def test_registro_con_5xx_devuelve_falso(self):
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_post=[RespuestaFalsa(500)]
        )

        self.assertFalse(await self.cliente._registrar_una_vez())

    async def test_registro_sin_red_devuelve_falso(self):
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_post=[httpx.ConnectError("sin red")]
        )

        with self.assertLogs("chat", level="INFO"):
            self.assertFalse(await self.cliente._registrar_una_vez())


# ---------------------------------------------------------------------------
# Latido
# ---------------------------------------------------------------------------


class PruebasLatido(BasePruebaCliente):
    async def test_latido_sano_devuelve_verdadero(self):
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_get=[RespuestaFalsa(200, {"status": "ok"})]
        )

        self.assertTrue(await self.cliente._consultar_salud())
        _, url, _ = self.cliente._cliente_http.llamadas[0]
        self.assertTrue(url.endswith("/health"))

    async def test_latido_con_5xx_devuelve_falso(self):
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_get=[RespuestaFalsa(503)]
        )

        self.assertFalse(await self.cliente._consultar_salud())

    async def test_latido_sin_red_devuelve_falso(self):
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_get=[httpx.ConnectError("sin red")]
        )

        self.assertFalse(await self.cliente._consultar_salud())


# ---------------------------------------------------------------------------
# Historial
# ---------------------------------------------------------------------------


class PruebasHistorial(BasePruebaCliente):
    async def test_sincronizacion_completa_guarda_y_deduplica(self):
        m1 = crear_mensaje(lamport=5, origin="g1", from_="ana@g1")
        m2 = crear_mensaje(lamport=8, origin="g2", from_="beto@g2")
        cuerpo = [m1.model_dump(by_alias=True), m2.model_dump(by_alias=True)]

        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_get=[RespuestaFalsa(200, {"messages": cuerpo})]
        )
        self.assertTrue(await self.cliente._sincronizar_historial())

        _, _, parametros = self.cliente._cliente_http.llamadas[0]
        self.assertEqual(parametros["since"], 0)
        self.assertEqual(self.base.contar_mensajes(), 2)
        self.assertEqual(self.reloj.valor, 9)
        self.assertEqual(self.base.obtener_estado("last_history_lamport"), "8")

        # Segunda sincronización: los mismos ids no se guardan otra vez y el
        # reloj no se infla.
        reloj_antes = self.reloj.valor
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_get=[RespuestaFalsa(200, {"messages": cuerpo})]
        )
        self.assertTrue(await self.cliente._sincronizar_historial())

        _, _, parametros = self.cliente._cliente_http.llamadas[0]
        self.assertEqual(parametros["since"], 7)  # marca 8 - 1
        self.assertEqual(self.base.contar_mensajes(), 2)
        self.assertEqual(self.reloj.valor, reloj_antes)

    async def test_acepta_una_lista_directa(self):
        mensaje = crear_mensaje(lamport=3, origin="g1", from_="ana@g1")
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_get=[RespuestaFalsa(200, [mensaje.model_dump(by_alias=True)])]
        )

        self.assertTrue(await self.cliente._sincronizar_historial())
        self.assertEqual(self.base.contar_mensajes(), 1)

    async def test_ignora_mensajes_invalidos_sin_romperse(self):
        valido = crear_mensaje(lamport=4, origin="g1", from_="ana@g1")
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_get=[
                RespuestaFalsa(
                    200,
                    {
                        "messages": [
                            {"id": "no-es-uuid", "from": "x@g1"},
                            valido.model_dump(by_alias=True),
                        ]
                    },
                )
            ]
        )

        self.assertTrue(await self.cliente._sincronizar_historial())
        self.assertEqual(self.base.contar_mensajes(), 1)

    async def test_un_4xx_del_historial_no_se_reintenta(self):
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_get=[RespuestaFalsa(400)]
        )

        self.assertFalse(await self.cliente._sincronizar_historial())
        self.assertEqual(len(self.cliente._cliente_http.llamadas), 1)

    async def test_un_5xx_del_historial_se_reintenta_tres_veces(self):
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_get=[RespuestaFalsa(500), RespuestaFalsa(500), RespuestaFalsa(500)]
        )

        with patch("app.central_client.asyncio.sleep", new=AsyncMock()):
            self.assertFalse(await self.cliente._sincronizar_historial())

        self.assertEqual(len(self.cliente._cliente_http.llamadas), 3)

    async def test_recuperacion_marca_en_linea(self):
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_post=[RespuestaFalsa(200)],
            respuestas_get=[RespuestaFalsa(200, {"messages": []})],
        )

        with self.assertLogs("chat", level="INFO") as registro:
            await self.cliente._recuperar()

        self.assertTrue(self.cliente.en_linea)
        self.assertTrue(any("[RECUPERACIÓN_OK]" in linea for linea in registro.output))

    async def test_si_el_historial_falla_no_se_marca_en_linea(self):
        # Comportamiento documentado: no se envía hasta sincronizar bien.
        self.cliente._cliente_http = ClienteHTTPFalso(
            respuestas_post=[RespuestaFalsa(200)],
            respuestas_get=[RespuestaFalsa(500), RespuestaFalsa(500), RespuestaFalsa(500)],
        )

        with patch("app.central_client.asyncio.sleep", new=AsyncMock()):
            await self.cliente._recuperar()

        self.assertFalse(self.cliente.en_linea)


if __name__ == "__main__":
    unittest.main()
