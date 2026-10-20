ARG MESHCENTRAL_VERSION=1.2.6
FROM ghcr.io/ylianst/meshcentral:${MESHCENTRAL_VERSION}

USER root

# Keep our custom files outside mounted data paths in the image. The wrapper
# refreshes them into the MeshCentral paths before starting the upstream entrypoint.
COPY --chmod=0644 custom/meshuser.js /opt/meshcentral/customization/meshuser.js
COPY --chmod=0644 custom/prepare-runtime.js /opt/meshcentral/customization/prepare-runtime.js
COPY --chmod=0644 custom/views/ /opt/meshcentral/customization/views/
COPY --chmod=0755 docker-entrypoint.sh /opt/meshcentral/custom-entrypoint.sh

ENTRYPOINT ["/bin/sh", "/opt/meshcentral/custom-entrypoint.sh"]
