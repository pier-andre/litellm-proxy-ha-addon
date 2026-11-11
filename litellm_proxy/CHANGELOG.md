# Changelog

## Non publié

## 0.4.5

- Moved the add-on into `litellm_proxy/`, next to its `CHANGELOG.md`: the folder name is the slug, which is what the app store and the repository's `renovate.json` expect.
- Added `renovate.json` at the repository root: dependency pull requests bump `version:` and add a line to this changelog.
- Repository URL in `config.yaml` now points at the add-on folder.

## 0.4.4
- bump to v1.106.6

## 0.4.3

- Integrated the mobile-responsive Ingress UI refinements as the maintained UI baseline.
- Added persistent per-model direct-test history with the latest five outcomes and recent median latency in the Web UI.
- Direct model and route test failures now report elapsed time as well as status/error information.
- NVIDIA NIM model definitions now force `reasoning_effort` to Default/omitted because the pinned LiteLLM `nvidia_nim` adapter rejects the explicit parameter.
- The NVIDIA reasoning selector is disabled in the visual editor with an explanatory hint.
- Removed all v0.2 migration-only model/provider/order/timeout fields from the Home Assistant add-on Configuration schema and translations.
- Removed the corresponding obsolete compatibility environment exports from the wrapper.

## 0.4.1

- Added per-model **Reasoning effort** in the visual model editor.
- Supported values: Default (parameter omitted), none, minimal, low, medium, high, xhigh, and max. Provider/model support varies.
- Existing per-model timeout control remains available and is now shown alongside reasoning effort in route members.
- Imported YAML preserves `litellm_params.reasoning_effort` when present.
- Generated YAML and direct model tests apply the selected `reasoning_effort`.
- Added documentation clarifying the database-free custom UI versus LiteLLM's upstream Admin UI.

## 0.4.0

- Added a Home Assistant Ingress Visual Router Manager on port 8099.
- Added model definition CRUD in the Web UI.
- Added logical routing groups with per-group active/inactive model toggles.
- Added up/down fallback ordering controls.
- Added explicit hop-by-hop fallback generation for deterministic cascading behavior.
- Added direct model tests with a fixed non-user prompt.
- Added applied-route tests against the running local LiteLLM proxy.
- Added generated-YAML preview.
- Added Advanced YAML mode as an escape hatch.
- Added one-time import of compatible v0.3.x YAML configurations.
- Preserves the original YAML and creates a pre-v0.4 backup during migration.
- Added atomic Save & Apply with LiteLLM child-process restart, health check, and automatic rollback.
- Provider secrets remain Supervisor options and are never returned to the Web UI.
- Management UI is Ingress-only and admin-only; port 8099 is not published.
- Updated the pinned LiteLLM image within the 1.100 stable line from v1.100.1 to v1.100.3.
- No database added.

## 0.3.1

- Added explicit chained-fallback migration and documentation.
- Added fallback source/target validation, cycle detection, and placeholder detection.
- Corrected Gemini migration normalization.
