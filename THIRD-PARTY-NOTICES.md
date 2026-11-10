# Third-party notices

This add-on repository contains only original code: `litellm_proxy/run.py`, the
Ingress UI in `litellm_proxy/ui/`, the Home Assistant add-on metadata and the
documentation. No third-party source code is vendored here.

At build and run time the add-on builds on the official LiteLLM container image
and talks to it through its documented interfaces (the `litellm --config ...
--host ... --port ...` command line and the `litellm` Python package). The
notices below cover those components.

---

## LiteLLM

- Component: LiteLLM Proxy / LiteLLM Python SDK
- Homepage: https://github.com/BerriAI/litellm
- Used as: base image `ghcr.io/berriai/litellm`, pinned to the tag `v1.106.6`
  in `litellm_proxy/Dockerfile`
- Licence: MIT (all content outside the `enterprise/` directory)
- Copyright (c) 2023 Berri AI

The MIT terms below apply to the LiteLLM code shipped in that image, excluding
`enterprise/`:

```
MIT License

Copyright (c) 2023 Berri AI

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

### Enterprise components are NOT MIT

The upstream image also ships the `enterprise/` directory, which is licensed
under the BerriAI Enterprise License (`enterprise/LICENSE.md` upstream), not
under MIT. That licence states, among other restrictions, that it is "forbidden
to copy, merge, publish, distribute, sublicense, and/or sell the Software"
without a valid BerriAI Enterprise licence.

**Do not publish prebuilt images of this add-on.** The Dockerfile pulls the
upstream image at build time, so each Home Assistant installation obtains
LiteLLM directly from BerriAI and no LiteLLM code is redistributed by this
repository. Publishing a compiled image to a registry would redistribute the
`enterprise/` code and require a BerriAI Enterprise licence.

---

## PyYAML

- Component: PyYAML (`import yaml`)
- Homepage: https://pyyaml.org/
- Used as: provided by the official LiteLLM image; imported at run time by
  `run.py` to read and write the generated LiteLLM configuration
- Licence: MIT
- Copyright (c) 2017-2021 Ingy döt Net
- Copyright (c) 2006-2016 Kirill Simonov

The full MIT text is the same as the one reproduced above, with the copyright
holders named in this section.

---

## Python

- Component: CPython runtime (Python 3.x)
- Homepage: https://www.python.org/
- Licence: Python Software Foundation License Version 2 (PSF-2.0)
- Copyright (c) 2001-2026 Python Software Foundation

---

## Container base image and bundled packages

`ghcr.io/berriai/litellm` is built from a Chainguard/Wolfi base image and
bundles additional packages (Python runtime, `pgbouncer`, Node.js, Prisma, and
their transitive dependencies), each under its own licence. Those components
are used unmodified and are not redistributed from this repository; their
notices travel with the upstream image.

Before publishing a release, regenerate an accurate inventory (for example
with `syft ghcr.io/berriai/litellm:v1.106.6 -o spdx-json`) and attach it to the
release, since the exact package set changes with every upstream tag.
