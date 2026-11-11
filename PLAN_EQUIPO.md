# Plan de trabajo — Equipo `g3`

**Integrantes:** Emerson, Daniela, Nataly y Mariana.
**Estado:** el repositorio ya tiene el **esqueleto funcional (MVP)**. No es el
proyecto terminado: es la base común sobre la que cada integrante hará sus
cambios sin pisar el trabajo de los demás.

La creación de la estructura, los archivos, el `Dockerfile`, el
`docker-compose.yml`, el central de prueba y el README base **ya está hecha y
se considera tarea grupal**. No se le asigna a una sola persona. Lo que sigue
es el reparto de la **evolución de cada módulo** y de los **entregables
grupales** (experimento, póster, sustentación y bitácora).

> **Reparto pedido por el equipo:** Emerson y Mariana concentran las partes
> más complejas. Emerson tiene la sincronización con el central y la
> resiliencia (el módulo de código más difícil) y además integra `main`.
> Mariana tiene Docker, CLI, experimento, README y póster (la parte más
> amplia y operativa). Daniela y Nataly tienen módulos más acotados y
> verificables. Si alguien quiere cambiar algo, háganlo **antes** de crear las
> ramas; lo importante es que cada archivo tenga un solo dueño.

---

## 1. Reglas para no generar conflictos

1. **Un archivo tiene un solo dueño.** Nadie edita archivos del módulo de
   otro sin avisar y acordarlo en el grupo.
2. **`main` está protegida.** Solo el integrador (Emerson) hace merges a
   `main`. Nadie hace `git push origin main` directamente.
3. **Cada rama nace de `main` actualizado.**
4. **Cambios pequeños y commits pequeños.** No hacer un commit gigante con
   todo el proyecto.
5. **Antes de pedir merge:** `git fetch`, `git rebase origin/main`, probar y
   hacer `git push --force-with-lease` sobre la rama personal.
6. **No cambiar los contratos entre módulos sin avisar.** Si necesitas cambiar
   el nombre o los parámetros de una función que otro usa, primero se acuerda.
7. **No subir** `.env`, bases `.db`, `data/`, `__pycache__` ni `.venv`.
8. **Cada integrante debe poder explicar su módulo** y las líneas que tocó.
9. **Si un cambio rompe el esqueleto, se corrige en la rama del dueño**, no
   directamente en `main`.

### Contratos que no se cambian en silencio

| Contrato | Dueño | Lo usan |
|---|---|---|
| Modelo `Message` y alias `from` | Daniela | Todos |
| Endpoints `/`, `/health`, `/users`, `/messages`, `/fed/*` | Daniela | Mariana (CLI), central |
| `Database` (`insert_message`, `list_messages`, `enqueue_outbox`, ...) | Nataly | Daniela, Emerson |
| `LamportClock.tick()`, `.observe()`, `.value` | Nataly | Daniela, Emerson |
| `CentralClient.run()`, `.is_online`, `.request_stop()` | Emerson | Daniela |
| Campos de `Settings` y variables de entorno | Emerson | Todos |
| Nombres de servicios/volúmenes del compose | Mariana | Todos |

Si alguien necesita cambiar una firma, lo dice en el grupo y el dueño del
contrato hace el cambio o autoriza el PR.

---

## 2. Reparto individual

| Persona | Rama | Archivos de su propiedad | Módulo | Entregable individual mínimo |
|---|---|---|---|---|
| **Daniela** | `feature/core-api-models` | `app/main.py`, `app/models.py`, `app/logger.py`, `app/__init__.py` | API FastAPI, modelos Pydantic, endpoints federados y logs | Pruebas de API + revisión del contrato OpenAPI |
| **Emerson** | `feature/central-sync-resilience` | `app/central_client.py`, `app/config.py` | Registro, heartbeat, reintentos, historial y configuración | Pruebas de backoff/retry + bitácora de fallos + integración |
| **Nataly** | `feature/lamport-storage` | `app/lamport.py`, `app/database.py` | Reloj de Lamport, SQLite, deduplicación y orden | Pruebas de reloj/persistencia/dedup + análisis del experimento |
| **Mariana** | `feature/cli-docker-infra` | `client/cli.py`, `client/__init__.py`, `scripts/run_experiment.py`, `scripts/mock_central.py`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.dockerignore`, `README.md` | Cliente, contenedores, experimento y documentación | Pruebas del CLI + ejecución real del experimento + README |

> `README.md` lo edita Mariana para evitar conflictos. Los demás le envían el
> texto de su sección o le dejan un comentario; no lo editan al mismo tiempo.
> `requirements.txt` solo se cambia con acuerdo del grupo (dueño: Emerson).

### 2.1 Daniela — API, modelos y logs

**Debe entender:** `app/main.py`, `app/models.py`, `app/logger.py`, cómo se
construye el UUID, cómo se aplica el reloj al enviar y al recibir, y por qué
`from` usa un alias.

**Tarea mínima de la rama:**

- Crear `tests/__init__.py` y `tests/test_api.py` (archivos nuevos, no tocan
  el código de los demás).
- Probar con `fastapi.testclient.TestClient`:
  - `GET /fed/health` responde `server` y `lamport`.
  - `POST /fed/messages` inserta una vez y el duplicado responde `200` con
    `duplicate=true`.
  - `POST /users` responde `201` y el repetido `409`.
  - `POST /messages` responde `201` y aparece en `GET /messages`.
  - Un campo JSON desconocido se ignora sin error.
- Revisar que los endpoints cumplan el contrato del enunciado.
- Si toca `app/models.py`, avisar porque todos dependen de `Message`.

> Sugerencia para el test: antes de importar `app.main`, define `DB_PATH` a un
> archivo temporal y `CENTRAL_URL` a un puerto que no exista. Así las pruebas
> no tocan `data/chat.db` ni dependen de un central real; el cliente de fondo
> simplemente registrará fallos de conexión sin afectar los endpoints.

**Rol grupal:** arquitectura, API y recorrido de un mensaje en el póster;
bitácora de interoperabilidad; sección de arquitectura/API en la sustentación.

**Commits sugeridos:**

```text
test(api): cubre endpoints locales y federados
docs(api): aclara el contrato de /fed/messages
```

**Criterio de aceptación:** `python -m unittest discover -s tests -v` pasa y
los endpoints siguen respondiendo igual que antes.

### 2.2 Emerson — sincronización, resiliencia e integración

**Debe entender:** `app/central_client.py`, `app/config.py`, las tres tareas
de fondo, el backoff con jitter, el heartbeat pull, la recuperación con
`/history` y por qué un `4xx` no se reintenta.

**Tarea mínima de la rama:**

- Crear `tests/test_backoff.py` y probar `exponential_backoff`:
  - el primer intento cae dentro del rango esperado,
  - la espera crece con los intentos,
  - nunca supera `RETRY_MAX_SECONDS` (con el jitter),
  - dos llamadas con el mismo intento no tienen por qué ser iguales.
- Documentar en `docs/PRUEBA_REINTENTOS.md` (archivo nuevo) cómo usar
  `scripts/mock_central.py --fail-first 2 --reject-after 1` y qué líneas
  `REINTENTO` / `DESCARTE_4XX` deben aparecer.
- Verificar el heartbeat y la recuperación con el central caído.
- No cambiar `run()`, `is_online` ni `request_stop()` sin avisar a Daniela
  (su `main.py` los usa).

**Rol grupal:** integrador de `main`; merge de las 4 ramas; prueba de
resiliencia y demo de caída/recuperación; sección de reintentos y recuperación
del póster y de la sustentación; lista de verificación final.

**Commits sugeridos:**

```text
test(sync): valida backoff exponencial y jitter
docs(sync): documenta la demo de reintentos y descarte 4xx
```

**Criterio de aceptación:** el backoff está probado, la demo de reintentos
produce `ENVÍO`, `REINTENTO` y `DESCARTE_4XX` en los logs, y `main` integra
las cuatro ramas sin conflictos.

### 2.3 Nataly — Lamport, persistencia y deduplicación

**Debe entender:** `app/lamport.py`, `app/database.py`, las reglas
`tick`/`observe`, por qué el reloj se persiste, cómo `INSERT OR IGNORE`
implementa la idempotencia y por qué se ordena por `(lamport, origin, id)`.

**Tarea mínima de la rama:**

- Crear `tests/test_storage.py` con una base SQLite temporal:
  - insertar el mismo UUID dos veces devuelve `True` y luego `False`,
  - el historial no crece con el duplicado,
  - `tick()` incrementa y `observe()` aplica `max(local, recibido) + 1`,
  - un reloj nuevo sobre la misma base conserva el valor,
  - `list_messages` respeta el orden `(lamport, origin, id)`,
  - la cola `outbox` se encola, se reprograma y se elimina.
- Verificar que el reinicio conserva usuarios, historial y reloj.
- No cambiar las firmas de `Database` sin avisar a Daniela y Emerson.

**Rol grupal:** analizar los JSON/CSV del experimento, producir la tabla o
gráfica y la sección de resultados/conclusiones del póster; explicar Lamport,
deduplicación y persistencia en la sustentación.

**Commits sugeridos:**

```text
test(storage): cubre deduplicación, reloj persistente y orden
docs(storage): explica por qué el reloj se guarda en SQLite
```

**Criterio de aceptación:** las pruebas pasan y el análisis del experimento
tiene tabla, unidades e interpretación.

### 2.4 Mariana — cliente, Docker, experimento y documentación

**Debe entender:** `client/cli.py`, `scripts/run_experiment.py`,
`scripts/mock_central.py`, el `Dockerfile`, el `compose` y cómo se mide la
caída.

**Tarea mínima de la rama:**

- Crear `tests/test_cli.py` y probar `format_message` con mensajes del propio
  grupo y de otro (texto, marca de Lamport y origen).
- Verificar el CLI contra el servidor local: `users`, `create-user`, `send`,
  `read`, `status` y `chat`.
- Levantar el sistema con Docker (`--profile local --profile pair`) y correr
  el experimento al menos 3 veces; guardar los JSON/CSV en
  `experiment/results/`.
- Actualizar el README con los resultados reales y cualquier ajuste de Docker.
- No editar `app/`; si un cambio de Docker requiere código, avisar al dueño.

**Rol grupal:** ejecución del experimento (con Daniela), diseño/edición y PDF
del póster, README y demo de contenedores/experimento en la sustentación.

**Commits sugeridos:**

```text
test(cli): valida el formato de mensajes propios y ajenos
docs(exp): agrega resultados de las 3 repeticiones
fix(docker): ajusta healthcheck/volumen si hace falta
```

**Criterio de aceptación:** el compose levanta en un equipo limpio, el CLI
funciona y hay 3 repeticiones guardadas con su tabla.

---

## 3. Entregables grupales: quién lidera y quién apoya

| Entregable | Líder | Apoyo | Evidencia en el repositorio |
|---|---|---|---|
| Integración y `main` | Emerson | Todos | `git log --graph`, merges sin conflictos |
| Experimento (3 repeticiones) | Mariana | Daniela (método/bitácora), Nataly (análisis), Emerson (verificación) | `experiment/results/*.json`, `*.csv`, tabla en README |
| Análisis y gráfica | Nataly | Mariana | `experiment/ANALISIS.md` o sección del README |
| Bitácora de interoperabilidad | Daniela | Todos aportan observaciones | `docs/BITACORA_INTEROPERABILIDAD.md` |
| Póster | Mariana (diseño/PDF) | Daniela (arquitectura/API), Emerson (resiliencia), Nataly (resultados), todos revisan | PDF + fuentes |
| Sustentación (10 min) | Todos | — | Guion y ensayo |
| README final | Mariana | Cada uno envía su sección | `README.md` |
| Pruebas | Cada dueño | Emerson integra | `tests/` |

### Reparto de la sustentación (10 minutos)

| Tiempo | Quién | Tema |
|---|---|---|
| 0:00–2:00 | Daniela | Arquitectura en estrella, API y recorrido de un mensaje. |
| 2:00–4:00 | Emerson | Reintentos, jitter, heartbeat, recuperación e idempotencia. |
| 4:00–6:00 | Nataly | Lamport, orden `(lamport, origin)`, deduplicación y SQLite. |
| 6:00–8:00 | Mariana | Demo de caída/recuperación, Docker y resultados del experimento. |
| 8:00–10:00 | Todos | Limitaciones, conclusiones y preguntas. |

El póster se reparte igual: Mariana arma el diseño; Daniela entrega
arquitectura/API; Emerson resiliencia; Nataly resultados; todos revisan antes
del PDF.

---

## 4. Plan de 7 días

| Día | Qué se hace | Punto de control |
|---|---|---|
| 1 | Reunión de 30 min, clonar, crear ramas, correr el esqueleto, leer el módulo propio. | Cada uno levanta el sistema y explica su módulo en 2 minutos. |
| 2 | Primer commit de cada rama (prueba o documentación). | 4 ramas con al menos 1 commit propio. |
| 3 | Mejora/verificación del módulo y pruebas locales. | Pruebas del módulo pasan. |
| 4 | Integración: API → storage → central → infra y prueba completa. | `main` levanta con Docker y los dos servidores conversan. |
| 5 | Experimento: 3 repeticiones y datos guardados. | JSON/CSV en `experiment/results` y tabla. |
| 6 | Póster y guion de la sustentación. | PDF del póster y ensayo cronometrado. |
| 7 | Ensayo final, demo con fallo provocado y entrega. | Todo el equipo responde preguntas. |

Si la fecha de entrega es más corta, prioricen en este orden: **integración
funcional → experimento con 3 repeticiones → póster/sustentación**. No
sacrifiquen el mínimo común por el diseño del póster.

---

## 5. Git: ramas y orden de merge

### 5.1 Una sola vez: Emerson crea el repositorio

```bash
cd chat-distribuido-g3
git init
git add .
git commit -m "chore: esqueleto base del chat distribuido en estrella"
git branch -M main
git remote add origin git@github.com:usuario/chat-distribuido-g3.git
git push -u origin main
```

### 5.2 Cada integrante crea su rama

```bash
git clone git@github.com:usuario/chat-distribuido-g3.git
cd chat-distribuido-g3
git config user.name "Nombre Apellido"
git config user.email "correo@uis.edu.co"

# Cada uno ejecuta SOLO la línea de su nombre.

# Daniela
git switch -c feature/core-api-models

# Nataly
git switch -c feature/lamport-storage

# Emerson
git switch -c feature/central-sync-resilience

# Mariana
git switch -c feature/cli-docker-infra
```

Ejemplo de commit de Emerson:

```bash
git status
git diff
git add app/central_client.py app/config.py tests/test_backoff.py
git commit -m "test(sync): valida backoff exponencial y jitter"
git push -u origin feature/central-sync-resilience
```

### 5.3 Orden de merge (solo Emerson)

| Orden | Rama | Dueño | Motivo |
|---|---|---|---|
| 1 | `feature/core-api-models` | Daniela | Define modelos y contratos de la API. |
| 2 | `feature/lamport-storage` | Nataly | Base de datos, reloj y deduplicación. |
| 3 | `feature/central-sync-resilience` | Emerson | Usa la API y el almacenamiento. |
| 4 | `feature/cli-docker-infra` | Mariana | Integra cliente, Docker y documentación. |

```bash
git switch main
git pull --ff-only origin main
git merge --no-ff feature/core-api-models -m "merge: núcleo de API y modelos"
git push origin main

git merge --no-ff feature/lamport-storage -m "merge: Lamport y SQLite"
git push origin main

git merge --no-ff feature/central-sync-resilience -m "merge: sync, heartbeat y reintentos"
git push origin main

git merge --no-ff feature/cli-docker-infra -m "merge: CLI, Docker y experimento"
git push origin main
```

Después de cada merge, Emerson corre la **prueba mínima**:

```bash
docker compose --profile local --profile pair up --build -d
python client/cli.py --url http://localhost:8001 create-user prueba
python client/cli.py --url http://localhost:8001 send prueba "hola"
sleep 2
python client/cli.py --url http://localhost:8002 read
docker compose down
```

Debe aparecer el mensaje en `g3b` y no debe haber errores en los logs.

### 5.4 Conflicto

```bash
git switch feature/mi-rama
git fetch origin
git rebase origin/main
# resolver el archivo, borrar <<<<<<< ======= >>>>>>>
git add archivo
git rebase --continue
git push --force-with-lease
```

El conflicto lo resuelve el **dueño del archivo**. Si es en `README.md` o
`docker-compose.yml`, Mariana; si es en `app/main.py`, Daniela; si es en
`app/central_client.py`, Emerson; si es en `app/database.py`, Nataly. Nunca se
resuelve directamente en `main`.

---

## 6. Checklist antes de cada merge

- [ ] Estoy en mi rama y `git status` está limpio.
- [ ] Hice `git fetch origin` y `git rebase origin/main`.
- [ ] Solo toqué mis archivos (o avisé por los compartidos).
- [ ] `python -m compileall app client scripts` pasa.
- [ ] `python -m unittest discover -s tests -v` pasa (si ya hay tests).
- [ ] El sistema levanta con Docker y los dos grupos conversan.
- [ ] Mi commit tiene un mensaje claro (`feat`, `fix`, `test`, `docs`).
- [ ] Puedo explicar en 2 minutos qué cambié y por qué.
- [ ] Avisé al integrador (Emerson) para el merge.

---

## 7. Tareas que ya están hechas y son grupales

No se reparten como si fueran de una sola persona; todos deben entenderlas y
pueden corregirlas avisando al grupo:

- estructura de carpetas y archivos base;
- `requirements.txt`, `.env.example`, `.gitignore`, `.dockerignore`;
- `Dockerfile` y `docker-compose.yml` base;
- central de prueba (`scripts/mock_central.py`);
- script base del experimento (`scripts/run_experiment.py`);
- README base con arquitectura, API y comandos;
- MVP que ya conversa entre dos grupos, deduplica y recupera historial.

El trabajo individual empieza **encima de esta base**, no reescribiéndola.
