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

from app.models import Message, utc_now_iso

_SCHEMA = """
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
class OutboxItem:
    """Un mensaje pendiente y el número de intentos ya realizados."""

    message: Message
    attempts: int


class Database:
    """Acceso a datos con operaciones cortas y seguras entre hilos."""

    def __init__(self, path: str) -> None:
        self.path = path

        # En Docker la ruta es /data/chat.db. Si el directorio no existe, se
        # crea para que el contenedor arranque sin pasos manuales.
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------
    # Esquema
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Usuarios
    # ------------------------------------------------------------------

    def add_user(self, username: str) -> bool:
        """Crea un usuario local. Devuelve False si ya existía."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO users (username, created_at) VALUES (?, ?)",
                (username, utc_now_iso()),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def user_exists(self, username: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            ).fetchone()
            return row is not None

    def list_users(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT username FROM users ORDER BY username"
            ).fetchall()
            return [row["username"] for row in rows]

    def count_users(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # Mensajes e idempotencia
    # ------------------------------------------------------------------

    def insert_message(self, message: Message) -> bool:
        """Guarda un mensaje si su UUID no existe.

        Devuelve True si se insertó y False si era un duplicado. La operación
        es atómica: dos entregas simultáneas del mismo id no pueden crear dos
        filas.
        """
        data = message.to_protocol_dict()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (id, sender, origin, lamport, sent_at, body)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    data["id"],
                    data["from"],
                    data["origin"],
                    data["lamport"],
                    data["sent_at"],
                    data["body"],
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def get_message(self, message_id: str) -> Message | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, sender, origin, lamport, sent_at, body
                FROM messages WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
        return self._row_to_message(row) if row is not None else None

    def list_messages(self, since_lamport: int = 0, limit: int = 500) -> list[Message]:
        """Historial ordenado por (lamport, origin).

        ``since_lamport`` se interpreta como "lamport mayor o igual que". El
        cliente CLI pide un poco de solapamiento y descarta ids ya vistos;
        así no se pierden mensajes que comparten la misma marca de Lamport.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, sender, origin, lamport, sent_at, body
                FROM messages
                WHERE lamport >= ?
                ORDER BY lamport ASC, origin ASC, id ASC
                LIMIT ?
                """,
                (since_lamport, limit),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def count_messages(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
            return int(row["n"]) if row else 0

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> Message:
        """Traduce una fila SQLite al modelo del protocolo."""
        return Message.model_validate(
            {
                "id": row["id"],
                "from": row["sender"],
                "origin": row["origin"],
                "lamport": row["lamport"],
                "sent_at": row["sent_at"],
                "body": row["body"],
            }
        )

    # ------------------------------------------------------------------
    # Cola de salida persistente
    # ------------------------------------------------------------------

    def enqueue_outbox(self, message_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO outbox
                    (message_id, attempts, next_retry_at, created_at)
                VALUES (?, 0, 0, ?)
                """,
                (message_id, utc_now_iso()),
            )
            self._conn.commit()

    def get_due_outbox(self, now: float, limit: int = 20) -> list[OutboxItem]:
        """Mensajes cuyo próximo intento ya venció, del más antiguo al más nuevo."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.id, m.sender, m.origin, m.lamport, m.sent_at, m.body,
                       o.attempts
                FROM outbox AS o
                JOIN messages AS m ON m.id = o.message_id
                WHERE o.next_retry_at <= ?
                ORDER BY o.next_retry_at ASC, m.lamport ASC
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [
            OutboxItem(message=self._row_to_message(row), attempts=int(row["attempts"]))
            for row in rows
        ]

    def schedule_outbox_retry(
        self, message_id: str, attempts: int, next_retry_at: float
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE outbox
                SET attempts = ?, next_retry_at = ?
                WHERE message_id = ?
                """,
                (attempts, next_retry_at, message_id),
            )
            self._conn.commit()

    def remove_outbox(self, message_id: str) -> None:
        """Quita de la cola: el central aceptó el mensaje o lo rechazó con 4xx."""
        with self._lock:
            self._conn.execute("DELETE FROM outbox WHERE message_id = ?", (message_id,))
            self._conn.commit()

    def pending_outbox_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()
            return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # Estado pequeño (reloj y marcas de sincronización)
    # ------------------------------------------------------------------

    def get_state(self, key: str, default: str = "0") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
            return str(row["value"]) if row is not None else default

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def advance_state_int(self, key: str, value: int) -> int:
        """Sube una marca entera solo si el nuevo valor es mayor.

        Se usa para ``last_history_lamport``: varias tareas (heartbeat e
        ingreso federado) pueden intentar avanzarla al mismo tiempo. Hacerlo
        en una sola operación evita una condición de carrera.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
            current = int(row["value"]) if row is not None else 0
            new_value = max(current, int(value))
            if row is None or new_value != current:
                self._conn.execute(
                    """
                    INSERT INTO state (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, str(new_value)),
                )
                self._conn.commit()
            return new_value
