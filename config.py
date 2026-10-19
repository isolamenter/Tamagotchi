from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


ROOT_DIR = Path(__file__).resolve().parent


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
    pet_style: dict
    style_corpus: dict
    pet_config: dict
    style_retrieval: dict

    pet_style_prompt: str
    pet_style_reinforcement: str
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
    proactive_eval_prompt: str
    proactive_prompt: str
    scheduled_event_prompt: str
    fallback_replies: dict
    gm_default_speak_trigger: str

    buffer_keep: int
    compress_threshold: int
    recall_scan_max: int
    reply_debounce_sec: float
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
    card_toast_done: str
    card_toast_expired: str


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    env = env or os.environ

    feishu_app_id = env["FEISHU_APP_ID"]
    feishu_app_secret = env["FEISHU_APP_SECRET"]
    feishu_verification_token = env["FEISHU_VERIFICATION_TOKEN"]
    feishu_encrypt_key = env.get("FEISHU_ENCRYPT_KEY", "")
    gm_token = env.get("GM_TOKEN") or feishu_verification_token

    llm_provider = (env.get("LLM_PROVIDER", "openai") or "openai").strip().lower()
    if llm_provider not in {"openai", "gemini"}:
        raise ValueError("LLM_PROVIDER must be 'openai' or 'gemini'")
    gemini_base_url = env.get("GEMINI_BASE_URL", "")
    gemini_api_key = (
        env["GEMINI_API_KEY"] if llm_provider == "gemini" else env.get("GEMINI_API_KEY", "")
    )

    prompts = _load_toml(ROOT_DIR / "prompts.toml")
    pet_style = _load_toml(ROOT_DIR / "pet_style.toml")
    style_corpus = _load_toml(ROOT_DIR / "style_corpus.toml")
    pet_config = _load_toml(ROOT_DIR / "pet_config.toml")

    memory_config = pet_config.get("memory", {})
    reply_config = pet_config.get("reply", {})
    observer_config = pet_config.get("observer", {})
    llm_config = pet_config.get("llm", {})
    autonomous_config = pet_config.get("autonomous", {})
    card_config = pet_config.get("card", {})
    style_retrieval = pet_config.get("style_retrieval", {})

    pet_style_prompt = pet_style["style"]["prompt"].strip()
    pet_style_reinforcement = pet_style["style"]["reinforcement"].strip()
    system_prompt = prompts["system"]["template"].format(
        style_prompt=pet_style_prompt
    )
    persona_reinforcement = prompts["persona_reinforcement"]["template"].format(
        style_reinforcement=pet_style_reinforcement
    )

    quiet_start = int(autonomous_config.get("quiet_start_hour", 19))
    quiet_end = int(autonomous_config.get("quiet_end_hour", 10))
    quiet_hours = (quiet_start, quiet_end)

    scheduled_hour_lock = {"diary": quiet_hours[0], "dream": quiet_hours[1]}
    scheduled_events = tuple(
        {**event, "hour": scheduled_hour_lock.get(event["kind"], event["hour"])}
        for event in prompts.get("scheduled_events", [])
    )

    card_prompts = prompts.get("card", {})

    return AppConfig(
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        feishu_verification_token=feishu_verification_token,
        feishu_encrypt_key=feishu_encrypt_key,
        gm_token=gm_token,
        openai_base_url=(env["OPENAI_BASE_URL"] if llm_provider == "openai" else env.get("OPENAI_BASE_URL", "")),
        openai_api_key=(env["OPENAI_API_KEY"] if llm_provider == "openai" else env.get("OPENAI_API_KEY", "")),
        model_name=env.get("CHAT_MODEL", "gpt-4o-mini"),
        embed_model=env.get("EMBED_MODEL", "text-embedding-3-small"),
        image_model=env.get("IMAGE_MODEL", ""),
        llm_provider=llm_provider,
        gemini_base_url=gemini_base_url,
        gemini_api_key=gemini_api_key,
        db_path=Path(env.get("STATE_DB", "state.db")),
        feishu_base="https://open.feishu.cn/open-apis",
        prompts=prompts,
        pet_style=pet_style,
        style_corpus=style_corpus,
        pet_config=pet_config,
        style_retrieval={
            "semantic_threshold": float(
                style_retrieval.get("semantic_threshold", 0.62)
            ),
            "example_card_semantic_threshold": float(
                style_retrieval.get("example_card_semantic_threshold", 0.62)
            ),
            "second_semantic_threshold": float(
                style_retrieval.get("second_semantic_threshold", 0.68)
            ),
            "second_max_gap": float(
                style_retrieval.get("second_max_gap", 0.05)
            ),
            "keyword_bonus": float(
                style_retrieval.get("keyword_bonus", 0.04)
            ),
            "keyword_bonus_cap": float(
                style_retrieval.get("keyword_bonus_cap", 0.12)
            ),
            "context_messages": int(style_retrieval.get("context_messages", 2)),
            "query_max_chars": int(style_retrieval.get("query_max_chars", 1500)),
            "assistant_history_keep": int(
                style_retrieval.get("assistant_history_keep", 2)
            ),
            "embedding_batch_size": int(
                style_retrieval.get("embedding_batch_size", 50)
            ),
            "init_timeout_sec": float(
                style_retrieval.get("init_timeout_sec", 30.0)
            ),
        },
        pet_style_prompt=pet_style_prompt,
        pet_style_reinforcement=pet_style_reinforcement,
        system_prompt=system_prompt,
        persona_reinforcement=persona_reinforcement,
        user_wrap_template=prompts["user_wrap"]["template"],
        user_wrap_direct_template=prompts["user_wrap"]["direct_template"],
        user_wrap_observer_template=prompts["user_wrap"]["observer_template"],
        compress_prompt=prompts["compress"]["prompt"],
        compress_user_line_template=prompts["compress"]["user_line_template"],
        compress_assistant_line_template=prompts["compress"]["assistant_line_template"],
        compress_user_message_template=prompts["compress"]["user_message_template"],
        recall_header=prompts["recall"]["header"],
        recall_card_template=prompts["recall"]["card_template"],
        json_output_prompt=prompts["json_output"]["prompt"],
        proactive_eval_prompt=prompts.get("proactive_eval", {}).get("prompt", ""),
        proactive_prompt=prompts["proactive"]["prompt"],
        scheduled_event_prompt=prompts["scheduled_event"]["prompt"],
        fallback_replies=prompts["fallback_reply"],
        gm_default_speak_trigger=prompts["gm"]["default_speak_trigger"],
        buffer_keep=int(memory_config.get("buffer_keep", 80)),
        compress_threshold=int(memory_config.get("compress_threshold", 200)),
        recall_scan_max=int(memory_config.get("recall_scan_max", 2000)),
        reply_debounce_sec=float(reply_config.get("debounce_sec", 1.5)),
        observer_flush_max_count=int(observer_config.get("flush_max_count", 50)),
        reply_max_tokens=int(llm_config.get("reply_max_tokens", 350)),
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
        event_interject_debounce_sec=float(autonomous_config.get("event_interject_debounce_sec", 2.0)),
        proactive_reply_cooldown_sec=float(autonomous_config.get("reply_cooldown_sec", 60.0)),
        card_enabled=bool(card_config.get("enabled", True)),
        card_image_timeout_sec=int(card_config.get("image_timeout_sec", 120)),
        card_toast_done=card_prompts.get("toast_done", "没问题"),
        card_toast_expired=card_prompts.get("toast_expired", "拉了"),
    )
