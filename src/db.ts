import Database from "better-sqlite3";
import fs from "node:fs";
import path from "node:path";

export interface ClientRecord {
  client_id: string;
  client_secret: string;
  install_id: string | null;
  active: number;
  created_at: string;
  revoked_at: string | null;
}

export interface InviteCodeRecord {
  code_hash: string;
  max_uses: number;
  used_count: number;
  expires_at: string | null;
  created_at: string;
  created_by: string | null;
}

export function runMigrations(
  db: Database.Database,
  migrationPath: string = path.resolve(process.cwd(), "migrations", "001_init.sql")
): void {
  const migrationSql = fs.readFileSync(migrationPath, "utf8");
  db.exec(migrationSql);
}

export function initDatabase(sqlitePath: string): Database.Database {
  let resolvedPath = sqlitePath;
  if (sqlitePath !== ":memory:") {
    resolvedPath = path.resolve(sqlitePath);
    fs.mkdirSync(path.dirname(resolvedPath), { recursive: true });
  }

  const db = new Database(resolvedPath);
  db.pragma("journal_mode = WAL");
  runMigrations(db);
  return db;
}
