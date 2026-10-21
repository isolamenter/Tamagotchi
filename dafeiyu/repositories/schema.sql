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
    meme_tag TEXT NOT NULL DEFAULT ''
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
    source_until_id INTEGER NOT NULL DEFAULT 0
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
PRAGMA user_version = 2;
COMMIT;
