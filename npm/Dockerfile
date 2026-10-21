FROM jc21/nginx-proxy-manager@sha256:52b2c59994f3d36acfcf70a1626f29734df0ed8c71bacc0269f78b6f939858bb
# Application overlay captured from the running 2.15.1-ddns.2 container.
ADD assets/runtime-overlay.tar.gz /
LABEL org.opencontainers.image.version="2.15.1-ddns.2"
ENV NPM_DDNS_VERSION=2.15.1-ddns.2
