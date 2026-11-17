# Bitácora de interoperabilidad

**Fecha de la prueba local:** 2026-11-17
**Alcance:** interoperabilidad local entre dos servidores del grupo (`g3` y
`g3b`) y el central de prueba del repositorio.

> **Importante:** esta bitácora documenta un ensayo local. La sesión oficial de
> interoperabilidad con el central del curso y los servidores de los demás
> grupos todavía debe realizarse y registrarse en este mismo archivo. No se
> presenta como si ya hubiera ocurrido.

## 1. Objetivo

Comprobar que dos servidores del grupo pueden:

1. registrarse en un central;
2. intercambiar mensajes de `#general` a través de él;
3. conservar el orden lógico y la deduplicación;
4. recuperar los mensajes perdidos cuando uno de ellos se cae y vuelve.

## 2. Entorno

- Linux, ejecución desde el anfitrión.
- Python 3.14 con el entorno virtual del proyecto.
- `central` de prueba (`scripts/mock_central.py`) en el puerto 19000.
- `g3` en el puerto 18001, base SQLite temporal.
- `g3b` en el puerto 18002, base SQLite temporal.
- Heartbeat cada 1 s, timeout de 1 s, 2 fallos para marcar caída.
- Backoff base 0.5 s, tope 3 s.

## 3. Procedimiento

1. Se levantaron el central, `g3` y `g3b`.
2. Se crearon los usuarios `ana` en `g3` y `beto` en `g3b`.
3. `g3b` envió 40 mensajes a 2 mensajes por segundo.
4. En el mensaje 10 se detuvo `g3` con `SIGTERM`.
5. En el mensaje 20 se volvió a levantar `g3` con la misma base.
6. Se esperó a que `g3b` drenara su cola y a que `g3` recuperara el historial.
7. Se compararon los UUID enviados con el historial de `g3`.

## 4. Resultado real

Salida registrada en
`docs/evidencia/interoperabilidad_local_20261117.txt`:

```text
Servidores arriba.
Deteniendo g3...
g3 detenido.
Levantando g3...
g3 arriba de nuevo.
Esperando drenaje de g3b...
Esperando recuperación de g3...
{'enviados': 40, 'faltantes': 0, 'duplicados': 0, 'recuperado': True,
 'caida_s': 5.664, 'g3_pendientes': 0, 'g3b_pendientes': 0}
```

| Métrica | Resultado |
|---|---:|
| Mensajes enviados | 40 |
| Mensajes faltantes en `g3` | 0 |
| Mensajes duplicados | 0 |
| Caída efectiva | 5.664 s |
| Recuperación completa | sí |
| Pendientes en `g3` | 0 |
| Pendientes en `g3b` | 0 |

## 5. Qué funcionó

- Registro y renovación del registro de ambos servidores.
- Envío de `g3b` al central y reenvío del central a `g3`.
- Cola `outbox` persistente: `g3b` no perdió mensajes mientras `g3` estaba
  caído.
- Recuperación por historial: `g3` incorporó los mensajes posteriores a su
  reaparición.
- Deduplicación por UUID: el historial no mostró mensajes repetidos.
- Orden lógico por `(lamport, origin, id)`.
- Latido y reintento: el sistema volvió a `en_linea` sin intervención manual.

## 6. Qué se encontró y qué falta

- El ensayo usa el **central de prueba**, no el central oficial del curso.
- Solo participan dos servidores del mismo grupo, no varios grupos de la clase.
- No se ejecutó el script de conformidad oficial; no hay salida oficial
  guardada.
- La corrida experimental con Docker de `experiment_20261116T165113Z` sí es
  real, pero su primera repetición perdió 7 mensajes. Esa pérdida debe
  explicarse en el póster y no ocultarse.
- La sesión oficial de interoperabilidad sigue pendiente. Cuando se realice,
  se debe agregar aquí:
  - fecha y grupos participantes;
  - conversación global observada;
  - caída y recuperación por turnos;
  - incompatibilidades encontradas;
  - correcciones aplicadas.

## 7. Conclusión

La interoperabilidad local entre `g3` y `g3b` funcionó: 40 mensajes, 0
faltantes, 0 duplicados y recuperación completa tras una caída de 5.664 s.
Esta evidencia no reemplaza la sesión oficial con el central del curso y los
demás grupos; es el ensayo previo y la base para completar la bitácora.
