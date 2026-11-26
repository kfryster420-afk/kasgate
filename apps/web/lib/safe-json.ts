/**
 * The gateway serialises BIGINT sompi (and micro-dollar) columns as JSON integers that may exceed
 * Number.MAX_SAFE_INTEGER. `JSON.parse` would silently round them. `parseGatewayJson` first rewrites every
 * integer literal whose object key ends in `_sompi` or `_usd_micros` into a JSON string (using a string-aware
 * scanner, so user text is never touched), then parses normally. Those fields therefore arrive as exact decimal strings.
 */

const NUMBER_TOKEN = /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
const INTEGER_TOKEN = /^-?\d+$/;

export function quoteSompiIntegers(text: string): string {
  let out = "";
  let i = 0;
  let lastKey = "";
  const n = text.length;

  while (i < n) {
    const ch = text.charAt(i);

    if (ch === '"') {
      let j = i + 1;
      while (j < n) {
        const c = text.charAt(j);
        if (c === "\\") {
          j += 2;
          continue;
        }
        if (c === '"') break;
        j += 1;
      }
      const end = Math.min(j + 1, n);
      let k = end;
      while (k < n && /\s/.test(text.charAt(k))) k += 1;
      if (text.charAt(k) === ":") lastKey = text.slice(i + 1, j);
      out += text.slice(i, end);
      i = end;
      continue;
    }

    if (ch === "-" || (ch >= "0" && ch <= "9")) {
      NUMBER_TOKEN.lastIndex = i;
      const match = NUMBER_TOKEN.exec(text);
      if (match) {
        const token = match[0];
        out += (lastKey.endsWith("_sompi") || lastKey.endsWith("_usd_micros")) && INTEGER_TOKEN.test(token) ? `"${token}"` : token;
        i += token.length;
        continue;
      }
    }

    if (ch === "," || ch === "{" || ch === "}" || ch === "[" || ch === "]") lastKey = "";
    out += ch;
    i += 1;
  }

  return out;
}

export function parseGatewayJson<T>(text: string): T {
  return JSON.parse(quoteSompiIntegers(text)) as T;
}

/**
 * JSON.stringify variant that emits selected string-valued fields as bare integer literals, so BigInt-sized
 * sompi values reach the gateway as exact JSON integers. `intFields` values must be digit-only strings.
 */
export function stringifyWithIntegers(value: Record<string, unknown>, intFields: readonly string[]): string {
  const marker = `__kg_int_${crypto.randomUUID()}__`;
  const copy: Record<string, unknown> = { ...value };
  for (const field of intFields) {
    const v = copy[field];
    if (typeof v === "string") {
      if (!/^-?\d+$/.test(v)) throw new Error(`${field} must be an integer string`);
      copy[field] = `${marker}${v}`;
    }
  }
  const pattern = new RegExp(`"${marker}(-?\\d+)"`, "g");
  return JSON.stringify(copy).replace(pattern, "$1");
}

/**
 * Like `stringifyWithIntegers`, but for nested data: every string value under a key in `intKeys`, at any depth,
 * is emitted as a bare JSON integer (e.g. `selections[].price_sompi`). Values must be digit-only strings.
 */
export function stringifyWithIntegerKeys(value: unknown, intKeys: readonly string[]): string {
  const marker = `__kg_int_${crypto.randomUUID()}__`;
  const mark = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(mark);
    if (node && typeof node === "object") {
      const out: Record<string, unknown> = {};
      for (const [key, child] of Object.entries(node as Record<string, unknown>)) {
        if (intKeys.includes(key) && typeof child === "string") {
          if (!/^-?\d+$/.test(child)) throw new Error(`${key} must be an integer string`);
          out[key] = `${marker}${child}`;
        } else {
          out[key] = mark(child);
        }
      }
      return out;
    }
    return node;
  };
  const pattern = new RegExp(`"${marker}(-?\\d+)"`, "g");
  return JSON.stringify(mark(value)).replace(pattern, "$1");
}
