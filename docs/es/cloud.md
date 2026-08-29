# Secretos, funciones y notificaciones

Tres cosas que un servicio necesita apenas sale de tu laptop: configuración que
no puede vivir en un archivo, una forma de correr algo sin servidor, y una
forma de llegar a un teléfono.

## Secretos

Las variables de entorno son la interfaz correcta. Los settings de cada plugin
ya las leen, y también cada lenguaje dentro de un workspace. Lo que suele estar
mal es cómo llegan ahí: un `.env` copiado a un servidor, o un valor pegado en
una variable de CI que nadie puede rotar.

```python
from jfastframework.secrets import load_secrets
from jfastframework import create_app

load_secrets()          # reads JFAST_SECRETS_*, populates os.environ
app = create_app()
```

```bash
JFAST_SECRETS_PROVIDER=aws
JFAST_SECRETS_NAME=prod/billing
JFAST_SECRETS_REGION=eu-west-1
```

o:

```bash
JFAST_SECRETS_PROVIDER=gcp
JFAST_SECRETS_NAME=billing
JFAST_SECRETS_PROJECT=my-project
```

El secreto guardado es un objeto JSON o líneas `KEY=value`:

```json
{ "JFAST_DB_DSN": "postgresql+asyncpg://...", "JFAST_AUTH_SECRET": "..." }
```

Dos reglas que impone:

- **Un valor que ya existe en el entorno gana.** A un desarrollador que exporta
  `JFAST_DB_DSN` localmente no lo pisa en silencio un secreto de producción, y
  la configuración propia de un contenedor le gana a una copia guardada y
  vieja. Pasa `override=True` para invertirlo, a propósito.
- **No se loguea nada.** La línea de log dice cuántos nombres llegaron y
  cuáles, nunca un valor.

El JSON anidado se rechaza en vez de aplanarse: `{"db": {"host": ...}}` no
tiene una escritura obvia como variable de entorno, e inventar una produce un
nombre que nadie puede predecir. Guárdalo como string JSON si el valor
realmente es estructurado.

Usa `prefix` para tener varios servicios en un mismo secreto sin que lean la
configuración del otro:

```bash
JFAST_SECRETS_PREFIX=BILLING_        # BILLING_DSN in the secret becomes DSN
```

No se le pasan credenciales a ninguno de los dos clientes. Ambos usan la cadena
por defecto de su nube, así que un instance role, un task role o workload
identity funcionan y no hay una llave de larga vida que se pueda filtrar.

Instalación: `pip install jfastframework[aws]` o `jfastframework[gcp]`.

## Funciones

```bash
jfast deploy function billing --target aws --account-id 123456789012
jfast deploy function billing --target gcp --project my-project
```

Esto escribe archivos. No los corre. Lee el script antes de hacerlo — son los
únicos artefactos generados que gastan dinero.

Los dos targets corren **la misma app ASGI** que corre el contenedor. Una
segunda copia de la aplicación que existe solo en la nube es una segunda copia
que se desvía.

**AWS Lambda** recibe `handler.py` (Mangum), `Dockerfile.lambda` y
`deploy-lambda.sh`. Imagen de contenedor, no un zip: el set de dependencias de
aquí — el core compilado de Pydantic, muchas veces asyncpg — convierte el
límite de 250 MB descomprimidos en una sorpresa recurrente, y el camino de la
imagen no tiene ese problema. El script fija `--platform linux/amd64`, porque
un build ARM sobre el runtime x86 de Lambda falla como timeout y no como error,
que es una hora genuinamente miserable.

**Google** recibe `deploy-cloudrun.sh`. "Cloud Functions gen2" *es* Cloud Run;
desplegar el contenedor directo se salta el shim de functions-framework, cuyo
único trabajo es hacer que un handler WSGI parezca un contenedor — que es lo
que una app ASGI ya es.

Los dos son **privados** por defecto. `--public` lo habilita e imprime una
advertencia, porque las dos nubes hacen de público la opción fácil y el error
es silencioso.

### Cuándo no usar esto

Los cold starts son el trade-off. Una app JFast con el plugin de base de datos
abre un pool de conexiones al arrancar, y Lambda corre el arranque en cada
contenedor frío. Para un servicio al que se llama todo el tiempo, un contenedor
detrás de Caddy sale más barato *y* más rápido.

Las funciones ganan en trabajo con picos, de bajo volumen o dirigido por
eventos: un receptor de webhooks, un job nocturno, un generador de miniaturas.

## Notificaciones

Push, sobre Firebase Cloud Messaging.

```toml
[plugin.notifications]
backend = "fcm"          # fcm | console
project_id = "my-project"
```

```python
from jfastframework.plugins.builtin.notifications import Notification

notifications = request.app.state.jfast.require("notifications")
await notifications.send(Notification(
    title="Invoice paid",
    body="INV-1042 was paid.",
    tokens=[device_token],
    data={"invoice_id": "1042"},
))
```

El backend por defecto es `console`: loguea en vez de enviar. Sin proyecto de
Firebase, sin credenciales, y el payload queda visible — que es lo que quieres
en desarrollo y en tests. En producción avisa que no se está entregando nada.

La API HTTP v1 de FCM, no la vieja de server key: esa llave no se puede acotar
ni rotar sin romper a todos los clientes, y Google la viene retirando. Las
credenciales salen de Application Default Credentials salvo que definas
`credentials_json`; en GKE o Cloud Run eso significa workload identity y ningún
archivo de llave en ningún lado.

**Envía desde un worker, no desde un handler de request.** Un push que falla lo
debe reintentar la cola, no hacer fallar el request HTTP que lo disparó.
Habilita el plugin `queue` y encola un job; el plugin loguea un recordatorio
cuando no ve una cola.

`SendResult.invalid_tokens` tiene los tokens que FCM reportó como no
registrados — se desinstaló la app, o el token rotó. Bórralos de tu base de
datos. Reintentarlos para siempre es la forma en que un backlog de push crece
sin límite.

Instalación: `pip install jfastframework[fcm]`.

**No verificado de punta a punta.** La construcción del payload y el batching
están testeados; no se envió nada a un proyecto FCM real en CI. Trata tu primer
envío como el test.

## Ver también

- [Despliegue](deploy.md) — el camino del contenedor
- [Colas y eventos](queues-and-events.md) — dónde corresponde un envío
