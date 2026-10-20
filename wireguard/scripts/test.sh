#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
ACL_SCOPE='192.168.50.0/23' python3 -m unittest discover -s acl/tests -v
python3 -m unittest discover -s noc-dashboard/tests -v
node noc-dashboard/tests/acl-ui.test.cjs noc-dashboard/static/acl.js
node noc-dashboard/tests/interactions.test.cjs noc-dashboard/static/app.js
