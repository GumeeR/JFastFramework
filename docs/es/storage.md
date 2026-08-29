# Almacenamiento

Los archivos viven en un **disco con nombre**. Tu código escribe en `"public"` o
`"invoices"`; que ese disco sea un directorio, un bucket de S3 o un contenedor
de MinIO es configuración. El mismo handler funciona en desarrollo y en
producción sin un solo cambio.

Esta es la idea de disco de Laravel, y vale la pena copiarla: la alternativa —un
handler que sabe que está escribiendo en `/var/app/uploads`— no se puede
desplegar en ningún otro lado sin reescribirlo.

```bash
jfast new service billing --plugins storage
```

## Los dos discos por defecto

Un servicio nuevo trae dos, y la diferencia entre ellos es todo el punto:

| Disco | Visibilidad | URL | Se usa para |
| --- | --- | --- | --- |
| `public` | `public` | permanente | avatares, logos, cualquier cosa ya pública |
| `private` | `private` | expira | facturas, contratos, exports, uploads |

```toml
[plugin.storage]
default = "public"
serve_local = true                    # development only

[plugin.storage.disks.public]
driver = "local"
root = "storage/public"
visibility = "public"

[plugin.storage.disks.private]
driver = "local"
root = "storage/private"
visibility = "private"
```

Un disco privado **se niega** a producir una URL permanente. No es un chequeo de
comodidad: una URL permanente a un disco privado es exactamente la forma en que
los PDFs de facturas terminan en un índice de búsqueda.

```python
storage = request.app.state.jfast.require("storage")

await storage.disk("public").put("logos/acme.png", data, content_type="image/png")
storage.disk("public").url("logos/acme.png")          # /storage/public/logos/acme.png

await storage.disk("private").put("invoices/1042.pdf", pdf)
storage.disk("private").url("invoices/1042.pdf")      # StorageError
await storage.disk("private").temporary_url("invoices/1042.pdf", expires_in=300)
```

## URLs temporales

Para un disco local el link lleva una expiración y una firma HMAC sobre **las
dos cosas**: la clave y esa expiración:

```
/storage/private/invoices/1042.pdf?expires=1793491200&signature=Yk3f...
```

Firmar solo la clave convertiría un link válido en una llave a todo el disco; el
portador podría editar la ruta. Firmar solo la expiración le dejaría editar la
ruta en su lugar. La firma cubre las dos, y la comparación es de tiempo
constante.

Configura la clave o las URLs temporales no funcionan:

```bash
JFAST_STORAGE_SIGNING_KEY=$(openssl rand -hex 32)
```

El plugin avisa al arrancar cuando un disco local privado no tiene clave, en vez
de dejar que lo descubra el primer usuario que haga clic en un link de descarga.

Un link expirado y uno falsificado devuelven el mismo 403 con el mismo mensaje.
Respuestas distintas le dirían a un atacante si la clave existe.

Para un disco S3, `temporary_url()` es una URL prefirmada y la clave de firma es
irrelevante: S3 hace la firma con sus propias credenciales.

## S3 y MinIO

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "acme-uploads"
region = "eu-west-1"
visibility = "private"
```

Nada de credenciales en la config: la cadena por defecto de boto3 encuentra el
instance role, el task role o `~/.aws/credentials`. Un task role es mejor que
cualquier key que pudieras poner aquí, porque no hay ningún secreto de larga
vida que se pueda filtrar.

MinIO es S3 con dos settings extra:

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "uploads"
endpoint_url = "http://localhost:8006"
access_key = "jfast"
secret_key = "${MINIO_PASSWORD}"
force_path_style = true              # MinIO needs it, real S3 does not
```

Para tener el contenedor en tu archivo de compose:

```toml
[plugin.storage]
minio_include_infra = true
```

`jfast deploy compose` emite entonces MinIO en el puerto base de tu servicio
`+6`, como el contenedor de cualquier otro plugin.

## Las claves no son rutas

Cada backend pasa la clave por el mismo validador antes de que llegue a un
filesystem o a un bucket:

- nada de `..`, sin `/` inicial, sin backslashes, sin null bytes;
- `.` y `..` resueltos *antes* del chequeo, así `a/../../b` se atrapa aquí y no
  en el filesystem;
- solo letras, dígitos, `.`, `_`, `-` y `/`;
- 1024 caracteres como máximo.

Los discos locales además vuelven a chequear después de resolver, porque un
symlink dentro de la raíz del disco puede apuntar afuera y solo la resolución lo
revela.

El path traversal es la vulnerabilidad de almacenamiento más común que existe.
Validar en el backend y no en el plugin significa que un backend usado directo
—desde un worker, desde un script— es tan seguro como uno usado a través de una
ruta.

## Servir archivos

`serve_local = true` monta `/storage/{disk}/{key}` para que las descargas
funcionen sin nada más corriendo. Es para desarrollo.

En producción, pon Caddy o un CDN delante del disco público y configura
`serve_local = false`. Un worker de Python que mantiene una conexión abierta
para transmitir un PDF de 40 MB es un worker que no está sirviendo requests. El
plugin loguea un warning si se encuentra sirviendo archivos en producción.

Las descargas se mandan como `Content-Disposition: attachment` con
`X-Content-Type-Options: nosniff`. Un `.html` o `.svg` subido y renderizado
inline corre el script de quien lo subió en tu origen, contra las cookies de tus
usuarios — así que los uploads se guardan, no se renderizan. Pasa `inline=True`
a `sanitised_download_headers()` solo para archivos que produjo tu propio
código.

## Salud

`/ready` reporta cada disco: un disco local que falta o es de solo lectura, un
bucket de S3 que no se puede alcanzar. Que el almacenamiento falle no vuelve al
servicio unhealthy por sí solo —una API que todavía puede responder consultas
debería seguir en el load balancer— así que se reporta como degradado.

## Lo que esto no hace

- **Sin procesamiento de imágenes.** Miniaturas, redimensionado y conversión de
  formato van en un job, no en una capa de almacenamiento.
- **Sin uploads en streaming.** `put()` recibe bytes. Un upload de varios
  gigabytes debería ir directo a S3 con un `upload_url()` prefirmado y no pasar
  nunca por la aplicación.
- **Sin escaneo de virus.** Si aceptas uploads del público, lo necesitas; aquí
  no está.

## Ver también

- [Multi-tenancy](multitenancy.md) — prefijos por tenant y subdominios
- [Despliegue](deploy.md) — Caddy delante del disco público
