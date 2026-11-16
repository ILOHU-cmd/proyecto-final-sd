# Prueba de reintentos, espera exponencial y descarte 4xx

**Módulo:** `app/central_client.py`, `app/config.py`
**Responsable:** Emerson
**Fecha de la evidencia:** 2026-11-15

## 1. Objetivo

Demostrar, con el servidor funcionando de verdad, las tres reglas de envío
hacia el servidor central:

| Respuesta del central | Qué debe hacer el servidor de grupo |
|---|---|
| `2xx` | Quitar el mensaje de la cola `outbox`. |
| `5xx` o error de red | Reintentar con espera exponencial y azar. |
| `4xx` | No reintentar: registrar `DESCARTE_4XX` y sacarlo de la cola. |

También se comprueba que **todos los intentos llevan el mismo `id`**, que es
lo que permite al central deduplicar.

## 2. Entorno

- Linux, Python 3.14.9 en un entorno virtual.
- `fastapi` 0.162.4, `uvicorn` 0.54.0, `httpx` 0.28.1.
- Central de prueba `scripts/mock_central.py` en el puerto `9100`.
- Servidor de grupo `g3` en el puerto `8111`, con base SQLite **nueva**.

> Los registros del servidor usan la hora local del equipo. El campo
> `sent_at` de los mensajes va en UTC. Por eso el log dice `13:35` y el
> mensaje dice `18:35`: es el mismo instante (UTC-5).

## 3. Preparación

**Importante:** se usa una base nueva. Si se reutiliza una base con mensajes
pendientes de pruebas anteriores, esos mensajes consumen los fallos
programados del mock y la demo sale distinta.

```bash
rm -f /tmp/demo-g3.db /tmp/demo-g3.db-wal /tmp/demo-g3.db-shm

# Terminal 1: central que falla las dos primeras entregas y luego rechaza
# con 400 el siguiente mensaje aceptado.
python scripts/mock_central.py --port 9100 --fail-first 2 --reject-after 1

# Terminal 2: servidor del grupo apuntando a ese central.
GROUP_NAME=g3 \
PUBLIC_URL=http://localhost:8111 \
CENTRAL_URL=http://localhost:9100 \
PORT=8111 \
DB_PATH=/tmp/demo-g3.db \
uvicorn app.main:app --port 8111
```

Luego se crea un usuario y se envían dos mensajes:

```bash
curl -X POST http://localhost:8111/users \
  -H 'Content-Type: application/json' -d '{"username":"ana"}'

# Mensaje 1: recibe 500, 500 y al tercer intento es aceptado.
curl -X POST http://localhost:8111/messages \
  -H 'Content-Type: application/json' \
  -d '{"user":"ana","body":"hola desde la demo de reintentos"}'

# Mensaje 2: el central lo rechaza con 400.
curl -X POST http://localhost:8111/messages \
  -H 'Content-Type: application/json' \
  -d '{"user":"ana","body":"este el central lo rechaza"}'
```

## 4. Resultado real

### 4.1 Log del servidor `g3`

> Se compactó el espaciado de la columna del nivel (`INFO    ` → `INFO`) para
> que las líneas quepan; el contenido es literal.

```text
2026-11-15T13:35:05 INFO [g3] [RECUPERACIÓN_INICIO] central=http://localhost:9100
2026-11-15T13:35:05 INFO [g3] [HISTORIAL_INICIO] modo=completa desde=0 ultima_marca=0
2026-11-15T13:35:05 INFO [g3] [HISTORIAL_SINCRONIZADO] desde=0 nuevos=0 total_recibidos=0 lamport_historial=0
2026-11-15T13:35:05 INFO [g3] [RECUPERACIÓN_OK] lamport=0 pendientes=0
2026-11-15T13:35:06 INFO [g3] [ENVÍO] destino=central id=90600bd9-17ff-4115-9f33-a614c464bace lamport=1 intento=1
2026-11-15T13:35:06 INFO [g3] [REINTENTO] id=90600bd9-17ff-4115-9f33-a614c464bace lamport=1 intento_fallido=1 proximo_intento=2 espera=1.047 motivo=HTTP 500
2026-11-15T13:35:08 INFO [g3] [ENVÍO] destino=central id=90600bd9-17ff-4115-9f33-a614c464bace lamport=1 intento=2
2026-11-15T13:35:08 INFO [g3] [REINTENTO] id=90600bd9-17ff-4115-9f33-a614c464bace lamport=1 intento_fallido=2 proximo_intento=3 espera=2.566 motivo=HTTP 500
2026-11-15T13:35:10 INFO [g3] [ENVÍO] destino=central id=90600bd9-17ff-4115-9f33-a614c464bace lamport=1 intento=3
2026-11-15T13:35:10 INFO [g3] [ENVÍO_OK] id=90600bd9-17ff-4115-9f33-a614c464bace lamport=1 status=200
2026-11-15T13:35:14 INFO [g3] [ENVÍO] destino=central id=a14f6195-9438-493e-a209-870adde5025a lamport=2 intento=1
2026-11-15T13:35:14 INFO [g3] [DESCARTE_4XX] id=a14f6195-9438-493e-a209-870adde5025a lamport=2 status=400 detalle={"detail":"rechazo permanente simulado"}
```

### 4.2 Log del central de prueba

```text
2026-11-15T13:35:06 WARNING [central-mock] fallo temporal simulado (500) en el intento 1
2026-11-15T13:35:08 WARNING [central-mock] fallo temporal simulado (500) en el intento 2
2026-11-15T13:35:10 INFO    [central-mock] mensaje aceptado id=90600bd9-17ff-4115-9f33-a614c464bace from=ana@g3 lamport=1
2026-11-15T13:35:14 WARNING [central-mock] rechazo permanente simulado (400) en el intento 4
```

### 4.3 Estado final del servidor

```json
{"server":"g3","lamport":2,"central_online":true,"pending_messages":0,"users":1}
```

`pending_messages = 0` confirma que la cola quedó vacía: el mensaje aceptado
salió con `2xx` y el rechazado salió con `4xx`, sin reintentos.

## 5. Lectura de la evidencia

1. **El `id` es el mismo** en los tres intentos del mensaje 1. Si se generara
   uno nuevo en cada reintento, el central guardaría tres mensajes distintos.
2. **La espera crece**: 1.047 s después del primer fallo y 2.566 s después
   del segundo. No son valores exactos (`1` y `2`) porque se les aplica el
   azar de ±50 %; en esta corrida el azar cayó por encima del valor base.
3. **El tope funciona**: `calcular_espera_reintento` aplica el `min()` dos
   veces, así que ni siquiera el azar puede superar `RETRY_MAX_SECONDS`.
4. **El 4xx no se reintenta**: hay un solo `ENVÍO` para el mensaje 2 y luego
   `DESCARTE_4XX`. El mensaje sale de la cola (`pending_messages = 0`).
5. **Un 2xx es la única confirmación** que hace que un mensaje deje la cola.
   Si el proceso se cae antes, al reiniciar se reenvía con el mismo `id`.

## 6. Observación honesta (limitación)

El mensaje rechazado con `400` **sigue en el historial local** (la base tiene
2 mensajes), aunque nunca llegó al central. Esto es a propósito: el usuario ya
vio su mensaje en pantalla y el servidor no puede borrarlo sin avisar. Lo que
se descarta es el **envío**, no el mensaje local. En un sistema real habría
que marcarlo como "fallido" y avisar al usuario.

## 7. Cómo repetirlo sin la demo manual

La lógica de la espera tiene pruebas automáticas:

```bash
python -m unittest discover -s tests -v
```

Casos incluidos en `tests/test_backoff.py`:

- la espera crece con cada intento;
- nunca supera el tope;
- tiene azar (dos corridas dan valores distintos);
- un intento 0 o negativo no produce una espera inválida.

## 8. Participación de Mariana: CLI, Docker y experimento

Esta sección corresponde al bloque de Mariana en el plan de trabajo. La
responsabilidad es entregar el cliente de consola, el empaquetado reproducible
con Docker y la medición de una caída de servidor.

### 8.1 Pruebas automatizadas

En `tests/test_cli.py` se verifican:

- el formato de un mensaje propio y la marca de Lamport;
- la distinción visual de un mensaje proveniente de otro grupo;
- la lectura de los argumentos del experimento;
- el rechazo de URLs iguales para emisor y objetivo;
- el rechazo de una tasa de envío no positiva.

Ejecución:

```bash
python -m unittest discover -s tests -v
```

Resultado de la validación: **46 pruebas exitosas** (`OK`), incluyendo las
pruebas del CLI y del script del experimento.

### 8.2 Evidencia del experimento

La corrida guardada en `experiment/results/experiment_20261115T033152Z.json`
y `.csv` ejecutó tres repeticiones con `--skip-docker`. En las tres se
enviaron 2 mensajes, no hubo mensajes perdidos ni duplicados y la cola terminó
vacía. Esta evidencia valida el flujo del script y la persistencia de
resultados, pero **no representa una caída real de Docker**: `during_downtime`
y `docker_down_seconds` son 0.0. La prueba de caída real debe ejecutarse en un
equipo con Docker Compose disponible mediante:

```bash
docker compose --profile local --profile pair up --build -d
python scripts/run_experiment.py \
  --sender-url http://localhost:8002 \
  --target-url http://localhost:8001 \
  --service chat-g3 \
  --rate 2 --duration 20 --down-at 5 --down-for 5 \
  --repetitions 3
docker compose down
```

### 8.3 Guion para el video de Mariana (6:00–8:00)

> En esta parte mostramos la operación desde el usuario y la recuperación
> completa del sistema. Primero levantamos los servicios con Docker Compose:
> el central de prueba, `g3` y `g3b`. Los volúmenes conservan la base SQLite,
> así que usuarios, historial, reloj y mensajes pendientes sobreviven a un
> reinicio.
>
> Desde el CLI creamos el usuario `ana`, enviamos un mensaje y consultamos el
> historial. El CLI siempre habla con el servidor local; la federación ocurre
> en segundo plano entre el grupo y el central. En `status` comprobamos el
> reloj de Lamport, el estado del central y cuántos mensajes quedan en la
> cola.
>
> Ahora ejecutamos el experimento tres veces. El script envía mensajes a una
> tasa fija, detiene `chat-g3` durante unos segundos y luego lo inicia de nuevo.
> Mientras está caído, `g3b` continúa enviando. Al volver, `g3` sincroniza el
> historial y entrega los mensajes pendientes usando los mismos UUID.
>
> La tabla resume las métricas: mensajes enviados, mensajes durante la caída,
> mensajes perdidos, duplicados y tiempo de recuperación. El resultado
> esperado es cero pérdidas y cero duplicados; esto confirma que la cola
> persistente, el historial y la deduplicación hacen tolerante la caída
> temporal del servidor.
>
> Como cierre, Docker hace reproducible el montaje, el CLI permite demostrarlo
> sin depender de llamadas manuales y el experimento convierte la recuperación
> en una medición verificable.
