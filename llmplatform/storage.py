from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OFFLOAD_DIR = DATA_DIR / "offload"
DB_PATH = DATA_DIR / "platform.sqlite3"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_load(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OFFLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                secret_ref TEXT NOT NULL,
                model_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                model TEXT,
                file_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                path TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workflows (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                definition TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                published_version INTEGER NOT NULL DEFAULT 0,
                published_definition TEXT NOT NULL DEFAULT '{}',
                published_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL REFERENCES workflows(id),
                parent_run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
                parent_node_id TEXT,
                parent_invocation INTEGER,
                workflow_version INTEGER NOT NULL DEFAULT 1,
                workflow_snapshot TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                input TEXT NOT NULL,
                state TEXT NOT NULL,
                current_node TEXT,
                pending TEXT,
                error TEXT,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                node_id TEXT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated_at);
            CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, id);
            CREATE TABLE IF NOT EXISTS system_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sampled_at TEXT NOT NULL,
                cpu_percent REAL,
                memory_used_bytes INTEGER,
                memory_total_bytes INTEGER,
                process_rss_bytes INTEGER,
                gpu_name TEXT,
                gpu_util_percent REAL,
                gpu_temp_c REAL,
                vram_used_bytes INTEGER,
                vram_total_bytes INTEGER,
                model_process_rss_bytes INTEGER,
                model_process_cpu_percent REAL,
                prompt_tokens_per_second REAL,
                generation_tokens_per_second REAL,
                requests_processing REAL,
                requests_deferred REAL,
                runtime_status TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_system_metrics_sampled
                ON system_metrics(sampled_at);
            CREATE TABLE IF NOT EXISTS monitor_alerts (
                id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                level TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                resolved_at TEXT,
                acknowledged_at TEXT
            );
            CREATE TABLE IF NOT EXISTS platform_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_monitor_alerts_seen
                ON monitor_alerts(last_seen DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_monitor_alerts_open_fingerprint
                ON monitor_alerts(fingerprint) WHERE status IN ('active','acknowledged');
            """
        )

        # Additive migrations keep existing local databases usable as the
        # workflow runtime and monitor gain durable version/resource fields.
        migrations = {
            "workflows": {
                "version": "INTEGER NOT NULL DEFAULT 1",
                "published_version": "INTEGER NOT NULL DEFAULT 0",
                "published_definition": "TEXT NOT NULL DEFAULT '{}'",
                "published_at": "TEXT",
            },
            "messages": {
                "reasoning": "TEXT NOT NULL DEFAULT ''",
            },
            "runs": {
                "workflow_version": "INTEGER NOT NULL DEFAULT 1",
                "workflow_snapshot": "TEXT NOT NULL DEFAULT '{}'",
                "started_at": "TEXT",
                "finished_at": "TEXT",
                "parent_run_id": "TEXT REFERENCES runs(id) ON DELETE CASCADE",
                "parent_node_id": "TEXT",
                "parent_invocation": "INTEGER",
            },
        }
        for table, columns in migrations.items():
            present = {
                row["name"]
                for row in db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, declaration in columns.items():
                if column not in present:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        db.execute("CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id, created_at)")

        metric_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(system_metrics)").fetchall()
        }
        if "gpu_temp_c" not in metric_columns:
            db.execute("ALTER TABLE system_metrics ADD COLUMN gpu_temp_c REAL")
        metric_migrations = {
            "model_process_rss_bytes": "INTEGER",
            "model_process_cpu_percent": "REAL",
            "prompt_tokens_per_second": "REAL",
            "generation_tokens_per_second": "REAL",
            "requests_processing": "REAL",
            "requests_deferred": "REAL",
        }
        for column, declaration in metric_migrations.items():
            if column not in metric_columns:
                db.execute(f"ALTER TABLE system_metrics ADD COLUMN {column} {declaration}")


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
