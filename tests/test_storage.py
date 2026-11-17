"""Pruebas de persistencia, deduplicación, reloj y cola de salida.

Cubren la parte de Nataly: el reloj de Lamport y la base SQLite. La idea es
comprobar las propiedades que sostienen la tolerancia a fallos:

1. Un UUID repetido no crea una segunda fila.
2. El reloj avanza y sigue vivo después de cerrar y abrir la base.
3. ``observar`` aplica ``max(local, recibido) + 1``.
4. El historial se ordena por ``(lamport, origin, id)``.
5. La cola de salida se puede encolar, reprogramar y quitar.
6. Usuarios, historial y reloj sobreviven a un reinicio.
"""

import os
import tempfile
import unittest
import uuid

from app.database import BaseDatos
from app.lamport import RelojLamport
from app.models import Mensaje


def crear_mensaje(
    lamport: int = 1,
    origin: str = "g3",
    identificador: str | None = None,
    cuerpo: str = "hola",
) -> Mensaje:
    """Construye un mensaje válido con un UUID único por defecto."""
    return Mensaje.model_validate(
        {
            "id": identificador or str(uuid.uuid4()),
            "from": f"ana@{origin}",
            "origin": origin,
            "lamport": lamport,
            "sent_at": "2026-01-01T00:00:00Z",
            "body": cuerpo,
        }
    )


class PruebasAlmacenamiento(unittest.TestCase):
    def setUp(self) -> None:
        # Cada prueba usa su propio archivo para no compartir estado.
        descriptor, self.ruta = tempfile.mkstemp(suffix=".db")
        os.close(descriptor)
        self.base = BaseDatos(self.ruta)
        self.base.crear_esquema()

    def tearDown(self) -> None:
        self.base.cerrar()
        if os.path.exists(self.ruta):
            os.unlink(self.ruta)

    # ------------------------------------------------------------------
    # Deduplicación
    # ------------------------------------------------------------------

    def test_un_uuid_repetido_no_crea_duplicados(self):
        mensaje = crear_mensaje()

        self.assertTrue(self.base.insertar_mensaje(mensaje))
        self.assertFalse(self.base.insertar_mensaje(mensaje))
        self.assertEqual(self.base.contar_mensajes(), 1)

    def test_dos_mensajes_distintos_si_se_guardan(self):
        self.assertTrue(self.base.insertar_mensaje(crear_mensaje()))
        self.assertTrue(self.base.insertar_mensaje(crear_mensaje()))
        self.assertEqual(self.base.contar_mensajes(), 2)

    # ------------------------------------------------------------------
    # Reloj de Lamport
    # ------------------------------------------------------------------

    def test_el_reloj_avanza_y_se_observa(self):
        reloj = RelojLamport(self.base)

        self.assertEqual(reloj.avanzar(), 1)
        self.assertEqual(reloj.avanzar(), 2)
        self.assertEqual(reloj.observar(10), 11)
        self.assertEqual(reloj.observar(3), 12)
        self.assertEqual(reloj.valor, 12)

    def test_el_reloj_sobrevive_al_cierre_de_la_base(self):
        reloj = RelojLamport(self.base)
        reloj.avanzar()
        reloj.observar(40)
        self.base.cerrar()

        # Se abre otra instancia sobre el mismo archivo.
        self.base = BaseDatos(self.ruta)
        self.base.crear_esquema()
        reloj_nuevo = RelojLamport(self.base)

        self.assertEqual(reloj_nuevo.valor, 41)
        self.assertEqual(reloj_nuevo.avanzar(), 42)

    # ------------------------------------------------------------------
    # Orden del historial
    # ------------------------------------------------------------------

    def test_el_historial_se_ordena_por_lamport_origen_e_id(self):
        # El orden esperado es: (1, g3, a), (2, g1, b), (2, g3, c).
        self.base.insertar_mensaje(
            crear_mensaje(
                lamport=2,
                origin="g3",
                identificador="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            )
        )
        self.base.insertar_mensaje(
            crear_mensaje(
                lamport=1,
                origin="g3",
                identificador="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            )
        )
        self.base.insertar_mensaje(
            crear_mensaje(
                lamport=2,
                origin="g1",
                identificador="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            )
        )

        mensajes = self.base.listar_mensajes()
        claves = [(m.lamport, m.origin, m.id) for m in mensajes]

        self.assertEqual(
            claves,
            [
                (1, "g3", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                (2, "g1", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                (2, "g3", "cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            ],
        )

    # ------------------------------------------------------------------
    # Cola de salida
    # ------------------------------------------------------------------

    def test_la_cola_se_encola_reprograma_y_quita(self):
        mensaje = crear_mensaje()
        self.base.insertar_mensaje(mensaje)
        self.base.encolar_salida(mensaje.id)

        self.assertEqual(self.base.contar_pendientes(), 1)
        pendientes = self.base.obtener_cola_pendiente(ahora=0)
        self.assertEqual(len(pendientes), 1)
        self.assertEqual(pendientes[0].mensaje.id, mensaje.id)
        self.assertEqual(pendientes[0].intentos, 0)

        self.base.programar_reintento_cola(
            mensaje.id,
            intentos=3,
            proximo_intento_en=100,
        )

        self.assertEqual(self.base.obtener_cola_pendiente(ahora=0), [])
        pendientes = self.base.obtener_cola_pendiente(ahora=101)
        self.assertEqual(len(pendientes), 1)
        self.assertEqual(pendientes[0].intentos, 3)

        self.base.quitar_de_cola(mensaje.id)
        self.assertEqual(self.base.contar_pendientes(), 0)

    # ------------------------------------------------------------------
    # Reinicio completo
    # ------------------------------------------------------------------

    def test_usuarios_historial_y_reloj_sobreviven_al_reinicio(self):
        self.base.agregar_usuario("ana")
        mensaje = crear_mensaje()
        self.base.insertar_mensaje(mensaje)
        self.base.encolar_salida(mensaje.id)
        reloj = RelojLamport(self.base)
        reloj.avanzar()
        self.base.cerrar()

        self.base = BaseDatos(self.ruta)
        self.base.crear_esquema()

        self.assertEqual(self.base.listar_usuarios(), ["ana"])
        self.assertEqual(self.base.contar_mensajes(), 1)
        self.assertEqual(self.base.obtener_mensaje(mensaje.id).id, mensaje.id)
        self.assertEqual(self.base.contar_pendientes(), 1)
        self.assertEqual(RelojLamport(self.base).valor, 1)


if __name__ == "__main__":
    unittest.main()
