import { afterEach, describe, expect, it } from "vitest";

import { initDatabase } from "../src/db";
import {
  cleanupExpiredNonces,
  hasSeenNonce,
  persistNonce
} from "../src/security/nonces";

describe("nonce replay helpers", () => {
  const db = initDatabase(":memory:");

  afterEach(() => {
    db.prepare("DELETE FROM nonces").run();
  });

  it("stores and detects used nonces", () => {
    const clientId = "relay-client-1";
    const nonce = "d1f8011d-6a1a-4d87-8db2-f7ca2b3fbf88";

    expect(hasSeenNonce(db, clientId, nonce)).toBe(false);

    persistNonce(
      db,
      clientId,
      nonce,
      "2026-01-01T00:00:00.000Z",
      "2026-01-01T00:10:00.000Z"
    );

    expect(hasSeenNonce(db, clientId, nonce)).toBe(true);
  });

  it("removes expired nonce rows", () => {
    persistNonce(
      db,
      "relay-client-1",
      "2a5d51dd-df9d-46a8-93cf-71b0f56fda38",
      "2026-01-01T00:00:00.000Z",
      "2026-01-01T00:01:00.000Z"
    );
    persistNonce(
      db,
      "relay-client-1",
      "975f6b46-52b4-4b4f-a59d-d67fe1614e66",
      "2026-01-01T00:00:00.000Z",
      "3026-01-01T00:01:00.000Z"
    );

    const removed = cleanupExpiredNonces(db, "2026-01-01T00:30:00.000Z");
    expect(removed).toBe(1);
    expect(
      hasSeenNonce(db, "relay-client-1", "2a5d51dd-df9d-46a8-93cf-71b0f56fda38")
    ).toBe(false);
    expect(
      hasSeenNonce(db, "relay-client-1", "975f6b46-52b4-4b4f-a59d-d67fe1614e66")
    ).toBe(true);
  });
});
