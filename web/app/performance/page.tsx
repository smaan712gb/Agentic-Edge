"use client";

import { useEffect, useState } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ArrowDownRight, ArrowUpRight, RefreshCw, TrendingUp, Wallet } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { Stat } from "@/components/Stat";
import { api, type EquityPoint, type Position, type TodayPerf } from "@/lib/api";
import { fmtDateShort, fmtMoney, fmtMoneyDelta, fmtPct } from "@/lib/utils";

const REFRESH_FAST_MS = 10_000;   // positions + today's P&L (live)
const REFRESH_SLOW_MS = 60_000;   // 90-day curve (changes daily)

export default function PerformancePage() {
  const [curve, setCurve] = useState<EquityPoint[] | null>(null);
  const [today, setToday] = useState<TodayPerf | null>(null);
  const [positions, setPositions] = useState<Position[] | null>(null);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);

  useEffect(() => {
    let alive = true;

    const tickFast = async () => {
      try {
        const [t, p] = await Promise.all([
          api.performance.today(),
          api.positions(),
        ]);
        if (!alive) return;
        setToday(t);
        setPositions(p);
        setLastUpdate(new Date());
      } catch (e) { console.error(e); }
    };
    const tickSlow = async () => {
      try {
        const r = await api.performance.curve();
        if (!alive) return;
        setCurve(r.points);
      } catch (e) { console.error(e); }
    };
    tickFast(); tickSlow();
    const fast = setInterval(tickFast, REFRESH_FAST_MS);
    const slow = setInterval(tickSlow, REFRESH_SLOW_MS);
    return () => { alive = false; clearInterval(fast); clearInterval(slow); };
  }, []);

  const startEq = curve?.[0]?.equity ?? 0;
  const endEq = curve?.[curve.length - 1]?.equity ?? 0;
  const totalGain = endEq - startEq;
  const totalGainPct = startEq ? (endEq / startEq - 1) * 100 : 0;
  const isUp = (today?.gain ?? 0) >= 0;

  return (
    <div>
      <PageHeader
        eyebrow="Paper account"
        title="Performance"
        subtitle="How the picks recommended by the agent team have performed in the paper account."
        actions={
          <span className="chip text-[10px] text-[var(--color-fg-muted)]">
            <RefreshCw className="h-3 w-3 animate-spin" style={{ animationDuration: "10s" }} />
            Live · {lastUpdate ? lastUpdate.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "…"}
          </span>
        }
      />

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
        <Stat
          label="Today"
          value={today ? fmtMoneyDelta(today.gain) : "—"}
          sub={today ? `${fmtPct(today.gain_pct)} · ${fmtDateShort(today.as_of)}` : undefined}
          tone={isUp ? "up" : "down"}
          icon={isUp ? <ArrowUpRight className="h-4 w-4" /> : <ArrowDownRight className="h-4 w-4" />}
        />
        <Stat
          label="Account value"
          value={today ? fmtMoney(today.equity) : "—"}
          sub={today?.cash != null ? `cash ${fmtMoney(today.cash)}` : "paper equity"}
          icon={<Wallet className="h-4 w-4" />}
        />
        <Stat
          label="Unrealized"
          value={today?.unrealized_pnl != null ? fmtMoneyDelta(today.unrealized_pnl) : "—"}
          sub={today?.realized_pnl != null ? `realized ${fmtMoneyDelta(today.realized_pnl)}` : undefined}
          tone={(today?.unrealized_pnl ?? 0) >= 0 ? "up" : "down"}
        />
        <Stat
          label="Buying power"
          value={today?.buying_power != null ? fmtMoney(today.buying_power) : "—"}
          sub={today?.available_funds != null ? `available ${fmtMoney(today.available_funds)}` : undefined}
        />
      </div>

      <div className="glass p-6 mb-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <div className="label-eyebrow">Daily equity</div>
            <div className="text-lg font-semibold mt-1">90-day account value</div>
          </div>
        </div>
        <div className="h-72">
          {curve && (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={curve} margin={{ top: 10, right: 12, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#7c5cff" stopOpacity={0.45} />
                    <stop offset="100%" stopColor="#7c5cff" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="#232a3a" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="date"
                  tickFormatter={(v) => fmtDateShort(v)}
                  stroke="#6c7388"
                  tick={{ fontSize: 12 }}
                  minTickGap={32}
                />
                <YAxis
                  stroke="#6c7388"
                  tick={{ fontSize: 12 }}
                  tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
                  domain={["auto", "auto"]}
                  width={56}
                />
                <Tooltip
                  contentStyle={{
                    background: "#161a23",
                    border: "1px solid #232a3a",
                    borderRadius: 10,
                    fontSize: 12,
                  }}
                  labelFormatter={(v) => fmtDateShort(String(v))}
                  formatter={(v: number) => [fmtMoney(v), "Equity"]}
                />
                <Area
                  type="monotone"
                  dataKey="equity"
                  stroke="#7c5cff"
                  strokeWidth={2}
                  fill="url(#eqfill)"
                />
              </AreaChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>

      <div className="glass p-6">
        <div className="label-eyebrow mb-3">Open positions</div>
        {positions === null ? (
          <div className="text-sm text-[var(--color-fg-dim)]">Loading…</div>
        ) : positions.length === 0 ? (
          <div className="text-sm text-[var(--color-fg-dim)]">No open positions.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-[var(--color-fg-dim)]">
                <tr className="text-left">
                  <th className="font-medium pb-3">Symbol</th>
                  <th className="font-medium pb-3">Quantity</th>
                  <th className="font-medium pb-3">Avg cost</th>
                  <th className="font-medium pb-3">Last</th>
                  <th className="font-medium pb-3 text-right">Unrealized</th>
                </tr>
              </thead>
              <tbody className="text-[var(--color-fg)]">
                {positions.map((p) => (
                  <tr key={p.symbol} className="border-t border-[var(--color-border)]">
                    <td className="py-3 font-medium">{p.symbol}</td>
                    <td className="py-3">{p.qty}</td>
                    <td className="py-3">{fmtMoney(p.avg_price)}</td>
                    <td className="py-3">{fmtMoney(p.last_price)}</td>
                    <td className={`py-3 text-right font-medium ${p.pnl >= 0 ? "text-[var(--color-up)]" : "text-[var(--color-down)]"}`}>
                      {fmtMoneyDelta(p.pnl)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
