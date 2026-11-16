# Poster - Chat distribuido en estrella

## Problema

Intercambiar mensajes entre grupos aunque exista una caida temporal de un
servidor o del central, sin perder mensajes ni mostrarlos duplicados.

## Solucion

- Cada grupo expone una API local y un CLI.
- Un central registra los grupos y distribuye los mensajes.
- SQLite conserva usuarios, historial, reloj de Lamport y cola de salida.
- Los UUID hacen idempotentes los reintentos.
- El historial se ordena por `(lamport, origin, id)`.

## Recorrido de un mensaje

`CLI -> API local -> SQLite/outbox -> central -> grupos registrados -> deduplicacion`

Si el central no responde, el mensaje permanece en la cola persistente. Al
recuperarse la conexion, se reintenta con el mismo UUID.

## Evidencia

La validacion local produjo 3 repeticiones, 6 mensajes enviados en total, 0
perdidos y 0 duplicados. Los datos completos estan en
`experiment/results/experiment_20261115T033152Z.json` y `.csv`.

| Repeticion | Enviados | Perdidos | Duplicados | Recuperacion |
|---:|---:|---:|---:|---:|
| 1 | 2 | 0 | 0 | 0.00 s |
| 2 | 2 | 0 | 0 | 0.00 s |
| 3 | 2 | 0 | 0 | 0.00 s |

> Esta tabla corresponde a una validacion con `--skip-docker`; debe repetirse
> con caida real usando Docker antes de presentarla como prueba de resiliencia.

## Demo sugerida

1. Levantar el compose con los perfiles `local` y `pair`.
2. Crear un usuario y enviar mensajes desde `g3b`.
3. Ejecutar `docker compose stop chat-g3`.
4. Enviar mensajes durante la caida.
5. Ejecutar `docker compose start chat-g3` y observar la recuperacion sin duplicados.

## Limitaciones

Central unico, sin autenticacion, HTTP sin TLS y SQLite orientado a un proceso.
