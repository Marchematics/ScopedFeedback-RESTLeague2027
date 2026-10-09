# Third-party dependencies and provenance

The `restleague/` implementation and synthetic `tests/` fixture/tests are original project code. This release contains no copied RESTgym implementation, official API implementation, third-party specification, benchmark dataset, raw API responses, or vendored dependency source.

The Docker recipes obtain Python from the Docker Official Python image and install PyYAML separately. Python, operating-system components in the base image, and PyYAML retain their upstream licenses. The project MIT license does not replace their terms. Distribution of a built image must preserve applicable upstream notices.

- Python license: https://docs.python.org/3/license.html
- Docker Official Python image source: https://github.com/docker-library/python
- PyYAML (MIT): https://github.com/yaml/pyyaml/blob/main/LICENSE

The public baseline source files were extracted byte-for-byte from project revision `c3a4d22d5ffff616e5caede58ca6956b9d1ca403`; fresh public-facing documentation and ignore files were added. `SOURCE_MANIFEST.json` lists SHA-256 hashes and byte lengths for each included file other than the manifest itself. Later observed-ID experiments and experiment archives are outside this release.
