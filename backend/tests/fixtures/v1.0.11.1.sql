-- DebridPulse v1.0.11.1 exact predecessor schema fixture
-- source commit: f06742847f60b5924e4584714055d0a311172158
BEGIN TRANSACTION;
CREATE TABLE debridpulse_aria2_owned_gids (
                gid TEXT PRIMARY KEY,
                download_file_id INTEGER,
                torrent_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
CREATE TABLE deferred_provider_submissions (
                torrent_id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                payload BLOB NOT NULL,
                filename TEXT,
                source TEXT DEFAULT 'manual',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (torrent_id) REFERENCES torrents(id)
            );
CREATE TABLE download_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torrent_id INTEGER,
                filename TEXT,
                size_bytes INTEGER,
                source_url TEXT,
                download_url TEXT,
                local_path TEXT,
                status TEXT DEFAULT 'pending',
                download_id TEXT,
                download_client TEXT DEFAULT 'aria2',
                blocked INTEGER DEFAULT 0,
                block_reason TEXT,
                retry_count INTEGER DEFAULT 0,
                mirror_group_id INTEGER,
                mirror_state TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (torrent_id) REFERENCES torrents(id)
            );
CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                torrent_id INTEGER,
                level TEXT DEFAULT 'info',
                message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (torrent_id) REFERENCES torrents(id)
            );
CREATE TABLE stats_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_json TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
CREATE TABLE torrents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hash TEXT UNIQUE NOT NULL,
                name TEXT,
                magnet TEXT,
                status TEXT DEFAULT 'pending',
                alldebrid_id TEXT,
                size_bytes INTEGER DEFAULT 0,
                progress REAL DEFAULT 0,
                download_url TEXT,
                local_path TEXT,
                source TEXT DEFAULT '',
                provider_status TEXT,
                provider_status_code INTEGER,
                polling_failures INTEGER DEFAULT 0,
                download_client TEXT DEFAULT 'aria2',
                label TEXT DEFAULT '',
                priority INTEGER DEFAULT 0,
                error_message TEXT,
                extraction_status TEXT DEFAULT '',
                extraction_error TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME
            , upload_retry_count INTEGER DEFAULT 0);
CREATE TABLE transfer_pause_intents (
                torrent_id INTEGER PRIMARY KEY,
                paused INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
CREATE INDEX idx_dlfiles_torrent_status ON download_files (torrent_id, status, blocked);
CREATE INDEX idx_dlfiles_queue ON download_files (status, download_client, blocked, torrent_id, id);
CREATE INDEX idx_dlfiles_download_id ON download_files (download_id);
CREATE INDEX idx_dlfiles_mirror_group ON download_files (torrent_id, mirror_group_id, mirror_state, status);
CREATE INDEX idx_torrents_alldebrid_id ON torrents (alldebrid_id);
CREATE INDEX idx_torrents_status ON torrents (status);
CREATE INDEX idx_torrents_status_alldebrid ON torrents (status, alldebrid_id);
CREATE INDEX idx_torrents_status_updated ON torrents (status, updated_at);
CREATE INDEX idx_torrents_status_priority ON torrents (status, priority DESC, id ASC);
CREATE INDEX idx_torrents_completed_at ON torrents (completed_at);
CREATE INDEX idx_torrents_priority ON torrents (priority DESC, id ASC);
CREATE INDEX idx_torrents_hash ON torrents (hash);
CREATE INDEX idx_torrents_created_at ON torrents (created_at);
CREATE INDEX idx_dlfiles_local_path ON download_files (local_path);
CREATE INDEX idx_events_torrent_id ON events (torrent_id);
CREATE INDEX idx_events_created_at ON events (created_at);
DELETE FROM "sqlite_sequence";
COMMIT;
