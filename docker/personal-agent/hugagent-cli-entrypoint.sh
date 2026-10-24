#!/bin/sh
exec /opt/python/current/bin/python3 -I -c 'import sys;sys.path.insert(0,"/opt/hugagent-cli");from hugagent_cli.main import main;raise SystemExit(main())' "$@"
