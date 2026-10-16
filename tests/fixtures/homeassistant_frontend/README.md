# Home Assistant frontend test fixtures

These test-only sources are from Home Assistant frontend **20260826.7**,
under the Apache License 2.0 copied in [LICENSE.md](LICENSE.md).
`fire_event.ts` also retains its upstream Polymer BSD copyright/license notice.
The upstream Home Assistant frontend contributors retain their rights.

Sources from https://github.com/home-assistant/frontend/tree/20260826.7:

- `src/components/ha-form/compute-initial-ha-form-data.ts` (complete original)
- `src/components/ha-form/get-selector-initial-value.ts` (complete original)
- `src/components/ha-form/conditions.ts` (complete original)
- `src/dialogs/config-flow/step-flow-form.ts`, lines 213-226:
  `checkAllRequiredFields` is extracted with its body unchanged; only an
  import/export wrapper and this provenance notice were added.
- The same file, lines 211-296: `_submitStep` is extracted unchanged into
  `submit-step.ts`, with an EventTarget class/import/export wrapper for Node.
- `src/common/dom/fire_event.ts` (complete original)

The Node runner strips TypeScript types and resolves these local fixtures.
It executes the original initial-data and required-field functions against
HA-native serialized integration schemas. Tests never fetch upstream sources,
set up an integration account, or connect to a device.
The original submit method calls an in-memory backend boundary so clearing a
number must demonstrably pass frontend validation and produce a request.

With Python running HA 2026.9.4 and the integration's pinned dependencies,
and Node 22.13 or newer, run from the repository root:

```sh
python tests/frontend_options_required_schema.py | node tests/frontend_options_required.mjs
```

The existing isolated Docker HA environment can run the Python side instead;
keep `--network none`, the read-only workspace mount and cached `uv --offline`.
The JSON goes directly from the schema dumper to Node without a snapshot file.
Node may report that its built-in TypeScript type stripper is experimental;
this does not skip or change any assertion.
