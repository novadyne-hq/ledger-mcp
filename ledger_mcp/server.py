"""Ledger MCP Server — double-entry accounting for AI agents.

Exposes the Ledger API (https://ledger-api.novadyne.ai) as MCP tools:
  - create_account: add an account to the chart of accounts
  - list_accounts / get_account: read the chart of accounts + balances
  - post_transaction: record a balanced journal entry (entries sum to zero)
  - list_transactions / get_transaction: read the journal
  - reverse_transaction: post a reversing entry
  - trial_balance: every account balance + debit/credit totals as of a date
  - general_ledger: per-account transaction history over a date range

The Ledger API is **paid per call via x402 micropayments** (USDC on Base).
This server pays automatically when LEDGER_X402_PRIVATE_KEY is set to a
funded Base wallet; without it, the read/write tools return the price and
how to enable payment (health, schema discovery, and pricing work keyless).
"""

import json
import os
import secrets
import time
import urllib.parse
import urllib.request
import urllib.error

from mcp.server import FastMCP

DEFAULT_API_URL = "https://ledger-api.novadyne.ai"
API_URL = os.environ.get("LEDGER_API_URL", DEFAULT_API_URL).rstrip("/")

# A funded Base wallet private key (hex, with or without 0x). When set, the
# server signs x402 EIP-3009 payments automatically so paid tools just work.
PRIVATE_KEY = (
    os.environ.get("LEDGER_X402_PRIVATE_KEY")
    or os.environ.get("X402_PRIVATE_KEY")
    or ""
).strip()

# Native USDC on Base — used only as a sanity check against the 402 challenge.
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

mcp = FastMCP(
    "Ledger",
    instructions=(
        "Ledger is a double-entry accounting API for AI agents. "
        "Create accounts, post balanced journal entries (every transaction's "
        "entries must sum to zero), and pull trial-balance / general-ledger "
        "reports. Amounts are signed integers in minor units (cents): a debit "
        "is positive, a credit is negative, and the entries of one transaction "
        "must sum to exactly 0. The API is paid per call via x402 micropayments "
        "(USDC on Base) — set LEDGER_X402_PRIVATE_KEY to a funded Base wallet to "
        "pay automatically. Start with trial_balance or list_accounts to see the "
        "current books."
    ),
)


# --- x402 payment ---

class PaymentError(Exception):
    pass


def _eip3009_header(accept: dict) -> str:
    """Build a base64 X-PAYMENT header (x402 v2) by signing an EIP-3009
    TransferWithAuthorization for the amount the 402 challenge asks for."""
    try:
        from eth_account import Account
    except ImportError:
        raise PaymentError(
            "eth-account is required to pay. Reinstall with `uvx ledger-mcp` "
            "(it is a declared dependency) or `pip install eth-account`."
        )

    if not PRIVATE_KEY:
        raise PaymentError("no wallet configured")

    asset = accept.get("asset", "")
    if asset and asset.lower() != USDC_BASE.lower():
        raise PaymentError(f"unexpected asset {asset}; expected USDC on Base")

    acct = Account.from_key(PRIVATE_KEY)
    value = str(accept.get("maxAmountRequired") or accept.get("amount") or "0")
    pay_to = accept["payTo"]
    timeout = int(accept.get("maxTimeoutSeconds", 60))
    now = int(time.time())
    authorization = {
        "from": acct.address,
        "to": pay_to,
        "value": value,
        "validAfter": "0",
        "validBefore": str(now + max(timeout, 60)),
        "nonce": "0x" + secrets.token_hex(32),
    }
    extra = accept.get("extra") or {}
    domain = {
        "name": extra.get("name", "USD Coin"),
        "version": extra.get("version", "2"),
        "chainId": 8453,
        "verifyingContract": asset or USDC_BASE,
    }
    types = {
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ]
    }
    message = {
        "from": acct.address,
        "to": pay_to,
        "value": int(value),
        "validAfter": 0,
        "validBefore": int(authorization["validBefore"]),
        "nonce": bytes.fromhex(authorization["nonce"][2:]),
    }
    signed = Account.sign_typed_data(PRIVATE_KEY, domain, types, message)
    sig = signed.signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    payload = {
        "x402Version": 2,
        "scheme": accept.get("scheme", "exact"),
        "network": accept.get("network", "eip155:8453"),
        "payload": {
            "signature": sig,
            "authorization": authorization,
        },
    }
    import base64
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _request(method: str, path: str, query: dict | None = None, body: dict | None = None) -> dict:
    """Call the Ledger API, paying the x402 challenge automatically if a wallet
    is configured. Returns {"ok": True, "data": ...} or {"ok": False, "error": ...}."""
    url = f"{API_URL}{path}"
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    def _do(payment_header: str | None):
        headers = {"User-Agent": "ledger-mcp/0.1", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        if payment_header:
            headers["X-PAYMENT"] = payment_header
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        return urllib.request.urlopen(req, timeout=90)

    try:
        with _do(None) as resp:
            return {"ok": True, "data": json.loads(resp.read() or "null")}
    except urllib.error.HTTPError as e:
        if e.code != 402:
            detail = e.read().decode() if e.fp else ""
            return {"ok": False, "error": f"HTTP {e.code}: {detail[:400]}"}
        challenge_raw = e.read().decode() if e.fp else "{}"
    except Exception as e:
        return {"ok": False, "error": f"Ledger API unreachable: {e}"}

    # 402 — pay and retry
    try:
        challenge = json.loads(challenge_raw)
        accept = (challenge.get("accepts") or [{}])[0]
        price = accept.get("amount") or accept.get("maxAmountRequired")
        price_usd = f"${int(price) / 1_000_000:.4f}" if price else "?"
    except Exception:
        accept, price_usd = {}, "?"

    if not PRIVATE_KEY:
        return {
            "ok": False,
            "needs_payment": True,
            "error": (
                f"This Ledger endpoint costs {price_usd} per call (x402, USDC on Base). "
                "Set LEDGER_X402_PRIVATE_KEY to a funded Base wallet private key to pay "
                "automatically, or pay the x402 challenge yourself. "
                "Free/keyless: health() and discover()."
            ),
        }

    try:
        header = _eip3009_header(accept)
    except PaymentError as e:
        return {"ok": False, "error": f"Could not build payment: {e}"}

    try:
        with _do(header) as resp:
            return {"ok": True, "data": json.loads(resp.read() or "null"), "paid": price_usd}
    except urllib.error.HTTPError as e:
        detail = e.read().decode() if e.fp else ""
        return {"ok": False, "error": f"Payment rejected — HTTP {e.code}: {detail[:400]}"}
    except Exception as e:
        return {"ok": False, "error": f"Ledger API unreachable after payment: {e}"}


def _out(result: dict) -> str:
    """Render an API result as compact, agent-readable text."""
    if not result.get("ok"):
        return f"Error: {result.get('error', 'unknown')}"
    data = result.get("data")
    paid = f"  (paid {result['paid']})" if result.get("paid") else ""
    return f"{json.dumps(data, indent=2)}{paid}"


# --- MCP Tools ---

@mcp.tool()
def health() -> str:
    """Check the Ledger API status (free, no payment). Returns service version,
    whether x402 payment is enabled, and the capability-token public key."""
    return _out(_request("GET", "/health"))


@mcp.tool()
def discover() -> str:
    """List the paid Ledger endpoints and their x402 prices (free, no payment).
    Reads the x402 discovery document — useful before spending."""
    return _out(_request("GET", "/.well-known/x402"))


@mcp.tool()
def create_account(name: str, type: str, parent_id: int = 0) -> str:
    """Create an account in the chart of accounts. (Paid: write.)

    Args:
        name: Account name, e.g. "Cash", "Sales Revenue", "Accounts Payable".
        type: One of asset | liability | equity | revenue | expense.
        parent_id: Optional parent account id for a sub-account (0 = top level).
    """
    return _out(_request("POST", "/ledger/accounts",
                         body={"name": name, "type": type, "parent_id": parent_id}))


@mcp.tool()
def list_accounts(include_inactive: bool = False) -> str:
    """List all accounts in the chart of accounts with current balances. (Paid: read.)

    Args:
        include_inactive: Also include deactivated accounts.
    """
    return _out(_request("GET", "/ledger/accounts",
                         query={"include_inactive": str(include_inactive).lower()}))


@mcp.tool()
def get_account(account_id: int, as_of: str | None = None) -> str:
    """Get one account and its balance, optionally as of a date. (Paid: read.)

    Args:
        account_id: The account id.
        as_of: Balance as of this date (YYYY-MM-DD); omit for current.
    """
    return _out(_request("GET", f"/ledger/accounts/{account_id}", query={"as_of": as_of}))


@mcp.tool()
def post_transaction(date: str, entries: list[dict], description: str = "",
                     reference: str | None = None) -> str:
    """Record a balanced double-entry journal transaction. (Paid: write.)

    Every transaction is two or more entries whose signed amounts sum to ZERO.
    Amounts are integers in MINOR units (cents): a debit is POSITIVE, a credit
    is NEGATIVE. Example — record a $500 cash sale (debit Cash, credit Revenue):
        entries = [
            {"account_id": 1, "amount": 50000, "memo": "cash in"},
            {"account_id": 2, "amount": -50000, "memo": "sales revenue"}
        ]

    Args:
        date: Transaction date, YYYY-MM-DD.
        entries: List of {"account_id": int, "amount": int (cents, +debit/-credit),
                 "memo": optional str}. Must have >=2 entries summing to 0.
        description: Human-readable description of the transaction.
        reference: Optional external reference (invoice #, etc.).
    """
    total = sum(int(e.get("amount", 0)) for e in entries)
    if total != 0:
        return (f"Error: entries must sum to zero (double-entry); they sum to {total} "
                f"cents. Adjust the amounts so debits (+) and credits (-) balance.")
    body = {"date": date, "description": description, "entries": entries}
    if reference is not None:
        body["reference"] = reference
    return _out(_request("POST", "/ledger/transactions", body=body))


@mcp.tool()
def list_transactions(from_date: str | None = None, to_date: str | None = None,
                      limit: int = 50, cursor: int = 0) -> str:
    """List journal transactions, newest first. (Paid: read.)

    Args:
        from_date: Only transactions on/after this date (YYYY-MM-DD).
        to_date: Only transactions on/before this date (YYYY-MM-DD).
        limit: Max rows (default 50).
        cursor: Pagination cursor from a previous response.
    """
    return _out(_request("GET", "/ledger/transactions",
                         query={"from": from_date, "to": to_date, "limit": limit, "cursor": cursor}))


@mcp.tool()
def get_transaction(tx_id: int) -> str:
    """Get a single transaction with all its entries. (Paid: read.)

    Args:
        tx_id: The transaction id.
    """
    return _out(_request("GET", f"/ledger/transactions/{tx_id}"))


@mcp.tool()
def reverse_transaction(tx_id: int, reason: str = "reversal") -> str:
    """Post a reversing transaction that negates an existing one. (Paid: write.)

    Args:
        tx_id: The transaction id to reverse.
        reason: Why it's being reversed (recorded on the reversing entry).
    """
    return _out(_request("POST", f"/ledger/transactions/{tx_id}/reverse",
                         body={"reason": reason}))


@mcp.tool()
def trial_balance(as_of: str | None = None) -> str:
    """Trial balance: every account with its balance, plus total debits and
    total credits (which must be equal in a balanced book). (Paid: read.)

    Args:
        as_of: Balances as of this date (YYYY-MM-DD); omit for current.
    """
    return _out(_request("GET", "/ledger/reports/trial-balance", query={"as_of": as_of}))


@mcp.tool()
def general_ledger(from_date: str, to_date: str | None = None,
                   account_id: int | None = None) -> str:
    """General ledger: per-account transaction detail over a date range. (Paid: read.)

    Args:
        from_date: Start date (YYYY-MM-DD), required.
        to_date: End date (YYYY-MM-DD); omit for through-today.
        account_id: Restrict to one account; omit for all.
    """
    return _out(_request("GET", "/ledger/reports/general-ledger",
                         query={"from": from_date, "to": to_date, "account_id": account_id}))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ledger MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse"],
                        default=os.environ.get("LEDGER_TRANSPORT", "stdio"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8384)
    args = parser.parse_args()
    if args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
