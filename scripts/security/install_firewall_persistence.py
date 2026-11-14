"""Write reviewed native-service persistence files; activation remains separate."""

import argparse
import json
from pathlib import Path

from homeserver_common.public_persistence import install_persistence, persistence_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True,
                        help='Absolute installation root; use a staging directory for review')
    parser.add_argument('--role', choices=('home', 'edge'), required=True)
    parser.add_argument('--options-json', type=Path, required=True,
                        help='Validated policy arguments; never an application .env file')
    args = parser.parse_args()
    options = json.loads(args.options_json.read_text())
    if not isinstance(options, dict):
        parser.error('Policy options must be a JSON object')
    plan = persistence_plan(args.role, **options)
    install_persistence(args.root, plan)
    print(json.dumps({'files_installed': len(plan.files), 'services_activated': False,
                      'rules_applied': False}))


if __name__ == '__main__':
    main()
