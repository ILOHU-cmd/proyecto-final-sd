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

RAIZ_PROYECTO = Path(__file__).resolve().parent.parent
DIRECTORIO_SALIDA_POR_DEFECTO = RAIZ_PROYECTO / "experiment" / "results"

# La API local limita cada consulta a 5000 mensajes. Para el volumen de un
# experimento de clase (decenas o cientos de mensajes) es más que suficiente.
TAMANO_PAGINA_HISTORIAL = 5000


# ---------------------------------------------------------------------------
# Utilidades HTTP
# ---------------------------------------------------------------------------


def obtener_json(
    cliente: httpx.Client,
    ruta: str,
    parametros: dict[str, Any] | None = None,
) -> Any:
    respuesta = cliente.get(ruta, params=parametros)
    respuesta.raise_for_status()
    return respuesta.json()


def enviar_json(
    cliente: httpx.Client,
    ruta: str,
    cuerpo: dict[str, Any],
) -> Any:
    respuesta = cliente.post(ruta, json=cuerpo)
    respuesta.raise_for_status()
    return respuesta.json()


def asegurar_usuario(cliente: httpx.Client, nombre_usuario: str) -> None:
    """Crea el usuario si no existe; un 409 significa que ya estaba."""
    respuesta = cliente.post("/users", json={"username": nombre_usuario})
    if respuesta.status_code in (201, 409):
        return
    respuesta.raise_for_status()


def esperar_hasta(
    condicion: Callable[[], bool],
    tiempo_maximo: float,
    intervalo: float = 0.5,
) -> tuple[bool, float]:
    """Espera a que ``condicion`` sea verdadera. Devuelve (ok, segundos)."""
    inicio = time.monotonic()

    while time.monotonic() - inicio < tiempo_maximo:
        try:
            if condicion():
                return True, time.monotonic() - inicio
        except (httpx.HTTPError, ValueError):
            pass

        time.sleep(intervalo)

    return False, time.monotonic() - inicio


def ejecutar_docker_compose(directorio_compose: Path, *argumentos: str) -> None:
    """Ejecuta ``docker compose ...`` mostrando el comando exacto."""
    archivo_compose = directorio_compose / "docker-compose.yml"

    comando = ["docker", "compose"]
    if archivo_compose.exists():
        comando += ["-f", str(archivo_compose)]
    comando += list(argumentos)

    print("    $ " + " ".join(comando))

    try:
        subprocess.run(comando, cwd=directorio_compose, check=True)
    except FileNotFoundError as error:
        raise RuntimeError(
            "No se encontró el comando 'docker'. El experimento debe correr "
            "en el anfitrión, no dentro de un contenedor."
        ) from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"Falló el comando de Docker (código {error.returncode})."
        ) from error


# ---------------------------------------------------------------------------
# Emisor
# ---------------------------------------------------------------------------


def bucle_emisor(
    cliente: httpx.Client,
    nombre_usuario: str,
    tasa: float,
    duracion: float,
    id_ejecucion: str,
    enviados: list[dict[str, Any]],
    errores: list[dict[str, Any]],
    evento_detener: threading.Event,
) -> None:
    """Envía a tasa fija durante ``duracion`` segundos.

    Cada mensaje tiene un cuerpo único para poder identificarlo al final. El
    id que devuelve el servidor es el UUID del protocolo: ese es el dato que
    se compara, no el texto.
    """
    intervalo = 1.0 / tasa
    inicio = time.monotonic()
    indice = 0
    proximo_envio = inicio

    while not evento_detener.is_set():
        ahora = time.monotonic()

        if ahora - inicio >= duracion:
            break

        if ahora < proximo_envio:
            evento_detener.wait(min(proximo_envio - ahora, 0.05))
            continue

        cuerpo = f"{id_ejecucion}-{indice:04d}"

        try:
            datos = enviar_json(
                cliente,
                "/messages",
                {"user": nombre_usuario, "body": cuerpo},
            )
            enviados.append(
                {
                    "index": indice,
                    "id": datos["id"],
                    "body": cuerpo,
                    "sent_epoch": time.time(),
                }
            )
        except (httpx.HTTPError, ValueError) as error:
            errores.append({"index": indice, "error": repr(error)})

        indice += 1
        proximo_envio = inicio + indice * intervalo


# ---------------------------------------------------------------------------
# Una repetición completa
# ---------------------------------------------------------------------------


def ejecutar_repeticion(
    numero: int,
    argumentos: argparse.Namespace,
    emisor: httpx.Client,
    objetivo: httpx.Client,
) -> dict[str, Any]:
    id_ejecucion = f"exp-r{numero}-{int(time.time())}"
    asegurar_usuario(emisor, argumentos.user)

    enviados: list[dict[str, Any]] = []
    errores: list[dict[str, Any]] = []
    evento_detener = threading.Event()

    hilo = threading.Thread(
        target=bucle_emisor,
        args=(
            emisor,
            argumentos.user,
            argumentos.rate,
            argumentos.duration,
            id_ejecucion,
            enviados,
            errores,
            evento_detener,
        ),
        name="hilo-emisor",
        daemon=True,
    )
    hilo.start()

    # ``time.monotonic`` sirve para medir duraciones; ``time.time`` se usa
    # para comparar con el ``sent_epoch`` que guarda el hilo emisor. No se
    # mezclan: son escalas distintas.
    caida_inicio = 0.0
    caida_inicio_reloj = 0.0
    comando_levantar_en = 0.0
    servicio_inicio = 0.0
    servicio_inicio_reloj = 0.0
    segundos_recuperacion = 0.0
    recupero_rezago = True
    ids_rezago: set[str] = set()

    def objetivo_tiene(ids: set[str]) -> bool:
        """True si el objetivo ya tiene todos los ids indicados."""
        if not ids:
            return True

        datos = obtener_json(
            objetivo,
            "/messages",
            parametros={"since": 0, "limit": TAMANO_PAGINA_HISTORIAL},
        )
        ids_actuales = {mensaje["id"] for mensaje in datos["messages"]}
        return ids.issubset(ids_actuales)

    if argumentos.skip_docker:
        print("  [sin caída] --skip-docker activo; no se detiene el objetivo")
    else:
        print(f"  esperando {argumentos.down_at:.1f}s para provocar la caída...")
        time.sleep(argumentos.down_at)

        if argumentos.restart:
            # Modo restart: la duración de la caída la decide Docker, no
            # nosotros. Sirve para cumplir el escenario del enunciado, aunque
            # para medir es más preciso stop/start (modo por defecto).
            print(f"  reiniciando '{argumentos.service}' con docker compose restart")
            caida_inicio = time.monotonic()
            caida_inicio_reloj = time.time()
            comando_levantar_en = time.monotonic()
            ejecutar_docker_compose(
                argumentos.compose_dir,
                "restart",
                argumentos.service,
            )
        else:
            print(
                f"  deteniendo '{argumentos.service}' durante "
                f"{argumentos.down_for:.1f}s"
            )
            caida_inicio = time.monotonic()
            caida_inicio_reloj = time.time()
            ejecutar_docker_compose(
                argumentos.compose_dir,
                "stop",
                argumentos.service,
            )
            time.sleep(argumentos.down_for)

            print(f"  levantando '{argumentos.service}'")
            comando_levantar_en = time.monotonic()
            ejecutar_docker_compose(
                argumentos.compose_dir,
                "start",
                argumentos.service,
            )

        # Todo lo enviado hasta este instante es el "rezago" que el objetivo
        # debe recuperar. Se congela la lista: los mensajes posteriores
        # llegarán por reenvío normal del central.
        servicio_inicio = time.monotonic()
        servicio_inicio_reloj = time.time()
        ids_rezago = {
            elemento["id"]
            for elemento in list(enviados)
            if elemento["sent_epoch"] <= servicio_inicio_reloj
        }

        print(f"  esperando recuperación del rezago ({len(ids_rezago)} mensajes)...")
        recupero_rezago, _ = esperar_hasta(
            lambda: objetivo_tiene(ids_rezago),
            tiempo_maximo=argumentos.recovery_timeout,
        )
        segundos_recuperacion = time.monotonic() - comando_levantar_en

        if not recupero_rezago:
            print(
                "  ADVERTENCIA: el objetivo no recuperó todo el rezago "
                "dentro del tiempo límite."
            )

    hilo.join()

    # Esperar a que el emisor vacíe su cola: si no, los mensajes ni siquiera
    # llegaron al central y el experimento mediría otra cosa.
    print("  esperando a que el emisor entregue todo al central...")
    drenaje_inicio = time.monotonic()
    drenado, _ = esperar_hasta(
        lambda: obtener_json(emisor, "/status")["pending_messages"] == 0,
        tiempo_maximo=argumentos.drain_timeout,
    )
    segundos_drenaje = time.monotonic() - drenaje_inicio

    if not drenado:
        print(
            "  ADVERTENCIA: el emisor aún tiene mensajes pendientes; "
            "los resultados pueden estar incompletos."
        )

    ids_enviados = {elemento["id"] for elemento in enviados}

    print("  verificando que el objetivo tenga todos los mensajes...")
    recupero_todo, _ = esperar_hasta(
        lambda: objetivo_tiene(ids_enviados),
        tiempo_maximo=argumentos.recovery_timeout,
    )
    recuperado = recupero_todo and recupero_rezago

    final = obtener_json(
        objetivo,
        "/messages",
        parametros={"since": 0, "limit": TAMANO_PAGINA_HISTORIAL},
    )
    ids = [mensaje["id"] for mensaje in final["messages"]]
    conteos = Counter(ids)

    ids_faltantes = sorted(ids_enviados - set(ids))
    cantidad_duplicados = sum(conteo - 1 for conteo in conteos.values() if conteo > 1)

    durante_caida = 0
    if caida_inicio_reloj and servicio_inicio_reloj:
        durante_caida = sum(
            1
            for elemento in enviados
            if caida_inicio_reloj <= elemento["sent_epoch"] <= servicio_inicio_reloj
        )

    resultado = {
        "repetition": numero,
        "run_id": id_ejecucion,
        "sent": len(enviados),
        "send_errors": len(errores),
        "during_downtime": durante_caida,
        "missing": len(ids_faltantes),
        "missing_ids": ids_faltantes[:20],
        "duplicates": cantidad_duplicados,
        "recovery_seconds": round(segundos_recuperacion, 3),
        "drained": drenado,
        "drain_seconds": round(segundos_drenaje, 3),
        "recovered": recuperado,
        "docker_down_seconds": (
            round(servicio_inicio - caida_inicio, 3)
            if caida_inicio and servicio_inicio
            else 0.0
        ),
        "errors": errores[:20],
    }

    print(
        "  resultado: enviados={sent} durante_caida={during_downtime} "
        "perdidos={missing} duplicados={duplicates} "
        "recuperacion={recovery_seconds}s".format(**resultado)
    )
    return resultado


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------


def leer_argumentos(argv: list[str] | None = None) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(
        description="Mide pérdidas, duplicados y tiempo de recuperación.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    analizador.add_argument(
        "--sender-url",
        default="http://localhost:8002",
        help="Servidor emisor",
    )
    analizador.add_argument(
        "--target-url",
        default="http://localhost:8001",
        help="Servidor que se cae",
    )
    analizador.add_argument(
        "--service",
        default="chat-g3",
        help="Nombre del servicio del objetivo en docker-compose.yml",
    )
    analizador.add_argument("--user", default="bot", help="Usuario emisor")
    analizador.add_argument(
        "--rate",
        type=float,
        default=2.0,
        help="Mensajes/segundo",
    )
    analizador.add_argument(
        "--duration",
        type=float,
        default=20.0,
        help="Segundos",
    )
    analizador.add_argument(
        "--down-at",
        type=float,
        default=5.0,
        help="Cuándo provocar la caída",
    )
    analizador.add_argument(
        "--down-for",
        type=float,
        default=5.0,
        help="Duración de la caída",
    )
    analizador.add_argument(
        "--repetitions",
        type=int,
        default=3,
        help="Repeticiones de la medición",
    )
    analizador.add_argument(
        "--recovery-timeout",
        type=float,
        default=60.0,
        help="Máximo a esperar por la recuperación",
    )
    analizador.add_argument(
        "--drain-timeout",
        type=float,
        default=60.0,
        help="Máximo a esperar a que el emisor vacíe su cola",
    )
    analizador.add_argument(
        "--compose-dir",
        type=Path,
        default=RAIZ_PROYECTO,
        help="Directorio donde está docker-compose.yml",
    )
    analizador.add_argument(
        "--output-dir",
        type=Path,
        default=DIRECTORIO_SALIDA_POR_DEFECTO,
        help="Directorio donde se guardan JSON y CSV",
    )
    analizador.add_argument(
        "--restart",
        action="store_true",
        help=(
            "Provocar la caída con 'docker compose restart' en lugar de "
            "stop/start. La duración la decide Docker."
        ),
    )
    analizador.add_argument(
        "--skip-docker",
        action="store_true",
        help="No detiene el objetivo (útil para probar el script sin Docker)",
    )
    return analizador.parse_args(argv)


def principal(argv: list[str] | None = None) -> int:
    # Los mensajes de progreso deben verse aunque la salida se redirija a un
    # archivo o a una tubería.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    argumentos = leer_argumentos(argv)

    if argumentos.sender_url.rstrip("/") == argumentos.target_url.rstrip("/"):
        print(
            "Error: --sender-url y --target-url deben ser servidores distintos.",
            file=sys.stderr,
        )
        return 2

    if argumentos.rate <= 0 or argumentos.duration <= 0 or argumentos.repetitions <= 0:
        print(
            "Error: rate, duration y repetitions deben ser positivos.",
            file=sys.stderr,
        )
        return 2

    if argumentos.rate * argumentos.duration > TAMANO_PAGINA_HISTORIAL:
        print(
            "Error: rate * duration supera el límite de "
            f"{TAMANO_PAGINA_HISTORIAL} mensajes por consulta.",
            file=sys.stderr,
        )
        return 2

    if not argumentos.skip_docker and argumentos.down_at >= argumentos.duration:
        print(
            "Error: --down-at debe ser menor que --duration.",
            file=sys.stderr,
        )
        return 2

    if (
        not argumentos.skip_docker
        and not argumentos.restart
        and argumentos.down_at + argumentos.down_for >= argumentos.duration
    ):
        print(
            "Error: la caída debe terminar antes de --duration "
            "(down_at + down_for < duration).",
            file=sys.stderr,
        )
        return 2

    emisor = httpx.Client(
        base_url=argumentos.sender_url.rstrip("/"),
        timeout=15.0,
    )
    objetivo = httpx.Client(
        base_url=argumentos.target_url.rstrip("/"),
        timeout=15.0,
    )

    resultados: list[dict[str, Any]] = []

    try:
        # Comprobación previa: ambos servidores deben estar arriba.
        for nombre, cliente in (("emisor", emisor), ("objetivo", objetivo)):
            try:
                estado = obtener_json(cliente, "/status")
            except (httpx.HTTPError, ValueError) as error:
                print(
                    f"Error: el servidor {nombre} ({cliente.base_url}) "
                    f"no responde: {error}",
                    file=sys.stderr,
                )
                return 1

            print(
                f"Servidor {nombre}: {estado['server']} "
                f"(central_online={estado['central_online']})"
            )

        for repeticion in range(1, argumentos.repetitions + 1):
            print(f"\nRepetición {repeticion}/{argumentos.repetitions}")
            resultado = ejecutar_repeticion(
                repeticion,
                argumentos,
                emisor,
                objetivo,
            )
            resultados.append(resultado)

    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        emisor.close()
        objetivo.close()

    # ------------------------------------------------------------------
    # Resumen
    # ------------------------------------------------------------------
    total_faltantes = sum(resultado["missing"] for resultado in resultados)
    total_duplicados = sum(resultado["duplicates"] for resultado in resultados)
    recuperaciones = [resultado["recovery_seconds"] for resultado in resultados]
    recuperacion_media = (
        sum(recuperaciones) / len(recuperaciones) if recuperaciones else 0.0
    )

    print("\n" + "=" * 78)
    print("RESULTADOS")
    print("=" * 78)

    encabezado = (
        f"{'Rep':>4} {'Enviados':>9} {'En caída':>9} {'Perdidos':>9} "
        f"{'Duplic.':>8} {'Recup.(s)':>10} {'Estado':>10}"
    )
    print(encabezado)
    print("-" * len(encabezado))

    for resultado in resultados:
        estado = "OK" if resultado["recovered"] and resultado["drained"] else "REVISAR"
        print(
            f"{resultado['repetition']:>4} {resultado['sent']:>9} "
            f"{resultado['during_downtime']:>9} "
            f"{resultado['missing']:>9} "
            f"{resultado['duplicates']:>8} "
            f"{resultado['recovery_seconds']:>10.2f} "
            f"{estado:>10}"
        )

    print("-" * len(encabezado))
    print(f"Total perdidos: {total_faltantes}   Total duplicados: {total_duplicados}")
    print(f"Tiempo medio de recuperación: {recuperacion_media:.2f} s")

    # ------------------------------------------------------------------
    # Persistencia de los datos
    # ------------------------------------------------------------------
    argumentos.output_dir.mkdir(parents=True, exist_ok=True)

    sello = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ruta_json = argumentos.output_dir / f"experiment_{sello}.json"
    ruta_csv = argumentos.output_dir / f"experiment_{sello}.csv"

    contenido = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "question": (
            "¿Cuántos mensajes se pierden o se duplican cuando el servidor del "
            "grupo se cae durante unos segundos?"
        ),
        "config": {
            "sender_url": argumentos.sender_url,
            "target_url": argumentos.target_url,
            "service": argumentos.service,
            "user": argumentos.user,
            "rate": argumentos.rate,
            "duration": argumentos.duration,
            "down_at": argumentos.down_at,
            "down_for": argumentos.down_for,
            "restart_mode": argumentos.restart,
            "repetitions": argumentos.repetitions,
            "skip_docker": argumentos.skip_docker,
        },
        "runs": resultados,
        "summary": {
            "total_sent": sum(resultado["sent"] for resultado in resultados),
            "total_missing": total_faltantes,
            "total_duplicates": total_duplicados,
            "mean_recovery_seconds": round(recuperacion_media, 3),
        },
    }

    ruta_json.write_text(
        json.dumps(contenido, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with ruta_csv.open("w", newline="", encoding="utf-8") as archivo:
        escritor = csv.DictWriter(
            archivo,
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
        escritor.writeheader()

        for resultado in resultados:
            escritor.writerow(
                {clave: resultado[clave] for clave in escritor.fieldnames}
            )

    print(f"\nDatos guardados en:\n  {ruta_json}\n  {ruta_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(principal())
