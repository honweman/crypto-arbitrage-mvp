from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import is_address, to_checksum_address


MAINNET_API_URL = "https://api.hyperliquid.xyz"
TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
AUTHORIZATION_TTL_SECONDS = 10 * 60
AGENT_NAME_PREFIX = "crypto-arb"


def hyperliquid_api_url(api_variant: str) -> str:
    return TESTNET_API_URL if api_variant == "testnet" else MAINNET_API_URL


def new_agent_credentials() -> tuple[str, str]:
    agent = Account.create()
    return agent.address, "0x" + bytes(agent.key).hex()


def build_agent_authorization(
    *,
    agent_address: str,
    agent_name: str,
    chain_id: int,
    api_variant: str,
    nonce: int | None = None,
) -> dict[str, Any]:
    if not is_address(agent_address):
        raise ValueError("agent address must be a valid EVM address")
    if chain_id <= 0:
        raise ValueError("wallet chain id must be positive")
    name = str(agent_name or "").strip()
    if not name or len(name) > 64:
        raise ValueError("agent name must contain 1-64 characters")
    if api_variant not in {"mainnet", "testnet"}:
        raise ValueError("unsupported Hyperliquid API variant")
    action_nonce = int(nonce if nonce is not None else time.time() * 1000)
    chain_name = "Testnet" if api_variant == "testnet" else "Mainnet"
    message = {
        "hyperliquidChain": chain_name,
        "agentAddress": to_checksum_address(agent_address),
        "agentName": name,
        "nonce": action_nonce,
    }
    typed_data = {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": chain_id,
            "verifyingContract": ZERO_ADDRESS,
        },
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "HyperliquidTransaction:ApproveAgent": [
                {"name": "hyperliquidChain", "type": "string"},
                {"name": "agentAddress", "type": "address"},
                {"name": "agentName", "type": "string"},
                {"name": "nonce", "type": "uint64"},
            ],
        },
        "primaryType": "HyperliquidTransaction:ApproveAgent",
        "message": message,
    }
    return {
        "action": {
            "type": "approveAgent",
            "signatureChainId": hex(chain_id),
            **message,
        },
        "nonce": action_nonce,
        "typed_data": typed_data,
    }


def recover_authorizer(typed_data: dict[str, Any], signature: str) -> str:
    signature_text = str(signature or "").strip()
    if not signature_text.startswith("0x"):
        signature_text = "0x" + signature_text
    if len(signature_text) != 132:
        raise ValueError("wallet signature must be a 65-byte hex value")
    try:
        signable = encode_typed_data(full_message=typed_data)
        return to_checksum_address(
            Account.recover_message(signable, signature=signature_text)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("wallet signature is invalid") from exc


def signature_parts(signature: str) -> dict[str, Any]:
    raw = bytes.fromhex(str(signature or "").removeprefix("0x"))
    if len(raw) != 65:
        raise ValueError("wallet signature must be 65 bytes")
    recovery_id = raw[64]
    if recovery_id in {0, 1}:
        recovery_id += 27
    if recovery_id not in {27, 28}:
        raise ValueError("wallet signature recovery id is invalid")
    return {
        "r": "0x" + raw[:32].hex(),
        "s": "0x" + raw[32:64].hex(),
        "v": recovery_id,
    }


async def submit_agent_authorization(
    *,
    action: dict[str, Any],
    nonce: int,
    signature: str,
    api_variant: str,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    api_url = hyperliquid_api_url(api_variant)
    timeout = aiohttp.ClientTimeout(total=max(2.0, timeout_seconds))
    request_payload = {
        "action": dict(action),
        "nonce": int(nonce),
        "signature": signature_parts(signature),
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{api_url}/exchange",
                json=request_payload,
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {str(body)[:240]}")
            if not isinstance(body, dict) or body.get("status") != "ok":
                raise RuntimeError(
                    f"Hyperliquid rejected authorization: {str(body)[:240]}"
                )
            async with session.post(
                f"{api_url}/info",
                json={"type": "userRole", "user": action["agentAddress"]},
            ) as response:
                role_payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(
                        f"agent verification HTTP {response.status}: "
                        f"{str(role_payload)[:200]}"
                    )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise RuntimeError(f"Hyperliquid authorization request failed: {exc}") from exc
    role = role_payload.get("role") if isinstance(role_payload, dict) else ""
    return {
        "status": "ok",
        "agent_address": action["agentAddress"],
        "agent_role": role,
        "agent_role_verified": role == "agent",
        "response_type": (body.get("response") or {}).get("type"),
    }
