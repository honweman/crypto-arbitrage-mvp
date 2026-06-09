from __future__ import annotations

from collections import defaultdict
from typing import Any

import aiohttp


TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


class SolanaRpcError(RuntimeError):
    pass


class SolanaTokenClient:
    def __init__(self, rpc_url: str) -> None:
        self.rpc_url = rpc_url
        self._session: aiohttp.ClientSession | None = None
        self._request_id = 0

    async def __aenter__(self) -> SolanaTokenClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def rpc(self, method: str, params: list[Any]) -> Any:
        await self.start()
        assert self._session is not None
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        async with self._session.post(self.rpc_url, json=payload, timeout=20) as resp:
            data = await resp.json(content_type=None)
        if "error" in data:
            raise SolanaRpcError(str(data["error"]))
        return data["result"]

    async def token_supply(self, mint: str) -> dict[str, Any]:
        result = await self.rpc("getTokenSupply", [mint])
        return result["value"]

    async def token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        result = await self.rpc("getTokenLargestAccounts", [mint])
        return list(result["value"])

    async def token_accounts_by_address(
        self,
        account_addresses: list[str],
    ) -> dict[str, dict[str, Any]]:
        if not account_addresses:
            return {}
        params = [
            account_addresses,
            {
                "encoding": "jsonParsed",
                "commitment": "confirmed",
            },
        ]
        try:
            result = await self.rpc("getMultipleAccounts", params)
        except SolanaRpcError:
            return await self.token_accounts_by_address_individual(account_addresses)

        values = result["value"]
        return {
            address: value
            for address, value in zip(account_addresses, values, strict=False)
            if value is not None
        }

    async def token_accounts_by_address_individual(
        self,
        account_addresses: list[str],
    ) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        for address in account_addresses:
            result = await self.rpc(
                "getAccountInfo",
                [
                    address,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                    },
                ],
            )
            value = result.get("value")
            if value is not None:
                results[address] = value
        return results


def _amount_from_ui_string(value: str | None) -> float:
    if value is None:
        return 0.0
    return float(value)


def aggregate_largest_token_accounts_by_owner(
    largest_accounts: list[dict[str, Any]],
    account_infos: dict[str, dict[str, Any]],
    *,
    top_n: int,
    total_supply_ui: float | None = None,
) -> list[dict[str, Any]]:
    owners: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "owner": "",
            "amount": 0.0,
            "token_accounts": [],
        }
    )

    for account in largest_accounts:
        token_account = account["address"]
        account_info = account_infos.get(token_account)
        parsed_info = (
            account_info.get("data", {})
            .get("parsed", {})
            .get("info", {})
            if account_info
            else {}
        )
        owner = parsed_info.get("owner", token_account)
        amount = _amount_from_ui_string(account.get("uiAmountString"))
        row = owners[owner]
        row["owner"] = owner
        row["amount"] += amount
        row["token_accounts"].append(token_account)

    rows = sorted(owners.values(), key=lambda item: item["amount"], reverse=True)
    for index, row in enumerate(rows[:top_n], start=1):
        row["rank"] = index
        if total_supply_ui and total_supply_ui > 0:
            row["share_pct"] = row["amount"] / total_supply_ui * 100
        else:
            row["share_pct"] = None
        row["token_account_count"] = len(row["token_accounts"])
    return rows[:top_n]


async def fetch_top_token_owners(
    client: SolanaTokenClient,
    mint: str,
    *,
    top_n: int,
) -> dict[str, Any]:
    supply = await client.token_supply(mint)
    supply_ui = _amount_from_ui_string(supply.get("uiAmountString"))
    largest_accounts = await client.token_largest_accounts(mint)
    account_addresses = [item["address"] for item in largest_accounts]
    account_infos = await client.token_accounts_by_address(account_addresses)
    rows = aggregate_largest_token_accounts_by_owner(
        largest_accounts,
        account_infos,
        top_n=top_n,
        total_supply_ui=supply_ui,
    )
    return {
        "mint": mint,
        "supply": supply_ui,
        "decimals": supply.get("decimals"),
        "holders": rows,
        "source_account_count": len(largest_accounts),
    }
