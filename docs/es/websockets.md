# WebSockets

Un mensaje se dirige a una **persona**, no a un socket. Qué worker tiene abierta
la conexión de esa persona no es algo que tu código deba saber.

```toml
[plugins]
enabled = ["observability", "cache", "auth", "websocket"]

[plugin.websocket]
channel = "websocket.fanout"
send_buffer = 32
heartbeat_seconds = 20
idle_timeout_seconds = 60
```

Declara un endpoint:

```python
from jfastframework.plugins.builtin.websocket import Connection, socket

@socket("/ws/notifications", scopes=("notifications:read",))
async def notifications(conn: Connection) -> None:
    await conn.join("orders")
    await conn.listen()
```

Envía a una persona, desde cualquier ruta, en cualquier worker:

```python
hub = ctx.require("websockets")
await hub.send("user-42", {"kind": "invoice.paid", "id": 7})
await hub.send_to_room("orders", {"kind": "order.moved"}, tenant_id="acme")
```

Apagado por defecto. `requires = ("auth", "cache")` lo impone el kernel, no lo
sugiere esta página: el handshake verifica un token, y Redis es el backplane que
lleva un mensaje al worker que tiene el socket del destinatario. Sin eso, esto
es un juguete monoproceso que aparenta escalar.

## Cómo cruza un mensaje entre workers

Cada worker se suscribe a **un** canal y entrega solo a los sockets que él
mismo tiene. Un publish nunca entrega localmente; la entrega local ocurre
únicamente a través de la suscripción.

```
POST /invoices        worker A ──publish──▶ Redis ──▶ worker A  (no tiene a nadie)
   (en worker A)                              │
                                              └──▶ worker B  (tiene user-42) ──▶ socket
```

Esa única regla es lo que lo hace **exactamente una vez por socket**. Entregar
localmente *y* por el backplane le daría a cada socket del worker que publica
dos copias del mismo mensaje: ese es el bug que este diseño existe para evitar, y
el que la suite de tests comprueba.

Como ningún worker es especial, **el balanceador no necesita sticky sessions**.
Cualquier worker puede aceptar cualquier conexión.

El costo de un solo canal es que todos los workers ven todos los mensajes y
descartan la mayoría. Canales de Redis por sujeto reducirían ese tráfico, a
cambio de un `SUBSCRIBE` y un `UNSUBSCRIBE` por conexión. Ese intercambio vale
la pena revisarlo por encima de unos miles de mensajes por segundo, no antes.

Un sujeto con dos sockets —dos pestañas del navegador— recibe una copia **por
socket**. La entrega única es por conexión, no por persona.

## El handshake

Un navegador no puede poner una cabecera `Authorization` en un WebSocket. Las
dos respuestas habituales son peores de lo que parecen:

| Vía | Lo que cuesta |
| --- | --- |
| Token en el query string | Queda escrito en cada access log, traza de proxy e historial del navegador por los que pasa, y no se puede rotar fuera de ninguno de ellos. |
| Token en el primer mensaje | El socket existe, y ocupa un descriptor de archivo, antes de que nadie haya probado quién es. |

**Este plugin lee el token de `Sec-WebSocket-Protocol`, y nunca del query
string.** Un navegador sí puede ofrecer subprotocolos, así que el token viaja en
una cabecera:

```javascript
const ws = new WebSocket("wss://api.example.com/ws/notifications", [
  "jfast.auth.bearer",
  accessToken,
]);
```

El servidor verifica la segunda entrada y devuelve `jfast.auth.bearer` — un
navegador aborta la conexión si el servidor selecciona un subprotocolo que él no
ofreció.

Los clientes que no son navegadores —un worker, una app móvil, un servicio en
Go— sí pueden poner una cabecera real, así que `Authorization: Bearer …` se lee
primero y tiene preferencia.

La verificación pasa por el `verify_token` del plugin `auth`, así que la
rotación de JWKS, el issuer, el audience, el pinning de algoritmo y la
revocación aplican igual que en una petición HTTP. Un refresh token se rechaza:
verifica como cualquier otro, y sin esa comprobación un token de 30 días abriría
un socket donde lo abriría un access token.

**El caso que cubre el primer mensaje** es un cliente que no controla ninguna de
las dos cabeceras. Está disponible, y apagado por defecto:

```toml
[plugin.websocket]
allow_first_message_auth = true
auth_deadline_seconds = 5
```

Con eso encendido, el socket se acepta y el servidor espera
`{"type": "auth", "token": "…"}`. Hasta que eso llega, la conexión no está en el
registro, no recibe nada, y se cierra cuando vence el plazo. El plazo es lo que
acota la ventana sin autenticar; esa ventana es la razón de que no sea el
comportamiento por defecto.

El rechazo ocurre **antes** de `accept()`, así que el handshake HTTP se responde
con una negativa y nunca se llega a promover ningún socket.

## Backpressure: a un cliente lento se le desconecta, no se le acumula

Cada conexión tiene una cola de salida acotada que drena una tarea escritora.
Los envíos nunca tocan el socket directamente: si no, quien publica quedaría
esperando una escritura TCP al cliente más lento del fan-out.

Cuando esa cola se llena hay tres respuestas posibles y solo un valor por
defecto:

| Política | Comportamiento | Cuándo |
| --- | --- | --- |
| `close` *(por defecto)* | Cierra con **1013 Try Again Later**. El cliente reconecta y vuelve a leer su estado. | Cualquier cosa donde un mensaje faltante cambie lo que el usuario ve. |
| `drop_oldest` | Conserva los frames más nuevos, descarta los viejos, y los cuenta en `conn.dropped`. | Un feed de telemetría donde solo importa el último valor. |
| sin límite | No se ofrece. | — |

Un buffer sin límite es la forma en que un cliente lento tumba un worker: la
memoria crece hasta que el proceso muere, y muere sirviendo a todos. Descartar en
silencio es peor de otra manera: el estado del cliente se separa del servidor sin
ninguna señal de que pasó. Desconectar es ruidoso, y el cliente ya tiene que
saber reconectar.

`send_buffer = 32` es aproximadamente un segundo de un feed hablador. Subirlo
sube la memoria que un solo cliente atascado puede retener.

## Heartbeats

Una conexión TCP caída no es observable desde la capa ASGI: el socket parece
escribible mientras el buffer del kernel acepte escrituras. **Solo el tráfico
entrante prueba que el par sigue vivo**, y por eso el timeout está en la
recepción y no en el envío.

- El servidor manda `{"type": "ping"}` cada `heartbeat_seconds` (20 por
  defecto).
- Una conexión **sin ningún frame entrante** durante `idle_timeout_seconds`
  (60 por defecto) se cierra con **1001**. Eso son dos pongs perdidos antes de
  que pase nada.
- Cualquier frame entrante cuenta como señal de vida, no solo un pong. A un
  cliente hablador nunca se le desconecta por no implementarlo.

Los clientes deberían responder:

```javascript
ws.onmessage = (event) => {
  const frame = JSON.parse(event.data);
  if (frame.type === "ping") return ws.send(JSON.stringify({ type: "pong" }));
  handle(frame.data);
};
```

Un pong nunca llega a tu handler: es contabilidad del transporte, y un handler
que tiene que filtrarlo es un handler que se va a olvidar de hacerlo.

Los 20 segundos están deliberadamente por debajo del timeout de inactividad de
los proxies que se ponen delante: el `proxy_read_timeout` de nginx y un ALB de
AWS traen ambos 60 por defecto. Pon `idle_timeout_seconds = 0` para desactivar
el timeout de la capa de aplicación y confiar en los pings a nivel de protocolo
que uvicorn ya manda (`--ws-ping-interval`), que los navegadores responden sin
una línea de código de aplicación.

## Expiración del token con la conexión abierta

**Un socket no sobrevive al token que lo abrió.** Cuando pasa el `exp` del
access token, la conexión se cierra con **1008**, y el cliente refresca y
reconecta.

La alternativa —dejarlo abierto— significa que un socket abierto con un token de
15 minutos sigue transmitiendo ocho horas después bajo una autorización que nadie
volvió a comprobar, y que un logout o una revocación nunca podrían alcanzarlo.
Reconectar cuesta un round trip; la otra opción cuesta el significado del tiempo
de vida del token.

```toml
[plugin.websocket]
close_on_token_expiry = false   # only if you know why
```

Un token sin `exp` nunca caduca por antigüedad, porque no hay nada que caducar.

## Reconexión: los mensajes enviados mientras estabas desconectado se pierden

Dicho sin rodeos, porque el transporte no admite lo contrario:

- Redis pub/sub no retiene nada. Un suscriptor que no está conectado cuando se
  publica un mensaje no lo ve nunca.
- Aquí no hay ningún buffer por cliente.
- No hay reintento, no hay acuse de recibo, y **no hay garantía de orden a través
  de una reconexión**.

Es decir: un mensaje publicado mientras el cliente estaba entre dos sockets
**se perdió**. Diseña contando con eso: haz que el cliente vuelva a leer su
estado al conectar, en vez de asumir que el stream es un historial completo:

```python
@socket("/ws/notifications")
async def notifications(conn: Connection) -> None:
    await conn.send({"kind": "resync", "unread": await unread_for(conn.subject)})
    await conn.listen()
```

Lo que no se puede perder va al plugin `queue`, que tiene reintentos y
dead-letter, no aquí.

## Multi-tenancy

El registro se indexa por `(tenant_id, subject)`, y el tenant sale del **token
verificado**, nunca del cliente. Dos consecuencias:

- El nombre de una sala está acotado al tenant que la abrió. `orders` en `acme` y
  `orders` en `globex` son salas distintas, así que un cliente no puede
  suscribirse a otro tenant adivinando un nombre.
- `hub.send("alice", …)` sin tenant llega solo a las conexiones cuyo token no
  traía ninguno. En un servicio multi-tenant el tenant es parte de la dirección:

```python
await hub.send("alice", {"kind": "invoice.paid"}, tenant_id="acme")
```

## El registro de conexiones

`conn.join(room)` y `conn.leave(room)` gestionan la pertenencia a salas; todo lo
demás es automático. Una conexión se quita del índice de sujetos **y de cada
sala a la que entró** en un `finally`, así que una desconexión abrupta —1006, sin
frame de cierre— no deja nada detrás. Quitarla solo del índice de sujetos es la
fuga que en producción se ve como memoria que crece con la rotación de
conexiones.

`GET /health` reporta el conteo vivo:

```json
{ "detail": "3 connection(s)", "connections": 3, "subjects": 2, "rooms": 1 }
```

## Qué recibe el cliente

Dos formas de frame, ambas objetos JSON:

```json
{ "type": "message", "room": null, "data": { "kind": "invoice.paid", "id": 7 } }
{ "type": "ping" }
```

`room` es el nombre de la sala para un mensaje de sala y `null` para uno dirigido
a un sujeto. Tu payload va siempre bajo `data`, así que un payload con su propia
clave `type` no puede chocar con la del transporte.

## Proxies

El upgrade tiene que sobrevivir al camino. La configuración de Caddy generada y
el plugin `gateway` lo dejan pasar; un nginx escrito a mano hay que decírselo:

```nginx
location /ws/ {
    proxy_pass http://app;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 300s;   # or the heartbeat has to be under 60
}
```

En Kubernetes, un Ingress que corta conexiones inactivas a los 60 segundos va a
cortar un WebSocket que por lo demás está sano. O subes el timeout
(`nginx.ingress.kubernetes.io/proxy-read-timeout`) o dejas `heartbeat_seconds`
por debajo de él — los 20 por defecto ya lo están.

## Madurez

`alpha`. Mira [STATUS.md](../../STATUS.md) para saber qué significa eso y qué no
se ha ejecutado.

El test entre workers —dos instancias de la app, un Redis real, un socket en cada
una, entrega única comprobada por número de secuencia— está en
`tests/test_websocket.py` y pasa contra `redis:7-alpine`. Se **salta** si no hay
un Redis alcanzable, y CI hoy no levanta ninguno, así que esto no está probado
por un job de CI. Esa es la puerta de promoción, y no está superada.
