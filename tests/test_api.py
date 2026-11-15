import os
import tempfile
import unittest
import uuid

# La configuración se define ANTES de importar la aplicación.
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test_api.db")
os.environ["GROUP_NAME"] = "g3"
os.environ["CENTRAL_URL"] = "http://127.0.0.1:9"  # puerto que no existe
os.environ["PUBLIC_URL"] = "http://localhost:8000"
os.environ["REGISTER_INTERVAL_SECONDS"] = "3600"
os.environ["HEARTBEAT_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

cliente = TestClient(app)


def mensaje_federado(**cambios):
    base = {
        "id": str(uuid.uuid4()),
        "from": "ana@g1",
        "origin": "g1",
        "lamport": 1,
        "sent_at": "2026-01-01T00:00:00Z",
        "body": "hola",
    }
    base.update(cambios)
    return base


def usuario_nuevo():
    # Nombre único para que las pruebas no choquen entre sí.
    nombre = "u" + uuid.uuid4().hex[:8]
    r = cliente.post("/users", json={"username": nombre})
    assert r.status_code == 201
    return nombre


class PruebasAPI(unittest.TestCase):
    # ---------- salud ----------
    def test_health(self):
        r = cliente.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["server"], "g3")

    def test_fed_health(self):
        r = cliente.get("/fed/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["server"], "g3")
        self.assertIn("lamport", r.json())

    # ---------- usuarios ----------
    def test_crear_usuario_201_y_duplicado_409(self):
        nombre = "u" + uuid.uuid4().hex[:8]
        primero = cliente.post("/users", json={"username": nombre})
        segundo = cliente.post("/users", json={"username": nombre})
        self.assertEqual(primero.status_code, 201)
        self.assertEqual(primero.json()["identity"], f"{nombre}@g3")
        self.assertEqual(segundo.status_code, 409)

    # ---------- mensajes locales ----------
    def test_enviar_mensaje_201_y_aparece_en_historial(self):
        nombre = usuario_nuevo()
        r = cliente.post("/messages", json={"user": nombre, "body": "hola a todos"})
        self.assertEqual(r.status_code, 201)
        msg = r.json()
        self.assertEqual(msg["from"], f"{nombre}@g3")
        self.assertEqual(msg["origin"], "g3")

        historial = cliente.get("/messages?since=0&limit=500").json()["messages"]
        self.assertIn(msg["id"], [m["id"] for m in historial])

    # ---------- API federada ----------
    def test_fed_messages_deduplica(self):
        msg = mensaje_federado()
        primera = cliente.post("/fed/messages", json=msg)
        segunda = cliente.post("/fed/messages", json=msg)
        self.assertEqual(primera.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertTrue(segunda.json()["duplicate"])

    def test_duplicado_no_cambia_el_reloj(self):
        msg = mensaje_federado(lamport=500)
        cliente.post("/fed/messages", json=msg)
        antes = cliente.get("/fed/health").json()["lamport"]
        cliente.post("/fed/messages", json=msg)  # reintento
        despues = cliente.get("/fed/health").json()["lamport"]
        self.assertEqual(antes, despues)

    def test_campo_desconocido_se_ignora(self):
        msg = mensaje_federado(campo_raro="x")
        r = cliente.post("/fed/messages", json=msg)
        self.assertEqual(r.status_code, 200)

    def test_id_no_uuid_responde_422(self):
        msg = mensaje_federado(id="esto-no-es-un-uuid")
        r = cliente.post("/fed/messages", json=msg)
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
