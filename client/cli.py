"""Cliente de consola para el chat distribuido.

Habla únicamente con el servidor de nuestro grupo (no con el central): esa es
la regla de la arquitectura en estrella. El servidor se encarga de federar.

Ejemplos:

    python client/cli.py create-user ana
    python client/cli.py users
    python client/cli.py send ana "¿Ya subieron el compose?"
    python client/cli.py read
    python client/cli.py read --follow
    python client/cli.py chat ana

La URL se puede cambiar con --url o con la variable GROUP_SERVER_URL.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import Any

import httpx

URL_POR_DEFECTO = os.getenv("GROUP_SERVER_URL", "http://localhost:8001")

_REINICIAR = "\033[0m"
_NEGRITA = "\033[1m"
_TENUE = "\033[2m"
_ROJO = "\033[31m"
_VERDE = "\033[32m"
_AMARILLO = "\033[33m"
_CIAN = "\033[36m"


class ErrorCli(Exception):
    """Error controlado para mostrar un mensaje claro al usuario."""


def _colorear(texto: str, codigo: str, activo: bool) -> str:
    return f"{codigo}{texto}{_REINICIAR}" if activo else texto


class ClienteApi:
    """Envoltorio pequeño de httpx con mensajes de error legibles."""

    def __init__(self, url_base: str) -> None:
        self.url_base = url_base.rstrip("/")
        self._cliente = httpx.Client(base_url=self.url_base, timeout=10.0)

    def cerrar(self) -> None:
        self._cliente.close()

    def solicitar(self, metodo: str, ruta: str, **opciones: Any) -> Any:
        try:
            respuesta = self._cliente.request(metodo, ruta, **opciones)
        except httpx.HTTPError as error:
            raise ErrorCli(
                f"No se pudo conectar con {self.url_base}: {type(error).__name__}"
            ) from error

        if respuesta.status_code >= 400:
            detalle: Any
            try:
                detalle = respuesta.json().get("detail", respuesta.text)
            except ValueError:
                detalle = respuesta.text
            raise ErrorCli(f"HTTP {respuesta.status_code}: {detalle}")

        if respuesta.status_code == 204:
            return None

        return respuesta.json()

    def obtener(self, ruta: str, **parametros: Any) -> Any:
        return self.solicitar("GET", ruta, params=parametros)

    def enviar(self, ruta: str, cuerpo: dict[str, Any]) -> Any:
        return self.solicitar("POST", ruta, json=cuerpo)


def formatear_mensaje(
    mensaje: dict[str, Any], servidor_propio: str, color: bool
) -> str:
    """Formatea un mensaje y distingue visualmente a los grupos.

    El color cian es nuestro grupo y el amarillo los demás. La marca [L=..]
    es el reloj de Lamport, que es el que define el orden real.
    """
    origen = str(mensaje.get("origin", "?"))
    remitente = str(mensaje.get("from", "?"))
    lamport = int(mensaje.get("lamport", 0))
    cuerpo = str(mensaje.get("body", ""))

    codigo = _CIAN if origen == servidor_propio else _AMARILLO
    etiqueta = _colorear(f"[{origen:>4}]", codigo, color)
    texto_lamport = _colorear(f"[L={lamport:>4}]", _TENUE, color)
    texto_remitente = _colorear(f"{remitente:<24}", codigo, color)
    return f"{texto_lamport} {etiqueta} {texto_remitente} {cuerpo}"


def obtener_servidor_propio(api: ClienteApi) -> str:
    """Consulta el nombre del servidor para colorear los mensajes propios."""
    try:
        return str(api.obtener("/status").get("server", "?"))
    except ErrorCli:
        return "?"


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


def comando_usuarios(api: ClienteApi, color: bool) -> None:
    datos = api.obtener("/users")
    usuarios = datos.get("users", [])

    if not usuarios:
        print("No hay usuarios locales. Crea uno con: create-user <nombre>")
        return

    print(_colorear("Usuarios locales:", _NEGRITA, color))
    for usuario in usuarios:
        print(f"  - {usuario['identity']}")


def comando_crear_usuario(api: ClienteApi, nombre_usuario: str, color: bool) -> None:
    usuario = api.enviar("/users", {"username": nombre_usuario})
    print(
        _colorear(
            f"Usuario creado: {usuario['identity']}",
            _VERDE,
            color,
        )
    )


def comando_enviar(
    api: ClienteApi, nombre_usuario: str, texto: str, color: bool
) -> None:
    mensaje = api.enviar(
        "/messages",
        {"user": nombre_usuario, "body": texto},
    )
    print(
        _colorear(
            f"Enviado (id={mensaje['id'][:8]}…, L={mensaje['lamport']})",
            _TENUE,
            color,
        )
    )


def comando_estado(api: ClienteApi, color: bool) -> None:
    informacion = api.obtener("/")
    estado = api.obtener("/status")

    en_linea = bool(estado.get("central_online"))
    texto_estado = (
        _colorear("EN LÍNEA", _VERDE, color)
        if en_linea
        else _colorear("CAÍDO", _ROJO, color)
    )

    print(_colorear(f"Servidor: {estado.get('server')}", _NEGRITA, color))
    print(f"URL pública:      {informacion.get('public_url')}")
    print(f"Central:          {informacion.get('central_url')}")
    print(f"Estado central:   {texto_estado}")
    print(f"Reloj Lamport:    {estado.get('lamport')}")
    print(f"Pendientes:       {estado.get('pending_messages')} mensaje(s)")
    print(f"Usuarios locales: {estado.get('users')}")


def comando_leer(
    api: ClienteApi,
    since: int,
    seguir: bool,
    intervalo: float,
    color: bool,
) -> None:
    servidor_propio = obtener_servidor_propio(api) if color else "?"
    vistos: set[str] = set()

    while True:
        datos = api.obtener("/messages", since=since, limit=5000)

        for mensaje in datos.get("messages", []):
            id_mensaje = str(mensaje.get("id"))
            if id_mensaje in vistos:
                continue

            vistos.add(id_mensaje)
            since = max(since, int(mensaje.get("lamport", 0)))
            print(formatear_mensaje(mensaje, servidor_propio, color))

        if not seguir:
            return

        time.sleep(intervalo)


def comando_chat(
    api: ClienteApi,
    nombre_usuario: str,
    since: int,
    intervalo: float,
    color: bool,
) -> None:
    """Modo interactivo: se escribe con Enter y se reciben mensajes en vivo."""
    servidor_propio = obtener_servidor_propio(api)
    detener = threading.Event()
    candado_impresion = threading.Lock()
    vistos: set[str] = set()
    estado = {"since": since}

    def consultar_mensajes() -> None:
        """Hilo lector: consulta el historial local periódicamente."""
        with httpx.Client(base_url=api.url_base, timeout=10.0) as cliente:
            while not detener.is_set():
                try:
                    respuesta = cliente.get(
                        "/messages",
                        params={"since": estado["since"], "limit": 5000},
                    )
                    respuesta.raise_for_status()
                    mensajes = respuesta.json().get("messages", [])
                except (httpx.HTTPError, ValueError):
                    # Si el servidor se reinicia, el chat sigue vivo y se
                    # recupera en la siguiente vuelta.
                    detener.wait(intervalo)
                    continue

                for mensaje in mensajes:
                    id_mensaje = str(mensaje.get("id"))
                    if id_mensaje in vistos:
                        continue

                    vistos.add(id_mensaje)
                    estado["since"] = max(
                        estado["since"],
                        int(mensaje.get("lamport", 0)),
                    )
                    linea = formatear_mensaje(
                        mensaje,
                        servidor_propio,
                        color,
                    )

                    with candado_impresion:
                        # \r limpia el "> " que muestra input() y luego se
                        # vuelve a dibujar el prompt.
                        print(f"\r{linea}")
                        print("> ", end="", flush=True)

                detener.wait(intervalo)

    lector = threading.Thread(
        target=consultar_mensajes,
        name="lector-chat",
        daemon=True,
    )
    lector.start()

    print(
        _colorear(
            f"Chat de {nombre_usuario}@{servidor_propio}. "
            "Enter para enviar, /usuarios para listar, /salir para terminar.",
            _NEGRITA,
            color,
        )
    )

    try:
        while True:
            try:
                linea = input("> ")
            except EOFError:
                print()
                break

            texto = linea.strip()
            if not texto:
                continue

            if texto in ("/salir", "/exit", "/quit"):
                break

            if texto == "/usuarios":
                try:
                    datos = api.obtener("/users")
                    with candado_impresion:
                        nombres = ", ".join(
                            usuario["identity"] for usuario in datos.get("users", [])
                        )
                        print(f"\rUsuarios: {nombres}")
                        print("> ", end="", flush=True)
                except ErrorCli as error:
                    print(f"\r{_colorear(str(error), _ROJO, color)}")
                    print("> ", end="", flush=True)
                continue

            try:
                mensaje = api.enviar(
                    "/messages",
                    {"user": nombre_usuario, "body": texto},
                )
                with candado_impresion:
                    print(
                        _colorear(
                            f"\r(enviado id={mensaje['id'][:8]}…, "
                            f"L={mensaje['lamport']})",
                            _TENUE,
                            color,
                        )
                    )
                    print("> ", end="", flush=True)
            except ErrorCli as error:
                with candado_impresion:
                    print(f"\r{_colorear(str(error), _ROJO, color)}")
                    print("> ", end="", flush=True)

    except KeyboardInterrupt:
        print()
    finally:
        detener.set()
        lector.join(timeout=2.0)
        print("Hasta luego.")


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------


def construir_analizador() -> argparse.ArgumentParser:
    analizador = argparse.ArgumentParser(
        description="Cliente de consola del chat distribuido (UIS EISI).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  python client/cli.py create-user ana\n"
            '  python client/cli.py send ana "hola a todos"\n'
            "  python client/cli.py read --follow\n"
            "  python client/cli.py chat ana\n"
        ),
    )
    analizador.add_argument(
        "--url",
        default=URL_POR_DEFECTO,
        help=f"URL de nuestro servidor de grupo (default: {URL_POR_DEFECTO})",
    )
    analizador.add_argument(
        "--no-color",
        action="store_true",
        help="Desactiva los colores ANSI (útil al redirigir la salida).",
    )

    subcomandos = analizador.add_subparsers(dest="command", required=True)

    subcomandos.add_parser("users", help="Lista los usuarios locales")
    subcomandos.add_parser(
        "status",
        help="Muestra el estado del servidor y del central",
    )

    crear = subcomandos.add_parser("create-user", help="Crea un usuario local")
    crear.add_argument("username")

    enviar = subcomandos.add_parser("send", help="Envía un mensaje a #general")
    enviar.add_argument("user")
    enviar.add_argument("text", nargs="+", help="Texto del mensaje")

    leer = subcomandos.add_parser("read", help="Lee el historial local")
    leer.add_argument("--since", type=int, default=0, help="Marca de Lamport")
    leer.add_argument(
        "--follow",
        action="store_true",
        help="Sigue leyendo mensajes nuevos",
    )
    leer.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Segundos entre consultas",
    )

    chat = subcomandos.add_parser("chat", help="Modo interactivo para conversar")
    chat.add_argument("user")
    chat.add_argument("--since", type=int, default=0, help="Marca de Lamport")
    chat.add_argument(
        "--interval",
        type=float,
        default=1.5,
        help="Segundos entre consultas",
    )

    return analizador


def principal(argv: list[str] | None = None) -> int:
    # Sin esto, al redirigir la salida a un archivo o a otra tubería los
    # mensajes se quedan en el búfer y pareciera que el cliente no funciona.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    analizador = construir_analizador()
    argumentos = analizador.parse_args(argv)

    color = not argumentos.no_color and sys.stdout.isatty()
    api = ClienteApi(argumentos.url)

    try:
        if argumentos.command == "users":
            comando_usuarios(api, color)
        elif argumentos.command == "create-user":
            comando_crear_usuario(api, argumentos.username, color)
        elif argumentos.command == "send":
            comando_enviar(
                api,
                argumentos.user,
                " ".join(argumentos.text),
                color,
            )
        elif argumentos.command == "read":
            comando_leer(
                api,
                argumentos.since,
                argumentos.follow,
                argumentos.interval,
                color,
            )
        elif argumentos.command == "chat":
            comando_chat(
                api,
                argumentos.user,
                argumentos.since,
                argumentos.interval,
                color,
            )
        elif argumentos.command == "status":
            comando_estado(api, color)
        else:  # pragma: no cover - argparse ya valida las opciones
            analizador.error(f"Comando desconocido: {argumentos.command}")
    except ErrorCli as error:
        print(_colorear(f"Error: {error}", _ROJO, color), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        api.cerrar()

    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
