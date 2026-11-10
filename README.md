# Home Assistant LiteLLM add-on v0.4.4

A database-free LiteLLM gateway with a Home Assistant Ingress **Visual Router Manager**.

The UI manages model definitions, logical routes, per-route active/inactive model toggles, ordered fallbacks, and recent model-test latency/status history. Secrets remain in Home Assistant Supervisor configuration; the router manager stores only environment-variable references.

The add-on automatically attempts to import a compatible v0.3.x `litellm_config.yaml` on first start and preserves the original YAML plus a backup. An Advanced YAML mode remains available for configurations the visual importer cannot safely represent.

For a local install, copy the contents of `litellm_proxy/` to `/addons/litellm_proxy/`, reload the Home Assistant app/add-on store, then rebuild/update the existing LiteLLM Proxy installation. Keep the same slug and do not uninstall first if you want to retain existing options and persistent configuration.

## License

This add-on is released under the MIT License. Copyright (c) 2026 Pier-Andre Maynard. See [LICENSE](LICENSE).

Third-party components — the upstream LiteLLM image, PyYAML and the Python runtime — are documented in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). Do not publish prebuilt images of this add-on: the upstream LiteLLM image also bundles code under the BerriAI Enterprise License, which does not permit redistribution.
