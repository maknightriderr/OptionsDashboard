"""
Interactive Telegram command bot (Module 8).

Runs as its own process and answers commands by reading the same database the
collector/engine write. It is read-only with respect to market data.

    python -m alerts.telegram_bot

Requires python-telegram-bot (v21+):  pip install "python-telegram-bot>=21"
The import is lazy so the rest of the project does not depend on it.

Implemented now (data-backed): /start /help /status /signals /pcr /maxpain
/settings. Commands tied to features from later phases (/trades /open /history
/chart) reply with a clear "not available yet" message rather than failing.
"""

from __future__ import annotations

import logging

from alerts.formatting import format_signal_html
from alerts.models import Priority, map_priority
from analytics.indicators import build_chain_dataframe, compute_max_pain, compute_pcr
from config.settings import Settings, get_settings
from database.factory import make_database
from database.interface import Database
from signals.models import Direction, Signal, SignalKind
from utils.logging import configure_logging

logger = logging.getLogger(__name__)

_HELP = (
    "<b>Option Terminal Bot</b>\n"
    "/status — collector/data health\n"
    "/signals [INDEX] — latest signals\n"
    "/pcr [INDEX] — current put-call ratio\n"
    "/maxpain [INDEX] — current max pain\n"
    "/settings — alert configuration\n"
    "/help — this message\n\n"
    "<i>Heuristic analytics — not trading advice.</i>"
)


def _default_index(settings: Settings, args: list[str]) -> str:
    return args[0].upper() if args else settings.indices[0]


def _signal_from_row(row: dict) -> Signal:
    """Rehydrate a stored signal row into a Signal for re-formatting."""
    import json
    return Signal(
        index_name=row["index_name"],
        direction=Direction(row["direction"]),
        kind=SignalKind(row["kind"]),
        spot=row["spot"],
        confidence=row["confidence"], risk=row["risk"], probability=row["probability"],
        entry=row["entry"], stop_loss=row["stop_loss"],
        target1=row["target1"], target2=row["target2"], target3=row["target3"],
        reason=row["reason"],
        supporting_indicators=json.loads(row.get("supporting", "[]")),
    )


def build_application(settings: Settings, db: Database):
    """Construct the PTB Application with handlers bound to ``db`` and ``settings``."""
    from telegram import Update                       # lazy import
    from telegram.ext import Application, CommandHandler, ContextTypes

    token, chat_id = settings.resolve_telegram()
    if not token:
        raise RuntimeError("OT_TELEGRAM_BOT_TOKEN is not set.")

    def _authorized(update: "Update") -> bool:
        # If a chat id is configured, only serve that chat.
        if not chat_id:
            return True
        return str(update.effective_chat.id) == str(chat_id)

    async def start(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not _authorized(update):
            return
        await update.message.reply_html(
            "👋 Option Terminal bot is online.\n\n" + _HELP
        )

    async def help_cmd(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not _authorized(update):
            return
        await update.message.reply_html(_HELP)

    async def status(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not _authorized(update):
            return
        lines = ["<b>Status</b>"]
        for name in settings.indices:
            spot = db.fetch_latest_spot(name)
            if spot:
                lines.append(f"{name}: spot {spot['ltp']:,.2f} (last {spot['ts']})")
            else:
                lines.append(f"{name}: no data yet")
        await update.message.reply_html("\n".join(lines))

    async def signals(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not _authorized(update):
            return
        name = _default_index(settings, ctx.args)
        rows = list(db.fetch_recent_signals(name, limit=3))
        if not rows:
            await update.message.reply_html(f"No signals for {name} yet.")
            return
        for row in rows:
            sig = _signal_from_row(row)
            await update.message.reply_html(format_signal_html(sig, map_priority(sig)))

    async def pcr(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not _authorized(update):
            return
        name = _default_index(settings, ctx.args)
        chain = build_chain_dataframe(list(db.fetch_latest_option_chain(name)))
        value = compute_pcr(chain)
        await update.message.reply_html(
            f"<b>{name} PCR:</b> {value}" if value is not None else f"No chain data for {name}."
        )

    async def maxpain(update: "Update", ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not _authorized(update):
            return
        name = _default_index(settings, ctx.args)
        chain = build_chain_dataframe(list(db.fetch_latest_option_chain(name)))
        value = compute_max_pain(chain)
        await update.message.reply_html(
            f"<b>{name} Max Pain:</b> {value:,.0f}" if value is not None
            else f"No chain data for {name}."
        )

    async def settings_cmd(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not _authorized(update):
            return
        await update.message.reply_html(
            "<b>Alert settings</b>\n"
            f"Indices: {', '.join(settings.indices)}\n"
            f"Min priority: {settings.alert_min_priority}\n"
            f"Cooldown: {settings.alert_cooldown_sec}s"
        )

    async def not_yet(update: "Update", _ctx: "ContextTypes.DEFAULT_TYPE") -> None:
        if not _authorized(update):
            return
        await update.message.reply_html(
            "That command depends on a feature from a later phase "
            "(trades/charts) and isn't available yet."
        )

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("signals", signals))
    app.add_handler(CommandHandler("pcr", pcr))
    app.add_handler(CommandHandler("maxpain", maxpain))
    app.add_handler(CommandHandler("settings", settings_cmd))
    for reserved in ("trades", "open", "history", "chart"):
        app.add_handler(CommandHandler(reserved, not_yet))
    return app


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)
    db = make_database(settings, read_only=True)
    db.connect()
    app = build_application(settings, db)
    logger.info("Telegram bot starting (polling).")
    app.run_polling()


if __name__ == "__main__":
    main()
