CREATE TABLE IF NOT EXISTS clients (
  client_id TEXT PRIMARY KEY,
  client_secret TEXT NOT NULL,
  install_id TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS invite_codes (
  code_hash TEXT PRIMARY KEY,
  max_uses INTEGER NOT NULL DEFAULT 1,
  used_count INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  created_by TEXT
);

CREATE TABLE IF NOT EXISTS nonces (
  client_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY (client_id, nonce)
);

CREATE TABLE IF NOT EXISTS upload_audit (
  request_id TEXT PRIMARY KEY,
  client_id TEXT,
  status TEXT NOT NULL,
  error_code TEXT,
  ip TEXT,
  created_at TEXT NOT NULL
);
