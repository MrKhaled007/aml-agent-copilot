import json
import os
from pathlib import Path

from databricks import sql
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

import logging

logging.getLogger("databricks").setLevel(logging.WARNING)

load_dotenv(Path(__file__).parent / ".env")

GOLD = "workspace.aml_gold"
MAX_ROWS = 50

mcp = MCPServer("aml-agent-copilot")


def _clean(value):
    """Convert Databricks array values (numpy) into plain Python lists."""
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def run_query(query: str, params: dict | None = None) -> list[dict]:
    """Run a read-only query against aml_gold and return rows as plain dictionaries."""
    with sql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, params or {})
            columns = [c[0] for c in cursor.description]
            rows = [{c: _clean(v) for c, v in zip(columns, row)} for row in cursor.fetchall()]
    return json.loads(json.dumps(rows, default=str))


@mcp.tool()
def get_rules() -> list[dict]:
    """Return the five alert rules with their plain-English descriptions and exact thresholds."""
    return run_query(f"""
        SELECT rule_id, rule_name, description
        FROM {GOLD}.alert_rules
        ORDER BY rule_id
    """)


@mcp.tool()
def list_alerts(priority: str | None = None, limit: int = 10) -> list[dict]:
    """List alerts from the queue, highest risk first.

    priority: optional, 'HIGH' or 'MEDIUM'.
    limit: number of alerts to return, between 1 and 50.
    """
    limit = max(1, min(int(limit), MAX_ROWS))
    params = {}
    where = ""

    if priority:
        priority = priority.upper()
        if priority not in ("HIGH", "MEDIUM"):
            raise ValueError("priority must be 'HIGH' or 'MEDIUM'")
        where = "WHERE priority = :priority"
        params["priority"] = priority

    return run_query(f"""
        SELECT alert_id, account_id, priority, rules_fired, triggered_rules,
               in_amount_usd, out_amount_usd
        FROM {GOLD}.alerts
        {where}
        ORDER BY rules_fired DESC, in_amount_usd DESC
        LIMIT {limit}
    """, params)


@mcp.tool()
def get_alert(alert_id: str) -> dict:
    """Return the full evidence for one alert: triggered rules, network features, amounts,
    activity dates, and the account owner's bank and entity."""
    rows = run_query(
        f"SELECT * FROM {GOLD}.alerts WHERE alert_id = :alert_id",
        {"alert_id": alert_id},
    )
    if not rows:
        return {"error": f"No alert found with id {alert_id}"}

    alert = rows[0]
    owner = run_query(
        f"""
        SELECT bank_name, entity_id, entity_name
        FROM {GOLD}.accounts
        WHERE account_id = :account_id
        """,
        {"account_id": alert["account_id"]},
    )
    alert["owner"] = owner[0] if owner else None
    return alert

@mcp.tool()
def get_account_transactions(account_id: str, limit: int = 20) -> list[dict]:
    """Return an account's transactions in time order (oldest first), with USD amounts,
    direction ('in' or 'out'), currencies, payment format and whether it is a self-transfer.

    limit: number of transactions to return, between 1 and 50.
    """
    limit = max(1, min(int(limit), MAX_ROWS))
    return run_query(f"""
        SELECT txn_id, txn_timestamp, from_account_id, to_account_id,
               CASE WHEN from_account_id = :account_id THEN 'out' ELSE 'in' END AS direction,
               ROUND(amount_paid_usd, 2) AS amount_usd,
               payment_currency, receiving_currency, payment_format, is_self_transfer
        FROM {GOLD}.transactions
        WHERE from_account_id = :account_id OR to_account_id = :account_id
        ORDER BY txn_timestamp ASC
        LIMIT {limit}
    """, {"account_id": account_id})


@mcp.tool()
def get_counterparties(account_id: str, limit: int = 20) -> list[dict]:
    """Return the accounts this account sends money to and receives money from (self-transfers
    excluded), largest flows first. For each counterparty: direction, number of transactions,
    total USD, first and last transaction, whether the counterparty is itself alerted, and
    whether it belongs to the same owner (entity).

    limit: number of counterparties to return, between 1 and 50.
    """
    limit = max(1, min(int(limit), MAX_ROWS))
    return run_query(f"""
        WITH flows AS (
            SELECT to_account_id AS counterparty_id, 'sent_to' AS direction,
                   amount_paid_usd AS amount_usd, txn_timestamp
            FROM {GOLD}.transactions
            WHERE from_account_id = :account_id AND NOT is_self_transfer
            UNION ALL
            SELECT from_account_id AS counterparty_id, 'received_from' AS direction,
                   amount_received_usd AS amount_usd, txn_timestamp
            FROM {GOLD}.transactions
            WHERE to_account_id = :account_id AND NOT is_self_transfer
        ),
        owner AS (
            SELECT entity_id FROM {GOLD}.accounts WHERE account_id = :account_id
        )
        SELECT f.counterparty_id,
               f.direction,
               COUNT(*) AS txn_count,
               ROUND(SUM(f.amount_usd), 2) AS total_usd,
               MIN(f.txn_timestamp) AS first_txn,
               MAX(f.txn_timestamp) AS last_txn,
               a.alert_id IS NOT NULL AS counterparty_alerted,
               a.priority AS counterparty_priority,
               acc.entity_id = o.entity_id AS same_owner
        FROM flows f
        CROSS JOIN owner o
        LEFT JOIN {GOLD}.alerts a ON a.account_id = f.counterparty_id
        LEFT JOIN {GOLD}.accounts acc ON acc.account_id = f.counterparty_id
        GROUP BY f.counterparty_id, f.direction, a.alert_id, a.priority, acc.entity_id, o.entity_id
        ORDER BY total_usd DESC
        LIMIT {limit}
    """, {"account_id": account_id})







if __name__ == "__main__":
    mcp.run()