"""
tests/test_auth.py

Integration tests for authentication endpoints.

Covers:
  - POST /api/v1/auth/register
  - POST /api/v1/auth/login
  - GET  /api/v1/auth/me
  - POST /api/v1/auth/refresh  (including refresh-token rotation)
  - POST /api/v1/auth/logout

Run:
  docker compose exec api pytest tests/test_auth.py -v
"""

import uuid

import pytest
from httpx import AsyncClient


# =============================================================================
# POST /api/v1/auth/register
# =============================================================================

class TestRegister:
    async def test_register_success_returns_201_with_tokens(self, client: AsyncClient):
        email = f"reg_{uuid.uuid4().hex[:8]}@test.dev"
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "full_name": "New User", "password": "TestPass1"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["message"] == "Registration successful. Welcome to PyWallet!"
        assert data["user"]["email"] == email
        assert "access_token" in data["tokens"]
        assert "refresh_token" in data["tokens"]
        assert data["tokens"]["token_type"] == "bearer"

    async def test_register_creates_user_id(self, client: AsyncClient):
        email = f"uid_{uuid.uuid4().hex[:8]}@test.dev"
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "full_name": "ID User", "password": "TestPass1"},
        )
        assert resp.status_code == 201
        user_id = resp.json()["user"]["id"]
        assert user_id  # non-empty UUID string

    async def test_register_duplicate_email_returns_409(self, client: AsyncClient, registered_user: dict):
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": registered_user["email"],
                "full_name": "Dupe",
                "password": "TestPass1",
            },
        )
        assert resp.status_code == 409

    async def test_register_invalid_email_returns_422(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "not-an-email", "full_name": "Bad Email", "password": "TestPass1"},
        )
        assert resp.status_code == 422

    async def test_register_missing_fields_returns_422(self, client: AsyncClient):
        resp = await client.post("/api/v1/auth/register", json={"email": "x@x.com"})
        assert resp.status_code == 422

    async def test_register_password_too_long_returns_422(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"long_{uuid.uuid4().hex[:6]}@test.dev",
                "full_name": "Long Pass",
                "password": "A" * 129,
            },
        )
        assert resp.status_code == 422

    async def test_register_weak_password_returns_422(self, client: AsyncClient):
        # Passwords must meet complexity requirements
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"weak_{uuid.uuid4().hex[:6]}@test.dev",
                "full_name": "Weak Pass",
                "password": "short",
            },
        )
        assert resp.status_code == 422


# =============================================================================
# POST /api/v1/auth/login
# =============================================================================

class TestLogin:
    async def test_login_success_returns_200_with_tokens(self, client: AsyncClient, registered_user: dict):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": registered_user["email"], "password": registered_user["password"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "Login successful"
        assert "access_token" in data["tokens"]
        assert "refresh_token" in data["tokens"]
        assert data["user"]["email"] == registered_user["email"]

    async def test_login_wrong_password_returns_401(self, client: AsyncClient, registered_user: dict):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": registered_user["email"], "password": "WrongPass999"},
        )
        assert resp.status_code == 401

    async def test_login_unknown_email_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@nowhere.dev", "password": "TestPass1"},
        )
        assert resp.status_code == 401

    async def test_login_error_message_is_generic(self, client: AsyncClient, registered_user: dict):
        resp_wrong_pass = await client.post(
            "/api/v1/auth/login",
            json={"email": registered_user["email"], "password": "BadPass1"},
        )
        resp_wrong_email = await client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@nowhere.dev", "password": "TestPass1"},
        )
        # Both should return the same generic error (no user enumeration)
        assert resp_wrong_pass.status_code == 401
        assert resp_wrong_email.status_code == 401
        assert resp_wrong_pass.json()["detail"] == resp_wrong_email.json()["detail"]

    async def test_login_password_too_long_returns_422(self, client: AsyncClient, registered_user: dict):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": registered_user["email"], "password": "X" * 129},
        )
        assert resp.status_code == 422


# =============================================================================
# GET /api/v1/auth/me
# =============================================================================

class TestGetMe:
    async def test_me_returns_current_user(self, client: AsyncClient, registered_user: dict, auth_headers: dict):
        resp = await client.get("/api/v1/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["email"] == registered_user["email"]
        assert data["user"]["id"] == registered_user["user_id"]

    async def test_me_without_token_returns_401(self, client: AsyncClient):
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    async def test_me_with_invalid_token_returns_401(self, client: AsyncClient):
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer this.is.not.a.valid.jwt"},
        )
        assert resp.status_code == 401

    async def test_me_with_wrong_scheme_returns_401(self, client: AsyncClient, registered_user: dict):
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Basic {registered_user['access_token']}"},
        )
        assert resp.status_code == 401


# =============================================================================
# POST /api/v1/auth/refresh  — token rotation
# =============================================================================

class TestRefreshTokenRotation:
    async def test_refresh_issues_new_token_pair(self, client: AsyncClient, registered_user: dict):
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data["tokens"]
        assert "refresh_token" in data["tokens"]

    async def test_refresh_new_tokens_are_different_from_old(self, client: AsyncClient, registered_user: dict):
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        assert resp.status_code == 200
        new_tokens = resp.json()["tokens"]
        assert new_tokens["access_token"] != registered_user["access_token"]
        assert new_tokens["refresh_token"] != registered_user["refresh_token"]

    async def test_refresh_new_access_token_is_valid(self, client: AsyncClient, registered_user: dict):
        refresh_resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        new_access_token = refresh_resp.json()["tokens"]["access_token"]

        me_resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {new_access_token}"},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["user"]["id"] == registered_user["user_id"]

    async def test_refresh_old_token_invalid_after_rotation(self, client: AsyncClient, registered_user: dict):
        # First refresh — rotates the token
        await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        # Attempt to use the old refresh token again — must fail
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        assert resp.status_code == 401

    async def test_refresh_replay_attack_revokes_all_sessions(self, client: AsyncClient, registered_user: dict):
        # Step 1: rotate once → get new tokens
        first_refresh = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        new_tokens = first_refresh.json()["tokens"]

        # Step 2: replay the ORIGINAL token (already rotated) → replay detected
        replay_resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        assert replay_resp.status_code == 401

        # Step 3: the NEW refresh token should also be revoked (security: all sessions killed)
        new_refresh_resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": new_tokens["refresh_token"]},
        )
        assert new_refresh_resp.status_code == 401

    async def test_refresh_with_invalid_token_returns_401(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": "invalid.jwt.token"},
        )
        assert resp.status_code == 401


# =============================================================================
# POST /api/v1/auth/logout
# =============================================================================

class TestLogout:
    async def test_logout_returns_200(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/api/v1/auth/logout", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "Logged out successfully"

    async def test_logout_invalidates_refresh_token(self, client: AsyncClient, registered_user: dict, auth_headers: dict):
        # Logout
        await client.post("/api/v1/auth/logout", headers=auth_headers)

        # Attempt to refresh after logout — must fail (token was deleted from Redis)
        resp = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": registered_user["refresh_token"]},
        )
        assert resp.status_code == 401

    async def test_logout_without_token_returns_401(self, client: AsyncClient):
        resp = await client.post("/api/v1/auth/logout")
        assert resp.status_code == 401
