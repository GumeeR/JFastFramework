# Autenticación

Verificación de JWT, scopes, rotación de claves y revocación.

```toml
[plugins]
enabled = ["observability", "cache", "auth"]

[plugin.auth]
mode = "jwks"
jwks_url = "https://id.example.com/.well-known/jwks.json"
issuer = "https://id.example.com/"
audience = "billing"
algorithms = ["RS256"]
```

```python
from fastapi import Depends
from jfastframework.auth import Principal, require_scopes

@router.post("/invoices")
async def create(caller: Principal = Depends(require_scopes("invoices:write"))):
    ...
```

---

## Qué hace y qué no hace

**Verifica** tokens, y puede **emitirlos**. No tiene **endpoint de login**,
porque comparar una contraseña contra tu tabla de usuarios es trabajo de tu
aplicación. `auth.issuer` está ahí para tu propia ruta de login:

```python
issuer = request.app.state.jfast.require("auth.issuer")
pair = await issuer.issue_pair(user.id, scopes=user.scopes, tenant_id=user.tenant_id)
```

Un framework que trajera un `/auth/login` tendría que inventar un modelo de
usuario, una política de hashing de contraseñas y una estrategia de bloqueo — y
pelearías contra las tres.

---

## Elegir un modo

| Modo | Clave | Úsalo cuando |
| --- | --- | --- |
| `jwks` | se obtiene del issuer | hay más de un servicio. **El default.** |
| `public_key` | un PEM fijado | un solo issuer, sin querer depender de la red |
| `secret` | secreto HMAC compartido | un único servicio que además emite |

**`secret` no va entre servicios.** Todo lo que puede verificar un token HMAC
también puede emitir uno. Un servicio de reportes de solo lectura que tenga ese
secreto puede falsificar un token de admin para el servicio de billing. `jwks` y
`public_key` separan eso: el issuer tiene la clave privada, el resto tiene una
pública.

---

## Los ataques que los defaults rechazan

Están verificados en `tests/test_auth.py`, un test por cada uno.

**Confusión de algoritmo.** Un servicio que confía en el header `alg` del propio
token puede recibir un token HS256 firmado con la *clave pública RSA que él
mismo publica* usada como secreto HMAC — y lo va a verificar. Los algoritmos
salen de la configuración y se pasan explícitamente al decoder. Configurar las
dos familias a la vez se rechaza de plano:

```
auth.algorithms mixes symmetric and asymmetric algorithms (HS256, RS256).
Allowing both lets a token signed with the public key as an HMAC secret verify.
Pick one family.
```

**`alg: none`.** Queda fuera por la misma allow-list. Nunca está en
`SUPPORTED_ALGORITHMS` y no debe agregarse.

**Reuso de token entre servicios.** Se verifican `aud` e `iss`. Los dos vienen
apagados por default en la mayoría de las librerías, y sin ellos un token válido
para un servicio *distinto* del mismo issuer se acepta aquí — que es como un
servicio comprometido de poco valor se convierte en acceso a uno de mucho.
Dejar `audience` vacío loguea un warning al arrancar en vez de aceptar todo en
silencio.

**Un clock skew generoso.** El leeway es de 30 segundos. Cinco minutos de leeway
son cinco minutos extra de vida para un token robado.

**Amplificación de refresh de JWKS.** Un `kid` desconocido dispara un refresh —
así se detecta la rotación — pero como mucho una vez por minuto. Sin ese piso,
un flujo de `kid` falsificados se convierte en una denegación de servicio contra
tu proveedor de identidad.

**Filtrar por qué falló un token.** El motivo va al log; el cliente recibe un
401 pelado. Decirle a un atacante *cuál* chequeo falló es reconocimiento gratis.

---

## El tenancy deja de ser falsificable

Antes de este plugin, `tenant_id` viene del header `X-Tenant-ID` — cómodo en
desarrollo, y seteable por cualquiera con curl. Con auth habilitado viene de un
**claim firmado**, y el header se ignora.

Esa es la razón principal de seguridad para prenderlo, más que el formulario de
login.

---

## 401 contra 403

- **401** — no sé quién eres. Sin token, o con uno inválido.
- **403** — sé quién eres, y no puedes. Autenticado, falta un scope o un rol.

Colapsarlos convierte cada bug de permisos en adivinanza. `require_scopes`
nombra los scopes que faltan en la respuesta, porque eso no es secreto frente a
un caller que ya está autenticado.

```python
require_auth                       # any verified caller
require_scopes("a", "b")           # all of these scopes
require_roles("admin", "owner")    # any one of these roles
optional_auth                      # Principal | None, for mixed routes
```

---

## Revocación

Los JWT no tienen estado, que es el punto y también el problema: un token es
válido hasta que expira y el "log out" no tiene sobre qué actuar. La respuesta
son lifetimes cortos de access token más una pequeña cantidad de estado.

`POST /auth/logout` revoca el `jti` del caller **y su familia de refresh** —
si no, el refresh token emitido junto a él emite una sesión nueva en silencio.

Las entradas de revocación llevan como TTL el tiempo de vida que le queda al
propio token: pasada la expiración el chequeo de firma lo rechaza igual, así que
guardar la entrada más tiempo solo hace crecer el store para siempre.

Con el plugin `cache` habilitado el store es Redis y un logout aplica a todas
las réplicas. Sin él el store es un dict, y `/ready` lo dice:

```
in-memory token store: revocation does not survive a restart or reach other replicas
```

No es crítico — el servicio sigue autenticando — pero es visible, en vez de
descubrirse desde un ticket de soporte.

---

## Rotación de refresh, con detección de reuso

Cada refresh devuelve un refresh token nuevo e invalida el que se presentó.
Presentar uno ya usado significa o un reintento del cliente o un token robado
reproducido, y desde el servidor esos dos casos son indistinguibles — así que se
revoca la **familia** entera y el usuario vuelve a loguearse.

Perder una sesión cuesta mucho menos que no darse cuenta de un robo.

```python
pair = await issuer.issue_pair("user-1", scopes=["invoices:read"], tenant_id="acme")
# POST /auth/refresh {"refresh_token": ...} -> a new pair
```

El store de Redis consume un refresh token con `DELETE`, cuyo valor de retorno
hace atómico el chequeo: dos refresh concurrentes no pueden tener éxito los dos.

---

## Rotación de claves

Con `mode = "jwks"` la rotación es una publicación, no un redeploy. El issuer
agrega una clave nueva a su documento JWKS y empieza a firmar con ella; los
servicios traen la clave nueva con el primer token que lleve un `kid`
desconocido.

Mantén la clave vieja publicada hasta que haya expirado todo token firmado con
ella.

Si el endpoint de JWKS es inalcanzable, las claves cacheadas siguen funcionando
— una caída de JWKS no debe tirar abajo todos los servicios — y `/ready` reporta
qué tan viejas están.

---

## Sign in with Google

```toml
[plugin.auth.providers.google]
client_id = "...apps.googleusercontent.com"
client_secret = "${GOOGLE_CLIENT_SECRET}"
redirect_uri = "https://app.example.com/auth/google/callback"
```

`google`, `microsoft` y `github` no necesitan nada más. Cualquier otro nombre
tiene que dar además `issuer`, `jwks_uri`, `authorization_endpoint` y
`token_endpoint`.

Aparecen dos rutas:

| Ruta | Hace |
| --- | --- |
| `GET /auth/google/start` | redirige a Google, setea una cookie de state/nonce |
| `GET /auth/google/callback` | verifica todo, y después llama a tu handler |

El handler es tuyo, porque solo tú sabes qué es un usuario aquí:

```python
auth = app.state.jfast.require("auth")

@auth.on_identity
async def sign_in(identity, request):
    user = await users.upsert_federated(identity.federated_id, identity.email)
    return auth.issuer.issue(subject=str(user.id), scopes=user.scopes)
```

Sin un handler registrado el callback devuelve un 500 diciéndolo. Un usuario
verificado y ningún lugar donde ponerlo es un error de configuración, y un 200
alegre lo escondería.

### Por qué emites tu propio token

Un ID token de Google dice "Google cree que esta persona es
person@example.com". No dice qué puede hacer en tu sistema, expira según el
calendario de Google, y no puedes revocarlo. Cambiarlo por tu propio token es lo
que devuelve los scopes, tu tenant y tu revocación a tu control.

### Qué se verifica, y qué frena cada verificación

| Verificación | Sin ella |
| --- | --- |
| `aud == client_id` | un token emitido para la app de Google de *cualquier otro* inicia sesión aquí |
| `iss == provider` | se acepta un token de un issuer completamente distinto |
| `state` coincide con la cookie | login CSRF: un code obtenido en el browser del atacante, reproducido |
| `nonce` dentro del token | un ID token capturado reproducido en un login nuevo |
| firma, vía el JWKS del proveedor | lo de siempre |

La cookie de state es `httponly` (un script no puede leerla), `samesite=lax`
(sobrevive el redirect top-level de vuelta de Google pero no un POST
cross-site) y `secure` fuera de desarrollo.

### Dos trampas

**Nunca asocies una cuenta existente a partir de un email no verificado.**
`email_verified` viaja en `OIDCIdentity` justamente para esto. Un proveedor que
deja al usuario poner cualquier email sin probarlo, cruzado contra tu tabla de
usuarios, es un account takeover en un solo paso.

**Guarda `identity.federated_id`, no `identity.subject`.** Los subject id son
únicos por proveedor, no globalmente. El usuario `42` de GitHub y el usuario
`42` de Google son personas distintas, y el subject pelado no te dice cuál.

GitHub es OAuth2, no OIDC: no hay ID token, así que `verify_id_token()` lo
rechaza y la llamada a userinfo es la única forma de saber quién se logueó. Está
listado para que la lista esté completa; esa llamada la haces tú.

Instalación: `pip install jfastframework[oidc]`.

---

## Checklist antes de producción

- [ ] `mode = "jwks"` o `public_key`, no `secret`, si hay más de un servicio
- [ ] `audience` apuntando a este servicio, `issuer` a tu proveedor de identidad
- [ ] `cache` habilitado, para que la revocación se comparta entre réplicas
- [ ] Lifetime del access token en minutos, no en horas
- [ ] `JFAST_AUTH_SECRET` (si aplica): 32+ bytes aleatorios de un secret manager
- [ ] Los tokens nunca se loguean. `Principal.describe()` es la forma segura

## Qué no está aquí

- **Un store de usuarios, hashing de contraseñas, MFA, bloqueo.** Cosas de la
  aplicación. El login social termina en una identidad verificada; convertirla
  en un usuario es tuyo.
- **PKCE.** El flujo de authorization code de aquí es el de cliente
  confidencial, corrido desde tu backend con un client secret. Un cliente
  público (una app móvil hablando directo con Google) necesita PKCE, que no
  está implementado.
- **mTLS o identidad service-to-service.** Los tokens de máquina funcionan hoy;
  SPIFFE no está implementado.
- **Aislamiento de claves por tenant.** Un issuer, un juego de claves.
