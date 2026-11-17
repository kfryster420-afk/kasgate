/** USD decimal string (up to 8 dp from the gateway) -> "$1,234.50" keeping every significant digit. */
export function formatUsd(value: string | null | undefined, minFraction = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return value;
  const sign = match[1] ?? "";
  const whole = (match[2] ?? "0").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const frac = (match[3] ?? "").replace(/0+$/, "").padEnd(minFraction, "0");
  return `${sign}$${whole}${frac ? `.${frac}` : ""}`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

export function shortHash(value: string, head = 8, tail = 6): string {
  return value.length <= head + tail + 1 ? value : `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function shortAddress(value: string): string {
  const colon = value.indexOf(":");
  const prefix = colon >= 0 ? value.slice(0, colon + 1) : "";
  const body = colon >= 0 ? value.slice(colon + 1) : value;
  return `${prefix}${shortHash(body, 6, 6)}`;
}
