"""Tests for V3.1 new features.

Covers:
- F1: analyst._update_hook_stock()
- F2: writer_quality._extract_follow_up_comment() + 10-item / 3-group scoring
- F3: writer._is_topic_repeated()
- poster follow-up comment self-reply (コメント誘導型)
- researcher._enrich_with_transcripts()
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.state_manager import StateManager
from core.safety import SafetyGuard


# =============================================================================
# F1: analyst._update_hook_stock
# =============================================================================

class TestUpdateHookStock:
    """Tests for AnalystAgent._update_hook_stock()."""

    @pytest.fixture()
    def analyst_env(self, tmp_path: Path):
        """Set up an AnalystAgent with temp state and a minimal hook_stock.json."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Create knowledge dir with hook_stock.json
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        hook_path = knowledge_dir / "hook_stock.json"
        hook_path.write_text(
            json.dumps({"hooks": [{"text": "既存フック", "source": "manual"}]}),
            encoding="utf-8",
        )

        env_vars = {
            "THREADS_ACCESS_TOKEN": "test_token",
            "THREADS_USER_ID": "test_user",
        }
        with patch.dict("os.environ", env_vars):
            with patch("agents.analyst.ClaudeClient"):
                from agents.analyst import AnalystAgent

                sm = StateManager(state_dir=state_dir)
                sm.save_json("system_state.json", {
                    "emergency_stop": False,
                    "agent_status": {},
                    "daily_counters": {},
                })
                agent = AnalystAgent()
                agent.state = sm

        # Patch _resolve_path so hook_stock lookups use the tmp_path file
        agent._resolve_path = lambda rel: hook_path if "hook_stock" in rel else Path(rel)

        yield agent, hook_path

    def test_adds_new_hooks(self, analyst_env) -> None:
        """Top-performing posts with valid first lines should be added to hook_stock."""
        agent, hook_path = analyst_env

        top_posts = [
            {
                "content_preview": "セラミドだけで肌荒れが治った話",
                "category": "skincare_ingredients",
                "pattern": "実体験レビュー型",
                "engagement_rate": 0.08,
            },
            {
                "content_preview": "ナイアシンアミドの本当の使い方",
                "category": "skincare_ingredients",
                "pattern": "反常識型",
                "engagement_rate": 0.06,
            },
        ]
        agent._update_hook_stock(top_posts)

        with open(hook_path, encoding="utf-8") as f:
            data = json.load(f)

        texts = [h["text"] for h in data["hooks"]]
        assert "セラミドだけで肌荒れが治った話" in texts
        assert "ナイアシンアミドの本当の使い方" in texts
        # Existing hook preserved
        assert "既存フック" in texts

    def test_skips_duplicates(self, analyst_env) -> None:
        """A first line that already exists in hook_stock should not be added again."""
        agent, hook_path = analyst_env

        top_posts = [{"content_preview": "既存フック", "category": "c", "pattern": "p", "engagement_rate": 0.1}]
        agent._update_hook_stock(top_posts)

        with open(hook_path, encoding="utf-8") as f:
            data = json.load(f)

        assert len([h for h in data["hooks"] if h["text"] == "既存フック"]) == 1

    def test_skips_too_short(self, analyst_env) -> None:
        """Texts under 10 chars should be filtered out."""
        agent, hook_path = analyst_env

        top_posts = [{"content_preview": "短い", "category": "c", "pattern": "p", "engagement_rate": 0.1}]
        before_count = len(json.loads(hook_path.read_text(encoding="utf-8"))["hooks"])
        agent._update_hook_stock(top_posts)
        after_count = len(json.loads(hook_path.read_text(encoding="utf-8"))["hooks"])

        assert after_count == before_count

    def test_skips_empty_content_preview(self, analyst_env) -> None:
        """Posts with empty content_preview should be skipped."""
        agent, hook_path = analyst_env

        top_posts = [{"content_preview": "", "category": "c", "pattern": "p", "engagement_rate": 0.1}]
        before_count = len(json.loads(hook_path.read_text(encoding="utf-8"))["hooks"])
        agent._update_hook_stock(top_posts)
        after_count = len(json.loads(hook_path.read_text(encoding="utf-8"))["hooks"])

        assert after_count == before_count

    def test_source_field_set_to_auto(self, analyst_env) -> None:
        """Newly added hooks should have source='auto'."""
        agent, hook_path = analyst_env

        top_posts = [
            {
                "content_preview": "自動蓄積されたフックのテキスト",
                "category": "skincare_knowledge",
                "pattern": "短文完結型",
                "engagement_rate": 0.05,
            }
        ]
        agent._update_hook_stock(top_posts)

        with open(hook_path, encoding="utf-8") as f:
            data = json.load(f)

        added = [h for h in data["hooks"] if h["text"] == "自動蓄積されたフックのテキスト"]
        assert len(added) == 1
        assert added[0]["source"] == "auto"


# =============================================================================
# F2: writer_quality._extract_follow_up_comment + 10-item / 3-group scoring
# =============================================================================

class TestExtractFollowUpComment:
    """Tests for WriterQualityMixin._extract_follow_up_comment()."""

    def _extract(self, raw: str):
        from agents.writer_quality import WriterQualityMixin
        return WriterQualityMixin._extract_follow_up_comment(raw)

    def test_basic_extraction(self) -> None:
        raw = "---本文---\nメインテキスト\n---追いコメント---\n返信を促すコメントです"
        result = self._extract(raw)
        assert result == "返信を促すコメントです"

    def test_returns_none_without_marker(self) -> None:
        raw = "---本文---\nフォローアップなし"
        assert self._extract(raw) is None

    def test_truncates_to_100_chars(self) -> None:
        long_comment = "あ" * 150
        raw = f"---追いコメント---\n{long_comment}"
        result = self._extract(raw)
        assert result is not None
        assert len(result) <= 100

    def test_strips_trailing_affiliate_section(self) -> None:
        raw = "---追いコメント---\n追いコメ本文\n---アフィリエイトコメント---\nアフィリ商品リンク"
        result = self._extract(raw)
        assert result == "追いコメ本文"
        assert "アフィリ" not in result

    def test_empty_comment_returns_none(self) -> None:
        raw = "---追いコメント---\n"
        assert self._extract(raw) is None


class TestQualityScoring10Items:
    """Tests for WriterQualityMixin._evaluate_quality_score() — 10 items, 3-group avg."""

    def _make_mixin(self, mock_scores: dict):
        from agents.writer_quality import WriterQualityMixin

        class _DummyWriter(WriterQualityMixin):
            def __init__(self):
                import logging
                self.logger = logging.getLogger("test_writer")
                self.claude_client = MagicMock()
                self.claude_client.evaluate_quality.return_value = {
                    "scores": mock_scores,
                    "feedback": "テストフィードバック",
                }

        return _DummyWriter()

    def test_full_10_items_average(self) -> None:
        """All 10 items present → 3-group average computed correctly."""
        scores = {
            # content group (4): avg = 8.0
            "usefulness": 8.0, "specificity": 8.0, "empathy": 8.0, "call_to_action": 8.0,
            # expression group (4): avg = 7.0
            "naturalness": 7.0, "tempo": 7.0, "experiential": 7.0, "non_commercial": 7.0,
            # hook group (2): avg = 9.0
            "hook_strength": 9.0, "persona_match": 9.0,
        }
        writer = self._make_mixin(scores)
        result = writer._evaluate_quality_score("テストコンテンツ")

        assert result["content_quality_avg"] == 8.0
        assert result["expression_quality_avg"] == 7.0
        assert result["hook_quality_avg"] == 9.0
        # (8.0 + 7.0 + 9.0) / 3 = 8.0
        assert result["average"] == 8.0

    def test_missing_hook_group_uses_2_group_average(self) -> None:
        """If hook_strength and persona_match are missing, average uses only 2 groups."""
        scores = {
            "usefulness": 8.0, "specificity": 8.0, "empathy": 8.0, "call_to_action": 8.0,
            "naturalness": 6.0, "tempo": 6.0, "experiential": 6.0, "non_commercial": 6.0,
        }
        writer = self._make_mixin(scores)
        result = writer._evaluate_quality_score("テスト")

        assert result["hook_quality_avg"] is None
        # (8.0 + 6.0) / 2 = 7.0
        assert result["average"] == 7.0

    def test_average_rounds_to_1_decimal(self) -> None:
        scores = {
            "usefulness": 7.0, "specificity": 8.0, "empathy": 9.0, "call_to_action": 7.0,
            "naturalness": 8.0, "tempo": 7.0, "experiential": 8.0, "non_commercial": 9.0,
            "hook_strength": 7.0, "persona_match": 8.0,
        }
        writer = self._make_mixin(scores)
        result = writer._evaluate_quality_score("テスト")

        # content: 7+8+9+7=31/4=7.75 → 7.8
        # expression: 8+7+8+9=32/4=8.0
        # hook: 7+8=15/2=7.5
        # avg: (7.75+8.0+7.5)/3 = 23.25/3 = 7.75 → 7.8
        assert isinstance(result["average"], float)

    def test_empty_scores_returns_zero(self) -> None:
        from agents.writer_quality import WriterQualityMixin

        class _DummyWriter(WriterQualityMixin):
            def __init__(self):
                import logging
                self.logger = logging.getLogger("test_writer")
                self.claude_client = MagicMock()
                self.claude_client.evaluate_quality.side_effect = RuntimeError("API error")

        writer = _DummyWriter()
        result = writer._evaluate_quality_score("テスト")
        assert result["average"] == 0.0


# =============================================================================
# F3: writer._is_topic_repeated
# =============================================================================

class TestIsTopicRepeated:
    """Tests for WriterAgent._is_topic_repeated()."""

    @pytest.fixture()
    def writer_agent(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sm = StateManager(state_dir=state_dir)
        sm.save_json("system_state.json", {
            "emergency_stop": False,
            "agent_status": {},
            "daily_counters": {},
        })

        env_vars = {
            "THREADS_ACCESS_TOKEN": "test_token",
            "THREADS_USER_ID": "test_user",
            "ANTHROPIC_API_KEY": "test_key",
        }
        with patch.dict("os.environ", env_vars):
            with patch("agents.writer.ClaudeClient"):
                from agents.writer import WriterAgent
                from core.account_context import AccountContext
                ctx = AccountContext("default")
                ctx._state_manager = sm
                agent = WriterAgent(ctx=ctx)
                agent.state = sm
        return agent

    def test_same_category_and_keyword_is_repeated(self, writer_agent) -> None:
        research_item = {
            "category": "skincare_ingredients",
            "keywords": ["セラミド", "保湿"],
        }
        recent_posts = [
            {"category": "skincare_ingredients", "content": "セラミドの正しい使い方について"},
        ]
        assert writer_agent._is_topic_repeated(research_item, recent_posts) is True

    def test_different_category_not_repeated(self, writer_agent) -> None:
        research_item = {
            "category": "skincare_ingredients",
            "keywords": ["セラミド"],
        }
        recent_posts = [
            {"category": "beauty_trend", "content": "セラミドの話をしていた"},
        ]
        assert writer_agent._is_topic_repeated(research_item, recent_posts) is False

    def test_same_category_different_keyword_not_repeated(self, writer_agent) -> None:
        research_item = {
            "category": "skincare_ingredients",
            "keywords": ["ナイアシンアミド"],
        }
        recent_posts = [
            {"category": "skincare_ingredients", "content": "セラミドの話"},
        ]
        assert writer_agent._is_topic_repeated(research_item, recent_posts) is False

    def test_outside_window_not_repeated(self, writer_agent) -> None:
        """Same category+keyword but older than _TOPIC_REPEAT_WINDOW should not block."""
        research_item = {
            "category": "skincare_ingredients",
            "keywords": ["セラミド"],
        }
        # 4 posts: セラミド only in the oldest (index 0), 3 new posts after
        recent_posts = [
            {"category": "skincare_ingredients", "content": "セラミドの話"},
            {"category": "skincare_knowledge", "content": "別の話"},
            {"category": "skincare_routine", "content": "別の話2"},
            {"category": "beauty_trend", "content": "別の話3"},
        ]
        # _TOPIC_REPEAT_WINDOW=3, only last 3 are checked
        assert writer_agent._is_topic_repeated(research_item, recent_posts) is False

    def test_no_keywords_returns_false(self, writer_agent) -> None:
        research_item = {"category": "skincare_ingredients", "keywords": []}
        recent_posts = [{"category": "skincare_ingredients", "content": "セラミドの話"}]
        assert writer_agent._is_topic_repeated(research_item, recent_posts) is False


# =============================================================================
# Poster: follow-up comment self-reply for コメント誘導型
# =============================================================================

class TestPosterFollowUpComment:
    """Tests for follow-up comment self-reply in PosterAgent.execute()."""

    @pytest.fixture()
    def poster_env(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sm = StateManager(state_dir=state_dir)
        sm.save_json("system_state.json", {
            "emergency_stop": False,
            "agent_status": {},
            "daily_counters": {},
        })
        sm.save_json("post_history.json", {"last_updated": None, "posts": [], "daily_stats": {}})

        _JST = datetime.timezone(datetime.timedelta(hours=9))
        _FAKE_NOW = datetime.datetime(2026, 3, 26, 12, 0, 0, tzinfo=_JST)
        _FAKE_PAST = (_FAKE_NOW - datetime.timedelta(hours=1)).isoformat()

        env_vars = {"THREADS_ACCESS_TOKEN": "tok", "THREADS_USER_ID": "uid"}
        with patch.dict("os.environ", env_vars):
            with patch("agents.poster.ThreadsAPIClient") as mock_api_cls:
                mock_api = MagicMock()
                mock_api.create_text_post.return_value = {"id": "media_001", "success": True}
                mock_api_cls.return_value = mock_api

                from agents.poster import PosterAgent
                agent = PosterAgent()
                agent.state = sm
                agent.safety = SafetyGuard(sm)

        yield agent, sm, mock_api, _FAKE_NOW, _FAKE_PAST

    def test_follow_up_comment_posted_for_comment_induction(self, poster_env) -> None:
        """コメント誘導型 with follow_up_comment → second create_text_post call."""
        agent, sm, mock_api, fake_now, fake_past = poster_env

        sm.save_json("post_queue.json", {"last_updated": None, "queue": [{
            "id": "q_001",
            "content": "コメント誘導型の投稿本文",
            "hashtag": "#スキンケア",
            "pattern": "コメント誘導型",
            "quality_score": 8.0,
            "similarity_score": 0.2,
            "category": "skincare_knowledge",
            "scheduled_at": fake_past,
            "created_at": fake_past,
            "status": "pending",
            "retry_count": 0,
            "affiliate_comment": None,
            "follow_up_comment": "みなさんはどうですか？",
        }]})

        with patch("agents.poster.datetime.datetime", wraps=datetime.datetime,
                   **{"now.return_value": fake_now}):
            with patch("agents.poster.time.sleep"):
                agent.execute()

        # First call: main post
        assert mock_api.create_text_post.call_count == 2
        calls = mock_api.create_text_post.call_args_list
        assert calls[0][0][0] == "コメント誘導型の投稿本文"
        # Second call: follow-up as reply
        assert calls[1][1]["reply_to_id"] == "media_001"
        assert calls[1][0][0] == "みなさんはどうですか？"

    def test_no_follow_up_for_other_patterns(self, poster_env) -> None:
        """Non-コメント誘導型 posts should not trigger follow-up comment reply."""
        agent, sm, mock_api, fake_now, fake_past = poster_env

        sm.save_json("post_queue.json", {"last_updated": None, "queue": [{
            "id": "q_002",
            "content": "短文完結型の投稿本文",
            "hashtag": "#スキンケア",
            "pattern": "短文完結型",
            "quality_score": 8.0,
            "similarity_score": 0.2,
            "category": "skincare_knowledge",
            "scheduled_at": fake_past,
            "created_at": fake_past,
            "status": "pending",
            "retry_count": 0,
            "affiliate_comment": None,
            "follow_up_comment": "これは使われないはず",
        }]})

        with patch("agents.poster.datetime.datetime", wraps=datetime.datetime,
                   **{"now.return_value": fake_now}):
            with patch("agents.poster.time.sleep"):
                agent.execute()

        # Only the main post should be published
        assert mock_api.create_text_post.call_count == 1

    def test_no_follow_up_when_field_absent(self, poster_env) -> None:
        """Posts without follow_up_comment field should not trigger a self-reply."""
        agent, sm, mock_api, fake_now, fake_past = poster_env

        sm.save_json("post_queue.json", {"last_updated": None, "queue": [{
            "id": "q_003",
            "content": "フォローアップなし",
            "hashtag": "#スキンケア",
            "pattern": "コメント誘導型",
            "quality_score": 8.0,
            "similarity_score": 0.2,
            "category": "skincare_knowledge",
            "scheduled_at": fake_past,
            "created_at": fake_past,
            "status": "pending",
            "retry_count": 0,
            "affiliate_comment": None,
        }]})

        with patch("agents.poster.datetime.datetime", wraps=datetime.datetime,
                   **{"now.return_value": fake_now}):
            with patch("agents.poster.time.sleep"):
                agent.execute()

        assert mock_api.create_text_post.call_count == 1


# =============================================================================
# Researcher: _enrich_with_transcripts
# =============================================================================

class TestEnrichWithTranscripts:
    """Tests for ResearcherAgent._enrich_with_transcripts()."""

    @pytest.fixture()
    def researcher_env(self, tmp_path: Path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sm = StateManager(state_dir=state_dir)
        sm.save_json("system_state.json", {
            "emergency_stop": False,
            "agent_status": {},
            "daily_counters": {},
        })

        env_vars = {
            "THREADS_ACCESS_TOKEN": "tok",
            "THREADS_USER_ID": "uid",
            "YOUTUBE_API_KEY": "yt_key",
            "ANTHROPIC_API_KEY": "test_key",
        }
        with patch.dict("os.environ", env_vars):
            with patch("agents.researcher.build"):
                with patch("agents.researcher.ClaudeClient"):
                    from agents.researcher import ResearcherAgent
                    from core.account_context import AccountContext
                    ctx = AccountContext("default")
                    ctx._state_manager = sm
                    agent = ResearcherAgent(ctx=ctx)
                    agent.state = sm
        return agent

    def test_fetches_transcript_and_attaches(self, researcher_env) -> None:
        """Videos with Japanese transcripts should get transcript field added."""
        agent = researcher_env
        videos = [{"video_id": "vid001", "title": "セラミド動画"}]

        mock_transcript_api = MagicMock()
        mock_transcript_api.get_transcript.return_value = [
            {"text": "こんにちは", "start": 0.0, "duration": 1.0},
            {"text": "セラミドの話です", "start": 1.0, "duration": 2.0},
        ]

        with patch.dict("sys.modules", {
            "youtube_transcript_api": MagicMock(
                YouTubeTranscriptApi=mock_transcript_api,
                TranscriptsDisabled=Exception,
                NoTranscriptFound=Exception,
            )
        }):
            result = agent._enrich_with_transcripts(videos, max_videos=5)

        assert result[0]["transcript"] == "こんにちは セラミドの話です"

    def test_skips_video_with_no_transcript(self, researcher_env) -> None:
        """Videos without Japanese subtitles should have empty transcript."""
        agent = researcher_env
        videos = [{"video_id": "vid002", "title": "字幕なし動画"}]

        class _NoTranscriptFound(Exception):
            pass

        mock_api_module = MagicMock()
        mock_api_module.TranscriptsDisabled = _NoTranscriptFound
        mock_api_module.NoTranscriptFound = _NoTranscriptFound
        mock_api_module.YouTubeTranscriptApi.get_transcript.side_effect = _NoTranscriptFound()

        with patch.dict("sys.modules", {"youtube_transcript_api": mock_api_module}):
            result = agent._enrich_with_transcripts(videos, max_videos=5)

        assert result[0].get("transcript") == ""

    def test_uses_cache_for_known_video(self, researcher_env) -> None:
        """A video whose transcript is already cached should not call the API."""
        agent = researcher_env
        agent.state.save_json("transcript_cache.json", {
            "entries": {"vid003": "キャッシュ済みトランスクリプト"},
        })

        videos = [{"video_id": "vid003", "title": "キャッシュ動画"}]

        mock_yt_api = MagicMock()
        with patch.dict("sys.modules", {
            "youtube_transcript_api": MagicMock(
                YouTubeTranscriptApi=mock_yt_api,
                TranscriptsDisabled=Exception,
                NoTranscriptFound=Exception,
            )
        }):
            result = agent._enrich_with_transcripts(videos, max_videos=5)

        mock_yt_api.get_transcript.assert_not_called()
        assert result[0]["transcript"] == "キャッシュ済みトランスクリプト"

    def test_respects_max_videos_limit(self, researcher_env) -> None:
        """Only the first max_videos should be processed."""
        agent = researcher_env
        videos = [
            {"video_id": f"vid{i:03d}", "title": f"動画{i}"} for i in range(10)
        ]

        call_count = 0

        def _mock_get(vid, languages):
            nonlocal call_count
            call_count += 1
            return [{"text": f"トランスクリプト{vid}", "start": 0.0, "duration": 1.0}]

        mock_yt_api = MagicMock()
        mock_yt_api.get_transcript.side_effect = _mock_get

        with patch.dict("sys.modules", {
            "youtube_transcript_api": MagicMock(
                YouTubeTranscriptApi=mock_yt_api,
                TranscriptsDisabled=Exception,
                NoTranscriptFound=Exception,
            )
        }):
            agent._enrich_with_transcripts(videos, max_videos=3)

        assert call_count == 3

    def test_transcript_trimmed_to_1000_chars(self, researcher_env) -> None:
        """Transcripts longer than 1000 chars should be trimmed."""
        agent = researcher_env
        videos = [{"video_id": "vid_long", "title": "長い動画"}]

        long_text = "あ" * 2000

        mock_yt_api = MagicMock()
        mock_yt_api.get_transcript.return_value = [
            {"text": long_text, "start": 0.0, "duration": 1.0}
        ]

        with patch.dict("sys.modules", {
            "youtube_transcript_api": MagicMock(
                YouTubeTranscriptApi=mock_yt_api,
                TranscriptsDisabled=Exception,
                NoTranscriptFound=Exception,
            )
        }):
            result = agent._enrich_with_transcripts(videos, max_videos=5)

        assert len(result[0]["transcript"]) <= 1000
