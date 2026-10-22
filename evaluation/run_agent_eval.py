import argparse
import inspect
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp_server"))

import anthropic  # noqa: E402 
import server  # noqa: E402  (also loads mcp_server/.env and the triage prompt)
from dotenv import dotenv_values  # noqa: E402

MODEL = "claude-sonnet-5-5"
MAX_TURNS = 8
PRICE_IN = 2.00 / 1_000_000
PRICE_OUT = 10.00 / 1_000_000

IDS_FILE = ROOT / "evaluation" / "sample_alert_ids.txt"
RESULTS_FILE = ROOT / "evaluation" / "results" / "agent_results.jsonl"
ENV = dotenv_values(ROOT / "mcp_server" / ".env")
TOOL_FUNCS = {
    "get_rules": server.get_rules,
    "list_alerts": server.list_alerts,
    "get_alert": server.get_alert,
    "get_account_transactions": server.get_account_transactions,
    "get_counterparties": server.get_counterparties,
}

ACCOUNT_LIMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "account_id": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    "required": ["account_id"],
}

TOOLS = [
    {
        "name": "get_rules",
        "description": inspect.getdoc(server.get_rules),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_alerts",
        "description": inspect.getdoc(server.list_alerts),
        "input_schema": {
            "type": "object",
            "properties": {
                "priority": {"type": "string", "enum": ["HIGH", "MEDIUM"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "get_alert",
        "description": inspect.getdoc(server.get_alert),
        "input_schema": {
            "type": "object",
            "properties": {"alert_id": {"type": "string"}},
            "required": ["alert_id"],
        },
    },
    {
        "name": "get_account_transactions",
        "description": inspect.getdoc(server.get_account_transactions),
        "input_schema": ACCOUNT_LIMIT_SCHEMA,
    },
    {
        "name": "get_counterparties",
        "description": inspect.getdoc(server.get_counterparties),
        "input_schema": ACCOUNT_LIMIT_SCHEMA,
    },
]


def triage(client, alert_id):
    """Run the agent on one alert until it gives a final answer."""
    messages = [{"role": "user", "content": f"Triage alert {alert_id}."}]
    tokens_in = 0
    tokens_out = 0
    tool_calls = []

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=server.TRIAGE_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        tokens_in += response.usage.input_tokens
        tokens_out += response.usage.output_tokens
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            text = "".join(block.text for block in response.content if block.type == "text")
            return text, tokens_in, tokens_out, tool_calls

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_calls.append(block.name)
            try:
                output = TOOL_FUNCS[block.name](**block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(output),
                })
            except Exception as exc:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Error: {exc}",
                    "is_error": True,
                })
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"Agent did not finish {alert_id} within {MAX_TURNS} turns")


def parse_decision(text):
    """Pull the JSON block out of the agent's answer."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = match.group(1) if match else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def main():
    parser = argparse.ArgumentParser(description="Run the AML triage agent on the evaluation sample.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of new alerts to triage")
    args = parser.parse_args()

    ids = [line.strip() for line in IDS_FILE.read_text().splitlines() if line.strip()]

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if RESULTS_FILE.exists():
        done = {
            json.loads(line)["alert_id"]
            for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    todo = [alert_id for alert_id in ids if alert_id not in done]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(done)} already done, {len(todo)} to run now with {MODEL}")

    client = anthropic.Anthropic(api_key=ENV["ANTHROPIC_API_KEY"])
    total_cost = 0.0

    for n, alert_id in enumerate(todo, 1):
        start = time.time()
        text, tokens_in, tokens_out, tool_calls = triage(client, alert_id)
        decision = parse_decision(text)
        cost = tokens_in * PRICE_IN + tokens_out * PRICE_OUT
        total_cost += cost

        record = {
            "alert_id": alert_id,
            "model": MODEL,
            "run_at": datetime.now(timezone.utc).isoformat(),
            "decision": decision,
            "raw_text": text,
            "tool_calls": tool_calls,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(cost, 4),
            "seconds": round(time.time() - start, 1),
        }
        with RESULTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        label = decision.get("decision") if decision else "PARSE ERROR"
        print(
            f"[{n}/{len(todo)}] {alert_id}: {label} | "
            f"{tokens_in:,} in / {tokens_out:,} out | ${cost:.3f} | {record['seconds']}s"
        )

    print(f"Total cost this run: ${total_cost:.2f}")


if __name__ == "__main__":
    main()