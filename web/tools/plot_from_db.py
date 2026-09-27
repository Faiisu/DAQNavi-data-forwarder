#!/usr/bin/env python3
# See: docs/architecture/context.md
# plot_from_db.py
# ─────────────────────────────────────────────────────────────────────────────
# Read DAQ data from TimescaleDB and plot it.
#
# MODES
#   --mode static   Query a fixed time window and plot once (default)
#   --mode live     Poll the DB continuously and update the plot in real-time
#
# CHANNEL SELECTION
#   --channels 0 1 2   Plot channels 0, 1, and 2 (default: all channels in DB)
#
# TIME RANGE (static mode only)
#   --last   60         Plot data from the last 60 seconds (default: 60)
#   --start "2026-01-01 10:00:00"  Plot from a specific start timestamp
#   --end   "2026-01-01 10:01:00"  Plot up to a specific end timestamp
#
# LIVE MODE OPTIONS
#   --window     10     Rolling window in seconds shown on the plot (default: 10)
#   --interval  500     Refresh interval in milliseconds (default: 500)
#
# EXAMPLES
#   python plot_from_db.py                              # static, all channels, last 60 s
#   python plot_from_db.py --channels 0 1              # static, CH0 + CH1
#   python plot_from_db.py --mode live --channels 0    # live, CH0 only
#   python plot_from_db.py --mode static --last 120    # last 2 minutes
#   python plot_from_db.py --mode static \
#       --start "2026-07-09 10:00:00" \
#       --end   "2026-07-09 10:01:00"
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import matplotlib

# Try backends in order: MacOSX (native on macOS), Qt5Agg, TkAgg, Agg (headless)
for _backend in ("MacOSX", "Qt5Agg", "TkAgg", "Agg"):
    try:
        matplotlib.use(_backend)
        import matplotlib.pyplot as plt
        plt.figure()   # probe – will raise if backend is broken
        plt.close()
        break
    except Exception:
        plt = None  # type: ignore[assignment]
else:
    raise RuntimeError("No working matplotlib backend found")
import matplotlib.dates as mdates
import matplotlib.ticker
from matplotlib.animation import FuncAnimation

import psycopg2
import psycopg2.extras

# ─── Import DB connection string from project config ─────────────────────────
import json
import os

try:
    with open(os.path.join(os.path.dirname(__file__), "config.json"), "r") as f:
        _cfg = json.load(f)
    DB_DSN = _cfg["DB_DSN"]
except Exception:
    try:
        from old.config import DB_DSN
    except ImportError:
        DB_DSN = "postgresql://admin:admin@172.21.108.86:5432/daq_db"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def connect():
    """Open and return a psycopg2 connection."""
    return psycopg2.connect(DB_DSN)


def available_channels(conn):
    """Return a sorted list of channel numbers that exist in the DB."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT channel FROM daq_samples ORDER BY channel;")
        return [row[0] for row in cur.fetchall()]


def query_static(conn, channels, start_dt, end_dt):
    """
    Return {channel: (times_list, values_list)} for the given time range.
    times_list items are timezone-aware datetime objects.
    """
    ch_filter = "AND channel = ANY(%s)" if channels else ""
    sql = f"""
        SELECT time, channel, value
        FROM   daq_samples
        WHERE  time >= %s
          AND  time <= %s
          {ch_filter}
        ORDER  BY channel, time;
    """
    params = [start_dt, end_dt] + ([channels] if channels else [])

    data = defaultdict(lambda: ([], []))
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            t, ch, v = row["time"], row["channel"], row["value"]
            data[ch][0].append(t)
            data[ch][1].append(v)
    return data


def query_live(conn, channels, since_dt):
    """
    Return new rows added after *since_dt*.
    Returns {channel: (times_list, values_list)} and the latest timestamp seen.
    """
    ch_filter = "AND channel = ANY(%s)" if channels else ""
    sql = f"""
        SELECT time, channel, value
        FROM   daq_samples
        WHERE  time > %s
          {ch_filter}
        ORDER  BY time;
    """
    params = [since_dt] + ([channels] if channels else [])

    data = defaultdict(lambda: ([], []))
    latest = since_dt
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            t, ch, v = row["time"], row["channel"], row["value"]
            data[ch][0].append(t)
            data[ch][1].append(v)
            if t > latest:
                latest = t
    return data, latest


def _ms_fmt(x, _):
    """Format a matplotlib date float as HH:MM:SS.mmm."""
    dt = mdates.num2date(x)
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _apply_ms_locator(ax):
    """Configure the x-axis so the smallest tick unit is 1 ms (mmm)."""
    locator = mdates.AutoDateLocator(
        minticks=3,
        maxticks=10,
        interval_multiples=True,
    )
    # MILLISECONDLY = 6 in matplotlib's date constants; set it as the
    # minimum granularity so ticks never coarsen beyond whole seconds.
    locator.intervald[mdates.SECONDLY]      = [1, 5, 10, 15, 30]
    locator.intervald[mdates.MINUTELY]      = [1, 5, 10, 15, 30]
    locator.intervald[mdates.HOURLY]        = [1, 3, 6, 12]
    locator.intervald[mdates.DAILY]         = [1, 2, 7]
    # Ensure milliseconds are always a candidate level
    if hasattr(mdates, 'MILLISECONDLY'):
        locator.intervald[mdates.MILLISECONDLY] = [1, 5, 10, 25, 50, 100, 250, 500]
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_ms_fmt))


# ─── Colour palette ──────────────────────────────────────────────────────────

COLOURS = [
    "#4FC3F7", "#81C784", "#FFB74D", "#F06292",
    "#BA68C8", "#4DB6AC", "#FF8A65", "#90A4AE",
]


# ─── Shared axis styling ──────────────────────────────────────────────────────

def _style_axes(ax, title, ylabel):
    ax.set_title(title, color="white", fontsize=11, pad=10)
    ax.set_xlabel("Time (UTC)", color="#AAAAAA", fontsize=9)
    ax.set_ylabel(ylabel,       color="#AAAAAA", fontsize=9)
    ax.tick_params(colors="#AAAAAA", labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor("#444")
    ax.grid(True, color="#333344", linewidth=0.6, linestyle="--")


# ─── Static plot ─────────────────────────────────────────────────────────────

def run_static(args):
    now = datetime.now(tz=timezone.utc)

    if args.start and args.end:
        start_dt = datetime.fromisoformat(args.start).astimezone(timezone.utc)
        end_dt   = datetime.fromisoformat(args.end).astimezone(timezone.utc)
    else:
        end_dt   = now
        start_dt = now - timedelta(seconds=args.last)

    conn     = connect()
    channels = args.channels if args.channels else available_channels(conn)

    if not channels:
        print("No channels found in the database.")
        conn.close()
        sys.exit(1)

    print(f"Querying channels {channels} from {start_dt.isoformat()} to {end_dt.isoformat()} ...")
    data = query_static(conn, channels, start_dt, end_dt)
    conn.close()

    if not any(data[ch][0] for ch in channels):
        print("No data returned for the given time range / channels.")
        sys.exit(0)

    # ── Plot ──────────────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(12, 5))
    fig.patch.set_facecolor("#1E1E2E")
    ax.set_facecolor("#1E1E2E")

    for idx, ch in enumerate(channels):
        times, values = data[ch]
        if times:
            colour = COLOURS[idx % len(COLOURS)]
            ax.plot(times, values, label=f"CH{ch}", color=colour,
                    linewidth=1.0, alpha=0.9)

    _style_axes(ax,
                title=(f"DAQ Static Plot  |  "
                       f"{start_dt.strftime('%Y-%m-%d %H:%M:%S')} - "
                       f"{end_dt.strftime('%H:%M:%S')} UTC"),
                ylabel="Voltage (V)")

    _apply_ms_locator(ax)
    fig.autofmt_xdate()
    ax.legend(loc="upper right", facecolor="#2A2A3E", edgecolor="#555",
              labelcolor="white", fontsize=9)

    plt.tight_layout()
    plt.show()


# ─── Live plot ────────────────────────────────────────────────────────────────

class LivePlotter:
    def __init__(self, channels, window_sec, interval_ms):
        self.channels    = channels
        self.window_sec  = window_sec
        self.interval_ms = interval_ms
        self.conn        = connect()

        # seed cursor just behind "now" so we don't load old history
        self.since = datetime.now(tz=timezone.utc) - timedelta(seconds=1)

        # rolling buffers  {ch: (list[datetime], list[float])}
        self.buf = {ch: ([], []) for ch in channels}

        # ── Figure ────────────────────────────────────────────────────────────
        self.fig, self.ax = plt.subplots(figsize=(12, 5))
        self.fig.patch.set_facecolor("#1E1E2E")
        self.ax.set_facecolor("#1E1E2E")

        self.lines = {}
        for idx, ch in enumerate(channels):
            colour = COLOURS[idx % len(COLOURS)]
            (ln,) = self.ax.plot([], [], label=f"CH{ch}", color=colour,
                                 linewidth=1.0, alpha=0.9)
            self.lines[ch] = ln

        _style_axes(self.ax, title="DAQ Live Plot  |  reading from database",
                    ylabel="Voltage (V)")
        _apply_ms_locator(self.ax)
        self.fig.autofmt_xdate()
        self.ax.legend(loc="upper right", facecolor="#2A2A3E", edgecolor="#555",
                       labelcolor="white", fontsize=9)
        plt.tight_layout()

    def update(self, _frame):
        try:
            new_data, self.since = query_live(self.conn, self.channels, self.since)
        except psycopg2.OperationalError:
            # reconnect on dropped connection
            try:
                self.conn = connect()
            except Exception:
                pass
            return list(self.lines.values())

        # append new samples into rolling buffers
        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=self.window_sec)
        for ch in self.channels:
            times, values = self.buf[ch]
            if ch in new_data:
                times.extend(new_data[ch][0])
                values.extend(new_data[ch][1])

            # trim old data outside the rolling window
            while times and times[0] < cutoff:
                times.pop(0)
                values.pop(0)

            self.lines[ch].set_data(times, values)

        # update axis limits
        now = datetime.now(tz=timezone.utc)
        self.ax.set_xlim(now - timedelta(seconds=self.window_sec), now)
        all_vals = [v for ch in self.channels for v in self.buf[ch][1] if self.buf[ch][1]]
        if all_vals:
            margin = (max(all_vals) - min(all_vals)) * 0.1 or 0.1
            self.ax.set_ylim(min(all_vals) - margin, max(all_vals) + margin)

        self.ax.set_title(
            f"DAQ Live Plot  |  last updated {now.strftime('%H:%M:%S')} UTC",
            color="white", fontsize=11, pad=10
        )
        return list(self.lines.values())

    def run(self):
        self.ani = FuncAnimation(
            self.fig, self.update,
            interval=self.interval_ms,
            blit=False,
            cache_frame_data=False,
        )
        plt.show()
        self.conn.close()


def run_live(args):
    conn     = connect()
    channels = args.channels if args.channels else available_channels(conn)
    conn.close()

    if not channels:
        print("No channels found in the database.")
        sys.exit(1)

    print(f"Live plotting channels {channels}  |  window={args.window}s  |  refresh={args.interval}ms")
    plotter = LivePlotter(channels, args.window, args.interval)
    plotter.run()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Plot DAQ data from TimescaleDB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python plot_from_db.py                              # static, all channels, last 60 s
  python plot_from_db.py --channels 0 1              # static, CH0 + CH1
  python plot_from_db.py --mode live --channels 0    # live feed, CH0 only
  python plot_from_db.py --mode static --last 120    # last 2 minutes
  python plot_from_db.py --mode static \\
      --start "2026-07-09 10:00:00" \\
      --end   "2026-07-09 10:01:00"
        """,
    )

    p.add_argument("--mode", choices=["static", "live"], default="static",
                   help="Plot mode: 'static' (one-shot) or 'live' (real-time). Default: static")

    p.add_argument("--channels", type=int, nargs="+", metavar="N",
                   help="Channel numbers to plot (e.g. --channels 0 1). Default: all channels in DB")

    p.add_argument("--dsn", default=None,
                   help="Database DSN. Default: value from config.py")

    # Static-mode options
    sg = p.add_argument_group("Static mode options")
    sg.add_argument("--last",  type=float, default=60,
                    help="Seconds of history to plot (default: 60). Ignored if --start/--end used.")
    sg.add_argument("--start", metavar="DATETIME",
                    help='Start timestamp, e.g. "2026-07-09 10:00:00"')
    sg.add_argument("--end",   metavar="DATETIME",
                    help='End timestamp,   e.g. "2026-07-09 10:01:00"')

    # Live-mode options
    lg = p.add_argument_group("Live mode options")
    lg.add_argument("--window",   type=float, default=10,
                    help="Rolling window size in seconds (default: 10)")
    lg.add_argument("--interval", type=int,   default=500,
                    help="Refresh interval in milliseconds (default: 500)")

    return p.parse_args()


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()

    # Allow DSN override from CLI
    if args.dsn:
        DB_DSN = args.dsn  # type: ignore[assignment]

    try:
        if args.mode == "static":
            run_static(args)
        else:
            run_live(args)
    except psycopg2.OperationalError as e:
        print(f"\n[DB ERROR] Could not connect to the database:\n  {e}")
        print(f"\nCheck that the DB is running and DSN is correct:\n  {DB_DSN}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
