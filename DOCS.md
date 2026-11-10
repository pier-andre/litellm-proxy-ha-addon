# LiteLLM Proxy for Home Assistant — Visual Router Manager

Version 0.4 adds a Home Assistant Ingress management UI for model selection, route membership, enable/disable controls, ordered fallback chains, and persistent model-test diagnostics. The add-on remains database-free: it stores small configuration files in the persistent `addon_config` directory and generates a normal LiteLLM YAML file underneath.

## What the Web UI manages

Open **LiteLLM Proxy → Open Web UI** in Home Assistant.

The **Routes** tab lets you:

- create logical model names such as `default-router`, `local-only`, `fast`, or `private`;
- turn a route on or off;
- toggle each configured model active/inactive inside a route;
- move models up/down to set fallback priority;
- test the currently applied route.

The **Models** tab lets you:

- add or remove model definitions;
- select the provider;
- set the provider model ID, API base, timeout, reasoning effort, and credential environment variable;
- mark local/free deployments as zero-cost metadata;
- run a harmless direct health/completion test against an individual model;
- see the last five direct-test outcomes and their recent median latency.

The **Generated YAML** tab shows the LiteLLM configuration produced from the visual state.

## Ordered fallbacks

The visual manager generates explicit hop-by-hop LiteLLM fallback mappings. For an active route ordered as:

```text
Groq → Gemini → NVIDIA → Ollama
```

the generated LiteLLM topology is equivalent to:

```yaml
fallbacks:
  - default-router:
      - __ui_<route>_2_gemini
  - __ui_<route>_2_gemini:
      - __ui_<route>_3_nvidia
  - __ui_<route>_3_nvidia:
      - __ui_<route>_4_ollama
```

The internal `__ui_...` names are generated implementation details. Clients continue requesting the route name, for example `default-router`.

This explicit chain avoids the cascading-fallback problem observed with a single mapping containing multiple targets on the LiteLLM version pinned by this add-on.

## Active/inactive models inside a route

A model definition can exist without participating in a route. Each route has its own active toggle for every model.

For example, if a route contains:

```text
✓ Groq
✗ Gemini
✓ NVIDIA
✓ Ollama
```

then the effective chain is:

```text
Groq → NVIDIA → Ollama
```

Gemini remains configured and can be re-enabled later without re-entering its model details.

## Provider credentials

Provider keys stay in the Home Assistant **Configuration** tab and are never displayed by the router UI. The UI only shows whether the selected credential environment variable is configured.

Available environment variables include:

```text
LITELLM_MASTER_KEY
GROQ_API_KEY
GEMINI_API_KEY
GOOGLE_API_KEY
NVIDIA_NIM_API_KEY
OPENAI_API_KEY
ANTHROPIC_API_KEY
OPENROUTER_API_KEY
XAI_API_KEY
MISTRAL_API_KEY
```

Model definitions reference these names, not the secret values.

## Supported provider helpers

The UI currently provides provider-aware model prefixing for:

- Groq → `groq/...`
- Gemini → `gemini/...`
- NVIDIA NIM → `nvidia_nim/...`
- Ollama → `ollama/...`
- OpenAI → `openai/...`
- Anthropic → `anthropic/...`
- OpenRouter → `openrouter/...`
- xAI → `xai/...`
- Mistral → `mistral/...`
- Custom → model string is passed through unchanged

Enter the provider-native model portion in the UI. For example, for NVIDIA NIM enter:

```text
z-ai/glm-5.3-flash
```

The generated LiteLLM model becomes:

```text
nvidia_nim/z-ai/glm-5.3-flash
```

## Persistence

Visual mode uses:

```text
/config/model_routes.json
```

as its source of truth and generates:

```text
/config/litellm.generated.yaml
```

Inside Home Assistant these files are backed by the add-on's persistent `addon_config` directory. They survive container rebuilds and are included with the add-on's configuration in backups.

The wrapper also keeps the most recent pre-apply backups:

```text
/config/model_routes.json.bak
/config/litellm.generated.yaml.bak
```

## Migration from v0.3.x

On the first v0.4 start, if `model_routes.json` does not exist and the configured advanced YAML file does exist, the wrapper attempts a one-time import.

It supports the common v0.3.x layout with:

- unique `model_name` values;
- normal `model_list` entries;
- explicit single-target fallback mappings.

When import succeeds:

1. the original YAML remains in place;
2. a `*.pre-v0.4-backup` copy is created;
3. equivalent model definitions and routes are created in `model_routes.json`;
4. the add-on starts in Visual Router Manager mode.

If the importer sees a topology it cannot preserve safely, such as duplicate `model_name` deployments used for load balancing, it does **not** guess. The add-on starts in **Advanced YAML** mode instead so the existing semantics are preserved.


## Per-model timeout and reasoning effort

The visual **Models** tab exposes both controls per deployment:

- **Timeout (s)** is written to `litellm_params.timeout`. If the provider does not answer before the deadline, LiteLLM can proceed according to the configured fallback topology.
- **Reasoning effort** is optional for providers where the pinned LiteLLM adapter accepts it. `Default` omits the parameter entirely.

For **NVIDIA NIM**, v0.4.3 and later deliberately disable explicit `reasoning_effort` in the visual editor and strip any previously saved value. The LiteLLM `nvidia_nim` adapter pinned by this add-on currently rejects the parameter before the request reaches NVIDIA, even for upstream models that may document reasoning controls. This is an adapter compatibility guard, not a statement about the underlying NVIDIA-hosted model capability.

The same normalized model definition is used by **Test model** and generated LiteLLM YAML.

## Why this add-on has its own UI

LiteLLM itself includes an Admin UI in the full AI Gateway. Its browser UI can add provider models and its Playground can test model-specific advanced request parameters. Upstream model management is database-backed, however. The official database-free LiteLLM mode uses a YAML configuration and does not provide Admin UI model management.

This add-on deliberately keeps PostgreSQL/Redis optional by storing visual router state in `addon_config` and generating ordinary LiteLLM YAML. Provider credentials remain Home Assistant Supervisor options.

## Advanced YAML mode

The Web UI includes a **Configuration mode** selector:

```text
Visual Router Manager
Advanced YAML
```

Advanced YAML is the escape hatch for LiteLLM features not represented by the visual editor. In that mode the file named by the Supervisor `config_filename` option is the active LiteLLM configuration.

The default is:

```text
/config/litellm_config.yaml
```

Secrets should still be referenced through `os.environ/...` rather than placed directly in YAML.

Switching back to Visual Router Manager makes `model_routes.json` and `litellm.generated.yaml` active again.

## Save & Apply and rollback

**Save & Apply** performs these steps:

1. validate model IDs, route names, timeouts, membership, and active routes;
2. generate and validate LiteLLM YAML;
3. write configuration atomically;
4. restart only the LiteLLM child process;
5. wait for `/health/liveliness`;
6. automatically restore the previous state/configuration if the new process fails to become healthy.

A failed apply therefore should not leave a previously working configuration replaced by an invalid one.

## Model and route tests

**Test model** sends a fixed harmless prompt (`Reply exactly with OK`) directly through LiteLLM's Python client using the selected provider definition. It does not use user content. Successful and failed tests both record elapsed time.

The add-on keeps up to 20 recent direct tests per model in:

```text
/config/model_test_history.json
```

The Web UI displays the most recent five outcomes, HTTP/error status, elapsed time, and the median latency across those five tests. This history survives add-on restarts and is diagnostic only; it is not sent to LiteLLM providers.

**Test applied route** calls the running local LiteLLM proxy using the route name. This validates the currently applied routing configuration rather than unsaved browser changes.

## Ingress security

The management UI uses Home Assistant Ingress on port `8099`. It is not published through the add-on `ports` section. The UI server accepts requests only from Home Assistant's documented Supervisor ingress gateway address (`172.30.32.2`). Home Assistant handles user authentication, and the sidebar panel is admin-only.

The LiteLLM OpenAI-compatible API remains available on port `4000` because external LAN clients may need to call it.

## API endpoint

```text
http://HOME_ASSISTANT_IP:4000/v1
```

Authenticate with the configured LiteLLM master key and request one of the enabled route names.

Health endpoint:

```text
http://HOME_ASSISTANT_IP:4000/health/liveliness
```

## No database

The package deliberately does not require PostgreSQL, Redis, or LiteLLM database-backed model management. Visual state is file-backed and the generated LiteLLM config remains inspectable.

## Home Assistant Configuration page

The Supervisor Configuration page contains only live settings: the LiteLLM master key, optional provider API keys, Advanced YAML filename, and debug switch. The old v0.2 migration-only provider/model/order/timeout fields have been removed from the add-on schema. Model IDs, API bases, timeouts, enable/disable state, and fallback order now belong in the Visual Router Manager.

## Updating the local add-on

Keep the same slug (`litellm_proxy`). Replace the files under `/addons/litellm_proxy/`, reload the Home Assistant app/add-on store, then rebuild/update the existing installation.

Do not uninstall first if you want to preserve Supervisor options and the persistent add-on configuration directory. Take a Home Assistant backup before a manual update.
