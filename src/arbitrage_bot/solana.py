from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiohttp


TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
HOLDER_CHANGE_EPSILON = 1e-9
ONCHAIN_RECENT_EVENT_LIMIT = 200


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


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _string_float_map(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): _float_or_zero(value) for key, value in raw.items()}


def _string_int_map(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


class OnchainHolderHistoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)


def _empty_holder_history(
    *,
    mint: str,
    label: str,
    holders: list[dict[str, Any]],
    observed_at: float,
) -> dict[str, Any]:
    latest_amounts = {
        str(holder["owner"]): _float_or_zero(holder.get("amount"))
        for holder in holders
    }
    latest_ranks = {
        str(holder["owner"]): int(holder.get("rank") or index)
        for index, holder in enumerate(holders, start=1)
    }
    return {
        "version": 1,
        "mint": mint,
        "label": label,
        "baseline_at": observed_at,
        "updated_at": observed_at,
        "baseline_amounts": latest_amounts,
        "latest_amounts": latest_amounts,
        "latest_ranks": latest_ranks,
        "latest_holders": [dict(holder) for holder in holders],
        "events": [],
    }


def update_holder_history(
    *,
    path: str | Path,
    mint: str,
    label: str,
    holders: list[dict[str, Any]],
    address_labels: dict[str, str],
    observed_at: float | None = None,
) -> dict[str, Any]:
    observed_at = observed_at or time.time()
    store = OnchainHolderHistoryStore(path)
    current_amounts = {
        str(holder["owner"]): _float_or_zero(holder.get("amount"))
        for holder in holders
    }
    current_ranks = {
        str(holder["owner"]): int(holder.get("rank") or index)
        for index, holder in enumerate(holders, start=1)
    }

    payload = store.load()
    if payload is None or payload.get("mint") != mint:
        payload = _empty_holder_history(
            mint=mint,
            label=label,
            holders=holders,
            observed_at=observed_at,
        )

    baseline_amounts = _string_float_map(payload.get("baseline_amounts"))
    latest_amounts = _string_float_map(payload.get("latest_amounts"))
    latest_ranks = _string_int_map(payload.get("latest_ranks"))
    previous_latest_amounts = dict(latest_amounts)
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    events = [event for event in events if isinstance(event, dict)]
    new_events: list[dict[str, Any]] = []

    for holder in holders:
        owner = str(holder["owner"])
        amount = current_amounts[owner]
        previous_amount = latest_amounts.get(owner)
        if previous_amount is None:
            if abs(amount) <= HOLDER_CHANGE_EPSILON:
                continue
            previous_amount = 0.0
            event_type = "entered_top_holders"
        else:
            if abs(amount - previous_amount) <= HOLDER_CHANGE_EPSILON:
                continue
            event_type = "balance_change"

        baseline_amount = baseline_amounts.get(owner, 0.0)
        owner_label = address_labels.get(owner)
        new_events.append(
            {
                "observed_at": observed_at,
                "mint": mint,
                "owner": owner,
                "label": owner_label or "Unknown",
                "is_labeled": owner_label is not None,
                "rank": current_ranks.get(owner),
                "previous_rank": latest_ranks.get(owner),
                "amount": amount,
                "previous_amount": previous_amount,
                "delta_amount": amount - previous_amount,
                "cumulative_delta_amount": amount - baseline_amount,
                "event_type": event_type,
            }
        )

    latest_amounts.update(current_amounts)
    latest_ranks.update(current_ranks)
    events.extend(new_events)
    payload.update(
        {
            "version": 1,
            "mint": mint,
            "label": label,
            "updated_at": observed_at,
            "baseline_amounts": baseline_amounts,
            "latest_amounts": latest_amounts,
            "latest_ranks": latest_ranks,
            "events": events,
        }
    )

    event_counts: dict[str, int] = defaultdict(int)
    last_events_by_owner: dict[str, dict[str, Any]] = {}
    for event in events:
        owner = str(event.get("owner") or "")
        if not owner:
            continue
        event_counts[owner] += 1
        if event.get("observed_at") is not None:
            last_events_by_owner[owner] = event

    for holder in holders:
        owner = str(holder["owner"])
        amount = current_amounts[owner]
        baseline_amount = baseline_amounts.get(owner, 0.0)
        previous_amount = previous_latest_amounts.get(owner)
        last_event = last_events_by_owner.get(owner)
        holder["baseline_amount"] = baseline_amount
        holder["cumulative_delta_amount"] = amount - baseline_amount
        holder["delta_amount"] = holder["cumulative_delta_amount"]
        holder["last_delta_amount"] = (
            _float_or_zero(last_event.get("delta_amount")) if last_event else 0.0
        )
        holder["last_change_at"] = last_event.get("observed_at") if last_event else None
        holder["change_count"] = event_counts.get(owner, 0)
        holder["previous_amount"] = previous_amount

    payload["latest_holders"] = [dict(holder) for holder in holders]
    store.save(payload)

    return {
        "enabled": True,
        "path": str(store.path),
        "baseline_at": payload.get("baseline_at"),
        "updated_at": payload.get("updated_at"),
        "event_count": len(events),
        "new_event_count": len(new_events),
        "recent_events": list(reversed(events[-ONCHAIN_RECENT_EVENT_LIMIT:])),
    }


def _holder_history_summary(
    *,
    store: OnchainHolderHistoryStore,
    payload: dict[str, Any],
) -> dict[str, Any]:
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    events = [event for event in events if isinstance(event, dict)]
    return {
        "enabled": True,
        "path": str(store.path),
        "baseline_at": payload.get("baseline_at"),
        "updated_at": payload.get("updated_at"),
        "event_count": len(events),
        "new_event_count": 0,
        "recent_events": list(reversed(events[-ONCHAIN_RECENT_EVENT_LIMIT:])),
    }


def _enrich_cached_holders_from_history(
    holders: list[dict[str, Any]],
    *,
    payload: dict[str, Any],
    address_labels: dict[str, str],
) -> list[dict[str, Any]]:
    baseline_amounts = _string_float_map(payload.get("baseline_amounts"))
    latest_amounts = _string_float_map(payload.get("latest_amounts"))
    latest_ranks = _string_int_map(payload.get("latest_ranks"))
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    events = [event for event in events if isinstance(event, dict)]
    event_counts: dict[str, int] = defaultdict(int)
    last_events_by_owner: dict[str, dict[str, Any]] = {}
    for event in events:
        owner = str(event.get("owner") or "")
        if not owner:
            continue
        event_counts[owner] += 1
        if event.get("observed_at") is not None:
            last_events_by_owner[owner] = event

    enriched: list[dict[str, Any]] = []
    for index, raw_holder in enumerate(holders, start=1):
        holder = dict(raw_holder)
        owner = str(holder.get("owner") or "")
        if not owner:
            continue
        amount = _float_or_zero(holder.get("amount"))
        if amount == 0.0 and owner in latest_amounts:
            amount = latest_amounts[owner]
        rank = int(holder.get("rank") or latest_ranks.get(owner) or index)
        owner_label = address_labels.get(owner) or str(holder.get("label") or "Unknown")
        baseline_amount = baseline_amounts.get(owner, amount)
        last_event = last_events_by_owner.get(owner)
        holder.update(
            {
                "owner": owner,
                "rank": rank,
                "amount": amount,
                "label": owner_label,
                "is_labeled": owner in address_labels,
                "token_account_count": int(holder.get("token_account_count") or 0),
                "share_pct": holder.get("share_pct"),
                "baseline_amount": baseline_amount,
                "cumulative_delta_amount": amount - baseline_amount,
                "delta_amount": amount - baseline_amount,
                "last_delta_amount": (
                    _float_or_zero(last_event.get("delta_amount")) if last_event else 0.0
                ),
                "last_change_at": last_event.get("observed_at") if last_event else None,
                "change_count": event_counts.get(owner, 0),
            }
        )
        enriched.append(holder)
    return sorted(enriched, key=lambda item: int(item.get("rank") or 0))


def load_cached_holder_snapshot(
    *,
    path: str | Path,
    mint: str,
    label: str,
    address_labels: dict[str, str],
    top_n: int,
) -> dict[str, Any] | None:
    store = OnchainHolderHistoryStore(path)
    payload = store.load()
    if payload is None or payload.get("mint") != mint:
        return None

    holders = payload.get("latest_holders")
    if not isinstance(holders, list) or not holders:
        latest_amounts = _string_float_map(payload.get("latest_amounts"))
        latest_ranks = _string_int_map(payload.get("latest_ranks"))
        holders = [
            {
                "owner": owner,
                "amount": amount,
                "rank": latest_ranks.get(owner),
                "token_account_count": 0,
                "share_pct": None,
            }
            for owner, amount in latest_amounts.items()
        ]
    holders = [
        holder for holder in holders
        if isinstance(holder, dict) and str(holder.get("owner") or "")
    ]
    holders = _enrich_cached_holders_from_history(
        holders,
        payload=payload,
        address_labels=address_labels,
    )[:top_n]
    if not holders:
        return None
    return {
        "status": "cached",
        "label": label,
        "mint": mint,
        "holders": holders,
        "history": _holder_history_summary(store=store, payload=payload),
        "last_finished": payload.get("updated_at"),
        "error": None,
        "cached": True,
    }
