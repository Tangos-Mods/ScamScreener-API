import { describe, expect, it } from "vitest";

import { isTimestampWithinSkew } from "../src/security/nonces";
import {
  buildCanonicalString,
  hmacSha256Hex,
  verifySignature
} from "../src/security/signature";

describe("signature utilities", () => {
  it("builds the expected canonical string", () => {
    const canonical = buildCanonicalString({
      method: "POST",
      path: "/api/v1/training-uploads",
      clientId: "relay-client-a1b2",
      timestamp: "1700000000",
      nonce: "8eb62752-e147-4f2b-95a8-62de00f99701",
      fileSha256: "abc123",
      fileSizeBytes: 42,
      schemaVersion: "1"
    });

    expect(canonical).toBe(
      [
        "POST",
        "/api/v1/training-uploads",
        "relay-client-a1b2",
        "1700000000",
        "8eb62752-e147-4f2b-95a8-62de00f99701",
        "abc123",
        "42",
        "1"
      ].join("\n")
    );
  });

  it("creates and verifies HMAC signatures", () => {
    const secret = "relay-secret-test";
    const canonical = [
      "POST",
      "/api/v1/training-uploads",
      "relay-client-test",
      "1700001234",
      "327dcf14-f6e9-41ea-8948-8c13823e3173",
      "f98c51f9e91f0f64bcf8f4f6a04591057d58a1a5cbf8f6df53f94940f2dc0f87",
      "100",
      "1"
    ].join("\n");

    const signature = hmacSha256Hex(secret, canonical);

    expect(signature).toMatch(/^[0-9a-f]{64}$/);
    expect(verifySignature(secret, canonical, signature)).toBe(true);
    expect(verifySignature(secret, canonical, "0".repeat(64))).toBe(false);
  });

  it("checks timestamp skew windows", () => {
    expect(isTimestampWithinSkew(1000, 1200, 300)).toBe(true);
    expect(isTimestampWithinSkew(1000, 1301, 300)).toBe(false);
  });
});
