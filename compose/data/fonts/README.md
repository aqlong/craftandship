# compose/data/fonts: self-hosted WOFF2 cache

This directory holds pre-fetched latin-subset WOFF2 files for the
`_harbor_meta.self_host_fonts: true` opt-in (see
`compose/utils/self_host_fonts.py`).

## Filename convention

`<family-slug>-<weight>.woff2`

The slug is the Google Fonts display family lowercased with non-alnum
runs collapsed to `-`:

- `Cormorant Garamond` → `cormorant-garamond`
- `DM Serif Display` → `dm-serif-display`
- `IBM Plex Serif` → `ibm-plex-serif`
- `Tangerine` → `tangerine`
- `Montserrat` → `montserrat`

This repository ships the cache EMPTY (no font files are committed), so
self-hosting falls back to the Google Fonts URL until you populate it.
Example file names once populated:

```
cormorant-garamond-400.woff2
cormorant-garamond-700.woff2
montserrat-400.woff2
montserrat-500.woff2
montserrat-600.woff2
montserrat-700.woff2
tangerine-400.woff2
tangerine-700.woff2
```

## Populating the cache

For a given font_pair (e.g. `tangerine-montserrat`), look up the
required weights in `compose/data/font-pairs.json`. For each
`(family, weight)` pair:

1. Hit the Google Fonts CSS endpoint with a real-browser User-Agent so
   it returns the WOFF2 variant (without UA, it returns TTF).
2. Parse the returned CSS for the `latin` subset `src: url(...)` line.
3. Download the WOFF2 from that URL.
4. Save to this directory as `<slug>-<weight>.woff2`.

The cache is read-only at compose time: missing entries fall through
to the Google Fonts URL path with a `self_host_fonts_fallback`
translation_warning so SE review can see what happened.

## Why no auto-fetch at compose time

Compose runs in worker containers + CI test runs without guaranteed
network access. A compose-time download would (a) make builds
non-deterministic across networks, (b) break tests, (c) slow cold
builds. The cache pattern keeps compose offline + byte-identical for
identical input.

Populate the cache once per font family + weight; commit the WOFF2s
(small: ~16KB each for latin subset).
