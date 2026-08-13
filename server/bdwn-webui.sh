#!/bin/sh
# bdwn-webui - wrapper starting the status web UI of a bdwn backup server.
# All the arguments are passed through to the Python module.
exec python3 -m bdwn_webui "$@"
