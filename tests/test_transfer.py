"""
tests/test_transfer.py

Integration tests for P2P wallet transfer endpoint.

Critical scenarios proven here:
  ✓ Successful transfer (double-entry ledger: DEBIT + CREDIT rows, same reference_id)
  ✓ Insufficient balance (400, neither wallet changes)
  ✓ Self-transfer (400, rejected before any lock)
  ✓ Receiver not found (404)
  ✓ Atomic rollback — validation failure mid-transfer leaves no partial state
  ✓ Concurrent transfers with asyncio.gather() — SELECT FOR UPDATE prevents double-spend

Run:
  docker compose exec api pytest tests/test_transfer.py -v
  docker compose exec api pytest tests/test_transfer.py -v -k concurrent -s
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient


def _ikey() -> str:
    return str(uuid.uuid4())


# =============================================================================
# POST /api/v1/wallet/transfer — happy path
# =============================================================================

class TestTransferHappyPath:
    async def test_transfer_success_returns_200(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={
                "receiver_email": second_user["email"],
                "amount": "100.00",
                "description": "Test payment",
            },
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "transfer_reference_id" in data
        assert "debit_transaction_id" in data
        assert Decimal(data["amount"]) == Decimal("100.00")

    async def test_transfer_deducts_sender_balance(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "250.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        balance_resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        assert Decimal(balance_resp.json()["balance"]) == Decimal("750.00")

    async def test_transfer_credits_receiver_balance(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "300.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        receiver_headers = {"Authorization": f"Bearer {second_user['access_token']}"}
        balance_resp = await client.get("/api/v1/wallet/balance", headers=receiver_headers)
        assert Decimal(balance_resp.json()["balance"]) == Decimal("300.00")

    async def test_transfer_response_contains_new_sender_balance(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "400.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 200
        assert Decimal(resp.json()["sender_new_balance"]) == Decimal("600.00")


# =============================================================================
# Double-entry ledger verification
# =============================================================================

class TestDoubleEntryLedger:
    async def test_transfer_creates_debit_and_credit_rows(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        transfer_resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "150.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert transfer_resp.status_code == 200
        ref_id = transfer_resp.json()["transfer_reference_id"]

        # Sender sees the DEBIT in their transaction history
        sender_txns = await client.get("/api/v1/wallet/transactions", headers=auth_headers)
        sender_items = sender_txns.json()["items"]
        debit_rows = [t for t in sender_items if t["transaction_type"] == "DEBIT"]
        assert len(debit_rows) == 1
        assert debit_rows[0]["transfer_reference_id"] == ref_id
        assert Decimal(debit_rows[0]["amount"]) == Decimal("150.00")

    async def test_transfer_receiver_sees_credit_row(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        transfer_resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "75.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        ref_id = transfer_resp.json()["transfer_reference_id"]

        receiver_headers = {"Authorization": f"Bearer {second_user['access_token']}"}
        recv_txns = await client.get("/api/v1/wallet/transactions", headers=receiver_headers)
        credit_rows = [t for t in recv_txns.json()["items"] if t["transaction_type"] == "CREDIT"]
        assert len(credit_rows) == 1
        assert credit_rows[0]["transfer_reference_id"] == ref_id
        assert Decimal(credit_rows[0]["amount"]) == Decimal("75.00")

    async def test_transfer_debit_and_credit_share_reference_id(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        transfer_resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "50.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        ref_id = transfer_resp.json()["transfer_reference_id"]
        debit_id = transfer_resp.json()["debit_transaction_id"]

        receiver_headers = {"Authorization": f"Bearer {second_user['access_token']}"}
        recv_txns = await client.get("/api/v1/wallet/transactions", headers=receiver_headers)
        credit_rows = recv_txns.json()["items"]
        assert len(credit_rows) == 1
        # Both DEBIT and CREDIT reference the same transfer
        assert credit_rows[0]["transfer_reference_id"] == ref_id

    async def test_transfer_balance_snapshots_are_correct(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        transfer_resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "200.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        debit_tx_id = transfer_resp.json()["debit_transaction_id"]

        debit_resp = await client.get(
            f"/api/v1/wallet/transactions/{debit_tx_id}", headers=auth_headers
        )
        assert debit_resp.status_code == 200
        debit_data = debit_resp.json()
        # Started with $1000, transferred $200
        assert Decimal(debit_data["balance_before"]) == Decimal("1000.00")
        assert Decimal(debit_data["balance_after"]) == Decimal("800.00")


# =============================================================================
# Validation failures
# =============================================================================

class TestTransferValidation:
    async def test_insufficient_balance_returns_400(
        self, client: AsyncClient, registered_user: dict, second_user: dict, auth_headers: dict
    ):
        # registered_user has $0 (not funded_user)
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "1.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 400
        assert "Insufficient" in resp.json()["detail"]

    async def test_insufficient_balance_sender_balance_unchanged(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        # Attempt to overdraw by more than balance
        await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "9999.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        balance_resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        # Balance must be unchanged — full $1000 still present
        assert Decimal(balance_resp.json()["balance"]) == Decimal("1000.00")

    async def test_insufficient_balance_receiver_balance_unchanged(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "9999.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        receiver_headers = {"Authorization": f"Bearer {second_user['access_token']}"}
        balance_resp = await client.get("/api/v1/wallet/balance", headers=receiver_headers)
        # Receiver must also be unchanged — no partial credit
        assert Decimal(balance_resp.json()["balance"]) == Decimal("0.00")

    async def test_self_transfer_returns_400(
        self, client: AsyncClient, funded_user: dict, auth_headers: dict
    ):
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": funded_user["email"], "amount": "10.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 400

    async def test_receiver_not_found_returns_404(
        self, client: AsyncClient, funded_user: dict, auth_headers: dict
    ):
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": "ghost@nobody.dev", "amount": "10.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 404

    async def test_negative_amount_returns_422(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "-10.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 422

    async def test_zero_amount_returns_422(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "0.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 422

    async def test_transfer_requires_idempotency_key(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "10.00"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_transfer_requires_auth(
        self, client: AsyncClient, second_user: dict
    ):
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "10.00"},
            headers={"Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 401


# =============================================================================
# Atomic rollback — validation failure leaves no partial state
# =============================================================================

class TestTransferAtomicRollback:
    async def test_failed_transfer_leaves_no_transaction_rows(
        self, client: AsyncClient, registered_user: dict, second_user: dict, auth_headers: dict
    ):
        # registered_user has $0 — transfer will fail with insufficient funds
        await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "500.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        # No transactions should exist for the sender (nothing committed)
        txns_resp = await client.get("/api/v1/wallet/transactions", headers=auth_headers)
        assert txns_resp.json()["total"] == 0

    async def test_failed_transfer_leaves_no_receiver_credit(
        self, client: AsyncClient, registered_user: dict, second_user: dict, auth_headers: dict
    ):
        await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "500.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        receiver_headers = {"Authorization": f"Bearer {second_user['access_token']}"}
        txns_resp = await client.get("/api/v1/wallet/transactions", headers=receiver_headers)
        # Receiver must have zero transactions — no partial CREDIT written
        assert txns_resp.json()["total"] == 0

    async def test_transfer_exact_balance_succeeds(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        # $1000 balance, transfer exactly $1000 — should succeed (not fail)
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "1000.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 200
        assert Decimal(resp.json()["sender_new_balance"]) == Decimal("0.00")

    async def test_transfer_one_cent_over_balance_fails(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "1000.01"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 400


# =============================================================================
# Concurrent transfers — SELECT FOR UPDATE prevents double-spend
#
# These tests use asyncio.gather() to fire multiple requests simultaneously,
# exercising the real SELECT FOR UPDATE locking behavior. They rely on:
#   - Real PostgreSQL (mocks cannot simulate row-level locking)
#   - asyncio.gather() on AsyncClient (each request gets its own session)
#   - The TRUNCATE + fresh fixtures providing a known starting balance
# =============================================================================

@pytest.mark.slow
class TestConcurrentTransfers:
    async def test_concurrent_deposits_accumulate_correctly(
        self, client: AsyncClient, auth_headers: dict
    ):
        # Fire 5 simultaneous deposits of $100 each
        tasks = [
            client.post(
                "/api/v1/wallet/deposit",
                json={"amount": "100.00"},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )
            for _ in range(5)
        ]
        responses = await asyncio.gather(*tasks)

        # All should succeed
        for r in responses:
            assert r.status_code == 201, f"Deposit failed: {r.json()}"

        # Balance must be exactly $500 (5 × $100)
        # SELECT FOR UPDATE ensures no two deposits read the same balance snapshot
        balance_resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        assert Decimal(balance_resp.json()["balance"]) == Decimal("500.00")

    async def test_concurrent_transfers_cannot_exceed_balance(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        # Funded user has $1000. Fire 5 concurrent transfers of $300 each.
        # Total requested: $1500 > $1000 available.
        # Exactly some will succeed (as many $300 chunks as balance allows: 3 max),
        # and the rest will fail with 400 insufficient funds.
        tasks = [
            client.post(
                "/api/v1/wallet/transfer",
                json={"receiver_email": second_user["email"], "amount": "300.00"},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )
            for _ in range(5)
        ]
        responses = await asyncio.gather(*tasks)

        successes = [r for r in responses if r.status_code == 200]
        failures = [r for r in responses if r.status_code == 400]

        # At most 3 transfers of $300 can succeed from a $1000 balance
        assert len(successes) <= 3
        assert len(successes) + len(failures) == 5

        # Final sender balance must be non-negative (never went into debt)
        balance_resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        sender_final = Decimal(balance_resp.json()["balance"])
        assert sender_final >= Decimal("0.00")

        # Total moved = successes × $300; balance math must hold
        expected_final = Decimal("1000.00") - len(successes) * Decimal("300.00")
        assert sender_final == expected_final

    async def test_concurrent_transfers_money_is_conserved(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        # Money conservation: sender_balance + receiver_balance must stay constant.
        # This proves no money is created or destroyed under concurrency.
        transfer_amount = Decimal("100.00")
        n_transfers = 5

        tasks = [
            client.post(
                "/api/v1/wallet/transfer",
                json={"receiver_email": second_user["email"], "amount": str(transfer_amount)},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )
            for _ in range(n_transfers)
        ]
        responses = await asyncio.gather(*tasks)
        successes = sum(1 for r in responses if r.status_code == 200)

        sender_balance_resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        receiver_headers = {"Authorization": f"Bearer {second_user['access_token']}"}
        receiver_balance_resp = await client.get("/api/v1/wallet/balance", headers=receiver_headers)

        sender_final = Decimal(sender_balance_resp.json()["balance"])
        receiver_final = Decimal(receiver_balance_resp.json()["balance"])

        # Initial total = $1000 (funded_user) + $0 (second_user)
        assert sender_final + receiver_final == Decimal("1000.00")
        # Receiver got exactly the successful transfer amount
        assert receiver_final == successes * transfer_amount
