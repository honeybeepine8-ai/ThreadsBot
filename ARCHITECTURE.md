# Threads完全自動運用Bot - 技術設計書

## 1. ディレクトリ構成

```
ThreadsBot/
├── ARCHITECTURE.md              # 本ドキュメント
├── .env                         # 環境変数（API キー、トークン等）
├── .env.example                 # 環境変数テンプレート
├── .gitignore
├── requirements.txt             # Python依存パッケージ
├── config/
│   ├── settings.yaml            # グローバル設定（安全装置の閾値等）
│   ├── tone.yaml                # 口調・文体の定義
│   ├── ng_words.txt             # NGワードリスト
│   └── posting_patterns.yaml    # 投稿パターン（時間帯・カテゴリ等）
├── agents/
│   ├── __init__.py
│   ├── base_agent.py            # 全エージェント共通の基底クラス
│   ├── researcher.py            # 1. リサーチャー
│   ├── analyst.py               # 2. アナリスト
│   ├── writer.py                # 3. ライター
│   ├── poster.py                # 4. ポスター
│   ├── fetcher.py               # 5. フェッチャー
│   └── supervisor.py            # 6. スーパーバイザー
├── core/
│   ├── __init__.py
│   ├── scheduler.py             # 実行スケジューラ（エントリポイント）
│   ├── state_manager.py         # 状態管理（JSON読み書き）
│   ├── quality_gate.py          # 品質チェック（スコア・類似度）
│   ├── safety.py                # 安全装置（投稿上限・間隔・緊急停止）
│   └── logger.py                # 構造化ログ
├── services/
│   ├── __init__.py
│   ├── threads_api.py           # Threads API クライアント
│   ├── youtube_api.py           # YouTube Data API クライアント
│   ├── x_scraper.py             # X（旧Twitter）情報取得
│   └── claude_client.py         # Claude API クライアント（文章生成・分析用）
├── data/
│   ├── state/
│   │   ├── post_history.json    # 投稿履歴
│   │   ├── post_queue.json      # 投稿キュー
│   │   ├── research_pool.json   # リサーチネタプール
│   │   └── system_state.json    # システム状態（緊急停止フラグ等）
│   ├── analytics/
│   │   ├── performance.json     # 投稿パフォーマンスデータ
│   │   └── audience.json        # 読者分析データ
│   └── logs/
│       └── (日付別ログファイル)
├── prompts/
│   ├── researcher.md            # リサーチャー用プロンプト
│   ├── analyst.md               # アナリスト用プロンプト
│   └── writer.md                # ライター用プロンプト
└── tests/
    ├── __init__.py
    ├── test_writer.py
    ├── test_quality_gate.py
    ├── test_safety.py
    └── test_threads_api.py
```

---

## 2. データフロー

### 全体フロー図

```
[YouTube/X]
    │
    ▼
┌──────────┐    research_pool.json    ┌──────────┐
│Researcher├─────────────────────────►│  Writer  │
└──────────┘                          └────┬─────┘
                                           │
                                    品質チェック (quality_gate)
                                    類似度チェック (vs post_history)
                                           │
                                           ▼
                                    post_queue.json
                                           │
                                           ▼
                                    ┌──────────┐   Threads API   ┌─────────┐
                                    │  Poster  ├────────────────►│ Threads │
                                    └────┬─────┘                 └─────────┘
                                         │
                                  post_history.json
                                         │
                                         ▼
┌──────────┐  performance.json   ┌──────────┐
│ Analyst  │◄────────────────────┤ Fetcher  │
└──────────┘                     └──────────┘
    │                                  ▲
    │ posting_patterns.yaml更新        │ insights取得
    ▼                                  │
 Writer/Researcherへ               Threads API
 フィードバック

            ┌──────────────┐
            │  Supervisor  │ ← 全体監視（常時）
            └──────────────┘
```

### 各エージェントの入出力

| エージェント | 入力 | 処理 | 出力 |
|---|---|---|---|
| **Researcher** | YouTube API / X データ | トレンド抽出、ネタ候補生成 | `research_pool.json` に追記 |
| **Analyst** | `performance.json`, `audience.json` | エンゲージメント分析、最適時間帯算出 | `posting_patterns.yaml` 更新、分析レポート |
| **Writer** | `research_pool.json`, `tone.yaml`, `posting_patterns.yaml` | Claude APIで投稿文生成 → 品質・類似度チェック | `post_queue.json` に追記 |
| **Poster** | `post_queue.json` | Threads APIで投稿実行 | `post_history.json` に追記、キューから削除 |
| **Fetcher** | `post_history.json`（投稿ID一覧） | Threads Insights API呼び出し | `performance.json` 更新 |
| **Supervisor** | 全JSONファイル、ログ | 異常検知、ヘルスチェック | アラート通知、`system_state.json` 更新 |

---

## 3. 状態管理 - JSONスキーマ設計

### 3.1 `data/state/research_pool.json`

```json
{
  "last_updated": "2026-03-22T10:00:00+09:00",
  "items": [
    {
      "id": "res_20260322_001",
      "source": "youtube",
      "source_url": "https://youtube.com/watch?v=xxx",
      "topic": "Claude 4のマルチモーダル機能が凄い",
      "summary": "Anthropicが発表した最新モデルの解説...",
      "keywords": ["AI", "Claude", "マルチモーダル"],
      "category": "tech_news",
      "collected_at": "2026-03-22T09:30:00+09:00",
      "used": false,
      "priority": 8
    }
  ]
}
```

### 3.2 `data/state/post_queue.json`

```json
{
  "last_updated": "2026-03-22T11:00:00+09:00",
  "queue": [
    {
      "id": "q_20260322_001",
      "research_id": "res_20260322_001",
      "content": "投稿本文テキスト（500文字以内）",
      "thread_posts": ["2ポスト目", "3ポスト目"],
      "hashtag": "#スキンケア",
      "pattern": "ツリー展開型",
      "quality_score": 8.2,
      "similarity_score": 0.32,
      "category": "skincare_ingredients",
      "scheduled_at": "2026-03-22T12:00:00+09:00",
      "created_at": "2026-03-22T11:00:00+09:00",
      "status": "pending",
      "retry_count": 0,
      "affiliate_comment": null,
      "profile_cta_included": false,
      "debate_id": "debate_001",
      "debate_title": "無添加は本当に安全か？"
    }
  ]
}
```

### 3.3 `data/state/post_history.json`

```json
{
  "last_updated": "2026-03-22T12:05:00+09:00",
  "posts": [
    {
      "id": "post_20260322_001",
      "queue_id": "q_20260322_001",
      "threads_media_id": "17890012345678901",
      "thread_media_ids": ["18901234567890", "18901234567891"],
      "content": "投稿本文テキスト",
      "hashtag": "#スキンケア",
      "pattern": "ツリー展開型",
      "posted_at": "2026-03-22T12:00:00+09:00",
      "quality_score": 8.2,
      "category": "skincare_ingredients",
      "debate_id": "debate_001",
      "debate_title": "無添加は本当に安全か？",
      "affiliate_added_at": null,
      "affiliate_media_id": null,
      "affiliate_product_id": null,
      "metrics": {
        "views": 0,
        "likes": 0,
        "replies": 0,
        "reposts": 0,
        "quotes": 0,
        "last_fetched": null
      }
    }
  ],
  "daily_stats": {
    "2026-03-22": {
      "total_posts": 1,
      "last_post_at": "2026-03-22T12:00:00+09:00"
    }
  }
}
```

### 3.4 `data/analytics/performance.json`

```json
{
  "last_updated": "2026-03-22T18:00:00+09:00",
  "summary": {
    "period": "last_30_days",
    "avg_views": 1250,
    "avg_likes": 45,
    "avg_replies": 8,
    "top_category": "tech_news",
    "best_posting_hours": [8, 12, 19, 21],
    "best_posting_days": ["monday", "wednesday", "friday"]
  },
  "by_category": {
    "tech_news": {
      "avg_views": 1800,
      "avg_likes": 62,
      "post_count": 45
    },
    "tips": {
      "avg_views": 900,
      "avg_likes": 30,
      "post_count": 30
    }
  },
  "posts": [
    {
      "post_id": "post_20260322_001",
      "threads_media_id": "17890012345678901",
      "posted_at": "2026-03-22T12:00:00+09:00",
      "views": 520,
      "likes": 18,
      "replies": 3,
      "reposts": 2,
      "quotes": 1,
      "engagement_rate": 0.046,
      "fetched_at": "2026-03-22T18:00:00+09:00"
    }
  ]
}
```

### 3.5 `data/state/system_state.json`

```json
{
  "emergency_stop": false,
  "emergency_stop_reason": null,
  "emergency_stop_at": null,
  "last_health_check": "2026-03-22T12:00:00+09:00",
  "agent_status": {
    "researcher": { "last_run": "2026-03-22T09:00:00+09:00", "status": "ok", "error": null },
    "analyst":    { "last_run": "2026-03-22T06:00:00+09:00", "status": "ok", "error": null },
    "writer":     { "last_run": "2026-03-22T10:00:00+09:00", "status": "ok", "error": null },
    "poster":     { "last_run": "2026-03-22T12:00:00+09:00", "status": "ok", "error": null },
    "fetcher":    { "last_run": "2026-03-22T11:00:00+09:00", "status": "ok", "error": null },
    "supervisor": { "last_run": "2026-03-22T12:05:00+09:00", "status": "ok", "error": null }
  },
  "daily_counters": {
    "2026-03-22": {
      "posts_published": 3,
      "posts_rejected_quality": 1,
      "posts_rejected_similarity": 0,
      "api_errors": 0
    }
  }
}
```

---

## 4. 実行方式（Windows環境）

### 4.1 スケジュール設計

各エージェントは独立したPythonスクリプトとして実行し、Windows Task Schedulerで制御する。

| エージェント | 実行間隔 | 実行タイミング例 | 備考 |
|---|---|---|---|
| **Researcher** | 4時間ごと | 06:00, 10:00, 14:00, 18:00, 22:00 | ネタ収集（APIレート制限考慮） |
| **Writer** | 2時間ごと | 07:00, 09:00, 11:00, ... | キューが少ない時のみ実行 |
| **Poster** | 1時間ごと | 毎時00分 | 安全装置チェック後に投稿 |
| **Fetcher** | 6時間ごと | 00:00, 06:00, 12:00, 18:00 | 投稿後6時間以上経過分を対象 |
| **Analyst** | 1日1回 | 毎日 05:00 | 前日分の集計・分析 |
| **Supervisor** | 15分ごと | 常時 | ヘルスチェック・異常検知 |

### 4.2 エントリポイント（`core/scheduler.py`）

```python
"""
使い方:
  python -m core.scheduler researcher
  python -m core.scheduler writer
  python -m core.scheduler poster
  ...
"""
import sys
from agents.researcher import ResearcherAgent
from agents.analyst import AnalystAgent
from agents.writer import WriterAgent
from agents.poster import PosterAgent
from agents.fetcher import FetcherAgent
from agents.supervisor import SupervisorAgent

AGENTS = {
    "researcher": ResearcherAgent,
    "analyst": AnalystAgent,
    "writer": WriterAgent,
    "poster": PosterAgent,
    "fetcher": FetcherAgent,
    "supervisor": SupervisorAgent,
}

def main():
    agent_name = sys.argv[1]
    agent_class = AGENTS[agent_name]
    agent = agent_class()
    agent.run()

if __name__ == "__main__":
    main()
```

### 4.3 Windows Task Scheduler登録（PowerShell）

```powershell
# 例: Posterを毎時00分に実行
$action = New-ScheduledTaskAction `
    -Execute "C:\ThreadsBot\.venv\Scripts\python.exe" `
    -Argument "-m core.scheduler poster" `
    -WorkingDirectory "C:\ThreadsBot"

$trigger = New-ScheduledTaskTrigger -Daily -At "00:00"
# 1時間ごとに繰り返し
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At "00:00" `
    -RepetitionInterval (New-TimeSpan -Hours 1)).Repetition

Register-ScheduledTask -TaskName "ThreadsBot-Poster" `
    -Action $action -Trigger $trigger `
    -Description "Threads Bot - Poster Agent (hourly)"
```

### 4.4 代替案: Task Schedulerの代わりにPython内蔵スケジューラ

常駐プロセスとして `APScheduler` を使う方法もある。Task Schedulerよりデバッグが容易。

```python
# core/daemon.py
from apscheduler.schedulers.blocking import BlockingScheduler

scheduler = BlockingScheduler()
scheduler.add_job(ResearcherAgent().run, 'interval', hours=4)
scheduler.add_job(WriterAgent().run,     'interval', hours=2)
scheduler.add_job(PosterAgent().run,     'interval', hours=1)
scheduler.add_job(FetcherAgent().run,    'interval', hours=6)
scheduler.add_job(AnalystAgent().run,    'cron',     hour=5)
scheduler.add_job(SupervisorAgent().run, 'interval', minutes=15)
scheduler.start()
```

---

## 5. 技術スタック

### 5.1 言語: Python 3.11+

**選定理由:**
- Claude API (Anthropic SDK) の公式サポートが充実
- データ処理・テキスト処理のライブラリが豊富
- JSON操作が簡潔
- Windows Task Scheduler との連携が容易

### 5.2 主要ライブラリ

```
# requirements.txt

# --- Core ---
anthropic>=0.40.0           # Claude API（文章生成・分析）
httpx>=0.27.0               # HTTP クライアント（Threads API呼び出し）
pyyaml>=6.0                 # 設定ファイル読み込み
pydantic>=2.5               # データバリデーション・スキーマ定義
python-dotenv>=1.0          # .env読み込み

# --- リサーチ ---
google-api-python-client>=2.100  # YouTube Data API v3
# X(旧Twitter) は公式APIの料金が高いため、
# RSSフィードやNitter等の代替手段も検討

# --- テキスト処理 ---
scikit-learn>=1.3           # TF-IDFベクトル化 + コサイン類似度
# (類似度チェックに使用。embeddingAPIの代替として軽量)

# --- スケジューリング（daemon方式の場合） ---
apscheduler>=3.10           # Python内蔵スケジューラ

# --- 監視・通知 ---
slack-sdk>=3.27             # Slack通知（異常検知アラート用、任意）

# --- テスト ---
pytest>=8.0
pytest-asyncio>=0.23
```

### 5.3 Threads API連携

Threads APIはMeta Graph APIベース。主要エンドポイント:

```
ベースURL: https://graph.threads.net/v1.0

# 投稿（2段階）
POST /{user_id}/threads          → メディアコンテナ作成
POST /{user_id}/threads_publish  → 公開

# インサイト取得
GET /{media_id}/insights?metric=views,likes,replies,reposts,quotes

# ユーザー情報
GET /me?fields=id,username,threads_biography
```

**認証:** OAuth 2.0 長期トークン（60日有効、自動更新が必要）

### 5.4 Claude API活用箇所

| 用途 | モデル | 理由 |
|---|---|---|
| 投稿文生成（Writer） | Claude Sonnet | コスト効率と品質のバランス |
| 品質スコア判定（Writer） | Claude Haiku | 高速・低コスト、数値判定のみ |
| トレンド分析（Researcher） | Claude Sonnet | 要約・抽出の精度 |
| パフォーマンス分析（Analyst） | Claude Sonnet | 分析・提案の質 |

---

## 6. エラーハンドリング・リカバリ戦略

### 6.1 共通方針

```python
# agents/base_agent.py の概要
class BaseAgent:
    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = [30, 120, 300]  # 指数的バックオフ

    def run(self):
        """全エージェント共通の実行フロー"""
        # 1. 緊急停止チェック
        if self.safety.is_emergency_stopped():
            self.logger.warning("Emergency stop active. Skipping.")
            return

        # 2. 実行（リトライ付き）
        for attempt in range(self.MAX_RETRIES):
            try:
                self.execute()  # サブクラスが実装
                self.update_agent_status("ok")
                return
            except RateLimitError:
                self.wait_and_retry(attempt)
            except APIError as e:
                self.logger.error(f"API error: {e}")
                if attempt == self.MAX_RETRIES - 1:
                    self.update_agent_status("error", str(e))
                    self.notify_supervisor(e)
            except Exception as e:
                self.logger.critical(f"Unexpected error: {e}")
                self.update_agent_status("error", str(e))
                self.notify_supervisor(e)
                break  # 予期しないエラーはリトライしない
```

### 6.2 エージェント別リカバリ

| エージェント | 想定エラー | リカバリ策 |
|---|---|---|
| **Researcher** | YouTube APIレート制限、X取得失敗 | レート制限時は次回実行まで待機。片方失敗でも他方のデータで継続 |
| **Writer** | Claude API障害、品質スコア不足 | 品質不足は最大3回再生成。API障害はリトライ後スキップ |
| **Poster** | Threads API 429(レート制限)、トークン期限切れ | 429はバックオフ後リトライ。トークン切れはアラート＋緊急停止 |
| **Fetcher** | 投稿ID無効、API障害 | 無効IDはスキップして次へ。全体障害は次回実行で再取得 |
| **Analyst** | データ不足（新規立ち上げ時） | 最低10件未満の場合はスキップ、デフォルト設定を維持 |
| **Supervisor** | 自身の障害 | Task Schedulerの「失敗時再起動」設定で対応 |

### 6.3 安全装置の実装

```python
# core/safety.py の概要
class SafetyGuard:
    MAX_DAILY_POSTS = 15
    MIN_POST_INTERVAL_MINUTES = 60
    MIN_QUALITY_SCORE = 7.0
    MAX_SIMILARITY_SCORE = 0.85

    def can_post(self) -> tuple[bool, str]:
        """投稿可否を判定。(可否, 理由) を返す"""
        state = self.load_system_state()

        # 緊急停止チェック
        if state["emergency_stop"]:
            return False, "Emergency stop is active"

        # 日次上限チェック
        today = date.today().isoformat()
        daily = state["daily_counters"].get(today, {})
        if daily.get("posts_published", 0) >= self.MAX_DAILY_POSTS:
            return False, f"Daily limit reached ({self.MAX_DAILY_POSTS})"

        # 最低間隔チェック
        history = self.load_post_history()
        if history["posts"]:
            last_post_time = parse(history["posts"][-1]["posted_at"])
            elapsed = (now() - last_post_time).total_seconds() / 60
            if elapsed < self.MIN_POST_INTERVAL_MINUTES:
                return False, f"Too soon ({elapsed:.0f}min < {self.MIN_POST_INTERVAL_MINUTES}min)"

        return True, "OK"

    def emergency_stop(self, reason: str):
        """緊急停止を発動"""
        state = self.load_system_state()
        state["emergency_stop"] = True
        state["emergency_stop_reason"] = reason
        state["emergency_stop_at"] = now().isoformat()
        self.save_system_state(state)
        self.send_alert(f"EMERGENCY STOP: {reason}")
```

### 6.4 品質ゲート

```python
# core/quality_gate.py の概要
class QualityGate:
    def check(self, content: str, history_contents: list[str]) -> dict:
        """投稿内容の品質・類似度を検証"""

        # 1. NGワードチェック
        ng_violations = self.check_ng_words(content)
        if ng_violations:
            return {"pass": False, "reason": f"NG words: {ng_violations}"}

        # 2. 品質スコア（Claude Haikuで判定）
        quality_score = self.evaluate_quality(content)
        if quality_score < SafetyGuard.MIN_QUALITY_SCORE:
            return {"pass": False, "reason": f"Quality {quality_score} < 7.0"}

        # 3. 類似度チェック（TF-IDF + コサイン類似度）
        max_similarity = self.check_similarity(content, history_contents)
        if max_similarity > SafetyGuard.MAX_SIMILARITY_SCORE:
            return {"pass": False, "reason": f"Similarity {max_similarity} > 0.85"}

        # 4. 文字数チェック（Threads制限: 500文字）
        if len(content) > 500:
            return {"pass": False, "reason": f"Too long: {len(content)} chars"}

        return {
            "pass": True,
            "quality_score": quality_score,
            "similarity_score": max_similarity
        }
```

---

## 7. 設定ファイル例

### 7.1 `config/settings.yaml`

```yaml
# グローバル設定
app:
  name: "ThreadsBot"
  timezone: "Asia/Tokyo"

safety:
  max_daily_posts: 15
  min_post_interval_minutes: 60
  min_quality_score: 7.0
  max_similarity_score: 0.85
  emergency_stop_file: "data/state/system_state.json"

threads_api:
  base_url: "https://graph.threads.net/v1.0"
  media_container_wait_seconds: 30
  max_text_length: 500
  max_hashtags: 1

claude:
  writer_model: "claude-sonnet-4-20250514"
  evaluator_model: "claude-haiku-4-20250414"
  max_tokens_write: 600
  max_tokens_evaluate: 200

researcher:
  youtube_max_results: 20
  categories:
    - "tech_news"
    - "tips"
    - "opinion"
    - "trend"

writer:
  max_generation_attempts: 3
  queue_target_size: 10  # キューがこの数未満なら生成する
```

### 7.2 `config/tone.yaml`

```yaml
# 口調・文体の定義
persona:
  name: "テックの人"
  description: "テクノロジーに詳しい30代エンジニア。カジュアルだが正確。"

style:
  tone: "friendly_casual"
  sentence_ending: ["だよね", "なんだよね", "って話", "じゃない？"]
  avoid: ["です", "ます", "ございます"]  # 敬体を使わない
  max_emoji_per_post: 2
  preferred_emoji: ["🔥", "💡", "👀", "🚀"]

rules:
  - "1文目で興味を引くフック文を入れる"
  - "専門用語は必ず簡単な言い換えも添える"
  - "最後は問いかけか共感で締める"
  - "ハッシュタグは1つだけ"
```

---

## 8. 構築の推奨順序

段階的に構築・テストすることを推奨する。

| フェーズ | 構築対象 | 期間目安 |
|---|---|---|
| **Phase 1** | Poster + Fetcher + Safety（手動で投稿→メトリクス取得の基本ループ） | 3-4日 |
| **Phase 2** | Writer + QualityGate（Claude APIで投稿文を自動生成） | 3-4日 |
| **Phase 3** | Researcher（YouTube/Xからネタ収集の自動化） | 2-3日 |
| **Phase 4** | Analyst + Supervisor（分析・監視の自動化） | 2-3日 |
| **Phase 5** | Task Scheduler統合、本番運用開始 | 1-2日 |

**合計: 約2-3週間** （個人開発・パートタイムの場合）

---

## 9. 注意事項

- **Threads APIアクセス**: Meta Developer Portal でアプリ承認が必要。審査に数日かかる場合がある
- **トークン管理**: OAuth長期トークンは60日で失効。自動更新の仕組みを早期に実装すること
- **API料金**: Claude API利用料が主要コスト。Haikuを積極活用してコスト最適化
- **24時間250件制限**: Threads APIの投稿上限。日15件の設定なら十分余裕がある
- **ハッシュタグ制限**: Threads は1投稿につき1つのハッシュタグのみ許可
