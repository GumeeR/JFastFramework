# El contrato de servicio

Un servicio JFast no es "un servicio escrito con JFast". Es un servicio que
cumple este contrato. Esa distinción es toda la razón por la que un servicio
en Go y uno en Python pueden estar detrás del mismo gateway, tomar puertos del
mismo workspace, y ser expuestos por el mismo Caddyfile — nada de lo cual sabe
ni le importa qué lenguaje los produjo.

Mantén esto estable. Los templates son implementación; esto es la interfaz.

---

## 1. Endpoints de sistema

| Endpoint | Debe | No debe |
| --- | --- | --- |
| `GET /health` | Devolver 200 mientras el proceso esté arriba | Sondear ninguna dependencia |
| `GET /ready` | Devolver 200 cuando sirve, 503 cuando una dependencia **crítica** está caída | Usarse como liveness probe |
| `GET /info` | Reportar versión e inventario | Existir cuando `JFAST_ENV=prod` |

`/health` y `/ready` están separados porque confundirlos causa reinicios en
cascada: la base parpadea, el liveness falla, el orquestador mata pods sanos, y
la estampida termina de matar la base.

Que falle una dependencia no crítica hace que `/ready` reporte `"degraded"` con
un 200. Una cache fría no debería sacar a un servicio de rotación.

```json
// GET /health
{"status": "ok", "service": "billing", "version": "0.1.0", "env": "local"}

// GET /ready
{"status": "degraded", "service": "billing",
 "checks": {"cache": {"healthy": false, "detail": "cold", "critical": false}}}
```

## 2. Correlación de requests

Lee `X-Request-ID` del request. **Reúsalo si viene**, genera uno si no.
Devuélvelo en la respuesta, adjúntalo a cada línea de log, y reenvíalo en las
llamadas salientes.

Generar un id nuevo en cada servicio en vez de reusar el del llamante es la
forma más común de que un trace distribuido quede inservible: cada hop arranca
un "trace" nuevo y nada se une.

## 3. Errores

Toda falla se serializa a RFC 7807 `application/problem+json`:

```json
{"type": "about:blank", "title": "Not Found", "status": 404,
 "detail": "invoice 7 not found", "instance": "/invoices/7",
 "request_id": "9f2c…"}
```

Las excepciones no manejadas incluidas. El `detail` de un error no manejado
debe ser genérico salvo que `JFAST_DEBUG=true` — filtrar internos a los
clientes es un hallazgo de information disclosure, y el default tiene que ser
el seguro.

## 4. Configuración

Desde el entorno, con estos nombres:

| Variable | Significado |
| --- | --- |
| `JFAST_APP_NAME` | Nombre del servicio, usado en logs y métricas |
| `JFAST_ENV` | `local` / `dev` / `staging` / `prod` |
| `JFAST_PORT` | Puerto HTTP — la base del bloque del servicio |
| `JFAST_DEBUG` | Si el detalle interno de los errores llega a los clientes |
| `JFAST_LOG_JSON_LOGS` | Logs estructurados encendidos o apagados |

Una sola convención de `.env` cubre todos los lenguajes. No inventes nombres
por lenguaje.

## 5. Puertos

Un servicio es dueño de **diez puertos consecutivos** desde su base. Los
plugins reclaman offsets dentro de ese bloque:

| Offset | Uso |
| --- | --- |
| +0 | HTTP |
| +1 | PostgreSQL |
| +2 | Kafka / PgBouncer |
| +3 | Redis |
| +4 | MongoDB |
| +5 / +6 | Prometheus / Grafana, RabbitMQ |
| +7 / +8 | Qdrant HTTP / gRPC |
| +9 | El gRPC propio del servicio |

El workspace asigna los bloques. No elijas un puerto a mano sin haber revisado
qué hay ya dentro de uno.

## 6. Logs

Un objeto JSON por línea, en stdout, llevando al menos `service`, `env`,
`level`, `message` y — dentro de un request — `request_id`.

No un archivo, no un socket. El runtime recolecta stdout; un servicio que
maneja sus propios archivos de log pelea con lo que ya los esté recolectando.

---

## Lenguajes

| | Python | Go |
| --- | --- | --- |
| Toolchain | `python3` | `go` |
| Implementación del contrato | `jfastframework` (importado) | `internal/jfast/` (vendored, ~300 líneas) |
| Sistema de plugins | sí | no |
| Generador de módulos | sí | un módulo de ejemplo |
| Migraciones | Alembic | las pones tú |
| Kinds | `api`, `web`, `gateway` | `api` |

```bash
jfast new service billing                     # Python
jfast new service edge --language go          # Go
jfast new service edge --language go --grpc   # + the .proto contract
```

Solo necesitas el toolchain de los lenguajes que realmente uses. Un equipo que
trabaja solo en Python nunca instala Go.

### Por qué Go hace vendoring del contrato en vez de importar un módulo compartido

Con dos servicios, `internal/jfast/` son 300 líneas que lees de una sentada.
Con diez, extráelo a su propio módulo de Go e impórtalo. Extraerlo el día uno
te compra un problema de versionado antes de que haya algo que versionar.

### Dónde Go se gana el sueldo

Un hot path, un handler de conexiones de larga vida, un binario que quieres
que pese 12MB y arranque en 5ms. No "porque es más rápido" — el sistema de
plugins, las migraciones y el generador de módulos valen más que los
milisegundos en la mayoría de los servicios.

---

## Agregar un lenguaje

1. Implementa las seis secciones de arriba.
2. Agrega un `LanguageSpec` a `jfastframework/languages.py`.
3. Agrega un árbol de templates `service_<lang>/`.
4. Agrega un job de CI que **buildee y corra** el servicio generado. Un
   scaffold que nadie corrió es un pasivo que parece un feature.

El paso 4 no es opcional. El soporte de Go en este repo existe porque CI
compila el servicio generado, corre sus tests, arranca el binario y le hace
curl — no porque el template se vea bien.

## Lo que deliberadamente no está en el contrato

- **Una librería cliente compartida.** Los servicios hablan HTTP o gRPC. Un
  cliente compartido es un deploy compartido.
- **Un ORM o formato de serialización común** más allá de JSON en el cable.
- **Un vendor de tracing obligatorio.** `X-Request-ID` es el piso;
  OpenTelemetry es un plugin, no un mandato.
