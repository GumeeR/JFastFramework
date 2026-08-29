# Paquetes, y las decisiones que llevan adentro

Nada de este catálogo se instala por defecto. Un servicio que sirve JSON no
debería cargar con numpy — y en cuanto lo hace, el grafo de plugins deja de
describir lo que el servicio realmente necesita, que es la propiedad que hace
que el grafo valga la pena.

```bash
jfast add                  # the catalogue
jfast add exports          # adds it to this service
jfast add xml --service catalog
```

`jfast add` edita el `requirements.txt` del servicio, mete el extra dentro del
pin `jfastframework[...]` que ya existe, e instala. En un workspace con más de
un backend pregunta a qué servicio, porque agregar una dependencia pesada al
equivocado es invisible hasta que se construye la imagen.

`jfast init` ofrece la misma lista al final.

## Qué hay adentro

| | |
| --- | --- |
| `exports` | Excel y PDF: escribir hojas grandes, unir muchos documentos |
| `render` | Crear PDFs a partir de plantillas HTML |
| `xml` | XML grande, rápido, con verificación de firma |
| `dataframes` | Análisis tabular sobre conjuntos de resultados grandes |
| `vision` | Procesamiento de imágenes y preparación para OCR |
| `validation` | Correos y números de teléfono, validados en serio |
| `locale` | Fechas, moneda y números en el idioma del lector |
| `retry` | Reintentos con backoff alrededor del servicio de otro |

Cada entrada trae la decisión que, si no, alguien tomaría mal a las tres de la
tarde: cuál de dos librerías y por qué, la trampa de packaging que hace fallar
el wheel obvio adentro de un contenedor, y qué más tiene que pasar además de
instalar. `jfast add <name>` la imprime.

Las dos que atrapan a la gente:

- **`vision` instala `opencv-python-headless`, nunca `opencv-python`.** El
  wheel por defecto enlaza librerías de GUI que no existen en un contenedor
  slim, así que instala limpio y después falla al importar con un error de
  libGL que parece cualquier cosa menos un error de packaging.
- **`render` necesita paquetes del sistema.** WeasyPrint importa y después
  falla sin libpango y libharfbuzz en la imagen. `jfast add render` imprime la
  línea de apt.

---

## Armar PDFs

`exports` cubre dos trabajos distintos que es fácil confundir.

**Crear** un documento — una factura a partir de una plantilla — es `render`, y
usa las mismas plantillas Jinja que usa el plugin de correo, así que un solo
diseño de factura sirve para el correo y para la descarga.

**Armar** documentos que ya existen — un legajo de cientos de páginas
escaneadas — es `jfastframework.exports.pdf`, y es una librería completamente
distinta.

```python
from jfastframework.exports import pdf

result = pdf.merge(paths, "legajo.pdf")
if not result.complete:
    for skipped in result.skipped:
        log.error("left out %s: %s", skipped.path, skipped.reason)
```

### Revisa `skipped`

La implementación obvia loguea un warning por un archivo faltante o corrupto y
sigue, devolviendo solo la ruta de salida. Quien llama se queda entonces con un
legajo que *parece* completo, y nadie lee los logs de un worker.

Para un legajo fiscal o legal ese es el peor resultado posible, así que
`merge()` devuelve lo que no pudo incluir, y hay un método para el caso en que
un legajo incompleto no es aceptable en absoluto:

```python
result.raise_if_incomplete()   # better to fail the job than to file it short
```

Una página corrupta sigue sin hacerte perder las otras novecientas. Lo único
que no puede es desaparecer en silencio.

### Memoria

`PdfWriter.append()` retiene cada página agregada hasta el `write()`, así que
el pico de memoria crece con el trabajo entero en vez de con el documento más
grande. Por eso `merge()` trabaja por lotes, volcando cada uno a un archivo
temporal y uniendo esos al final — el pico de memoria es un lote.

```python
pdf.merge(paths, "out.pdf", batch_size=50)
```

El valor por defecto está bien para páginas escaneadas comunes. Súbelo cuando
las fuentes son chicas, bájalo cuando no lo son.

### Imágenes

`img2pdf`, no Pillow. Embebe un JPEG sin pérdida en vez de decodificarlo y
volver a codificarlo, así que una página escaneada no se degrada ni se
reconstruye lentamente.

---

## Hojas de cálculo

openpyxl tiene dos modos, y la diferencia decide si un reporte grande termina o
no. El normal construye el workbook entero como objetos; **el modo write-only
va escribiendo las filas al archivo a medida que se agregan**, que es lo que
usa `stream_rows`.

```python
from jfastframework.exports import excel

excel.stream_rows(
    "report.xlsx",
    [
        excel.Column("RFC", width=16),
        excel.Column("Total", width=12, number_format="#,##0.00"),
    ],
    cursor,          # consumed lazily; never held whole
)
```

El costo es real: **no puedes volver atrás.** Nada de leer una celda que ya
escribiste, nada de auto-ajustar una columna con datos que ya olvidaste. Por
eso los anchos de columna se declaran de entrada.

La lectura de vuelta usa `read_only` y `data_only`, así que una fórmula sale
como `1250.00` en vez de como `=SUM(B2:B40)` — que es casi siempre lo que quien
lee quiere.
