# Guion del video — parte de Emerson (2:00 a 4:00)

**Tema:** sincronización con el servidor central, reintentos, latido,
recuperación e idempotencia.
**Duración objetivo:** 2 minutos (el plan reparte 10 minutos entre cuatro).
**Regla:** no leer el README en cámara. Este documento es la chuleta.

---

## 0. Antes de grabar (5 minutos)

- [ ] Terminal con letra grande y fondo oscuro.
- [ ] Cerrar notificaciones y pestañas que no se usen.
- [ ] Grabar por segmentos, no todo de corrido: si algo falla, se repite solo
      ese pedazo.
- [ ] Usar **base de datos nueva** (si no, la cola vieja arruina la demo).
- [ ] Tener abiertas tres terminales: central, servidor y cliente.

Si se graba con Docker:

```bash
docker compose down -v          # borra volúmenes: base limpia
docker compose --profile local up --build -d
docker compose logs -f chat-g3
```

Si se graba sin Docker (verificado, no depende de la red):

```bash
# Terminal 1 — central de prueba
python scripts/mock_central.py --port 9000

# Terminal 2 — servidor del grupo, con base nueva
GROUP_NAME=g3 PUBLIC_URL=http://localhost:8001 \
CENTRAL_URL=http://localhost:9000 PORT=8001 DB_PATH=/tmp/demo-g3.db \
uvicorn app.main:app --port 8001

# Terminal 3 — cliente
python client/cli.py --url http://localhost:8001 create-user ana
```

---

## 1. Guion (2:00)

### 0:00–0:15 — Qué hace tu módulo

> "Mi módulo mantiene el enlace con el servidor central. Son tres tareas de
> fondo: una renueva el registro cada 10 segundos, otra pregunta cada 5
> segundos si el central está vivo, y la tercera saca de una cola persistente
> los mensajes que todavía no han sido aceptados."

Muestra el arranque:

```text
[REGISTRO_OK] servidor=g3 url=http://localhost:8001
[RECUPERACIÓN_INICIO] central=http://localhost:9000
[HISTORIAL_SINCRONIZADO] ... nuevos=0
[RECUPERACIÓN_OK] lamport=0 pendientes=0
```

### 0:15–0:45 — Se cae el central, el mensaje no se pierde

```bash
# Terminal 3
python client/cli.py send ana "antes de la caída"
python client/cli.py status
# -> EN LÍNEA, pendientes 0

# Terminal central (o la consola de Docker)
# Ctrl+C al mock / docker compose stop central
```

Espera **20 segundos** (3 latidos fallidos × 5 s = 15 s) y envía:

```bash
python client/cli.py send ana "mensaje con el central caído"
python client/cli.py status
# -> CAÍDO, pendientes 1
```

Di:

> "El servidor sigue aceptando mensajes de sus usuarios porque la cola está en
> SQLite, no en memoria. Detectó la caída con el latido: tres fallos seguidos
> de `GET /health`."

Log esperado:

```text
[CAÍDA_CENTRAL] intentos_fallidos=3 mensajes_pendientes=1
```

### 0:45–1:10 — Reintentos con espera exponencial

Vuelve a levantar el central. Mientras arranca se ve:

```text
[REINTENTO] id=... intento_fallido=1 proximo_intento=2 espera=1.33
[REINTENTO] id=... intento_fallido=2 proximo_intento=3 espera=2.028
```

> "La espera se duplica en cada intento y se le suma un azar de ±50 %. El azar
> evita que los treinta grupos reintenten en el mismo milisegundo cuando el
> central vuelve: eso se llama *thundering herd*. El tope de 30 segundos se
> aplica dos veces, así que ni el azar lo supera."

### 1:10–1:35 — Recuperación en orden

Cuando el central responde:

```text
[RECUPERACIÓN_INICIO] central=http://localhost:9000
[HISTORIAL_INICIO] modo=incremental desde=0 ...
[HISTORIAL_SINCRONIZADO] ... nuevos=0
[RECUPERACIÓN_OK] lamport=3 pendientes=1
[ENVÍO] id=... intento=1
[ENVÍO_OK] id=... status=200
```

> "El orden importa: primero me registro otra vez, después pido el historial
> que me perdí con `GET /history?since=...`, y solo entonces marco el central
> como en línea y vacío la cola."

```bash
python client/cli.py status
# -> EN LÍNEA, pendientes 0
```

### 1:35–2:00 — Idempotencia y cierre

> "Si se pierde la respuesta del central después de que guardó el mensaje, el
> grupo reintenta con el mismo UUID. El central lo reconoce como duplicado y
> responde 200 sin guardarlo otra vez. Por eso el reintento no produce
> mensajes repetidos en pantalla."

Cierre y puente:

> "Eso es entrega al menos una vez más deduplicación. El orden de los mensajes
> lo garantiza el reloj de Lamport, y eso lo explica Nataly."

---

## 2. Demo de respaldo (30 s): 5xx y 4xx

Solo si sobra tiempo o si el profesor pide ver el descarte. Usar **otra base
nueva** y otro puerto para no mezclar con la demo anterior.

```bash
rm -f /tmp/demo-reintentos.db*
python scripts/mock_central.py --port 9100 --fail-first 2 --reject-after 1
```

```bash
GROUP_NAME=g3 PUBLIC_URL=http://localhost:8111 \
CENTRAL_URL=http://localhost:9100 PORT=8111 \
DB_PATH=/tmp/demo-reintentos.db uvicorn app.main:app --port 8111
```

Se envían dos mensajes y se ve:

```text
[ENVÍO] ... intento=1
[REINTENTO] ... intento_fallido=1 espera=1.047 motivo=HTTP 500
[ENVÍO] ... intento=2
[REINTENTO] ... intento_fallido=2 espera=2.566 motivo=HTTP 500
[ENVÍO] ... intento=3
[ENVÍO_OK] ... status=200
[DESCARTE_4XX] ... status=400
```

La evidencia completa está en [`PRUEBA_REINTENTOS.md`](PRUEBA_REINTENTOS.md).

---

## 3. Preguntas probables del profesor (respuestas cortas)

**¿Por qué el `id` se genera en el origen y no en el central?**
Porque el emisor necesita reintentar sin cambiar la identidad del mensaje. Si
el central lo asignara, un reintento podría crear un mensaje nuevo.

**¿Qué pasa si se pierde la respuesta del central después de guardar?**
El emisor reintenta con el mismo `id`; el central lo reconoce y responde 200
sin duplicar. Eso es idempotencia.

**¿Por qué la cola está en SQLite y no en memoria?**
Porque debe sobrevivir a un reinicio del contenedor, que es justo el escenario
que evalúa el proyecto.

**¿Por qué azar y no solo backoff exponencial?**
Sin azar, todos los grupos reintentan sincronizados y golpean al central al
mismo tiempo cuando vuelve.

**¿Por qué el latido es *pull* y no *push*?**
Porque el grupo no necesita exponer un endpoint extra y detecta la caída por
sí mismo; funciona aunque el central se reinicie.

**¿Por qué `sent_at` no se usa para ordenar?**
Porque los relojes físicos de las máquinas están desfasados. El orden lo da
Lamport.

**¿Qué pasa si el central está caído cuando arranca el grupo?**
Arranca igual, sirve su API local, guarda en `outbox` y sigue consultando
`/health`. Cuando el central vuelve, se registra, sincroniza y vacía la cola.

**¿Qué pasa si el grupo se cae con mensajes pendientes?**
Están en SQLite. Al reiniciar los encuentra y los reenvía con el mismo UUID.

**¿Por qué hay dos `min()` en el cálculo de la espera?**
El primero limita el valor exponencial; el segundo limita el resultado
después del azar. Sin el segundo, el +50 % podría superar el tope.

---

## 4. Errores que pueden salir en cámara (y qué decir)

| Síntoma | Causa | Qué hacer |
|---|---|---|
| `CAÍDA_CENTRAL` no aparece | Se enviaron los mensajes antes de los 15 s | Esperar 20 s y volver a mostrar `/status` |
| La demo de 5xx no falla | La base tenía cola pendiente y consumió los fallos | Borrar la base y reiniciar el servidor |
| `[REINTENTO] operacion=POST /register` al arrancar | El central tardó en levantar | Ignorarlo; es el registro reintentando |
| Log en `13:35` y mensaje en `18:35` | Log en hora local, `sent_at` en UTC | "El log es hora local; `sent_at` va en UTC" |
| `central_online: false` al inicio | El primer latido aún no ha pasado | Esperar 5 s |
