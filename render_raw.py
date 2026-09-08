#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

"""WearableQA — dataset renderer.

Reads the structured dataset file ``WearableQA_raw.json`` (questions +
``user_histories`` + ``cohort_reference``) and renders every question into a
flat prompt, writing one JSON object per line to ``WearableQA.jsonl``.

The sensor time-series can be serialized in four ways via ``--format``:
``row`` (one line per day), ``col`` (one block per metric), ``csv``, or
``markdown``. The default ``row`` output reproduces the released
``WearableQA.jsonl`` byte-for-byte.

The structured source file lives next to this script (``WearableQA_raw.json``).
Running with no arguments regenerates the released ``WearableQA.jsonl``.

Usage:
  python3 render.py                       # regenerate WearableQA.jsonl (row format)
  python3 render.py --format markdown --out WearableQA_markdown.jsonl
"""
import argparse
import json
import os
from datetime import datetime, timedelta

OPTION_KEYS = list("ABCDEFGHIJ")

INSTRUCTION = (
    "You are given a user's demographics, wearable health sensor history, "
    "blood biomarker panel, a cohort reference distribution, and a "
    "multiple-choice question. Analyze the data carefully and select the "
    "single best answer."
)

FIELD_LABELS = {
    "steps": "Steps", "rhr": "RHR", "resting_hr": "RHR",
    "active_burn": "Active Cal", "active_kcal": "Active Cal",
    "sleep_duration": "Sleep(h)", "sleep_efficiency": "Sleep Eff",
    "deep_percentage": "Deep%", "rem_percentage": "REM%", "hrv": "HRV",
    "avg_stress_level": "Stress", "exercise_mets": "Ex METs",
    "exercise_hours": "Ex Hours", "exercise_avg_hr": "Ex Avg HR",
    "exercise_count": "Ex Count", "vo2_max": "VO2max", "vo2Max": "VO2max",
    "bed_time_std": "Bedtime Std(min)", "calories_bmr": "BMR Cal",
}


def format_val(v):
    if v is None:
        return None
    if isinstance(v, float):
        if v != v:
            return None
        a = abs(v)
        if a < 1:
            return f"{v:.3f}"
        if a < 10:
            return f"{v:.2f}"
        if a < 1000:
            return f"{v:.1f}"
        return f"{v:.0f}"
    return str(v)


def _ts_fields(eval_data):
    ts_rows = [r for r in eval_data if r.get("date")]
    keys = set()
    for r in ts_rows:
        keys.update(r.keys())
    fields = sorted(k for k in keys if k not in ("date", "_source", "_period"))
    return ts_rows, fields


def format_data(eval_data, style="row"):
    ts_rows, fields = _ts_fields(eval_data)
    if not fields:
        if ts_rows:
            return ""
        return "=== SENSOR DATA ===\n  (no wearable time-series available)"
    label = {f: FIELD_LABELS.get(f, f.replace("_", " ").title()) for f in fields}
    if style == "row":
        if not any(format_val(day.get(f)) is not None for day in ts_rows for f in fields):
            return ""
        lines = ["=== SENSOR DATA (row: one line per day) ==="]
        for day in ts_rows:
            parts = [f"{label[f]}={format_val(day.get(f))}" for f in fields
                     if format_val(day.get(f)) is not None]
            if parts:
                lines.append(f"  {day['date']}: {', '.join(parts)}")
        return "\n".join(lines)
    if style == "col":
        if not any(format_val(day.get(f)) is not None for day in ts_rows for f in fields):
            return ""
        lines = ["=== SENSOR DATA (column: one block per metric) ==="]
        for f in fields:
            series = [f"{day['date']}={format_val(day.get(f))}" for day in ts_rows
                      if format_val(day.get(f)) is not None]
            if series:
                lines.append(f"  {label[f]}: {', '.join(series)}")
        return "\n".join(lines)
    if style in ("markdown", "csv"):
        active = [f for f in fields if any(format_val(day.get(f)) is not None for day in ts_rows)]
        if not active:
            return ""
        head = ["date"] + [label[f] for f in active]
        body = []
        for day in ts_rows:
            cells = [day["date"]]
            for f in active:
                fv = format_val(day.get(f))
                cells.append(fv if fv is not None else "")
            body.append(cells)
        if style == "markdown":
            lines = ["=== SENSOR DATA (markdown table) ===", "| " + " | ".join(head) + " |",
                     "|" + "---|" * len(head)]
            lines += ["| " + " | ".join(c) + " |" for c in body]
        else:
            lines = ["=== SENSOR DATA (csv) ===", ",".join(head)]
            lines += [",".join(c) for c in body]
        return "\n".join(lines)
    raise ValueError(f"unknown format {style!r}")


def slice_history(history, window_end, window_size, days_cap=500):
    if not history:
        return []
    end = datetime.strptime(window_end, "%Y-%m-%d").date()
    start = end - timedelta(days=window_size + max(0, int(days_cap)))
    return [d for d in history
            if start <= datetime.strptime(d["date"], "%Y-%m-%d").date() <= end]


def render_question(mcq, fmt, hist, cohort):
    """Build the prompt body (demographics + sensor data + blood panel +
    cohort reference + question + options) for a single question."""
    we = mcq["end_date"]
    ws = mcq.get("window_size", 28)
    ed = [dict(r) for r in slice_history(hist or [], we, ws, days_cap=500)]
    full_drop = set(mcq.get("full_dropped_metrics") or [])
    if mcq.get("hidden_metric"):
        full_drop.add(mcq["hidden_metric"])
    win_drop = set(mcq.get("window_dropped_metrics") or [])
    win_start = (datetime.strptime(we, "%Y-%m-%d").date() - timedelta(days=ws)).strftime("%Y-%m-%d") if win_drop else None
    for r in ed:
        for m in full_drop:
            if m in r:
                r[m] = None
        if win_drop and win_start and r.get("date", "") >= win_start:
            for m in win_drop:
                if m in r:
                    r[m] = None
        for _sm in ("exercise_avg_hr", "exercise_mets", "exercise_hours"):
            if r.get(_sm) == 0:
                r[_sm] = None
        asl = r.get("avg_stress_level")
        if asl is not None:
            try:
                if float(asl) <= 0:
                    r["avg_stress_level"] = None
            except (TypeError, ValueError):
                pass

    ctx = mcq.get("context", {}) or {}
    demo = ctx.get("demographics") or {}
    blood = ctx.get("blood_panel") or {}
    coh = {} if mcq.get("no_cohort") else (cohort or {})

    _instr = INSTRUCTION if coh else INSTRUCTION.replace(", a cohort reference distribution,", ",")
    parts = [_instr + "\n"]
    dp = []
    if demo.get("age"):
        dp.append(f"Age: {demo['age']:.0f}")
    if demo.get("sex"):
        dp.append(f"Sex: {demo['sex']}")
    if demo.get("bmi"):
        dp.append(f"BMI: {demo['bmi']:.1f}")
    if demo.get("ethnicity"):
        dp.append(f"Ethnicity: {demo['ethnicity']}")
    if dp:
        parts.append("=== USER PROFILE ===")
        parts.append("  " + ", ".join(dp) + "\n")

    sensor = format_data(ed, fmt)
    if sensor:
        parts.append(sensor)

    if blood:
        parts.append("\n=== BLOOD BIOMARKER PANEL ===")
        bt = ""
        for k, v in blood.items():
            bt += f" {k}: {format_val(v)},"
        parts.append(bt.rstrip(","))

    if coh:
        parts.append("\n=== COHORT REFERENCE (population percentiles) ===")
        _skip = {"_n_users", "_median_days_per_user"}
        for metric, pcs in coh.items():
            if isinstance(pcs, dict):
                ps = ", ".join(f"{k}={v}" for k, v in pcs.items() if k not in _skip)
                parts.append(f"  {metric}: {ps}")

    q = mcq["stem"]
    q_low = q.lower()
    has_window = any(p in q_low for p in ("based on", "-day window", "day window", "recent days", "days of data"))
    if has_window:
        qtext = q
    elif "Based on this user" in q:
        qtext = q.replace("Based on this user's", "Based on this user's recent")
    else:
        qtext = f"Based on the most recent {ws} days of data, {q[0].lower()}{q[1:]}"
    parts.append(f"\n=== QUESTION ===\n{qtext}\n")

    parts.append("=== OPTIONS ===")
    for k in OPTION_KEYS:
        if k in mcq.get("options", {}):
            parts.append(f"{k}. {mcq['options'][k]}")

    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="WearableQA dataset renderer.")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--data", default=os.path.join(here, "WearableQA_raw.json"),
                    help="structured dataset file (WearableQA_raw.json)")
    ap.add_argument("--format", choices=["row", "col", "csv", "markdown"],
                    default="row", help="sensor time-series serialization")
    ap.add_argument("--out", default=os.path.join(here, "WearableQA.jsonl"),
                    help="output JSONL path")
    args = ap.parse_args()

    data = json.load(open(args.data))
    uh = data["user_histories"]
    cohort = data.get("cohort_reference") or {}
    mcqs = data["mcqs"]

    n = 0
    with open(args.out, "w") as f:
        for m in mcqs:
            hist = uh.get(str(m["user_id"])) or uh.get(m["user_id"])
            body = render_question(m, args.format, hist, cohort)
            rec = {
                "id": m["id"],
                "question": body,
                "choices": m["options"],
                "answer": m["gt_letter"],
                "category": m["question_type"],
                "reasoning_group": m["reasoning_group"],
                "signal": m["signal"],
                "grounding": m["grounding"],
                "representation": args.format,
            }
            f.write(json.dumps(rec) + "\n")
            n += 1
    print(f"rendered {n} questions ({args.format}) -> {args.out}")


if __name__ == "__main__":
    main()
