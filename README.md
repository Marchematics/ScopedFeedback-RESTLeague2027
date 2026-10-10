# ScopedFeedback

## Complete plan-v3 package

The source on `main` is the historical baseline; the baseline documentation below is retained. For the complete plan-v3 source, Dockerfiles, installation instructions, tests, and offline PyYAML wheel, download the explicitly uploaded [ScopedFeedback-plan-v3-release-ready.zip](https://github.com/Marchematics/ScopedFeedback-RESTLeague2027/releases/download/v0.3.0-plan-v3-artifact/ScopedFeedback-plan-v3-release-ready.zip) (840,260 bytes) from the [plan-v3 artifact release](https://github.com/Marchematics/ScopedFeedback-RESTLeague2027/releases/tag/v0.3.0-plan-v3-artifact).

Expected SHA-256: `24e5c73753671094ac68a1be6f8e6bcc774a7b43b4897e36467fea07f8b61940`

GitHub's automatically generated **Source code (zip)** and **Source code (tar.gz)** archives for that tag contain the historical baseline, not the complete plan-v3 package. Use the uploaded ZIP linked above.

CPU-only black-box REST API test generation for REST League 2027.

Author: **Jiahao Zhang**, Zhengzhou University of Aeronautics.

## Release status

This public source release contains the conservative `scoped-feedback` baseline and its `independent` ablation. The implementation and 36 regression tests were extracted unchanged from source revision `c3a4d22d5ffff616e5caede58ca6956b9d1ca403`. All 36 tests passed again on 2026-10-09, including the owned loopback fixture. Later experimental policies are not included.

This exact public Docker recipe has not yet received a verified build/run acceptance. A separate development image required packaging changes; its successful build is not attributed to this source release. Full privileged RESTgym integration, organizer Restats metrics, and competition acceptance remain pending. No official rank, SOTA result, or effectiveness advantage is claimed.

## Method

The generator reads a local OpenAPI 3.x or Swagger 2.0 JSON/YAML specification. It orders creation and shallow operations before deeper requests and deletion. Successful top-level response scalar values are cached by resource and bound parent-resource scope, up to 64 values per field. Schema examples and defaults take precedence over feedback. The independent ablation uses the same scheduling and generation logic without response reuse.

The tool uses no LLM, remote inference, target implementation source, attack-payload library, or online schema retrieval. Runtime requests are confined to the supplied target origin; redirects and ambient proxies are not used.

## Local use

Use Python 3.12. Test only a sandbox you own or have explicit authorization to test.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -v
python -m restleague --spec my-openapi.json --base-url http://127.0.0.1:8080 \
  --max-requests 120 --seconds 40 --rate 20 --out output/local
```

Choose `--mechanism independent` for the ablation. Loopback is allowed by default; non-loopback use requires an exact `--allow-target-host HOST`. The supplied target overrides specification server hosts. External schema references are rejected; bundle them locally first.

## Container recipe

The following are build/run instructions, not evidence that this exact recipe has passed container acceptance:

```sh
docker build -t scoped-feedback-cpu:local .
mkdir -p output
docker run --rm --network host \
  -v "$PWD/my-openapi.json:/specifications/api.json:ro" \
  -v "$PWD/output:/output" \
  -e HOST=127.0.0.1 -e PORT=8080 \
  -e OPENAPI_SPEC=/specifications/api.json -e TIME_BUDGET=1 \
  scoped-feedback-cpu:local
```

The adapter repeats time-budgeted cycles until the organizer terminates the container. `TIME_BUDGET` is minutes per cycle, not a total process lifetime. Use an external Docker/RESTgym resource and lifetime limit. The CLI instead stops at its request or time limit.

## RESTgym integration

Place this checkout at `tools/scoped-feedback-cpu/` inside the official RESTgym repository. Replace that tool directory's `Dockerfile` with `Dockerfile.restgym` and keep `restgym-tool-config.yml` at `tools/scoped-feedback-cpu/restgym-tool-config.yml`. RESTgym builds with its repository root as context, making `apis/*/specifications/*` available. Flattened specification basenames must be unique. This repository does not redistribute organizer APIs, benchmark data, or third-party specifications.

Environment inputs:

- `HOST`, `PORT`: required organizer-authorized sandbox target
- `API`: selects `/specifications/$API-openapi.json`, `/specifications/$API.yaml`, or `/specifications/$API.yml`
- `OPENAPI_SPEC`: optional explicit local specification file
- `TIME_BUDGET`: minutes per cycle, default 60
- `BASE_PATH`, `SCHEME`: optional path/transport overrides; default scheme is HTTP
- `SEED`, `RATE`, `OUTPUT_DIR`: defaults 20261009, 20 requests/second, `/output`
- `RESTLEAGUE_AUTH_HEADERS`, `RESTLEAGUE_AUTH_QUERY`: optional JSON maps supplied securely at runtime; never bake credentials into an image or commit them

Each adapter launch creates a fresh session directory. Each cycle writes `http.jsonl` and `summary.json`; redacted attempt records also go to stdout for the organizer's capture. Secret-key heuristics and authentication-value redaction are not a complete personal-data anonymizer. Use synthetic data, inspect traces before sharing, and retain organizer proxy traces separately. Opaque non-JSON bodies and most response headers are omitted from local logs.

## Evidence and limitations

A historical bounded native PetClinic pilot used six fresh runs, three seeds and two mechanisms, totaling 840 requests. Both mechanisms reached 33 of 35 operations with a 2xx response in every run and produced identical request sequences. Examples/defaults prevented feedback activation. This is a negative mechanism result and does not establish a competitive gain. The historical pilot predates later logging fixes in this release; it is not a new evaluation of this exact public commit. Raw API responses and private experiment archives are not distributed here.

Supported generation includes common scalar/array/object parameters, local references, JSON bodies, and URL-encoded forms. Multipart bodies, regex-constrained generation, complex composition/discriminators, complex cookie parameters, non-simple path/header styles, and per-operation server overrides are incomplete or rejected. Internal operation coverage and 5xx signatures are not official Restats metrics, code coverage, or unique faults.

## License and references

Original tool code is released under the MIT License; see `LICENSE` and `THIRD_PARTY.md`.

- Competition instructions: https://github.com/SeUniVr/RestLeague/blob/main/2027/README.md
- RESTgym tool contract: https://github.com/restgym/restgym/blob/main/tools/%23tool-template/README.md
- Submission summary: [SUBMISSION.md](SUBMISSION.md)
