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

DEFAULT_URL = os.getenv("GROUP_SERVER_URL", "http://localhost:8001")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


class CliError(Exception):
    """Error controlado para mostrar un mensaje claro al usuario."""


def _color(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{_RESET}" if enabled else text


class Api:
    """Envoltorio pequeño de httpx con mensajes de error legibles."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=10.0)

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CliError(
                f"No se pudo conectar con {self.base_url}: {type(exc).__name__}"
            ) from exc

        if response.status_code >= 400:
            detail: Any
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise CliError(f"HTTP {response.status_code}: {detail}")

        if response.status_code == 204:
            return None
        return response.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("POST", path, json=payload)


def format_message(message: dict[str, Any], own_server: str, color: bool) -> str:
    """Formatea un mensaje y distingue visualmente a los grupos.

    El color cian es nuestro grupo y el amarillo los demás. La marca [L=..]
    es el reloj de Lamport, que es el que define el orden real.
    """
    origin = str(message.get("origin", "?"))
    sender = str(message.get("from", "?"))
    lamport = int(message.get("lamport", 0))
    body = str(message.get("body", ""))

    code = _CYAN if origin == own_server else _YELLOW
    tag = _color(f"[{origin:>4}]", code, color)
    lamport_text = _color(f"[L={lamport:>4}]", _DIM, color)
    sender_text = _color(f"{sender:<24}", code, color)
    return f"{lamport_text} {tag} {sender_text} {body}"


def get_own_server(api: Api) -> str:
    """Consulta el nombre del servidor para colorear los mensajes propios."""
    try:
        return str(api.get("/status").get("server", "?"))
    except CliError:
        return "?"


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------


def cmd_users(api: Api, color: bool) -> None:
    data = api.get("/users")
    users = data.get("users", [])
    if not users:
        print("No hay usuarios locales. Crea uno con: create-user <nombre>")
        return
    print(_color("Usuarios locales:", _BOLD, color))
    for user in users:
        print(f"  - {user['identity']}")


def cmd_create_user(api: Api, username: str, color: bool) -> None:
    user = api.post("/users", {"username": username})
    print(_color(f"Usuario creado: {user['identity']}", _GREEN, color))


def cmd_send(api: Api, user: str, text: str, color: bool) -> None:
    message = api.post("/messages", {"user": user, "body": text})
    print(
        _color(
            f"Enviado (id={message['id'][:8]}…, L={message['lamport']})",
            _DIM,
            color,
        )
    )


def cmd_status(api: Api, color: bool) -> None:
    info = api.get("/")
    status = api.get("/status")

    online = bool(status.get("central_online"))
    online_text = (
        _color("EN LÍNEA", _GREEN, color) if online else _color("CAÍDO", _RED, color)
    )

    print(_color(f"Servidor: {status.get('server')}", _BOLD, color))
    print(f"URL pública:      {info.get('public_url')}")
    print(f"Central:          {info.get('central_url')}")
    print(f"Estado central:   {online_text}")
    print(f"Reloj Lamport:    {status.get('lamport')}")
    print(f"Pendientes:       {status.get('pending_messages')} mensaje(s)")
    print(f"Usuarios locales: {status.get('users')}")


def cmd_read(api: Api, since: int, follow: bool, interval: float, color: bool) -> None:
    own_server = get_own_server(api) if color else "?"
    seen: set[str] = set()

    while True:
        data = api.get("/messages", since=since, limit=5000)
        for message in data.get("messages", []):
            message_id = str(message.get("id"))
            if message_id in seen:
                continue
            seen.add(message_id)
            since = max(since, int(message.get("lamport", 0)))
            print(format_message(message, own_server, color))

        if not follow:
            return
        time.sleep(interval)


def cmd_chat(api: Api, user: str, since: int, interval: float, color: bool) -> None:
    """Modo interactivo: se escribe con Enter y se reciben mensajes en vivo."""
    own_server = get_own_server(api)
    stop = threading.Event()
    print_lock = threading.Lock()
    seen: set[str] = set()
    state = {"since": since}

    def poll_messages() -> None:
        """Hilo lector: consulta el historial local periódicamente."""
        with httpx.Client(base_url=api.base_url, timeout=10.0) as client:
            while not stop.is_set():
                try:
                    response = client.get(
                        "/messages",
                        params={"since": state["since"], "limit": 5000},
                    )
                    response.raise_for_status()
                    messages = response.json().get("messages", [])
                except (httpx.HTTPError, ValueError):
                    # Si el servidor se reinicia, el chat sigue vivo y se
                    # recupera en la siguiente vuelta.
                    stop.wait(interval)
                    continue

                for message in messages:
                    message_id = str(message.get("id"))
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    state["since"] = max(state["since"], int(message.get("lamport", 0)))
                    line = format_message(message, own_server, color)
                    with print_lock:
                        # \r limpia el "> " que muestra input() y luego se
                        # vuelve a dibujar el prompt.
                        print(f"\r{line}")
                        print("> ", end="", flush=True)

                stop.wait(interval)

    reader = threading.Thread(target=poll_messages, name="chat-reader", daemon=True)
    reader.start()

    print(
        _color(
            f"Chat de {user}@{own_server}. "
            "Enter para enviar, /usuarios para listar, /salir para terminar.",
            _BOLD,
            color,
        )
    )

    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                print()
                break

            text = line.strip()
            if not text:
                continue
            if text in ("/salir", "/exit", "/quit"):
                break
            if text == "/usuarios":
                try:
                    data = api.get("/users")
                    with print_lock:
                        names = ", ".join(u["identity"] for u in data.get("users", []))
                        print(f"\rUsuarios: {names}")
                        print("> ", end="", flush=True)
                except CliError as exc:
                    print(f"\r{_color(str(exc), _RED, color)}")
                    print("> ", end="", flush=True)
                continue

            try:
                message = api.post("/messages", {"user": user, "body": text})
                with print_lock:
                    print(
                        _color(
                            f"\r(enviado id={message['id'][:8]}…, "
                            f"L={message['lamport']})",
                            _DIM,
                            color,
                        )
                    )
                    print("> ", end="", flush=True)
            except CliError as exc:
                with print_lock:
                    print(f"\r{_color(str(exc), _RED, color)}")
                    print("> ", end="", flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
        reader.join(timeout=2.0)
        print("Hasta luego.")


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"URL de nuestro servidor de grupo (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Desactiva los colores ANSI (útil al redirigir la salida).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("users", help="Lista los usuarios locales")
    sub.add_parser("status", help="Muestra el estado del servidor y del central")

    create = sub.add_parser("create-user", help="Crea un usuario local")
    create.add_argument("username")

    send = sub.add_parser("send", help="Envía un mensaje a #general")
    send.add_argument("user")
    send.add_argument("text", nargs="+", help="Texto del mensaje")

    read = sub.add_parser("read", help="Lee el historial local")
    read.add_argument("--since", type=int, default=0, help="Marca de Lamport")
    read.add_argument(
        "--follow", action="store_true", help="Sigue leyendo mensajes nuevos"
    )
    read.add_argument(
        "--interval", type=float, default=2.0, help="Segundos entre consultas"
    )

    chat = sub.add_parser("chat", help="Modo interactivo para conversar")
    chat.add_argument("user")
    chat.add_argument("--since", type=int, default=0, help="Marca de Lamport")
    chat.add_argument(
        "--interval", type=float, default=1.5, help="Segundos entre consultas"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    # Sin esto, al redirigir la salida a un archivo o a otra tubería los
    # mensajes se quedan en el búfer y pareciera que el cliente no funciona.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    parser = build_parser()
    args = parser.parse_args(argv)

    color = not args.no_color and sys.stdout.isatty()
    api = Api(args.url)

    try:
        if args.command == "users":
            cmd_users(api, color)
        elif args.command == "create-user":
            cmd_create_user(api, args.username, color)
        elif args.command == "send":
            cmd_send(api, args.user, " ".join(args.text), color)
        elif args.command == "read":
            cmd_read(api, args.since, args.follow, args.interval, color)
        elif args.command == "chat":
            cmd_chat(api, args.user, args.since, args.interval, color)
        elif args.command == "status":
            cmd_status(api, color)
        else:  # pragma: no cover - argparse ya valida las opciones
            parser.error(f"Comando desconocido: {args.command}")
    except CliError as exc:
        print(_color(f"Error: {exc}", _RED, color), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    finally:
        api.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
