"""Experimento de pérdidas y recuperación ante una caída.

Pregunta elegida (sección 5 del enunciado):
    ¿Cuántos mensajes se pierden o se duplican cuando el servidor de un grupo
    se detiene durante unos segundos mientras otro servidor sigue enviando?

Método:
    1. Un servidor "emisor" (por ejemplo g3b) envía mensajes a #general a una
       tasa fija durante ``--duration`` segundos.
    2. En ``--down-at`` segundos se detiene el contenedor del servidor
       "objetivo" (por ejemplo g3) con ``docker compose stop``.
    3. Tras ``--down-for`` segundos se vuelve a levantar con
       ``docker compose start``. Se usa stop/start (y no restart) para
       controlar la duración exacta de la caída; ``docker compose restart``
       también sirve para verificar persistencia, pero no permite fijar
       cuánto tiempo estará caído el servicio.
    4. Al final se compara el historial del objetivo con los ids que el emisor
       recibió al crear cada mensaje. Se cuentan faltantes y duplicados, y se
       mide cuánto tardó el objetivo en alcanzar al emisor.

La hipótesis es que no se pierde ninguno (el objetivo pide /history al volver)
y no se duplica ninguno (deduplicación por UUID). El script imprime una tabla
y guarda los datos en JSON y CSV dentro de ``experiment/results/``.

Uso típico (desde la raíz del repositorio, con el central y los dos grupos
levantados):

    python scripts/run_experiment.py \
        --sender-url http://localhost:8002 \
        --target-url http://localhost:8001 \
        --service chat-g3 \
        --repetitions 3

Con ``--restart`` se usa ``docker compose restart`` en lugar de stop/start;
la duración de la caída la decide Docker. El modo por defecto (stop/start)
permite fijar ``--down-for`` y medir con más precisión.

El script debe ejecutarse en el computador anfitrión (necesita Docker). No se
ejecuta dentro de un contenedor.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "experiment" / "results"

# La API local limita cada consulta a 5000 mensajes. Para el volumen de un
# experimento de clase (decenas o cientos de mensajes) es más que suficiente.
HISTORY_PAGE_SIZE = 5000


# ---------------------------------------------------------------------------
# Utilidades HTTP
# ---------------------------------------------------------------------------


def get_json(
    client: httpx.Client, path: str, params: dict[str, Any] | None = None
) -> Any:
    response = client.get(path, params=params)
    response.raise_for_status()
    return response.json()


def post_json(client: httpx.Client, path: str, payload: dict[str, Any]) -> Any:
    response = client.post(path, json=payload)
    response.raise_for_status()
    return response.json()


def ensure_user(client: httpx.Client, username: str) -> None:
    """Crea el usuario si no existe; un 409 significa que ya estaba."""
    response = client.post("/users", json={"username": username})
    if response.status_code in (201, 409):
        return
    response.raise_for_status()


def wait_until(
    predicate: Callable[[], bool], timeout: float, interval: float = 0.5
) -> tuple[bool, float]:
    """Espera a que ``predicate`` sea verdadero. Devuelve (ok, segundos)."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if predicate():
                return True, time.monotonic() - started
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(interval)
    return False, time.monotonic() - started


def docker_compose(compose_dir: Path, *args: str) -> None:
    """Ejecuta ``docker compose ...`` mostrando el comando exacto."""
    compose_file = compose_dir / "docker-compose.yml"
    command = ["docker", "compose"]
    if compose_file.exists():
        command += ["-f", str(compose_file)]
    command += list(args)

    print("    $ " + " ".join(command))
    try:
        subprocess.run(command, cwd=compose_dir, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "No se encontró el comando 'docker'. El experimento debe correr "
            "en el anfitrión, no dentro de un contenedor."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Falló el comando de Docker (código {exc.returncode})."
        ) from exc


# ---------------------------------------------------------------------------
# Emisor
# ---------------------------------------------------------------------------


def sender_loop(
    client: httpx.Client,
    user: str,
    rate: float,
    duration: float,
    run_id: str,
    sent: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    stop_event: threading.Event,
) -> None:
    """Envía a tasa fija durante ``duration`` segundos.

    Cada mensaje tiene un cuerpo único para poder identificarlo al final. El
    id que devuelve el servidor es el UUID del protocolo: ese es el dato que
    se compara, no el texto.
    """
    interval = 1.0 / rate
    started = time.monotonic()
    index = 0
    next_at = started

    while not stop_event.is_set():
        now = time.monotonic()
        if now - started >= duration:
            break
        if now < next_at:
            stop_event.wait(min(next_at - now, 0.05))
            continue

        body = f"{run_id}-{index:04d}"
        try:
            data = post_json(client, "/messages", {"user": user, "body": body})
            sent.append(
                {
                    "index": index,
                    "id": data["id"],
                    "body": body,
                    "sent_epoch": time.time(),
                }
            )
        except (httpx.HTTPError, ValueError) as exc:
            errors.append({"index": index, "error": repr(exc)})

        index += 1
        next_at = started + index * interval


# ---------------------------------------------------------------------------
# Una repetición completa
# ---------------------------------------------------------------------------


def run_repetition(
    number: int,
    args: argparse.Namespace,
    sender: httpx.Client,
    target: httpx.Client,
) -> dict[str, Any]:
    run_id = f"exp-r{number}-{int(time.time())}"
    ensure_user(sender, args.user)

    sent: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    stop_event = threading.Event()

    thread = threading.Thread(
        target=sender_loop,
        args=(
            sender,
            args.user,
            args.rate,
            args.duration,
            run_id,
            sent,
            errors,
            stop_event,
        ),
        name="experiment-sender",
        daemon=True,
    )
    thread.start()

    # ``time.monotonic`` sirve para medir duraciones; ``time.time`` se usa
    # para comparar con el ``sent_epoch`` que guarda el hilo emisor. No se
    # mezclan: son escalas distintas.
    down_started = 0.0
    down_started_wall = 0.0
    up_command_at = 0.0
    service_started_at = 0.0
    service_started_wall = 0.0
    recovery_seconds = 0.0
    recovered_backlog = True
    backlog_ids: set[str] = set()

    def target_has(ids: set[str]) -> bool:
        """True si el objetivo ya tiene todos los ids indicados."""
        if not ids:
            return True
        data = get_json(
            target,
            "/messages",
            params={"since": 0, "limit": HISTORY_PAGE_SIZE},
        )
        current_ids = {message["id"] for message in data["messages"]}
        return ids.issubset(current_ids)

    if args.skip_docker:
        print("  [sin caída] --skip-docker activo; no se detiene el objetivo")
    else:
        print(f"  esperando {args.down_at:.1f}s para provocar la caída...")
        time.sleep(args.down_at)

        if args.restart:
            # Modo restart: la duración de la caída la decide Docker, no
            # nosotros. Sirve para cumplir el escenario del enunciado, aunque
            # para medir es más preciso stop/start (modo por defecto).
            print(f"  reiniciando '{args.service}' con docker compose restart")
            down_started = time.monotonic()
            down_started_wall = time.time()
            up_command_at = time.monotonic()
            docker_compose(args.compose_dir, "restart", args.service)
        else:
            print(f"  deteniendo '{args.service}' durante {args.down_for:.1f}s")
            down_started = time.monotonic()
            down_started_wall = time.time()
            docker_compose(args.compose_dir, "stop", args.service)
            time.sleep(args.down_for)

            print(f"  levantando '{args.service}'")
            up_command_at = time.monotonic()
            docker_compose(args.compose_dir, "start", args.service)

        # Todo lo enviado hasta este instante es el "rezago" que el objetivo
        # debe recuperar. Se congela la lista: los mensajes posteriores
        # llegarán por reenvío normal del central.
        service_started_at = time.monotonic()
        service_started_wall = time.time()
        backlog_ids = {
            item["id"]
            for item in list(sent)
            if item["sent_epoch"] <= service_started_wall
        }

        print(f"  esperando recuperación del rezago ({len(backlog_ids)} mensajes)...")
        recovered_backlog, _ = wait_until(
            lambda: target_has(backlog_ids),
            timeout=args.recovery_timeout,
        )
        recovery_seconds = time.monotonic() - up_command_at
        if not recovered_backlog:
            print(
                "  ADVERTENCIA: el objetivo no recuperó todo el rezago "
                "dentro del tiempo límite."
            )

    thread.join()

    # Esperar a que el emisor vacíe su cola: si no, los mensajes ni siquiera
    # llegaron al central y el experimento mediría otra cosa.
    print("  esperando a que el emisor entregue todo al central...")
    drain_started = time.monotonic()
    drained, _ = wait_until(
        lambda: get_json(sender, "/status")["pending_messages"] == 0,
        timeout=args.drain_timeout,
    )
    drain_seconds = time.monotonic() - drain_started
    if not drained:
        print(
            "  ADVERTENCIA: el emisor aún tiene mensajes pendientes; "
            "los resultados pueden estar incompletos."
        )

    sent_ids = {item["id"] for item in sent}

    print("  verificando que el objetivo tenga todos los mensajes...")
    recovered_all, _ = wait_until(
        lambda: target_has(sent_ids),
        timeout=args.recovery_timeout,
    )
    recovered = recovered_all and recovered_backlog

    final = get_json(
        target, "/messages", params={"since": 0, "limit": HISTORY_PAGE_SIZE}
    )
    ids = [message["id"] for message in final["messages"]]
    counts = Counter(ids)

    missing_ids = sorted(sent_ids - set(ids))
    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)

    during_downtime = 0
    if down_started_wall and service_started_wall:
        during_downtime = sum(
            1
            for item in sent
            if down_started_wall <= item["sent_epoch"] <= service_started_wall
        )

    result = {
        "repetition": number,
        "run_id": run_id,
        "sent": len(sent),
        "send_errors": len(errors),
        "during_downtime": during_downtime,
        "missing": len(missing_ids),
        "missing_ids": missing_ids[:20],
        "duplicates": duplicate_count,
        "recovery_seconds": round(recovery_seconds, 3),
        "drained": drained,
        "drain_seconds": round(drain_seconds, 3),
        "recovered": recovered,
        "docker_down_seconds": (
            round(service_started_at - down_started, 3)
            if down_started and service_started_at
            else 0.0
        ),
        "errors": errors[:20],
    }

    print(
        "  resultado: enviados={sent} durante_caida={during_downtime} "
        "perdidos={missing} duplicados={duplicates} recuperacion={recovery_seconds}s".format(
            **result
        )
    )
    return result


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mide pérdidas, duplicados y tiempo de recuperación.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sender-url", default="http://localhost:8002", help="Servidor emisor"
    )
    parser.add_argument(
        "--target-url", default="http://localhost:8001", help="Servidor que se cae"
    )
    parser.add_argument(
        "--service",
        default="chat-g3",
        help="Nombre del servicio del objetivo en docker-compose.yml",
    )
    parser.add_argument("--user", default="bot", help="Usuario emisor")
    parser.add_argument("--rate", type=float, default=2.0, help="Mensajes/segundo")
    parser.add_argument("--duration", type=float, default=20.0, help="Segundos")
    parser.add_argument(
        "--down-at", type=float, default=5.0, help="Cuándo provocar la caída"
    )
    parser.add_argument(
        "--down-for", type=float, default=5.0, help="Duración de la caída"
    )
    parser.add_argument(
        "--repetitions", type=int, default=3, help="Repeticiones de la medición"
    )
    parser.add_argument(
        "--recovery-timeout",
        type=float,
        default=60.0,
        help="Máximo a esperar por la recuperación",
    )
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=60.0,
        help="Máximo a esperar a que el emisor vacíe su cola",
    )
    parser.add_argument(
        "--compose-dir",
        type=Path,
        default=PROJECT_ROOT,
        help="Directorio donde está docker-compose.yml",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directorio donde se guardan JSON y CSV",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help=(
            "Provocar la caída con 'docker compose restart' en lugar de "
            "stop/start. La duración la decide Docker."
        ),
    )
    parser.add_argument(
        "--skip-docker",
        action="store_true",
        help="No detiene el objetivo (útil para probar el script sin Docker)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Los mensajes de progreso deben verse aunque la salida se redirija a un
    # archivo o a una tubería.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    args = parse_args(argv)

    if args.sender_url.rstrip("/") == args.target_url.rstrip("/"):
        print(
            "Error: --sender-url y --target-url deben ser servidores distintos.",
            file=sys.stderr,
        )
        return 2
    if args.rate <= 0 or args.duration <= 0 or args.repetitions <= 0:
        print(
            "Error: rate, duration y repetitions deben ser positivos.", file=sys.stderr
        )
        return 2
    if args.rate * args.duration > HISTORY_PAGE_SIZE:
        print(
            "Error: rate * duration supera el límite de "
            f"{HISTORY_PAGE_SIZE} mensajes por consulta.",
            file=sys.stderr,
        )
        return 2
    if not args.skip_docker and args.down_at >= args.duration:
        print(
            "Error: --down-at debe ser menor que --duration.",
            file=sys.stderr,
        )
        return 2
    if (
        not args.skip_docker
        and not args.restart
        and args.down_at + args.down_for >= args.duration
    ):
        print(
            "Error: la caída debe terminar antes de --duration "
            "(down_at + down_for < duration).",
            file=sys.stderr,
        )
        return 2

    sender = httpx.Client(base_url=args.sender_url.rstrip("/"), timeout=15.0)
    target = httpx.Client(base_url=args.target_url.rstrip("/"), timeout=15.0)

    results: list[dict[str, Any]] = []
    try:
        # Comprobación previa: ambos servidores deben estar arriba.
        for name, client in (("emisor", sender), ("objetivo", target)):
            try:
                status = get_json(client, "/status")
            except (httpx.HTTPError, ValueError) as exc:
                print(
                    f"Error: el servidor {name} ({client.base_url}) no responde: {exc}",
                    file=sys.stderr,
                )
                return 1
            print(
                f"Servidor {name}: {status['server']} "
                f"(central_online={status['central_online']})"
            )

        for repetition in range(1, args.repetitions + 1):
            print(f"\nRepetición {repetition}/{args.repetitions}")
            result = run_repetition(repetition, args, sender, target)
            results.append(result)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        sender.close()
        target.close()

    # ------------------------------------------------------------------
    # Resumen
    # ------------------------------------------------------------------
    total_missing = sum(r["missing"] for r in results)
    total_duplicates = sum(r["duplicates"] for r in results)
    recoveries = [r["recovery_seconds"] for r in results]
    mean_recovery = sum(recoveries) / len(recoveries) if recoveries else 0.0

    print("\n" + "=" * 78)
    print("RESULTADOS")
    print("=" * 78)
    header = (
        f"{'Rep':>4} {'Enviados':>9} {'En caída':>9} {'Perdidos':>9} "
        f"{'Duplic.':>8} {'Recup.(s)':>10} {'Estado':>10}"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        state = "OK" if result["recovered"] and result["drained"] else "REVISAR"
        print(
            f"{result['repetition']:>4} {result['sent']:>9} "
            f"{result['during_downtime']:>9} {result['missing']:>9} "
            f"{result['duplicates']:>8} {result['recovery_seconds']:>10.2f} "
            f"{state:>10}"
        )
    print("-" * len(header))
    print(f"Total perdidos: {total_missing}   Total duplicados: {total_duplicates}")
    print(f"Tiempo medio de recuperación: {mean_recovery:.2f} s")

    # ------------------------------------------------------------------
    # Persistencia de los datos
    # ------------------------------------------------------------------
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = args.output_dir / f"experiment_{stamp}.json"
    csv_path = args.output_dir / f"experiment_{stamp}.csv"

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question": (
            "¿Cuántos mensajes se pierden o se duplican cuando el servidor del "
            "grupo se cae durante unos segundos?"
        ),
        "config": {
            "sender_url": args.sender_url,
            "target_url": args.target_url,
            "service": args.service,
            "user": args.user,
            "rate": args.rate,
            "duration": args.duration,
            "down_at": args.down_at,
            "down_for": args.down_for,
            "restart_mode": args.restart,
            "repetitions": args.repetitions,
            "skip_docker": args.skip_docker,
        },
        "runs": results,
        "summary": {
            "total_sent": sum(r["sent"] for r in results),
            "total_missing": total_missing,
            "total_duplicates": total_duplicates,
            "mean_recovery_seconds": round(mean_recovery, 3),
        },
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "repetition",
                "run_id",
                "sent",
                "during_downtime",
                "missing",
                "duplicates",
                "recovery_seconds",
                "docker_down_seconds",
                "drained",
                "recovered",
                "send_errors",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow({key: result[key] for key in writer.fieldnames})

    print(f"\nDatos guardados en:\n  {json_path}\n  {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
