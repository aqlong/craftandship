# Third-party notices

This project's own code is released under the MIT License; see [LICENSE](LICENSE). The works below are third-party works that this repository derives from, adapts, or credits. Each section gives the upstream project, its license, its copyright line, and what in this repository relates to it. Where the provenance of something is uncertain, the section says "not recorded". The permission notices for the MIT and ISC works are reproduced once each at the end.

## UI UX Pro Max skill

- Project: https://github.com/nextlevelbuilder/ui-ux-pro-max-skill
- License: MIT
- Copyright: Copyright (c) 2024 Next Level Builder
- In this repository: [cli/design_data.py](cli/design_data.py). Its docstring says the palettes, font pairings, product recommendations and UX rules in it were extracted from the upstream CSV files (`colors.csv`, `typography.csv`, `products.csv`, `ux-guidelines.csv`) and curated. The module is imported by the v1 engine ([cli/build.py](cli/build.py), [cli/intake.py](cli/intake.py)); the code and tests call this material "UUPM". Whether the v2 decision files in [compose/data/](compose/data/palettes.json) draw on the same dataset is not recorded.

## taste-skill

- Project: https://github.com/Leonxlnx/taste-skill
- License: MIT
- Copyright: Copyright (c) 2026 Leonxlnx
- In this repository: the comment block that opens the design-system section of the stylesheet emitted by [compose/build.py](compose/build.py). It says the motion and depth rules in that block (spring easing, tinted shadows, haptic press, staggered reveal, bezel cards, ambient depth) are inspired by the design guidance published as "taste-skill", and that the pattern names "double-bezel (Doppelrand)" and "button-in-button" are that project's terms. Two later comments on the matching CSS classes repeat the credit. The CSS in this repository is this project's own implementation.

## Impeccable

- Project: https://github.com/pbakaus/impeccable
- License: Apache-2.0. The license text is available at https://www.apache.org/licenses/LICENSE-2.0.
- Copyright: Copyright 2025 Paul Bakaus
- In this repository: [docs/IMPECCABLE_DESIGN_SYSTEM.md](docs/IMPECCABLE_DESIGN_SYSTEM.md) borrows the name and the idea of an anti-pattern review from the project. The document's own attribution note says its palette, rules and wording are this project's and that no upstream text is reproduced. A few comments in [compose/](compose/build.py) refer to that review by name.

## Deep Project, Deep Plan and Deep Implement plugins

- Project: https://github.com/piercelamb/deep-plan (with `deep-project` and `deep-implement`)
- License: MIT
- Copyright: Copyright (c) 2025 Pierce Lamb
- In this repository: [docs/plans/b-plus-c/claude-plan.md](docs/plans/b-plus-c/claude-plan.md) carries an export note saying it came out of a sectionized planning workflow and names the `/deep-plan` and `/deep-implement` commands; whether [docs/plans/se-review-gate-spec.md](docs/plans/se-review-gate-spec.md) was produced the same way is not recorded. No plugin code is part of this repository.

## Lucide, with portions derived from Feather

- Project: https://lucide.dev (https://github.com/lucide-icons/lucide)
- License: ISC
- Copyright: Copyright (c) 2026 Lucide Icons and Contributors. Portions derived from Feather: MIT License, Copyright (c) 2013-present Cole Bemis.
- In this repository: [cli/templates/icon_helper.js](cli/templates/icon_helper.js) holds Lucide icon artwork as inline SVG; its header credits Lucide and notes that icons derived from Feather are MIT. The v1 build also declares the `lucide-static` npm package as a dependency of the apps it generates ([cli/build.py](cli/build.py)).

## lite-youtube-embed

- Project: https://github.com/paulirish/lite-youtube-embed
- License: see the upstream LICENSE file. The headers of the two files below, and [cli/README.md](cli/README.md), describe it as Apache-2.0. The Apache License 2.0 text is available at https://www.apache.org/licenses/LICENSE-2.0.
- Copyright: Copyright 2019 Paul Irish
- In this repository: [cli/templates/lite-youtube.js](cli/templates/lite-youtube.js) and [cli/templates/lite-youtube.css](cli/templates/lite-youtube.css). Their headers call them an independent reimplementation of the lite-youtube-embed pattern that reuses the upstream element name, the `videoid` attribute and the play-button icon path; class names, DOM structure and styles differ from upstream.

## Heroicons, and other icon shapes of uncertain origin

- Project: https://github.com/tailwindlabs/heroicons
- License: MIT
- Copyright: Copyright (c) Tailwind Labs, Inc.
- In this repository: [compose/widgets/utility/icon.ejs](compose/widgets/utility/icon.ejs). The per-glyph source was not recorded, and the file's header comment says so: the icon set was assembled by hand while the project was being built, and it is not all original work. On inspection the header reports that the `check` and `menu` paths are identical to the Heroicons v1 outline icons of the same names; that `arrow-up-right` has the geometry of the Feather icon of that name (Feather is covered in the Lucide section above); that `phone` and `external` look like redrawn variants of Heroicons v1 outlines; and that the remaining stroke icons (arrows, chevrons, calendar, clock, map-pin, search, mail, plus, minus, x, star) are simple geometric shapes with no verbatim match found. The same hamburger-menu path as `menu` also appears inline in the navigation widgets [nav-public.ejs](compose/widgets/shell/nav-public.ejs), [nav-admin.ejs](compose/widgets/shell/nav-admin.ejs), [nav-side-rail.ejs](compose/widgets/shell/nav-side-rail.ejs) and [nav-image-banner.ejs](compose/widgets/shell/nav-image-banner.ejs); the source of those paths is not recorded either.
- Brand glyphs in the same file (facebook, linkedin, twitter, tiktok, youtube, venmo, and the outlined instagram) are, per the header, the respective companies' logos in the single-path form that brand icon collections distribute. The header adds that the linkedin and youtube paths read like coordinate-rounded copies of the Simple Icons paths. The logos are trademarks of their owners. Their provenance and license are not recorded.

## Runtime dependencies

See [web/package.json](web/package.json), [requirements.worker.txt](requirements.worker.txt) and [requirements-dev.txt](requirements-dev.txt). Their licenses are those of the respective packages. [Dockerfile.worker](Dockerfile.worker) installs further software when the worker image is built (a Playwright base image, Node.js, the PostgreSQL client and the Railway CLI), and the apps the build engines generate declare their own npm dependencies in the `package.json` they emit.

## Fonts

No font files are vendored: the repository contains no `.woff`, `.woff2`, `.ttf`, `.otf` or `.eot` file. Fonts are loaded from Google Fonts at runtime. The `fonts.googleapis.com` stylesheet link appears in [web/views/layout.ejs](web/views/layout.ejs), [web/views/layout-harbor.ejs](web/views/layout-harbor.ejs), [compose/templates/layout-public.ejs](compose/templates/layout-public.ejs) and [compose/templates/layout-admin.ejs](compose/templates/layout-admin.ejs), and in the `google_fonts_url` values of [compose/data/font-pairs.json](compose/data/font-pairs.json). The opt-in self-hosting code in [compose/utils/self_host_fonts.py](compose/utils/self_host_fonts.py) reads a cache directory that ships empty ([compose/data/fonts/README.md](compose/data/fonts/README.md)).

## Other material noted elsewhere in the repository

- [cli/video_pools.json](cli/video_pools.json) lists ids of public YouTube videos; per [cli/README.md](cli/README.md) the videos are not this project's.
- The images in [cli/stock_photos/](cli/stock_photos/README.md) are generated placeholders; the README says the original photos were removed from the public copy.

## Brand assets

The Craft & Ship and Medicine Waves names, logos and artwork under `web/public/` and `cli/assets/` are not covered by the MIT license and may not be reused.

## MIT License

Applies to the works above that are marked MIT. The copyright line for each is the one given in its section.

```text
MIT License

Copyright (c) <the copyright holder named in the section above>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## ISC License

Applies to Lucide. The copyright line is the one given in its section.

```text
ISC License

Copyright (c) <the copyright holder named in the section above>

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
```
