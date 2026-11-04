"""Stop one exact controlled Linux series invocation; never clear a CRE fence."""

import argparse
import json
from cre_series_control import stop_series


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series-dir", required=True)
    parser.add_argument("--invocation-id", required=True)
    parser.add_argument("--wait-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    rc, result = stop_series(args.series_dir, args.invocation_id, args.wait_seconds)
    print(json.dumps(result, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
