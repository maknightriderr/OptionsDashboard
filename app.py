"""
Phase 2 dashboard — a READ-ONLY view over the collector's database.

Run it as its own process (separate from main.py)::

    streamlit run dashboard/app.py

It never writes; it opens the SQLite file in query-only mode and re-reads the
latest option chain on an interval using a Streamlit fragment. The collector
(main.py) must be running and populating the database for data to appear.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# Allow `streamlit run dashboard/app.py` to import sibling packages.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings              # noqa: E402
from analytics.chain_analytics import (                # noqa: E402
    atm_iv,
    compute_chain_greeks,
    gamma_exposure,
    iv_percentile,
    iv_rank,
)
from dashboard.data import (                           # noqa: E402
    build_chain_dataframe,
    summarise_chain,
)
from database.factory import make_database             # noqa: E402
from database.interface import Database                # noqa: E402
from backtest.datasource import DatabaseDataSource     # noqa: E402
from backtest.engine import Backtester                 # noqa: E402
from backtest.models import BacktestConfig             # noqa: E402
from signals.engine import SignalEngine                # noqa: E402

st.set_page_config(
    page_title="Option Terminal",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def get_database() -> Database:
    """Open a single shared read-only connection for the dashboard process."""
    settings = get_settings()
    db = make_database(settings, read_only=True)
    db.connect()
    return db


def _fmt(value: float | None, nd: int = 2) -> str:
    return "—" if value is None else f"{value:,.{nd}f}"


def _style_chain(chain: pd.DataFrame, atm: float | None) -> "pd.io.formats.style.Styler":
    """Highlight the ATM row and tint the OI columns for quick scanning."""
    def highlight_atm(row: pd.Series) -> list[str]:
        if atm is not None and row["STRIKE"] == atm:
            return ["background-color: rgba(250, 204, 21, 0.18)"] * len(row)
        return [""] * len(row)

    styler = chain.style.apply(highlight_atm, axis=1)
    int_cols = ["CE_OI", "CE_OI_CHG", "CE_VOL", "PE_VOL", "PE_OI_CHG", "PE_OI", "STRIKE"]
    styler = styler.format({c: "{:,.0f}" for c in int_cols})
    styler = styler.format({"CE_LTP": "{:,.2f}", "PE_LTP": "{:,.2f}"})
    # Light green for call OI, light red for put OI — the usual chain convention.
    styler = styler.background_gradient(cmap="Greens", subset=["CE_OI"])
    styler = styler.background_gradient(cmap="Reds", subset=["PE_OI"])
    return styler


def render_greeks(db: Database, index_name: str, chain, spot, atm, rows) -> None:
    """Greeks tab: per-strike IV/Greeks table, IV smile, gamma exposure, IV rank."""
    settings = get_settings()
    expiry = min((r.get("expiry", "") for r in rows if r.get("expiry")), default="")
    if not expiry or spot is None:
        st.info("Greeks need spot and expiry data — waiting for the collector.")
        return

    cg = compute_chain_greeks(
        chain, spot, settings.risk_free_rate, settings.dividend_yield, expiry
    )
    if cg.empty:
        st.info("Could not compute Greeks for this snapshot yet.")
        return

    current_atm_iv = atm_iv(cg, atm)
    history = list(db.fetch_iv_history(index_name, limit=500))
    rank = iv_rank(current_atm_iv, history) if current_atm_iv is not None else None
    pct = iv_percentile(current_atm_iv, history) if current_atm_iv is not None else None

    m1, m2, m3 = st.columns(3)
    m1.metric("ATM IV %", _fmt(current_atm_iv, 2))
    m2.metric("IV Rank", _fmt(rank, 1))
    m3.metric("IV Percentile", _fmt(pct, 1))

    st.markdown("**IV smile**")
    smile_df = cg[["STRIKE", "CE_IV", "PE_IV"]].set_index("STRIKE")
    st.line_chart(smile_df)

    st.markdown("**Gamma exposure (per strike)**")
    gex = gamma_exposure(cg, spot, contract_size=1.0)
    if not gex.per_strike.empty:
        st.bar_chart(gex.per_strike.set_index("STRIKE"))
        cap = f"Net GEX: {gex.total:,.0f}"
        if gex.flip_strike is not None:
            cap += f"  ·  approx flip strike: {gex.flip_strike:,.0f}"
        st.caption(cap + "  (×lot size for notional)")

    st.markdown("**Per-strike Greeks**")
    st.dataframe(cg, use_container_width=True, hide_index=True,
                 height=min(60 + 35 * len(cg), 520))


def render_overview(summary, index_name: str) -> None:
    """Sidebar market overview block."""
    st.sidebar.markdown(f"### {index_name}")
    st.sidebar.metric("Spot", _fmt(summary.spot))
    st.sidebar.metric("ATM Strike", _fmt(summary.atm_strike, 0))
    st.sidebar.metric("PCR (OI)", _fmt(summary.pcr, 3))
    st.sidebar.metric("Max Pain", _fmt(summary.max_pain, 0))
    st.sidebar.caption(f"Contracts: {summary.contracts}")
    if summary.last_update:
        st.sidebar.caption(f"Last tick: {summary.last_update}")


def render_signals(db: Database, index_name: str, limit: int = 8) -> None:
    """Render the most recent signals as colour-coded cards."""
    try:
        rows = list(db.fetch_recent_signals(index_name, limit=limit))
    except Exception:  # noqa: BLE001 - signals table may not exist yet
        rows = []
    st.subheader("Signals")
    if not rows:
        st.caption("No signals yet — run the signal engine (`python -m signals.runner`).")
        return

    for r in rows:
        bullish = r["direction"] == "bullish"
        colour = "#16a34a" if bullish else "#dc2626"
        arrow = "▲" if bullish else "▼"
        try:
            supporting = ", ".join(json.loads(r.get("supporting", "[]")))
        except (ValueError, TypeError):
            supporting = ""
        with st.container(border=True):
            head, scores = st.columns([3, 2])
            head.markdown(
                f"<span style='color:{colour};font-weight:700'>{arrow} "
                f"{r['direction'].upper()} · {r['kind'].replace('_', ' ').title()}</span>",
                unsafe_allow_html=True,
            )
            head.caption(r.get("ts", ""))
            scores.markdown(
                f"**Conf** {r['confidence']} · **Risk** {r['risk']} · **Prob** {r['probability']}"
            )
            e1, e2, e3, e4, e5 = st.columns(5)
            e1.metric("Entry", f"{r['entry']:,.1f}")
            e2.metric("Stop", f"{r['stop_loss']:,.1f}")
            e3.metric("T1", f"{r['target1']:,.1f}")
            e4.metric("T2", f"{r['target2']:,.1f}")
            e5.metric("T3", f"{r['target3']:,.1f}")
            st.caption(r.get("reason", ""))
            if supporting:
                st.caption(f"Indicators: {supporting}")


def main() -> None:
    settings = get_settings()
    db = get_database()

    # ---- sidebar controls ---------------------------------------------------
    st.sidebar.title("📈 Option Terminal")
    try:
        available = list(db.fetch_available_indices()) or settings.indices
    except Exception:  # DB file may not exist yet on first launch
        available = settings.indices
    index_name = st.sidebar.selectbox("Index", available, index=0)
    interval = st.sidebar.slider("Refresh every (sec)", 1, 30, 3)
    st.sidebar.divider()

    st.title(f"{index_name} — Live Option Chain")

    # ---- live region --------------------------------------------------------
    @st.fragment(run_every=f"{interval}s")
    def live_chain() -> None:
        try:
            rows = list(db.fetch_latest_option_chain(index_name))
            spot_row = db.fetch_latest_spot(index_name)
        except Exception as exc:  # noqa: BLE001
            st.warning(
                "Could not read the database yet. Is the collector (main.py) "
                f"running?\n\n`{exc}`"
            )
            return

        if not rows:
            st.info("No option data yet — waiting for the collector to populate ticks.")
            return

        spot = float(spot_row["ltp"]) if spot_row else None
        last_update = max((r.get("ts", "") for r in rows), default=None)
        chain = build_chain_dataframe(rows)
        summary = summarise_chain(chain, spot, last_update)

        render_overview(summary, index_name)

        # Headline metrics.
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Spot", _fmt(summary.spot))
        c2.metric("PCR (OI)", _fmt(summary.pcr, 3))
        c3.metric("Max Pain", _fmt(summary.max_pain, 0))
        c4.metric("Total CE OI", f"{summary.total_ce_oi:,}")
        c5.metric("Total PE OI", f"{summary.total_pe_oi:,}")

        tab_chain, tab_greeks, tab_signals = st.tabs(
            ["Option Chain", "Greeks", "Signals"]
        )

        with tab_chain:
            st.dataframe(
                _style_chain(chain, summary.atm_strike),
                use_container_width=True,
                hide_index=True,
                height=min(60 + 35 * len(chain), 720),
            )
            st.caption("CE (calls) left · STRIKE centre · PE (puts) right. ATM row highlighted.")

        with tab_greeks:
            render_greeks(db, index_name, chain, summary.spot, summary.atm_strike, rows)

        with tab_signals:
            render_signals(db, index_name)
            with st.expander("Recent alerts"):
                try:
                    alert_rows = list(db.fetch_recent_alerts(index_name, limit=10))
                except Exception:  # noqa: BLE001 - alerts table may not exist yet
                    alert_rows = []
                if not alert_rows:
                    st.caption("No alerts yet.")
                else:
                    _PRI = {0: "LOW", 1: "MEDIUM", 2: "HIGH", 3: "CRITICAL"}
                    st.dataframe(
                        [
                            {
                                "time": a.get("ts", ""),
                                "priority": _PRI.get(a.get("priority"), a.get("priority")),
                                "direction": a.get("direction"),
                                "kind": a.get("kind"),
                                "conf": a.get("confidence"),
                                "status": a.get("status"),
                                "channel": a.get("channel"),
                            }
                            for a in alert_rows
                        ],
                        use_container_width=True,
                        hide_index=True,
                    )

    live_chain()

    # ---- backtest (on-demand; outside the auto-refresh fragment) ------------
    st.divider()
    with st.expander("🔬 Backtest — replay stored history through the signal engine"):
        c1, c2 = st.columns(2)
        interval = c1.number_input("Eval interval (sec)", 30, 3600, 60, step=30)
        target_idx = c2.selectbox("Exit on target", [1, 2, 3], index=0)
        if st.button("Run backtest", type="primary"):
            with st.spinner("Replaying history…"):
                source = DatabaseDataSource(db)
                bt = Backtester(
                    SignalEngine(),
                    source,
                    BacktestConfig(eval_interval_sec=int(interval), target_index=int(target_idx)),
                )
                report = bt.run(index_name)
            if not report.trades:
                st.info("No trades generated over the stored window yet — "
                        "let the collector and signal runner gather more history.")
            else:
                m = report.metrics
                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Trades", m["trades"])
                k2.metric("Win rate %", m["win_rate"])
                k3.metric("Expectancy R", f"{m['expectancy_r']:+.2f}")
                k4.metric("Total R", f"{m['total_r']:+.1f}")
                st.markdown("**Equity curve (cumulative R)**")
                st.line_chart({"cumulative R": report.equity_curve})
                st.markdown("**Insights**")
                for line in report.insights:
                    st.markdown(f"- {line}")
                st.markdown("**Trades**")
                st.dataframe(
                    [
                        {
                            "entry_ts": t.entry_ts, "dir": t.signal.direction.value,
                            "kind": t.signal.kind.value, "entry": round(t.entry, 1),
                            "exit": round(t.exit_price, 1), "reason": t.exit_reason.value,
                            "R": round(t.realized_r, 2),
                        }
                        for t in report.trades
                    ],
                    use_container_width=True, hide_index=True,
                )
        st.caption("Simulated on stored ticks — evaluation aid, not trading advice.")


if __name__ == "__main__":
    main()
