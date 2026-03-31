# ThreadsBot - Claude Code プロジェクト設定

## プロジェクト概要
Threads完全自動運用Bot。スキンケア成分知識特化の匿名アカウント（@seibun_love）をAIエージェントで自動運用し、アフィリエイトで収益化する。V3: 運用堅牢化完了。

## 技術スタック
- Python 3.11+, Claude API (Sonnet/Haiku), Threads API (Meta Graph API), YouTube Data API v3
- scikit-learn (TF-IDF), APScheduler, httpx, pydantic, pyyaml

## ディレクトリ構成
- `agents/` — 7つのAIエージェント（researcher, analyst, writer, poster, fetcher, replier, supervisor）
  - `writer_*.py` — Writer分割モジュール（constants, pattern, prompt, quality, thread, content, draft）
- `core/` — 共通基盤（logger, state_manager, safety, quality_gate, scheduler, boost, notifier, fact_checker, account_context）
- `services/` — 外部API連携（threads_api, claude_client, token_manager）
- `config/` — 設定ファイル（settings.yaml, schedule.yaml, tone.yaml, ng_words.txt, ugc_templates.yaml）
- `knowledge/` — コンテンツナレッジ（posting_rules.md, hook_stock.json）
- `prompts/` — エージェント用プロンプト
- `data/state/` — 状態JSON（git管理外）
- `data/archive/` — 90日超投稿のアーカイブ（自動生成）
- `data/backups/` — 日次バックアップzip（30世代保持）
- `scripts/` — 運用スクリプト（dashboard, backup, review, kill_switch, telegram_bot）
- `tests/` — テスト（275件）
- `docs/` — 設計資料

## 設計資料
- `docs/DESIGN_V1_archived.md` — V1全体設計（アーカイブ済み）
- `docs/DESIGN_V2.md` — V2詳細設計書（16セクション、匿名物知り系・1アカウント方針）
- `docs/DESIGN_V3.md` — V3運用堅牢化設計（トークン自動更新・通知・アーカイブ・E2Eテスト）
- `docs/ASSESSMENT.md` — 現段階評価レポート
- `docs/GROWTH_DESIGN.md` — 成長戦略詳細設計（5施策の技術設計）
- `docs/API_INTERFACES.md` — モジュール間インターフェース定義
- `docs/QUICKSTART.md` — セットアップ・運用ガイド
- `docs/RUNBOOK.md` — 運用手順書

## 実行方法
```bash
python -m core.scheduler <agent名>                  # 個別実行
python -m core.scheduler all                         # daemon一括実行
python scripts/review.py                             # 下書きレビュー（対話式）
python scripts/review.py list                        # レビュー待ち一覧
python scripts/review.py expire                      # タイムアウト処理
python scripts/dashboard.py                          # ダッシュボード表示
python scripts/dashboard.py --watch                  # 30秒自動更新
python scripts/backup.py                             # 手動バックアップ
python scripts/telegram_bot.py                       # Telegram Botサーバ起動
python scripts/kill_switch.py stop "理由"            # 緊急停止
pytest tests/ -v                                     # テスト（271件）
```

## 重要な設計原則
1. エージェント分離（1エージェント=1タスク）
2. ナレッジとロジックの分離（口調・パターン・NGワードは外部ファイル）
3. 状態管理（全てJSON、アトミック書き込み、ファイルロック対応）
4. 多重安全装置（NGワード→類似度→品質スコア→投稿間隔→日次上限→Circuit Breaker→KILL_SWITCH）
5. 薬機法・ステマ規制の遵守
6. Gmail通知（アラート・日次レポート。設定: GMAIL_USER / GMAIL_APP_PASSWORD）
7. データ耐久性（90日アーカイブ・日次バックアップ・JSON破損復旧）
8. 1アカウント優先 — まず@seibun_loveの単体運用を安定させてからマルチアカウント展開に進む。マルチアカウント関連の実装は単体運用が軌道に乗るまで着手しない
