# ScopedFeedback: Scoped Response Reuse for CPU-Only Black-Box REST API Testing

**Author:** Jiahao Zhang  
**Affiliation:** Zhengzhou University of Aeronautics  
**Tool:** ScopedFeedback  
**Source:** https://github.com/Marchematics/ScopedFeedback-RESTLeague2027

## Abstract

ScopedFeedback is a lightweight, CPU-only black-box REST API test generator. It consumes a local OpenAPI specification and an authorized API endpoint, generates schema-informed HTTP requests, and reuses successful response scalar values within resource and bound parent-resource scopes. Its deterministic scheduler prioritizes creation and shallow paths, while bounded response pools support cross-request dependencies without inspecting the target implementation. The released baseline preserves specification examples and defaults ahead of feedback and includes an independent-generation ablation using the same scheduler. A RESTgym adapter accepts organizer endpoint and time-budget inputs and exports HTTP-attempt traces.

A bounded native PetClinic pilot comprising six runs and 840 requests reached 33 of 35 operations with 2xx responses in both modes. The modes generated identical sequences because specification hints bypassed feedback, so the pilot does not demonstrate an effectiveness gain. The public release includes source, container recipes, and 36 passing regression tests. Full RESTgym acceptance and official Restats evaluation remain pending. No official ranking or state-of-the-art claim is made.

## Evaluation handoff

- Language/runtime: Python 3.12; runtime Python package PyYAML 6.0.3
- Standalone build: `docker build -t scoped-feedback-cpu:local .`
- RESTgym build: use `Dockerfile.restgym` as described in README
- Entrypoint: `python -m restleague.entrypoint`
- Default strategy: `scoped-feedback`; default rate 20 requests/second
- Authentication: runtime-only JSON header/query mappings, described in README
- Resource control: organizer/container CPU, memory, and total runtime limits; adapter cycles continue until termination
- Current limitations: exact public recipe container build/run and complete official integration not yet verified

Publication of this source package does not itself mean that the organizer has received or accepted a submission.
