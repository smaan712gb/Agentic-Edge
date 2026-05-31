"""Load ``managers.toml`` and upsert it into the manager tables.

Config is the source of truth — editing the TOML and restarting is how you
add or retune a manager, no migration required. Called once from the FastAPI
lifespan. "lookup" CIKs are resolved best-effort via the EDGAR provider; a
manager whose CIK can't be resolved is left inactive rather than blocking
startup.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

from ..db import get_session as db_session
from .repo import HedgeFundRepo

logger = logging.getLogger("agentic_edge.hedge_funds")

_CONFIG_PATH = Path(__file__).with_name("managers.toml")


def _cik10(cik: str) -> str:
    digits = "".join(ch for ch in str(cik) if ch.isdigit())
    return digits.zfill(10) if digits else ""


async def load_managers_from_config(path: Path | None = None) -> int:
    """Upsert every [[manager]] block. Returns the count loaded.

    Resolves any ``"lookup"`` CIK via the EDGAR provider when available. A
    manager left with zero resolved CIKs is marked inactive (the poller skips
    inactive managers) so an unresolved name never errors the sweep.
    """
    path = path or _CONFIG_PATH
    if not path.exists():
        logger.warning("hedge_funds: %s not found — no managers loaded", path)
        return 0

    data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    blocks = data.get("manager") or []
    if not blocks:
        return 0

    edgar = _try_get_edgar()
    loaded = 0
    for block in blocks:
        slug = block.get("slug")
        name = block.get("name")
        if not slug or not name:
            logger.warning("hedge_funds: skipping manager block missing slug/name: %r", block)
            continue

        ciks: list[tuple[str, str | None]] = []
        for raw in block.get("ciks", []):
            if str(raw).lower() == "lookup":
                resolved = await _resolve_cik(edgar, name)
                if resolved:
                    ciks.append((resolved, name))
                    logger.info("hedge_funds: resolved CIK %s for %s", resolved, name)
                else:
                    logger.warning("hedge_funds: could not resolve CIK for %s — left inactive", name)
            else:
                padded = _cik10(raw)
                if padded:
                    ciks.append((padded, name))

        active = bool(ciks)  # no CIKs → inactive, poller skips it
        async with db_session() as s:
            await HedgeFundRepo(s).upsert_manager(
                slug=slug, name=name, ciks=ciks,
                macro_only=bool(block.get("macro_only", False)),
                active=active,
                primary_themes=block.get("themes") or [],
                weighting_profile=block.get("weighting_profile") or {},
            )
        loaded += 1
    logger.info("hedge_funds: loaded %d manager(s) from config", loaded)
    return loaded


def _try_get_edgar() -> Any | None:
    """The provider raises AuthError when EDGAR_USER_AGENT_EMAIL is unset; in
    that case "lookup" CIKs simply can't be resolved (manager stays inactive),
    which is fine — config loading must not hard-depend on EDGAR creds."""
    try:
        from tradingagents.dataflows.providers.registry import get_provider
        return get_provider("edgar")
    except Exception as e:
        logger.info("hedge_funds: EDGAR provider unavailable for CIK lookup (%s)", e)
        return None


async def _resolve_cik(edgar: Any | None, name: str) -> str | None:
    if edgar is None:
        return None
    try:
        # Use the distinctive entity token (e.g. "Atreides") for the search.
        query = name.split("—")[-1].strip() if "—" in name else name
        return await edgar.lookup_cik_by_name(query)
    except Exception as e:
        logger.warning("hedge_funds: CIK lookup failed for %s: %s", name, e)
        return None
