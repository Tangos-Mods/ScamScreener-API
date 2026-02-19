import Database from "better-sqlite3";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { initDatabase } from "../src/db";
import { hashInviteCode } from "../src/security/signature";

export interface TempDbContext {
  db: Database.Database;
  dbPath: string;
  cleanup: () => void;
}

export function createTempDatabase(prefix: string): TempDbContext {
  const dbPath = path.join(
    os.tmpdir(),
    `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}.db`
  );
  const db = initDatabase(dbPath);

  return {
    db,
    dbPath,
    cleanup: () => {
      db.close();
      for (const suffix of ["", "-wal", "-shm"]) {
        const filePath = `${dbPath}${suffix}`;
        if (fs.existsSync(filePath)) {
          fs.unlinkSync(filePath);
        }
      }
    }
  };
}

export function seedInviteCode(
  db: Database.Database,
  inviteCode: string,
  options: {
    maxUses?: number;
    usedCount?: number;
    expiresAt?: string | null;
  } = {}
): void {
  db.prepare(
    `
      INSERT INTO invite_codes (code_hash, max_uses, used_count, expires_at, created_at, created_by)
      VALUES (?, ?, ?, ?, ?, ?)
    `
  ).run(
    hashInviteCode(inviteCode),
    options.maxUses ?? 1,
    options.usedCount ?? 0,
    options.expiresAt ?? null,
    new Date().toISOString(),
    "test"
  );
}

export function seedClient(
  db: Database.Database,
  clientId: string,
  clientSecret: string,
  options: {
    installId?: string;
    active?: 0 | 1;
  } = {}
): void {
  db.prepare(
    `
      INSERT INTO clients (client_id, client_secret, install_id, active, created_at, revoked_at)
      VALUES (?, ?, ?, ?, ?, NULL)
    `
  ).run(
    clientId,
    clientSecret,
    options.installId ?? "00000000-0000-0000-0000-000000000001",
    options.active ?? 1,
    new Date().toISOString()
  );
}
