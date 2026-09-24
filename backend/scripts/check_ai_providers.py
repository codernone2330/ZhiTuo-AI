"""Opt-in live provider smoke test; never prints or stores API keys.

Run only with the desired *_API_KEY environment variable set. Each run sends
one short real request and may incur provider charges.
"""

import argparse
import os

from app.core.exceptions import AppError
from app.integrations.llm.client import MODELS, ask_provider


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=sorted(MODELS))
    parser.add_argument("model")
    args = parser.parse_args()
    if args.model not in MODELS[args.provider]:
        parser.error("model is not on this provider's allowlist")
    key = os.getenv(args.provider.upper() + "_API_KEY", "")
    if len(key) < 12:
        parser.error("provider API key environment variable is missing")
    try:
        result = ask_provider(
            args.provider,
            args.model,
            key,
            [{"role": "user", "content": "请只回复：连接成功"}],
        )
    except AppError as exc:
        print(f"{args.provider}/{args.model}: {exc.code} ({exc.message})")
        return 1
    print(f"{args.provider}/{args.model}: HTTP success, response chars={len(result['content'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
