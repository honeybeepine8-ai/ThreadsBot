# ThreadsBot V3 設計書 — 運用堅牢化

> V2評価スコア 86/100 → V3目標 92/100
> 最終更新: 2026-03-28

---

## 目次

1. [V3の目的](#1-v3の目的)
2. [現状の課題マップ](#2-現状の課題マップ)
3. [Phase 0: V2コード整理](#3-phase-0-v2コード整理week-1)
4. [Phase 1: 運用生命線](#4-phase-1-運用生命線week-1-2)
5. [Phase 2: 可観測性](#5-phase-2-可観測性week-2-3)
6. [Phase 3: データ耐久性](#6-phase-3-データ耐久性week-3)
7. [Phase 4: テスト強化](#7-phase-4-テスト強化week-3-4)
8. [Phase 5: コード健全化](#8-phase-5-コード健全化week-4)
9. [タスク依存関係図](#9-タスク依存関係図)
10. [リスクと判断基準](#10-リスクと判断基準)
11. [完了基準](#11-完了基準)

---

## 1. V3の目的

### 1.1 方針

V2で設計・安全・コンテンツ戦略は商用レベルに到達した。
V3は**新機能追加ゼロ**。運用で死なないための堅牢化に全振りする。

```
V2の達成: 「動くものを作った」
V3の達成: 「止まらない仕組みにした」
```

### 1.2 スコア目標

| 分野 | V2 | V3目標 | 差分 |
|------|-----|--------|------|
| アーキテクチャ設計 | 92 | 92 | 維持 |
| 安全設計 | 95 | 95 | 維持 |
| コード品質 | 85 | 90 | +5 |
| テスト | 88 | 93 | +5 |
| **運用準備度** | **75** | **92** | **+17** |
| コンテンツ戦略 | 88 | 88 | 維持 |
| **総合** | **86** | **92** | **+6** |

### 1.3 原則

- **新機能を追加しない** — 運用基盤だけを固める
- **動いているコードは壊さない** — リファクタは最小限
- **各タスクは独立してマージ可能** — 1タスク=1PR単位
- **テストが通らない状態を作らない** — 全フェーズでテストグリーン維持

---

## 2. 現状の課題マップ

V2評価レポート（`docs/ASSESSMENT.md`）から抽出した課題を、影響度×緊急度で分類する。

### 2.1 致命的（運用前に必須）

| # | 課題 | 影響 | 現状 |
|---|------|------|------|
| C1 | OAuthトークン自動更新 | トークン切れ＝全停止 | `services/token_manager.py` にステータス判定はある。自動リフレッシュAPIコールが未実装 |
| C2 | 外部通知（Telegram） | 障害に気づけない | DESIGN_V2で設計済み（セクション13.1）。`core/notifier.py` が未実装 |

### 2.2 高（運用1ヶ月以内に必要）

| # | 課題 | 影響 | 現状 |
|---|------|------|------|
| H1 | JSON肥大化対策 | post_history.json無限膨張→性能劣化 | アーカイブ機構なし |
| H2 | data/バックアップ | データ破損時の復旧手段なし | DESIGN_V2で設計済み（セクション13.2）。`scripts/backup.py` 未実装 |
| H3 | V2コード整理 | 不要コード(マルチアカウント等)が残存 | DESIGN_V2セクション16で削除対象定義済み |

### 2.3 中（運用3ヶ月以内に必要）

| # | 課題 | 影響 | 現状 |
|---|------|------|------|
| M1 | E2Eテスト | パイプライン全体のリグレッション防止 | 単体テスト182件は充実。統合テストなし |
| M2 | ダッシュボードCLI | 運用状況の即時把握 | DESIGN_V2で設計済み（セクション13.5）。未実装 |
| M3 | Writerリファクタ | 2,016行が肥大。保守性低下 | 動作には問題なし |
| M4 | DRY違反の解消 | JSON操作・バリデーションの重複 | 動作には問題なし |

---

## 3. Phase 0: V2コード整理（Week 1）

DESIGN_V2 セクション16の移行計画に基づく不要コード削除。

### タスク一覧

| ID | タスク | 変更ファイル | 工数 | 依存 |
|----|--------|-------------|------|------|
| P0-1 | マルチアカウント関連コード削除 | 下記参照 | 2h | なし |
| P0-2 | 削除に伴うテスト整理 | `tests/` | 1h | P0-1 |
| P0-3 | scheduler.pyの`--account`引数除去 | `core/scheduler.py` | 30min | P0-1 |
| P0-4 | CLAUDE.md更新（構成反映） | `CLAUDE.md` | 30min | P0-1 |

### P0-1 削除対象

```
削除:
  config/accounts/riku/          # りくアカウント設定
  config/accounts/hina/          # ひなアカウント設定
  agents/cross_poster.py         # クロスポスターエージェント
  core/account_context.py        # アカウントコンテキスト

削除テスト:
  tests/test_cross_poster.py     # クロスポストテスト
  tests/test_account_context.py  # アカウント切替テスト
```

### P0-2 テスト整理

- `test_cross_poster.py`、`test_account_context.py` を削除
- `conftest.py` から `account_context` 関連fixture除去
- `test_boost.py` のアカウント切替関連テストを確認・修正
- `pytest tests/ -v` で全テストパス確認

### 完了条件

- [ ] 削除対象ファイルがリポジトリから消えている
- [ ] `pytest tests/ -v` が全パス
- [ ] `python -m core.scheduler writer` が `--account` なしで正常動作
- [ ] テスト数: 182 - (test_cross_poster 10件 + test_account_context 13件) = **159件以上**パス

---

## 4. Phase 1: 運用生命線（Week 1-2）

**止まらないための最低限**。これが完了するまで本番運用を開始しない。

### タスク一覧

| ID | タスク | 変更ファイル | 工数 | 依存 |
|----|--------|-------------|------|------|
| P1-1 | OAuthトークン自動リフレッシュ | `services/token_manager.py`, `services/threads_api.py` | 4h | なし |
| P1-2 | Notifierクラス実装 | `core/notifier.py`（新規） | 3h | なし |
| P1-3 | Supervisor通知統合 | `agents/supervisor.py` | 2h | P1-2 |
| P1-4 | Poster/Fetcher通知統合 | `agents/poster.py`, `agents/fetcher.py` | 1h | P1-2 |
| P1-5 | トークン期限通知 | `services/token_manager.py` | 1h | P1-1, P1-2 |
| P1-6 | Notifierテスト | `tests/test_notifier.py`（新規） | 2h | P1-2 |
| P1-7 | トークンリフレッシュテスト | `tests/test_token_manager.py`（更新） | 1h | P1-1 |

### P1-1 OAuthトークン自動リフレッシュ

**現状**: `services/token_manager.py` にトークンステータス判定（valid/expiring_soon/expired）は実装済み。ただしリフレッシュAPIコールが未実装。

**実装内容**:

```python
# services/token_manager.py に追加

class TokenManager:
    REFRESH_WINDOW_DAYS = 30  # 期限30日前からリフレッシュ試行

    def refresh_if_needed(self) -> bool:
        """トークンがexpiring_soonなら自動リフレッシュ"""
        status = self.get_status()
        if status != "expiring_soon":
            return False

        new_token = self._refresh_token()
        if new_token:
            self._save_token(new_token)
            return True
        return False

    def _refresh_token(self) -> str | None:
        """Meta Graph APIのトークンリフレッシュエンドポイント呼び出し"""
        # GET /oauth/access_token
        #   ?grant_type=fb_exchange_token
        #   &client_id={app_id}
        #   &client_secret={app_secret}
        #   &fb_exchange_token={current_token}
        #
        # 成功時: {"access_token": "new_token", "token_type": "bearer", "expires_in": 5184000}
        # 失敗時: {"error": {...}}
        ...
```

**Meta Graph APIトークンリフレッシュ仕様**:

| パラメータ | 値 |
|-----------|-----|
| エンドポイント | `https://graph.facebook.com/v19.0/oauth/access_token` |
| メソッド | GET |
| grant_type | `fb_exchange_token` |
| client_id | `.env` の `META_APP_ID` |
| client_secret | `.env` の `META_APP_SECRET` |
| fb_exchange_token | 現在のアクセストークン |
| レスポンス | `{"access_token": "...", "token_type": "bearer", "expires_in": 5184000}` |

**リフレッシュ実行タイミング**:

- Supervisorの15分ごとの定期実行でチェック
- 期限30日前から毎回リフレッシュ試行
- 成功時: `token_state.json` 更新 + 通知（低重要度）
- 失敗時: リトライ（次回Supervisor実行時） + 通知（高重要度）
- 期限7日前で未リフレッシュ: 通知（最高重要度）

**必要な`.env`追加項目**:

```
META_APP_ID=your_app_id
META_APP_SECRET=your_app_secret
```

### P1-2 Notifierクラス実装

DESIGN_V2 セクション13.1の設計に基づく。

**実装方針**:

```python
# core/notifier.py

class Notifier:
    """通知送信クラス。Telegram Bot APIを使用"""

    def __init__(self, settings: dict):
        self.enabled = settings.get("notifications", {}).get("enabled", False)
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    def send(self, event_type: str, message: str, severity: str = "info") -> bool:
        """通知を送信。severity: info / warning / error / critical"""
        if not self.enabled or not self.bot_token:
            return False

        # 重複抑制: 同一event_typeは30分以内に再送しない
        if self._is_duplicate(event_type):
            return False

        formatted = self._format_message(event_type, message, severity)
        return self._send_telegram(formatted)

    def _send_telegram(self, text: str) -> bool:
        """Telegram Bot API sendMessage"""
        # POST https://api.telegram.org/bot{token}/sendMessage
        # body: {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        # リトライ: severity=error以上で最大3回（指数バックオフ）
        ...
```

**通知イベント（DESIGN_V2セクション13.1準拠）**:

| イベント | severity | 送信条件 |
|---------|----------|---------|
| draft_generated | info | Writer がdraft追加時 |
| circuit_breaker | error | CB発動時 |
| kill_switch | critical | 緊急停止時 |
| compliance_risk | error | コンプラリスクコメント検知時 |
| token_expiring | error | トークン期限7日前 |
| token_refreshed | info | トークンリフレッシュ成功時 |
| token_refresh_failed | critical | トークンリフレッシュ失敗時 |
| research_stale | warning | research_pool 24h未更新時 |
| daily_report | info | 日次KPIレポート |
| buzz_detected | warning | バズ検知時 |

**必要な`.env`追加項目**:

```
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

**`config/settings.yaml`追加項目**:

```yaml
notifications:
  enabled: true
  duplicate_suppress_minutes: 30
  retry_max: 3
  retry_delays: [10, 30, 60]
```

### P1-3 Supervisor通知統合

`agents/supervisor.py` の既存アラートロジックを `Notifier.send()` 呼び出しに接続する。

**変更内容**:

- `_send_alert()` メソッド内で `Notifier.send()` を呼ぶ
- エージェントヘルスチェック異常 → `severity: warning`
- Circuit Breaker発動 → `severity: error`
- Emergency Stop → `severity: critical`
- トークン期限チェック → `severity: error`
- 日次レポート生成 → `severity: info`

### P1-4 Poster/Fetcher通知統合

- Poster: 投稿失敗時に `severity: error` 通知
- Fetcher: バズ検知時に `severity: warning` 通知

### 完了条件

- [ ] トークンリフレッシュが `token_state.json` を正しく更新する（テスト）
- [ ] Telegram通知が送信される（手動テスト: テストメッセージ送信スクリプト）
- [ ] Supervisor実行時にトークン期限チェック→通知が動作する
- [ ] `pytest tests/ -v` 全パス（新規テスト含む）
- [ ] `.env.example` に新規項目追加済み

---

## 5. Phase 2: 可観測性（Week 2-3）

**何が起きているか分かるようにする。**

### タスク一覧

| ID | タスク | 変更ファイル | 工数 | 依存 |
|----|--------|-------------|------|------|
| P2-1 | ダッシュボードCLI | `scripts/dashboard.py`（新規） | 3h | なし |
| P2-2 | 日次レポート自動送信 | `agents/analyst.py` | 2h | P1-2 |
| P2-3 | Telegram killコマンド | `scripts/telegram_bot.py`（新規） | 3h | P1-2 |
| P2-4 | ダッシュボードテスト | `tests/test_dashboard.py`（新規） | 1h | P2-1 |

### P2-1 ダッシュボードCLI

DESIGN_V2 セクション13.5に設計済み。

**実装範囲**:

```bash
python scripts/dashboard.py           # 1回表示
python scripts/dashboard.py --watch   # 30秒ごと自動更新
```

**表示内容**:

```
=== ThreadsBot Dashboard ===
Status    : RUNNING (heartbeat: 2分前)
Today     : 3/6 posts | Next: 14:15 JST
Queues    : draft 4件 / post 2件 / research 18件

--- Agents ---
  researcher : 12:00 OK     (4h間隔)
  writer     : 13:15 OK     (キュー補充済)
  poster     : 14:00 OK     (次回 14:15)
  fetcher    : 08:00 OK     (6h間隔)
  analyst    : 05:00 OK     (日次)
  replier    : 14:15 OK     (30min間隔)
  supervisor : 14:30 OK     (15min間隔)

--- Safety ---
  Emergency Stop : inactive
  Circuit Breaker: inactive
  Token Expires  : 52 days
```

**データソース**: `system_state.json`, `post_queue.json`, `draft_queue.json`, `research_pool.json`, `token_state.json`

### P2-2 日次レポート自動送信

Analyst の日次実行（05:00）完了後に、KPIサマリーをTelegram通知する。

**送信内容**:

```
[Daily Report] 2026-03-28
投稿: 6件 | Avg Engagement: 4.2%
Top: skincare_ingredients (ER 6.1%)
Queue: draft 5件 / post 3件 / research 22件
Token: 52日
```

### P2-3 Telegram killコマンド

モバイルからの緊急停止/状態確認。

**コマンド**:

| コマンド | 処理 |
|---------|------|
| `/status` | dashboard.pyと同等の状態表示 |
| `/stop 理由` | `kill_switch.py stop` と同等 |
| `/clear` | `kill_switch.py clear` と同等 |

**実装方式**: Telegram Bot long polling。daemon起動時に別スレッドで実行。

### 完了条件

- [ ] `python scripts/dashboard.py` で状態が正しく表示される
- [ ] Analyst実行後に日次レポートがTelegramに届く
- [ ] Telegram `/status` コマンドでBotの状態が返る
- [ ] Telegram `/stop` で緊急停止が発動する

---

## 6. Phase 3: データ耐久性（Week 3）

**データが壊れない・溢れない仕組み。**

### タスク一覧

| ID | タスク | 変更ファイル | 工数 | 依存 |
|----|--------|-------------|------|------|
| P3-1 | post_historyアーカイブ機構 | `core/state_manager.py` | 3h | なし |
| P3-2 | バックアップスクリプト | `scripts/backup.py`（新規） | 2h | なし |
| P3-3 | JSON破損時の自動復旧 | `core/state_manager.py` | 2h | P3-2 |
| P3-4 | performance.jsonローテーション | `agents/analyst.py` | 1h | なし |
| P3-5 | アーカイブテスト | `tests/test_archive.py`（新規） | 2h | P3-1 |
| P3-6 | バックアップテスト | `tests/test_backup.py`（新規） | 1h | P3-2 |

### P3-1 post_historyアーカイブ機構

**問題**: `post_history.json` に全投稿が無制限に蓄積される。1日6投稿×365日=2,190件。JSONのパース・書き込みが遅くなる。

**解決策**: 90日超の投稿を月別アーカイブファイルに移動。

```
data/state/post_history.json          # 直近90日分のみ
data/archive/post_history_2026-01.json # 2026年1月分
data/archive/post_history_2026-02.json # 2026年2月分
```

**アーカイブロジック**:

```python
# core/state_manager.py に追加

class StateManager:
    ARCHIVE_THRESHOLD_DAYS = 90

    def archive_old_posts(self):
        """90日超の投稿をアーカイブ"""
        history = self.load("post_history")
        now = datetime.now(JST)
        threshold = now - timedelta(days=self.ARCHIVE_THRESHOLD_DAYS)

        keep = []
        archive_buckets = {}  # {YYYY-MM: [posts]}

        for post in history["posts"]:
            posted_at = parse_datetime(post["posted_at"])
            if posted_at >= threshold:
                keep.append(post)
            else:
                month_key = posted_at.strftime("%Y-%m")
                archive_buckets.setdefault(month_key, []).append(post)

        # アーカイブファイルに追記
        for month, posts in archive_buckets.items():
            archive_path = f"data/archive/post_history_{month}.json"
            existing = self._load_archive(archive_path)
            existing.extend(posts)
            self._save_archive(archive_path, existing)

        # 本体を更新
        history["posts"] = keep
        self.save("post_history", history)
```

**実行タイミング**: Analystの日次実行（05:00）の最後にアーカイブ処理を呼ぶ。

**類似度チェックへの影響**: `quality_gate.py` の類似度チェックは直近100件を参照。90日以内に収まるため影響なし。

### P3-2 バックアップスクリプト

DESIGN_V2 セクション13.2に設計済み。

```python
# scripts/backup.py

def main():
    """data/state/ を日次zip圧縮。30世代保持。"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path("data/backups")
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 1. data/state/ をzip圧縮
    zip_name = f"state_{timestamp}"
    shutil.make_archive(str(backup_dir / zip_name), "zip", "data/state")

    # 2. 古いバックアップを削除（30世代保持）
    backups = sorted(backup_dir.glob("state_*.zip"))
    for old in backups[:-30]:
        old.unlink()

    logger.info(f"Backup completed: {zip_name}.zip")
```

**実行方法**: Task Scheduler で毎日04:00に実行。

### P3-3 JSON破損時の自動復旧

**現状**: `state_manager.py` にデフォルトスキーマ fallback はある。ただし破損ファイルの保全がない。

**追加処理**:

```python
def load(self, name: str) -> dict:
    try:
        return self._load_json(path)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 1. 破損ファイルを .bak にリネーム保存
        bak_name = f"{path.stem}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}{path.suffix}"
        path.rename(path.parent / bak_name)
        logger.error(f"JSON破損検知: {path.name} → {bak_name} に退避。デフォルトで再作成")

        # 2. 通知（P1-2のNotifier経由）
        self.notifier.send("json_corrupted", f"{path.name} が破損。バックアップから復旧してください", "error")

        # 3. デフォルトスキーマで再作成
        return self._default_for(name)
```

### P3-4 performance.jsonローテーション

**問題**: `performance.json` の `records` 配列も無制限に膨張する。

**解決策**: Analyst実行時に90日超のレコードを削除（アーカイブ不要。post_historyにメトリクスが残るため）。

### 完了条件

- [ ] `archive_old_posts()` が90日超の投稿を月別ファイルに移動する（テスト）
- [ ] `scripts/backup.py` が `data/backups/` にzipを作成する
- [ ] JSON破損時に `.bak` が保存され、デフォルトで復旧する（テスト）
- [ ] performance.json の records が90日以内に絞り込まれる
- [ ] `pytest tests/ -v` 全パス

---

## 7. Phase 4: テスト強化（Week 3-4）

**パイプライン全体の信頼性を上げる。**

### タスク一覧

| ID | タスク | 変更ファイル | 工数 | 依存 |
|----|--------|-------------|------|------|
| P4-1 | E2E統合テスト（生成→投稿） | `tests/test_e2e_pipeline.py`（新規） | 4h | P0完了 |
| P4-2 | E2E統合テスト（計測→分析） | `tests/test_e2e_analytics.py`（新規） | 3h | P0完了 |
| P4-3 | Notifier統合テスト | `tests/test_notifier_integration.py`（新規） | 2h | P1-2 |
| P4-4 | アーカイブ統合テスト | `tests/test_archive_integration.py`（新規） | 2h | P3-1 |

### P4-1 E2E統合テスト（生成→投稿パイプライン）

**テストシナリオ**:

```python
# tests/test_e2e_pipeline.py

class TestPipelineE2E:
    """research_pool → Writer → draft_queue → (approve) → post_queue → Poster → post_history"""

    def test_happy_path(self, tmp_path, mock_claude, mock_threads_api):
        """正常フロー: リサーチネタから投稿完了まで"""
        # 1. research_pool にテストネタを配置
        # 2. Writer.execute() で投稿生成
        # 3. draft_queue に追加されたことを確認
        # 4. 自動承認（スコア8.5以上）でpost_queueに移動
        # 5. Poster.execute() で投稿
        # 6. post_history に記録されたことを確認
        # 7. research_pool のネタが used=true になったことを確認

    def test_quality_rejection(self, tmp_path, mock_claude):
        """品質不足で棄却されるフロー"""
        # mock_claudeが低スコアを返す → draft_queueに入らない

    def test_ng_word_block(self, tmp_path, mock_claude):
        """NGワードでブロックされるフロー"""
        # mock_claudeがNGワード含む文を返す → 即ブロック

    def test_safety_limit(self, tmp_path, mock_claude, mock_threads_api):
        """日次上限でPosterが投稿しないフロー"""
        # daily_counters を上限に設定 → Poster.execute()がスキップ
```

### P4-2 E2E統合テスト（計測→分析パイプライン）

**テストシナリオ**:

```python
# tests/test_e2e_analytics.py

class TestAnalyticsE2E:
    """post_history → Fetcher(1h/6h/24h) → performance.json → Analyst → audience.json"""

    def test_metrics_collection_stages(self, tmp_path, mock_threads_api):
        """3段階メトリクス収集"""
        # 1. post_historyに投稿を配置（1h前に投稿）
        # 2. Fetcher.execute() → 1hメトリクス取得
        # 3. 時刻を進めて再実行 → 6hメトリクス取得
        # 4. performance.jsonに記録確認

    def test_analyst_feedback_loop(self, tmp_path, mock_claude):
        """Analystのフィードバック生成"""
        # 1. performance.jsonにテストデータ配置
        # 2. Analyst.execute()
        # 3. audience.jsonのTOP5/BOTTOM3が正しく生成されたか確認
```

### 完了条件

- [ ] E2Eテストが生成→投稿の全フローをカバー
- [ ] E2Eテストが計測→分析の全フローをカバー
- [ ] 全テスト合計: **175件以上**パス（P0削除分を差し引いてもV2より多い）
- [ ] テスト実行時間: 10秒以内

---

## 8. Phase 5: コード健全化（Week 4）

**保守性の向上。動作は変えない。**

### タスク一覧

| ID | タスク | 変更ファイル | 工数 | 依存 |
|----|--------|-------------|------|------|
| P5-1 | Writer分割リファクタ | `agents/writer.py` → 複数ファイル | 4h | P4-1 |
| P5-2 | JSON操作の共通化 | `core/state_manager.py` | 2h | なし |
| P5-3 | .env.example更新 | `.env.example` | 30min | P1完了 |
| P5-4 | DESIGN.md（V1）アーカイブ | `docs/` | 15min | なし |

### P5-1 Writer分割リファクタ

**現状**: `agents/writer.py` が2,016行。責務が多すぎる。

**分割案**:

```
agents/
├── writer.py               # WriterAgent本体（execute, _generate_single_post）→ ~400行
├── writer_prompt.py         # プロンプト構築（_build_prompt, _inject_feedback）→ ~300行
├── writer_quality.py        # 品質評価（_evaluate_quality_score, _validate）→ ~400行
├── writer_pattern.py        # パターン選択（_select_pattern, _select_time_slot）→ ~300行
└── writer_thread.py         # スレッド生成（_generate_thread, _parse_thread）→ ~300行
```

**制約**:
- 外部インターフェース（`WriterAgent.execute()`）は変更しない
- 既存テスト(`test_writer_thread.py`)がそのまま通ること
- importパスの変更はテスト側で吸収

### P5-2 JSON操作の共通化

`state_manager.py` の `load()` / `save()` を全エージェントが直接呼ぶパターンを整理。重複しているバリデーションロジックを `StateManager` に寄せる。

### 完了条件

- [ ] `agents/writer.py` が500行以下
- [ ] 全既存テストがパス
- [ ] `.env.example` に全ての必要項目が記載されている

---

## 9. タスク依存関係図

```
Phase 0 (V2コード整理)
  P0-1 マルチアカウント削除
    ├── P0-2 テスト整理
    ├── P0-3 scheduler引数除去
    └── P0-4 CLAUDE.md更新

Phase 1 (運用生命線) ← P0完了後
  P1-1 OAuthリフレッシュ ──┬── P1-5 トークン期限通知
  P1-2 Notifier実装 ──────┤
    ├── P1-3 Supervisor通知  ├── P1-7 トークンテスト
    ├── P1-4 Poster/Fetcher通知
    └── P1-6 Notifierテスト

Phase 2 (可観測性) ← P1-2完了後
  P2-1 ダッシュボードCLI
  P2-2 日次レポート ← P1-2
  P2-3 Telegram killコマンド ← P1-2
  P2-4 ダッシュボードテスト ← P2-1

Phase 3 (データ耐久性) ← 独立
  P3-1 post_historyアーカイブ
  P3-2 バックアップスクリプト
  P3-3 JSON破損復旧 ← P3-2
  P3-4 performance.jsonローテーション
  P3-5 アーカイブテスト ← P3-1
  P3-6 バックアップテスト ← P3-2

Phase 4 (テスト強化) ← P0完了後
  P4-1 E2E（生成→投稿）
  P4-2 E2E（計測→分析）
  P4-3 Notifier統合テスト ← P1-2
  P4-4 アーカイブ統合テスト ← P3-1

Phase 5 (コード健全化) ← P4-1完了後
  P5-1 Writer分割
  P5-2 JSON操作共通化
  P5-3 .env.example更新
  P5-4 DESIGN.mdアーカイブ
```

**並行実行可能な組み合わせ**:

| 期間 | 作業A | 作業B |
|------|-------|-------|
| Week 1 | Phase 0 → Phase 1 (P1-1, P1-2) | Phase 3 (P3-1, P3-2) |
| Week 2 | Phase 1 (P1-3〜P1-7) | Phase 3 (P3-3〜P3-6) |
| Week 3 | Phase 2 | Phase 4 (P4-1, P4-2) |
| Week 4 | Phase 5 | Phase 4 (P4-3, P4-4) |

---

## 10. リスクと判断基準

### 10.1 リスク

| リスク | 確率 | 影響 | 緩和策 |
|--------|------|------|--------|
| Meta APIのトークンリフレッシュ仕様変更 | 低 | 高 | リフレッシュ失敗時の通知を最優先実装。手動更新フォールバックを残す |
| Telegram Bot APIの一時障害 | 中 | 中 | 通知失敗をログに記録。通知なしでも安全装置は独立して動作する設計 |
| Writer分割で予期しないリグレッション | 中 | 中 | Phase 4のE2Eテスト完了後にPhase 5を着手。テストで担保 |
| post_historyアーカイブで類似度チェックに影響 | 低 | 高 | アーカイブ閾値(90日)は類似度チェック範囲(100件≒約17日分)よりはるかに大きい |

### 10.2 判断基準: 本番運用開始の条件

以下が全て満たされたとき本番運用を開始する:

- [x] V2の全機能が実装済み（達成済み）
- [x] 182テスト全パス（達成済み）
- [ ] **Phase 0完了**: 不要コード削除済み
- [ ] **Phase 1完了**: トークン自動更新 + Telegram通知が動作
- [ ] **P3-2完了**: バックアップスクリプトが動作
- [ ] Threads APIトークンが有効（手動確認）
- [ ] 10件以上のテスト投稿を手動確認

Phase 2以降は本番運用と並行で進めてよい。

---

## 11. 完了基準

V3の完了基準。全Phase完了時に以下を満たす。

### コード

- [ ] マルチアカウント関連コードが全て削除されている
- [ ] `agents/writer.py` が500行以下に分割されている
- [ ] `.env.example` に全ての環境変数が記載されている
- [ ] 新規ファイル: `core/notifier.py`, `scripts/dashboard.py`, `scripts/backup.py`, `scripts/telegram_bot.py`

### テスト

- [ ] テスト合計: **185件以上**パス
- [ ] E2Eテスト: 生成→投稿、計測→分析の2パイプラインをカバー
- [ ] 新規テスト: notifier, archive, backup, dashboard, E2E
- [ ] テスト実行時間: 15秒以内

### 運用

- [ ] OAuthトークンが自動リフレッシュされる
- [ ] Telegram通知が全イベントで動作する
- [ ] Telegramから緊急停止/状態確認が可能
- [ ] `scripts/dashboard.py` で状態表示される
- [ ] `scripts/backup.py` で日次バックアップが動作する
- [ ] post_history.jsonが90日超で自動アーカイブされる
- [ ] JSON破損時に`.bak`退避+デフォルト復旧される

### ドキュメント

- [ ] `docs/ASSESSMENT.md` が最新の評価を反映
- [ ] `docs/DESIGN_V3.md` （本ドキュメント）が完成
- [ ] `CLAUDE.md` がV3の構成を反映
- [ ] `docs/RUNBOOK.md` にトークンリフレッシュ手順が追加

---

## Appendix: 工数サマリー

| Phase | タスク数 | 推定工数 | 内容 |
|-------|---------|---------|------|
| Phase 0 | 4 | 4h | V2コード整理 |
| Phase 1 | 7 | 14h | 運用生命線（トークン+通知） |
| Phase 2 | 4 | 9h | 可観測性（ダッシュボード+Telegram） |
| Phase 3 | 6 | 11h | データ耐久性（アーカイブ+バックアップ） |
| Phase 4 | 4 | 11h | テスト強化（E2E） |
| Phase 5 | 4 | 7h | コード健全化（Writer分割） |
| **合計** | **29** | **56h** | **約2週間（フルタイム）/ 4週間（パートタイム）** |
