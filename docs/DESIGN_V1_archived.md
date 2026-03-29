# ThreadsBot 設計資料

> Threads完全自動運用Bot — スキンケア特化アカウント
> 最終更新: 2026-03-22

---

## 1. プロジェクト概要

Claude Code + Threads API で、スキンケア特化のThreadsアカウントを完全自動運用するシステム。
6つのAIエージェントがそれぞれ独立したタスクを担当し、cronで定期実行する。

### ビジネス目標
- ジャンル: スキンケア特化（成分ベースの解説）
- ペルソナ: 「ゆう｜元美容部員28歳OL」
- マネタイズ: ASPアフィリエイト（A8.net / afb）、コメント欄にPRリンク
- 目標: 1アカウント月3万円〜（フォロワー1,000人時点）

### 技術スタック
- 言語: Python 3.11+
- AI: Claude API（Sonnet=生成、Haiku=品質判定）
- 投稿: Threads API（Meta Graph API）
- リサーチ: YouTube Data API v3
- 類似度: scikit-learn（TF-IDF + コサイン類似度）
- スケジュール: APScheduler / Windows Task Scheduler

---

## 2. ディレクトリ構成

```
ThreadsBot/
├── .env.example              # 環境変数テンプレート
├── .gitignore
├── requirements.txt          # 依存パッケージ (Python)
├── ARCHITECTURE.md           # 詳細技術設計書（JSONスキーマ等）
│
├── config/                   # 設定（ナレッジとロジックの分離）
│   ├── settings.yaml         # グローバル設定・安全装置閾値
│   ├── schedule.yaml         # 投稿スケジュール（時間帯・曜日）
│   ├── tone.yaml             # ペルソナ・口調・ターゲット
│   ├── ng_words.txt          # NGワード辞書（薬機法・bot臭い表現）
│   ├── affiliate_products.yaml # アフィリエイト商品カタログ
│   ├── ugc_templates.yaml    # UGCテンプレート定義
│   └── accounts/             # マルチアカウント設定（riku/, hina/）
│
├── agents/                   # 8つのAIエージェント
│   ├── base_agent.py         # 共通基底クラス
│   ├── researcher.py         # ネタ収集
│   ├── analyst.py            # パフォーマンス分析
│   ├── writer.py             # 投稿生成 ★メインエンジン（スレッド対応）
│   ├── poster.py             # 投稿実行（スレッド連結対応）
│   ├── fetcher.py            # メトリクス取得 + 後乗せアフィリ（商品カタログ対応）
│   ├── replier.py            # 自動リプライ
│   ├── supervisor.py         # 監視・異常検知
│   └── cross_poster.py       # マルチアカウント相互引用
│
├── core/                     # 共通基盤
│   ├── logger.py             # 構造化ログ（60行）
│   ├── state_manager.py      # JSONアトミック書き込み（124行）
│   ├── safety.py             # 安全装置・KILL_SWITCH（193行）
│   ├── quality_gate.py       # NGワード・類似度チェック（201行）
│   └── scheduler.py          # CLIエントリポイント + daemon（236行）
│
├── services/                 # 外部API連携
│   ├── threads_api.py        # Threads API クライアント（238行）
│   └── claude_client.py      # Claude API クライアント（174行）
│
├── data/                     # 実行時データ（git管理外）
│   ├── state/                # post_history, post_queue, system_state 等
│   ├── analytics/            # performance, audience
│   └── logs/                 # 日付別ログ
│
├── knowledge/                # コンテンツナレッジ
│   ├── posting_rules.md      # 投稿パターン9種+スレッド形式・品質基準
│   ├── profile.yaml          # ペルソナ・口調詳細定義
│   ├── debate_whitelist.yaml # 論争テーマホワイトリスト（8テーマ）
│   ├── theme_tree.yaml       # テーマツリー（リサーチカバレッジ追跡）
│   ├── genre.yaml            # ジャンル定義
│   ├── target.yaml           # ターゲット定義
│   └── domain_knowledge.md   # ドメイン知識
│
├── prompts/                  # エージェント用プロンプト
│   ├── writer.md
│   ├── researcher.md
│   └── analyst.md
│
├── scripts/                  # 運用スクリプト
│   ├── setup.ps1             # 環境セットアップ
│   ├── init_data.py          # データファイル初期化
│   └── kill_switch.py        # 緊急停止操作
│
├── tests/                    # ユニットテスト（139テスト）
│   ├── conftest.py
│   ├── test_state_manager.py
│   ├── test_safety.py
│   ├── test_quality_gate.py
│   ├── test_poster.py
│   ├── test_replier.py
│   ├── test_fetcher.py
│   ├── test_supervisor.py
│   ├── test_boost.py
│   ├── test_account_context.py
│   ├── test_cross_poster.py
│   ├── test_token_manager.py
│   └── test_writer_thread.py
│
└── docs/                     # 設計資料
    └── DESIGN.md             # 本ドキュメント
```

**総コード量: 約4,500行（Python）**

---

## 3. システムアーキテクチャ

### 3.1 データフロー全体図

```
[YouTube]
    │
    ▼
┌────────────┐  research_pool.json  ┌────────────┐
│ Researcher ├─────────────────────►│   Writer   │
└────────────┘                      └─────┬──────┘
                                          │
                                   品質チェック (QualityGate)
                                   ├─ NGワードフィルタ
                                   ├─ 類似度チェック (TF-IDF)
                                   ├─ パターンローテーション
                                   └─ 品質スコア (Claude Haiku)
                                          │
                                          ▼
                                   post_queue.json
                                          │
                                          ▼
                                   ┌────────────┐  Threads API
                                   │   Poster   ├──────────────►[Threads]
                                   └─────┬──────┘
                                         │
                                  post_history.json
                                         │
                                         ▼
┌────────────┐  performance.json  ┌────────────┐
│  Analyst   │◄───────────────────┤  Fetcher   │
└──────┬─────┘                    └────────────┘
       │                                ▲
       │ フィードバック                   │ insights取得
       ▼                                │
Writer/Researcher                   Threads API
へ分析結果を反映

              ┌──────────────┐
              │  Supervisor  │ ← 全体監視（15分ごと）
              └──────────────┘
```

### 3.2 エージェント一覧

| # | エージェント | ファイル | 実行間隔 | 入力 | 出力 |
|---|------------|---------|---------|------|------|
| 1 | **Researcher** | agents/researcher.py | 4時間 | YouTube API | research_pool.json |
| 2 | **Analyst** | agents/analyst.py | 毎日05:00 | performance.json | schedule.yaml更新, audience.json |
| 3 | **Writer** | agents/writer.py | 2時間 | research_pool, tone, posting_rules | post_queue.json |
| 4 | **Poster** | agents/poster.py | 1時間 | post_queue.json | post_history.json → Threads |
| 5 | **Fetcher** | agents/fetcher.py | 6時間 | post_history.json | performance.json + 後乗せアフィリ |
| 6 | **Replier** | agents/replier.py | 30分 | post_history + Threads返信 | reply_state.json → Threads |
| 7 | **Supervisor** | agents/supervisor.py | 15分 | 全JSONファイル | アラート, system_state.json |
| 8 | **CrossPoster** | agents/cross_poster.py | 12時間 | 各アカウントのpost_history | cross_post_state.json → Threads |

---

## 4. 各モジュール詳細

### 4.1 agents/base_agent.py（共通基底）

```
BaseAgent(ABC)
├── __init__(name)     → logger, StateManager, SafetyGuard 初期化
├── run()              → 緊急停止チェック → Circuit Breakerチェック → execute()
│   ├── MAX_RETRIES=3, RETRY_DELAYS=[30, 120, 300]秒
│   ├── RateLimitError    → リトライ（指数的バックオフ）
│   ├── AuthenticationError → 即座にemergency_stop
│   └── その他例外       → エラー記録 → リトライ → 上限で停止
├── execute()          → 抽象メソッド（サブクラスが実装）
└── update_status()    → system_state.json 更新
```

### 4.2 agents/writer.py（メインエンジン）

```
WriterAgent(BaseAgent)
├── execute()
│   ├── post_queue の pending数 < queue_target_size(10) なら生成開始
│   ├── research_pool から未使用ネタを priority順で取得
│   └── 各ネタに _generate_single_post() を実行
│
├── _generate_single_post(research_item)
│   ├── _select_pattern()        → 15パターンからローテーション選択
│   ├── _select_time_slot()      → schedule.yaml に基づき投稿時刻決定
│   ├── _build_prompt()          → ネタ+パターン+口調ルールでプロンプト構築
│   ├── claude_client.generate_post()  → 投稿文生成（Sonnet）
│   ├── quality_gate.validate()  → NGワード・類似度・パターンチェック
│   ├── _evaluate_quality_score() → 品質スコア判定（Haiku）
│   └── スコア7.0未満 → 再生成（最大5回）
│
├── _select_pattern()
│   ├── 直近3件で使ったパターンを除外
│   ├── 現在時間帯の推奨パターンを70%優先
│   └── ランダム要素を含む
│
└── _select_time_slot()
    ├── 曜日別投稿数（水木=4, 土=2, 他=3）
    ├── base_time ± jitter のランダム時刻
    └── 既存キューとの重複回避
```

### 4.3 agents/poster.py（投稿実行）

```
PosterAgent(BaseAgent)
└── execute()
    ├── post_queue から scheduled_at ≤ now の pending を1件取得
    ├── safety.can_post() チェック
    ├── ThreadsAPI.create_text_post() で投稿
    ├── post_history.json に記録
    ├── affiliate_comment があれば自己リプライで投稿
    └── safety.record_post() でカウント記録
```

### 4.4 agents/researcher.py（ネタ収集）

```
ResearcherAgent(BaseAgent)
└── execute()
    ├── _search_youtube()     → 5カテゴリ × キーワードで検索
    ├── _extract_topics()     → Claude で動画情報からネタ抽出
    ├── _is_duplicate()       → 既存ネタとのキーワード重複チェック
    ├── research_pool.json に追記
    └── _cleanup_old_items()  → 7日超used=True / 30日超を削除
```

### 4.5 agents/analyst.py（分析）

```
AnalystAgent(BaseAgent)
└── execute()
    ├── performance.json から投稿データ読み込み
    ├── _analyze_performance() → カテゴリ別/パターン別/時間帯別の集計
    ├── _generate_feedback()   → Claude で分析 → audience.json 保存
    └── schedule.yaml の推奨パターンを更新
```

### 4.6 agents/supervisor.py（監視）

```
SupervisorAgent(BaseAgent)
└── execute()
    ├── _check_agent_health()       → 各エージェントの last_run 確認
    ├── _check_posting_anomalies()  → 投稿間隔・連続カテゴリ等の異常
    ├── _check_error_rate()         → エラー率・棄却率チェック
    └── _send_alert()               → 3件以上の同時警告で emergency_stop
```

---

## 5. core層

### 5.1 core/safety.py（安全装置）

| 機能 | パラメータ | 値 |
|------|----------|-----|
| 日次投稿上限 | max_daily_posts | 10件 |
| 最低投稿間隔 | min_post_interval_minutes | 90分 |
| ランダム揺らぎ | interval_jitter_minutes | ±45分 |
| 活動時間帯 | active_hours | 7:00〜23:00 |
| 品質スコア閾値 | min_quality_score | 7.0 |
| 類似度閾値 | max_similarity_score | 0.85 |
| フック類似度閾値 | max_hook_similarity_score | 0.75 |
| 連続エラー上限 | max_consecutive_errors | 3回 |
| CB冷却期間 | circuit_breaker_cooldown_minutes | 30分 |

```
SafetyGuard
├── can_post()          → (bool, reason) 投稿可否判定
├── emergency_stop()    → 緊急停止発動
├── clear_emergency_stop() → 解除（手動のみ）
├── record_post()       → 投稿カウント記録
├── record_error()      → エラー記録
└── is_circuit_open()   → Circuit Breaker判定（自動クールダウン）
```

### 5.2 core/quality_gate.py（品質チェック）

```
QualityGate
├── check_ng_words(content)           → 薬機法・bot表現フィルタ
├── check_similarity(content, history) → TF-IDF + コサイン類似度
├── check_pattern_rotation(pattern, recent) → 直近3件の重複チェック
└── validate(content, pattern, history) → 総合判定
```

### 5.3 core/state_manager.py（状態管理）

- data/state/ 配下のJSONファイルをアトミック書き込み
- tempfile + os.replace でデータ破損防止
- ファイル未存在時はデフォルトスキーマを返す

---

## 6. services層

### 6.1 services/threads_api.py

```
ThreadsAPIClient
├── create_text_post(text, reply_to_id=None)
│   ├── Step1: POST /{user_id}/threads → コンテナ作成
│   ├── Step2: 30秒待機
│   └── Step3: POST /{user_id}/threads_publish → 公開
├── get_post_insights(media_id) → views,likes,replies,reposts,quotes
├── get_user_profile() → id,username,bio
├── delete_post(media_id)
└── _request(method, endpoint) → 共通HTTPメソッド
    ├── 429 → RateLimitError
    └── 401/403 → AuthenticationError
```

**注意**: OAuthトークンは60日で失効。自動更新は未実装（要手動更新 or 追加実装）。

### 6.2 services/claude_client.py

```
ClaudeClient
├── generate_post(prompt, system_prompt)  → Sonnet で投稿生成
├── evaluate_quality(content, criteria)   → Haiku で品質スコア判定
│   → {scores: {hook,usefulness,specificity,tempo,persona_match}, average, feedback}
└── analyze(prompt, data)                 → Sonnet で汎用分析
```

---

## 7. 設定ファイル

### 7.1 config/settings.yaml
グローバル設定。安全装置の閾値、APIエンドポイント、Claudeモデル指定、各エージェントの実行パラメータ。

### 7.2 config/schedule.yaml
投稿スケジュール。Buffer社250万投稿分析 + 日本市場補正に基づく:

| スロット | 時刻 | 揺らぎ | 推奨コンテンツ | アフィリ |
|---------|------|--------|-------------|---------|
| morning | 07:30 | ±20分 | 朝の豆知識 | - |
| lunch | 12:15 | ±15分 | 成分解説・ランキング | ★優先 |
| evening | 20:00 | ±30分 | 共感・コメント誘導 | - |
| night | 21:30 | ±30分 | 軽めの共感（水木のみ） | - |

曜日別投稿数: 水木=4, 月火金=3, 土日=2

### 7.3 config/tone.yaml
ペルソナ「ゆう」の口調定義。一人称「わたし」、タメ口寄り丁寧語、漢字率30%以下。

### 7.4 config/ng_words.txt
薬機法違反表現（即ブロック）、景表法違反、bot臭い表現の辞書。約100語。

---

## 8. データスキーマ（JSON）

### 8.1 post_queue.json
```json
{
  "queue": [{
    "id": "q_20260322_001",
    "research_id": "res_20260322_001",
    "content": "投稿本文（500文字以内）",
    "hashtag": "#スキンケア",
    "pattern": "コメント誘導型",
    "quality_score": 8.2,
    "similarity_score": 0.32,
    "category": "skincare_ingredients",
    "scheduled_at": "2026-03-22T12:15:00+09:00",
    "created_at": "2026-03-22T10:00:00+09:00",
    "status": "pending",
    "retry_count": 0,
    "affiliate_comment": "PR用コメント or null"
  }]
}
```

### 8.2 post_history.json
```json
{
  "posts": [{
    "id": "post_20260322_001",
    "queue_id": "q_20260322_001",
    "threads_media_id": "17890012345678901",
    "content": "投稿本文",
    "hashtag": "#スキンケア",
    "pattern": "コメント誘導型",
    "posted_at": "2026-03-22T12:15:00+09:00",
    "quality_score": 8.2,
    "category": "skincare_ingredients",
    "metrics": {
      "views": 0, "likes": 0, "replies": 0,
      "reposts": 0, "quotes": 0, "last_fetched": null
    }
  }],
  "daily_stats": {
    "2026-03-22": {"total_posts": 1, "last_post_at": "..."}
  }
}
```

### 8.3 research_pool.json
```json
{
  "items": [{
    "id": "res_20260322_001",
    "source": "youtube",
    "source_url": "https://youtube.com/watch?v=xxx",
    "topic": "セラミド化粧水の正しい選び方",
    "summary": "セラミドにはヒト型と疑似型があり...",
    "keywords": ["セラミド", "化粧水", "乾燥肌"],
    "category": "skincare_ingredients",
    "collected_at": "2026-03-22T09:30:00+09:00",
    "used": false,
    "priority": 8
  }]
}
```

### 8.4 system_state.json
```json
{
  "emergency_stop": false,
  "emergency_stop_reason": null,
  "emergency_stop_at": null,
  "last_health_check": "2026-03-22T12:00:00+09:00",
  "agent_status": {
    "poster": {"last_run": "...", "status": "ok", "error": null}
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

## 9. 実行方法

### 個別実行
```bash
python -m core.scheduler poster
python -m core.scheduler writer
python -m core.scheduler researcher
python -m core.scheduler fetcher
python -m core.scheduler analyst
python -m core.scheduler supervisor
```

### daemon一括実行
```bash
python -m core.scheduler all
```

### 緊急停止
```bash
python scripts/kill_switch.py stop "理由"
python scripts/kill_switch.py status
python scripts/kill_switch.py clear
```

---

## 10. 安全設計

### 多重防御
1. **NGワードフィルタ** — 薬機法・景表法違反表現を即ブロック
2. **類似度チェック** — 直近50件と TF-IDF比較、0.85以上で棄却
3. **品質スコア** — Claude Haikuで5項目採点、平均7.0未満で棄却
4. **パターンローテーション** — 直近3件と同じ投稿パターン禁止
5. **投稿間隔** — 最低90分 + ランダム揺らぎ
6. **日次上限** — 1日10件まで
7. **Circuit Breaker** — 連続3エラーで30分自動停止
8. **KILL_SWITCH** — 手動 or 自動で全エージェント即停止
9. **Supervisor** — 15分ごとの異常監視、3件同時警告で自動停止

### ステマ規制対応
- affiliate_comment 付き投稿には「PR」表記を自動付与
- poster.py がコメント投稿時に処理

---

## 11. テスト

```bash
pytest tests/ -v
```

| テストファイル | テスト数 | カバー範囲 |
|-------------|---------|----------|
| test_state_manager.py | 7 | デフォルトスキーマ、save/load往復、アトミック書き込み |
| test_safety.py | 6 | 投稿可否、緊急停止、日次上限、間隔、Circuit Breaker |
| test_quality_gate.py | 14 | NGワード、類似度、パターンローテーション、validate統合、debate NGチェック |
| test_poster.py | 10 | 投稿実行、スケジュール、緊急停止、PRラベル、スレッド連結投稿 |
| test_supervisor.py | 11 | エージェント監視、投稿異常検知、エラー率、トークン期限 |
| test_token_manager.py | 13 | トークン管理、ステータス判定、初期化、PRラベル保持 |
| test_boost.py | 13 | ブーストモード判定、safety/schedule/writer/viralパターン上書き |
| test_account_context.py | 13 | マルチアカウント、config分離、fallback、get_all_accounts |
| test_replier.py | 11 | 日次制限、遅延、返信率、NG検出、状態クリーンアップ、per-user制限 |
| test_fetcher.py | 17 | メトリクス取得、refetch判定、後乗せアフィリ、バズ判定、PRラベル、商品カタログマッチング |
| test_cross_poster.py | 10 | 無効状態、日次制限、クールダウン、閾値、ハッピーパス、ヘルパー |
| test_writer_thread.py | 11 | スレッド比率算出、レスポンスパース、プロンプト構築、20+件エッジケース |
| test_researcher_season.py | 12 | 季節マトリクス構造検証、カテゴリ推測、重み付け配分 |
| test_hook_ab.py | 6 | フックストック読み込み、バズフック収集、閾値・重複・上限チェック |
| test_qa_cycle.py | 10 | 質問検出、重複排除、QA募集スケジュール、投稿生成、パターン一覧 |

---

## 12. 実装済み成長施策（2026-03-26追加、03-28更新）

| 施策 | 状態 | 概要 |
|------|------|------|
| Replierエージェント | 実装済 | フォロワーコメントに自動返信（エンゲージメント強化） |
| 後乗せアフィリ方式 | 実装済 | バズ投稿に事後的にアフィリコメント追加（Fetcher拡張） |
| プロフCTA | 実装済 | 投稿の35%にプロフ誘導文を自動追加（Writer拡張） |
| 初期ブーストモード | 実装済 | 2週間の特別モード（投稿頻度UP、バイラルパターン優先） |
| UGCテンプレ型 | 実装済 | 16番目の投稿パターン（週次テンプレ投稿） |
| マルチアカウント基盤 | 実装済 | AccountContext + りく/ひなペルソナ + --account CLI |
| 口癖シグネチャー | 実装済 | tone.yamlに signature_endings 追加 |
| CrossPosterエージェント | 実装済 | マルチアカウント間の相互引用リポスト（6ペア定義） |
| テスト拡充 | 実装済 | boost/account_context/replier/fetcher/cross_poster/writer_thread（計139テスト） |
| replier per-userリミット修正 | 実装済 | _get_today_user_countsバグ修正 + reply_to_username記録 |
| account_context引数名修正 | 実装済 | get_state_managerのbase_dir→state_dir |
| fetcher affiliate_added_at修正 | 実装済 | None値でのstartswith呼び出しバグ修正 |
| 高単価アフィリ商品カタログ | 実装済 | `config/affiliate_products.yaml` で商品定義、Fetcherがカテゴリ+キーワードでマッチング |
| 論争テーマホワイトリスト | 実装済 | `knowledge/debate_whitelist.yaml` で8テーマ定義、Writerが40%の確率で注入 |
| スレッド形式投稿（40%比率） | 実装済 | 自己リプライ連結投稿（2-3ポスト）、比率制御、Poster連結対応 |
| 季節×悩みマトリクス | 実装済 | `knowledge/season_matrix.yaml` で月別キーワード・カテゴリ重み付け、Researcherに統合 |
| フック文A/Bテスト | 実装済 | 同一ネタで複数フック生成→Claudeスコアリング→最良採用、バズフック自動蓄積 |
| 質問募集→回答サイクル | 実装済 | パターン17/18追加、Replierが質問検出→research_pool追加、Writer回答投稿生成 |

詳細設計: `docs/GROWTH_DESIGN.md` 参照

## 13. 未実装・今後の課題

| 項目 | 優先度 | 内容 |
|------|--------|------|
| OAuthトークン自動更新 | 高 | 60日失効のトークンを自動リフレッシュ |
| note連携 | 中 | バズ投稿のまとめ記事自動生成 |
| バズ予測モデル | 中 | 投稿初速からの最終リーチ予測 |
| Instagram連携 | 中 | Threads投稿のInstagramストーリー連携 |
| Slack/Discord通知 | 中 | Supervisor のアラートを外部通知 |
| X(旧Twitter)リサーチ | 中 | YouTube以外のネタ元追加 |
| Web管理画面 | 低 | 投稿キュー・履歴の可視化 |
