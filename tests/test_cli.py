import unittest

from client.cli import formatear_mensaje


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


if __name__ == "__main__":
    unittest.main()
