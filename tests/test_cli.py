import unittest
from unittest.mock import patch

from client.cli import formatear_mensaje
from scripts.run_experiment import leer_argumentos, principal


class FormatearMensajeTests(unittest.TestCase):
    def test_mensaje_propio_incluye_remitente_cuerpo_y_lamport(self):
        mensaje = {
            "id": "11111111-1111-4111-8111-111111111111",
            "from": "ana@g3",
            "origin": "g3",
            "lamport": 5,
            "sent_at": "2026-01-01T00:00:00Z",
            "body": "hola",
        }

        texto = formatear_mensaje(mensaje, servidor_propio="g3", color=False)

        self.assertIn("ana@g3", texto)
        self.assertIn("hola", texto)
        self.assertIn("L=   5", texto)
        self.assertIn("[  g3]", texto)

    def test_mensaje_de_otro_grupo_se_distingue_por_su_origen(self):
        mensaje = {
            "id": "22222222-2222-4222-8222-222222222222",
            "from": "luis@g1",
            "origin": "g1",
            "lamport": 8,
            "sent_at": "2026-01-01T00:00:00Z",
            "body": "mensaje remoto",
        }

        propio = formatear_mensaje(mensaje, servidor_propio="g3", color=False)
        remoto = formatear_mensaje(
            {**mensaje, "origin": "g3"}, servidor_propio="g3", color=False
        )

        self.assertIn("[  g1]", propio)
        self.assertIn("[  g3]", remoto)
        self.assertNotEqual(propio, remoto)


class ExperimentoCliTests(unittest.TestCase):
    def test_argumentos_del_experimento_tienen_valores_reproducibles(self):
        argumentos = leer_argumentos(
            [
                "--sender-url",
                "http://localhost:8002",
                "--target-url",
                "http://localhost:8001",
                "--rate",
                "1",
                "--duration",
                "4",
                "--repetitions",
                "3",
                "--skip-docker",
            ]
        )

        self.assertEqual(argumentos.rate, 1.0)
        self.assertEqual(argumentos.duration, 4.0)
        self.assertEqual(argumentos.repetitions, 3)
        self.assertTrue(argumentos.skip_docker)

    def test_experimento_rechaza_mismo_servidor_emisor_y_objetivo(self):
        resultado = principal(
            [
                "--sender-url",
                "http://localhost:8001",
                "--target-url",
                "http://localhost:8001",
                "--skip-docker",
            ]
        )

        self.assertEqual(resultado, 2)

    def test_experimento_rechaza_parametros_no_positivos(self):
        with patch("sys.stderr"):
            resultado = principal(
                [
                    "--rate",
                    "0",
                    "--skip-docker",
                ]
            )

        self.assertEqual(resultado, 2)


if __name__ == "__main__":
    unittest.main()
