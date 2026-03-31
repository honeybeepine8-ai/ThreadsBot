# ThreadsBot V3.1 設計書 — コンテンツ品質強化

> V3目標 92/100 → V3.1目標 97/100
> 最終更新: 2026-03-31
> 前提: V3（運用堅牢化）が完了済みであること

---

## 位置づけ

```
V3: 「止まらない仕組みにした」（堅牢化）
V3.1: 「バズる仕組みにした」（コンテンツ品質強化）
```

V3.1では以下の2種類の課題を扱う。

1. **矛盾修正（Fixes）** — 現在のコードと設計意図が食い違っており、機能が意図通りに動いていない箇所
2. **追加フェーズ（Phase 6〜8）** — 参照アーキテクチャとの差分を埋める新機能

---

## 目次

1. [V3.1の課題マップ](#1-v31の課題マップ)
2. [矛盾修正（Fixes）](#2-矛盾修正fixes)
3. [Phase 6: コンテンツ品質強化](#3-phase-6-コンテンツ品質強化)
4. [Phase 7: ネタ収集深化](#4-phase-7-ネタ収集深化)
5. [Phase 8: バズ拡張機能](#5-phase-8-バズ拡張機能)
6. [タスク依存関係](#6-タスク依存関係)
7. [スコア目標](#7-スコア目標)
8. [完了基準](#8-完了基準)

---

## 1. V3.1の課題マップ

### 1.1 矛盾（現在の実装が設計意図を達成できていない）

| # | 課題 | 影響 | 詳細 |
|---|------|------|------|
| **F1** | フックストックが実質空 | **致命的** | `knowledge/hook_stock.json` にエントリが1件のみ。`_generate_hook_candidates()` のA/Bテストが機能していない |
| **F2** | コメント誘導型の追いコメが未実装 | 高 | `agents/poster.py` はアフィリエイトコメントのself-replyのみ実装。コメント誘導型パターンの「続きをコメントに書く」挙動がない |
| **F3** | テーマ連続チェックがない | 中 | パターンの直近3件ブロックはあるが、同一トピック（例:「セラミド」）が3連続した場合の切り替えロジックがない |

### 1.2 不足（設計に存在せず、追加が必要な機能）

| # | 課題 | 優先度 | 詳細 |
|---|------|--------|------|
| **G1** | 投稿パターン数が不足（9種→15種目標） | 高 | `knowledge/posting_rules.md` のパターン9種では多様性が不足。15種以上への拡充が必要 |
| **G2** | 品質採点項目が少ない（7項目→10項目） | 中 | `agents/writer_quality.py` の採点基準7項目を10項目に拡充 |
| **G3** | YouTubeトランスクリプト未取得 | 高 | `agents/researcher.py` が動画タイトル+説明文のみ取得。字幕（transcript）を取得することでネタの深さが段違いになる |
| **G4** | バズピボット機能がない | 中 | エンゲージメント上位の投稿から関連派生投稿を3本自動生成する機能が未実装 |
| **G5** | Xリサーチがない | 低 | Xのバズ投稿から構造を学ぶリサーチが未実装（API制限の関係で優先度低） |

---

## 2. 矛盾修正（Fixes）

### F1: フックストック充実

**現状の問題**:

`knowledge/hook_stock.json` は以下の構造を持つが、`hooks` 配列に1件しか入っていない。

```json
{
  "last_updated": "...",
  "hooks": [
    { "text": "...", "category": "...", "pattern": "..." }
  ]
}
```

`agents/writer.py` の `_generate_hook_candidates()` はhook_stock.jsonから構造を参照してClaude APIにフック案を複数生成させ、`_score_hooks()` でA/Bテストを行う設計になっている。1件だけでは「構造から学ぶ」効果が出ない。

**修正方針**:

フックストックを **スキンケア成分ジャンルのバズ投稿1行目** で充実させる。最終目標は100件以上（記事の265件目標に対してまず実現可能なラインから）。

**hook_stock.json の拡張スキーマ**:

```json
{
  "last_updated": "2026-03-31",
  "hooks": [
    {
      "text": "イデベノン、まだ使ってない人は損してる",
      "category": "skincare_ingredients",
      "pattern": "反常識型",
      "engagement_type": "拡散",
      "structure": "成分名 + 断言 + 損得フレーム",
      "source": "manual"
    },
    {
      "text": "セラミドの濃度、ちゃんと確認してる？",
      "category": "skincare_ingredients",
      "pattern": "コメント誘導型",
      "engagement_type": "拡散",
      "structure": "成分名 + 問いかけ",
      "source": "manual"
    }
  ]
}
```

**収集方法**:

| ステップ | 方法 |
|---------|------|
| 初期投入（手動） | スキンケア系Threadsアカウントの高反応投稿の1行目を手動で50件収集 |
| 自動蓄積 | Analyst実行時、engagement_rate上位5件の1行目をhook_stockに自動追加 |
| 定期確認 | 月1回、古い低精度フックを削除・入れ替え |

**Analystへの自動蓄積追加実装**（`agents/analyst.py`）:

```python
def _update_hook_stock(self, top_posts: list[dict]) -> None:
    """エンゲージメント上位投稿の1行目をhook_stockに追加"""
    hook_stock = self.state.load_json("hook_stock.json") or {"hooks": []}
    existing_texts = {h["text"] for h in hook_stock["hooks"]}

    for post in top_posts:
        content = post.get("content", "")
        first_line = content.split("\n")[0].strip()
        if not first_line or first_line in existing_texts:
            continue
        if len(first_line) < 10 or len(first_line) > 60:
            continue  # フックとして短すぎ/長すぎ

        hook_stock["hooks"].append({
            "text": first_line,
            "category": post.get("category", ""),
            "pattern": post.get("pattern", ""),
            "engagement_type": "実績あり",
            "structure": "",  # 空欄。人間が後から記述可
            "source": "auto",
            "added_at": datetime.now(JST).isoformat(),
            "engagement_rate": post.get("engagement_rate", 0.0),
        })
        existing_texts.add(first_line)

    hook_stock["last_updated"] = datetime.now(JST).isoformat()
    self.state.save_json("hook_stock.json", hook_stock)
    self.logger.info("hook_stock updated: %d hooks total", len(hook_stock["hooks"]))
```

**変更ファイル**: `knowledge/hook_stock.json`（データ追加）、`agents/analyst.py`（自動蓄積）

**完了条件**:
- [ ] `hook_stock.json` に50件以上のフックが登録されている
- [ ] Analyst実行後にhook_stockが自動更新される（テスト）
- [ ] Writer の `_generate_hook_candidates()` が複数のフック候補を返す（テスト）

---

### F2: コメント誘導型の追いコメ実装

**現状の問題**:

`agents/poster.py` は `affiliate_comment` フィールドがある場合のself-reply実装のみ存在する（line 176-193）。

しかし「コメント誘導型」パターンの設計意図は「本文の続き・補足をコメント欄に書いて、コメント欄を盛り上げる」こと。これが未実装のため、コメント誘導型を投稿しても効果が半減している。

**修正方針**:

Writerが `follow_up_comment` フィールドをpost_queueアイテムに含め、Posterがそれをself-replyで投稿する。

**Writerの変更**（`agents/writer.py`）:

```python
# _build_prompt() でコメント誘導型の場合に追加指示
if pattern == "コメント誘導型":
    prompt_parts.extend([
        "",
        "## 追いコメント指示（コメント誘導型専用）",
        "本文とは別に、コメント欄に投稿する「続き・補足」を作成してください。",
        "フォーマット:",
        "---本文---",
        "(投稿本文)",
        "---追いコメント---",
        "(コメント欄に投稿する内容。100文字以内。本文への補足や実体験など)",
    ])
```

**Posterの変更**（`agents/poster.py`）:

```python
# 既存のaffiliate_commentの後に追加

# 6. コメント誘導型の追いコメント投稿
follow_up_comment: str | None = target.get("follow_up_comment")
if follow_up_comment and target.get("pattern") == "コメント誘導型":
    self.logger.info("Publishing follow-up comment for コメント誘導型 post %s …", threads_media_id)
    try:
        self.threads.create_text_post(
            follow_up_comment,
            reply_to_id=threads_media_id,
        )
        self.logger.info("Follow-up comment posted.")
    except Exception as exc:
        self.logger.warning("Follow-up comment failed for %s: %s", post_record["id"], exc)
```

**writer_content.py の変更**（`_extract_follow_up_comment()` 追加）:

```python
def _extract_follow_up_comment(self, raw_content: str) -> str | None:
    """コメント誘導型の追いコメントを抽出"""
    marker = "---追いコメント---"
    if marker not in raw_content:
        return None
    parts = raw_content.split(marker)
    if len(parts) < 2:
        return None
    return parts[1].strip()[:100]  # 100文字上限
```

**post_queueアイテムへのフィールド追加**:

```python
post_item = {
    ...
    "follow_up_comment": follow_up_comment,  # コメント誘導型のみ。それ以外はNone
}
```

**変更ファイル**: `agents/writer.py`、`agents/poster.py`、`agents/writer_content.py`（または該当モジュール）

**完了条件**:
- [ ] コメント誘導型の投稿後、追いコメントがself-replyで投稿される
- [ ] 他パターンでは `follow_up_comment` が None になる
- [ ] テスト追加: `tests/test_poster.py` にコメント誘導型のself-replyテスト

---

### F3: テーマ連続チェック追加

**現状の問題**:

`agents/writer.py` の `_select_pattern()` はパターンの連続使用をブロックするが、**トピックのキーワード**が連続する場合のチェックがない。

例: research_poolに「セラミド」関連が大量にある場合、3連続でセラミド投稿が生成されてしまう。

**修正方針**:

`_generate_single_post()` 内で、直近3件の投稿と「主要キーワード（カテゴリ+主成分名）」が被る場合はskipして次のresearch_itemに移る。

**writer.py への追加**:

```python
def _is_topic_repeated(self, research_item: dict, recent_posts: list[dict]) -> bool:
    """直近3件と同一トピックキーワードが被る場合Trueを返す"""
    TOPIC_REPEAT_WINDOW = 3

    # チェック対象: カテゴリ + キーワードの主要1語
    item_category = research_item.get("category", "")
    item_keywords = research_item.get("keywords", [])
    item_primary = item_keywords[0].lower() if item_keywords else ""

    for post in recent_posts[-TOPIC_REPEAT_WINDOW:]:
        post_category = post.get("category", "")
        post_content = post.get("content", "").lower()

        # 同カテゴリ かつ 主要キーワードが本文に含まれる
        if post_category == item_category and item_primary and item_primary in post_content:
            return True
    return False

# _generate_single_post() の冒頭に追加:
# if self._is_topic_repeated(research_item, recent_posts):
#     return None  # 呼び出し側でスキップ
```

**変更ファイル**: `agents/writer.py`

**完了条件**:
- [ ] 同カテゴリ+同キーワードの投稿が3連続しない（テスト）
- [ ] 異なるキーワードなら同カテゴリでも通過する（テスト）

---

## 3. Phase 6: コンテンツ品質強化

V3.1の中核。参照アーキテクチャとのギャップG1・G2を解消する。

### タスク一覧

| ID | タスク | 変更ファイル | 工数 | 依存 |
|----|--------|-------------|------|------|
| P6-1 | 投稿パターン拡充（9→15種） | `knowledge/posting_rules.md` | 3h | なし |
| P6-2 | Writer側パターン対応 | `agents/writer.py`（パターン選択ロジック） | 1h | P6-1 |
| P6-3 | 品質採点10項目化 | `agents/writer_quality.py`, `knowledge/posting_rules.md` | 2h | なし |
| P6-4 | フックストック初期データ投入 | `knowledge/hook_stock.json` | 3h | なし |
| P6-5 | Analyst自動蓄積 | `agents/analyst.py` | 2h | P6-4 |
| P6-6 | P6テスト | `tests/test_writer_quality.py`, `tests/test_analyst.py` | 2h | P6-3, P6-5 |

---

### P6-1: 投稿パターン拡充（9→15種）

**現状**: `knowledge/posting_rules.md` に9種（+ツリー+2特殊=12）。

**追加するパターン**（参照アーキテクチャの「15種以上」に合わせる）:

| パターン番号 | 名前 | 分類 | 概要 |
|------------|------|------|------|
| パターン10 | 暴露系【拡散系】 | 拡散 | 「実は〇〇だった」形式で意外な真実を明かす構造 |
| パターン11 | 需要確認型【拡散系】 | 拡散 | 「これ需要ある？」「まとめていい？」でコメントを稼ぐ |
| パターン12 | ビフォーアフター型【保存系】 | 保存 | 「〇ヶ月前→今」の変化を具体的数値とともに語る |
| パターン13 | 失敗談型【拡散系】 | 拡散 | 自分の失敗経験から学んだことを語る。共感＋有益性 |
| パターン14 | ランキング異議型【拡散系】 | 拡散 | 「世間の評価に異議あり」でコメント誘発 |
| パターン15 | 季節タイムリー型【拡散系/保存系】 | 両方 | 季節に合わせた旬の成分・ケアを提案。season_matrixと連動 |

**各パターンの記述形式**（既存パターンと統一）:

```markdown
#### パターン10: 暴露系【拡散系】

**コンセプト**: 「実は〇〇だった」という意外性で読者を引き込む。権威への批判ではなく、業界の慣習や誤解を優しく暴露するトーン。

**構造**:
1行目: 「〇〇、実はずっと誤解されてた」（暴露宣言）
2〜4行: 一般的な誤解の説明
5〜7行: 実際はどうなのか（成分・データ付き）
末尾: 「知ってた？」でコメント誘導

**文字数**: 150〜250字
**注意**: 特定商品・ブランドへの言及禁止。成分・成分カテゴリのみ扱う。
```

**変更ファイル**: `knowledge/posting_rules.md`（パターン10〜15を追記）

---

### P6-2: Writer側パターン対応

**現状**: `agents/writer_constants.py`（または `writer.py` 内）のパターンリストが9種のみ。

**変更内容**:

```python
# agents/writer_constants.py（または writer.py の _PATTERNS 定義箇所）

_PATTERNS = [
    "コメント誘導型",
    "反常識型",
    "短文完結型",
    "二択比較型",
    "あるある共感型",
    "リスト系",
    "まとめ結論先出型",
    "タイムライン型",
    "数字インパクト型",
    # V3.1追加
    "暴露系",
    "需要確認型",
    "ビフォーアフター型",
    "失敗談型",
    "ランキング異議型",
    "季節タイムリー型",
]

# 拡散系・保存系の分類も更新
_KAKUSAN_PATTERNS = [
    "コメント誘導型", "反常識型", "短文完結型", "二択比較型", "あるある共感型",
    "暴露系", "需要確認型", "失敗談型", "ランキング異議型",
]
_HOZON_PATTERNS = [
    "リスト系", "まとめ結論先出型", "タイムライン型", "数字インパクト型",
    "ビフォーアフター型",
]
_BOTH_PATTERNS = ["季節タイムリー型"]
```

**配分設定**（`config/schedule.yaml` または `settings.yaml`）:

```yaml
writer:
  pattern_distribution:
    kakusan_ratio: 0.55    # 拡散系55%（V3: 60% → V3.1: 55%）
    hozon_ratio: 0.35      # 保存系35%（V3: 40% → V3.1: 35%）
    seasonal_ratio: 0.10   # 季節タイムリー10%（新設）
```

---

### P6-3: 品質採点10項目化

**現状**: `agents/writer_quality.py` の採点基準は7項目×10点。

**追加する3項目**（参照アーキテクチャの「10項目で採点」に合わせる）:

| 項目 | 英語キー | グループ | 採点基準 |
|------|---------|---------|---------|
| フックの強さ | `hook_strength` | 表現品質 | 1行目だけで読み続けたくなるか。「何これ？」感があるか |
| 行動誘発力 | `call_to_action` | 内容品質 | 読後に「試してみよう」「保存しよう」「コメントしよう」という気持ちになるか |
| ペルソナ一致度 | `persona_match` | 表現品質 | @seibun_love のキャラクター（成分オタク・親しみやすい・主張控えめ）と合っているか |

**writer_quality.py の変更**:

```python
criteria = """
【内容品質群】
1. 有益性（usefulness）: 読者が今日から行動を変えられるか（/10）
2. 具体性（specificity）: 成分名+濃度+条件など具体性があるか（/10）
3. 感情移入しやすさ（empathy）: 「わかる！」と共感できるか（/10）
4. 行動誘発力（call_to_action）: 読後に何か行動したくなるか（/10）

【表現品質群】
5. 自然さ（naturalness）: bot臭くないか（/10）
6. テンポ（tempo）: 文が短く最後まで読める（/10）
7. 体験語り感（experiential）: 「この人、実際に試したんだな」と感じるか（/10）
8. 業者臭さのなさ（non_commercial）: 友達の話を聞いている感覚か（/10）

【フック品質群】（新設）
9. フックの強さ（hook_strength）: 1行目だけで読み続けたくなるか（/10）
10. ペルソナ一致度（persona_match）: seibun_loveのキャラクターと合っているか（/10）
"""

# 3群平均方式に変更
content_avg = avg(usefulness, specificity, empathy, call_to_action)
expression_avg = avg(naturalness, tempo, experiential, non_commercial)
hook_avg = avg(hook_strength, persona_match)
final_avg = (content_avg + expression_avg + hook_avg) / 3
```

**min_quality_score の見直し**:

10項目になり採点基準が厳格化されるため、閾値を調整する。

```yaml
# config/settings.yaml
writer:
  min_quality_score: 7.0     # 変更なし（10項目でも同じ絶対値）
  auto_approve_threshold: 8.0  # 8.5 → 8.0（10項目で8.5は高すぎる）
```

**posting_rules.md の採点基準セクション更新**:

セクション3「品質スコア採点基準」を10項目版に更新する（コード側と一致させる）。

---

## 4. Phase 7: ネタ収集深化

### タスク一覧

| ID | タスク | 変更ファイル | 工数 | 依存 |
|----|--------|-------------|------|------|
| P7-1 | YouTubeトランスクリプト取得 | `agents/researcher.py` | 4h | なし |
| P7-2 | トランスクリプトキャッシュ | `data/state/transcript_cache.json` | 1h | P7-1 |
| P7-3 | P7テスト | `tests/test_researcher.py` | 2h | P7-1 |

---

### P7-1: YouTubeトランスクリプト取得

**現状の問題**:

`_search_youtube()` は動画タイトル・チャンネル名・説明文（300文字）のみ取得。動画の本体（話している内容）は全く参照していない。

参照アーキテクチャでは「YouTubeの字幕（文字起こし）を全部読んで、使えるネタだけ抽出」している。

**実装方針**:

`youtube-transcript-api` ライブラリを使用して日本語字幕を取得。字幕がない動画はスキップ。取得したテキストをClaudeに渡してネタ抽出する。

**requirements.txt への追加**:

```
youtube-transcript-api>=0.6.2
```

**実装内容** (`agents/researcher.py`):

```python
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

class ResearcherAgent:

    TRANSCRIPT_MAX_CHARS = 3000   # Claudeに渡す上限（トークン節約）
    TRANSCRIPT_CACHE_TTL_DAYS = 7 # キャッシュ有効期限

    def _get_transcript(self, video_id: str) -> str | None:
        """YouTube字幕を取得してテキストに変換。キャッシュあり。"""
        # 1. キャッシュ確認
        cache = self._load_transcript_cache()
        if video_id in cache:
            cached = cache[video_id]
            cached_at = datetime.fromisoformat(cached["cached_at"])
            if (datetime.now(JST) - cached_at).days < self.TRANSCRIPT_CACHE_TTL_DAYS:
                return cached["text"]

        # 2. 字幕取得
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            # 日本語字幕を優先。なければ自動生成字幕
            try:
                transcript = transcript_list.find_transcript(["ja"])
            except NoTranscriptFound:
                transcript = transcript_list.find_generated_transcript(["ja"])

            text = " ".join([t["text"] for t in transcript.fetch()])
            text = text[:self.TRANSCRIPT_MAX_CHARS]

        except (TranscriptsDisabled, NoTranscriptFound, Exception) as exc:
            self.logger.debug("Transcript unavailable for %s: %s", video_id, exc)
            return None

        # 3. キャッシュ保存
        cache[video_id] = {
            "text": text,
            "cached_at": datetime.now(JST).isoformat(),
        }
        self._save_transcript_cache(cache)

        return text

    def _extract_topics_with_transcript(
        self, videos: list[dict], coverage_gaps: list[str]
    ) -> list[dict]:
        """字幕付きで動画からトピック抽出（既存の_extract_topicsを強化）"""
        lines = []
        for v in videos:
            transcript = self._get_transcript(v["video_id"])
            transcript_text = f"\n  字幕抜粋: {transcript[:500]}" if transcript else ""
            lines.append(
                f"[{v['category']}] {v['title']}\n"
                f"  チャンネル: {v['channel_title']}\n"
                f"  説明: {v['description'][:200]}"
                f"{transcript_text}\n"
            )
        # 以降は既存の_extract_topics()と同様にClaude APIへ送信
        ...
```

**フォールバック設計**:

字幕取得は補助情報であり、失敗しても研究フロー全体は継続する。

```
字幕取得成功 → タイトル + 説明文 + 字幕 でトピック抽出（深い）
字幕取得失敗 → タイトル + 説明文 のみでトピック抽出（従来通り）
```

**キャッシュ設計** (`data/state/transcript_cache.json`):

```json
{
  "dQw4w9WgXcQ": {
    "text": "今日はセラミドについて解説します...",
    "cached_at": "2026-03-31T10:00:00+09:00"
  }
}
```

- TTL: 7日（動画内容は変わらないため長め）
- サイズ上限: エントリ数が200件を超えたら古い順に削除
- git管理外（`.gitignore` に `data/state/transcript_cache.json` を追加）

**変更ファイル**:
- `agents/researcher.py`（`_get_transcript()`, `_extract_topics_with_transcript()` 追加）
- `requirements.txt`（`youtube-transcript-api` 追加）
- `.gitignore`（`transcript_cache.json` 追加）

**完了条件**:
- [ ] 字幕あり動画でトランスクリプトが取得できる（テスト）
- [ ] 字幕なし動画でもエラーなく従来フローで継続する（テスト）
- [ ] キャッシュが有効期限内で再取得しない（テスト）
- [ ] `pytest tests/ -v` 全パス

---

## 5. Phase 8: バズ拡張機能

### タスク一覧

| ID | タスク | 変更ファイル | 工数 | 依存 |
|----|--------|-------------|------|------|
| P8-1 | バズピボット実装 | `agents/analyst.py`, `agents/writer.py` | 4h | V3 Phase完了後 |
| P8-2 | バズピボットテスト | `tests/test_analyst.py`, `tests/test_writer.py` | 2h | P8-1 |

---

### P8-1: バズピボット機能

**概要**:

エンゲージメントが急上昇した「バズ投稿」を検知し、その投稿と関連するネタを3本自動派生させてresearch_poolに追加する機能。

バズった話題は「今まさに読者が興味を持っている」サインであり、関連投稿を続けることで連鎖拡散を狙う。

**バズ検知基準**:

```yaml
# config/settings.yaml
fetcher:
  buzz_threshold:
    likes_1h: 20           # 1時間いいね20以上
    likes_6h: 50           # 6時間いいね50以上
    engagement_rate: 5.0   # エンゲージメント率5%以上
```

**Analystへの実装** (`agents/analyst.py`):

```python
def _detect_buzz_posts(self, recent_posts: list[dict]) -> list[dict]:
    """バズ投稿を検知してbuzz_pool.jsonに記録"""
    buzz_threshold = self.config.get("buzz_threshold", {})
    buzz_posts = []

    for post in recent_posts:
        metrics = post.get("metrics", {})
        likes_1h = metrics.get("likes_1h", 0)
        engagement_rate = metrics.get("engagement_rate", 0.0)

        if likes_1h >= buzz_threshold.get("likes_1h", 20) or \
           engagement_rate >= buzz_threshold.get("engagement_rate", 5.0):
            buzz_posts.append(post)
            self.logger.info("Buzz detected: post %s (ER: %.1f%%)", post["id"], engagement_rate)

    return buzz_posts

def _generate_buzz_pivot_topics(self, buzz_post: dict) -> list[dict]:
    """バズ投稿から派生ネタを3本生成してresearch_poolに追加"""
    prompt = (
        "以下のThreads投稿がバズりました。\n"
        "この投稿の関連トピックを3件提案してください。\n"
        "バズった投稿と同じ成分・テーマを別角度で掘り下げるネタにしてください。\n\n"
        f"バズ投稿:\n{buzz_post['content']}\n\n"
        "JSON配列で返してください（topic, summary, keywords, category, priority）\n"
        "priorityは8以上（バズ派生は高優先度）"
    )

    raw = self.claude.analyze(prompt=prompt, data="")
    topics = self._parse_claude_response(raw)

    # research_poolに追加
    pool = self.state.load_json("research_pool.json") or {"items": []}
    now = datetime.now(JST)

    for i, t in enumerate(topics[:3]):  # 最大3件
        pool["items"].append({
            "id": f"buzz_{buzz_post['id']}_{i:02d}",
            "source": "buzz_pivot",
            "topic": t.get("topic", ""),
            "summary": t.get("summary", ""),
            "keywords": t.get("keywords", []),
            "category": t.get("category", buzz_post.get("category", "")),
            "collected_at": now.isoformat(),
            "used": False,
            "priority": max(8, int(t.get("priority", 8))),  # 8以上を保証
            "parent_post_id": buzz_post["id"],
        })

    self.state.save_json("research_pool.json", pool)
    self.logger.info("Buzz pivot: added %d topics from post %s", len(topics[:3]), buzz_post["id"])
```

**バズピボットの流れ**:

```
Fetcher (6時間ごと)
  → いいね数・ERを更新
  → buzz_threshold を超えたら buzz_detected イベント発火

Analyst (日次 05:00)
  → post_historyからバズ投稿を検出
  → バズ投稿ごとに関連ネタを3本生成
  → research_poolに priority=8〜10 で追加（最優先で使用される）

Writer (次回実行時)
  → research_poolのunused + priority降順で取得
  → バズ派生ネタが最優先で選ばれる
```

**バズピボットの上限**:

同一バズ投稿から生成する派生ネタは3件まで。1日の全体バズピボット上限は9件（3件×3投稿）。

```yaml
# config/settings.yaml
analyst:
  buzz_pivot_max_per_post: 3
  buzz_pivot_max_daily: 9
```

**変更ファイル**: `agents/analyst.py`、`config/settings.yaml`

**完了条件**:
- [ ] バズ検知条件を満たした投稿が `buzz_pool.json` に記録される
- [ ] バズ投稿から3件のresearch_poolアイテムが生成される（priority≥8）
- [ ] 上限（1投稿3件、日次9件）が守られる
- [ ] `pytest tests/ -v` 全パス

---

## 6. タスク依存関係

```
【矛盾修正】（V3完了後すぐ着手可能）
  F1 フックストック充実
    └── Analyst自動蓄積 (P6-5)
  F2 コメント誘導型追いコメ ← 独立
  F3 テーマ連続チェック ← 独立

【Phase 6】（矛盾修正と並行可能）
  P6-1 パターン拡充（posting_rules.md）
    └── P6-2 Writer側対応
  P6-3 品質採点10項目化 ← 独立
  P6-4 フックストック初期データ投入 (= F1と同)
    └── P6-5 Analyst自動蓄積
  P6-6 テスト ← P6-3, P6-5

【Phase 7】（Phase 6と並行可能）
  P7-1 YouTubeトランスクリプト
    └── P7-2 トランスクリプトキャッシュ
    └── P7-3 テスト

【Phase 8】（Phase 6, 7完了後）
  P8-1 バズピボット ← P7-1完了推奨（バズ派生の品質向上のため）
    └── P8-2 テスト
```

**推奨着手順序**:

| 週 | 作業内容 |
|---|---------|
| Week 1 | F1（フックストック初期データ投入）、F2（コメント誘導型追いコメ）、F3（テーマ連続チェック） |
| Week 2 | P6-1〜P6-3（パターン拡充 + 品質10項目）、P7-1（トランスクリプト） |
| Week 3 | P6-4〜P6-6（Analyst蓄積 + テスト）、P7-2〜P7-3（キャッシュ + テスト） |
| Week 4 | P8-1〜P8-2（バズピボット） |

---

## 7. スコア目標

| 分野 | V3 | V3.1目標 | 差分 | 主な改善施策 |
|------|-----|---------|------|------------|
| アーキテクチャ設計 | 92 | 94 | +2 | バズピボット追加、パターン体系整備 |
| 安全設計 | 95 | 95 | 維持 | 変更なし |
| コード品質 | 90 | 91 | +1 | F2のPoster拡張 |
| テスト | 93 | 93 | 維持 | テスト追加で現状維持 |
| 運用準備度 | 92 | 92 | 維持 | V3で完了済み |
| **コンテンツ戦略** | **88** | **96** | **+8** | フック充実・パターン拡充・バズピボット |
| **総合** | **92** | **97** | **+5** | |

---

## 8. 完了基準

### 矛盾修正

- [ ] `knowledge/hook_stock.json` に50件以上のフックが登録されている
- [ ] コメント誘導型パターンの投稿後、自動でself-replyが投稿される
- [ ] 同カテゴリ+同キーワードの投稿が3連続しない

### Phase 6

- [ ] `knowledge/posting_rules.md` に15種のパターンが定義されている
- [ ] `agents/writer_constants.py`（または writer.py）のパターンリストが15種
- [ ] 品質採点が10項目×10点で行われる
- [ ] `auto_approve_threshold` が 8.0 に更新されている

### Phase 7

- [ ] 字幕付きYouTube動画でトランスクリプトが取得・活用される
- [ ] 字幕なし動画で従来フローにフォールバックする
- [ ] `transcript_cache.json` が機能し、7日間のキャッシュが有効

### Phase 8

- [ ] バズ検知がpost_historyの計測値に基づいて動作する
- [ ] バズ投稿から3件の派生research_itemが生成される
- [ ] バズ派生アイテムがwriterで優先的に選ばれる（priority≥8）

### テスト

- [ ] V3.1追加テストを含め全テストパス
- [ ] 新規テストカバー: フックA/Bテスト、コメント誘導型self-reply、テーマ連続チェック、バズピボット

---

## Appendix: 工数サマリー

| 区分 | タスク数 | 推定工数 | 内容 |
|------|---------|---------|------|
| 矛盾修正（F1〜F3） | 3 | 5h | フックストック・追いコメ・テーマチェック |
| Phase 6（コンテンツ品質） | 6 | 13h | パターン拡充・品質10項目・フック蓄積 |
| Phase 7（ネタ収集深化） | 3 | 7h | YouTubeトランスクリプト |
| Phase 8（バズ拡張） | 2 | 6h | バズピボット |
| **合計** | **14** | **31h** | **約1週間（フルタイム）/ 2〜3週間（パートタイム）** |
