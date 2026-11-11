"""Configuración del servidor de grupo.

Este archivo tiene una sola responsabilidad: leer los datos que el servidor
necesita para funcionar y comprobar que sean válidos.

Los datos NO se escriben dentro del código. Se reciben desde afuera mediante
variables de entorno, para que el mismo programa sirva tanto en el computador
del grupo como en la sesión de interoperabilidad del curso.

Si algún dato llega mal, el servidor se detiene al arrancar con un mensaje
claro. Así el error se ve de inmediato y no a mitad de una demostración.

Variables obligatorias del enunciado:
    GROUP_NAME  -> nombre corto del grupo, por ejemplo "g3"
    PUBLIC_URL  -> dirección con la que el servidor central puede alcanzarnos
    CENTRAL_URL -> dirección base del servidor central del profesor
    PORT        -> puerto en el que escucha el servidor

Variables opcionales (controlan el comportamiento ante fallos):
    DB_PATH, REGISTER_INTERVAL_SECONDS, HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS, HEARTBEAT_FAILURES, REQUEST_TIMEOUT_SECONDS,
    RETRY_BASE_SECONDS, RETRY_MAX_SECONDS, LOG_LEVEL
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Patrón que debe cumplir el nombre del grupo.
# Se aceptan letras, números, punto, guion y guion bajo: "g3", "g3a", "grupo-03".
# Se rechazan espacios y símbolos raros porque ese nombre forma la identidad
# visible de los usuarios, por ejemplo "ana@g3".
_PATRON_NOMBRE_GRUPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


@dataclass(frozen=True)
class Configuracion:
    """Datos de configuración ya validados y listos para usar.

    ``frozen=True`` significa que el objeto queda congelado: una vez creado,
    nadie puede cambiarle la dirección del central o el nombre del grupo por
    accidente a mitad de la ejecución. Es seguro compartirlo entre las tareas
    de fondo.
    """

    # Identidad del servidor, por ejemplo "g3".
    nombre_grupo: str

    # Dirección con la que el servidor central nos llama.
    url_publica: str

    # Dirección base del servidor central.
    url_central: str

    # Puerto de uvicorn (lo usa el contenedor, no la aplicación).
    puerto: int

    # Ruta del archivo SQLite donde se guardan los mensajes.
    ruta_base_datos: str

    # Cada cuántos segundos se renueva el registro en el central.
    segundos_entre_registros: float

    # Cada cuántos segundos se pregunta si el central está vivo.
    segundos_entre_latidos: float

    # Cuánto se espera como máximo la respuesta de un latido.
    segundos_espera_latido: float

    # Fallos seguidos de latido necesarios para declarar caído al central.
    fallos_latido_para_caida: int

    # Tiempo máximo de espera de una petición normal.
    segundos_espera_peticion: float

    # Espera inicial de los reintentos.
    segundos_base_reintento: float

    # Espera máxima de los reintentos.
    segundos_maximos_reintento: float

    # Nivel de detalle de los registros: INFO, DEBUG, WARNING...
    nivel_registro: str


def leer_variable_texto(nombre: str, valor_por_defecto: str) -> str:
    """Lee una variable de entorno de texto y le quita los espacios sobrantes."""
    # os.getenv busca la variable. Si no existe, devuelve el valor por defecto.
    valor = os.getenv(nombre, valor_por_defecto)

    # strip() elimina espacios al inicio y al final, que son un error de
    # tipeo muy común al configurar contenedores.
    return valor.strip()


def leer_variable_decimal(nombre: str, valor_por_defecto: float) -> float:
    """Lee una variable de entorno numérica y comprueba que sea positiva."""
    texto = os.getenv(nombre)

    # Si la variable no existe o está vacía, se usa el valor por defecto.
    if texto is None or texto.strip() == "":
        return valor_por_defecto

    # float() convierte texto en número decimal. Si el texto no es un número,
    # lanza ValueError; aquí lo transformamos en un mensaje que dice qué
    # variable está mal, en lugar de dejar un error difícil de leer.
    try:
        numero = float(texto)
    except ValueError as error:
        raise ValueError(
            f"{nombre} debe ser un número; se recibió {texto!r}"
        ) from error

    # Un tiempo negativo o cero no tiene sentido para intervalos ni esperas.
    if numero <= 0:
        raise ValueError(f"{nombre} debe ser mayor que cero; se recibió {texto!r}")

    return numero


def leer_variable_entera(nombre: str, valor_por_defecto: int) -> int:
    """Lee una variable de entorno entera y comprueba que sea positiva.

    Funciona igual que ``leer_variable_decimal``, pero solo acepta números
    enteros (por ejemplo, 8000 y no 8000.5).
    """
    texto = os.getenv(nombre)

    if texto is None or texto.strip() == "":
        return valor_por_defecto

    try:
        numero = int(texto)
    except ValueError as error:
        raise ValueError(
            f"{nombre} debe ser un entero; se recibió {texto!r}"
        ) from error

    if numero <= 0:
        raise ValueError(f"{nombre} debe ser mayor que cero; se recibió {texto!r}")

    return numero


def normalizar_direccion(valor: str, nombre: str) -> str:
    """Deja una dirección lista para usarse en las peticiones HTTP.

    Se encarga de dos cosas:

    1. Quitar la barra final. Sin esto, al unir la dirección con rutas como
       ``/register`` quedaría una doble barra: ``http://host//register``.
    2. Comprobar que empiece por http:// o https://, para detectar errores de
       configuración como ``localhost:9000`` (sin el http://) antes de
       intentar conectarse.
    """
    valor = valor.strip().rstrip("/")

    if not valor.startswith(("http://", "https://")):
        raise ValueError(
            f"{nombre} debe comenzar por http:// o https://; se recibió {valor!r}"
        )

    return valor


def cargar_configuracion() -> Configuracion:
    """Lee todas las variables, las valida y devuelve la configuración completa.

    Se llama una sola vez, al arrancar la aplicación. A partir de ese momento
    el resto del código usa el objeto ``Configuracion`` y no vuelve a leer
    variables de entorno.
    """
    # --- Nombre del grupo -------------------------------------------------
    # Es la identidad del servidor: los usuarios se verán como "ana@g3".
    nombre_grupo = leer_variable_texto("GROUP_NAME", "g3")

    if not _PATRON_NOMBRE_GRUPO.fullmatch(nombre_grupo):
        raise ValueError(
            "GROUP_NAME solo admite letras, números, '.', '-' y '_' "
            f"(máximo 32 caracteres); se recibió {nombre_grupo!r}"
        )

    # --- Puerto y direcciones --------------------------------------------
    # PUBLIC_URL es la dirección que el central usará para alcanzarnos.
    # Debe ser una dirección accesible desde la red, no "localhost" en la
    # sesión real con el profesor.
    puerto = leer_variable_entera("PORT", 8000)

    url_publica = normalizar_direccion(
        leer_variable_texto("PUBLIC_URL", f"http://localhost:{puerto}"),
        "PUBLIC_URL",
    )

    url_central = normalizar_direccion(
        leer_variable_texto("CENTRAL_URL", "http://localhost:9000"),
        "CENTRAL_URL",
    )

    # --- Tiempos de reintento --------------------------------------------
    # El máximo no puede ser menor que la base: no tendría sentido que el
    # primer reintento esperara más que el tope.
    segundos_base_reintento = leer_variable_decimal("RETRY_BASE_SECONDS", 1.0)
    segundos_maximos_reintento = leer_variable_decimal("RETRY_MAX_SECONDS", 30.0)

    if segundos_maximos_reintento < segundos_base_reintento:
        raise ValueError(
            "RETRY_MAX_SECONDS debe ser mayor o igual a RETRY_BASE_SECONDS"
        )

    # --- Objeto final -----------------------------------------------------
    # Se construye una sola vez y queda congelado (frozen=True).
    return Configuracion(
        nombre_grupo=nombre_grupo,
        url_publica=url_publica,
        url_central=url_central,
        puerto=puerto,
        ruta_base_datos=leer_variable_texto("DB_PATH", "./data/chat.db"),
        segundos_entre_registros=leer_variable_decimal(
            "REGISTER_INTERVAL_SECONDS", 10.0
        ),
        segundos_entre_latidos=leer_variable_decimal("HEARTBEAT_INTERVAL_SECONDS", 5.0),
        segundos_espera_latido=leer_variable_decimal("HEARTBEAT_TIMEOUT_SECONDS", 3.0),
        fallos_latido_para_caida=leer_variable_entera("HEARTBEAT_FAILURES", 3),
        segundos_espera_peticion=leer_variable_decimal("REQUEST_TIMEOUT_SECONDS", 5.0),
        segundos_base_reintento=segundos_base_reintento,
        segundos_maximos_reintento=segundos_maximos_reintento,
        nivel_registro=leer_variable_texto("LOG_LEVEL", "INFO").upper(),
    )
