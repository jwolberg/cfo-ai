"""The Plaid transport rung (tickets 0034-0038): Link exchange, the webhook doorbell, the
Cloud Tasks fan-out, and the cursored `/transactions/sync` loop. Everything that lands real
rows in `plaid_transactions` and stops one seam short of `assemble_snapshot()`.
"""
