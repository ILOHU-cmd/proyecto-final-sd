# Plan de trabajo — Equipo `g3`

**Integrantes:** Emerson, Daniela, Nataly y Mariana.
**Repositorio:** <https://github.com/ILOHU-cmd/proyecto-final-sd>
**Rama base:** `main`

**Estado actual:** el esqueleto funcional ya está en `main`. Las cuatro ramas
de trabajo (`feature/core-api-models`, `feature/lamport-storage`,
`feature/central-sync-resilience` y `feature/cli-docker-infra`) ya existen en
el repositorio y parten de la misma base.

> La estructura de carpetas, el `Dockerfile`, el `docker-compose.yml`, el
> central de prueba, el script del experimento y el README base ya están hechos.
> Eso es **tarea grupal**: todos deben entenderlo, pero no se reparte como
> trabajo individual. El trabajo individual empieza **encima** de esa base.

---

## 0. Resumen de una página

| Persona | Rama | Qué hace | Primer paso concreto | Terminado cuando |
|---|---|---|---|---|
| **Daniela** | `feature/core-api-models` | API, modelos, logs y endpoints federados. | Crear `tests/test_api.py`. | Las pruebas de API pasan y el contrato se cumple. |
| **Nataly** | `feature/lamport-storage` | Reloj de Lamport, SQLite, deduplicación y orden. | Crear `tests/test_storage.py`. | Las pruebas de reloj, dedup y orden pasan. |
| **Emerson** | `feature/central-sync-resilience` | Registro, latido, reintentos, historial e integración de `main`. | Crear `tests/test_backoff.py`. | Las pruebas pasan, la demo de caída funciona y `main` queda integrada. |
| **Mariana** | `feature/cli-docker-infra` | CLI, Docker, experimento, README y póster. | Crear `tests/test_cli.py` y levantar el compose. | 3 repeticiones guardadas y el compose levanta en limpio. |

### Regla de oro

**Un archivo tiene un solo dueño.** Nadie edita archivos del módulo de otra
persona sin avisar y acordarlo. Si necesitas un cambio en un archivo ajeno, se
lo pides al dueño.

### Lo que cada uno NO debe tocar

| Persona | No toca |
|---|---|
| Daniela | `app/config.py`, `app/central_client.py`, `app/database.py`, `app/lamport.py`, `client/`, `scripts/`, Docker. |
| Nataly | `app/main.py`, `app/models.py`, `app/config.py`, `app/central_client.py`, `client/`, `scripts/`, Docker. |
| Emerson | La lógica de `app/main.py` (es de Daniela), `app/database.py`, `app/lamport.py`, `client/`. Sí puede ajustar los imports de `app/main.py` durante la integración. |
| Mariana | Todo `app/`. Si Docker necesita un cambio de código, avisa al dueño del archivo. |

---

## 1. Cómo empezar (todos, el mismo día)

### Paso 1. Clonar el repositorio

El repositorio es privado. Emerson debe invitar a cada integrante como
colaboradora antes de este paso.

```bash
git clone https://github.com/ILOHU-cmd/proyecto-final-sd.git
cd proyecto-final-sd
```

### Paso 2. Configurar la identidad de Git

Se hace una sola vez por computador:

```bash
git config user.name "Nombre Apellido"
git config user.email "correo@uis.edu.co"
```

### Paso 3. Cambiar a la rama personal

Las cuatro ramas ya están creadas en el repositorio. Cada una ejecuta
**solo la línea de su nombre**:

```bash
git fetch origin

# Daniela
git switch feature/core-api-models

# Nataly
git switch feature/lamport-storage

# Emerson
git switch feature/central-sync-resilience

# Mariana
git switch feature/cli-docker-infra
```

Si `git switch` dice que la rama no existe, ejecuta `git fetch origin` y
vuelve a intentarlo.

### Paso 4. Instalar y comprobar que el esqueleto funciona

```bash
python -m venv .venv
source .venv/bin/activate          # En Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

En una terminal, levantar el central de prueba:

```bash
python scripts/mock_central.py --port 9000
```

En otra terminal, levantar el servidor de grupo:

```bash
CENTRAL_URL=http://localhost:9000 \
GROUP_NAME=g3 \
PUBLIC_URL=http://localhost:8001 \
PORT=8001 \
DB_PATH=./data/chat.db \
uvicorn app.main:app --port 8001
```

Comprobar que responde:

```bash
curl http://localhost:8001/health
curl http://localhost:8001/status
```

Si `central_online` sale en `true`, el esqueleto está bien.

### Paso 5. Leer solo tu módulo

No hace falta leer todo el proyecto. Cada una lee los archivos de la sección 2
que le corresponden, en el orden indicado.

---

## 2. Paso a paso por persona

### 2.1 Daniela — API, modelos y logs

**Archivos tuyos:** `app/main.py`, `app/models.py`, `app/logger.py`,
`app/__init__.py`. También puedes crear `tests/test_api.py` y
`tests/__init__.py`.

**Qué debes entender:**

1. Cómo se construye un mensaje: `id`, `from`, `origin`, `lamport`, `sent_at`,
   `body`.
2. Por qué `from` se guarda como `from_` con `alias="from"` (porque `from` es
   una palabra reservada de Python).
3. Diferencia entre la API local (`/users`, `/messages`) y la federada
   (`/fed/health`, `/fed/messages`).
4. Qué responde `POST /fed/messages` cuando el UUID ya existe.
5. Cómo se aplica el reloj al enviar (`tick`) y al recibir (`observe`).

**Paso 1. Crear `tests/__init__.py`**

Archivo vacío. Sirve para que `unittest` encuentre las pruebas.

**Paso 2. Crear `tests/test_api.py`**

Este es un esqueleto; completa los casos que faltan:

```python
import os
import tempfile
import uuid

# La configuración se define ANTES de importar la aplicación.
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test_api.db")
os.environ["GROUP_NAME"] = "g3"
os.environ["CENTRAL_URL"] = "http://127.0.0.1:9"  # puerto que no existe
os.environ["PUBLIC_URL"] = "http://localhost:8000"
os.environ["REGISTER_INTERVAL_SECONDS"] = "3600"
os.environ["HEARTBEAT_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

cliente = TestClient(app)


def test_health():
    respuesta = cliente.get("/health")
    assert respuesta.status_code == 200
    assert respuesta.json()["server"] == "g3"


def test_fed_messages_deduplica():
    mensaje = {
        "id": str(uuid.uuid4()),
        "from": "ana@g1",
        "origin": "g1",
        "lamport": 1,
        "sent_at": "2026-01-01T00:00:00Z",
        "body": "hola",
    }

    primera = cliente.post("/fed/messages", json=mensaje)
    segunda = cliente.post("/fed/messages", json=mensaje)

    assert primera.status_code == 200
    assert segunda.status_code == 200
    assert segunda.json()["duplicate"] is True
```

Casos que debes agregar:

- `POST /users` responde `201`; el mismo usuario otra vez responde `409`.
- `POST /messages` responde `201` y el mensaje aparece en `GET /messages`.
- Un campo JSON desconocido en `/fed/messages` se ignora sin error.
- Un `id` que no es UUID válido responde `422`.

**Paso 3. Ejecutar las pruebas**

```bash
python -m unittest discover -s tests -v
```

**Paso 4. Guardar y subir**

```bash
git add tests/__init__.py tests/test_api.py app/main.py app/models.py app/logger.py
git commit -m "test(api): cubre endpoints locales y federados"
git push -u origin feature/core-api-models
```

**Terminado cuando:** `python -m unittest discover -s tests -v` pasa y los
endpoints responden igual que antes.

**Commits sugeridos:**

```text
test(api): cubre endpoints locales y federados
docs(api): aclara el contrato de /fed/messages
```

---

### 2.2 Nataly — Lamport, persistencia y deduplicación

**Archivos tuyos:** `app/lamport.py`, `app/database.py`. También puedes crear
`tests/test_storage.py`.

**Qué debes entender:**

1. `tick()` incrementa el reloj antes de enviar.
2. `observe(recibido)` aplica `max(local, recibido) + 1`.
3. El reloj se guarda en SQLite para que sobreviva a un reinicio.
4. `INSERT OR IGNORE` con `id` como clave primaria implementa la
   deduplicación.
5. El historial se ordena por `(lamport, origin, id)`.

**Paso 1. Crear `tests/test_storage.py`**

Esqueleto:

```python
import tempfile

from app.database import BaseDatos
from app.lamport import RelojLamport
from app.models import Mensaje


def crear_base():
    ruta = tempfile.mktemp(suffix=".db")
    base = BaseDatos(ruta)
    base.crear_esquema()
    return base


def crear_mensaje(identificador: str, lamport: int = 1) -> Mensaje:
    return Mensaje.model_validate(
        {
            "id": identificador,
            "from": "ana@g3",
            "origin": "g3",
            "lamport": lamport,
            "sent_at": "2026-01-01T00:00:00Z",
            "body": "hola",
        }
    )


def test_deduplicacion():
    base = crear_base()
    mensaje = crear_mensaje("11111111-1111-4111-8111-111111111111")

    assert base.insertar_mensaje(mensaje) is True
    assert base.insertar_mensaje(mensaje) is False
    assert base.contar_mensajes() == 1


def test_reloj():
    base = crear_base()
    reloj = RelojLamport(base)

    assert reloj.avanzar() == 1
    assert reloj.observar(10) == 11
```

Casos que debes agregar:

- Un reloj nuevo sobre la misma base conserva el valor.
- `listar_mensajes` respeta el orden `(lamport, origin, id)`.
- La cola `outbox` se encola, se reprograma y se elimina.
- Un reinicio conserva usuarios, historial y reloj.

**Paso 2. Ejecutar**

```bash
python -m unittest discover -s tests -v
```

**Paso 3. Guardar y subir**

```bash
git add tests/test_storage.py app/lamport.py app/database.py
git commit -m "test(storage): cubre deduplicacion, reloj persistente y orden"
git push -u origin feature/lamport-storage
```

**Terminado cuando:** las pruebas pasan y el orden del historial es el
esperado.

**Commits sugeridos:**

```text
test(storage): cubre deduplicacion, reloj persistente y orden
docs(storage): explica por que el reloj se guarda en SQLite
```

---

### 2.3 Emerson — sincronización, resiliencia e integración

**Archivos tuyos:** `app/central_client.py`, `app/config.py`. También puedes
crear `tests/test_backoff.py` y `docs/PRUEBA_REINTENTOS.md`.

**Qué debes entender:**

1. Las tres tareas de fondo: registro, latido y cola de salida.
2. `calcular_espera_reintento`: espera exponencial con variación aleatoria.
3. El latido consultado (`GET /health`) y los `fallos_latido_para_caida`.
4. La recuperación: registro + `GET /history` + volver a enviar.
5. La regla 2xx / 4xx / 5xx al enviar un mensaje.
6. Por qué reintentar no duplica (UUID + deduplicación del central).

**Paso 1. Crear `tests/test_backoff.py`**

```python
from app.central_client import calcular_espera_reintento


def test_la_espera_crece():
    assert 0.5 <= calcular_espera_reintento(1, 1.0, 30.0) <= 1.5
    assert 1.0 <= calcular_espera_reintento(2, 1.0, 30.0) <= 3.0
    assert 2.0 <= calcular_espera_reintento(3, 1.0, 30.0) <= 6.0


def test_nunca_supera_el_tope():
    for intento in range(1, 20):
        assert calcular_espera_reintento(intento, 1.0, 30.0) <= 30.0


def test_tiene_azar():
    valores = {calcular_espera_reintento(3, 1.0, 30.0) for _ in range(20)}
    assert len(valores) > 1
```

**Paso 2. Ejecutar**

```bash
python -m unittest discover -s tests -v
```

**Paso 3. Probar la resiliencia con el central de prueba**

```bash
# Falla los dos primeros envios y luego rechaza uno con 400.
python scripts/mock_central.py --port 9000 --fail-first 2 --reject-after 1
```

Con el servidor apuntando a `CENTRAL_URL=http://localhost:9000`, envía
mensajes y revisa en el log:

- `ENVÍO` y `REINTENTO` cuando el central devuelve 500.
- `DESCARTE_4XX` cuando devuelve 400.
- `ENVÍO_OK` cuando por fin acepta.

**Paso 4. Documentar la prueba**

Crear `docs/PRUEBA_REINTENTOS.md` con:

- el comando usado,
- qué se esperaba,
- qué líneas del log aparecieron,
- una captura o fragmento del log.

**Paso 5. Guardar y subir**

```bash
git add tests/test_backoff.py docs/PRUEBA_REINTENTOS.md app/central_client.py app/config.py
git commit -m "test(sync): valida backoff exponencial y jitter"
git push
```

**Paso 6. Integrar `main` (solo Emerson)**

Antes de que las demás creen sus ramas, integra tu rama para que todos partan
de los nombres nuevos:

```bash
git switch main
git pull --ff-only origin main
git merge --no-ff feature/central-sync-resilience -m "merge: configuracion y cliente central documentados"
git push origin main
```

Después sigue el orden de la sección 6.3.

**Terminado cuando:** las pruebas de backoff pasan, la demo de reintentos
produce `REINTENTO` y `DESCARTE_4XX`, y `main` integra las cuatro ramas.

**Commits sugeridos:**

```text
test(sync): valida backoff exponencial y jitter
docs(sync): documenta la demo de reintentos y descarte 4xx
```

---

### 2.4 Mariana — cliente, Docker, experimento y documentación

**Archivos tuyos:** `client/cli.py`, `client/__init__.py`,
`scripts/run_experiment.py`, `scripts/mock_central.py`, `Dockerfile`,
`docker-compose.yml`, `.env.example`, `.dockerignore`, `README.md`. También
puedes crear `tests/test_cli.py`.

**Qué debes entender:**

1. Los comandos del CLI: `users`, `create-user`, `send`, `read`, `chat`,
   `status`.
2. Cómo se levanta el sistema con `docker compose`.
3. Qué mide el experimento: mensajes enviados, perdidos, duplicados y tiempo
   de recuperación.
4. Cómo se provoca la caída de un servidor y cómo se recupera.

**Paso 1. Crear `tests/test_cli.py`**

```python
from client.cli import formatear_mensaje


def test_mensaje_propio():
    mensaje = {
        "id": "11111111-1111-4111-8111-111111111111",
        "from": "ana@g3",
        "origin": "g3",
        "lamport": 5,
        "sent_at": "2026-01-01T00:00:00Z",
        "body": "hola",
    }

    texto = formatear_mensaje(mensaje, servidor_propio="g3", color=False)

    assert "ana@g3" in texto
    assert "hola" in texto
    assert "5" in texto
```

Agrega un caso con un mensaje de otro grupo (`origin="g1"`) y verifica que el
formato lo distingue.

**Paso 2. Ejecutar**

```bash
python -m unittest discover -s tests -v
```

**Paso 3. Probar el CLI contra el servidor local**

```bash
python client/cli.py --url http://localhost:8001 users
python client/cli.py --url http://localhost:8001 create-user ana
python client/cli.py --url http://localhost:8001 send ana "hola"
python client/cli.py --url http://localhost:8001 read
python client/cli.py --url http://localhost:8001 status
```

**Paso 4. Levantar con Docker y correr el experimento**

```bash
docker compose --profile local --profile pair up --build -d

python scripts/run_experiment.py --repetitions 3

docker compose down
```

Resultados esperados en `experiment/results/`: un JSON y un CSV por
repetición, con mensajes enviados, perdidos, duplicados y tiempo de
recuperación.

**Paso 5. Actualizar el README**

Agregar los resultados reales (tabla) y cualquier ajuste de Docker. El README
lo edita solo Mariana; las demás le envían su sección.

**Paso 6. Guardar y subir**

```bash
git add tests/test_cli.py client/ scripts/ Dockerfile docker-compose.yml README.md
git commit -m "test(cli): valida el formato de mensajes propios y ajenos"
git commit -m "docs(exp): agrega resultados de las 3 repeticiones"
git push -u origin feature/cli-docker-infra
```

**Terminado cuando:** el compose levanta en un equipo limpio, el CLI funciona
y hay 3 repeticiones guardadas con su tabla.

**Commits sugeridos:**

```text
test(cli): valida el formato de mensajes propios y ajenos
docs(exp): agrega resultados de las 3 repeticiones
fix(docker): ajusta healthcheck/volumen si hace falta
```

---

## 3. Contratos que no se cambian en silencio

| Contrato | Dueño | Lo usan |
|---|---|---|
| `Mensaje`, `SolicitudEnvioMensaje` y el alias `from` | Daniela | Todos |
| Endpoints `/`, `/health`, `/users`, `/messages`, `/fed/*` | Daniela | Mariana (CLI), central |
| `BaseDatos` (`insertar_mensaje`, `listar_mensajes`, `encolar_salida`, ...) | Nataly | Daniela, Emerson |
| `RelojLamport.avanzar()`, `.observar()`, `.valor` | Nataly | Daniela, Emerson |
| `ClienteCentral.ejecutar()`, `.en_linea`, `.solicitar_apagado()` | Emerson | Daniela |
| `Configuracion` (campos) y variables de entorno | Emerson | Todos |
| Servicios y volúmenes del `docker-compose.yml` | Mariana | Todos |

### 3.1 Nombres internos: antes y ahora

Todo el código interno quedó en español. Estas son las equivalencias
principales, por si aparece una referencia vieja:

| Antes | Ahora |
|---|---|
| `load_settings` | `cargar_configuracion` |
| `Settings` | `Configuracion` |
| `CentralClient` | `ClienteCentral` |
| `CentralClient.run()` | `ClienteCentral.ejecutar()` |
| `is_online` | `en_linea` |
| `request_stop()` | `solicitar_apagado()` |
| `exponential_backoff` | `calcular_espera_reintento` |
| `Database` | `BaseDatos` |
| `insert_message` | `insertar_mensaje` |
| `list_messages` | `listar_mensajes` |
| `get_due_outbox` | `obtener_cola_pendiente` |
| `LamportClock` | `RelojLamport` |
| `tick()` | `avanzar()` |
| `observe()` | `observar()` |
| `Message` | `Mensaje` |
| `SendMessageRequest` | `SolicitudEnvioMensaje` |
| `log_event` | `registrar_evento` |
| `format_message` | `formatear_mensaje` |

Lo que **no** se tradujo, porque es el contrato del curso:

- rutas HTTP: `/register`, `/messages`, `/history`, `/health`, `/fed/messages`;
- campos JSON: `id`, `from`, `origin`, `lamport`, `sent_at`, `body`;
- variables de entorno: `GROUP_NAME`, `PUBLIC_URL`, `CENTRAL_URL`, `PORT`;
- nombres de servicios Docker: `chat-g3`, `chat-g3-b`, `central`.

---

## 4. Entregables grupales: quién lidera y quién apoya

| Entregable | Líder | Apoyo | Evidencia en el repositorio |
|---|---|---|---|
| Integración y `main` | Emerson | Todos | `git log --graph`, merges sin conflictos |
| Experimento (3 repeticiones) | Mariana | Daniela (método), Nataly (análisis), Emerson (verificación) | `experiment/results/*.json`, `*.csv` |
| Análisis y gráfica | Nataly | Mariana | Sección del README o `experiment/ANALISIS.md` |
| Bitácora de interoperabilidad | Daniela | Todos | `docs/BITACORA_INTEROPERABILIDAD.md` |
| Póster | Mariana (diseño/PDF) | Daniela (arquitectura), Emerson (resiliencia), Nataly (resultados) | PDF y fuentes |
| Sustentación (10 min) | Todos | — | Guion y ensayo |
| README final | Mariana | Cada uno envía su sección | `README.md` |
| Pruebas | Cada dueño | Emerson integra | `tests/` |

### Reparto de la sustentación (10 minutos)

| Tiempo | Quién | Tema |
|---|---|---|
| 0:00–2:00 | Daniela | Arquitectura en estrella, API y recorrido de un mensaje. |
| 2:00–4:00 | Emerson | Reintentos, jitter, latido, recuperación e idempotencia. |
| 4:00–6:00 | Nataly | Lamport, orden `(lamport, origin)`, deduplicación y SQLite. |
| 6:00–8:00 | Mariana | Demo de caída/recuperación, Docker y resultados. |
| 8:00–10:00 | Todos | Limitaciones, conclusiones y preguntas. |

---

## 5. Plan de 7 días

| Día | Qué se hace | Punto de control |
|---|---|---|
| 1 | Reunión de 30 min, clonar, crear ramas, levantar el esqueleto y leer el módulo propio. | Cada una levanta el sistema y explica su módulo en 2 minutos. |
| 2 | Primer commit de cada rama (prueba o documentación). | 4 ramas con al menos 1 commit propio. |
| 3 | Terminar las pruebas del módulo. | `python -m unittest discover -s tests -v` pasa. |
| 4 | Integración: API → storage → central → infra. | `main` levanta con Docker y los dos servidores conversan. |
| 5 | Experimento: 3 repeticiones. | JSON/CSV en `experiment/results` y tabla. |
| 6 | Póster y guion de la sustentación. | PDF del póster y ensayo cronometrado. |
| 7 | Ensayo final, demo con fallo provocado y entrega. | Todo el equipo responde preguntas. |

Si la fecha es más corta, prioricen: **integración funcional → experimento con
3 repeticiones → póster/sustentación**.

---

## 6. Git: ramas, integración y conflictos

### 6.1 El repositorio ya existe

No hay que crearlo. Solo se clona:

```bash
git clone https://github.com/ILOHU-cmd/proyecto-final-sd.git
cd proyecto-final-sd
```

### 6.2 Cada una cambia a su rama

Las ramas ya existen en el repositorio:

```bash
git fetch origin
git switch feature/mi-rama
```

Para actualizarse con lo último de `main` antes de pedir el merge:

```bash
git fetch origin
git rebase origin/main
```

### 6.3 Orden de merge (solo Emerson)

| Orden | Rama | Dueño | Motivo |
|---|---|---|---|
| 0 | `feature/central-sync-resilience` | Emerson | **Ya integrada.** Dejó la base con los nombres en español. |
| 1 | `feature/core-api-models` | Daniela | Define modelos y contratos de la API. |
| 2 | `feature/lamport-storage` | Nataly | Base de datos, reloj y deduplicación. |
| 3 | `feature/cli-docker-infra` | Mariana | Integra cliente, Docker y documentación. |

El orden 0 ya está hecho. A partir de aquí se integra en el orden 1 → 2 → 3.

```bash
git switch main
git pull --ff-only origin main
git merge --no-ff feature/core-api-models -m "merge: nucleo de API y modelos"
git push origin main
```

Repetir el merge y el push con cada rama, en el orden de la tabla.

### 6.4 Prueba mínima después de cada merge

```bash
docker compose --profile local --profile pair up --build -d
python client/cli.py --url http://localhost:8001 create-user prueba
python client/cli.py --url http://localhost:8001 send prueba "hola"
sleep 2
python client/cli.py --url http://localhost:8002 read
docker compose down
```

El mensaje debe aparecer en `g3b` y no debe haber errores en los logs.

### 6.5 Si hay conflicto

```bash
git switch feature/mi-rama
git fetch origin
git rebase origin/main
# Resolver el archivo y borrar las marcas <<<<<<< ======= >>>>>>>
git add archivo
git rebase --continue
git push --force-with-lease
```

El conflicto lo resuelve **el dueño del archivo**:

| Archivo | Dueño |
|---|---|
| `app/main.py`, `app/models.py`, `app/logger.py` | Daniela |
| `app/database.py`, `app/lamport.py` | Nataly |
| `app/central_client.py`, `app/config.py` | Emerson |
| `docker-compose.yml`, `README.md`, `Dockerfile` | Mariana |

Nunca se resuelve directamente en `main`.

---

## 7. Checklist antes de pedir el merge

- [ ] Estoy en mi rama y `git status` está limpio.
- [ ] Hice `git fetch origin` y `git rebase origin/main`.
- [ ] Solo toqué mis archivos (o avisé por los compartidos).
- [ ] `python -m compileall app client scripts` pasa.
- [ ] `python -m unittest discover -s tests -v` pasa.
- [ ] El sistema levanta y los dos servidores conversan.
- [ ] Mi commit tiene un mensaje claro (`feat`, `fix`, `test`, `docs`).
- [ ] Puedo explicar en 2 minutos qué cambié y por qué.
- [ ] Avisé a Emerson para el merge.

---

## 8. Glosario mínimo de Git

| Comando | Qué hace |
|---|---|
| `git clone` | Trae el repositorio a tu computador. |
| `git switch -c rama` | Crea una rama nueva y te cambia a ella. |
| `git status` | Muestra qué archivos cambiaste. |
| `git add archivo` | Marca un archivo para guardarlo en el próximo commit. |
| `git commit -m "..."` | Guarda una "foto" de los cambios. |
| `git push` | Sube tu rama a GitHub. |
| `git pull` | Trae lo último que hay en GitHub. |
| `git rebase origin/main` | Pone tus cambios encima de lo último de `main`. |
| `git merge rama` | Une una rama con la actual. |

---

## 9. Tareas que ya están hechas y son grupales

Todos deben entenderlas y pueden corregirlas avisando al grupo:

- estructura de carpetas y archivos base;
- `requirements.txt`, `.env.example`, `.gitignore`, `.dockerignore`;
- `Dockerfile` y `docker-compose.yml` base;
- central de prueba (`scripts/mock_central.py`);
- script base del experimento (`scripts/run_experiment.py`);
- README base con arquitectura, API y comandos;
- MVP que ya conversa entre dos grupos, deduplica y recupera historial.

El trabajo individual empieza **encima de esta base**, no reescribiéndola.
