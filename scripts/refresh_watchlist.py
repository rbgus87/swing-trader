"""watchlist 수동 갱신.

Usage:
    python scripts/refresh_watchlist.py              # 조회만
    python scripts/refresh_watchlist.py --apply       # config.yaml 반영
    python scripts/refresh_watchlist.py --top-n 25    # 25종목
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.provider import get_provider


def main():
    parser = argparse.ArgumentParser(description="watchlist 수동 갱신")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--min-cap", type=int, default=5_000_000_000_000)
    parser.add_argument("--min-amount", type=int, default=10_000_000_000)
    parser.add_argument("--apply", action="store_true", help="config.yaml에 반영")
    args = parser.parse_args()

    provider = get_provider()
    result = provider.generate_watchlist(
        top_n=args.top_n,
        min_market_cap=args.min_cap,
        min_daily_amount=args.min_amount,
    )
    if not result:
        print("조건 충족 종목 없음")
        return

    print(f"\n선정 결과: {len(result)}종목\n")
    print(f"{'#':>3} {'코드':>8} {'종목명':<16} {'시총(조)':>8} {'거래대금(억)':>10} {'ATR%':>6}")
    print("-" * 60)
    for i, item in enumerate(result, 1):
        print(
            f"{i:3d} {item['code']:>8} {item['name']:<16} "
            f"{item['market_cap']/1e12:8.1f} {item['avg_amount']/1e8:10.0f} "
            f"{item['atr_pct']:6.2%}"
        )

    codes = [item["code"] for item in result]
    if args.apply:
        try:
            from ruamel.yaml import YAML

            yaml = YAML()
            yaml.preserve_quotes = True
            with open("config.yaml", "r", encoding="utf-8") as f:
                data = yaml.load(f)
            data["watchlist"] = codes
            with open("config.yaml", "w", encoding="utf-8") as f:
                yaml.dump(data, f)
            print(f"\nconfig.yaml watchlist 업데이트 ({len(codes)}종목)")
        except ImportError:
            print("\nruamel.yaml 미설치 — pip install ruamel.yaml")
    else:
        print("\n(--apply로 config.yaml 반영)")


if __name__ == "__main__":
    main()
