# Análisis del experimento de caída y recuperación

**Fuente principal:** `experiment/results/experiment_20261116T165113Z.json` y
`experiment/results/experiment_20261116T165113Z.csv`.

**Corrida descartada para resiliencia:**
`experiment/results/experiment_20261115T033152Z.json` se ejecutó con
`--skip-docker` (`skip_docker: true`), solo envió 2 mensajes por repetición y
no provocó caída (`docker_down_seconds: 0.0`). Sirve para validar el script,
pero no como evidencia de tolerancia a fallos.

## 1. Pregunta

¿Cuántos mensajes se pierden o se duplican cuando el servidor de un grupo se
cae durante unos segundos mientras otro grupo sigue enviando?

## 2. Montaje real

- Emisor: `g3b` en `http://localhost:8002`.
- Receptor sometido a caída: `g3` en `http://localhost:8001`.
- Central de prueba: `central` del `docker-compose.yml`.
- Carga: 40 mensajes por repetición a 2 mensajes por segundo.
- Caída configurada: 5 s, iniciada a los 5 s.
- Repeticiones: 3.
- Modo: `docker compose stop` / `docker compose start` (`skip_docker: false`).

## 3. Resultados reales

| Repetición | Enviados | En caída | Perdidos | Duplicados | Recuperación (s) | Caída efectiva (s) | Drenado | Recuperado |
|---|---:|---:|---:|---:|---:|---:|---|---|
| 1 | 40 | 13 | 7 | 0 | 1.563 | 6.547 | sí | no |
| 2 | 40 | 14 | 0 | 0 | 1.547 | 6.578 | sí | sí |
| 3 | 40 | 13 | 0 | 0 | 2.313 | 6.547 | sí | sí |
| **Total** | **120** | **40** | **7** | **0** | **1.808** | — | — | **2/3** |

- Total de mensajes enviados: **120**.
- Total de mensajes perdidos: **7**.
- Total de mensajes duplicados: **0**.
- Porcentaje de pérdida: **5.8 %** (7/120).
- Tiempo medio de recuperación reportado: **1.808 s**.

Los valores de recuperación son los que reporta `run_experiment.py` al medir
la recuperación del rezago. La columna `Recuperado` es la verificación final
del script sobre todos los UUID enviados.

## 4. Lectura por repetición

### Repetición 1

Se enviaron 40 mensajes; 13 ocurrieron durante la caída. El historial final de
`g3` no contenía 7 de los UUID enviados y el script marcó `recovered: false`.
La caída efectiva fue de 6.547 s, superior a los 5 s configurados. No hubo
errores de envío (`send_errors: 0`) ni duplicados (`duplicates: 0`).

**Explicación compatible con la evidencia:** la recuperación de la ventana de
caída fue incompleta. El receptor volvió y el emisor drenó su cola, pero la
verificación final no encontró esos 7 mensajes en `g3`. El JSON no conserva
la marca `last_history_lamport` ni las trazas de los reintentos de `/history`,
así que no es posible aislar una causa única con los datos guardados. No se
debe afirmar que la causa fue una sola: la evidencia permite afirmar la
pérdida y su carácter de recuperación, no la causa raíz exacta.

### Repetición 2

Se enviaron 40 mensajes; 14 durante la caída. El receptor recuperó todos los
UUID, no hubo duplicados y el tiempo de recuperación fue de 1.547 s. La cola
quedó drenada y `recovered: true`.

### Repetición 3

Se enviaron 40 mensajes; 13 durante la caída. El receptor recuperó todos los
UUID, no hubo duplicados y el tiempo de recuperación fue de 2.313 s. La cola
quedó drenada y `recovered: true`.

## 5. Pruebas automatizadas

La suite actual tiene **53 pruebas aprobadas** con
`python -m unittest discover -s tests -v`:

| Bloque | Archivo | Pruebas |
|---|---|---:|
| API y endpoints | `tests/test_api.py` | 8 |
| Backoff y jitter | `tests/test_backoff.py` | 5 |
| Cliente central | `tests/test_central_client.py` | 19 |
| CLI y experimento | `tests/test_cli.py` | 5 |
| Configuración | `tests/test_config.py` | 9 |
| Almacenamiento y Lamport | `tests/test_storage.py` | 7 |
| **Total** |  | **53** |

También se verificó:

- Dos respuestas `500`, luego `200`; tres intentos con el mismo UUID y esperas
  de 1.047 s y 2.566 s; `pending_messages = 0`.
- Un `400` se descarta con `DESCARTE_4XX`, sin reintento.
- El heartbeat marca caída tras 3 fallos consecutivos y recupera el historial
  antes de volver a `en_linea = true`.

## 6. Conclusión del análisis

La hipótesis se cumple parcialmente. La deduplicación fue efectiva en las tres
repeticiones y no hubo pérdidas en dos de ellas. La primera repetición perdió
7 mensajes, por lo que no se puede presentar el resultado global como “cero
pérdidas”. La brecha está en la recuperación de la ventana de caída, no en la
idempotencia ni en el orden lógico.

Para una versión futura se debe instrumentar la marca de historial, la ventana
de recuperación y los reintentos de `/history`, y repetir el experimento con
más repeticiones para determinar si la pérdida es esporádica o sistemática.
