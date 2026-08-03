#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from arbitrage_bot.config import load_config
from arbitrage_bot.runtime_account_migration import (
    import_runtime_accounts,
    write_runtime_credential_links,
)
from arbitrage_bot.user_workspace import UserWorkspaceStore


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move environment-backed runtime APIs into the encrypted account store."
    )
    parser.add_argument("--config", default="config.acs.json")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--exchange", action="append", dest="exchanges")
    parser.add_argument(
        "--confirm-withdrawals-disabled",
        action="store_true",
        help="Required acknowledgement for every imported API key.",
    )
    parser.add_argument("--write-config-links", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    store = UserWorkspaceStore(
        cfg.web_security.user_workspace_path,
        master_key_env=cfg.web_security.credential_master_key_env,
    )
    result = import_runtime_accounts(
        store,
        cfg,
        owner_email=args.owner,
        exchange_ids=args.exchanges,
        withdrawal_disabled_confirmed=args.confirm_withdrawals_disabled,
    )
    result["config_links_written"] = (
        write_runtime_credential_links(
            args.config,
            runtime_links=result["runtime_links"],
            owner_email=result["owner_email"],
            credential_store_path=cfg.web_security.user_workspace_path,
            credential_master_key_env=(
                cfg.web_security.credential_master_key_env
                or "CRYPTO_ARB_CREDENTIAL_MASTER_KEY"
            ),
        )
        if args.write_config_links
        else 0
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
