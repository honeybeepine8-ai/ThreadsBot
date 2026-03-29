# 成長戦略 詳細設計書

> 2026-03-26作成 — 7人チームブレストに基づく5施策の技術設計

---

## 施策1: Replierエージェント（自動リプライ）

### 1.1 概要

フォロワーからのコメントに「ゆう」のペルソナで自動返信するエージェント。
エンゲージメント率を2-3倍に引き上げ、返信内のプロフ誘導でアフィリCVRも向上させる。

### 1.2 データフロー

```
Threads API (GET /{media_id}/replies)
    │
    ▼
┌──────────┐   reply_state.json   ┌──────────────┐
│ Replier  ├─────────────────────►│ Claude (生成) │
└────┬─────┘                      └──────┬───────┘
     │                                    │
     │ 返信済みをマーク                      │ 返信テキスト
     ▼                                    ▼
reply_state.json              Threads API (create_text_post)
```

### 1.3 新規ファイル

| ファイル | 用途 |
|---------|------|
| `agents/replier.py` | Replierエージェント本体 |
| `prompts/replier.md` | 返信生成用システムプロンプト |
| `data/state/reply_state.json` | 処理済みコメントの追跡 |

### 1.4 `agents/replier.py` 設計

```python
class ReplierAgent(BaseAgent):
    """Fetch follower comments and reply as 'ゆう'."""

    REPLY_RATE = 0.65          # 全コメントの65%に返信（残りは「いいね」のみ）
    MIN_REPLY_DELAY_SEC = 300  # 返信までの最短遅延: 5分
    MAX_REPLY_DELAY_SEC = 7200 # 返信までの最長遅延: 2時間
    MAX_REPLIES_PER_RUN = 5    # 1回の実行で返信する最大数
    PROFILE_CTA_RATE = 0.3     # 返信にプロフ誘導CTAを含める確率

    def execute(self):
        # 1. post_history から直近48時間の投稿を取得
        # 2. 各投稿のコメントをThreads APIで取得
        # 3. reply_state.json で未処理のコメントをフィルタ
        # 4. REPLY_RATE に基づき返信対象を選定
        # 5. 各コメントに対して:
        #    a. 遅延タイマーをチェック（即時返信しない）
        #    b. Claude でペルソナ口調の返信を生成
        #    c. PROFILE_CTA_RATE に基づきプロフ誘導文を追加
        #    d. NG ワードチェック
        #    e. Threads API で返信投稿
        #    f. reply_state.json に記録
```

### 1.5 `data/state/reply_state.json` スキーマ

```json
{
  "last_updated": "2026-03-26T12:00:00+09:00",
  "processed_comments": {
    "<comment_media_id>": {
      "post_id": "post_20260326_001",
      "action": "replied",           // "replied" | "liked" | "skipped"
      "replied_at": "2026-03-26T12:30:00+09:00",
      "reply_media_id": "18901234567890",
      "reply_text": "ありがとう！...",
      "had_profile_cta": true
    }
  },
  "daily_reply_count": {
    "2026-03-26": 8
  }
}
```

### 1.6 `prompts/replier.md` 設計

```markdown
あなたは「ゆう」としてフォロワーのコメントに返信します。

## ルール
- 一人称「わたし」、友達に返すようなタメ口寄り丁寧語
- 相手のコメント内容に具体的に触れること（汎用的な返答禁止）
- 1返信は50〜150文字
- 絵文字は0〜1個
- 質問には正確に答える。わからなければ正直に「ちょっと調べてみるね」
- 薬機法違反表現は絶対禁止

## プロフ誘導CTA（指示された場合のみ追加）
- 「プロフにまとめてるから見てみて→」
- 「プロフのリンクに詳しく書いてあるよ」
- 自然な文脈でのみ使用。押し売り感を出さない
```

### 1.7 Threads API 拡張（`services/threads_api.py` に追加）

```python
def get_post_replies(self, media_id: str) -> list[dict]:
    """投稿に対するコメント一覧を取得。

    Returns:
        [{"id": "...", "text": "...", "username": "...",
          "timestamp": "...", "from": {"id": "..."}}]
    """
    raw = self._request(
        "GET",
        f"/{media_id}/replies",
        params={"fields": "id,text,username,timestamp"},
    )
    return raw.get("data", [])
```

### 1.8 安全設計

| 制約 | 値 | 理由 |
|------|-----|------|
| 日次返信上限 | 20件 | bot検知回避 |
| 返信遅延 | 5分〜2時間 | 人間らしさ |
| 返信率 | 65% | 全コメ返信はbot臭い |
| NG コメントスキップ | — | スパム・攻撃的コメントは無視 |
| 自分のコメントスキップ | — | アフィリコメントに返信しない |
| 同一ユーザー連続返信 | 最大2回/日 | ストーカー的にならない |

### 1.9 settings.yaml 追加項目

```yaml
# --- リプライヤー ---
replier:
  run_interval_minutes: 30
  reply_rate: 0.65
  min_reply_delay_seconds: 300
  max_reply_delay_seconds: 7200
  max_replies_per_run: 5
  max_daily_replies: 20
  profile_cta_rate: 0.3
  scan_hours: 48              # 直近N時間の投稿をスキャン
  skip_own_replies: true
  max_replies_per_user_per_day: 2
```

### 1.10 scheduler.py 登録

```python
_AGENT_REGISTRY["replier"] = ("agents.replier", "ReplierAgent")

# daemon: 30分間隔
# "replier": ("replier", "run_interval_minutes"),
```

---

## 施策2: プロフLP誘導 + 後乗せアフィリ方式

### 2.1 概要

現状の「投稿生成時にアフィリコメントを同時作成」方式から
「バズった投稿に事後的にアフィリコメントを追加」方式に切り替える。
加えて、投稿本文にプロフィールへの誘導CTAを自然に組み込む。

### 2.2 変更方針

**A. 後乗せアフィリ方式（新機能）**

Fetcher がメトリクスを取得した際に「バズ判定」を行い、
閾値を超えた投稿に対して自動的にアフィリコメントを追加する。

```
Fetcher (メトリクス取得)
    │
    ├── views >= buzz_threshold?
    │   ├── YES → アフィリコメント生成 → Threads API で自己リプライ
    │   └── NO  → 通常通り
    │
    ▼
performance.json 更新
```

**B. プロフ誘導CTA（Writer修正）**

投稿本文の末尾に、一定確率でプロフィールへの誘導文を追加。

### 2.3 Writer 変更点

```python
# writer.py の _build_prompt() に追加
PROFILE_CTA_TEMPLATES = [
    "プロフにおすすめまとめてるよ→",
    "気になる人はプロフ見てみてね",
    "もっと知りたい人→プロフにリンクあるよ",
]

# _generate_single_post() 内:
# - pr_ratio に基づくアフィリコメント生成を廃止
# - 代わりに profile_cta_rate (0.35) でCTA文を本文末尾に追加
```

### 2.4 Fetcher 変更点（後乗せロジック追加）

```python
# fetcher.py に追加
class FetcherAgent(BaseAgent):
    BUZZ_THRESHOLDS = {
        "views_min": 500,        # 最低閾値
        "engagement_rate_min": 0.05,  # エンゲージメント率5%以上
    }

    def _check_buzz_and_add_affiliate(self, post, insights):
        """バズ投稿にアフィリコメントを後乗せ。"""
        # 1. 既にアフィリコメント付きならスキップ
        # 2. バズ判定（views >= 500 AND engagement_rate >= 5%）
        # 3. Claude でカテゴリに合ったアフィリコメントを生成
        # 4. PR ラベル付与
        # 5. Threads API で自己リプライ投稿
        # 6. post_history にアフィリ追加を記録
```

### 2.5 設定変更

```yaml
# settings.yaml 変更
writer:
  pr_ratio: 0.0               # 旧方式を無効化 (0.3 → 0.0)
  profile_cta_rate: 0.35       # 投稿の35%にプロフ誘導CTA追加

# 新規追加
affiliate:
  mode: "retroactive"          # "inline"(旧) | "retroactive"(後乗せ)
  buzz_views_threshold: 500
  buzz_engagement_threshold: 0.05
  max_affiliate_per_day: 5
  cooldown_hours: 6            # 同一投稿への重複追加防止
```

### 2.6 post_history.json スキーマ拡張

```json
{
  "posts": [{
    // ...既存フィールド...
    "affiliate_added_at": null,       // 後乗せアフィリの投稿日時
    "affiliate_media_id": null,       // アフィリコメントのmedia_id
    "profile_cta_included": false     // プロフCTAが含まれているか
  }]
}
```

---

## 施策3: 初期ブースト戦略（2週間モード）

### 3.1 概要

アカウント開設後の最初の2週間は通常運用と異なる特別モードで稼働。
0→1,000フォロワーの速度を最大化する。

### 3.2 モード切り替え

```yaml
# settings.yaml に追加
boost_mode:
  enabled: true
  start_date: "2026-04-01"    # ブーストモード開始日
  duration_days: 14
  # 以下、ブースト中のみ有効な上書き設定
  overrides:
    safety:
      max_daily_posts: 15      # 通常10 → 15に引き上げ
      min_post_interval_minutes: 60  # 通常90 → 60に短縮
    writer:
      pr_ratio: 0.0            # アフィリ完全停止（信頼構築期間）
      profile_cta_rate: 0.0    # CTAも停止
    schedule:
      # 水木だけでなく全日4投稿
      daily_post_count:
        monday: 4
        tuesday: 4
        wednesday: 5
        thursday: 5
        friday: 4
        saturday: 3
        sunday: 3
    content:
      # バイラル系パターンに集中（70%）
      viral_pattern_boost: 0.7
      viral_patterns:
        - "コメント誘導型"
        - "あるある共感型"
        - "二択・比較型"
        - "暴露・裏話系"
        - "反常識型"
        - "需要確認型"
```

### 3.3 実装変更点

**A. safety.py — ブーストモード判定**

```python
class SafetyGuard:
    def _get_effective_config(self) -> dict:
        """boost_mode が有効なら overrides を適用した設定を返す。"""
        base_config = self._load_safety_config()
        boost = self._load_boost_config()

        if not boost.get("enabled"):
            return base_config

        start = date.fromisoformat(boost["start_date"])
        end = start + timedelta(days=boost["duration_days"])

        if start <= date.today() < end:
            # overrides で上書き
            overrides = boost.get("overrides", {}).get("safety", {})
            return {**base_config, **overrides}

        return base_config
```

**B. writer.py — バイラルパターン優先**

```python
def _select_pattern(self):
    # ブーストモード中は viral_patterns を 70% で優先選択
    if self._is_boost_mode():
        boost_cfg = self._load_boost_config()
        viral_patterns = boost_cfg["overrides"]["content"]["viral_patterns"]
        viral_boost = boost_cfg["overrides"]["content"]["viral_pattern_boost"]

        available = [p for p in _ALL_PATTERNS if p not in recent_patterns]
        viral_available = [p for p in available if p in viral_patterns]

        if viral_available and random.random() < viral_boost:
            return random.choice(viral_available)

    # 通常のパターン選択ロジック
    ...
```

**C. Outbound Engagement（手動 → 将来自動化）**

ブースト期間中は手動で以下を実施（Phase2で自動化検討）：
- 同ジャンルアカウントへの積極的なリプライ
- 関連投稿の引用リポスト
- ハッシュタグ活用の強化

### 3.4 ブーストモード自動終了

```python
# supervisor.py に追加
def _check_boost_mode_expiry(self):
    """ブーストモード期限切れを検知し、自動で通常モードに戻す。"""
    boost = self._load_boost_config()
    if not boost.get("enabled"):
        return

    end_date = date.fromisoformat(boost["start_date"]) + timedelta(days=boost["duration_days"])
    if date.today() >= end_date:
        # settings.yaml の boost_mode.enabled を false に更新
        self._disable_boost_mode()
        self.logger.info("Boost mode expired. Switched to normal mode.")
```

---

## 施策4: UGCテンプレ + ハッシュタグチャレンジ

### 4.1 概要

フォロワーが自分バージョンを投稿したくなる「テンプレ投稿」を定期的に作成。
ハッシュタグで追跡し、参加者の投稿を引用リポストすることで拡散の連鎖を生む。

### 4.2 新規投稿パターン追加（16番目）

```
#### パターン16: UGCテンプレ型

**構成テンプレート:**
[フック: 参加を促す一言]
[テンプレ本文: 空欄をフォロワーが埋める形式]
[ハッシュタグ: #ゆうチャレンジ + カテゴリタグ]

**美容例:**
わたしの朝スキンケア3ステップ

① ___（洗顔）
② ___（化粧水）
③ ___（保湿）

みんなのも知りたい→
引用リポストで教えて

#ゆうチャレンジ #朝スキンケア
```

### 4.3 UGCサイクル設計

```
Week N (金曜 evening):
  Writer → UGCテンプレ投稿を生成・キュー追加
  Poster → テンプレ投稿を公開

Week N (土〜木):
  UGCTracker → #ゆうチャレンジ の引用リポストを検索
  Replier → 参加者の投稿に感謝リプライ

Week N+1 (月曜 morning):
  Writer → 「先週のチャレンジまとめ」投稿を生成
  → 参加者の投稿を紹介（引用リポスト）
```

### 4.4 UGCTracker 機能（Fetcher に統合）

```python
# fetcher.py に追加
def _scan_ugc_hashtag(self):
    """#ゆうチャレンジ のハッシュタグ検索。

    Threads API の hashtag search を使用。
    見つかった投稿を ugc_candidates.json に保存。
    """
    # Note: Threads API にはハッシュタグ検索エンドポイントがまだない
    # → 代替案: 自分の投稿への引用リポストを取得
    #   GET /{media_id}/insights?metric=quotes で数を確認
    #   → 数が増えたらログに記録し、手動で引用リポスト
```

### 4.5 UGCテンプレのバリエーション

```yaml
# config/ugc_templates.yaml (新規)
templates:
  - name: "朝スキンケア3ステップ"
    frequency: "weekly"
    schedule_day: "friday"
    hashtag: "#ゆうチャレンジ #朝スキンケア"
    template: |
      わたしの朝スキンケア3ステップ
      ① ___
      ② ___
      ③ ___
      みんなのも知りたい→引用で教えて

  - name: "今月のベストコスメ"
    frequency: "monthly"
    schedule_day: "last_friday"
    hashtag: "#ゆうチャレンジ #ベストコスメ"
    template: |
      今月いちばんよかったコスメ
      🏆 ___
      理由→___
      みんなのベストも教えて

  - name: "やめてよかったスキンケア"
    frequency: "biweekly"
    hashtag: "#ゆうチャレンジ #やめてよかった"
    template: |
      やめたら肌よくなったこと
      ___
      みんなにもある？

  - name: "二択チャレンジ"
    frequency: "weekly"
    schedule_day: "wednesday"
    hashtag: "#ゆうチャレンジ"
    template: |
      どっち派？
      A: ___
      B: ___
      引用で理由も教えて
```

### 4.6 posting_rules.md 追加

```markdown
#### パターン16: UGCテンプレ型

**構成テンプレート:**
[参加を促すフック（1行目）]
[空欄付きテンプレート（フォロワーが自分バージョンを投稿できる形式）]
[CTA: 引用リポストでの参加を促す]
[ハッシュタグ: #ゆうチャレンジ + テーマタグ]

**使いどころ:** 週1回（金曜evening）。UGC連鎖によるバイラル拡散狙い。

**禁止事項:**
- テンプレートが複雑すぎる（3項目以内）
- 個人情報を求める内容
- 特定商品を強制する内容
```

### 4.7 Writer 変更点

```python
_ALL_PATTERNS.append("UGCテンプレ型")

# UGCテンプレ投稿は週1回固定スケジュール
def _should_generate_ugc(self) -> bool:
    """今日がUGCテンプレ投稿日かチェック。"""
    now = datetime.datetime.now(_JST)
    # 金曜日にUGCテンプレを1つ生成
    if now.weekday() == 4:  # Friday
        # 今日まだUGCテンプレを生成していないかチェック
        queue_data = self.state.load_json("post_queue.json")
        today = now.strftime("%Y-%m-%d")
        for item in queue_data.get("queue", []):
            if (item.get("pattern") == "UGCテンプレ型"
                and item.get("created_at", "").startswith(today)):
                return False
        return True
    return False
```

---

## 施策5: マルチアカウント（3ペルソナ）

### 5.1 概要

3つのペルソナを異なるアカウントで運用し、相互引用で拡散を加速する。

| アカウント | ペルソナ | ターゲット | 差別化 |
|-----------|---------|-----------|--------|
| ゆう | 28歳元BA女性 | 25-32歳OL | 実体験・プロ知識 |
| りく | 24歳理系院生♂ | 20-28歳男女 | 成分分析・論理的 |
| ひな | 32歳ワーママ | 28-38歳ママ層 | 時短・コスパ重視 |

### 5.2 アーキテクチャ変更

```
ThreadsBot/
├── config/
│   ├── settings.yaml           # グローバル設定
│   ├── accounts/               # ★新規: アカウント別設定
│   │   ├── yuu/                # ゆう
│   │   │   ├── tone.yaml
│   │   │   ├── schedule.yaml
│   │   │   └── .env            # THREADS_ACCESS_TOKEN, THREADS_USER_ID
│   │   ├── riku/               # りく
│   │   │   ├── tone.yaml
│   │   │   ├── schedule.yaml
│   │   │   └── .env
│   │   └── hina/               # ひな
│   │       ├── tone.yaml
│   │       ├── schedule.yaml
│   │       └── .env
│   └── cross_posting.yaml      # ★新規: 相互引用ルール
│
├── data/
│   ├── yuu/                    # ★アカウント別データ
│   │   ├── state/
│   │   └── analytics/
│   ├── riku/
│   └── hina/
```

### 5.3 設定ファイルのアカウント対応

```python
# core/account_context.py (新規)

class AccountContext:
    """アカウント固有の設定・状態を管理するコンテキスト。"""

    def __init__(self, account_name: str):
        self.account_name = account_name
        self.config_dir = PROJECT_ROOT / "config" / "accounts" / account_name
        self.data_dir = PROJECT_ROOT / "data" / account_name

        # アカウント固有の .env をロード
        load_dotenv(self.config_dir / ".env", override=True)

        self.tone_config = self._load_yaml("tone.yaml")
        self.schedule_config = self._load_yaml("schedule.yaml")

    def get_state_manager(self) -> StateManager:
        """アカウント固有のデータディレクトリを使う StateManager."""
        return StateManager(base_dir=self.data_dir / "state")
```

### 5.4 CLI インターフェース拡張

```bash
# アカウント指定で実行
python -m core.scheduler --account yuu poster
python -m core.scheduler --account riku writer
python -m core.scheduler --account all all    # 全アカウント一括

# 相互引用の実行
python -m core.scheduler cross_post
```

### 5.5 scheduler.py 変更

```python
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("agent", help="Agent name or 'all'")
    parser.add_argument("--account", default="yuu", help="Account name")
    args = parser.parse_args()

    if args.account == "all":
        # 全アカウントを順次実行（タイミングをずらす）
        for account in ["yuu", "riku", "hina"]:
            ctx = AccountContext(account)
            _run_single_with_context(args.agent, ctx)
            time.sleep(300)  # 5分間隔でアカウント切り替え
    else:
        ctx = AccountContext(args.account)
        _run_single_with_context(args.agent, ctx)
```

### 5.6 相互引用エージェント

```python
# agents/cross_poster.py (新規)

class CrossPosterAgent(BaseAgent):
    """アカウント間の相互引用リポストを管理。"""

    CROSS_POST_RATE = 0.15      # 投稿の15%を相互引用対象に
    MIN_INTERVAL_HOURS = 24     # 同一ペア間の引用間隔

    def execute(self):
        # 1. 各アカウントの直近投稿から高エンゲージメント投稿を抽出
        # 2. 引用元アカウント≠引用先アカウントの組み合わせを選択
        # 3. 引用先のペルソナ口調で引用コメントを生成
        #    例: りく→ゆうの投稿を引用
        #    「成分的にもこれ正しくて、○○にはセラミドが...」
        # 4. Threads API で引用リポスト
```

### 5.7 りく・ひなのペルソナ設定

**config/accounts/riku/tone.yaml:**
```yaml
persona:
  name: "りく"
  display_name: "りく｜成分で読み解くスキンケア"
  age: 24
  background: "理系大学院生（有機化学専攻）。成分表が読める"

style:
  first_person: "僕"
  tone: "論理的だけど親しみやすい"
  use_endings:
    - "〜なんだよね"
    - "〜だと思う"
    - "〜って研究が出てる"
    - "〜が正解"
  avoid_endings:
    - "〜ですわ"
    - "〜でございます"
  preferred_emoji: ["🔬", "📊", "◎", "→"]
```

**config/accounts/hina/tone.yaml:**
```yaml
persona:
  name: "ひな"
  display_name: "ひな｜ズボラワーママのスキンケア"
  age: 32
  background: "2歳児ワーママ。時短命。コスパ重視"

style:
  first_person: "わたし"
  tone: "共感重視のママ友トーク"
  use_endings:
    - "〜だよね"
    - "〜なのよ"
    - "〜しか勝たん"
    - "〜でいいのよ"
  preferred_emoji: ["👶", "⏰", "◎", "→"]
```

### 5.8 IP/Bot検知対策

| 対策 | 実装方法 |
|------|---------|
| 投稿タイミング分散 | アカウント間で最低5分の間隔 |
| IP分散 | 将来的にプロキシ導入（初期は時間差で対応） |
| パターン分散 | アカウントごとに得意パターンを設定 |
| 相互引用頻度制限 | 1ペア1日1回まで |

---

## 実装順序まとめ

```
Phase 1 (Week 1-2): 設定変更 + Replier                    [DONE]
├── Day 1-2: settings.yaml / tone.yaml 修正                [DONE]
├── Day 3-5: Replier エージェント実装                       [DONE]
├── Day 5-7: Threads API 拡張（get_post_replies）           [DONE]
├── Day 7-8: 後乗せアフィリ（Fetcher拡張）                  [DONE]
└── Day 8-10: テスト + 調整                                 [DONE]

Phase 1.5: 収益化強化 + コンテンツ多様化 (2026-03-28)      [DONE]
├── 高単価アフィリ商品カタログ（affiliate_products.yaml）    [DONE]
├── 論争テーマホワイトリスト（debate_whitelist.yaml）        [DONE]
├── スレッド形式投稿 40% 比率（Writer+Poster拡張）          [DONE]
└── テスト追加（+22テスト、計139テスト）                     [DONE]

Phase 2 (Week 3-4): ブースト + UGC                         [DONE]
├── Day 1-3: ブーストモード実装                             [DONE]
├── Day 3-5: UGCテンプレパターン追加                        [DONE]
├── Day 5-7: Writer の UGC スケジュール統合                 [DONE]
└── Day 7-8: テスト + 調整                                  [DONE]

Phase 3 (Week 5-8): マルチアカウント                        [DONE]
├── Day 1-3: AccountContext 基盤                            [DONE]
├── Day 3-5: ディレクトリ構造リファクタ                      [DONE]
├── Day 5-7: りく / ひな ペルソナ設定                       [DONE]
├── Day 7-10: CrossPoster 実装                              [DONE]
└── Day 10-12: 統合テスト + IP対策                          [DONE]
```

---

## 施策6: 高単価アフィリ商品カタログ（Phase 1.5、2026-03-28追加）

### 6.1 概要

汎用PRコメント生成から、商品カタログベースのターゲティング推薦に移行。
カテゴリ+キーワードマッチでtier優先（high→medium→low）の商品を選択し、
プロフィールリンク誘導でCVR最大化。

### 6.2 新規ファイル

| ファイル | 用途 |
|---------|------|
| `config/affiliate_products.yaml` | 商品カタログ（5商品、tier/keywords/priority定義） |

### 6.3 変更ファイル

| ファイル | 変更内容 |
|---------|---------|
| `agents/fetcher.py` | `_select_product()`, `_load_product_catalog()`, `_get_weekly_product_count()`, `_record_product_usage()` 追加 |

### 6.4 マッチングロジック

1. カテゴリでフィルタ（投稿と商品のcategoryが一致）
2. キーワード重複数でスコアリング（min_keyword_overlap: 1）
3. tierボーナス加算（high=3, medium=2, low=1）
4. 週間推薦上限チェック（max_same_product_per_week: 3）
5. (overlap + tier_bonus, priority) の降順でベスト商品を選択

---

## 施策7: 論争テーマホワイトリスト（Phase 1.5、2026-03-28追加）

### 7.1 概要

「安全に論争できるテーマ」をホワイトリスト化し、コメント誘導率を最大化。
ホワイトリスト外の論争テーマには踏み込まない安全設計。

### 7.2 新規ファイル

| ファイル | 用途 |
|---------|------|
| `knowledge/debate_whitelist.yaml` | 8テーマ定義（stance/talking_points/ng_statements） |

### 7.3 変更ファイル

| ファイル | 変更内容 |
|---------|---------|
| `agents/writer.py` | `_maybe_inject_debate_theme()`, `_get_recent_debate_ids()` 追加 |
| `core/quality_gate.py` | `check_debate_ng()` 追加 |

### 7.4 テーマ一覧

| ID | テーマ | safety_level |
|----|--------|-------------|
| debate_001 | 無添加は本当に安全か？ | green |
| debate_002 | 高い化粧品 vs 安い化粧品 | green |
| debate_003 | オーガニック化粧品の実態 | green |
| debate_004 | 朝の洗顔は必要か？ | green |
| debate_005 | 化粧水は本当に必要か？ | yellow |
| debate_006 | スキンケアの順番問題 | green |
| debate_007 | レチノールは誰でも使うべきか？ | yellow |
| debate_008 | 日焼け止めの塗り直し問題 | green |

### 7.5 安全設計

- 対象パターンの40%で注入（debate_injection_rate: 0.4）
- 同一テーマ7日間クールダウン（min_days_between_same_debate: 7）
- 全テーマの ng_statements を quality_gate でチェック（green/yellow問わず）
- ホワイトリスト外テーマは投稿しない

---

## 施策8: スレッド形式投稿 40%比率（Phase 1.5、2026-03-28追加）

### 8.1 概要

投稿の40%を自己リプライ連結のスレッド形式（2-3ポスト）にし、
情報量の多いコンテンツでの保存率・リーチを最大化。

### 8.2 設定

```yaml
# settings.yaml
writer:
  thread_ratio: 0.40
  thread_min_posts: 2
  thread_max_posts: 3
```

### 8.3 パターン比率制御（Writer）

`_get_current_thread_ratio()` で直近20件+pending queueのスレッド比率を算出。
目標（40%）を下回る場合、`min(deficit * 2, 0.8)` の確率で「ツリー展開型」を優先選択。

### 8.4 スレッド生成（Writer）

`_generate_thread_post()` が Claude に `---THREAD_BREAK---` 区切りで
2-3ポスト分を生成させ、各ポストに品質チェック+NGワード検証+debate NGチェックを実施。

### 8.5 連結投稿（Poster）

キューの `thread_posts` リストにフォローアップポストを格納。
Poster が自己リプライチェーンで順番に投稿（5秒間隔でrate limit回避）。
post_history に `thread_media_ids` を記録。

### 8.6 投稿キュースキーマ拡張

```json
{
  "id": "q_20260328_001",
  "pattern": "ツリー展開型",
  "content": "1ポスト目（フック）",
  "thread_posts": ["2ポスト目（解説）", "3ポスト目（まとめ）"],
  "debate_id": "debate_001",
  "debate_title": "無添加は本当に安全か？",
  "affiliate_product_id": null
}
```
