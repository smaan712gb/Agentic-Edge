import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function fmtMoney(n: number): string {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

export function fmtMoneyDelta(n: number): string {
  const sign = n >= 0 ? "+" : "−";
  return `${sign}${Math.abs(n).toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })}`;
}

export function fmtPct(n: number): string {
  const sign = n >= 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}%`;
}

export function fmtDateShort(iso: string): string {
  // ``new Date("2026-05-08")`` parses as UTC midnight — toLocaleDateString
  // then shifts to the prior calendar day in negative-UTC zones (ET, PT).
  // Parse the YYYY-MM-DD parts as a local date instead so the displayed
  // calendar day matches the API's ``as_of`` (which we already send in
  // the trading-relevant ET timezone).
  const datePart = iso.includes("T") ? iso.slice(0, 10) : iso;
  const [y, m, d] = datePart.split("-").map(Number);
  if (!y || !m || !d) {
    // Fall back to native parsing for non-date-only strings (timestamps).
    return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }
  const dt = new Date(y, m - 1, d);
  return dt.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
