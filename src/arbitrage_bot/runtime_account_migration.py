from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import BotConfig, ExchangeConfig
from .user_workspace import UserApiConnection, UserWorkspaceStore


@dataclass(frozen=True)
class RuntimeAccountGroup:
    exchange: str
    api_variant: str
    runtime_keys: tuple[str, ...]
    market_types: tuple[str, ...]
    api_key_env: str
    secret_env: str
    password_env: str


def _catalog_exchange_id(exchange: ExchangeConfig) -> str:
    return "binance" if exchange.id == "binanceusdm" else exchange.id


def _api_variant(exchange: ExchangeConfig) -> str:
    if exchange.id == "bithumb":
        return "v2"
    if exchange.id == "upbit":
        hostname = str(exchange.options.get("hostname") or "").lower()
        return "indonesia" if hostname.startswith("id-") else "korea"
    return "default"


def runtime_account_groups(
    cfg: BotConfig,
    *,
    exchange_ids: Iterable[str] | None = None,
) -> list[RuntimeAccountGroup]:
    allowed = {
        str(item).strip().lower() for item in (exchange_ids or ()) if str(item).strip()
    }
    grouped: dict[tuple[str, str, str, str, str], list[ExchangeConfig]] = {}
    for exchange in [*cfg.spot_exchanges, *cfg.derivative_exchanges]:
        catalog_id = _catalog_exchange_id(exchange)
        if allowed and catalog_id not in allowed:
            continue
        if not exchange.api_key_env or not exchange.secret_env:
            continue
        key = (
            catalog_id,
            _api_variant(exchange),
            exchange.api_key_env,
            exchange.secret_env,
            exchange.password_env or "",
        )
        grouped.setdefault(key, []).append(exchange)
    rows = []
    for key, exchanges in grouped.items():
        catalog_id, variant, api_key_env, secret_env, password_env = key
        rows.append(
            RuntimeAccountGroup(
                exchange=catalog_id,
                api_variant=variant,
                runtime_keys=tuple(sorted({exchange.key for exchange in exchanges})),
                market_types=tuple(
                    sorted({exchange.market_type for exchange in exchanges})
                ),
                api_key_env=api_key_env,
                secret_env=secret_env,
                password_env=password_env,
            )
        )
    return sorted(rows, key=lambda row: (row.exchange, row.runtime_keys))


def _group_credentials(group: RuntimeAccountGroup) -> dict[str, str]:
    credentials = {
        "api_key": str(os.environ.get(group.api_key_env) or "").replace("\\n", "\n"),
        "secret": str(os.environ.get(group.secret_env) or "").replace("\\n", "\n"),
    }
    if group.password_env:
        credentials["passphrase"] = str(
            os.environ.get(group.password_env) or ""
        ).replace("\\n", "\n")
    return {key: value for key, value in credentials.items() if value}


def _credential_fingerprint(credentials: dict[str, str]) -> str:
    payload = json.dumps(
        credentials,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _matching_connection(
    store: UserWorkspaceStore,
    *,
    owner_email: str,
    group: RuntimeAccountGroup,
    credentials: dict[str, str],
) -> UserApiConnection | None:
    target = _credential_fingerprint(credentials)
    for connection in store.list_api_connections(
        owner_email=owner_email,
        is_admin=False,
    ):
        if (
            connection.exchange != group.exchange
            or connection.api_variant != group.api_variant
            or not store.credential_status(connection.id)["configured"]
        ):
            continue
        existing: dict[str, str] = {}
        try:
            existing = store.decrypt_credentials(
                account_id=connection.id,
                owner_email=owner_email,
            )
            if _credential_fingerprint(existing) == target:
                return connection
        except (RuntimeError, ValueError):
            continue
        finally:
            existing.clear()
    return None


def _sole_compatible_connection(
    store: UserWorkspaceStore,
    *,
    owner_email: str,
    group: RuntimeAccountGroup,
) -> UserApiConnection | None:
    candidates = [
        connection
        for connection in store.list_api_connections(
            owner_email=owner_email,
            is_admin=False,
        )
        if connection.exchange == group.exchange
        and connection.api_variant == group.api_variant
        and store.credential_status(connection.id)["configured"]
    ]
    return candidates[0] if len(candidates) == 1 else None


def _deterministic_connection_id(
    owner_email: str,
    group: RuntimeAccountGroup,
) -> str:
    identity = "|".join(
        (
            owner_email.lower(),
            group.exchange,
            group.api_variant,
            group.api_key_env,
            group.secret_env,
            group.password_env,
        )
    )
    return "connection-runtime-" + hashlib.sha256(identity.encode()).hexdigest()[:12]


def import_runtime_accounts(
    store: UserWorkspaceStore,
    cfg: BotConfig,
    *,
    owner_email: str,
    exchange_ids: Iterable[str] | None = None,
    withdrawal_disabled_confirmed: bool,
) -> dict[str, Any]:
    owner = str(owner_email or "").strip().lower()
    if not owner:
        raise ValueError("owner email is required")
    if not withdrawal_disabled_confirmed:
        raise ValueError(
            "confirm that withdrawal permission is disabled before importing APIs"
        )
    imported = []
    skipped = []
    runtime_links: dict[str, str] = {}
    for group in runtime_account_groups(cfg, exchange_ids=exchange_ids):
        credentials = _group_credentials(group)
        if not credentials.get("api_key") or not credentials.get("secret"):
            existing = _sole_compatible_connection(
                store,
                owner_email=owner,
                group=group,
            )
            if existing is not None:
                raw = existing.to_dict()
                raw["runtime_keys"] = sorted(
                    set(existing.runtime_keys) | set(group.runtime_keys)
                )
                saved = store.upsert_api_connection(
                    UserApiConnection.from_dict(raw)
                )
                for runtime_key in group.runtime_keys:
                    runtime_links[runtime_key] = saved.id
                imported.append(
                    {
                        "id": saved.id,
                        "label": saved.label,
                        "exchange": saved.exchange,
                        "market_types": list(saved.market_types),
                        "runtime_keys": list(saved.runtime_keys),
                        "matched_existing": True,
                        "linked_without_legacy_env": True,
                    }
                )
                credentials.clear()
                continue
            skipped.append(
                {
                    "exchange": group.exchange,
                    "runtime_keys": list(group.runtime_keys),
                    "reason": (
                        "required credential environment variables are missing and "
                        "the encrypted account match is not unique"
                    ),
                }
            )
            credentials.clear()
            continue
        existing = _matching_connection(
            store,
            owner_email=owner,
            group=group,
            credentials=credentials,
        )
        connection_id = (
            existing.id
            if existing is not None
            else _deterministic_connection_id(owner, group)
        )
        deterministic_existing = store.get_api_connection(connection_id)
        base = existing or deterministic_existing
        label = (
            base.label
            if base is not None
            else store.suggest_api_connection_label(
                owner_email=owner,
                exchange=group.exchange,
            )
        )
        raw = base.to_dict() if base is not None else {}
        raw.update(
            {
                "id": connection_id,
                "owner_email": owner,
                "label": label,
                "exchange": group.exchange,
                "market_type": (
                    "spot" if "spot" in group.market_types else group.market_types[0]
                ),
                "market_types": list(group.market_types),
                "api_variant": group.api_variant,
                "runtime_keys": sorted(
                    set(base.runtime_keys if base is not None else ())
                    | set(group.runtime_keys)
                ),
                "withdrawal_disabled_confirmed": True,
                "trade_permission_confirmed": True,
            }
        )
        saved = store.upsert_api_connection(
            UserApiConnection.from_dict(raw),
            credentials=credentials if existing is None else None,
        )
        for runtime_key in group.runtime_keys:
            runtime_links[runtime_key] = saved.id
        imported.append(
            {
                "id": saved.id,
                "label": saved.label,
                "exchange": saved.exchange,
                "market_types": list(saved.market_types),
                "runtime_keys": list(saved.runtime_keys),
                "matched_existing": existing is not None,
                "linked_without_legacy_env": False,
            }
        )
        credentials.clear()
    return {
        "owner_email": owner,
        "imported": imported,
        "skipped": skipped,
        "runtime_links": runtime_links,
    }


def write_runtime_credential_links(
    config_path: str | Path,
    *,
    runtime_links: dict[str, str],
    owner_email: str,
    credential_store_path: str,
    credential_master_key_env: str,
) -> int:
    path = Path(config_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    for section in ("spot_exchanges", "derivative_exchanges"):
        for exchange in raw.get(section, []):
            if not isinstance(exchange, dict):
                continue
            runtime_key = str(
                exchange.get("label")
                or f"{exchange.get('id')}:{exchange.get('market_type', 'spot')}"
            )
            connection_id = runtime_links.get(runtime_key)
            if not connection_id:
                continue
            values = {
                "credential_connection_id": connection_id,
                "credential_owner_email": owner_email,
                "credential_store_path": credential_store_path,
                "credential_master_key_env": credential_master_key_env,
            }
            if any(exchange.get(key) != value for key, value in values.items()):
                exchange.update(values)
                changed += 1
    if changed:
        backup = path.with_suffix(path.suffix + ".before-account-migration")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        path.write_text(
            json.dumps(raw, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return changed
