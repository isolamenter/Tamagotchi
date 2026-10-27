"""Read a schema-1 or schema-2 snapshot and backfill a separate current-schema file.

Never overwrites a destination, switches the service, or copies credential caches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from dafeiyu.repositories.sqlite import SCHEMA_VERSION, Database

STATE_DEFAULTS = {
    "last_update_ts": 0.0,
    "last_social_ts": 0.0,
    "last_reply_ts": 0.0,
    "last_proactive_ts": 0.0,
    "last_dream_date": "",
    "last_diary_date": "",
    "last_proactive_topic": "",
    "last_proactive_answered": 1,
}


def _digest(records: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(
            records, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            default=lambda value: {"blob_hex": value.hex()} if isinstance(value, bytes) else value,
        ).encode()
    ).hexdigest()


def _insert(conn: sqlite3.Connection, table: str, records: list[dict]) -> None:
    if not records:
        return
    columns = list(records[0])
    conn.executemany(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        [tuple(record[c] for c in columns) for record in records],
    )


def _write_destination(
    source: Path,
    destination: Path,
    groups: list[tuple[str, list[dict], str]],
    *,
    source_version: int,
    extra_report: dict,
    sequences: dict[str, int],
) -> dict:
    # Initialize with a sentinel version, not Database.init_db(): even a crash
    # between schema creation and backfill must not leave a runtime-ready empty DB.
    schema = Path(__file__).resolve().parents[1].joinpath("repositories/schema.sql").read_text()
    version_line = f"PRAGMA user_version = {SCHEMA_VERSION};"
    if schema.count(version_line) != 1:
        raise RuntimeError("Cannot prepare migration schema version")
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    database = Database(destination)
    with database.connect() as new:
        # Persist the sentinel before the schema transaction too: an interrupted
        # schema script must not be mistaken for a pristine runtime database.
        new.execute("PRAGMA user_version = 300")
        new.executescript(schema.replace(version_line, "PRAGMA user_version = 300;"))
    report = {
        "schema_version": SCHEMA_VERSION,
        "source_schema_version": source_version,
        "source": str(source),
        "destination": str(destination),
        "tables": {},
    }
    with database.connect() as new:
        new.execute("BEGIN IMMEDIATE")
        for table, records, order in groups:
            _insert(new, table, records)
            columns = list(records[0]) if records else order.split(",")
            actual = [
                dict(r)
                for r in new.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY {order}")
            ]
            if len(actual) != len(records) or _digest(actual) != _digest(records):
                raise RuntimeError(f"Backfill verification failed: {table}")
            report["tables"][table] = {
                "source_count": len(records),
                "target_count": len(actual),
                "sha256": _digest(actual),
            }
        for table, sequence in sequences.items():
            current = new.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
            next_sequence = max(sequence, current[0] if current else 0)
            new.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
            new.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, next_sequence))
        if new.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Foreign-key verification failed")
        if new.execute("""
            SELECT id FROM conversations c WHERE summary_until_id != 0 AND NOT EXISTS
            (SELECT 1 FROM messages m WHERE m.id=c.summary_until_id AND m.conversation_id=c.id)
        """).fetchall():
            raise RuntimeError("Compression marker refers to a missing or different conversation message")
        if new.execute("""
            SELECT id FROM memories c WHERE source_until_id != 0 AND NOT EXISTS
            (SELECT 1 FROM messages m WHERE m.id=c.source_until_id AND m.conversation_id=c.conversation_id)
        """).fetchall():
            raise RuntimeError("Memory source marker refers to a missing or different conversation message")
        if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Target integrity check failed")
        new.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    report.update({
        "integrity_check": "ok",
        "foreign_key_check": "ok",
        "credential_caches": "not imported",
        "autoincrement_high_water_marks": sequences,
        "identity_policy": "legacy names retained for audit only; no aliases inferred; memory subjects unresolved",
        "rollback": "keep the source snapshot; do not point older runtime at the new file",
        **extra_report,
    })
    return report


# Explicit source columns: additive target defaults must not manufacture source
# evidence or reinterpret global legacy names as scoped identities.
SCHEMA2_COLUMNS = {
    "conversations": "id,chat_id,title,created_at,summary_until_id,compress_fail_count,last_compress_attempt,last_update_ts,last_social_ts,last_reply_ts,last_proactive_ts,last_dream_date,last_diary_date,last_proactive_topic,last_proactive_answered",
    "messages": "id,conversation_id,role,content,ts,sender_name,sender_open_id,is_observer,source,reply_mode,meme_tag",
    "memories": "id,conversation_id,kind,when_text,who,what,vibe,hooks,created_at,source_until_id",
    "user_names": "open_id,name,updated_at",
    "event_dedup": "event_id,created_at",
    "published_cards": "card_id,conversation_id,message_id,content,image_key,created_at",
    "memory_embeddings": "memory_id,provider,model,dimension,content,vec,updated_at",
    "style_embeddings": "example_id,embedding_type,provider,model,content_hash,dimension,vec,updated_at",
}



def _sequences(old: sqlite3.Connection, table_map: dict[str, str]) -> dict[str, int]:
    if not old.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
        return {}
    result = {}
    for name, sequence in old.execute("SELECT name,seq FROM sqlite_sequence"):
        if name in table_map:
            if type(sequence) is not int or sequence < 0:
                raise ValueError("Source has an invalid autoincrement sequence")
            result[table_map[name]] = sequence
    return result


def _migrate_schema2(old: sqlite3.Connection, source: Path, destination: Path) -> dict:
    tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not (set(SCHEMA2_COLUMNS) | {"sys_cache"}) <= tables:
        raise ValueError("Source is not the expected schema-2 database")
    if old.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("Source integrity check failed")
    if old.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("Source foreign-key check failed")
    groups = []
    for table, columns in SCHEMA2_COLUMNS.items():
        present = {r[1] for r in old.execute(f"PRAGMA table_info({table})")}
        if not set(columns.split(",")) <= present:
            raise ValueError(f"Source has an unsupported table definition: {table}")
        order = "example_id,provider,model" if table == "style_embeddings" else columns.split(",")[0]
        rows = [dict(r) for r in old.execute(f"SELECT {columns} FROM {table} ORDER BY {order}")]
        groups.append((table, rows, order))
    memories = next(rows for table, rows, _ in groups if table == "memories")
    embeddings = next(rows for table, rows, _ in groups if table == "memory_embeddings")
    return _write_destination(
        source, destination, groups, source_version=2,
        sequences=_sequences(old, {name: name for name in ("conversations", "messages", "memories")}),
        extra_report={
            "excluded_non_content_or_unpublished_cards": 0,
            "excluded_state_fields": [],
            "memory_vectors_to_rebuild": len(memories) - len(embeddings),
            "style_vectors": "preserved; active provider/model/content hash matching still required",
            "memory_vectors": "preserved; active provider/model/dimension matching still required",
        },
    )


def migrate(source: Path, destination: Path) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or not source.is_file():
        raise ValueError("Source must be an existing file distinct from the destination")
    if destination.exists():
        raise FileExistsError("Destination exists; choose a new file")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old:
        old.row_factory = sqlite3.Row
        old.execute("BEGIN")
        source_version = old.execute("PRAGMA user_version").fetchone()[0]
        if source_version == 2:
            return _migrate_schema2(old, source, destination)
        if source_version not in {0, 1}:
            raise ValueError("Source must be a schema-1 or schema-2 database")
        tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {
            "pets",
            "messages",
            "memory_cards",
            "user_names",
            "event_dedup",
            "card_instances",
        }
        if not required <= tables:
            raise ValueError("Source is not the expected 1.x database schema")
        required_columns = {
            "pets": {
                "id",
                "chat_id",
                "born_at",
                "summary_until_id",
                "state_json",
                "compress_fail_count",
                "last_compress_attempt",
            },
            "messages": {
                "id",
                "pet_id",
                "role",
                "content",
                "ts",
                "sender_name",
                "sender_open_id",
                "is_observer",
            },
            "memory_cards": {
                "id",
                "pet_id",
                "kind",
                "when_text",
                "who",
                "what",
                "vibe",
                "hooks",
                "created_at",
                "source_until_id",
            },
            "user_names": {"open_id", "name", "updated_at"},
            "event_dedup": {"event_id", "created_at"},
            "card_instances": {
                "card_id",
                "pet_id",
                "message_id",
                "mode",
                "announced",
                "base_text",
                "img_key",
                "built_at",
            },
        }
        for table, columns in required_columns.items():
            present = {r[1] for r in old.execute(f"PRAGMA table_info({table})")}
            if not columns <= present:
                raise ValueError(f"Source has an unsupported table definition: {table}")
        if old.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Source integrity check failed")
        conversations = []
        removed_state_keys: set[str] = set()
        for row in old.execute("SELECT * FROM pets ORDER BY id"):
            stored = json.loads(row["state_json"] or "{}")
            if not isinstance(stored, dict):
                raise ValueError("Source has an invalid conversation state")
            removed_state_keys.update(set(stored) - STATE_DEFAULTS.keys())
            state = {key: stored.get(key, default) for key, default in STATE_DEFAULTS.items()}
            for key, default in STATE_DEFAULTS.items():
                value = state[key]
                if isinstance(default, float):
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                    ):
                        raise ValueError(f"Source has an invalid numeric state field: {key}")
                    state[key] = float(value)
                elif key == "last_proactive_answered":
                    if type(value) not in {bool, int} or value not in {0, 1}:
                        raise ValueError("Source has an invalid proactive response flag")
                    state[key] = int(value)
                elif not isinstance(value, str):
                    raise ValueError(f"Source has an invalid text state field: {key}")
            conversations.append(
                {
                    "id": row["id"],
                    "chat_id": row["chat_id"],
                    "title": "",
                    "created_at": row["born_at"],
                    "summary_until_id": row["summary_until_id"],
                    "compress_fail_count": row["compress_fail_count"],
                    "last_compress_attempt": row["last_compress_attempt"],
                    **state,
                }
            )
        messages = [
            {
                "id": r["id"],
                "conversation_id": r["pet_id"],
                "role": r["role"],
                "content": r["content"],
                "ts": r["ts"],
                "sender_name": r["sender_name"],
                "sender_open_id": r["sender_open_id"],
                "is_observer": r["is_observer"],
                "source": "observer" if r["is_observer"] else "imported",
            }
            for r in old.execute("SELECT * FROM messages ORDER BY id")
        ]
        memories = []
        for r in old.execute("SELECT * FROM memory_cards ORDER BY id"):
            if r["kind"] not in {"reality", "hearsay", "imagination"}:
                raise ValueError("Source contains an unknown memory kind; review before importing")
            memories.append(
                {
                    "id": r["id"],
                    "conversation_id": r["pet_id"],
                    **{
                        k: r[k]
                        for k in (
                            "kind",
                            "when_text",
                            "who",
                            "what",
                            "vibe",
                            "hooks",
                            "created_at",
                            "source_until_id",
                        )
                    },
                }
            )
        names = [
            dict(r)
            for r in old.execute("SELECT open_id,name,updated_at FROM user_names ORDER BY open_id")
        ]
        events = [
            dict(r)
            for r in old.execute("SELECT event_id,created_at FROM event_dedup ORDER BY event_id")
        ]
        cards = [
            {
                "card_id": r["card_id"],
                "conversation_id": r["pet_id"],
                "message_id": r["message_id"],
                "content": r["base_text"],
                "image_key": r["img_key"],
                "created_at": r["built_at"],
            }
            for r in old.execute(
                "SELECT * FROM card_instances WHERE mode='content' AND announced=1 AND message_id!='' ORDER BY card_id"
            )
        ]
        excluded_cards = old.execute("SELECT COUNT(*) FROM card_instances").fetchone()[0] - len(
            cards
        )
        return _write_destination(
            source, destination,
            [
                ("conversations", conversations, "id"),
                ("messages", messages, "id"),
                ("memories", memories, "id"),
                ("user_names", names, "open_id"),
                ("event_dedup", events, "event_id"),
                ("published_cards", cards, "card_id"),
            ],
            source_version=source_version,
            sequences=_sequences(old, {
                "pets": "conversations", "messages": "messages", "memory_cards": "memories",
            }),
            extra_report={
                "excluded_non_content_or_unpublished_cards": excluded_cards,
                "excluded_state_fields": sorted(removed_state_keys),
                "memory_vectors_to_rebuild": len(memories),
                "style_vectors": "rebuild using the active corpus",
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Report already exists; choose a new file")
    try:
        report = migrate(args.source, args.destination)
        with args.report.open("x") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
    except Exception as exc:
        # No raw rows, database values or credentials in command output.
        raise SystemExit(f"Migration stopped: {type(exc).__name__}: {exc}") from None
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "verified": True,
                "counts": {k: v["target_count"] for k, v in report["tables"].items()},
                "memory_vectors_to_rebuild": report["memory_vectors_to_rebuild"],
                "report": str(args.report),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
