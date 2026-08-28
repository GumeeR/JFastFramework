#!/usr/bin/env python3
"""Build the JFastFramework documentation site.

    python docs-site/build.py --version latest --output site/latest

Design constraints, in order:

1. **The markdown in docs/ is the source.** A site that duplicates the docs
   goes stale the first week. This renders them; it does not restate them.
2. **No JavaScript framework, no build toolchain.** The site is HTML and one
   stylesheet, so it still builds in five years.
3. **Versioned.** Each release publishes under its own path and older versions
   keep working, because someone is always pinned to one.

Requires: ``pip install jfastframework[docs]``
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import markdown

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
ASSETS = Path(__file__).resolve().parent / "assets"
GITHUB = "https://github.com/JFabrizzio5/JFastFramework"
FAVICON = (
    "data:image/svg+xml,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<text y='26' font-size='26'>⚡</text></svg>"
)
DESCRIPTION = "A plugin-based FastAPI framework for microservices, built to be driven by AI agents."


@dataclass(frozen=True)
class Page:
    slug: str
    title: str
    source: Path
    summary: str = ""


# Order is the reading order, not alphabetical: someone landing here should be
# able to start at the top and keep going.
PAGES: tuple[Page, ...] = (
    Page("quickstart", "Quickstart", REPO / "README.md", "Install, generate, run."),
    Page(
        "modules", "Modules and layouts", DOCS / "modules.md", "Layered or screaming, JSON or HTMX."
    ),
    Page(
        "datastores",
        "Datastores",
        DOCS / "datastores.md",
        "PostgreSQL, Redis, Mongo, Qdrant.",
    ),
    Page("queues", "Queues and events", DOCS / "queues-and-events.md", "Jobs and streams."),
    Page("frontend", "Frontends", DOCS / "frontend.md", "HTMX, Vue, React."),
    Page("workspaces", "Workspaces", DOCS / "workspaces.md", "Many services, one gateway."),
    Page(
        "languages", "Polyglot services", DOCS / "service-contract.md", "The contract Go satisfies."
    ),
    Page(
        "migrations",
        "Migrations and tests",
        DOCS / "migrations-and-tests.md",
        "Alembic and pytest.",
    ),
    Page("auth", "Authentication", DOCS / "auth.md", "JWT, scopes, rotation, revocation."),
    Page("storage", "Storage", DOCS / "storage.md", "Disks, signed URLs, S3 and MinIO."),
    Page(
        "multitenancy",
        "Multi-tenancy",
        DOCS / "multitenancy.md",
        "Subdomains, tokens, trust order.",
    ),
    Page("cloud", "Cloud", DOCS / "cloud.md", "Secrets, functions, notifications."),
    Page("kubernetes", "Kubernetes", DOCS / "kubernetes.md", "Manifests from the contract."),
    Page("contracts", "Contracts", DOCS / "contracts.md", "Rules an agent cannot drift past."),
    Page(
        "local-setup",
        "Running it locally",
        DOCS / "local-setup.md",
        "Install, generate, run.",
    ),
    Page("plugins", "Writing a plugin", DOCS / "plugins.md", "The extension point."),
    Page("deploy", "Deployment", DOCS / "deploy.md", "Compose, Caddy, Dockerfile."),
    Page("skills", "Skills for agents", DOCS / "skills.md", "Making it legible to AI."),
    Page("architecture", "Architecture", REPO / "ARCHITECTURE.md", "Decisions and their costs."),
    Page("roadmap", "Roadmap", REPO / "PLAN.md", "Done, partial, not started."),
    Page("changelog", "Changelog", REPO / "CHANGELOG.md", "What changed, and why."),
)


def render_markdown(text: str) -> tuple[str, str]:
    """Render one document, returning (html, first paragraph)."""
    converter = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc", "codehilite", "sane_lists", "attr_list"],
        extension_configs={"codehilite": {"guess_lang": False, "css_class": "highlight"}},
    )
    body = converter.convert(text)

    # First real paragraph, for the card summary on the index.
    match = re.search(r"<p>(.*?)</p>", body, re.DOTALL)
    summary = re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else ""
    return body, summary


def rewrite_links(body: str) -> str:
    """Turn repository-relative markdown links into site links.

    The docs are written to be read on GitHub, where ``docs/modules.md`` is
    correct. On the site the same link has to become ``modules.html``, and
    anything with no page of its own (a skill, a script, a template) has to
    become an absolute link into the repository rather than a 404.

    Rewriting here rather than editing the markdown keeps one source of truth:
    the docs stay readable in the editor and in a pull request.
    """
    by_source = {page.source.name: page.slug for page in PAGES}

    def replace(match: re.Match[str]) -> str:
        href = match.group(1)
        if href.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)

        target, _, fragment = href.partition("#")
        suffix = f"#{fragment}" if fragment else ""
        filename = target.rsplit("/", 1)[-1]

        slug = by_source.get(filename)
        if slug is not None:
            return f'href="{slug}.html{suffix}"'

        # No page for it: point at the file in the repository.
        clean = target.lstrip("./").removeprefix("../")
        return f'href="{GITHUB}/blob/main/{clean}{suffix}"'

    return re.sub(r'href="([^"]+)"', replace, body)


def strip_leading_h1(body: str) -> tuple[str, str | None]:
    """Move the document's own <h1> into the page header."""
    match = re.match(r"\s*<h1[^>]*>(.*?)</h1>", body, re.DOTALL)
    if not match:
        return body, None
    title = re.sub(r"<[^>]+>", "", match.group(1)).strip()
    return body[match.end() :], title


def layout(
    *,
    title: str,
    body: str,
    version: str,
    versions: list[str],
    active: str,
    depth: int = 0,
    hero: str = "",
    wide: bool = False,
) -> str:
    root = "../" * depth

    # Built outside the f-string: nesting the same quote character inside one
    # is a syntax error before Python 3.12, and CI runs 3.11.
    def nav_link(page: Page) -> str:
        active_class = ' class="active"' if page.slug == active else ""
        return (
            f'        <a href="{root}{page.slug}.html"{active_class}>{html.escape(page.title)}</a>'
        )

    nav_items = "\n".join(nav_link(page) for page in PAGES)

    def version_option(name: str) -> str:
        selected = " selected" if name == version else ""
        return f'          <option value="{name}"{selected}>{name}</option>'

    options = "\n".join(version_option(v) for v in versions)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · JFastFramework</title>
<meta name="description" content="{DESCRIPTION}">
<link rel="stylesheet" href="{root}assets/site.css">
<link rel="icon" href="{FAVICON}">
</head>
<body>
<a class="skip" href="#main">Skip to content</a>

<header class="topbar">
  <a class="brand" href="{root}index.html">
    <span class="mark">JF</span>
    <span>JFastFramework</span>
  </a>
  <div class="topbar-right">
    <label class="version-picker">
      <span class="sr-only">Version</span>
      <select onchange="location.href='../'+this.value+'/'+location.pathname.split('/').pop()">
{options}
      </select>
    </label>
    <a class="ghost" href="{GITHUB}">GitHub</a>
  </div>
</header>

<div class="shell{" wide" if wide else ""}">
  <nav class="sidebar" aria-label="Documentation">
{nav_items}
  </nav>

  <main id="main" class="content">
{hero}
{body}
    <footer class="page-footer">
      <p>JFastFramework {html.escape(version)} · MIT ·
        <a href="{GITHUB}">source</a> ·
        <a href="{GITHUB}/blob/main/CHANGELOG.md">changelog</a>
      </p>
    </footer>
  </main>
</div>
</body>
</html>
"""


def build_index(version: str, versions: list[str], summaries: dict[str, str]) -> str:
    cards = "\n".join(
        f"""      <a class="card" href="{page.slug}.html">
        <h3>{html.escape(page.title)}</h3>
        <p>{html.escape(page.summary or summaries.get(page.slug, ""))}</p>
      </a>"""
        for page in PAGES
        if page.slug not in ("changelog", "roadmap")
    )

    def created(path: str, note: str) -> str:
        return f'  <span class="dim">created</span>  {path:<20}<span class="dim">{note}</span>'

    prompt = '<span class="c">$</span>'
    terminal = "\n".join(
        [
            f"{prompt} pip install jfastframework",
            f"{prompt} jfast start shop",
            "",
            created("shop/", "FastAPI + PostgreSQL + pgvector + Redis + jobs"),
            created("shop-web/", "Vue 3 + Vite + Tailwind"),
            created("docker-compose.yml", "derived from the plugin graph"),
            created("Caddyfile", "one hostname, TLS, static assets"),
        ]
    )

    hero = f"""    <section class="hero">
      <p class="eyebrow">{html.escape(version)} · alpha</p>
      <h1>Build a service, not a scaffold.</h1>
      <p class="lede">
        A plugin-based FastAPI framework where monitoring, databases, queues,
        vector search and HTML rendering are all removable plugins â and the
        generator emits only the code that is genuinely yours.
      </p>
      <div class="cta">
        <a class="button primary" href="quickstart.html">Get started</a>
        <a class="button" href="architecture.html">Why it is built this way</a>
      </div>
      <pre class="terminal"><code>{terminal}</code></pre>
    </section>
"""

    body = f"""    <section class="pitch">
      <div>
        <h2>Everything above the kernel is a plugin</h2>
        <p>
          Delete <code>"metrics"</code> from one list and the middleware, the
          endpoint and the Prometheus container in the generated compose file
          all disappear together. Infrastructure is derived from the plugin
          graph, so it cannot drift from what the app actually loads.
        </p>
      </div>
      <div>
        <h2>Polyglot, because the contract is the API</h2>
        <p>
          A Go service satisfies the same contract a Python one does:
          <code>/health</code>, <code>/ready</code>, <code>X-Request-ID</code>,
          <code>problem+json</code>. That is why the gateway routes to it
          without knowing it is Go.
        </p>
      </div>
      <div>
        <h2>Built to be driven by an agent</h2>
        <p>
          <code>jfast describe --json</code> returns the settings schema, the
          plugin graph and the provider keys without importing the app. The
          structure is knowable in advance, so generated work lands in a shape
          you already reviewed.
        </p>
      </div>
    </section>

    <section>
      <h2>Documentation</h2>
      <div class="cards">
{cards}
      </div>
    </section>

    <section class="honest">
      <h2>What is not done</h2>
      <p>
        This is alpha, and the roadmap says so in the same file that tracks
        what works. Multi-tenancy is a convention rather than an isolation
        guarantee until row-level security lands. RabbitMQ and Kafka are wired
        but have not been run against real brokers in CI. Angular is not
        generated. <a href="roadmap.html">The full list</a> is kept honest on
        purpose â a roadmap that overstates completion is worse than none.
      </p>
    </section>
"""
    return layout(
        title="A FastAPI framework for microservices",
        body=body,
        version=version,
        versions=versions,
        active="index",
        hero=hero,
        wide=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="latest")
    parser.add_argument("--output", type=Path, default=REPO / "site" / "latest")
    parser.add_argument(
        "--versions",
        default="",
        help="Comma-separated versions for the picker. Defaults to this one.",
    )
    args = parser.parse_args()

    versions = [v.strip() for v in args.versions.split(",") if v.strip()] or [args.version]
    if args.version not in versions:
        versions.insert(0, args.version)

    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ASSETS, output / "assets", dirs_exist_ok=True)

    summaries: dict[str, str] = {}
    for page in PAGES:
        if not page.source.is_file():
            print(f"  skipped  {page.slug} ({page.source.name} not found)")
            continue

        body, summary = render_markdown(page.source.read_text(encoding="utf-8"))
        body = rewrite_links(body)
        body, own_title = strip_leading_h1(body)
        summaries[page.slug] = summary

        heading = html.escape(own_title or page.title)
        hero = f'    <header class="doc-head"><h1>{heading}</h1></header>\n'
        (output / f"{page.slug}.html").write_text(
            layout(
                title=page.title,
                body=body,
                version=args.version,
                versions=versions,
                active=page.slug,
                hero=hero,
            ),
            encoding="utf-8",
        )
        print(f"  built    {page.slug}.html")

    (output / "index.html").write_text(
        build_index(args.version, versions, summaries), encoding="utf-8"
    )
    (output / "versions.json").write_text(json.dumps(versions, indent=2), encoding="utf-8")
    print(f"  built    index.html\n\nSite written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
