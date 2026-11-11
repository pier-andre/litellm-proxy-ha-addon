# Home Assistant LiteLLM add-on v0.4.5

A database-free LiteLLM gateway with a Home Assistant Ingress **Visual Router Manager**.

The UI manages model definitions, logical routes, per-route active/inactive model toggles, ordered fallbacks, and recent model-test latency/status history. Secrets remain in Home Assistant Supervisor configuration; the router manager stores only environment-variable references.

The add-on automatically attempts to import a compatible v0.3.x `litellm_config.yaml` on first start and preserves the original YAML plus a backup. An Advanced YAML mode remains available for configurations the visual importer cannot safely represent.

## Install

This repository is a Home Assistant app repository: `repository.yaml` sits at the root, and the add-on lives in `litellm_proxy/` — the folder name is the add-on slug, and the folder holds `config.yaml`, `CHANGELOG.md` (shown in the add-on's Changelog tab) and `DOCS.md` (shown in its Documentation tab).

1. In Home Assistant: **Settings → Apps → App store** → ⋮ → **Repositories** → add `https://github.com/pier-andre/litellm-proxy-ha-addon`. The repository must stay public: the Supervisor clones it without credentials.
2. Install **LiteLLM Proxy** from the store and start it. The image is built on your device on top of the pinned upstream LiteLLM image, so the first install is slow but no prebuilt image is published (see the licence note below).
3. Home Assistant installs it as `<hash>_litellm_proxy`, so the persistent configuration is `/addon_configs/<hash>_litellm_proxy/`, mounted at `/config` inside the add-on. Coming from a local `/addons` copy means moving `/addon_configs/local_litellm_proxy/` to that path.

For local development, copy the contents of `litellm_proxy/` to `/addons/litellm_proxy/`, reload the app store, then rebuild/update the existing installation. Keep the same slug and do not uninstall first if you want to retain Supervisor options and the persistent configuration directory.

## Updates

`renovate.json` at the repository root is read by the [Renovate add-on](https://github.com/pier-andre/renovate-ha-addon). Each dependency pull request also increments `version:` in `litellm_proxy/config.yaml` and adds a line to `litellm_proxy/CHANGELOG.md`, which is what makes Home Assistant offer the update once the pull request is merged.

## License

This add-on is released under the MIT License. Copyright (c) 2026 Pier-Andre Maynard. See [LICENSE](LICENSE).

Third-party components — the upstream LiteLLM image, PyYAML and the Python runtime — are documented in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). Do not publish prebuilt images of this add-on: the upstream LiteLLM image also bundles code under the BerriAI Enterprise License, which does not permit redistribution.
