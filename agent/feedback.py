"""
Claude Feedback Loop — analyzes closed trade history and suggests parameter adjustments.

How it works:
  1. Query closed trades from the DB (last N trades)
  2. Compute performance metrics broken down by: pair, hour, day-of-week, regime, direction
  3. Send the metrics package to Claude with the current default params
  4. Claude returns structured suggestions (which params to adjust and why)
  5. Suggestions are stored in the DB as pending — user approves/rejects in the dashboard
  6. Approved suggestions update the live _test_params / DEFAULT_PARAMS

Claude does NOT auto-apply changes. Every suggestion requires human approval.
"""
import json
from datetime import datetime, timezone
import anthropic
from config import ANTHROPIC_API_KEY
from data.store import _conn

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def get_trade_metrics(account_id=None, min_trades=10):
    """
    Compute performance metrics from closed trades.
    Returns None if there aren't enough trades to analyse meaningfully.
    """
    where = "WHERE status='closed' AND pnl IS NOT NULL AND close_price IS NOT NULL AND open_price IS NOT NULL"
    params = []
    if account_id:
        where += " AND account_id=%s"
        params.append(account_id)

    with _conn() as conn:
        rows = conn.execute(
            f"SELECT pair, direction, open_price, close_price, pnl, opened, is_test, sl_price "
            f"FROM trades {where} ORDER BY opened DESC LIMIT 200",
            params
        ).fetchall()

    if len(rows) < min_trades:
        return None

    # Overall stats
    total    = len(rows)
    wins     = [r for r in rows if r[4] > 0]
    losses   = [r for r in rows if r[4] <= 0]
    win_rate = round(len(wins) / total * 100, 1)
    avg_win  = round(sum(r[4] for r in wins) / max(len(wins), 1), 2)
    avg_loss = round(sum(r[4] for r in losses) / max(len(losses), 1), 2)
    total_pnl = round(sum(r[4] for r in rows), 2)
    expectancy = round((win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss), 2)

    # By pair
    pairs = {}
    for r in rows:
        pair = r[0]
        if pair not in pairs:
            pairs[pair] = {"trades": 0, "wins": 0, "pnl": 0}
        pairs[pair]["trades"] += 1
        pairs[pair]["pnl"] = round(pairs[pair]["pnl"] + r[4], 2)
        if r[4] > 0:
            pairs[pair]["wins"] += 1
    for p in pairs:
        pairs[p]["win_rate"] = round(pairs[p]["wins"] / pairs[p]["trades"] * 100, 1)

    # By direction
    by_direction = {}
    for r in rows:
        d = r[1]
        if d not in by_direction:
            by_direction[d] = {"trades": 0, "wins": 0, "pnl": 0}
        by_direction[d]["trades"] += 1
        by_direction[d]["pnl"] = round(by_direction[d]["pnl"] + r[4], 2)
        if r[4] > 0:
            by_direction[d]["wins"] += 1

    # By hour of day (UTC)
    by_hour = {}
    for r in rows:
        try:
            hour = int(r[5][11:13]) if r[5] else None
        except Exception:
            hour = None
        if hour is None:
            continue
        bucket = f"{hour:02d}:00"
        if bucket not in by_hour:
            by_hour[bucket] = {"trades": 0, "wins": 0, "pnl": 0}
        by_hour[bucket]["trades"] += 1
        by_hour[bucket]["pnl"] = round(by_hour[bucket]["pnl"] + r[4], 2)
        if r[4] > 0:
            by_hour[bucket]["wins"] += 1

    # Worst losing pairs (candidates for increased scrutiny)
    worst_pairs = sorted(
        [(p, pairs[p]["pnl"], pairs[p]["win_rate"]) for p in pairs],
        key=lambda x: x[1]
    )[:3]

    # Best performing pairs
    best_pairs = sorted(
        [(p, pairs[p]["pnl"], pairs[p]["win_rate"]) for p in pairs],
        key=lambda x: -x[1]
    )[:3]

    return {
        "total_trades":  total,
        "win_rate_pct":  win_rate,
        "avg_win":       avg_win,
        "avg_loss":      avg_loss,
        "total_pnl":     total_pnl,
        "expectancy":    expectancy,
        "by_pair":       pairs,
        "by_direction":  by_direction,
        "by_hour_utc":   by_hour,
        "worst_pairs":   worst_pairs,
        "best_pairs":    best_pairs,
        "is_test_mix":   sum(1 for r in rows if r[6]) / total,
    }


FEEDBACK_SYSTEM = """You are a professional forex trading system analyst reviewing the performance of an automated trading agent.

You will receive:
1. Performance metrics from closed trades
2. The agent's current default parameters

Your job is to identify patterns in the data and suggest specific, conservative parameter adjustments.

Rules:
- Only suggest changes backed by the data. Do not speculate.
- Prefer small, incremental adjustments (e.g. confidence_min from 0.60 → 0.65, not 0.60 → 0.80)
- If a pair is consistently losing, suggest either increasing its confidence threshold or excluding it
- If trades at certain hours are underperforming, suggest tightening the market hours window
- If win rate is low but avg win > avg loss, note that expectancy might still be positive
- Maximum 5 suggestions per feedback session
- If there isn't enough signal in the data, say so clearly and suggest collecting more trades first

Always respond with valid JSON in exactly this structure:
{
  "summary": "2-3 sentence assessment of overall performance",
  "data_quality": "sufficient" | "limited" | "insufficient",
  "suggestions": [
    {
      "param": "parameter_name",
      "current_value": <current>,
      "suggested_value": <suggested>,
      "rationale": "specific data-backed reason",
      "confidence": "high" | "medium" | "low",
      "pair_specific": null | "PAIR_NAME"
    }
  ],
  "notable_patterns": ["list of observations worth noting even if no param change suggested"]
}"""


def run_feedback_analysis(account_id=None, current_params=None):
    """
    Main entry point. Runs metrics, asks Claude, returns structured suggestions.
    Saves results to DB. Returns the full response dict.
    """
    metrics = get_trade_metrics(account_id=account_id)

    if metrics is None:
        return {
            "status":  "insufficient_data",
            "message": "Not enough closed trades to analyse. Need at least 10 closed trades with P&L recorded.",
            "suggestions": []
        }

    from execution.risk import DEFAULT_PARAMS
    params_context = {**DEFAULT_PARAMS, **(current_params or {})}

    prompt = f"""## Trade Performance Metrics

{json.dumps(metrics, indent=2)}

## Current Agent Parameters

{json.dumps(params_context, indent=2)}

Based on this data, provide your analysis and suggestions."""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=FEEDBACK_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        result["status"]    = "ok"
        result["metrics"]   = metrics
        result["generated"] = datetime.now(timezone.utc).isoformat()

    except Exception as e:
        result = {
            "status":    "error",
            "error":     str(e),
            "metrics":   metrics,
            "suggestions": []
        }

    _save_feedback(result, account_id)
    return result


def _save_feedback(result, account_id):
    """Persist the feedback session and each suggestion to the DB."""
    with _conn() as conn:
        row = conn.execute("""
            INSERT INTO feedback_sessions (created, account_id, summary, data_quality, metrics_json, raw_json)
            VALUES (NOW(), %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            account_id,
            result.get("summary", ""),
            result.get("data_quality", ""),
            json.dumps(result.get("metrics", {})),
            json.dumps(result),
        )).fetchone()
        session_id = row[0]

        for s in result.get("suggestions", []):
            conn.execute("""
                INSERT INTO feedback_suggestions
                (session_id, account_id, param, current_value, suggested_value,
                 rationale, confidence, pair_specific, created)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """, (
                session_id,
                account_id,
                s.get("param", ""),
                str(s.get("current_value", "")),
                str(s.get("suggested_value", "")),
                s.get("rationale", ""),
                s.get("confidence", ""),
                s.get("pair_specific"),
            ))
        conn.commit()
    return session_id


def get_pending_suggestions(account_id=None):
    """Return all pending suggestions for the dashboard to display."""
    with _conn() as conn:
        try:
            where = "WHERE fs.status='pending'"
            params = []
            if account_id:
                where += " AND fs.account_id=%s"
                params.append(account_id)
            rows = conn.execute(f"""
                SELECT fs.id, fs.param, fs.current_value, fs.suggested_value,
                       fs.rationale, fs.confidence, fs.pair_specific,
                       fs.created, sess.summary
                FROM feedback_suggestions fs
                JOIN feedback_sessions sess ON sess.id = fs.session_id
                {where}
                ORDER BY fs.id DESC LIMIT 20
            """, params).fetchall()
            return rows
        except Exception:
            return []


def apply_suggestion(suggestion_id, account_id=None):
    """Mark a suggestion as applied."""
    with _conn() as conn:
        conn.execute(
            "UPDATE feedback_suggestions SET status='applied' WHERE id=%s",
            (suggestion_id,)
        )
        conn.commit()


def dismiss_suggestion(suggestion_id):
    """Mark a suggestion as dismissed."""
    with _conn() as conn:
        conn.execute(
            "UPDATE feedback_suggestions SET status='dismissed' WHERE id=%s",
            (suggestion_id,)
        )
        conn.commit()


def get_latest_session(account_id=None):
    """Get the most recent feedback session summary."""
    with _conn() as conn:
        try:
            where = "WHERE account_id=%s" if account_id else ""
            params = [account_id] if account_id else []
            row = conn.execute(
                f"SELECT created, summary, data_quality, raw_json FROM feedback_sessions {where} ORDER BY id DESC LIMIT 1",
                params
            ).fetchone()
            if not row:
                return None
            return {
                "created":      row[0],
                "summary":      row[1],
                "data_quality": row[2],
                "raw":          json.loads(row[3]) if row[3] else {}
            }
        except Exception:
            return None
