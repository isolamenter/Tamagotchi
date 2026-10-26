from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dafeiyu.domain.meme import MemeCatalog

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppConfig:
    feishu_app_id: str
    feishu_app_secret: str
    feishu_verification_token: str
    feishu_encrypt_key: str
    gm_token: str
    openai_base_url: str
    openai_api_key: str
    model_name: str
    embed_model: str
    image_model: str
    llm_provider: str
    gemini_base_url: str
    gemini_api_key: str
    db_path: Path
    feishu_base: str

    prompts: dict
    persona: dict
    style_corpus: dict
    settings: dict
    style_retrieval: dict

    character_name: str
    character_icon: str
    memes_dir: Path
    meme_catalog: MemeCatalog

    persona_style_prompt: str
    persona_style_reinforcement: str
    system_prompt: str
    persona_reinforcement: str
    user_wrap_template: str
    user_wrap_direct_template: str
    user_wrap_observer_template: str
    compress_prompt: str
    compress_user_line_template: str
    compress_assistant_line_template: str
    compress_user_message_template: str
    recall_header: str
    recall_card_template: str
    json_output_prompt: str
    web_search_prompt: str
    proactive_eval_prompt: str
    proactive_prompt: str
    scheduled_event_prompt: str
    fallback_replies: dict
    gm_default_speak_trigger: str

    buffer_keep: int
    compress_threshold: int
    recall_scan_max: int
    reply_debounce_sec: float
    meme_review_timeout_sec: float
    observer_flush_max_count: int
    reply_max_tokens: int
    reply_temperature: float
    autonomous_temperature: float
    scheduled_max_tokens: int
    compress_max_tokens: int

    tick_interval_sec: int
    proactive_cooldown_sec: int
    unanswered_cooldown_sec: int
    proactive_tz_offset_hours: float
    quiet_hours: tuple[int, int]
    quiet_weekends: bool
    scheduled_events: tuple[dict, ...]
    scheduled_grace_sec: int
    proactive_min_new_msgs: int
    event_interject_enabled: bool
    event_interject_debounce_sec: float
    proactive_reply_cooldown_sec: float

    card_enabled: bool
    card_image_timeout_sec: int


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    env = os.environ if env is None else env

    feishu_app_id = env["FEISHU_APP_ID"]
    feishu_app_secret = env["FEISHU_APP_SECRET"]
    feishu_verification_token = env["FEISHU_VERIFICATION_TOKEN"]
    feishu_encrypt_key = env.get("FEISHU_ENCRYPT_KEY", "")
    gm_token = env.get("GM_TOKEN", "")

    llm_provider = (env.get("LLM_PROVIDER", "openai") or "openai").strip().lower()
    if llm_provider not in {"openai", "gemini"}:
        raise ValueError("LLM_PROVIDER must be 'openai' or 'gemini'")
    gemini_base_url = env.get("GEMINI_BASE_URL", "")
    gemini_api_key = (
        env["GEMINI_API_KEY"] if llm_provider == "gemini" else env.get("GEMINI_API_KEY", "")
    )

    prompts = _load_toml(ROOT_DIR / "config" / "prompts.toml")
    persona = _load_toml(ROOT_DIR / "config" / "persona.toml")
    style_corpus = _load_toml(ROOT_DIR / "config" / "style_corpus.toml")
    settings = _load_toml(ROOT_DIR / "config" / "settings.toml")

    memory_config = settings.get("memory", {})
    reply_config = settings.get("reply", {})
    meme_config = settings["meme"]
    observer_config = settings.get("observer", {})
    llm_config = settings.get("llm", {})
    autonomous_config = settings.get("autonomous", {})
    card_config = settings.get("card", {})
    style_retrieval = settings.get("style_retrieval", {})

    persona_style_prompt = persona["style"]["prompt"].strip()
    persona_style_reinforcement = persona["style"]["reinforcement"].strip()
    system_prompt = prompts["system"]["template"].format(style_prompt=persona_style_prompt)
    persona_reinforcement = prompts["persona_reinforcement"]["template"].format(
        style_reinforcement=persona_style_reinforcement
    )

    quiet_start = int(autonomous_config.get("quiet_start_hour", 19))
    quiet_end = int(autonomous_config.get("quiet_end_hour", 10))
    quiet_hours = (quiet_start, quiet_end)

    scheduled_hour_lock = {"diary": quiet_hours[0], "dream": quiet_hours[1]}
    scheduled_events = tuple(
        {**event, "hour": scheduled_hour_lock.get(event["kind"], event["hour"])}
        for event in prompts.get("scheduled_events", [])
    )

    character_name = str(persona.get("style", {}).get("name", "大肥鱼")).strip() or "大肥鱼"
    character_icon = str(persona.get("style", {}).get("icon", "🐋")).strip() or "🐋"
    memes_dir = ROOT_DIR / "assets" / "memes"
    meme_catalog = MemeCatalog(_load_toml(ROOT_DIR / "config" / "meme_corpus.toml"), memes_dir)

    config = AppConfig(
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        feishu_verification_token=feishu_verification_token,
        feishu_encrypt_key=feishu_encrypt_key,
        gm_token=gm_token,
        openai_base_url=(
            env["OPENAI_BASE_URL"] if llm_provider == "openai" else env.get("OPENAI_BASE_URL", "")
        ),
        openai_api_key=(
            env["OPENAI_API_KEY"] if llm_provider == "openai" else env.get("OPENAI_API_KEY", "")
        ),
        model_name=env["CHAT_MODEL"],
        embed_model=env["EMBED_MODEL"],
        image_model=env.get("IMAGE_MODEL", ""),
        llm_provider=llm_provider,
        gemini_base_url=gemini_base_url,
        gemini_api_key=gemini_api_key,
        db_path=Path(env.get("STATE_DB", str(ROOT_DIR / "dafeiyu.db"))),
        feishu_base="https://open.feishu.cn/open-apis",
        prompts=prompts,
        persona=persona,
        style_corpus=style_corpus,
        settings=settings,
        character_name=character_name,
        character_icon=character_icon,
        memes_dir=memes_dir,
        meme_catalog=meme_catalog,
        style_retrieval={
            "semantic_threshold": float(style_retrieval.get("semantic_threshold", 0.62)),
            "example_card_semantic_threshold": float(
                style_retrieval.get("example_card_semantic_threshold", 0.62)
            ),
            "second_semantic_threshold": float(
                style_retrieval.get("second_semantic_threshold", 0.68)
            ),
            "second_max_gap": float(style_retrieval.get("second_max_gap", 0.05)),
            "keyword_bonus": float(style_retrieval.get("keyword_bonus", 0.04)),
            "keyword_bonus_cap": float(style_retrieval.get("keyword_bonus_cap", 0.12)),
            "context_messages": int(style_retrieval.get("context_messages", 2)),
            "query_max_chars": int(style_retrieval.get("query_max_chars", 1500)),
            "embedding_batch_size": int(style_retrieval.get("embedding_batch_size", 50)),
            "init_timeout_sec": float(style_retrieval.get("init_timeout_sec", 30.0)),
        },
        persona_style_prompt=persona_style_prompt,
        persona_style_reinforcement=persona_style_reinforcement,
        system_prompt=system_prompt,
        persona_reinforcement=persona_reinforcement,
        user_wrap_template=prompts["user_wrap"]["template"],
        user_wrap_direct_template=prompts["user_wrap"]["direct_template"],
        user_wrap_observer_template=prompts["user_wrap"]["observer_template"],
        compress_prompt=prompts["compress"]["prompt"],
        compress_user_line_template=prompts["compress"]["user_line_template"],
        compress_assistant_line_template=prompts["compress"]["assistant_line_template"].replace(
            "{character_name}", character_name
        ),
        compress_user_message_template=prompts["compress"]["user_message_template"],
        recall_header=prompts["recall"]["header"],
        recall_card_template=prompts["recall"]["card_template"],
        json_output_prompt=prompts["json_output"]["prompt"] + "\n" + meme_catalog.render_prompt(),
        web_search_prompt=prompts["web_search"]["prompt"],
        proactive_eval_prompt=prompts.get("proactive_eval", {}).get("prompt", ""),
        proactive_prompt=prompts["proactive"]["prompt"],
        scheduled_event_prompt=prompts["scheduled_event"]["prompt"],
        fallback_replies=prompts["fallback_reply"],
        gm_default_speak_trigger=prompts["gm"]["default_speak_trigger"],
        buffer_keep=int(memory_config.get("buffer_keep", 80)),
        compress_threshold=int(memory_config.get("compress_threshold", 200)),
        recall_scan_max=int(memory_config.get("recall_scan_max", 2000)),
        reply_debounce_sec=float(reply_config.get("debounce_sec", 1.5)),
        meme_review_timeout_sec=float(meme_config["review_timeout_sec"]),
        observer_flush_max_count=int(observer_config.get("flush_max_count", 50)),
        reply_max_tokens=int(llm_config.get("reply_max_tokens", 1200)),
        reply_temperature=float(llm_config.get("reply_temperature", 0.5)),
        autonomous_temperature=float(llm_config.get("autonomous_temperature", 0.5)),
        scheduled_max_tokens=int(llm_config.get("scheduled_max_tokens", 1000)),
        compress_max_tokens=int(llm_config.get("compress_max_tokens", 2000)),
        tick_interval_sec=int(autonomous_config.get("tick_interval_sec", 600)),
        proactive_cooldown_sec=int(autonomous_config.get("cooldown_sec", 600)),
        unanswered_cooldown_sec=int(autonomous_config.get("unanswered_cooldown_sec", 1800)),
        proactive_tz_offset_hours=float(autonomous_config.get("timezone_offset_hours", 8.0)),
        quiet_hours=quiet_hours,
        quiet_weekends=bool(autonomous_config.get("quiet_weekends", False)),
        scheduled_events=tuple(scheduled_events),
        scheduled_grace_sec=int(autonomous_config.get("scheduled_grace_sec", 3600)),
        proactive_min_new_msgs=int(autonomous_config.get("proactive_min_new_msgs", 2)),
        event_interject_enabled=bool(autonomous_config.get("event_interject_enabled", True)),
        event_interject_debounce_sec=float(
            autonomous_config.get("event_interject_debounce_sec", 2.0)
        ),
        proactive_reply_cooldown_sec=float(autonomous_config.get("reply_cooldown_sec", 60.0)),
        card_enabled=bool(card_config.get("enabled", True)),
        card_image_timeout_sec=int(card_config.get("image_timeout_sec", 120)),
    )

    required = {
        "FEISHU_APP_ID": config.feishu_app_id,
        "FEISHU_APP_SECRET": config.feishu_app_secret,
        "FEISHU_VERIFICATION_TOKEN": config.feishu_verification_token,
        "CHAT_MODEL": config.model_name,
        "EMBED_MODEL": config.embed_model,
    }
    if config.llm_provider == "gemini":
        required["GEMINI_API_KEY"] = config.gemini_api_key
    else:
        required.update(
            OPENAI_BASE_URL=config.openai_base_url, OPENAI_API_KEY=config.openai_api_key
        )
    missing = [key for key, value in required.items() if not value.strip()]
    if missing:
        raise ValueError("Missing configuration: " + ", ".join(missing))
    if not 0 <= config.buffer_keep < config.compress_threshold:
        raise ValueError("memory.buffer_keep must be below compress_threshold")
    if any(
        value <= 0
        for value in (
            config.tick_interval_sec,
            config.reply_max_tokens,
            config.scheduled_max_tokens,
            config.observer_flush_max_count,
            config.recall_scan_max,
        )
    ):
        raise ValueError("Intervals, limits and token budgets must be positive")
    if config.reply_debounce_sec < 0 or config.event_interject_debounce_sec < 0:
        raise ValueError("Debounce cannot be negative")
    if not 0 < config.meme_review_timeout_sec <= 60:
        raise ValueError("meme.review_timeout_sec must be in (0, 60]")
    if not all(0 <= hour < 24 for hour in config.quiet_hours):
        raise ValueError("Quiet hours must be between 0 and 23")
    if any(event["kind"] not in {"dream", "diary"} for event in config.scheduled_events):
        raise ValueError("Unknown scheduled event")
    return config
