"""Tests for core.quality_gate.QualityGate."""

from __future__ import annotations

from core.quality_gate import QualityGate


class TestCheckNgWords:
    """check_ng_words should detect NG words in content."""

    def test_check_ng_words_found(self, quality_gate: QualityGate) -> None:
        # "シミが消える" is in ng_words.txt
        content = "この化粧水を使えばシミが消えるらしい"
        found = quality_gate.check_ng_words(content)
        assert len(found) > 0, "NG words should be detected in content containing a known NG word"
        assert "シミが消える" in found, "Expected 'シミが消える' to be detected"

    def test_check_ng_words_clean(self, quality_gate: QualityGate) -> None:
        content = "朝の洗顔後にしっかり保湿することが大切です"
        found = quality_gate.check_ng_words(content)
        assert found == [], f"Clean text should have no NG words, got: {found}"


class TestCheckSimilarity:
    """check_similarity should return appropriate similarity scores."""

    def test_check_similarity_high(self, quality_gate: QualityGate) -> None:
        content = "朝のスキンケアは洗顔と保湿が大切です"
        history = [
            "朝のスキンケアは洗顔と保湿が大切です",  # identical
        ]
        score = quality_gate.check_similarity(content, history)
        assert score >= 0.9, f"Identical texts should have similarity >= 0.9, got {score:.3f}"

    def test_check_similarity_low(self, quality_gate: QualityGate) -> None:
        content = "朝のスキンケアは洗顔と保湿が大切です"
        history = [
            "Python is a programming language used for web development",
        ]
        score = quality_gate.check_similarity(content, history)
        assert score < 0.3, f"Unrelated texts should have similarity < 0.3, got {score:.3f}"

    def test_check_similarity_empty_history(self, quality_gate: QualityGate) -> None:
        score = quality_gate.check_similarity("any text", [])
        assert score == 0.0, "Empty history should return 0.0 similarity"


class TestCheckPatternRotation:
    """check_pattern_rotation should enforce pattern diversity."""

    def test_check_pattern_rotation_blocked(self, quality_gate: QualityGate) -> None:
        recent = ["tips", "myth_bust", "routine"]
        # "tips" is in the recent 3 — should be blocked (return False)
        result = quality_gate.check_pattern_rotation("tips", recent, block_count=3)
        assert result is False, "Pattern found in recent window should be blocked (return False)"

    def test_check_pattern_rotation_ok(self, quality_gate: QualityGate) -> None:
        recent = ["tips", "myth_bust", "routine"]
        # "ingredient" is NOT in the recent 3 — safe to use (return True)
        result = quality_gate.check_pattern_rotation("ingredient", recent, block_count=3)
        assert result is True, "Pattern not in recent window should be allowed (return True)"


class TestValidatePass:
    """validate should return passed=True when all checks pass."""

    def test_validate_pass(self, quality_gate: QualityGate) -> None:
        content = "朝の洗顔後にしっかり保湿することが大切です"
        pattern = "ingredient"
        post_history = [
            {"content": "日焼け止めは年中必須のアイテムです", "pattern": "tips"},
            {"content": "セラミド配合の化粧水がおすすめ", "pattern": "routine"},
        ]
        result = quality_gate.validate(content, pattern, post_history)
        assert result["passed"] is True, f"All checks should pass, got reason={result['reason']}"
        assert result["reason"] == "ok"
        assert result["ng_words"] == []


class TestValidateFailNgWord:
    """validate should fail when NG words are present."""

    def test_validate_fail_ng_word(self, quality_gate: QualityGate) -> None:
        content = "この化粧水でシミが消える効果があります"
        pattern = "tips"
        post_history: list[dict] = []
        result = quality_gate.validate(content, pattern, post_history)
        assert result["passed"] is False, "Should fail when NG words are detected"
        assert "ng_level1_block" in result["reason"], (
            f"Reason should contain ng_level1_block, got: {result['reason']}"
        )
        assert len(result["ng_words"]) > 0, "ng_words list should not be empty"


class TestCheckDebateNg:
    """check_debate_ng should catch debate-specific forbidden expressions."""

    def test_detects_ng_statement(self, quality_gate: QualityGate) -> None:
        ng = ["無添加を選ぶ人はバカ", "パラベンは完全に安全"]
        content = "パラベンは完全に安全だから気にしなくていい"
        result = quality_gate.check_debate_ng(content, ng)
        assert result["passed"] is False
        assert "debate_ng" in result["reason"]

    def test_passes_clean_content(self, quality_gate: QualityGate) -> None:
        ng = ["無添加を選ぶ人はバカ", "パラベンは完全に安全"]
        content = "無添加の定義は実は曖昧で、成分表を確認するのが大事"
        result = quality_gate.check_debate_ng(content, ng)
        assert result["passed"] is True

    def test_passes_empty_ng_list(self, quality_gate: QualityGate) -> None:
        result = quality_gate.check_debate_ng("何でも書いてOK", [])
        assert result["passed"] is True


class TestCheckBrandNames:
    """check_brand_names should detect brand/product names in content."""

    def test_detects_brand_name(self, quality_gate: QualityGate) -> None:
        content = "最近SK-IIの化粧水が話題だけど成分的にはどうなの？"
        hits = quality_gate.check_brand_names(content)
        assert "SK-II" in hits, f"Expected 'SK-II' to be detected, got: {hits}"

    def test_detects_multiple_brands(self, quality_gate: QualityGate) -> None:
        content = "キュレルとミノンを比べてみた"
        hits = quality_gate.check_brand_names(content)
        assert "キュレル" in hits
        assert "ミノン" in hits

    def test_clean_content_no_brand(self, quality_gate: QualityGate) -> None:
        content = "セラミドは保湿に大切な成分です"
        hits = quality_gate.check_brand_names(content)
        assert hits == [], f"Clean content should have no brand hits, got: {hits}"

    def test_empty_content_no_brand(self, quality_gate: QualityGate) -> None:
        hits = quality_gate.check_brand_names("")
        assert hits == [], "Empty content should have no brand hits"

    def test_short_ascii_no_false_positive(self, quality_gate: QualityGate) -> None:
        # "VT" is a brand name but should NOT match inside "invite" or "EVENT"
        content = "今日のイベント(EVENT)にinviteされた"
        hits = quality_gate.check_brand_names(content)
        assert "VT" not in hits, f"'VT' should not match inside other words, got: {hits}"

    def test_short_ascii_matches_standalone(self, quality_gate: QualityGate) -> None:
        content = "VT のシカクリームが人気"
        hits = quality_gate.check_brand_names(content)
        assert "VT" in hits, f"'VT' as standalone word should be detected, got: {hits}"

    def test_short_ascii_matches_adjacent_japanese(self, quality_gate: QualityGate) -> None:
        # "DHCの化粧水" — ASCII brand followed by Japanese particle
        content = "今日はDHCの化粧水を試した"
        hits = quality_gate.check_brand_names(content)
        assert "DHC" in hits, f"'DHC' adjacent to Japanese chars should match, got: {hits}"

    def test_brand_names_not_in_ng_v2(self, quality_gate: QualityGate) -> None:
        # After dedup fix: check_ng_words_v2 should NOT include brand names
        content = "ランコムの美容液に入ってる成分がすごい"
        result = quality_gate.check_ng_words_v2(content)
        assert "ランコム" not in result.level2_hits, (
            "Brand names should only appear via validate(), not in check_ng_words_v2()"
        )


class TestValidateBrandWarning:
    """validate should set review_required when brand names are found."""

    def test_validate_brand_review_required(self, quality_gate: QualityGate) -> None:
        content = "無印良品の化粧水の成分を調べてみた"
        pattern = "ingredient"
        post_history: list[dict] = []
        result = quality_gate.validate(content, pattern, post_history)
        assert result["passed"] is True, "Brand names should NOT block, just flag"
        assert result.get("review_required") is True
        assert "無印良品" in result["ng_check"]["brand_hits"]

    def test_validate_level1_ng_blocks_before_brand_check(self, quality_gate: QualityGate) -> None:
        # Level 1 NG should cause early return before brand check runs
        content = "SK-IIを使えばシミが消えるらしい"
        pattern = "tips"
        post_history: list[dict] = []
        result = quality_gate.validate(content, pattern, post_history)
        assert result["passed"] is False, "Level 1 NG should block"
        assert "ng_level1_block" in result["reason"]
        # brand_hits should NOT be present (early return before step 1.5)
        assert "brand_hits" not in result["ng_check"]


class TestCheckHookSimilarity:
    """check_hook_similarity should compare only the first line of posts."""

    def test_identical_hooks_high_score(self, quality_gate: QualityGate) -> None:
        content = "セラミドって知ってる？\n実は3種類あるんです"
        history = [
            "セラミドって知ってる？\n全然違う本文",
        ]
        score = quality_gate.check_hook_similarity(content, history)
        assert score >= 0.8, f"Identical hooks should score high, got {score:.3f}"

    def test_different_hooks_low_score(self, quality_gate: QualityGate) -> None:
        content = "セラミドって知ってる？\n実は3種類あるんです"
        history = [
            "朝の洗顔後にしっかり保湿することが大切です\nセラミドの話",
        ]
        score = quality_gate.check_hook_similarity(content, history)
        assert score < 0.75, f"Different hooks should score low, got {score:.3f}"

    def test_empty_history(self, quality_gate: QualityGate) -> None:
        score = quality_gate.check_hook_similarity("何か投稿", [])
        assert score == 0.0

    def test_extract_hook_skips_blank_lines(self, quality_gate: QualityGate) -> None:
        hook = quality_gate._extract_hook("\n\n  \nこれが1行目\n2行目")
        assert hook == "これが1行目"


class TestValidateHookSimilarity:
    """validate should fail when hook similarity is too high."""

    def test_validate_fail_hook_similarity(self, quality_gate: QualityGate) -> None:
        content = "朝の洗顔後にしっかり保湿することが大切です\n以下は全く違う本文"
        pattern = "tips"
        post_history = [
            {
                "content": "朝の洗顔後にしっかり保湿することが大切です\n別の話題の本文",
                "pattern": "ingredient",
            },
        ]
        result = quality_gate.validate(content, pattern, post_history)
        # Full similarity may pass (different body), but hook similarity should catch it
        assert result.get("hook_similarity_score", 0) > 0


class TestValidateFailSimilarity:
    """validate should fail when similarity is too high."""

    def test_validate_fail_similarity(self, quality_gate: QualityGate) -> None:
        content = "朝の洗顔後にしっかり保湿することが大切です"
        pattern = "ingredient"
        # Use an identical post in history to guarantee high similarity
        post_history = [
            {"content": "朝の洗顔後にしっかり保湿することが大切です", "pattern": "tips"},
        ]
        result = quality_gate.validate(content, pattern, post_history)
        assert result["passed"] is False, "Should fail when similarity is too high"
        assert "similarity_too_high" in result["reason"], (
            f"Reason should contain similarity_too_high, got: {result['reason']}"
        )
        assert result["similarity_score"] >= 0.85, (
            f"Score should be >= 0.85 for identical content, got {result['similarity_score']:.3f}"
        )
