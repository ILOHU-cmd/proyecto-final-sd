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
    from app.database import BaseDatos

_CLAVE_ESTADO = "lamport"


class RelojLamport:
    """Contador lógico persistente y seguro entre hilos."""

    def __init__(self, base_datos: BaseDatos) -> None:
        self._base_datos = base_datos
        self._candado = threading.Lock()
        self._valor = int(base_datos.obtener_estado(_CLAVE_ESTADO, "0"))

    @property
    def valor(self) -> int:
        """Valor actual, sin modificarlo."""
        with self._candado:
            return self._valor

    def avanzar(self) -> int:
        """Evento de envío: incrementa el reloj y devuelve la nueva marca."""
        with self._candado:
            self._valor += 1
            self._base_datos.guardar_estado(_CLAVE_ESTADO, str(self._valor))
            return self._valor

    def observar(self, lamport_recibido: int) -> int:
        """Evento de recepción: se sincroniza con la marca del mensaje recibido."""
        with self._candado:
            self._valor = max(self._valor, int(lamport_recibido)) + 1
            self._base_datos.guardar_estado(_CLAVE_ESTADO, str(self._valor))
            return self._valor
