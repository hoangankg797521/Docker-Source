#!/bin/sh
set -eu

install -m 0644 /opt/meshcentral/customization/meshuser.js /opt/meshcentral/meshcentral/meshuser.js
install -d -m 0755 /opt/meshcentral/meshcentral-web/views
install -m 0644 /opt/meshcentral/customization/views/default.handlebars /opt/meshcentral/meshcentral-web/views/default.handlebars
install -m 0644 /opt/meshcentral/customization/views/login2.handlebars /opt/meshcentral/meshcentral-web/views/login2.handlebars

node /opt/meshcentral/customization/prepare-runtime.js
exec /bin/bash /opt/meshcentral/entrypoint.sh "$@"
