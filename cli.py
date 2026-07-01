"""
Lightweight terminal view of the live data — a no-Streamlit alternative.

Reads the same database the collector writes (read-only) and prints the option
chain, headline metrics, ATM IV/Greeks, and recent signals. Useful when the
dashboard is laggy or you just want a fast snapshot.

    python -m cli                 # one snapshot of the first configured index
    python -m cli NIFTY           # a specific index
    python -m cli NIFTY --watch   # refresh every few seconds in place
    python -m cli NIFTY --watch --interval 3
"""

from __future__ import annotations

import argparse
import time

from analytics.chain_analytics import atm_iv, compute_chain_greeks
from analytics.indicators import build_chain_dataframe, find_atm_strike
from config.settings import get_settings
from dashboard.data import summarise_chain
from database.factory import make_database


def _snapshot(db, settings, index_name: str) -> None:
    rows = list(db.fetch_latest_option_chain(index_name))
    if not rows:
        print(f"[{index_name}] no data yet — is the collector (main.py) running, "
              f"and is the market open?")
        return

    spot_row = db.fetch_latest_spot(index_name)
    spot = float(spot_row["ltp"]) if spot_row else None
    chain = build_chain_dataframe(rows)
    summary = summarise_chain(chain, spot, max((r.get("ts", "") for r in rows), default=None))
    atm = summary.atm_strike

    print(f"\n===== {index_name} =====")
    print(f"Spot: {summary.spot}   ATM: {atm}   PCR: {summary.pcr}   "
          f"MaxPain: {summary.max_pain}")
    print(f"Total CE OI: {summary.total_ce_oi:,}   Total PE OI: {summary.total_pe_oi:,}   "
          f"Last tick: {summary.last_update}")

    # ATM IV (Black-76) if we can resolve an expiry.
    expiry = min((r.get("expiry", "") for r in rows if r.get("expiry")), default="")
    if expiry and spot:
        try:
            cg = compute_chain_greeks(chain, spot, settings.risk_free_rate,
                                      settings.dividend_yield, expiry)
            aiv = atm_iv(cg, atm)
            if aiv is not None:
                print(f"ATM IV: {aiv}%")
        except Exception:
            pass

    # Compact chain table (header + rows around ATM).
    print(f"\n{'CE_OI':>10} {'CE_LTP':>9} | {'STRIKE':>8} | {'PE_LTP':>9} {'PE_OI':>10}")
    print("-" * 56)
    for _, r in chain.iterrows():
        mark = " <ATM" if atm is not None and r["STRIKE"] == atm else ""
        print(f"{int(r['CE_OI']):>10,} {r['CE_LTP']:>9.2f} | {int(r['STRIKE']):>8} | "
              f"{r['PE_LTP']:>9.2f} {int(r['PE_OI']):>10,}{mark}")

    # Recent signals.
    signals = list(db.fetch_recent_signals(index_name, limit=3))
    if signals:
        print("\nRecent signals:")
        for s in signals:
            print(f"  {s['ts']}  {s['direction'].upper()} {s['kind']}  "
                  f"conf={s['confidence']} entry={s['entry']} SL={s['stop_loss']} "
                  f"T1={s['target1']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Terminal view of the option terminal.")
    parser.add_argument("index", nargs="?", default=None, help="Index name, e.g. NIFTY")
    parser.add_argument("--watch", action="store_true", help="Refresh continuously")
    parser.add_argument("--interval", type=int, default=5, help="Watch refresh seconds")
    args = parser.parse_args()

    settings = get_settings()
    index_name = (args.index or settings.indices[0]).upper()

    db = make_database(settings, read_only=True)
    db.connect()
    try:
        if not args.watch:
            _snapshot(db, settings, index_name)
        else:
            while True:
                print("\033[2J\033[H", end="")  # clear screen
                _snapshot(db, settings, index_name)
                print(f"\n(refreshing every {args.interval}s — Ctrl-C to stop)")
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
