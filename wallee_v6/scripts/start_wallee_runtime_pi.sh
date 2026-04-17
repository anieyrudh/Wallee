#!/bin/sh
set -eu
cd /home/b0/wallee_v6_live
set -a
. /home/b0/wallee_v6/.env
set +a
exec /home/b0/wallee_v6_live/.venv/bin/python -m wallee_v6.main --forever --goal 'Reduce stringing while keeping the current print running. Use one bounded action at a time.'
