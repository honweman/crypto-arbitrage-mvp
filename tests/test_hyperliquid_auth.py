from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

from arbitrage_bot.hyperliquid_auth import (
    build_agent_authorization,
    recover_authorizer,
    signature_parts,
)
from arbitrage_bot.user_workspace import (
    UserExchangeAccount,
    UserProject,
    UserWorkspaceStore,
)


class HyperliquidAuthorizationTest(unittest.TestCase):
    def test_typed_authorization_recovers_owner_and_encodes_signature(self) -> None:
        owner = Account.create()
        agent = Account.create()
        authorization = build_agent_authorization(
            agent_address=agent.address,
            agent_name="crypto-arb-test",
            chain_id=42161,
            api_variant="mainnet",
            nonce=1_800_000_000_000,
        )
        signed = owner.sign_message(
            encode_typed_data(full_message=authorization["typed_data"])
        )
        signature = signed.signature.hex()

        self.assertEqual(
            recover_authorizer(authorization["typed_data"], signature),
            owner.address,
        )
        parts = signature_parts(signature)
        self.assertIn(parts["v"], {27, 28})
        self.assertEqual(len(parts["r"]), 66)
        self.assertEqual(len(parts["s"]), 66)
        self.assertEqual(authorization["action"]["signatureChainId"], "0xa4b1")
        self.assertEqual(authorization["action"]["hyperliquidChain"], "Mainnet")

    def test_agent_secret_stays_encrypted_and_account_remains_disabled(self) -> None:
        owner = Account.create()
        master_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {"TEST_WORKSPACE_KEY": master_key},
            ),
        ):
            path = Path(tmp) / "workspace.sqlite3"
            store = UserWorkspaceStore(path, master_key_env="TEST_WORKSPACE_KEY")
            project = store.upsert_project(
                UserProject.from_dict(
                    {
                        "id": "project-hype",
                        "owner_email": "trader@example.com",
                        "name": "HYPE",
                        "asset": "HYPE",
                        "quote_currency": "USDC",
                        "status": "active",
                    }
                )
            )
            challenge = store.create_wallet_challenge(
                owner_email=project.owner_email,
                address=owner.address,
                chain_id=42161,
                wallet_type="metamask",
                domain="trade.example.com",
            )
            wallet = store.verify_wallet_challenge(
                owner_email=project.owner_email,
                challenge_id=challenge["challenge_id"],
                signature=owner.sign_message(
                    encode_defunct(text=challenge["message"])
                ).signature.hex(),
                label="MetaMask",
            )
            account = UserExchangeAccount.from_dict(
                {
                    "id": "account-hyperliquid",
                    "owner_email": project.owner_email,
                    "project_id": project.id,
                    "label": "Hyperliquid MetaMask",
                    "exchange": "hyperliquid",
                    "market_type": "swap",
                    "api_variant": "mainnet",
                    "symbol": "HYPE/USDC:USDC",
                    "enabled": False,
                    "withdrawal_disabled_confirmed": True,
                    "trade_permission_confirmed": True,
                }
            )

            pending = store.prepare_hyperliquid_authorization(
                owner_email=project.owner_email,
                wallet=wallet,
                account=account,
                chain_id=42161,
            )
            public_text = json.dumps(pending, sort_keys=True)
            self.assertNotIn("private_key", public_text.lower())
            self.assertNotIn('"secret"', public_text)
            self.assertEqual(pending["wallet_address"], owner.address)

            signature = owner.sign_message(
                encode_typed_data(full_message=pending["typed_data"])
            ).signature.hex()
            self.assertEqual(
                recover_authorizer(pending["typed_data"], signature),
                owner.address,
            )
            saved = store.finalize_hyperliquid_authorization(
                pending["authorization_id"],
                owner_email=project.owner_email,
            )
            credentials = store.decrypt_credentials(
                account_id=saved.id,
                owner_email=project.owner_email,
            )

            self.assertFalse(saved.enabled)
            self.assertEqual(saved.connection_status, "unverified")
            self.assertEqual(saved.wallet_id, wallet.id)
            self.assertEqual(saved.agent_address, pending["agent_address"])
            self.assertEqual(credentials["api_key"], owner.address)
            self.assertEqual(
                Account.from_key(credentials["secret"]).address,
                pending["agent_address"],
            )
            with self.assertRaisesRegex(ValueError, "revoke the API Wallet"):
                store.delete_wallet(wallet.id, owner_email=project.owner_email)
            with self.assertRaisesRegex(ValueError, "was not found"):
                store.get_hyperliquid_authorization(
                    pending["authorization_id"],
                    owner_email=project.owner_email,
                )
