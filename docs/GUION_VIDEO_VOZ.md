# Guion de voz — presentación del chat distribuido

**Video:** `docs/presentacion.mp4` · 2:59 min · 1920×1080 · 30 fps · pista de audio silenciosa.
**Cómo se generó:** `visualizacion.html?video=1` grabado a 1× desde Chromium headless.
**Para qué sirve:** el video no trae voz. Graba esta narración encima y ajusta los tiempos
a tu ritmo. Los tiempos son aproximados; guíate también por el número de paso que aparece
arriba a la derecha.

> Los bloques de código que se ven son fragmentos reales de
> `app/main.py`, `app/central_client.py`, `app/lamport.py`, `client/cli.py` y
> `scripts/mock_central.py`. No es una simulación: el estado, los relojes y las colas
> siguen el flujo real del proyecto.

## Guion por tiempos

| Tiempo | Paso | Qué decir |
|---|---|---|
| 0:00–0:05 | Intro | Este es nuestro chat distribuido: dos grupos, **g3** y **g3b**, coordinados por un central. Vamos a seguir un mensaje real, paso a paso, y luego veremos qué pasa si el central falla. |
| 0:05–0:11 | 0 | El central de prueba escucha en el puerto 9000. Guarda la lista de servidores y el historial global; no conoce usuarios ni relojes. |
| 0:11–0:17 | 1 | **g3** arranca, crea su base SQLite y su reloj de Lamport, y lanza tres tareas en paralelo: registro, latido y cola de salida. |
| 0:17–0:23 | 2 | Se registra con `POST /register` y repite el registro cada pocos segundos, porque el central puede reiniciarse y olvidar la lista. |
| 0:23–0:30 | 3 | Antes de enviar, pide el historial con `GET /history?since=0`. Solo cuando termina marca `en_linea = true`; esa es la condición para enviar. |
| 0:30–0:35 | 4 | **g3b** hace lo mismo. Ahora el central conoce dos destinos y puede reenviar entre ellos. La topología es en estrella. |
| 0:35–0:41 | 5 | **ana** escribe *“hola desde g3”* en el chat de consola. El cliente hace `POST /messages` al servidor local; todavía no hay nada en la red federada. |
| 0:41–0:46 | 6 | **g3** valida que **ana** exista en su base local. Si no existe, responde 404. El central no participa: cada grupo maneja sus usuarios. |
| 0:46–0:52 | 7 | Regla de emisión: `L = L + 1`. El reloj pasa de 0 a 1 y se guarda en SQLite antes de continuar. El mensaje nace con **L=1**. |
| 0:52–0:53 | 8 | **g3** guarda el mensaje con un UUID generado en el origen y lo encola en la *outbox*. La respuesta al cliente no espera al central. |
| 0:53–0:57 | Demo | Los controles están sincronizados: pauso, retrocedo al paso 7, avanzo al 8 y al 9, y reanudo. El código y el estado se actualizan en cada paso. |
| 0:57–1:02 | 9 | **g3** responde **201** con L=1. El cliente muestra *Enviado*. La entrega real al central ocurre en segundo plano. |
| 1:02–1:07 | 10 | La cola revisa SQLite cada medio segundo. Encuentra el mensaje pendiente y comprueba que el central siga en línea. |
| 1:07–1:13 | 11 | Se envía el JSON del protocolo con el mismo UUID. El id no cambia en los reintentos: es la clave para deduplicar. |
| 1:13–1:20 | 12 | El central comprueba el UUID. Es nuevo, lo guarda y responde *accepted*. Si ya existiera, respondería *duplicate* sin duplicar. |
| 1:20–1:26 | 13 | El central reenvía el mensaje a los demás grupos, en segundo plano, para no bloquear al emisor. |
| 1:26–1:32 | 14 | **g3b** lo recibe en `/fed/messages`. Si el UUID es nuevo, lo inserta; si ya estaba, responde *duplicate* y no toca el reloj. |
| 1:32–1:38 | 15 | Regla de recepción: `L = max(L, L_recibido) + 1 = max(0, 1) + 1 = 2`. El reloj salta para quedar por delante del mensaje recibido. |
| 1:38–1:44 | 16 | El central responde **200**. Solo ahora **g3** borra el mensaje de la cola. Si el proceso muere antes, al reiniciar lo reenvía con el mismo id. |
| 1:44–1:50 | 17 | **ana** escribe *“Holaaaa”*. **g3** avanza de 1 a 2 y encola el nuevo mensaje. El primero ya fue confirmado, así que la cola vuelve a 1. |
| 1:50–1:57 | 18 | El central guarda L=2 y lo reenvía a **g3b**. **g3b** observa 2 y su reloj pasa a 3. Ese **L=3 no es un mensaje**: es un evento interno. Por eso el historial salta de 2 a 4. |
| 1:57–2:03 | 19 | **beto** responde desde **g3b**. **g3b** avanza de 3 a 4 y encola la respuesta. Por eso el mensaje sale con **L=4**, como en la captura de Daniela. |
| 2:03–2:11 | 20 | El central guarda L=4 y lo reenvía a **g3**. **g3** observa 4 y su reloj pasa a 5: `max(2, 4) + 1`. La respuesta de **beto** ya aparece en el chat de **ana**. El hueco L=5 también es interno. |
| 2:11–2:18 | 21 | Si el central reintenta el mismo mensaje, **g3b** ve el mismo UUID, responde *duplicate* y el reloj no cambia. Eso es **idempotencia**. |
| 2:18–2:25 | 22 | Si el central responde **500**, el mensaje no se pierde: **g3** lo reprograma con espera exponencial y *jitter*, y reintenta con el mismo UUID. |
| 2:25–2:32 | 23 | Si el central se cae, tres latidos seguidos lo detectan. **g3** deja de enviar, pero sigue guardando en SQLite. La cola es persistente: no se pierde nada. |
| 2:32–2:41 | 24 | Cuando el central vuelve, **g3** renueva el registro y pide el historial desde la marca anterior. Solo entonces `en_linea = true` y la cola drena los pendientes. |
| 2:41–2:50 | 25 | Resumen: Lamport ordena los eventos; el UUID evita duplicados; la *outbox* con *backoff* garantiza entrega al menos una vez; y el historial repara lo que se perdió. Cuatro mensajes, dos servidores y la cola vacía. |
| 2:50–2:59 | Outro | Tres ideas para recordar: Lamport ordena; el UUID nace en el origen y no cambia; la *outbox* persistente y el historial permiten seguir escribiendo si el central cae. Gracias. |

## Versión seguida (para leer de corrido)

> Unas **490 palabras**: a 160 palabras por minuto dura aproximadamente 3:00.
> Si hablas más rápido, puedes dejar las pausas; si hablas más lento, recorta
> primero los pasos 13 y 21.

Este es nuestro chat distribuido: dos grupos, g3 y g3b, coordinados por un central.
Vamos a seguir un mensaje real, paso a paso, y luego veremos qué pasa si el central falla.

El central escucha en el puerto 9000 y guarda la lista de servidores y el historial
global. g3 arranca, crea su base SQLite y su reloj de Lamport, y lanza tres tareas:
registro, latido y cola de salida. Se registra con `POST /register` y repite el registro
cada pocos segundos, porque el central puede reiniciarse y olvidar la lista. Antes de
enviar pide el historial con `GET /history?since=0`; solo entonces marca
`en_linea = true`. g3b hace lo mismo.

ana escribe “hola desde g3”. El cliente hace `POST /messages` al servidor local; g3
valida que ana exista y responde 404 si no. Regla de emisión: `L = L + 1`; el reloj pasa
de 0 a 1 y se guarda en SQLite. g3 guarda el mensaje con un UUID del origen y lo encola
en la outbox; la respuesta al cliente no espera al central.

Los controles están sincronizados: pauso, retrocedo al paso 7, avanzo al 8 y al 9, y
reanudo. El código y el estado se actualizan en cada paso.

g3 responde 201. La cola revisa SQLite cada medio segundo, ve el pendiente y envía el
JSON con el mismo UUID. El central comprueba el UUID: si es nuevo, lo guarda y responde
“accepted”; si ya existe, “duplicate”. Luego lo reenvía a los demás grupos. g3b lo recibe
en `/fed/messages`, inserta si el UUID es nuevo y aplica
`L = max(L, L_recibido) + 1 = 2`. El central responde 200 y g3 borra el mensaje de la cola.

ana escribe “Holaaaa”: g3 avanza a L=2 y encola. El central guarda L=2 y lo reenvía a
g3b; g3b observa 2 y su reloj pasa a 3. Ese L=3 es interno, no un mensaje; por eso el
historial salta de 2 a 4.

beto responde desde g3b: g3b avanza a L=4 y encola. El central lo reenvía a g3; g3 observa
4 y su reloj pasa a 5. La respuesta ya aparece en el chat de ana; el hueco L=5 también es
interno.

Si el central reintenta el mismo mensaje, g3b responde “duplicate” y el reloj no cambia.
Si responde 500, g3 reintenta con espera exponencial y jitter, con el mismo UUID.

Si el central se cae, tres latidos lo detectan; g3 deja de enviar pero sigue guardando en
SQLite. Al volver, renueva el registro, pide el historial desde la marca anterior y drena
la cola.

Resumen: Lamport ordena, el UUID evita duplicados, la outbox con backoff garantiza entrega
al menos una vez y el historial repara lo perdido. Tres ideas: Lamport ordena; el UUID
nace en el origen y no cambia; la outbox y el historial permiten seguir si el central cae.
Gracias.

## Notas de grabación

- El video ya tiene una pista AAC en silencio; puedes grabar la voz en otra pista y
  mezclar, o reemplazarla.
- Si quieres recortar la intro o el outro, los cortes naturales son **0:05** (entra el
  paso 0) y **2:50** (entra el outro).
- La demostración de controles está entre **0:53** y **0:57**; no hace falta narrar cada
  clic, basta con decir que la navegación mantiene sincronizados el código y el estado.
- Para volver a generar el video:
  ```bash
  # 1) abrir Chromium headless con la página
  chromium --headless=new --no-sandbox --disable-gpu --hide-scrollbars \
    --window-size=1920,1080 --remote-debugging-port=9222 \
    'file:///.../visualizacion.html?video=1'
  # 2) grabar con Page.startScreencast (ver scripts de apoyo) o grabar la ventana
  ```
- El guion técnico con líneas de código y estados intermedios está en
  `docs/guion_voz_detallado.md` (archivo local, ignorado por git).
