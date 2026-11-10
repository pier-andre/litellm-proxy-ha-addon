FROM ghcr.io/berriai/litellm:v1.106.6

ARG BUILD_VERSION
ARG BUILD_ARCH

USER root

COPY run.py /ha-addon/run.py
COPY ui /ha-addon/ui
RUN chmod 0755 /ha-addon/run.py

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

EXPOSE 4000 8099

ENTRYPOINT []
CMD ["python3", "/ha-addon/run.py"]
