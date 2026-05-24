"""
tests/test_wallet.py

Integration tests for wallet endpoints.

Covers:
  - GET  /api/v1/wallet/balance
  - POST /api/v1/wallet/deposit   (idempotent)
  - GET  /api/v1/wallet/transactions
  - GET  /api/v1/wallet/transactions/{id}

Run:
  docker compose exec api pytest tests/test_wallet.py -v
"""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient


def _ikey() -> str:
    return str(uuid.uuid4())


# =============================================================================
# GET /api/v1/wallet/balance
# =============================================================================

class TestGetBalance:
    async def test_balance_zero_for_new_user(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert Decimal(data["balance"]) == Decimal("0.00")
        assert data["currency"] == "USD"

    async def test_balance_without_token_returns_401(self, client: AsyncClient):
        resp = await client.get("/api/v1/wallet/balance")
        assert resp.status_code == 401

    async def test_balance_updates_after_deposit(self, client: AsyncClient, auth_headers: dict):
        await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "250.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        assert resp.status_code == 200
        assert Decimal(resp.json()["balance"]) == Decimal("250.00")

    async def test_balance_response_has_required_fields(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "balance" in data
        assert "currency" in data
        assert "wallet_id" in data


# =============================================================================
# POST /api/v1/wallet/deposit
# =============================================================================

class TestDeposit:
    async def test_deposit_returns_201_with_transaction(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "100.00", "description": "First deposit"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "transaction" in data
        assert "new_balance" in data
        assert Decimal(data["new_balance"]) == Decimal("100.00")

    async def test_deposit_transaction_has_pending_status(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "50.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 201
        # Deposit creates a PENDING transaction; Celery would move it to COMPLETED
        assert resp.json()["transaction"]["status"] == "PENDING"

    async def test_deposit_without_idempotency_key_returns_400(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "100.00"},
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_deposit_without_token_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "100.00"},
            headers={"Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 401

    async def test_deposit_negative_amount_returns_422(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "-50.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 422

    async def test_deposit_zero_amount_returns_422(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "0.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 422

    async def test_deposit_nan_returns_422(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "NaN"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 422

    async def test_deposit_too_many_decimal_places_returns_422(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "10.001"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 422

    async def test_deposit_exact_decimal_precision_accepted(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "99.99"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 201

    async def test_multiple_deposits_accumulate_correctly(self, client: AsyncClient, auth_headers: dict):
        for amount in ["100.00", "200.50", "49.50"]:
            await client.post(
                "/api/v1/wallet/deposit",
                json={"amount": amount},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )
        resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        assert Decimal(resp.json()["balance"]) == Decimal("350.00")

    async def test_deposit_idempotency_replay_returns_same_response(self, client: AsyncClient, auth_headers: dict):
        key = _ikey()
        payload = {"amount": "75.00", "description": "Idempotent deposit"}

        first = await client.post(
            "/api/v1/wallet/deposit",
            json=payload,
            headers={**auth_headers, "Idempotency-Key": key},
        )
        second = await client.post(
            "/api/v1/wallet/deposit",
            json=payload,
            headers={**auth_headers, "Idempotency-Key": key},
        )

        assert first.status_code == 201
        assert second.status_code == 201
        # Replayed — same transaction ID, same balance
        assert first.json()["transaction"]["id"] == second.json()["transaction"]["id"]
        assert first.json()["new_balance"] == second.json()["new_balance"]

    async def test_deposit_idempotency_no_double_credit(self, client: AsyncClient, auth_headers: dict):
        key = _ikey()
        payload = {"amount": "500.00"}

        await client.post("/api/v1/wallet/deposit", json=payload, headers={**auth_headers, "Idempotency-Key": key})
        await client.post("/api/v1/wallet/deposit", json=payload, headers={**auth_headers, "Idempotency-Key": key})
        await client.post("/api/v1/wallet/deposit", json=payload, headers={**auth_headers, "Idempotency-Key": key})

        balance_resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        # Must be 500 (once), NOT 1500 (three times)
        assert Decimal(balance_resp.json()["balance"]) == Decimal("500.00")

    async def test_deposit_payload_mismatch_same_key_returns_422(self, client: AsyncClient, auth_headers: dict):
        key = _ikey()
        await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "100.00"},
            headers={**auth_headers, "Idempotency-Key": key},
        )
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "999.00"},  # different body, same key
            headers={**auth_headers, "Idempotency-Key": key},
        )
        assert resp.status_code == 422


# =============================================================================
# GET /api/v1/wallet/transactions
# =============================================================================

class TestListTransactions:
    async def test_transactions_empty_for_new_user(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/api/v1/wallet/transactions", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_transactions_includes_deposit(self, client: AsyncClient, auth_headers: dict):
        await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "123.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        resp = await client.get("/api/v1/wallet/transactions", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert Decimal(data["items"][0]["amount"]) == Decimal("123.00")

    async def test_transactions_ordered_newest_first(self, client: AsyncClient, auth_headers: dict):
        for amount in ["10.00", "20.00", "30.00"]:
            await client.post(
                "/api/v1/wallet/deposit",
                json={"amount": amount},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )
        resp = await client.get("/api/v1/wallet/transactions", headers=auth_headers)
        items = resp.json()["items"]
        assert len(items) == 3
        amounts = [Decimal(i["amount"]) for i in items]
        # Last deposited ($30) should be first in the list
        assert amounts[0] == Decimal("30.00")

    async def test_transactions_pagination(self, client: AsyncClient, auth_headers: dict):
        for _ in range(5):
            await client.post(
                "/api/v1/wallet/deposit",
                json={"amount": "1.00"},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )
        resp = await client.get(
            "/api/v1/wallet/transactions?limit=3&offset=0",
            headers=auth_headers,
        )
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 3

    async def test_transactions_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/wallet/transactions")
        assert resp.status_code == 401

    async def test_transactions_isolated_from_other_users(
        self, client: AsyncClient, auth_headers: dict, second_user: dict
    ):
        # Deposit as primary user
        await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "100.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        # Second user sees zero transactions
        second_headers = {"Authorization": f"Bearer {second_user['access_token']}"}
        resp = await client.get("/api/v1/wallet/transactions", headers=second_headers)
        assert resp.json()["total"] == 0


# =============================================================================
# GET /api/v1/wallet/transactions/{id}
# =============================================================================

class TestGetTransaction:
    async def test_get_transaction_returns_correct_data(self, client: AsyncClient, auth_headers: dict):
        deposit_resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "88.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        tx_id = deposit_resp.json()["transaction"]["id"]

        resp = await client.get(f"/api/v1/wallet/transactions/{tx_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == tx_id
        assert Decimal(resp.json()["amount"]) == Decimal("88.00")

    async def test_get_transaction_not_found_returns_404(self, client: AsyncClient, auth_headers: dict):
        fake_id = str(uuid.uuid4())
        resp = await client.get(f"/api/v1/wallet/transactions/{fake_id}", headers=auth_headers)
        assert resp.status_code == 404

    async def test_get_transaction_idor_prevention(
        self, client: AsyncClient, auth_headers: dict, second_user: dict
    ):
        # Primary user deposits and gets tx_id
        deposit_resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "55.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        tx_id = deposit_resp.json()["transaction"]["id"]

        # Second user tries to access primary user's transaction — must get 404
        second_headers = {"Authorization": f"Bearer {second_user['access_token']}"}
        resp = await client.get(f"/api/v1/wallet/transactions/{tx_id}", headers=second_headers)
        assert resp.status_code == 404

    async def test_get_transaction_requires_auth(self, client: AsyncClient, auth_headers: dict):
        deposit_resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "10.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        tx_id = deposit_resp.json()["transaction"]["id"]
        resp = await client.get(f"/api/v1/wallet/transactions/{tx_id}")
        assert resp.status_code == 401
