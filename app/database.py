"""Persistencia en SQLite.

Se eligió SQLite porque viene en la biblioteca estándar de Python, no requiere
otro contenedor y deja el proyecto fácil de explicar. El archivo vive en un
volumen de Docker, de modo que usuarios, historial, reloj de Lamport y cola de
salida sobreviven a ``docker compose restart``.

Decisiones importantes:

* Una sola conexión protegida por un ``threading.RLock``. FastAPI atiende
  peticiones en varios hilos, y SQLite no permite compartir una conexión sin
  control. Para el tamaño de este proyecto, un candado es más simple y
  suficiente que un pool de conexiones.
* ``INSERT OR IGNORE`` sobre la clave primaria ``messages.id`` implementa la
  deduplicación de forma atómica. Si el id ya existe, ``rowcount`` es 0 y el
  mensaje se descarta sin error: esa es la base de la idempotencia.
* La cola ``outbox`` es persistente. Si el servidor se cae con mensajes
  pendientes de enviar al central, al reiniciar los vuelve a encontrar y los
  reintenta con su mismo UUID.
* ``journal_mode=WAL`` permite leer mientras se escribe y reduce los bloqueos.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass

from app.models import Mensaje, ahora_utc_iso

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    sender     TEXT NOT NULL,
    origin     TEXT NOT NULL,
    lamport    INTEGER NOT NULL,
    sent_at    TEXT NOT NULL,
    body       TEXT NOT NULL
);

-- El historial se ordena por (lamport, origin). El id solo desempata de
-- forma determinista cuando dos mensajes coinciden en los dos primeros.
CREATE INDEX IF NOT EXISTS idx_messages_order
    ON messages (lamport, origin, id);

-- Cola de mensajes locales que todavía no han sido aceptados por el central.
CREATE TABLE IF NOT EXISTS outbox (
    message_id    TEXT PRIMARY KEY,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages (id) ON DELETE CASCADE
);

-- Estado pequeño de valor único: reloj de Lamport y última marca de historial.
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ElementoColaSalida:
    """Un mensaje pendiente y el número de intentos ya realizados."""

    mensaje: Mensaje
    intentos: int


class BaseDatos:
    """Acceso a datos con operaciones cortas y seguras entre hilos."""

    def __init__(self, ruta: str) -> None:
        self.ruta = ruta

        # En Docker la ruta es /data/chat.db. Si el directorio no existe, se
        # crea para que el contenedor arranque sin pasos manuales.
        if ruta != ":memory:":
            directorio = os.path.dirname(os.path.abspath(ruta))
            os.makedirs(directorio, exist_ok=True)

        self._candado = threading.RLock()
        self._conexion = sqlite3.connect(ruta, check_same_thread=False)
        self._conexion.row_factory = sqlite3.Row

        with self._candado:
            self._conexion.execute("PRAGMA journal_mode=WAL")
            self._conexion.execute("PRAGMA synchronous=NORMAL")
            self._conexion.execute("PRAGMA busy_timeout=5000")
            self._conexion.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------
    # Esquema
    # ------------------------------------------------------------------

    def crear_esquema(self) -> None:
        with self._candado:
            self._conexion.executescript(_ESQUEMA)
            self._conexion.commit()

    def cerrar(self) -> None:
        with self._candado:
            self._conexion.close()

    # ------------------------------------------------------------------
    # Usuarios
    # ------------------------------------------------------------------

    def agregar_usuario(self, nombre_usuario: str) -> bool:
        """Crea un usuario local. Devuelve False si ya existía."""
        with self._candado:
            cursor = self._conexion.execute(
                "INSERT OR IGNORE INTO users (username, created_at) VALUES (?, ?)",
                (nombre_usuario, ahora_utc_iso()),
            )
            self._conexion.commit()
            return cursor.rowcount == 1

    def existe_usuario(self, nombre_usuario: str) -> bool:
        with self._candado:
            fila = self._conexion.execute(
                "SELECT 1 FROM users WHERE username = ?", (nombre_usuario,)
            ).fetchone()
            return fila is not None

    def listar_usuarios(self) -> list[str]:
        with self._candado:
            filas = self._conexion.execute(
                "SELECT username FROM users ORDER BY username"
            ).fetchall()
            return [fila["username"] for fila in filas]

    def contar_usuarios(self) -> int:
        with self._candado:
            fila = self._conexion.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            return int(fila["n"]) if fila else 0

    # ------------------------------------------------------------------
    # Mensajes e idempotencia
    # ------------------------------------------------------------------

    def insertar_mensaje(self, mensaje: Mensaje) -> bool:
        """Guarda un mensaje si su UUID no existe.

        Devuelve True si se insertó y False si era un duplicado. La operación
        es atómica: dos entregas simultáneas del mismo id no pueden crear dos
        filas.
        """
        datos = mensaje.a_diccionario_protocolo()

        with self._candado:
            cursor = self._conexion.execute(
                """
                INSERT OR IGNORE INTO messages
                    (id, sender, origin, lamport, sent_at, body)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    datos["id"],
                    datos["from"],
                    datos["origin"],
                    datos["lamport"],
                    datos["sent_at"],
                    datos["body"],
                ),
            )
            self._conexion.commit()
            return cursor.rowcount == 1

    def obtener_mensaje(self, id_mensaje: str) -> Mensaje | None:
        with self._candado:
            fila = self._conexion.execute(
                """
                SELECT id, sender, origin, lamport, sent_at, body
                FROM messages WHERE id = ?
                """,
                (id_mensaje,),
            ).fetchone()

        return self._fila_a_mensaje(fila) if fila is not None else None

    def listar_mensajes(
        self, desde_lamport: int = 0, limite: int = 500
    ) -> list[Mensaje]:
        """Historial ordenado por (lamport, origin).

        ``desde_lamport`` se interpreta como "lamport mayor o igual que". El
        cliente CLI pide un poco de solapamiento y descarta ids ya vistos;
        así no se pierden mensajes que comparten la misma marca de Lamport.
        """
        with self._candado:
            filas = self._conexion.execute(
                """
                SELECT id, sender, origin, lamport, sent_at, body
                FROM messages
                WHERE lamport >= ?
                ORDER BY lamport ASC, origin ASC, id ASC
                LIMIT ?
                """,
                (desde_lamport, limite),
            ).fetchall()

        return [self._fila_a_mensaje(fila) for fila in filas]

    def contar_mensajes(self) -> int:
        with self._candado:
            fila = self._conexion.execute(
                "SELECT COUNT(*) AS n FROM messages"
            ).fetchone()
            return int(fila["n"]) if fila else 0

    @staticmethod
    def _fila_a_mensaje(fila: sqlite3.Row) -> Mensaje:
        """Traduce una fila SQLite al modelo del protocolo."""
        return Mensaje.model_validate(
            {
                "id": fila["id"],
                "from": fila["sender"],
                "origin": fila["origin"],
                "lamport": fila["lamport"],
                "sent_at": fila["sent_at"],
                "body": fila["body"],
            }
        )

    # ------------------------------------------------------------------
    # Cola de salida persistente
    # ------------------------------------------------------------------

    def encolar_salida(self, id_mensaje: str) -> None:
        with self._candado:
            self._conexion.execute(
                """
                INSERT OR IGNORE INTO outbox
                    (message_id, attempts, next_retry_at, created_at)
                VALUES (?, 0, 0, ?)
                """,
                (id_mensaje, ahora_utc_iso()),
            )
            self._conexion.commit()

    def obtener_cola_pendiente(
        self, ahora: float, limite: int = 20
    ) -> list[ElementoColaSalida]:
        """Mensajes cuyo próximo intento ya venció, del más antiguo al más nuevo."""
        with self._candado:
            filas = self._conexion.execute(
                """
                SELECT m.id, m.sender, m.origin, m.lamport, m.sent_at, m.body,
                       o.attempts
                FROM outbox AS o
                JOIN messages AS m ON m.id = o.message_id
                WHERE o.next_retry_at <= ?
                ORDER BY o.next_retry_at ASC, m.lamport ASC
                LIMIT ?
                """,
                (ahora, limite),
            ).fetchall()

        return [
            ElementoColaSalida(
                mensaje=self._fila_a_mensaje(fila),
                intentos=int(fila["attempts"]),
            )
            for fila in filas
        ]

    def programar_reintento_cola(
        self, id_mensaje: str, intentos: int, proximo_intento_en: float
    ) -> None:
        with self._candado:
            self._conexion.execute(
                """
                UPDATE outbox
                SET attempts = ?, next_retry_at = ?
                WHERE message_id = ?
                """,
                (intentos, proximo_intento_en, id_mensaje),
            )
            self._conexion.commit()

    def quitar_de_cola(self, id_mensaje: str) -> None:
        """Quita de la cola: el central aceptó el mensaje o lo rechazó con 4xx."""
        with self._candado:
            self._conexion.execute(
                "DELETE FROM outbox WHERE message_id = ?", (id_mensaje,)
            )
            self._conexion.commit()

    def contar_pendientes(self) -> int:
        with self._candado:
            fila = self._conexion.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()
            return int(fila["n"]) if fila else 0

    # ------------------------------------------------------------------
    # Estado pequeño (reloj y marcas de sincronización)
    # ------------------------------------------------------------------

    def obtener_estado(self, clave: str, por_defecto: str = "0") -> str:
        with self._candado:
            fila = self._conexion.execute(
                "SELECT value FROM state WHERE key = ?", (clave,)
            ).fetchone()
            return str(fila["value"]) if fila is not None else por_defecto

    def guardar_estado(self, clave: str, valor: str) -> None:
        with self._candado:
            self._conexion.execute(
                """
                INSERT INTO state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (clave, valor),
            )
            self._conexion.commit()

    def avanzar_estado_entero(self, clave: str, valor: int) -> int:
        """Sube una marca entera solo si el nuevo valor es mayor.

        Se usa para ``last_history_lamport``: varias tareas (latido e ingreso
        federado) pueden intentar avanzarla al mismo tiempo. Hacerlo en una
        sola operación evita una condición de carrera.
        """
        with self._candado:
            fila = self._conexion.execute(
                "SELECT value FROM state WHERE key = ?", (clave,)
            ).fetchone()
            actual = int(fila["value"]) if fila is not None else 0
            nuevo_valor = max(actual, int(valor))

            if fila is None or nuevo_valor != actual:
                self._conexion.execute(
                    """
                    INSERT INTO state (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (clave, str(nuevo_valor)),
                )
                self._conexion.commit()

            return nuevo_valor
