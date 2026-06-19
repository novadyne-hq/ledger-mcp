# Ledger MCP

**Double-entry accounting for AI agents.** An MCP server over the
[Ledger API](https://ledger.novadyne.ai) — create a chart of accounts, post
balanced journal entries, and pull trial-balance / general-ledger reports,
all from inside your agent.

Ledger is **agent-native**: it's paid **per call via [x402](https://x402.org)
micropayments** (USDC on the Base network), so an agent can use real
double-entry bookkeeping without an account, API key, or subscription — it just
pays a fraction of a cent per call from its own wallet.

## Install

```bash
uvx ledger-mcp
```

Or add it to your MCP client (`claude-code-config.json` / Claude Desktop):

```json
{
  "mcpServers": {
    "ledger": {
      "command": "uvx",
      "args": ["ledger-mcp"],
      "env": {
        "LEDGER_X402_PRIVATE_KEY": "0xYOUR_FUNDED_BASE_WALLET_KEY"
      }
    }
  }
}
```

## Payment

Most tools are **paid per call** (reads ~$0.002, writes ~$0.01, in USDC on Base):

- Set `LEDGER_X402_PRIVATE_KEY` to the private key of a **funded Base wallet**
  (holding USDC). The server signs the x402 (EIP-3009) payment automatically on
  each call — gasless, you only spend the USDC the call costs.
- Without a key, `health` and `discover` still work, and paid tools return the
  exact price plus how to enable payment (nothing is spent).
- Use a **dedicated low-balance wallet** for your agent — never your main key.
  The balance is the blast radius.

Run `discover` to see every endpoint and its current price before spending.

## Tools

| Tool | Cost | What it does |
|------|------|--------------|
| `health` | free | API status + version |
| `discover` | free | List paid endpoints + x402 prices |
| `create_account` | write | Add an account (asset/liability/equity/revenue/expense) |
| `list_accounts` | read | Chart of accounts + balances |
| `get_account` | read | One account's balance (optionally as-of a date) |
| `post_transaction` | write | Record a balanced journal entry |
| `list_transactions` | read | Journal, newest first |
| `get_transaction` | read | One transaction with all entries |
| `reverse_transaction` | write | Post a reversing entry |
| `trial_balance` | read | All balances + debit/credit totals |
| `general_ledger` | read | Per-account detail over a date range |

## The one rule: entries sum to zero

Every transaction is two or more entries whose **signed amounts sum to exactly
0**. Amounts are integers in **minor units (cents)**: a **debit is positive**, a
**credit is negative**.

Record a $500 cash sale (debit Cash, credit Sales Revenue):

```json
{
  "date": "2026-06-19",
  "description": "Cash sale",
  "entries": [
    {"account_id": 1, "amount": 50000, "memo": "cash in"},
    {"account_id": 2, "amount": -50000, "memo": "sales revenue"}
  ]
}
```

The server rejects an unbalanced transaction before spending.

## Links

- Docs & API: <https://ledger.novadyne.ai>
- API base: `https://ledger-api.novadyne.ai`
- x402 discovery: `https://ledger-api.novadyne.ai/.well-known/x402`

MIT licensed. By [Novadyne](https://novadyne.ai). The API backend is operated by
Novadyne; this package is the open client that talks to it.
