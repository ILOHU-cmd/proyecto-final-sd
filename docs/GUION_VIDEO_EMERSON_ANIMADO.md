# Guion de voz — parte de Emerson

**Video:** `docs/presentacion_emerson.mp4` · **1:46** · 1920×1080 · 30 fps · pista de audio silenciosa.
**Alcance:** solo la parte de Emerson: `app/central_client.py` y `app/config.py`,
con sus pruebas. No repite la API, el CLI ni el reloj de Lamport de los demás módulos.
**Visualización:** `visualizacion_emerson.html` (modo normal, `?video=1` o
`?step=N&still=1`).

> El video es mudo a propósito. Graba esta narración encima. Los tiempos son
> aproximados; también puedes guiarte por el número de paso que aparece arriba.
> El guion técnico con líneas de código y estados está en
> `docs/guion_voz_emerson_detallado.md` (archivo local, ignorado por git).

## Guion por tiempos

| Tiempo | Paso | Qué decir |
|---|---|---|
| 0:00–0:05 | Intro | Esta es la parte de Emerson: el **ClienteCentral**, el módulo que conecta **g3** con el central y lo mantiene vivo aunque haya fallos. |
| 0:05–0:11 | 0 | `ejecutar()` lanza tres tareas en paralelo: **registro**, **latido** y **cola**. Comparten un `httpx.AsyncClient`. |
| 0:11–0:18 | 1 | `cargar_configuracion()` valida URLs y tiempos; si algo está mal, el servidor no arranca. |
| 0:18–0:24 | 2 | Cada 10 segundos hace `POST /register`; repetirlo solo actualiza la dirección. |
| 0:24–0:31 | 3 | Antes de enviar pide `GET /history?since=0`; guarda lo nuevo, deduplica por UUID y pone `en_linea = True`. |
| 0:31–0:37 | 4 | La cola lee SQLite cada medio segundo y envía los pendientes si el central está en línea. |
| 0:37–0:43 | 5 | **2xx**: borra el mensaje de la outbox. Si el proceso muere antes, se reenvía con el mismo UUID. |
| 0:43–0:49 | 6 | **4xx**: el mensaje tiene un error; se descarta y no se reintenta. |
| 0:49–0:56 | 7 | **5xx o red**: incrementa el intento, calcula la espera y guarda la hora del próximo intento en SQLite. |
| 0:56–1:03 | 8 | La espera se duplica: 1, 2, 4, 8… hasta 30 s. El **jitter** de ±50 % evita reintentos simultáneos. |
| 1:03–1:09 | 9 | Cada 5 segundos pregunta `GET /health`; si responde y estaba caído, llama a `_recuperar`. |
| 1:09–1:16 | 10 | Tras 3 fallos, `en_linea = False` y `CAIDA_CENTRAL`; la cola deja de enviar, pero los mensajes siguen en SQLite. |
| 1:16–1:24 | 11 | Al volver, renueva el registro, pide el historial desde la marca anterior y solo entonces `en_linea = True`; después drena la cola. |
| 1:24–1:31 | 12 | El reintento conserva el mismo UUID; si el central ya lo tiene, responde `duplicate` y no inserta dos veces. |
| 1:31–1:38 | 13 | `test_backoff.py`, `test_config.py` y `test_central_client.py` cubren backoff, configuración, registro, latido e historial: **33 pruebas propias**. |
| 1:38–1:44 | 14 | Tres tareas, un estado que coordina, backoff con jitter y un UUID que no cambia: si el central cae, no se pierde nada. |
| 1:44–1:46 | Outro | Gracias. |

## Timeline exacta

| Paso | Entra en | Título |
|---|---|---|
| 0 | 0:05.3 | Tres tareas de fondo |
| 1 | 0:11.3 | La configuración se valida al arrancar |
| 2 | 0:17.8 | POST /register cada 10 s |
| 3 | 0:24.3 | Sincronización inicial: GET /history?since=0 |
| 4 | 0:30.9 | La cola persistente despierta |
| 5 | 0:36.9 | 2xx: se borra de la cola |
| 6 | 0:42.9 | 4xx: no se reintenta |
| 7 | 0:49.4 | 5xx o red: se reprograma el reintento |
| 8 | 0:56.4 | Espera exponencial + jitter |
| 9 | 1:03.4 | GET /health cada 5 s |
| 10 | 1:09.4 | 3 fallos seguidos: central caído |
| 11 | 1:16.5 | Registro + historial antes de enviar |
| 12 | 1:24.5 | El mismo UUID en cada reintento |
| 13 | 1:31.0 | 33 pruebas propias |
| 14 | 1:37.5 | Tres tareas, un estado, cero mensajes perdidos |
| Outro | 1:44.5 | Tres ideas para recordar |

## Versión seguida (~280 palabras)

Esta es la parte de Emerson: el ClienteCentral, el módulo que conecta g3 con el central
y lo mantiene vivo aunque haya fallos.

`ejecutar()` lanza tres tareas en paralelo: registro, latido y cola. Comparten un
`httpx.AsyncClient`.

`cargar_configuracion()` valida URLs y tiempos; si algo está mal, el servidor no arranca.

Cada 10 segundos hace `POST /register`; repetirlo solo actualiza la dirección.

Antes de enviar pide `GET /history?since=0`; guarda lo nuevo, deduplica por UUID y pone
`en_linea = True`.

La cola lee SQLite cada medio segundo y envía los pendientes si el central está en línea.

**2xx**: borra el mensaje de la outbox. Si el proceso muere antes, se reenvía con el mismo
UUID.

**4xx**: el mensaje tiene un error; se descarta y no se reintenta.

**5xx o red**: incrementa el intento, calcula la espera y guarda la hora del próximo
intento en SQLite.

La espera se duplica: 1, 2, 4, 8… hasta 30 s. El **jitter** de ±50 % evita reintentos
simultáneos.

Cada 5 segundos pregunta `GET /health`; si responde y estaba caído, llama a `_recuperar`.

Tras 3 fallos, `en_linea = False` y `CAIDA_CENTRAL`; la cola deja de enviar, pero los
mensajes siguen en SQLite.

Al volver, renueva el registro, pide el historial desde la marca anterior y solo entonces
`en_linea = True`; después drena la cola.

El reintento conserva el mismo UUID; si el central ya lo tiene, responde `duplicate` y no
inserta dos veces.

`test_backoff.py`, `test_config.py` y `test_central_client.py` cubren backoff,
configuración, registro, latido, historial y cola: **33 pruebas propias**.

Tres tareas, un estado que coordina, backoff con jitter y un UUID que no cambia: si el
central cae, no se pierde nada.

Gracias.

## Notas

- El video está pensado para el bloque **2:00–4:00** de la sustentación (la parte de
  Emerson). Dura 1:46; quedan unos segundos para la transición.
- La pista de audio está en silencio: puedes grabar la voz en otra pista o reemplazarla.
- Para volver a grabar: abre `visualizacion_emerson.html?video=1` en Chromium y graba
  la ventana, o usa `Page.startScreencast` como en la grabación original.
- El video general del proyecto sigue en `docs/presentacion.mp4` y su guion en
  `docs/GUION_VIDEO_VOZ.md`; este video es el recorte de Emerson.
