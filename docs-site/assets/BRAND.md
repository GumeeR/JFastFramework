# Brand assets

Three files, and one that is optional.

| File | Where it is used |
| --- | --- |
| `mark.svg` | The topbar monogram, and the hero art when no mascot is present. |
| `favicon.svg` | The browser tab. A square crop, with no gradients, because at 16px they turn to mud. |
| `site.css` | The palette. Black and crimson; the tokens are defined once on `:root` and re-pointed under `prefers-color-scheme`. |
| `mascot.png` | **Optional.** The owl. Drop it here and the home page hero uses it instead of the monogram. |

## The mark

A J leaning forward. **One shear angle, 18 degrees, governs every cut** in it,
including the foot of the hook -- that single constraint is what keeps three
shapes reading as one letter rather than as a pile of parallelograms. Flat
fills only, no gradients: a favicon at 16px loses a gradient and keeps a
silhouette, so the silhouette is what carries the mark.

The two speed lines behind it are set at 22% and 15% opacity. They are meant to
be noticed second, and they are dropped entirely from the favicon, where at
16px they are two grey pixels of noise.

`favicon.svg` centres the letter's bounding box inside its tile. If you edit
the paths, recompute the transform: getting it wrong clips the hook off the
right edge, which reads as a rendering bug rather than as a logo.

## The mascot

The site is built to work without it, which is what stops a missing binary from
breaking the build:

```bash
cp ~/wherever/owl.png docs-site/assets/mascot.png
python docs-site/build.py --version latest --output site/latest
```

`build.py` checks for the file and falls back to `mark.svg` when it is absent.
Nothing else needs editing.

Save it at roughly 1520×1014 (2× the 760×507 the page reserves) so it stays
sharp on a high-density display, and keep the black ground baked into the
image — the hero paints black behind it in light mode for exactly that reason.

## The palette

| Token | Dark | Light |
| --- | --- | --- |
| Accent | `#ff3b45` | `#d0111c` |
| Background | `#08080a` | `#fbfbfc` |
| Terminal | `#050506` | `#0b0b0e` |

The wordmark sets `jfast` in the accent and `framework` in the text colour, so
it reads as one word and still says which half is the name.
