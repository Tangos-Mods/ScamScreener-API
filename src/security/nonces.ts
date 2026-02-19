import type Database from "better-sqlite3";

export function isTimestampWithinSkew(
  timestampSeconds: number,
  nowSeconds: number,
  maxClockSkewSeconds: number
): boolean {
  return Math.abs(nowSeconds - timestampSeconds) <= maxClockSkewSeconds;
}

export function hasSeenNonce(
  db: Database.Database,
  clientId: string,
  nonce: string
): boolean {
  const row = db
    .prepare(
      "SELECT 1 FROM nonces WHERE client_id = ? AND nonce = ? LIMIT 1"
    )
    .get(clientId, nonce);

  return Boolean(row);
}

export function persistNonce(
  db: Database.Database,
  clientId: string,
  nonce: string,
  seenAtIso: string,
  expiresAtIso: string
): void {
  db.prepare(
    `
      INSERT INTO nonces (client_id, nonce, seen_at, expires_at)
      VALUES (?, ?, ?, ?)
    `
  ).run(clientId, nonce, seenAtIso, expiresAtIso);
}

export function cleanupExpiredNonces(
  db: Database.Database,
  nowIso: string = new Date().toISOString()
): number {
  const result = db
    .prepare("DELETE FROM nonces WHERE expires_at <= ?")
    .run(nowIso);

  return result.changes;
}
