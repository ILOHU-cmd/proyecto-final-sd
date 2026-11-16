"""Pruebas de la espera exponencial con azar (módulo de Emerson).

La función ``calcular_espera_reintento`` es la que decide cuánto esperar antes
de volver a intentar un envío al servidor central. Estas pruebas comprueban
las tres propiedades que exige el enunciado:

1. La espera crece con cada intento (1, 2, 4...).
2. Nunca supera el tope configurado.
3. Tiene azar, para que varios grupos no reintenten al mismo tiempo.
"""

import unittest

from app.central_client import calcular_espera_reintento


class PruebasEsperaReintento(unittest.TestCase):
    def test_crece_con_cada_intento(self):
        # Con base 1 y tope 30, el intento n espera alrededor de 1, 2, 4...
        # El azar de ±50 % se refleja en los intervalos de abajo.
        self.assertGreaterEqual(calcular_espera_reintento(1, 1.0, 30.0), 0.5)
        self.assertLessEqual(calcular_espera_reintento(1, 1.0, 30.0), 1.5)

        self.assertGreaterEqual(calcular_espera_reintento(2, 1.0, 30.0), 1.0)
        self.assertLessEqual(calcular_espera_reintento(2, 1.0, 30.0), 3.0)

        self.assertGreaterEqual(calcular_espera_reintento(3, 1.0, 30.0), 2.0)
        self.assertLessEqual(calcular_espera_reintento(3, 1.0, 30.0), 6.0)

    def test_respeta_los_parametros_recibidos(self):
        # Base 2 y tope 10: el intento 3 valdría 8, y el azar podría llegar a
        # 12; el tope lo recorta a 10.
        self.assertGreaterEqual(calcular_espera_reintento(1, 2.0, 10.0), 1.0)
        self.assertLessEqual(calcular_espera_reintento(1, 2.0, 10.0), 3.0)

        self.assertGreaterEqual(calcular_espera_reintento(3, 2.0, 10.0), 4.0)
        self.assertLessEqual(calcular_espera_reintento(3, 2.0, 10.0), 10.0)

    def test_nunca_supera_el_tope(self):
        # Sin el segundo min(), el azar de +50 % superaría el tope.
        for intento in range(1, 100):
            espera = calcular_espera_reintento(intento, 1.0, 30.0)
            self.assertLessEqual(espera, 30.0)
            self.assertGreater(espera, 0.0)

    def test_tiene_azar(self):
        valores = {calcular_espera_reintento(3, 1.0, 30.0) for _ in range(20)}
        self.assertGreater(len(valores), 1)

    def test_intento_invalido_no_rompe(self):
        # Un intento 0 o negativo no debería producir una espera negativa.
        for intento in (0, -1, -5):
            espera = calcular_espera_reintento(intento, 1.0, 30.0)
            self.assertGreaterEqual(espera, 0.5)
            self.assertLessEqual(espera, 1.5)


if __name__ == "__main__":
    unittest.main()
