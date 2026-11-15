"""Pruebas de la configuración (módulo de Emerson).

Estas pruebas comprueban que el servidor falla al arrancar cuando una
variable de entorno está mal, en lugar de fallar a mitad de la demo.
"""

import os
import unittest
from unittest.mock import patch

from app.config import cargar_configuracion


def cargar_con(variables):
    """Ejecuta ``cargar_configuracion`` con un entorno controlado.

    ``clear=True`` borra las variables del proceso para que una prueba no
    herede la configuración de otra.
    """
    with patch.dict(os.environ, variables, clear=True):
        return cargar_configuracion()


class PruebasConfiguracion(unittest.TestCase):
    def test_valores_por_defecto(self):
        configuracion = cargar_con({})

        self.assertEqual(configuracion.nombre_grupo, "g3")
        self.assertEqual(configuracion.puerto, 8000)
        self.assertEqual(configuracion.url_publica, "http://localhost:8000")
        self.assertEqual(configuracion.url_central, "http://localhost:9000")
        self.assertEqual(configuracion.fallos_latido_para_caida, 3)
        self.assertEqual(configuracion.segundos_base_reintento, 1.0)
        self.assertEqual(configuracion.segundos_maximos_reintento, 30.0)

    def test_url_publica_sin_esquema_falla(self):
        with self.assertRaises(ValueError):
            cargar_con({"PUBLIC_URL": "192.168.1.50:8001"})

    def test_url_central_sin_esquema_falla(self):
        with self.assertRaises(ValueError):
            cargar_con({"CENTRAL_URL": "localhost:9000"})

    def test_quita_la_barra_final(self):
        configuracion = cargar_con(
            {
                "PUBLIC_URL": "http://localhost:8001/",
                "CENTRAL_URL": "http://localhost:9000/",
            }
        )

        self.assertEqual(configuracion.url_publica, "http://localhost:8001")
        self.assertEqual(configuracion.url_central, "http://localhost:9000")

    def test_nombre_de_grupo_invalido_falla(self):
        with self.assertRaises(ValueError):
            cargar_con({"GROUP_NAME": "grupo tres"})

    def test_tope_de_reintento_menor_que_la_base_falla(self):
        with self.assertRaises(ValueError):
            cargar_con(
                {
                    "RETRY_BASE_SECONDS": "10",
                    "RETRY_MAX_SECONDS": "5",
                }
            )

    def test_numero_invalido_falla(self):
        with self.assertRaises(ValueError):
            cargar_con({"HEARTBEAT_INTERVAL_SECONDS": "cinco"})

    def test_intervalo_negativo_falla(self):
        with self.assertRaises(ValueError):
            cargar_con({"REGISTER_INTERVAL_SECONDS": "-1"})

    def test_variable_vacia_usa_el_valor_por_defecto(self):
        configuracion = cargar_con({"HEARTBEAT_FAILURES": "  "})

        self.assertEqual(configuracion.fallos_latido_para_caida, 3)


if __name__ == "__main__":
    unittest.main()
