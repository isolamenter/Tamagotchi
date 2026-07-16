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
    pet_config: dict

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
    state_render_header: str
    state_render_line_prefix: str
    state_render_vibe_template: str
    state_render_lines: dict
    json_output_prompt: str
    proactive_prompt: str
    proactive_trigger_templates: dict
    proactive_user_stub_template: str
    scheduled_event_prompt: str
    scheduled_user_stub_template: str
    fallback_replies: dict
    gm_default_speak_trigger: str

    buffer_keep: int
    compress_threshold: int
    recall_scan_max: int
    reply_min_interval_sec: int
    observer_flush_max_count: int
    reply_max_tokens: int
    scheduled_max_tokens: int
    compress_max_tokens: int
    card_reply_max_tokens: int

    initial_state: dict[str, float]
    decay_active: dict
    decay_quiet: dict
    state_bands: dict
    state_numeric_keys: tuple[str, ...]
    recent_vibe_pool: list

    tick_interval_sec: int
    proactive_cooldown_sec: int
    proactive_tz_offset_hours: float
    quiet_hours: tuple[int, int]
    quiet_weekends: bool
    scheduled_events: tuple[dict, ...]
    satiety_trigger: float
    mood_trigger: float
    energy_trigger: float
    curiosity_trigger: float
    affection_trigger: float
    spontaneous_prob: float

    card_enabled: bool
    card_bar_width: int
    card_max_buttons: int
    card_scheduled_max_settlements: int
    card_default_actions: list
    card_button_ttl_sec: int
    card_log_max_lines: int
    card_actions: dict
    card_bar_filled: str
    card_bar_empty: str
    card_bars_header: str
    card_toast_done: str
    card_toast_expired: str
    card_followup_prefix: str
    card_vibe_template: str
    card_bar_labels: dict
    card_action_text: dict
    card_action_reply_prompt: str
    card_image_timeout_sec: int

    gameplay_enabled: bool
    gameplay_need_ttl_sec: int
    gameplay_need_cooldown_sec: int
    gameplay_need_thresholds: dict[str, float]
    gameplay_resolution_thresholds: dict[str, float]
    gameplay_need_severe_ttl_sec: int
    scheduled_grace_sec: int


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _parse_decay_table(initial_state: dict[str, float], raw: dict) -> dict:
    out = {}
    for key in initial_state.keys():
        cfg = raw.get(key) or {}
        out[key] = {
            "baseline": float(cfg.get("baseline", initial_state.get(key, 50.0))),
            "rate": float(cfg.get("rate", 0.0)),
        }
    return out


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
    # provider=gemini 时缺 key 直接 KeyError 早死（符合 os.environ 直读约束）
    gemini_api_key = (
        env["GEMINI_API_KEY"] if llm_provider == "gemini" else env.get("GEMINI_API_KEY", "")
    )

    prompts = _load_toml(ROOT_DIR / "prompts.toml")
    pet_style = _load_toml(ROOT_DIR / "pet_style.toml")
    pet_config = _load_toml(ROOT_DIR / "pet_config.toml")

    pet_style_prompt = pet_style["style"]["prompt"]
    pet_style_reinforcement = pet_style["style"]["reinforcement"]
    system_prompt = prompts["system"]["template"].format(style_prompt=pet_style_prompt)
    persona_reinforcement = prompts["persona_reinforcement"]["template"].format(
        style_reinforcement=pet_style_reinforcement
    )

    memory_config = pet_config["memory"]
    reply_config = pet_config["reply"]
    observer_config = pet_config["observer"]
    llm_config = pet_config["llm"]
    state_config = pet_config["state"]

    initial_state = {k: float(v) for k, v in state_config["initial"].items()}
    state_numeric_keys = tuple(initial_state.keys())
    decay_active = _parse_decay_table(initial_state, state_config.get("decay_active", {}))
    decay_quiet = _parse_decay_table(initial_state, state_config.get("decay_quiet", {}))

    autonomous_config = pet_config["autonomous"]
    quiet_hours = (
        int(autonomous_config["quiet_start_hour"]),
        int(autonomous_config["quiet_end_hour"]),
    )
    scheduled_hour_lock = {"diary": quiet_hours[0], "dream": quiet_hours[1]}
    scheduled_events = tuple(
        {**event, "hour": scheduled_hour_lock.get(event["kind"], event["hour"])}
        for event in prompts["scheduled_events"]
    )
    trigger_thresholds = autonomous_config["trigger_thresholds"]

    card_config = pet_config.get("card", {})
    gameplay_config = pet_config.get("gameplay", {})
    card_prompts = prompts.get("card", {})
    card_bars = dict(card_prompts.get("bars", {}))
    card_vibe_template = card_bars.pop("vibe_template", "✨ {vibe}")

    state_render = prompts["state_render"]

    return AppConfig(
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        feishu_verification_token=feishu_verification_token,
        feishu_encrypt_key=feishu_encrypt_key,
        gm_token=gm_token,
        # The two providers are alternatives: a Gemini-only deployment should
        # not require unused OpenAI credentials at startup.
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
        pet_config=pet_config,
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
        state_render_header=state_render["header"],
        state_render_line_prefix=state_render["line_prefix"],
        state_render_vibe_template=state_render["vibe_template"],
        state_render_lines=state_render["lines"],
        json_output_prompt=prompts["json_output"]["prompt"],
        proactive_prompt=prompts["proactive"]["prompt"],
        proactive_trigger_templates=prompts["proactive_triggers"],
        proactive_user_stub_template=prompts["autonomous_user_stub"]["proactive"],
        scheduled_event_prompt=prompts["scheduled_event"]["prompt"],
        scheduled_user_stub_template=prompts["autonomous_user_stub"]["scheduled"],
        fallback_replies=prompts["fallback_reply"],
        gm_default_speak_trigger=prompts["gm"]["default_speak_trigger"],
        buffer_keep=int(memory_config["buffer_keep"]),
        compress_threshold=int(memory_config["compress_threshold"]),
        recall_scan_max=int(memory_config.get("recall_scan_max", 2000)),
        reply_min_interval_sec=int(reply_config["min_interval_sec"]),
        observer_flush_max_count=int(observer_config["flush_max_count"]),
        reply_max_tokens=int(llm_config["reply_max_tokens"]),
        scheduled_max_tokens=int(llm_config["scheduled_max_tokens"]),
        compress_max_tokens=int(llm_config["compress_max_tokens"]),
        card_reply_max_tokens=int(llm_config.get("card_reply_max_tokens", 150)),
        initial_state=initial_state,
        decay_active=decay_active,
        decay_quiet=decay_quiet,
        state_bands={k: dict(v) for k, v in state_config.get("bands", {}).items()},
        state_numeric_keys=state_numeric_keys,
        recent_vibe_pool=list(pet_style.get("recent_vibes", {}).get("pool", [])),
        tick_interval_sec=int(autonomous_config["tick_interval_sec"]),
        proactive_cooldown_sec=int(autonomous_config["cooldown_sec"]),
        proactive_tz_offset_hours=float(autonomous_config["timezone_offset_hours"]),
        quiet_hours=quiet_hours,
        quiet_weekends=bool(autonomous_config.get("quiet_weekends", False)),
        scheduled_events=scheduled_events,
        satiety_trigger=float(trigger_thresholds["satiety"]),
        mood_trigger=float(trigger_thresholds["mood"]),
        energy_trigger=float(trigger_thresholds["energy"]),
        curiosity_trigger=float(trigger_thresholds["curiosity"]),
        affection_trigger=float(trigger_thresholds["affection"]),
        spontaneous_prob=float(autonomous_config["spontaneous_prob"]),
        card_enabled=bool(card_config.get("enabled", False)),
        card_bar_width=int(card_config.get("bar_width", 10)),
        card_max_buttons=int(card_config.get("max_buttons", 3)),
        card_scheduled_max_settlements=int(card_config.get("scheduled_max_settlements", 3)),
        card_default_actions=list(card_config.get("default_actions", [])),
        card_button_ttl_sec=int(card_config.get("button_ttl_sec", 1800)),
        card_log_max_lines=int(card_config.get("card_log_max_lines", 6)),
        card_actions={k: dict(v) for k, v in card_config.get("actions", {}).items()},
        card_bar_filled=card_prompts.get("bar_filled", "▰"),
        card_bar_empty=card_prompts.get("bar_empty", "▱"),
        card_bars_header=card_prompts.get("bars_header", ""),
        card_toast_done=card_prompts.get("toast_done", "✓"),
        card_toast_expired=card_prompts.get("toast_expired", "这张卡片过期了~"),
        card_followup_prefix=card_prompts.get("followup_prefix", "↳ "),
        card_vibe_template=card_vibe_template,
        card_bar_labels=card_bars,
        card_action_text={k: dict(v) for k, v in card_prompts.get("actions", {}).items()},
        card_action_reply_prompt=card_prompts.get("action_reply", {}).get("prompt", ""),
        card_image_timeout_sec=int(card_config.get("image_timeout_sec", 120)),
        gameplay_enabled=bool(gameplay_config.get("enabled", True)),
        gameplay_need_ttl_sec=int(gameplay_config.get("need_ttl_sec", 7200)),
        gameplay_need_cooldown_sec=int(
            gameplay_config.get(
                "need_cooldown_sec", gameplay_config.get("need_ttl_sec", 7200)
            )
        ),
        gameplay_need_thresholds={
            k: float(v)
            for k, v in gameplay_config.get(
                "need_thresholds",
                {
                    "hungry": 80,
                    "sleepy": 25,
                    "sad": 30,
                    "bored": 30,
                    "lonely": 25,
                },
            ).items()
        },
        gameplay_resolution_thresholds={
            k: float(v)
            for k, v in gameplay_config.get("resolution_thresholds", {}).items()
        },
        gameplay_need_severe_ttl_sec=int(
            gameplay_config.get("need_severe_ttl_sec", gameplay_config.get("need_ttl_sec", 7200))
        ),
        scheduled_grace_sec=int(autonomous_config.get("scheduled_grace_sec", 3600)),
    )
