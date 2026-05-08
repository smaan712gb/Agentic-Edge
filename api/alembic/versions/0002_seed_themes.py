"""seed the 15 default themes from the original main.py SEED_THEMES list

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-07

The seed data exactly matches what the FastAPI mock had wired in
``api/app/main.py:SEED_THEMES`` so existing run history and frontend
copy still line up. Inserts use ``INSERT … WHERE NOT EXISTS`` so a
re-apply on a non-empty DB is a no-op rather than a failure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEED_THEMES = [
    ("ai-memory-wall", "AI memory wall",
     "HBM, NAND, and enterprise SSD shortage as AI accelerators outrun memory bandwidth and capacity.",
     "Every AI chip is bandwidth-limited. HBM3E and HBM4 supply is sold out through 2026 and "
     "enterprise SSD pricing has flipped from glut to allocation. The handful of memory makers "
     "that own the capacity capture pricing power as accelerator buyers compete for slots.",
     ["MU", "SNDK", "WDC", "STX", "SIMO", "RMBS"]),
    ("optical-networking", "Optical networking",
     "800G and 1.6T transceivers as the AI bandwidth bottleneck moves from chip to fabric.",
     "Electrical interconnect has plateaued; pluggable optics are the only practical way to move "
     "AI training traffic between racks. Bandwidth doubles per generation, and the short list of "
     "vendors qualified into hyperscaler reference designs captures the entire upgrade cycle.",
     ["LITE", "COHR", "FN", "CRDO", "MRVL", "CIEN", "AAOI"]),
    ("data-center-power-wall", "Data center power wall",
     "Electrical infrastructure as the gating constraint on every new AI data center.",
     "Compute is no longer the bottleneck — getting megawatts to the rack is. Switchgear, "
     "transformers, busways, and on-site generation are the practical limit on buildouts, and "
     "lead times are stretched into 2027. Equipment vendors enjoy structural order books.",
     ["VRT", "ETN", "PWR", "CAT", "CEG", "BE", "NVTS"]),
    ("nuclear-for-ai", "Nuclear power for AI",
     "Hyperscalers contracting nuclear capacity for 24/7 carbon-free gigawatts.",
     "AI loads need carbon-free, 24/7, gigawatt-scale power that grids cannot deliver in the next "
     "decade. Existing nuclear is signing decade-long PPAs at premium prices, and SMR vendors "
     "hold the only credible new-build option. Fuel and conversion sit upstream of all of it.",
     ["CEG", "BWXT", "LEU", "CCJ", "OKLO", "SMR"]),
    ("advanced-packaging", "Advanced packaging",
     "CoWoS, hybrid bonding, and HBM packaging as the physical bottleneck for every leading AI chip.",
     "TSMC's CoWoS-S/L capacity is the choke point of the entire AI industry; allocation is "
     "rationed quarter by quarter. Hybrid-bonding and HBM stacking equipment vendors sell the "
     "tools that unlock more capacity, so they monetize the bottleneck without competing for it.",
     ["TSM", "AMAT", "KLAC", "KLIC", "CAMT", "BESIY", "ASML", "LRCX"]),
    ("custom-silicon-supply", "Custom silicon supply chain",
     "Hyperscalers escaping merchant-GPU margins by designing their own AI ASICs.",
     "Every hyperscaler is now designing its own AI silicon. Whether the buyer is AWS, Google, "
     "or Meta, the merchant ASIC supply chain — design services, IP, advanced packaging — "
     "captures the same TAM. The economics work no matter which architecture wins.",
     ["AVGO", "MRVL", "ALAB", "TSM"]),
    ("ai-interconnect", "AI interconnect",
     "CXL, PCIe Gen6, and high-radix switching as the next bottleneck after memory.",
     "As GPU clusters scale past 100k accelerators, the network becomes the limit on training "
     "throughput. Dollars rotate out of server CPU and into the fabric — switch silicon, retimers, "
     "smart NICs, and CXL controllers — concentrating revenue in a small qualified vendor list.",
     ["ALAB", "MRVL", "AVGO", "CRDO", "ANET", "CSCO"]),
    ("liquid-cooling", "Liquid cooling",
     "Direct-to-chip and immersion cooling as racks blow past the air-cooling ceiling.",
     "Air cooling tops out around 50 kW per rack; current AI racks already exceed 130 kW. Liquid "
     "cooling is no longer optional, and the few vendors with reference architectures inside "
     "Nvidia's GB200/300 designs lock in multi-year, attached upgrade revenue.",
     ["VRT", "MOD", "DELL", "HPE", "ETN", "PH"]),
    ("grid-bottleneck", "Grid bottleneck",
     "Transformers, substations, and electrification capex as the backbone of AI growth.",
     "US interconnect queues stretch four to seven years, and transformer lead times are past "
     "two years. Anyone making the physical equipment that ships electrons — switchgear, "
     "transformers, transmission EPC — has structural pricing power for at least the cycle.",
     ["ETN", "PWR", "HUBB", "POWL", "GEV", "ABBNY"]),
    ("critical-materials", "Critical materials",
     "Specialty substrates, rare earths, and high-purity copper as the upstream squeeze.",
     "InP, GaAs, GaN substrates, rare earths, and high-purity copper are the upstream choke "
     "point of the AI buildout. Permitting bottlenecks and concentrated supply protect margins "
     "for the few qualified producers — the inputs cannot be substituted at scale.",
     ["AXTI", "MP", "FCX", "SCCO", "TECK", "GLW"]),
    ("ai-storage", "AI storage explosion",
     "Enterprise SSD and HDD demand pulled forward by training datasets and inference logs.",
     "AI training datasets and inference logging are pulling enterprise storage demand forward "
     "by years. Unlike the consumer cycle, this demand is contracted and AI-driven; suppliers "
     "rerate from cyclical to capacity-led growth and pricing leverage shifts back to the seller.",
     ["SNDK", "WDC", "STX", "MU", "SIMO", "NTAP"]),
    ("ai-test-metrology", "AI testing and metrology",
     "Burn-in, metrology, and yield control as advanced-node chips become harder to test.",
     "Yield is the choke point of advanced nodes. Every CoWoS reticle and HBM stack must be "
     "tested at multiple stages, and metrology and burn-in equipment scale linearly with bit "
     "and wafer volume — a quiet but mandatory tax on every leading-edge AI chip shipped.",
     ["AEHR", "CAMT", "KLAC", "ONTO", "TER", "FORM"]),
    ("private-ai-compute", "Private AI compute",
     "GPU clouds and AI-chip startups as the IPO pipeline expands beyond the incumbents.",
     "Public-market AI compute is dominated by Nvidia and the hyperscalers, but a parallel "
     "ecosystem of GPU clouds, custom-silicon startups, and inference specialists is approaching "
     "public markets. Selective entries here capture the second wave without paying the leader's multiple.",
     ["CRWV"]),
    ("ai-dc-construction", "AI data center construction",
     "Specialty contractors and equipment lessors with hyperscale relationships.",
     "Beyond chips and power, someone has to pour the concrete and run the steel. Specialty "
     "mechanical/electrical contractors and equipment lessors with hyperscale relationships "
     "hold the only practical buildout pipeline; backlog is the new earnings visibility.",
     ["VRT", "ETN", "PWR", "CAT", "FIX", "STRL", "GEV"]),
    ("on-device-ai", "On-device AI",
     "Edge inference sidesteps the data-center bottleneck for the long tail of AI compute.",
     "Inference at the edge sidesteps every data-center bottleneck — power, cooling, network. "
     "Mobile, automotive, and industrial silicon vendors capture the long tail of AI compute "
     "that doesn't need a hyperscaler, and the install base is already deployed.",
     ["ARM", "QCOM", "STM", "NXPI", "INTC", "AMD"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)

    themes_table = sa.table(
        "themes",
        sa.column("id", sa.String),
        sa.column("user_id", sa.String),
        sa.column("name", sa.String),
        sa.column("thesis", sa.Text),
        sa.column("chokepoint", sa.Text),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    symbols_table = sa.table(
        "theme_symbols",
        sa.column("theme_id", sa.String),
        sa.column("symbol", sa.String),
        sa.column("position", sa.Integer),
        sa.column("weight", sa.Float),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    # Skip rows that already exist — makes this migration idempotent so a
    # second `alembic upgrade head` on a populated DB does nothing harmful.
    existing = {r[0] for r in bind.execute(sa.select(sa.column("id")).select_from(sa.text("themes")))}

    theme_rows = [
        {
            "id": tid, "user_id": None, "name": name, "thesis": thesis,
            "chokepoint": cp, "created_at": now, "updated_at": now,
        }
        for tid, name, thesis, cp, _syms in SEED_THEMES if tid not in existing
    ]
    if theme_rows:
        op.bulk_insert(themes_table, theme_rows)

    symbol_rows = []
    for tid, _name, _thesis, _cp, syms in SEED_THEMES:
        if tid in existing:
            continue
        for i, sym in enumerate(syms):
            symbol_rows.append({
                "theme_id": tid, "symbol": sym, "position": i,
                "weight": 1.0, "created_at": now,
            })
    if symbol_rows:
        op.bulk_insert(symbols_table, symbol_rows)


def downgrade() -> None:
    bind = op.get_bind()
    seed_ids = tuple(t[0] for t in SEED_THEMES)
    bind.execute(sa.text("DELETE FROM theme_symbols WHERE theme_id IN :ids").bindparams(
        sa.bindparam("ids", value=seed_ids, expanding=True)
    ))
    bind.execute(sa.text("DELETE FROM themes WHERE id IN :ids").bindparams(
        sa.bindparam("ids", value=seed_ids, expanding=True)
    ))
