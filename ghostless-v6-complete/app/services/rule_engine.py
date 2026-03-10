"""
Ghostless API — Validation Rule Engine

Rules are registered as methods. Each returns:
  (List[Warning], List[str], float)  →  warnings, suggestions, score_delta

Add new rules by:
  1. Define _check_<rule_name>(self, task, history) method
  2. Add rule_name to RULES[task_type] list

ML hook is pluggable — replace ml_score_hook with your model server call.
"""
from typing import List, Optional, Tuple
from pydantic import BaseModel
from app.config import settings


# ─── Pydantic schemas ─────────────────────────────────────────────────────────

class ValidationWarning(BaseModel):
    code: str
    message: str
    severity: str  # info | warning | error
    field: Optional[str] = None


class RuleResult(BaseModel):
    warnings: List[ValidationWarning]
    suggestions: List[str]
    quality_score: float
    flags: List[str]


# ─── Rule Engine ─────────────────────────────────────────────────────────────

class RuleEngine:

    # Rules applied per task type. 'default' is always applied.
    RULES: dict = {
        "image_label":   ["check_completion_time", "check_min_labels", "check_confidence_spread", "check_bounding_boxes"],
        "transcription": ["check_completion_time", "check_word_count",  "check_punctuation_density"],
        "survey":        ["check_completion_time", "check_straight_lining", "check_min_survey_length"],
        "moderation":    ["check_completion_time", "check_moderation_verdict"],
        "default":       ["check_completion_time"],
    }

    def run(self, task_type: str, payload: dict, completion_time: float, worker_history: dict) -> RuleResult:
        warnings:    List[ValidationWarning] = []
        suggestions: List[str] = []
        flags:       List[str] = []
        score = 1.0

        rules = self.RULES.get(task_type, []) + self.RULES["default"]
        # Deduplicate while preserving order
        seen, unique_rules = set(), []
        for r in rules:
            if r not in seen:
                seen.add(r); unique_rules.append(r)

        for rule_name in unique_rules:
            fn = getattr(self, f"_{rule_name}", None)
            if fn is None:
                continue
            w, s, delta = fn(payload, completion_time, worker_history)
            warnings.extend(w)
            suggestions.extend(s)
            score = max(0.0, score + delta)
            # Extract flags from warning codes
            for warning in w:
                flags.append(warning.code)

        # Worker trust bonus/penalty (±10 pts shifts score ±2%)
        trust = worker_history.get("trust_score", 50)
        trust_adjustment = (trust - 50) / 500   # ±0.10 max
        score = min(1.0, max(0.0, score + trust_adjustment))

        return RuleResult(
            warnings=warnings,
            suggestions=suggestions,
            quality_score=round(score, 4),
            flags=list(set(flags)),
        )

    # ── Rule Implementations ──────────────────────────────────────────────────

    def _check_completion_time(self, payload, completion_time, history) -> Tuple:
        avg = history.get("avg_completion_time", 120.0)
        warnings, suggestions = [], []

        if completion_time < avg * settings.SPEED_FLAG_THRESHOLD_PCT:
            return (
                [ValidationWarning(
                    code="SPEED_FLAG",
                    message=f"Completed in {completion_time:.0f}s, far below your average of {avg:.0f}s. This triggers an automatic quality flag.",
                    severity="error",
                )],
                ["Slow down and review your work carefully before submitting."],
                -0.40,
            )

        if completion_time < avg * settings.SPEED_WARNING_THRESHOLD_PCT:
            return (
                [ValidationWarning(
                    code="SPEED_WARNING",
                    message=f"Completed in {completion_time:.0f}s — unusually fast compared to your average ({avg:.0f}s).",
                    severity="warning",
                )],
                ["Take a moment to review your answers before submitting."],
                -0.15,
            )

        return [], [], 0.0

    def _check_min_labels(self, payload, completion_time, history) -> Tuple:
        labels = payload.get("labels", [])
        if not labels:
            return (
                [ValidationWarning(code="NO_LABELS", message="At least one label is required for image labeling tasks.", severity="error")],
                ["Select at least one label that best describes the image."],
                -1.0,
            )
        if len(labels) > 20:
            return (
                [ValidationWarning(code="TOO_MANY_LABELS", message=f"You submitted {len(labels)} labels — this may indicate low selectivity.", severity="warning")],
                ["Focus on the most relevant labels rather than selecting everything."],
                -0.10,
            )
        return [], [], 0.0

    def _check_confidence_spread(self, payload, completion_time, history) -> Tuple:
        confidences = [v for k, v in payload.items() if "confidence" in k.lower() and isinstance(v, (int, float))]
        if not confidences:
            return [], [], 0.0
        if all(c >= 0.98 for c in confidences):
            return (
                [],
                ["If you're uncertain about some labels, mark them with lower confidence — it improves overall accuracy."],
                -0.05,
            )
        return [], [], 0.0

    def _check_bounding_boxes(self, payload, completion_time, history) -> Tuple:
        boxes = payload.get("bounding_boxes", [])
        labels = payload.get("labels", [])
        if labels and not boxes:
            return (
                [ValidationWarning(code="MISSING_BOUNDING_BOX", message="Labels found but no bounding boxes drawn.", severity="warning")],
                ["Draw bounding boxes around the objects you labeled for higher accuracy."],
                -0.20,
            )
        return [], [], 0.0

    def _check_word_count(self, payload, completion_time, history) -> Tuple:
        text = payload.get("text", "")
        audio_len = payload.get("audio_length_seconds", 30)
        words = len(text.split())
        # Very rough estimate: avg ~130 WPM in audio
        expected_min_words = max(1, int(audio_len / 60 * 130 * 0.4))  # 40% of max as floor

        if words < 2:
            return (
                [ValidationWarning(code="EMPTY_TRANSCRIPTION", message="Transcription appears empty or too short.", severity="error")],
                ["Transcribe all audible speech in the audio clip."],
                -1.0,
            )
        if words < expected_min_words:
            return (
                [ValidationWarning(code="SHORT_TRANSCRIPTION", message=f"Transcription may be incomplete ({words} words for {audio_len:.0f}s audio).", severity="warning")],
                ["Listen to the full audio clip and transcribe all speech."],
                -0.15,
            )
        return [], [], 0.0

    def _check_punctuation_density(self, payload, completion_time, history) -> Tuple:
        text = payload.get("text", "")
        if len(text) < 20:
            return [], [], 0.0
        punct_chars = sum(1 for c in text if c in ".!?,;:")
        punct_ratio = punct_chars / max(len(text), 1)
        if punct_ratio < 0.01:
            return (
                [],
                ["Add punctuation (periods, commas) to make the transcription easier to read."],
                -0.05,
            )
        return [], [], 0.0

    def _check_straight_lining(self, payload, completion_time, history) -> Tuple:
        responses = payload.get("responses", {})
        if not isinstance(responses, dict) or len(responses) < 4:
            return [], [], 0.0
        unique_answers = set(str(v) for v in responses.values())
        if len(unique_answers) == 1:
            return (
                [ValidationWarning(
                    code="STRAIGHT_LINE",
                    message="All survey responses are identical — this is a common pattern of low-quality data.",
                    severity="warning",
                )],
                [
                    "Read each question carefully before answering.",
                    "Identical answers to different questions are flagged as low quality.",
                ],
                -0.25,
            )
        return [], [], 0.0

    def _check_min_survey_length(self, payload, completion_time, history) -> Tuple:
        responses = payload.get("responses", {})
        if len(responses) < 1:
            return (
                [ValidationWarning(code="NO_RESPONSES", message="No survey responses found.", severity="error")],
                ["Complete all required questions."],
                -1.0,
            )
        return [], [], 0.0

    def _check_moderation_verdict(self, payload, completion_time, history) -> Tuple:
        verdict = payload.get("verdict", "")
        if not verdict:
            return (
                [ValidationWarning(code="NO_VERDICT", message="A verdict (safe/unsafe/review) is required.", severity="error")],
                ["Provide a verdict for this moderation task."],
                -1.0,
            )
        if verdict not in ("safe", "unsafe", "review"):
            return (
                [ValidationWarning(code="INVALID_VERDICT", message=f"Invalid verdict '{verdict}'. Must be: safe | unsafe | review.", severity="error")],
                [],
                -1.0,
            )
        return [], [], 0.0


# ─── ML Score Hook (plug your model here) ─────────────────────────────────────

async def ml_quality_hook(
    task_type: str,
    payload: dict,
    base_score: float,
    worker_history: dict,
) -> float:
    """
    Replace this stub with a real model inference call.

    Examples:
      - POST to a FastAPI model server:
          resp = await httpx.AsyncClient().post(MODEL_URL, json={...})
          return resp.json()["score"]

      - AWS SageMaker:
          boto3.client("sagemaker-runtime").invoke_endpoint(...)

      - Modal / Replicate:
          await modal_fn.remote(payload)

    Returns a 0.0–1.0 adjusted quality score.
    """
    # TODO: Replace with actual model call
    return base_score


# Singleton
rule_engine = RuleEngine()
