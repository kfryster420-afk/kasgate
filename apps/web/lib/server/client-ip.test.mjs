// Pure-helper tests for the X-Forwarded-For derivation (run: npm run test:unit; Node >= 22.18 strips the TS types).
import assert from "node:assert/strict";
import { test } from "node:test";
import { clientIpFromForwardedFor, normaliseIp, parseTrustedProxyHops } from "./client-ip.ts";

test("default (no trusted proxy) never trusts an inbound X-Forwarded-For", () => {
  assert.equal(parseTrustedProxyHops(undefined), 0);
  assert.equal(clientIpFromForwardedFor("6.6.6.6", 0), undefined);
  assert.equal(clientIpFromForwardedFor("6.6.6.6, 203.0.113.7", 0), undefined);
});

test("one trusted proxy: the right-most entry; client-supplied entries to its left are ignored", () => {
  assert.equal(clientIpFromForwardedFor("203.0.113.7", 1), "203.0.113.7");
  assert.equal(clientIpFromForwardedFor("6.6.6.6, 203.0.113.7", 1), "203.0.113.7");
  assert.equal(clientIpFromForwardedFor("6.6.6.6,203.0.113.7", 2), "6.6.6.6");
});

test("too few entries, junk or a smuggled list forward nothing", () => {
  assert.equal(clientIpFromForwardedFor("203.0.113.7", 2), undefined);
  assert.equal(clientIpFromForwardedFor("6.6.6.6, not-an-ip", 1), undefined);
  assert.equal(clientIpFromForwardedFor("", 1), undefined);
  assert.equal(clientIpFromForwardedFor(null, 1), undefined);
  assert.equal(clientIpFromForwardedFor("6.6.6.6, ", 1), undefined);
});

test("IPv6 and IPv4-mapped addresses are normalised", () => {
  assert.equal(clientIpFromForwardedFor("2001:DB8::1", 1), "2001:db8::1");
  assert.equal(normaliseIp("::ffff:198.51.100.4"), "198.51.100.4");
  assert.equal(normaliseIp("1.2.3.4:5678"), null);
});

test("TRUSTED_PROXY_HOPS parsing falls back to 0 on anything invalid", () => {
  assert.equal(parseTrustedProxyHops("1"), 1);
  assert.equal(parseTrustedProxyHops(" 2 "), 2);
  for (const bad of ["-1", "1.5", "abc", "6", "10", "true"]) assert.equal(parseTrustedProxyHops(bad), 0, bad);
});
