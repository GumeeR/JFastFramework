# Packages, and the decisions inside them

Nothing in this catalogue is installed by default. A service that serves JSON
should not carry numpy — and the moment it does, the plugin graph stops
describing what the service actually needs, which is the property that makes
the graph worth having.

```bash
jfast add                  # the catalogue
jfast add exports          # adds it to this service
jfast add xml --service catalog
```

`jfast add` edits the service's `requirements.txt`, folds the extra into the
existing `jfastframework[...]` pin, and installs. In a workspace with more than
one backend it asks which service, because adding a heavy dependency to the
wrong one is invisible until the image is built.

`jfast init` offers the same list at the end.

## What is in it

| | |
| --- | --- |
| `exports` | Excel and PDF: write large sheets, merge many documents |
| `render` | Create PDFs from HTML templates |
| `xml` | Large XML, fast, with signature verification |
| `dataframes` | Tabular analysis over large result sets |
| `vision` | Image processing and OCR preparation |
| `validation` | Email and phone numbers, validated properly |
| `locale` | Dates, currency and numbers in the reader's language |
| `retry` | Retries with backoff around somebody else's service |

Each entry carries the decision somebody would otherwise make badly at three in
the afternoon: which of two libraries and why, the packaging trap that makes
the obvious wheel fail inside a container, and what has to happen besides
installing. `jfast add <name>` prints it.

The two that catch people:

- **`vision` installs `opencv-python-headless`, never `opencv-python`.** The
  default wheel links GUI libraries that do not exist in a slim container, so
  it installs cleanly and then fails at import with a libGL error that looks
  like anything but a packaging mistake.
- **`render` needs system packages.** WeasyPrint imports and then fails without
  libpango and libharfbuzz in the image. `jfast add render` prints the apt line.

---

## Assembling PDFs

`exports` covers two different jobs that are easy to confuse.

**Creating** a document — an invoice from a template — is `render`, and it
uses the same Jinja templates the mail plugin does, so one invoice design
serves the email and the download.

**Assembling** existing documents — a bundle of hundreds of scanned pages — is
`jfastframework.exports.pdf`, and it is a different library entirely.

```python
from jfastframework.exports import pdf

result = pdf.merge(paths, "legajo.pdf")
if not result.complete:
    for skipped in result.skipped:
        log.error("left out %s: %s", skipped.path, skipped.reason)
```

### Check `skipped`

The obvious implementation logs a warning for a missing or corrupt file and
carries on, returning only the output path. The caller then gets a bundle that
*looks* complete, and nobody reads a worker's logs.

For a fiscal or legal bundle that is the worst available outcome, so `merge()`
returns what it could not include, and there is a method for the case where a
short bundle is not acceptable at all:

```python
result.raise_if_incomplete()   # better to fail the job than to file it short
```

One corrupt page still does not lose the other nine hundred. It just cannot
disappear quietly.

### Memory

`PdfWriter.append()` holds every appended page until `write()`, so peak memory
grows with the whole job rather than with the largest document. `merge()`
therefore works in batches, spilling each to a temporary file and merging those
at the end — peak memory is one batch.

```python
pdf.merge(paths, "out.pdf", batch_size=50)
```

The default is fine for ordinary scanned pages. Raise it when the sources are
small, lower it when they are not.

### Images

`img2pdf`, not Pillow. It embeds a JPEG losslessly rather than decoding and
re-encoding it, so a scanned page is neither degraded nor slowly rebuilt.

---

## Spreadsheets

openpyxl has two modes, and the difference decides whether a large report
finishes. The normal one builds the whole workbook as objects; **write-only
mode streams rows to the file as they are appended**, which is what
`stream_rows` uses.

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

The cost is real: **you cannot go back.** No reading a cell you already wrote,
no auto-sizing a column from data you have forgotten. Column widths are
declared up front for that reason.

Reading back uses `read_only` and `data_only`, so a formula comes out as
`1250.00` rather than as `=SUM(B2:B40)` — which is almost always what a reader
wants.
