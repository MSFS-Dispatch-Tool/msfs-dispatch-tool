"""Creates (or resets) a demo pilot account with a realistic logbook - the
command-line twin of the "Demo account" form on /admin/users (see demo.py).

    python tools/seed_demo_account.py --email you+vddemo@gmail.com --password 'choose-one'

Needs SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and DATABASE_URL in the
environment. Without --password a random one is generated and printed.
"""

import argparse
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402  (loads the active carriers' routes and fleets)
import demo  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password")
    parser.add_argument("--flights", type=int, default=170)
    parser.add_argument("--months", type=int, default=20)
    parser.add_argument("--base", default="EGKK")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    password = args.password or secrets.token_urlsafe(12)
    try:
        result = demo.seed_account(args.email, password, A.CARRIERS, flights=args.flights,
                                   months=args.months, base=args.base, seed=args.seed)
    except demo.DemoError as exc:
        sys.exit(str(exc))
    print(f"{'Created' if result['created'] else 'Reset'} demo account with {result['flights']} PIREPs")
    print(f"  email:    {args.email.strip()}")
    print(f"  password: {password}")
    print(f"  user id:  {result['user_id']}")


if __name__ == "__main__":
    main()
