"""Validate synthetic review cases and create a blank manual review sheet.

This tool never imports the app, reads credentials, calls a model, or grades replies.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

DEFAULT_CASES = Path(__file__).with_name("dialogue_cases.json")
CATEGORIES = {
    "persona",
    "memory_correction",
    "speaker_identity",
    "repetition",
    "boundaries",
    "group_intervention",
}
REPLY_MODES = {"reaction", "normal", "substantive"}
MEMORY_KINDS = {"reality", "hearsay", "imagination"}


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _text_list(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(_text(item) for item in value)


def validate_cases(data: object) -> list[str]:
    """Return all actionable structural errors; this is not a behavior evaluation."""
    if not isinstance(data, dict):
        return ["dataset must be an object"]
    errors = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if data.get("synthetic") is not True:
        errors.append("synthetic must be true; do not add private chat exports")
    if data.get("status") != "not_run":
        errors.append("dataset status must remain not_run; record results in a separate sheet")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        return [*errors, "cases must be a nonempty list"]
    seen = set()
    categories = set()
    for index, case in enumerate(cases):
        label = f"case[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{label} must be an object")
            continue
        case_id = case.get("id")
        if not _text(case_id):
            errors.append(f"{label}.id must be a nonempty string")
        elif case_id in seen:
            errors.append(f"{label}.id duplicates {case_id}")
        else:
            seen.add(case_id)
            label = case_id
        if not _text(case.get("title")):
            errors.append(f"{label}.title is required")
        category = case.get("category")
        if not isinstance(category, str) or category not in CATEGORIES:
            errors.append(f"{label}.category is unknown")
        else:
            categories.add(category)
        stage = case.get("stage")
        if stage not in ("reply", "proactive_eval"):
            errors.append(f"{label}.stage must be reply or proactive_eval")
        if (category == "group_intervention") != (stage == "proactive_eval"):
            errors.append(f"{label}.stage does not match its category")
        participants = case.get("participants")
        if not isinstance(participants, dict) or not participants:
            errors.append(f"{label}.participants must be a nonempty mapping")
            participants = {}
        elif not all(_text(key) and _text(value) for key, value in participants.items()):
            errors.append(f"{label}.participants needs nonempty speaker IDs and names")
        memories = case.get("memories")
        if not isinstance(memories, list):
            errors.append(f"{label}.memories must be a list")
        else:
            for memory in memories:
                if not isinstance(memory, dict):
                    errors.append(f"{label}.memory must be an object")
                    continue
                if memory.get("kind") not in ("reality", "hearsay", "imagination"):
                    errors.append(f"{label}.memory kind is invalid")
                if not _text(memory.get("who")) or not _text(memory.get("what")):
                    errors.append(f"{label}.memory requires who and what")
        turns = case.get("turns")
        if not isinstance(turns, list) or len(turns) < 2:
            errors.append(f"{label}.turns needs multi-turn context")
            turns = []
        users = 0
        for turn in turns:
            if not isinstance(turn, dict):
                errors.append(f"{label}.turn must be an object")
                continue
            if not _text(turn.get("text")):
                errors.append(f"{label}.turn text is required")
            if turn.get("role") == "user":
                users += 1
                speaker = turn.get("speaker_id")
                if not isinstance(speaker, str) or speaker not in participants:
                    errors.append(f"{label}.turn references an unknown speaker")
                if type(turn.get("directed_to_bot")) is not bool:
                    errors.append(f"{label}.turn needs boolean directed_to_bot")
                if stage == "proactive_eval" and turn.get("directed_to_bot") is not False:
                    errors.append(f"{label}.proactive inputs must not mention the bot")
            elif turn.get("role") != "assistant":
                errors.append(f"{label}.turn role must be user or assistant")
        if users < 2:
            errors.append(f"{label}.turns needs at least two user messages")
        if turns and (not isinstance(turns[-1], dict) or turns[-1].get("role") != "user"):
            errors.append(f"{label}.last turn must be a user message to evaluate")
        if turns and isinstance(turns[-1], dict) and stage == "reply":
            if turns[-1].get("directed_to_bot") is not True:
                errors.append(f"{label}.final reply input must be directed to the bot")
        expected = case.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{label}.expected must be an object")
            continue
        for key in ("must", "must_not"):
            if not _text_list(expected.get(key)):
                errors.append(f"{label}.expected.{key} needs nonempty criteria")
        if stage == "reply":
            modes = expected.get("allowed_reply_modes")
            if not _text_list(modes) or any(mode not in REPLY_MODES for mode in modes):
                errors.append(f"{label}.allowed_reply_modes is invalid")
        elif stage == "proactive_eval" and type(expected.get("should_speak")) is not bool:
            errors.append(f"{label}.expected.should_speak must be boolean")
    if missing := CATEGORIES - categories:
        errors.append(f"missing categories: {', '.join(sorted(missing))}")
    return errors


def make_review_sheet(data: dict) -> dict:
    """Create explicit unexecuted observations, never inferred or fabricated scores."""
    errors = validate_cases(data)
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "schema_version": 1,
        "status": "not_run",
        "run_metadata": {
            "repository_commit": "",
            "model": "",
            "temperature": None,
            "prompt_config_revision": "",
            "execution_date": "",
            "reviewer": "",
            "context_preparation": "",
        },
        "results": [
            {
                "case_id": case["id"],
                "stage": case["stage"],
                "status": "not_run",
                "actual_output": None,
                "contract_verdict": "not_run",
                "criteria": [
                    {"kind": kind, "text": text, "verdict": "not_run", "evidence": ""}
                    for kind in ("must", "must_not")
                    for text in case["expected"][kind]
                ],
                "notes": "",
            }
            for case in data["cases"]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--template", type=Path, help="Create a blank review JSON; refuses to overwrite files"
    )
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.cases.read_text(encoding="utf-8"))
        errors = validate_cases(data)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        counts = Counter(case["category"] for case in data["cases"])
        print(f"Validated {len(data['cases'])} synthetic cases: {dict(sorted(counts.items()))}")
        print("Structure only. Model behavior has NOT been evaluated.")
        if args.template:
            with args.template.open("x", encoding="utf-8") as target:
                json.dump(make_review_sheet(data), target, ensure_ascii=False, indent=2)
                target.write("\n")
            print(f"Created unexecuted review sheet: {args.template}")
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
