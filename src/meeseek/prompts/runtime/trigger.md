# Triggers — runtime/triggers.py: the request an Inbox item's first turn starts from, one section per source

Sections are read by name — `prompts.text("runtime/trigger#<section>", ...)`. A section runs from its `## name` line to the next one; the blank lines around it are not part of the text. This note is not sent to any model.

## common

這是自動進來的事件，不是使用者在對話裡的請求；使用者稍後會在 Inbox 看你的結果。
- 你只能讀取與草擬。要回覆對方時，把回覆全文放在 ```reply 區塊裡（使用者會自己複製送出），區塊外不要重複內容。
- 需要寫入的動作（開 Jira、MR 留言、啟動 pipeline…）照常呼叫工具：它會變成待使用者同意的動作，同意後才執行。
- 先查清楚再下結論；資訊不夠就說缺什麼，不要猜。回覆精簡：重點、你查到的依據、建議的下一步。

## gmail

{{common}}

一封寄給使用者的信需要處理（分流判斷：{{why}}）。

寄件者：{{sender}}
主旨：{{title}}
thread_id：{{thread_id}}

{{body}}

需要時用 get_gmail_thread 讀整串。判斷對方要什麼；要回覆就草擬回覆，要查資料或 debug 就先查。

## chat

{{common}}

Google Chat 上有人找使用者（{{where}}；分流判斷：{{why}}）。

發話者：{{sender}}
space：{{space}}

{{body}}

需要前後文時用 get_chat_messages 讀這個 space。判斷對方要什麼；要回覆就草擬回覆，要查資料或 debug 就先查。

## mr

{{common}}

GitLab 上有 MR 的事找使用者（使用者在這個 MR 是 {{role}}；分流判斷：{{why}}）。

MR：{{title}}
連結：{{link}}
最新一則來自：{{sender}}

{{body}}

用 review_context 讀這個 MR（mr 參數直接給連結）。請使用者 review 的就做第一輪 review，把建議的留言草擬出來（不要直接留言）；有人留言問使用者的就草擬回覆；要查 log 或 pipeline 就先查。

## jira

{{common}}

Jira 上有票的事找使用者（{{role}}；分流判斷：{{why}}）。

票：{{title}}
連結：{{link}}
最新一則來自：{{sender}}

{{body}}

用 jira_get_issue 讀這張票（描述、最近的留言、關聯的票）。有人問使用者的就草擬回覆（放在 ```reply 區塊，使用者自己貼到 Jira）；指派給他的新票就整理要做什麼、缺什麼資訊、相關的 MR 與票；要查 log 或程式碼就先查。不要直接留言或改票。

## alert

{{common}}

GCP 告警觸發了，請初步 debug。

{{title}}
專案：{{project}}
開始：{{opened}}
對象：{{subject}}
policy：{{policy}}

先用 get_alert_policy 看條件，再用 query_logs 與 k8s_status 查告警開始前後的錯誤與 pod 狀態。回覆：發生了什麼、影響範圍、最可能的原因與依據、建議動作（例如回滾、重啟、開 Jira）。需要時可以照常提出寫入動作，它會等使用者同意。

## calendar

{{common}}

使用者的會議 {{minutes}} 分鐘後開始，請準備會前簡報。

會議：{{title}}
時間：{{start}} – {{end}}
與會者：{{attendees}}

{{body}}

從知識庫、Jira、最近的會議記錄找跟這場會議有關的背景、未決事項與上次的待辦，整理成會前 3–6 點重點。找不到相關資料就直說。不要草擬回覆。

## rehearsal

{{common}}

使用者的會議 {{minutes}} 分鐘後開始。幫他做會前沙盤推演：目標是讓他在會上講得清楚、有影響力，而不只是知道背景。

會議：{{title}}
時間：{{start}} – {{end}}
與會者：{{attendees}}
使用者：{{me}}

{{body}}

{{focus}}

上一次會後報告要他練習的：
{{practice}}

團隊成員的角色與關係（團隊關係圖：「確認」來自團隊名單；其餘從過去的會議推斷，次數是看到的會議數，標「推斷」的只看過一次）：
{{team}}

先查，再寫：
1. Jira 上 {{me}} 名下的票：進行中的、上次這個會之後更新過的、卡住的。GitLab 上他開的或要他 review 的 MR。
2. 知識庫 sources/meetings 裡這個會議最近兩次的記錄（標題相近的檔案）：上次的決議和待辦，誰做了、誰還沒。
3. 和這場會有關的其他背景（議程提到的主題）。

照這個格式寫，用繁體中文：

## 這場會要達成什麼
一兩行：這場會的目的，以及他在裡面的角色。

## 你要報告的
照重要性排，每點一句結論加依據（ticket key、MR 編號、數字）。

## 可能被問到的
3–5 題，每題附建議的回答，用他會說出口的口語。

## 要爭取的事
一件他想推動的決定或請求：為什麼現在提、具體怎麼開口（誰、做什麼、什麼時候）。

## 風險與地雷
可能被追問或有爭議的地方，怎麼回應。

## 60 秒口頭稿
可以直接照著講的稿子：daily 用「昨天／今天／卡點」，其他會議結論先行。短句、口語、一分鐘內講完。

## 這次練習
把上次報告的練習點變成這場的具體提醒（例如「報告每點先講結論」）。沒有上次的報告，就從前面內容挑一件最值得練的。

找不到資料就直說缺什麼，不要編。誰是誰的主管、誰負責什麼這類組織關係，只照團隊名單、知識庫和上面的角色與關係寫；用到只看過一次的要註明「（推斷）」，都沒有的就不要推測，也不要寫出來。不要草擬回覆。

## meeting_coached

{{common}}

會議「{{title}}」剛結束。錄音已轉成會議記錄，也做好了使用者的會後報告（他的談吐與內容），報告在下面。

會後報告、逐字稿、錄音：
{{files}}

{{report}}

---

會議記錄：

{{notes}}

回覆的開頭寫「**你的表現**」：從報告挑三點（最好的一點、最該改的一點、下次練習），每點一行，接著原樣列出上面三個檔案路徑。然後整理：決議、待辦（誰、做什麼、期限）、還沒解決要追的問題。和知識庫或 Jira 現況對不上的地方指出來。

{{jira}}

不要草擬回覆。

## meeting

{{common}}

會議「{{title}}」剛結束，錄音已轉成下面的會議記錄。

{{notes}}

整理成：決議、待辦（誰、做什麼、期限）、還沒解決要追的問題。和知識庫或 Jira 現況對不上的地方指出來。

{{jira}}

不要草擬回覆。

要對照之前的會議：記錄在 `sources/meetings/`，檔名是「日期 時間 會議名稱.md」（例如 `2026-10-19 1601 Agent 高階任務規劃 sync.md`）；先用 `kb_list` 或 `kb_grep` 找到檔名再讀，不要猜。

## meeting_jira_held

你和你團隊的待辦已經對照過 Jira，下面是每一件提出的動作（每一個都已經放在 Inbox 等使用者逐一同意）：
{{tickets}}

不要再為這些待辦提出 Jira 寫入。回覆的待辦段落照上面標出哪些已經提出、哪些不用動 Jira；你看到上面漏掉的待辦，或提出的票不對，就在回覆裡說明，不要自己再提出。

## meeting_jira_free

待辦需要開 Jira 或更新票的，先用 jira_search 找既有的票：有就在那張票提出留言，沒有才提出開新票；只為使用者自己負責的待辦開新票。照常呼叫工具提出（會等使用者同意）。

## phone

使用者從手機（{{app}}）傳訊息給你，這是使用者本人的請求，照一般對話處理。
- 你的回覆會以純文字傳回手機：不要用表格；重點先講，精簡，必要的連結直接貼網址。
- 回覆裡寫出的 `knowledge_base/outputs/` 底下的檔案（圖片、HTML、PDF、CSV、報告），會當附件一起傳到手機。使用者要檔案時，確定它在 outputs 底下（不在就用 `ship_output` 存一份），在回覆寫出路徑；不要說手機收不到檔案。
- 需要寫入的動作（開 Jira、MR 留言、啟動 pipeline…）照常呼叫工具：它會變成待同意的動作，使用者可以直接在手機上按同意，同意後才執行。

{{text}}
