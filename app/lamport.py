"""Reloj lógico de Lamport.

Un reloj físico no sirve para ordenar eventos en un sistema distribuido: los
relojes de las máquinas se desfasan y no existe un "ahora" global. Lamport
propone un contador que avanza con los eventos y se sincroniza al recibir
mensajes:

    Al emitir:  L = L + 1
    Al recibir: L = max(L, L_recibido) + 1

El valor se guarda en SQLite después de cada cambio. Si no se persistiera, al
reiniciar el servidor el reloj podría retroceder y romper el orden del
historial.

El candado protege el contador porque FastAPI atiende peticiones en hilos
distintos mientras las tareas de fondo corren en el bucle de asyncio.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # Solo para tipos; evita un import circular en ejecución.
    from app.database import Database

_STATE_KEY = "lamport"


class LamportClock:
    """Contador lógico persistente y seguro entre hilos."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._lock = threading.Lock()
        self._value = int(database.get_state(_STATE_KEY, "0"))

    @property
    def value(self) -> int:
        """Valor actual, sin modificarlo."""
        with self._lock:
            return self._value

    def tick(self) -> int:
        """Evento de envío: incrementa el reloj y devuelve la nueva marca."""
        with self._lock:
            self._value += 1
            self._db.set_state(_STATE_KEY, str(self._value))
            return self._value

    def observe(self, received_lamport: int) -> int:
        """Evento de recepción: se sincroniza con la marca del mensaje recibido."""
        with self._lock:
            self._value = max(self._value, int(received_lamport)) + 1
            self._db.set_state(_STATE_KEY, str(self._value))
            return self._value
