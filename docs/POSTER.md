# Póster científico — Chat distribuido en estrella

**Versión para imprimir:** [`poster_final.pdf`](poster_final.pdf) (A1 vertical).

**Fuente de diseño:** [`poster_final.html`](poster_final.html).

**Título:** Chat distribuido en estrella

**Autores:** Emerson Ballesteros Díaz, Daniela López Pinto, Nataly [apellido pendiente], Mariana [apellido pendiente]

**Asignatura:** 41336 - Sistemas Distribuidos — **Grupo G3**

**Afiliación:** Escuela de Ingeniería de Sistemas e Informática, Universidad Industrial de Santander

> Los apellidos y correos de Nataly, Mariana y del docente no están completos en el repositorio. Deben completarse antes de imprimir.

---

## 1. Pregunta e hipótesis

**Pregunta:** ¿cuántos mensajes se pierden o se duplican cuando el servidor de un grupo se cae durante unos segundos mientras otro grupo sigue enviando?

**Hipótesis:** con una cola de salida persistente, UUID estables, deduplicación y recuperación por historial, no deberían aparecer duplicados y las pérdidas deberían ser nulas o mínimas.

**Objetivo:** implementar el mínimo común del protocolo de chat distribuido y medir pérdidas, duplicados y tiempo de recuperación ante una caída controlada.

---

## 2. Arquitectura

```text
                         +---------------------------+
                         |   Servidor central        |
                         |   POST /register          |
                         |   POST /messages          |
                         |   GET  /history?since=... |
                         |   GET  /health            |
                         +------------+--------------+
                                      |
              +-----------------------+-----------------------+
              |                                               |
      +-------v--------+                              +-------v--------+
      |      g3        |                              |      g3b       |
      |  FastAPI       |                              |  FastAPI       |
      |  SQLite        |                              |  SQLite        |
      |  outbox        |                              |  outbox        |
      |  Lamport       |                              |  Lamport       |
      +-------+--------+                              +-------+--------+
              |                                               |
        cliente CLI                                     cliente CLI
```

Los servidores de grupo **no se hablan directamente**. Todo mensaje pasa por el central. Cada grupo conserva usuarios, historial, reloj de Lamport y cola de salida en SQLite.

**Recorrido de un mensaje:** CLI → API local → SQLite/outbox → central → servidores registrados → deduplicación → historial local.

---

## 3. Método

- **Diseño:** caída controlada con `docker compose stop` y `docker compose start`; no se usa `restart` porque la duración debe ser medible.
- **Nodos:** emisor `g3b`, receptor `g3`, central de prueba.
- **Carga:** 40 mensajes por repetición, a 2 mensajes por segundo.
- **Caída:** 5 s configurados, iniciada a los 5 s.
- **Repeticiones:** 3.
- **Entorno:** Linux, Python 3.14.10, FastAPI 0.162.4, Uvicorn 0.54.0, HTTPX 0.28.1, Pydantic 2.14.3, SQLite y Docker Compose.
- **Protocolo:** `POST /register`, `POST /messages`, `GET /history?since=...`, `GET /health`, `GET /fed/health`, `POST /fed/messages`.
- **Mecanismos:** UUID inmutable, reloj de Lamport, `outbox` persistente, backoff exponencial con jitter, heartbeat y recuperación por historial.

**Medición:** el script registra los UUID enviados y los compara con el historial de `g3`. Calcula enviados, mensajes durante la caída, perdidos, duplicados y tiempo de recuperación. La caída efectiva se mide entre `stop` y `start`.

**Datos:** `experiment/results/experiment_20261116T165113Z.json` y `.csv` (`skip_docker: false`). La corrida `20261115T033152Z` se descartó para resiliencia porque usó `--skip-docker` y no tuvo caída.

---

## 4. Resultados

| Repetición | Enviados | En caída | Perdidos | Duplicados | Recuperación (s) | Caída efectiva (s) | Estado |
|---|---:|---:|---:|---:|---:|---:|---|
| 1 | 40 | 13 | 7 | 0 | 1.563 | 6.547 | Parcial |
| 2 | 40 | 14 | 0 | 0 | 1.547 | 6.578 | OK |
| 3 | 40 | 13 | 0 | 0 | 2.313 | 6.547 | OK |
| **Total** | **120** | **40** | **7** | **0** | **1.808** | — | **2/3 OK** |

**Lectura:** 0 duplicados en las tres repeticiones; 0 pérdidas en las repeticiones 2 y 3; 7 pérdidas en la repetición 1 (5.8 % de 120). **No se puede afirmar “cero pérdidas” como resultado global.**

**Explicación de las 7 pérdidas:** en la repetición 1 se enviaron 13 mensajes durante la caída; 7 UUID no aparecieron en el historial final de `g3` y el script marcó `recovered=false`. La caída efectiva fue 6.547 s, mayor que los 5 s configurados. No hubo errores de envío ni duplicados. La evidencia no conserva `last_history_lamport` ni las trazas de `/history`, por lo que no permite aislar una causa única. El patrón es compatible con una recuperación incompleta de la ventana de caída.

### Pruebas automatizadas

| Bloque | Pruebas |
|---|---:|
| API | 8 |
| Backoff y jitter | 5 |
| Cliente central | 19 |
| CLI y experimento | 5 |
| Configuración | 9 |
| Almacenamiento y Lamport | 7 |
| **Total** | **53** |

**Evidencia de resiliencia:** dos respuestas `500`, luego `200`; 3 intentos con el mismo UUID; esperas de 1.047 s y 2.566 s; `pending_messages=0`. Un `400` se descarta con `DESCARTE_4XX`, sin reintento. El heartbeat marca caída tras 3 fallos consecutivos y recupera el historial antes de volver a `en_linea=true`.

---

## 5. Interoperabilidad

**Ensayo local real `g3` ↔ `g3b`:** 40 mensajes, 0 faltantes, 0 duplicados, caída de 5.664 s y recuperación completa; ambos servidores terminaron con 0 pendientes. Evidencia: `docs/evidencia/interoperabilidad_local_20261117.txt`.

**Qué funcionó:** registro y renovación, reenvío por el central, `outbox`, recuperación por historial, deduplicación por UUID, orden `(lamport, origin, id)` y detección por heartbeat.

**Qué falta:** la sesión oficial con el central del curso y los servidores de los demás grupos. La bitácora base está en `docs/BITACORA_INTEROPERABILIDAD.md`; debe completarse con fecha, grupos participantes, fallas encontradas y correcciones. **No se presenta como una sesión oficial ya realizada.**

---

## 6. Limitaciones y conclusiones

1. **Arquitectura y protocolo:** la estrella con HTTP/JSON, UUID, `outbox` y recuperación por historial es una base coherente y desacoplada.
2. **Hipótesis parcial:** se confirma la ausencia de duplicados, pero no la ausencia total de pérdidas. La primera repetición perdió 7 mensajes.
3. **Tolerancia y orden:** Lamport persistente, orden `(lamport, origin, id)` y deduplicación evitaron duplicados; la brecha está en la recuperación de la ventana de caída.
4. **Reproducibilidad y observabilidad:** Docker Compose, el script, JSON/CSV y 53 pruebas permiten repetir y auditar; falta instrumentar mejor la marca de historial y la ventana de recuperación.

**Limitaciones:** SQLite y un solo proceso por servidor; sin autenticación ni TLS; central único; el central de prueba es en memoria y no reemplaza el central oficial; falta la salida del script de conformidad oficial y la sesión oficial de interoperabilidad.

---

## 7. Trabajo futuro

1. Ejecutar el script de conformidad oficial y guardar su salida.
2. Integrar y validar con el central del curso y varios grupos.
3. Instrumentar `last_history_lamport`, `/history` y la ventana de recuperación para explicar las pérdidas tempranas.
4. Marcar los mensajes descartados por `4xx`, añadir TLS/autenticación y probar en varios equipos.

---

## 8. Contacto

- **Emerson Ballesteros Díaz** — GitHub: `ILOHU-cmd`; correo no registrado.
- **Daniela López Pinto** — `lopezpintodaniela55@gmail.com`.
- **Nataly** — apellido y correo no registrados.
- **Mariana** — apellido y correo no registrados; la cuenta `LauraVargas22` aparece en Git, pero no se confirmó su identidad.
- **Docente de 41336** — nombre y correo no registrados.
- **Repositorio:** https://github.com/ILOHU-cmd/proyecto-final-sd

---

## 9. Referencias

Coulouris, G., Dollimore, J., Kindberg, T., & Blair, G. (2012). *Distributed systems: Concepts and design* (5th ed.). Addison-Wesley.

Docker Inc. (2026). *Docker Compose overview*. https://docs.docker.com/compose/

Encode OSS. (s. f.). *HTTPX documentation*. https://www.python-httpx.org/

Fielding, R., & Reschke, J. (2014). *Hypertext Transfer Protocol (HTTP/1.1): Semantics and content* (RFC 7231). Internet Engineering Task Force. https://doi.org/10.17487/RFC7231

Lamport, L. (1978). Time, clocks, and the ordering of events in a distributed system. *Communications of the ACM, 21*(7), 558–565. https://doi.org/10.1145/359545.359563

Python Software Foundation. (2026). *sqlite3 — DB-API 2.0 interface for SQLite databases*. https://docs.python.org/3/library/sqlite3.html

Ramírez, S. (s. f.). *FastAPI documentation*. https://fastapi.tiangolo.com/

Universidad Industrial de Santander. (2026). *Enunciado y especificación OpenAPI del curso 41336 - Sistemas Distribuidos*. Documento del curso.

Vitillo, R. (2022). *Understanding distributed systems* (2nd ed.). Roberto Vitillo.
