# ThreadsBot API・インターフェース定義

> 各モジュール間のインターフェースを定義。新しいコンテキストでの実装・修正時に参照。

---

## core層

### core.logger

```python
def get_logger(name: str) -> logging.Logger
```

### core.state_manager.StateManager

```python
class StateManager:
    def __init__(self) -> None
    def load_json(self, filename: str) -> dict
    def save_json(self, filename: str, data: dict) -> None
```

- `filename`: `data/state/` 配下のファイル名（例: `"post_history.json"`）
- デフォルトスキーマ: `post_history.json`, `post_queue.json`, `research_pool.json`, `system_state.json`

### core.safety.SafetyGuard

```python
class SafetyGuard:
    def __init__(self, state_manager: StateManager) -> None
    def can_post(self) -> tuple[bool, str]
    def emergency_stop(self, reason: str) -> None
    def clear_emergency_stop(self) -> None
    def record_post(self) -> None
    def record_error(self, agent_name: str, error: str) -> None
    def is_circuit_open(self, agent_name: str) -> bool
```

### core.quality_gate.QualityGate

```python
class QualityGate:
    def check_ng_words(self, content: str) -> list[str]
    def check_similarity(self, content: str, history_contents: list[str]) -> float
    def check_pattern_rotation(self, pattern: str, recent_patterns: list[str], block_count: int = 3) -> bool
    def validate(self, content: str, pattern: str, post_history: list[dict]) -> dict
```

validate の戻り値:
```python
{"passed": bool, "reason": str, "similarity_score": float, "ng_words": list[str]}
```

---

## services層

### services.threads_api.ThreadsAPIClient

```python
class ThreadsAPIClient:
    def __init__(self) -> None
    def create_text_post(self, text: str, reply_to_id: str | None = None) -> dict
    def get_post_insights(self, media_id: str) -> dict
    def get_user_profile(self) -> dict
    def delete_post(self, media_id: str) -> bool
```

例外:
```python
class ThreadsAPIError(Exception): ...
class RateLimitError(ThreadsAPIError): ...      # 429
class AuthenticationError(ThreadsAPIError): ... # 401/403
```

### services.claude_client.ClaudeClient

```python
class ClaudeClient:
    def __init__(self) -> None
    def generate_post(self, prompt: str, system_prompt: str | None = None) -> str
    def evaluate_quality(self, content: str, criteria: str) -> dict
    def analyze(self, prompt: str, data: str) -> str
```

evaluate_quality の戻り値:
```python
{
    "scores": {
        "hook": float,        # フックの強さ (0-10)
        "usefulness": float,  # 有益性 (0-10)
        "specificity": float, # 具体性 (0-10)
        "tempo": float,       # テンポ (0-10)
        "persona_match": float # ペルソナ一致度 (0-10)
    },
    "average": float,
    "feedback": str
}
```

---

## agents層

### agents.base_agent.BaseAgent

```python
class BaseAgent(ABC):
    MAX_RETRIES: int = 3
    RETRY_DELAYS: list[int] = [30, 120, 300]

    def __init__(self, name: str) -> None
    def run(self) -> None          # エントリポイント（安全チェック + execute）
    def execute(self) -> None      # 抽象メソッド
    def update_status(self, status: str, error: str | None = None) -> None
```

### agents.poster.PosterAgent

- `execute()`: キューから1件取得 → 投稿 → 履歴記録 → アフィリコメント

### agents.fetcher.FetcherAgent

- `execute()`: 投稿後6h以上経過分のメトリクス取得 → performance.json更新

### agents.writer.WriterAgent

- `execute()`: キュー不足分を一括生成。ネタ → 投稿文 → 品質チェック → キュー追加

### agents.researcher.ResearcherAgent

- `execute()`: YouTube検索 → Claudeでネタ抽出 → 重複チェック → research_pool追加

### agents.analyst.AnalystAgent

- `execute()`: パフォーマンス分析 → フィードバック生成 → schedule.yaml更新

### agents.supervisor.SupervisorAgent

- `execute()`: ヘルスチェック → 異常検知 → エラー率チェック → アラート

---

## 設定ファイル

| ファイル | 用途 | 読み込み元 |
|---------|------|----------|
| config/settings.yaml | グローバル設定 | SafetyGuard, ClaudeClient, 全エージェント |
| config/schedule.yaml | 投稿スケジュール | WriterAgent, AnalystAgent |
| config/tone.yaml | ペルソナ・口調 | WriterAgent |
| config/ng_words.txt | NGワード辞書 | QualityGate |
| knowledge/posting_rules.md | 投稿パターン・品質基準 | WriterAgent |
| prompts/*.md | エージェント用プロンプト | 各エージェント |

---

## データファイル

| ファイル | 書き込み | 読み込み |
|---------|---------|---------|
| data/state/post_queue.json | Writer | Poster |
| data/state/post_history.json | Poster | Writer, Fetcher, Analyst, Supervisor |
| data/state/research_pool.json | Researcher | Writer |
| data/state/system_state.json | 全エージェント | 全エージェント |
| data/analytics/performance.json | Fetcher, Analyst | Analyst |
| data/analytics/audience.json | Analyst | Writer (将来) |
