CREATE TABLE IF NOT EXISTS files (
  id TEXT PRIMARY KEY,
  owner_email TEXT NOT NULL,
  office_name TEXT,
  original_name TEXT NOT NULL,
  storage_key TEXT UNIQUE NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  download_count INTEGER NOT NULL DEFAULT 0,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS files_owner_idx ON files(owner_email, created_at);
CREATE INDEX IF NOT EXISTS files_expiry_idx ON files(expires_at, deleted_at);
CREATE TABLE IF NOT EXISTS download_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id TEXT NOT NULL,
  owner_email TEXT NOT NULL,
  downloaded_at TEXT NOT NULL,
  user_agent TEXT
);
