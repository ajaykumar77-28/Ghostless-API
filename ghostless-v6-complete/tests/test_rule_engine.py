"""
Unit tests for the validation rule engine (services/rule_engine.py).

Tests cover:
  - Speed flag / warning thresholds
  - Image label rules (min labels, bounding boxes, confidence spread)
  - Transcription rules (word count, punctuation)
  - Survey rules (straight-lining, min responses)
  - Moderation rules (verdict validation)
  - Score clamping
  - Trust adjustment integration
"""
import pytest
from app.services.rule_engine import RuleEngine, RuleResult

engine = RuleEngine()

HISTORY_NORMAL = {"avg_completion_time": 120.0, "trust_score": 50}
HISTORY_HIGH_TRUST = {"avg_completion_time": 120.0, "trust_score": 80}
HISTORY_LOW_TRUST  = {"avg_completion_time": 120.0, "trust_score": 20}


def run(task_type: str, payload: dict, completion_time: float, history: dict = None) -> RuleResult:
    return engine.run(task_type, payload, completion_time, history or HISTORY_NORMAL)


# ─── Speed checks ─────────────────────────────────────────────────────────────

class TestSpeedChecks:
    def test_normal_speed_no_flag(self):
        result = run("default", {}, 120.0)
        assert not any(w.code == "SPEED_FLAG" for w in result.warnings)

    def test_speed_flag_very_fast(self):
        result = run("default", {}, 5.0)   # << 15% of 120s avg
        assert any(w.code == "SPEED_FLAG" for w in result.warnings)
        assert result.quality_score < 0.6   # penalised

    def test_speed_warning_moderately_fast(self):
        result = run("default", {}, 35.0)   # ~29% of avg, < 35%
        assert any(w.code == "SPEED_WARNING" for w in result.warnings)

    def test_speed_flag_in_flags_list(self):
        result = run("default", {}, 5.0)
        assert "SPEED_FLAG" in result.flags

    def test_slow_submission_no_penalty(self):
        result = run("default", {}, 300.0)
        assert all(w.code != "SPEED_FLAG" for w in result.warnings)


# ─── Image label rules ────────────────────────────────────────────────────────

class TestImageLabelRules:
    def test_no_labels_error(self):
        result = run("image_label", {}, 60.0)
        assert any(w.code == "NO_LABELS" for w in result.warnings)
        assert result.quality_score == pytest.approx(0.0, abs=0.05)

    def test_valid_labels_no_error(self):
        result = run("image_label", {"labels": ["cat", "dog"]}, 60.0)
        assert not any(w.code == "NO_LABELS" for w in result.warnings)

    def test_too_many_labels_warning(self):
        result = run("image_label", {"labels": list(range(25))}, 60.0)
        assert any(w.code == "TOO_MANY_LABELS" for w in result.warnings)

    def test_missing_bounding_box_warning(self):
        result = run("image_label", {"labels": ["cat"]}, 60.0)
        assert any(w.code == "MISSING_BOUNDING_BOX" for w in result.warnings)

    def test_bounding_boxes_with_labels_ok(self):
        result = run("image_label", {"labels": ["cat"], "bounding_boxes": [{"x": 0, "y": 0}]}, 60.0)
        assert not any(w.code == "MISSING_BOUNDING_BOX" for w in result.warnings)


# ─── Transcription rules ──────────────────────────────────────────────────────

class TestTranscriptionRules:
    def test_empty_transcription_error(self):
        result = run("transcription", {"text": "", "audio_length_seconds": 30}, 45.0)
        assert any(w.code == "EMPTY_TRANSCRIPTION" for w in result.warnings)

    def test_short_transcription_warning(self):
        # 30s audio, expected ~26 words min; submit only 3
        result = run("transcription", {"text": "hello world", "audio_length_seconds": 30}, 45.0)
        assert any(w.code == "SHORT_TRANSCRIPTION" for w in result.warnings)

    def test_adequate_transcription_no_warning(self):
        text = " ".join(["word"] * 50)
        result = run("transcription", {"text": text, "audio_length_seconds": 30}, 45.0)
        assert not any(w.code in ("EMPTY_TRANSCRIPTION", "SHORT_TRANSCRIPTION") for w in result.warnings)

    def test_no_punctuation_suggestion(self):
        result = run("transcription", {"text": "hello world this is a test with no punctuation anywhere"},  45.0)
        # Should get a suggestion but no error
        assert result.quality_score > 0.5


# ─── Survey rules ─────────────────────────────────────────────────────────────

class TestSurveyRules:
    def test_straight_lining_flagged(self):
        responses = {f"q{i}": "3" for i in range(10)}
        result = run("survey", {"responses": responses}, 60.0)
        assert any(w.code == "STRAIGHT_LINE" for w in result.warnings)
        assert result.quality_score < 0.8

    def test_varied_responses_ok(self):
        responses = {"q1": "1", "q2": "3", "q3": "5", "q4": "2", "q5": "4"}
        result = run("survey", {"responses": responses}, 60.0)
        assert not any(w.code == "STRAIGHT_LINE" for w in result.warnings)

    def test_no_responses_error(self):
        result = run("survey", {"responses": {}}, 60.0)
        assert any(w.code == "NO_RESPONSES" for w in result.warnings)


# ─── Moderation rules ─────────────────────────────────────────────────────────

class TestModerationRules:
    def test_no_verdict_error(self):
        result = run("moderation", {}, 30.0)
        assert any(w.code == "NO_VERDICT" for w in result.warnings)

    def test_invalid_verdict_error(self):
        result = run("moderation", {"verdict": "maybe"}, 30.0)
        assert any(w.code == "INVALID_VERDICT" for w in result.warnings)

    def test_valid_verdicts(self):
        for verdict in ("safe", "unsafe", "review"):
            result = run("moderation", {"verdict": verdict}, 30.0)
            assert not any(w.code in ("NO_VERDICT", "INVALID_VERDICT") for w in result.warnings)


# ─── Trust adjustment ─────────────────────────────────────────────────────────

class TestTrustAdjustment:
    def test_high_trust_boosts_score(self):
        low  = run("default", {}, 120.0, HISTORY_LOW_TRUST)
        high = run("default", {}, 120.0, HISTORY_HIGH_TRUST)
        assert high.quality_score > low.quality_score

    def test_score_bounded_zero_to_one(self):
        result = run("default", {}, 5.0)   # worst case: speed flag
        assert 0.0 <= result.quality_score <= 1.0
        result2 = run("default", {}, 120.0, HISTORY_HIGH_TRUST)
        assert 0.0 <= result2.quality_score <= 1.0

    def test_unknown_task_type_uses_default_rules(self):
        result = run("unknown_task_xyz", {}, 120.0)
        assert isinstance(result, RuleResult)
        assert result.quality_score > 0.0
