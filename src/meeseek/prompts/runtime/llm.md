# Model-wrapper messages — runtime/llm.py

Sections are read by name — `prompts.text("runtime/llm#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## split_the_call

上一次的工具呼叫參數太長，串流到一半被輸出上限截斷，參數不是合法 JSON，所以沒有執行。請改用比較小的呼叫重做：例如寫檔就分段寫（先寫前半，再用第二次呼叫補後半）、報告就先送摘要再補細節。不要原封不動重送同一個呼叫。

## no_access

⛔ 你目前登入的 Google 帳號沒有 GCP 專案 {{projects}} 的使用權限，所以 meeseek 叫不到模型。需要這個專案的 Vertex AI User（`roles/aiplatform.user`）和 Service Usage Consumer（`roles/serviceusage.serviceUsageConsumer`）角色，請向該專案的管理員申請；權限加上前重試結果都一樣。

錯誤：{{detail}}

## unfilled

⚙️ project.yaml 的 `google_cloud.project` 還是範例的 `{{project}}`，所以 meeseek 叫不到模型。請填入你的 GCP 專案（README → Setup → 2. `project.yaml`），重啟後再試。

錯誤：{{detail}}

## login_invalid

🔑 這台電腦的 Google 登入不存在或已失效，所以 meeseek 叫不到模型。請重新登入（README → Setup → 4. Google login）後再試。

錯誤：{{detail}}
