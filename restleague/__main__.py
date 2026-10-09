import argparse
import json
import os
from .core import load_spec, run


def main():
    p = argparse.ArgumentParser(description='Conservative CPU-only OpenAPI request testing for authorized sandbox APIs')
    p.add_argument('--spec', required=True, help='Local JSON/YAML OpenAPI 2/3 document')
    p.add_argument('--base-url', required=True, help='Authorized API origin and optional base path; overrides spec server')
    p.add_argument('--out', default='results/manual')
    p.add_argument('--seed', type=int, default=20261009)
    p.add_argument('--mechanism', choices=['independent', 'scoped-feedback'], default='scoped-feedback')
    p.add_argument('--max-requests', type=int, default=120, help='0 means time-limited only')
    p.add_argument('--seconds', type=float, default=60)
    p.add_argument('--rate', type=float, default=20)
    p.add_argument('--allow-target-host', help='Exact non-loopback host explicitly authorized by operator')
    args = p.parse_args()
    headers = json.loads(os.environ.get('RESTLEAGUE_AUTH_HEADERS', '{}'))
    auth_query = json.loads(os.environ.get('RESTLEAGUE_AUTH_QUERY', '{}'))
    result = run(load_spec(args.spec), args.base_url, args.out, args.seed, args.mechanism,
                 args.max_requests, args.seconds, args.rate, args.allow_target_host, headers, auth_query)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
