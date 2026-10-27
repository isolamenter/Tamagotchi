BEGIN IMMEDIATE;
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT UNIQUE NOT NULL CHECK(length(chat_id) > 0),
    title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    summary_until_id INTEGER NOT NULL DEFAULT 0,
    compress_fail_count INTEGER NOT NULL DEFAULT 0,
    last_compress_attempt REAL NOT NULL DEFAULT 0,
    last_update_ts REAL NOT NULL DEFAULT 0,
    last_social_ts REAL NOT NULL DEFAULT 0,
    last_reply_ts REAL NOT NULL DEFAULT 0,
    last_proactive_ts REAL NOT NULL DEFAULT 0,
    last_dream_date TEXT NOT NULL DEFAULT '',
    last_diary_date TEXT NOT NULL DEFAULT '',
    last_proactive_topic TEXT NOT NULL DEFAULT '',
    last_proactive_answered INTEGER NOT NULL DEFAULT 1 CHECK(last_proactive_answered IN (0,1))
);
CREATE TABLE user_names (
    open_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    ts REAL NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    sender_open_id TEXT NOT NULL DEFAULT '',
    is_observer INTEGER NOT NULL DEFAULT 0 CHECK(is_observer IN (0,1)),
    source TEXT NOT NULL DEFAULT 'direct' CHECK(source IN ('direct','observer','proactive','dream','diary','manual','imported')),
    reply_mode TEXT NOT NULL DEFAULT '',
    meme_tag TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    mentions_json TEXT NOT NULL DEFAULT '[]',
    content_kind TEXT NOT NULL DEFAULT 'text',
    evidence_text TEXT
);
CREATE INDEX idx_messages_conversation ON messages(conversation_id,id);
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL CHECK(kind IN ('reality','hearsay','imagination')),
    when_text TEXT NOT NULL DEFAULT '',
    who TEXT NOT NULL DEFAULT '',
    what TEXT NOT NULL,
    vibe TEXT NOT NULL DEFAULT '',
    hooks TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    source_until_id INTEGER NOT NULL DEFAULT 0,
    subject_ids TEXT NOT NULL DEFAULT '[]',
    reporter_ids TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'legacy' CHECK(origin IN ('legacy','compression','daily')),
    profile_key TEXT NOT NULL DEFAULT '',
    profile_category TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'uncertain',
    expires_at REAL,
    superseded_by INTEGER REFERENCES memories(id)
);
CREATE INDEX idx_memories_conversation ON memories(conversation_id,id);
CREATE TABLE memory_embeddings (
    memory_id INTEGER PRIMARY KEY REFERENCES memories(id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK(dimension > 0),
    content TEXT NOT NULL,
    vec BLOB NOT NULL,
    updated_at REAL NOT NULL,
    CHECK(length(vec) = dimension * 4)
);
CREATE TABLE style_embeddings (
    example_id TEXT NOT NULL,
    embedding_type TEXT NOT NULL CHECK(embedding_type IN ('catchphrase','example_card')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK(dimension > 0),
    vec BLOB NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(example_id,provider,model),
    CHECK(length(vec) = dimension * 4)
);
CREATE TABLE event_dedup (event_id TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE sys_cache (key TEXT PRIMARY KEY, val TEXT NOT NULL, expires_at REAL);
CREATE TABLE published_cards (
    card_id TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    message_id TEXT NOT NULL,
    content TEXT NOT NULL,
    image_key TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE consolidation_state (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id),
    until_id INTEGER NOT NULL DEFAULT 0,
    next_offset INTEGER NOT NULL DEFAULT 0,
    target_id INTEGER NOT NULL DEFAULT 0,
    run_date TEXT NOT NULL DEFAULT '',
    calls_today INTEGER NOT NULL DEFAULT 0,
    last_attempt REAL NOT NULL DEFAULT 0,
    last_success REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE identity_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    subject_open_id TEXT NOT NULL CHECK(length(subject_open_id)>0),
    alias TEXT NOT NULL CHECK(length(alias)>0),
    status TEXT NOT NULL CHECK(status IN ('confirmed','uncertain','retired')),
    evidence TEXT NOT NULL,
    created_at REAL NOT NULL,
    source_until_id INTEGER NOT NULL,
    UNIQUE(conversation_id,subject_open_id,alias,status,source_until_id,evidence)
);
CREATE INDEX idx_identity_conversation ON identity_aliases(conversation_id,subject_open_id,id);
CREATE TABLE proactive_evaluations (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id),
    until_id INTEGER NOT NULL DEFAULT 0,
    last_attempt REAL NOT NULL DEFAULT 0
);
PRAGMA user_version = 3;
COMMIT;
