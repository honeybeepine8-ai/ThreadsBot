## 5. バズ投稿1行目ストック（hook_stock）

### 5.1 仕組み

- バズった投稿の1行目を大量にストック（目標: 265件以上）
- ジャンル不問 — 構造だけ学習して自分のジャンル（スキンケア成分）に変換して生成
- ストックは `knowledge/hook_stock.json` に保存

### 5.2 ストック形式（鮮度管理付き）

```json
[
  {
    "id": "hook_001",
    "hook": "化粧水、手で塗るかコットンで塗るかより",
    "structure": "AよりBのほうが大事系",
    "structure_code": "comparison_redirect",
    "source": "threads",
    "engagement": "high",
    "added_at": "2026-03-20T00:00:00+09:00",
    "used_count": 3,
    "last_used_at": "2026-03-25T10:00:00+09:00",
    "last_performance": {
      "engagement_rate": 0.085,
      "post_id": "post_20260325_003"
    }
  }
]
```

### 5.3 構造タイプ（enum化）

| structure_code | 説明 | 例 |
|---|---|---|
| comparison_redirect | AよりBのほうが大事 | 「〇〇より△△のほうが100倍大事」 |
| limited_confession | 限定告白系 | 「正直1つしかない」 |
| counter_common | 常識への反論 | 「〇〇って言われてるけど、実は…」 |
| number_hook | 数字フック | 「3種類あるの知ってた？」 |
| question_denial | 質問+否定 | 「〇〇してる？ それ、意味ないよ」 |
| urgency | 緊急系 | 「今すぐ成分表見て」 |
| secret_reveal | 秘密暴露系 | 「誰も教えてくれない〇〇の話」 |
| before_after | ビフォアフ系 | 「〇〇やめたら肌変わった」 |
| ranking | ランキング系 | 「成分オタクが選ぶTOP3」 |
| relatable | あるある系 | 「〇〇な人、絶対いるよね」 |

### 5.4 ライターでの使い方

1. ストックからランダムに3-5件の構造を選択（used_countが少ないものを優先）
2. 「この構造をスキンケア成分の話に変換して1行目を書け」とプロンプト
3. 生成された1行目 + 本文をセットで品質採点

### 5.5 鮮度管理

- 3ヶ月以上未使用 or 直近の実績(engagement_rate)が平均の50%未満 → archive フラグ
- アナリストが「この構造が最近伸びてる/伸びてない」を月次レポートに含める
- X巡回エージェント（P3）でバズ投稿を自動収集し、hook_stock.jsonへの追加を半自動化
- `scripts/add_hook.py "フック文" "structure_code"` CLIツールを用意

---

## 6. 下書きレビュー（人間介入5%）

### 6.1 フロー全体図

```
Writer → 品質スコア7.5以上 → draft_queue.json に追加
                                    │
                              ┌─────▼─────────────┐
                              │  自動承認ルール判定  │
                              │                    │
                              │ スコア8.5以上       │ → 自動で post_queue に移動
                              │ スコア7.5-8.4       │ → レビュー待ち（通知送信）
                              │ Level 2 NGワード    │ → review_required=true
                              │ fact_check_required │ → review_required=true
                              └────────┬───────────┘
                                       │ レビュー待ちのもの
                              ┌────────▼────────┐
                              │   人間が確認      │
                              │                  │
                              │ "approve"        │ → post_queue.json に移動
                              │ "reject"         │ → 破棄（理由コード+テキスト記録）
                              │ "edit"           │ → 編集後 post_queue に移動
                              └──────────────────┘
                                       │
                              ┌────────▼────────┐
                              │ タイムアウト       │
                              │ 12時間未レビュー:  │
                              │   スコア8.0以上    │ → 自動承認
                              │   スコア7.5-7.9   │ → 自動破棄
                              └──────────────────┘
```

### 6.2 draft_queue.json スキーマ（詳細）

```json
{
  "drafts": [
    {
      "id": "d_20260326_001",
      "content": "セラミドって3種類あるの知ってた？...",
      "pattern": "短文完結型",
      "category": "skincare_ingredients",
      "hook_structure": "number_hook",
      "quality_score": 8.2,
      "quality_details": {
        "content_quality": {
          "usefulness": 9,
          "specificity": 8,
          "empathy": 7
        },
        "expression_quality": {
          "naturalness": 8,
          "tempo": 9,
          "experiential": 8,
          "non_commercial": 9
        },
        "content_quality_avg": 8.0,
        "expression_quality_avg": 8.5
      },
      "similarity_score": 0.42,
      "ng_check": {
        "level1_hits": [],
        "level2_hits": [],
        "level3_hits": []
      },
      "flags": {
        "auto_approved": false,
        "review_required": false,
        "fact_check_required": false,
        "pharma_law_check": "pass"
      },
      "research_id": "r_20260325_042",
      "created_at": "2026-03-26T10:00:00+09:00",
      "expires_at": "2026-03-26T22:00:00+09:00",
      "status": "pending_review",
      "review": null
    }
  ],
  "stats": {
    "total_generated": 150,
    "auto_approved": 45,
    "human_approved": 80,
    "human_rejected": 15,
    "human_edited": 10,
    "expired_approved": 0,
    "expired_discarded": 0
  }
}
```

### 6.3 reject理由コード（enum化）

| reason_code | 説明 |
|---|---|
| weak_hook | 1行目が弱い |
| off_topic | テーマがずれている |
| too_generic | 具体性不足 |
| bad_timing | 今出すべき内容ではない |
| similar_recent | 直近の投稿と似すぎ |
| compliance_risk | 法令違反リスク |
| bot_feeling | bot臭い |
| factual_error | 事実誤認の可能性 |

### 6.4 レビューインターフェース（マルチチャネル）

**A. CLI（メイン）**: `python scripts/review_drafts.py`

- 下書きを一覧表示 → 1件ずつ approve / reject(理由コード選択) / edit
- 編集時はエディタが開く（$EDITOR）

**B. Telegram Bot（モバイル対応）**:

- draft_queue に新しい投稿が入ったら Telegram に通知
- メッセージ内にインラインボタン: [OK] [NG] [あとで]
- [OK]タップで即承認
- [NG]タップ後に理由コードを選択
- edit操作はCLIにフォールバック

**C. フォールバック設計**:

- 12時間レビューされなかった場合:
  - スコア8.0以上 → 自動承認（auto_approved=true）
  - スコア7.5-7.9 → 自動破棄
- これにより「人間が忙しくて何もレビューしない日」でも運用が止まらない

### 6.5 レビュー結果の活用

- reject理由コードの頻度分布をAnalystが月次集計
- edit差分（original_content vs edited_content）を蓄積 → 「人間の修正傾向レポート」をWriterプロンプトに反映
- reject率が高い投稿パターン・カテゴリを自動検知 → Writerの生成優先度を調整

### 6.6 運用の具体像

- 1日2回の固定レビュータイム: 朝8時と夜21時
- Writerが夜間にバッチ生成 → 朝のレビューで承認 → 日中に順次投稿
- 1回のレビュー所要時間: 約5-10分（3-5件）
- 品質スコア8.5以上は自動承認されるため、実質レビュー対象は1日2-3件

---

## 7. フィードバックループ（自動改善サイクル）

### 7.1 メトリクス3段階計測

| タイミング | 目的 | 取得データ | アクション |
|---|---|---|---|
| 投稿1時間後 | 初速チェック（バズの兆候） | views, likes, replies | 初速テーブルに基づくアクション |
| 投稿6時間後 | 中間計測 | views, likes, replies, reposts, quotes | パフォーマンス初期評価 |
| 投稿24時間後 | 最終計測 | 全メトリクス確定 | engagement_rate算出、performance.jsonに記録 |

※ saves（保存数）はThreads APIで取得不可のため、計測対象外

**メトリクス時系列保存構造（post_history.json内）:**

```json
{
  "metrics": {
    "stages": {
      "1h": {"views": 50, "likes": 5, "replies": 2, "fetched_at": "..."},
      "6h": {"views": 200, "likes": 20, "replies": 5, "reposts": 3, "quotes": 1, "fetched_at": "..."},
      "24h": {"views": 500, "likes": 45, "replies": 12, "reposts": 8, "quotes": 3, "fetched_at": "..."}
    },
    "current_stage": "24h",
    "engagement_rate": 0.136
  }
}
```

**Fetcherのステートマシン:**

```
投稿直後 → [wait 1h] → 1h計測 → [wait 5h] → 6h計測 → [wait 18h] → 24h計測 → 完了
```

各投稿に `metrics.current_stage` フィールドで管理。Fetcher実行時、各投稿の「次に取得すべき段階」を判定して実行。

**1h初速チェックに基づくアクション表:**

| 初速パターン | 判定 | アクション |
|---|---|---|
| views 500+ かつ engagement 5%+ | バズ兆候 | 次の投稿を3h以上空ける。Replier即時起動。通知 |
| views 100-500, engagement 3%+ | 好調 | 通常運用 |
| views 100未満 | 低調 | 同テーマ・同構造の投稿を当日は控える |
| replies 10+ | 議論発生 | Replier即時起動して会話を盛り上げる |

### 7.2 TOP5 + BOTTOM3 プロンプト注入

**TOP5選出ルール（過学習防止）:**

- engagement_rateでソート
- 同一カテゴリから最大2件まで（stratified sampling）
- 直近30日以内の投稿から選出

**BOTTOM3選出ルール:**

- engagement_rateが最低の3件
- 「避けるべきパターン」の学習材料

**プロンプト注入フォーマット:**

```
## 過去に伸びた投稿TOP5（構造を参考にせよ）

1. [engagement_rate: 12.3% | category: skincare_ingredients | pattern: 反常識型]
   「セラミドって3種類あるの知ってた？...」
   → 分析: 具体的な成分名 + 二項対立が刺さった

2. ...

## 過去に伸びなかった投稿BOTTOM3（この構造は避けよ）

1. [engagement_rate: 0.8% | category: beauty_trend | pattern: リスト系]
   → 分析: 抽象的すぎて具体性がなかった

## 最近の傾向
- 断言系の1行目が伸びている
- 「〇〇って知ってた？」系はやや飽きられ始め

## 人間レビューの傾向
- reject理由で最も多いのは「weak_hook」
- edit で最も修正されるのは1行目の言い回し
```

**トークン数への配慮:**

- TOP5+BOTTOM3は構造要約（各3行以内）で注入
- 推定追加トークン: 約500-800トークン

### 7.3 リサーチャーへのフィードバック

| 分析結果 | リサーチャーのアクション |
|---|---|
| テーマ飽和 | 別テーマを重点調査 |
| 見分け方系が伸びてる | 見分け方ネタを優先収集 |
| カテゴリストック不足 | テーマツリー照合で補充 |
| カテゴリのreject率高い | そのカテゴリの研究深掘り |

### 7.4 多様性モニタリング

- Analystが「直近30投稿のカテゴリ分布エントロピー」を算出
- エントロピーが閾値を下回ったらアラート
- アラート時: Writerがカテゴリ分散を強制

---

## 8. リサーチソース

### 8.1 YouTube（既存・改善）

- YouTube Data API v3
- 動画の文字起こし（youtube-transcript-api）を読み、使えるネタだけ抽出
- Claudeで要約 → topic, summary, keywords, category を構造化
- テーマツリー照合: 不足テーマのキーワードで優先検索
- 実行間隔: 4時間

### 8.2 X（新規・P3）

- X API v2 Basic tier（$100/月）
- スキンケア関連バズポスト（いいね1000+）を巡回
- 投稿内容はパクらない — 構造だけ分析:
  - 「この型が今伸びてる」レポート生成
  - 1行目のフックを hook_stock.json に追加（半自動）
- 実行間隔: 6時間

### 8.3 Instagram（新規・P3）

- Instagram Graph API（Meta Business Suite認証）
- スキンケア関連ハッシュタグのトレンド分析
- テーマのみ抽出（画像/動画分析はしない）
- 実行間隔: 12時間

### 8.4 トレンドスキャンモード

- テーマツリー照合とは別に、2回に1回は「トレンドスキャン」モードで実行
- 急上昇キーワード検出 → beauty_trendカテゴリに自動追加
- テーマツリー自体を動的更新可能に
