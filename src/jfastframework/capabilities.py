"""The catalogue behind ``jfast add``.

A framework's job here is not to be a nicer ``pip install``. It is to carry the
decisions somebody would otherwise make badly at three in the afternoon:

* which of two libraries that do the same thing, and why;
* the packaging traps -- ``opencv-python`` drags X11 into a container and
  fails at import, ``opencv-python-headless`` does not, and the difference is
  a wheel name nobody guesses;
* what has to happen *besides* installing, like the system packages WeasyPrint
  needs in the image.

Nothing here is installed by default. A service that serves JSON should not
carry numpy, and the plugin graph stops describing the service the moment it
does.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    """One thing a service can be given, and everything that comes with it."""

    name: str
    summary: str
    # The pyproject extra that carries the packages.
    extra: str
    packages: tuple[str, ...]
    # Why these packages and not the obvious alternatives.
    rationale: str = ""
    # A plugin this capability turns on, when it has one.
    plugin: str = ""
    # apt packages the generated Dockerfile needs. Empty for pure wheels.
    system_packages: tuple[str, ...] = ()
    # Shown after installing: the thing that is not obvious from the import.
    after: str = ""
    heavy: bool = False
    related: tuple[str, ...] = field(default_factory=tuple)


CATALOG: dict[str, Capability] = {
    "exports": Capability(
        name="exports",
        summary="Excel and PDF: write large sheets, merge many documents",
        extra="exports",
        packages=("openpyxl>=3.1", "pypdf>=5.1", "img2pdf>=0.5", "pillow>=11.0"),
        rationale=(
            "img2pdf rather than Pillow for images: it embeds a JPEG losslessly "
            "instead of re-encoding it, so a scanned page is neither degraded nor "
            "slowly rebuilt. openpyxl's write-only mode streams rows, which is the "
            "difference between a 200k-row report working and not."
        ),
        after=(
            "jfastframework.exports.pdf.merge() reports what it could not include -- "
            "check `result.skipped`, or a bundle goes out short in silence."
        ),
    ),
    "render": Capability(
        name="render",
        summary="Create PDFs from HTML templates",
        extra="render",
        packages=("weasyprint>=63",),
        rationale=(
            "For *creating* a document, not assembling one: an invoice from a "
            "template. It renders the same Jinja templates the mail plugin uses, so "
            "one invoice design serves the email and the download. ReportLab is the "
            "alternative and needs no system libraries, but you draw the page by "
            "hand rather than writing HTML."
        ),
        system_packages=("libpango-1.0-0", "libpangoft2-1.0-0", "libharfbuzz0b", "libffi-dev"),
        after="Add the system packages above to the Dockerfile, or it imports and then fails.",
        related=("exports",),
    ),
    "xml": Capability(
        name="xml",
        summary="Large XML, fast, with signature verification",
        extra="xml",
        packages=("lxml>=5.3", "signxml>=4.0"),
        rationale=(
            "lxml parses an order of magnitude faster than the standard library and "
            "supports iterparse, which is what keeps a million-document batch from "
            "being a million documents in memory. signxml verifies XML-DSig, which "
            "is how a signed fiscal document proves it was not altered."
        ),
        after=(
            "Use lxml.etree.iterparse() for anything large -- parsing the whole tree "
            "is the mistake this package exists to let you avoid."
        ),
    ),
    "dataframes": Capability(
        name="dataframes",
        summary="Tabular analysis over large result sets",
        extra="dataframes",
        packages=("polars>=1.17",),
        rationale=(
            "polars over pandas for new code: lazy execution, no index to reason "
            "about, and materially lower memory on the same data. pandas is still "
            "the right answer when you need a library that only speaks pandas -- "
            "`jfast add dataframes --pandas` installs that instead."
        ),
        heavy=True,
    ),
    "vision": Capability(
        name="vision",
        summary="Image processing and OCR preparation",
        extra="vision",
        packages=("opencv-python-headless>=4.10", "pillow>=11.0"),
        rationale=(
            "headless, always. The default opencv-python wheel links GUI libraries "
            "that do not exist in a slim container, so it installs cleanly and then "
            "fails at import with a libGL error that looks like anything but a "
            "packaging mistake."
        ),
        heavy=True,
    ),
    "validation": Capability(
        name="validation",
        summary="Email and phone numbers, validated properly",
        extra="validation",
        packages=("email-validator>=2.2", "phonenumbers>=8.13"),
        rationale=(
            "pydantic's EmailStr does nothing without email-validator installed -- "
            "the field silently accepts anything. phonenumbers knows that a Mexican "
            "mobile is ten digits and a landline is not, which a regex does not."
        ),
    ),
    "locale": Capability(
        name="locale",
        summary="Dates, currency and numbers in the reader's language",
        extra="locale",
        packages=("babel>=2.16",),
        rationale=(
            "Formatting money and dates by hand is how an invoice ends up saying "
            "$1,234.50 to a reader whose locale writes $1.234,50. Babel has the CLDR "
            "data; a format string does not."
        ),
    ),
    "retry": Capability(
        name="retry",
        summary="Retries with backoff around somebody else's service",
        extra="retry",
        packages=("tenacity>=9.0",),
        rationale=(
            "Backoff and jitter, declared as a decorator instead of written again in "
            "every integration. It is not a circuit breaker -- it will keep retrying "
            "a service that is entirely down, one call at a time."
        ),
    ),
}


def get(name: str) -> Capability:
    try:
        return CATALOG[name]
    except KeyError:
        known = ", ".join(sorted(CATALOG))
        raise KeyError(f"Unknown capability {name!r}. Known: {known}.") from None


def names() -> tuple[str, ...]:
    return tuple(CATALOG)


__all__ = ["CATALOG", "Capability", "get", "names"]
