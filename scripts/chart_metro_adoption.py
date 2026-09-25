"""LinkedIn chart: metro commute share over the year, base vs new metro line (3 seeds each).

Usage: uv run --with matplotlib python scripts/chart_metro_adoption.py
Reads runs/base-or3 and runs/metro-or3 batch summaries; writes media/metro_adoption.png.
"""

import json
from datetime import date, timedelta

import matplotlib.pyplot as plt
from matplotlib import font_manager

BG = "#07090d"
PANEL = "#0e1117"
TEXT = "#f2f2ef"
TEXT_2 = "#b9b8b0"
MUTED = "#6f6e69"
GRID = "#23262d"
LINE = "#3987e5"  # categorical slot 1 (dark)
BASE = "#8a8a86"  # neutral context series, dashed + direct label (not a categorical hue)
DISTRICTS = [("nou_barris", "Nou Barris"), ("sant_andreu", "Sant Andreu"),
             ("sants_montjuic", "Sants-Montjuïc")]
OPEN_TICK = 60
START = date(2026, 1, 1)


def series(batch: str, district: str) -> list[dict]:
    with open(f"runs/{batch}/batch_summary.json") as fh:
        s = json.load(fh)["series"]
    return s[district]["mode_share.metro"]


def main() -> None:
    names = {f.name for f in font_manager.fontManager.ttflist}
    family = next((f for f in ("Inter", "Helvetica Neue", "Helvetica", "Arial") if f in names),
                  "sans-serif")
    plt.rcParams.update({"font.family": family, "font.size": 13, "text.color": TEXT,
                         "axes.labelcolor": TEXT_2, "xtick.color": MUTED, "ytick.color": MUTED})

    fig, axes = plt.subplots(1, 3, figsize=(16, 9), dpi=120, sharey=True)
    fig.patch.set_facecolor(BG)
    for ax, (did, label) in zip(axes, DISTRICTS, strict=True):
        ax.set_facecolor(BG)
        for key, color, style, name in (("base-or3", BASE, (0, (5, 3)), "No new line"),
                                        ("metro-or3", LINE, "-", "New metro line")):
            pts = series(key, did)
            x = [START + timedelta(days=p["tick"] - 1) for p in pts]
            mean = [p["mean"] * 100 for p in pts]
            lo = [p["min"] * 100 for p in pts]
            hi = [p["max"] * 100 for p in pts]
            ax.fill_between(x, lo, hi, color=color, alpha=0.18, linewidth=0)
            ax.plot(x, mean, color=color, linewidth=2.4, linestyle=style, solid_capstyle="round")
            ax.annotate(f"{mean[-1]:.0f}%", xy=(x[-1], mean[-1]),
                        xytext=(8, 0), textcoords="offset points", va="center",
                        fontsize=15, color=TEXT_2 if key == "base-or3" else TEXT,
                        fontweight="bold" if key == "metro-or3" else "normal")
        opens = START + timedelta(days=OPEN_TICK - 1)
        ax.axvline(opens, color=MUTED, linewidth=1, linestyle=(0, (2, 3)))
        ax.text(opens, 57, "  line opens", color=TEXT_2, fontsize=11, va="top")
        ax.set_title(label, loc="left", fontsize=17, fontweight="bold", color=TEXT, pad=12)
        ax.set_ylim(0, 60)
        ax.set_xlim(START, START + timedelta(days=364))
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.xaxis.set_major_locator(plt.matplotlib.dates.MonthLocator(bymonth=[1, 4, 7, 10]))
        ax.xaxis.set_major_formatter(plt.matplotlib.dates.DateFormatter("%b"))
        ax.tick_params(length=0, labelsize=12)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    axes[0].set_ylabel("Residents commuting by metro")

    fig.text(0.055, 0.955, "A new metro line, simulated: 1,000 AI citizens of Barcelona",
             fontsize=24, fontweight="bold", color=TEXT, ha="left", va="top")
    fig.text(0.055, 0.905, "Share of commuters using the metro · line = mean of 3 simulated years, "
             "band = range across runs", fontsize=14, color=TEXT_2, ha="left", va="top")
    fig.text(0.055, 0.035, "Decisions by Jev (TypeSafe AI) via OpenRouter · Barcelona data: "
             "Open Data BCN, EMEF 2024 · Simulation, not a forecast", fontsize=11, color=MUTED,
             ha="left")
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], color=LINE, linewidth=2.6, label="With the new metro line"),
               Line2D([], [], color=BASE, linewidth=2.6, linestyle=(0, (5, 3)),
                      label="Without it (baseline)")]
    fig.legend(handles=handles, loc="upper right", bbox_to_anchor=(0.965, 0.965), frameon=False,
               fontsize=13, labelcolor=TEXT_2, handlelength=2.6)
    fig.subplots_adjust(left=0.055, right=0.955, top=0.8, bottom=0.12, wspace=0.22)
    fig.savefig("media/metro_adoption.png", facecolor=BG)
    print("wrote media/metro_adoption.png")


if __name__ == "__main__":
    main()
