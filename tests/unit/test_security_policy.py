"""安全政策測試 — agent 的寫入面必須是「明示核准」的封閉集合。

兩層控管的 tool 層防回歸：
1. 全系統寫入工具 = 核准清單（新增寫工具沒登記 → fail）
2. 每個 skill 的寫入工具 ⊆ 該 skill 的核准寫入集（綁錯 → fail）
3. MCP tool_filter 禁止已知危險工具（filter 被放寬 → fail）
"""

from meeseek.config import cfg
from meeseek.tools.registry import READ_PREFIXES, tool_names
from meeseek.runtime.skills import discover_skills

# ---------------------------------------------------------------------------
# 核准的寫入工具（唯一事實來源——要開新寫入通道，先來這裡登記並說明理由）
# ---------------------------------------------------------------------------

# 外部系統寫入（影響他人可見的狀態）
APPROVED_EXTERNAL_WRITES = {
    "comment_mr", "comment_mr_lines",          # code review 發佈 — code skill 核心功能
    "reply_mr_comment",                       # 在既有的 MR 討論串裡回覆（可順便 resolve）——
                                              # 和 comment_mr 同一個寫入面，只是接在別人的留言下
    "create_mr",                              # 從「已經被人推上遠端」的 branch 開 MR；
                                              # 本身不推 code，push 仍是人工那一步
    "update_mr",                              # 改 MR 的 title/description/target；
                                              # 不能 close/merge/reassign，也不碰 code
    "jira_create_issue", "jira_add_comment",  # 開票/留言 — sprint 流程功能
    "jira_update_issue", "jira_transition_issue", "jira_add_issues_to_sprint",
    "jira_create_issue_link", "jira_link_to_epic",          # sprint MCP
    "confluence_create_page", "confluence_update_page", "confluence_add_comment",
    "send_message",                            # （目前無 server 暴露，預留登記位）
}

# 本機寫入（僅本機檔案，不觸外部系統）
APPROVED_LOCAL_WRITES = {
    "save_refinement", "update_kb_file", "sync",   # KB 維護 — research skill
    "ship_output",                                  # 報告存檔（loader 注入）
    "generate_image",                               # 寫 PNG 到 outputs/images — 本機產物；推論走專案自己的 Vertex，
                                                    # prompt 會出機器但不寫任何外部系統（通用工具，所有 skill 都有）
    "propose_sop_rule",                             # consent-gated 學習規則
    "ask_claude",                                   # 交給 Claude Code（本機，auto mode）：資料夾外的寫入與
                                                    # meeseek 的寫入工具一律拒絕（runtime/claude_agent.py:policy）
    "task_create", "task_note", "task_update",      # 跨 turn 任務（runtime/tasks.py）——本機 cache 的 JSON 檔；
                                                    # 建立與結案在工具內以 origin 限制在 console session
    "workstream_note",                              # 工作流的共用筆記（runtime/workstreams.py）——本機 JSON 檔，
                                                    # 不觸外部系統；meeseek 與 Claude Code 共用同一份 context
    "remind_me",                                    # 提醒（runtime/wakes.py 的 remind wake）——本機 wake 記錄；
                                                    # 到期只推一句話到使用者自己的 Telegram，不起 turn、不吃預算
}

APPROVED_WRITES = APPROVED_EXTERNAL_WRITES | APPROVED_LOCAL_WRITES

# 已知的唯讀工具字首（啟發式：新工具若不符且不在核准清單 → 要求登記）
_READ_PREFIXES = READ_PREFIXES


def _looks_readonly(name: str) -> bool:
    return name.startswith(_READ_PREFIXES)


class TestWriteSurfaceIsClosed:
    def test_every_registry_tool_is_readonly_or_approved(self):
        unregistered = [
            n for n in tool_names()
            if not _looks_readonly(n) and n not in APPROVED_WRITES
        ]
        assert unregistered == [], (
            f"未登記的疑似寫入工具: {unregistered} — 若為唯讀請調整命名，"
            "若為寫入請在 APPROVED_*_WRITES 登記並說明理由"
        )

    def test_the_declared_write_flag_matches_the_approved_list(self):
        """`_ToolDef.writes` 決定一個工具會不會被自動發給每個 agent，所以它必須
        跟這份核准清單一致——不能靠名字猜。

        名字啟發式（_looks_readonly）擋不住「叫 get_* 但其實會寫」的工具；
        宣告擋得住。兩邊對不上就是有人只改了一邊。
        """
        from meeseek.tools.registry import _ALL_TOOLS

        mismatches = []
        for td in _ALL_TOOLS:
            approved_write = td.name in APPROVED_EXTERNAL_WRITES
            if td.writes != approved_write:
                mismatches.append(
                    f"{td.name}: writes={td.writes} 但核准清單說 {approved_write}")
        assert mismatches == [], (
            "writes 旗標與核准清單不一致：\n- " + "\n- ".join(mismatches)
            + "\n（writes=True 的工具不會被自動發給所有 agent，必須由 skill 明確宣告）"
        )

    def test_no_gated_tool_is_globally_attached(self):
        """會改外部狀態的工具永遠不能出現在 GENERAL_TOOLS——那等於發給每個 agent。"""
        from meeseek.tools.registry import GATED_TOOLS, GENERAL_TOOLS

        leaked = sorted(set(GENERAL_TOOLS) & set(GATED_TOOLS))
        assert leaked == [], f"寫入工具被全域掛上了: {leaked}"
        external = sorted(set(GENERAL_TOOLS) & APPROVED_EXTERNAL_WRITES)
        assert external == [], f"外部寫入工具被全域掛上了: {external}"

    def test_every_mcp_filtered_tool_is_readonly_or_approved(self):
        unregistered = [
            f"{s.name}/{n}" for s in cfg.mcp_servers for n in s.tool_filter
            if not _looks_readonly(n) and n not in APPROVED_WRITES
        ]
        assert unregistered == [], f"MCP filter 含未登記寫入工具: {unregistered}"

    def test_remote_mcp_filters_exclude_dangerous_tools(self):
        """GKE managed server 也暴露 create/apply/delete/patch — filter 絕不可放進來。"""
        FORBIDDEN = {"create_cluster", "update_cluster", "apply_k8s_manifest",
                     "delete_k8s_resource", "patch_k8s_resource", "create_node_pool",
                     "update_node_pool", "cancel_operation",
                     "create_file", "copy_file",           # drive MCP 若啟用
                     "create_draft", "label_thread"}       # gmail MCP 若啟用
        leaked = [
            f"{s.name}/{n}" for s in cfg.mcp_servers for n in s.tool_filter
            if n in FORBIDDEN
        ]
        assert leaked == [], f"MCP filter 放入了危險工具: {leaked}"


# 每個 skill 核准的「外部寫入」集合（本機寫入不在此限）
#
# 兩條通道都算在同一份授權裡：SKILL.md 的 `tools:`（registry 工具）與 project.yaml
# 的 `mcp_servers[].agents`（MCP 工具）——誰拿到什麼寫入權，要在這張表上讀得出來。
SKILL_EXTERNAL_WRITE_GRANTS = {
    # review 留言；create_mr 只接在人工 push 之後
    "code": {"comment_mr", "comment_mr_lines", "reply_mr_comment", "create_mr", "update_mr"},
    # sprint 是 Jira/Confluence 的「寫入級」主人（mcp-atlassian）——票務流程本身就是寫
    "sprint": {"jira_create_issue", "jira_add_comment",
               "jira_update_issue", "jira_transition_issue", "jira_add_issues_to_sprint",
               "jira_create_issue_link", "jira_link_to_epic",
               "confluence_create_page", "confluence_update_page", "confluence_add_comment"},
}


def _mcp_tools_for(skill_name: str) -> set[str]:
    """project.yaml 允許這個 skill 拿到的 MCP 工具名。

    刻意不看 SKILL.md 的 `mcp: true`：那是一個開關，翻開的那一刻這些寫入工具就
    到手了。用 config 允許的最大集合當基準，翻開開關才不會是「悄悄多了一組寫入權」。
    """
    return {
        name for server in cfg.mcp_servers
        if skill_name in server.agents
        for name in server.tool_filter
    }


class TestSkillWriteGrants:
    def test_skills_only_bind_granted_external_writes(self):
        violations = []
        for s in discover_skills():
            granted = SKILL_EXTERNAL_WRITE_GRANTS.get(s.name, set())
            bound_writes = set(s.tools) & APPROVED_EXTERNAL_WRITES
            extra = bound_writes - granted
            if extra:
                violations.append(f"{s.name}: {sorted(extra)}")
        assert violations == [], (
            f"skill 綁定了未核准的外部寫入工具: {violations} — "
            "讀場景不該有寫入；確有需要請在 SKILL_EXTERNAL_WRITE_GRANTS 登記"
        )

    def test_skills_only_get_granted_external_writes_via_mcp(self):
        """MCP 側的寫入工具也要逐 skill 核准，不是列進 filter 就算過。

        filter 是「這個 server 暴露什麼」，agents 才是「誰拿得到」。少了這道，
        一個新 skill 只要被寫進 `agents:` 就繼承整組 Jira 寫入權，而且沒有任何
        測試會出聲。
        """
        violations = []
        for s in discover_skills():
            granted = SKILL_EXTERNAL_WRITE_GRANTS.get(s.name, set())
            extra = (_mcp_tools_for(s.name) & APPROVED_EXTERNAL_WRITES) - granted
            if extra:
                violations.append(f"{s.name}: {sorted(extra)}")
        assert violations == [], (
            f"skill 透過 MCP 拿到未核准的外部寫入工具: {violations} — "
            "請從 project.yaml 的 agents/tool_filter 移除，或在 "
            "SKILL_EXTERNAL_WRITE_GRANTS 登記並說明理由"
        )

    def test_no_mcp_server_targets_an_unknown_or_mcp_less_skill(self):
        """`agents:` 寫了一個不存在／沒開 mcp 的 skill = 沒生效的設定。

        危險的不是它現在沒效，是它看起來有效：授權讀在 project.yaml 上，實際綁定
        卻取決於某個 SKILL.md 的一行開關。兩邊必須一致。
        """
        skills = {s.name: s for s in discover_skills()}
        dangling = []
        for server in cfg.mcp_servers:
            for name in server.agents:
                if name == "*":     # every skill: reads, and writes a skill's `tools:` grants
                    continue
                skill = skills.get(name)
                if skill is None:
                    dangling.append(f"{server.name}→{name}（沒有這個 skill）")
                elif not skill.use_mcp:
                    dangling.append(f"{server.name}→{name}（該 skill 的 mcp: false）")
        assert dangling == [], f"MCP server 的 agents 指向無效目標: {dangling}"


# ---------------------------------------------------------------------------
# 註冊 ≠ 拿得到：每個 registry 工具都要有「誰用得到」的答案
# ---------------------------------------------------------------------------
#
# 寫入面的測試是單向的（綁到的必須被核准），所以一個工具可以註冊完、核准完、
# 然後沒有任何 agent 拿得到——加工具的人以為好了，agent 那邊工具清單裡根本沒有。
# 這道測試補反方向：註冊了就要說得出經由哪條路被拿到。
#
# registry 的寫入工具只有一條路抵達 agent：skill SKILL.md 的 `tools:`。
# Jira／Confluence 寫入是 mcp-atlassian 的工具，綁定走 project.yaml 的 mcp_servers。


class TestEveryToolIsReachable:
    def test_every_gated_tool_reaches_some_agent(self):
        """讀取工具不必檢查——它們照定義發給每個 agent（registry.read_tools）。
        會漏的只剩寫入工具：註冊了、核准了，卻沒有任何 skill 宣告它。"""
        from meeseek.tools.registry import GATED_TOOLS

        reachable: set[str] = set()
        for s in discover_skills():
            reachable |= set(s.tools)

        orphans = sorted(set(GATED_TOOLS) - reachable)
        assert orphans == [], (
            f"註冊了但沒有任何 agent 拿得到的寫入工具: {orphans} — "
            "綁到某個 skill 的 `tools:`，或刪掉它。"
        )


class TestMcpSurfaceIsDeclared:
    """MCP 是第三個消費者，它的工具表面在這裡明列：改動要嘛是有意的
    （更新這份清單），要嘛會紅。"""

    # 外部 MCP client 看得到的工具。ask_meeseek／check_meeseek 這對入口不是 registry
    # 工具，是 MCP 專屬的；只動自己發起的 task，不寫外部系統。
    EXPECTED_MCP_TOOLS = {
        "ask_meeseek", "check_meeseek",
        "list_outputs",
        # meeseek 的 plugin（mcp_server.plugin_tools）：registry 裡所有不改外部狀態的工具——
        # 查團隊 context（唯讀）、畫圖、review_page。一次一問一答、背後沒有 agent turn；
        # meeseek 交辦的 Claude session 能用的就只有這些。外部呼叫端本來就能透過
        # ask_meeseek 查到同樣的東西，這裡只是少一次轉譯。
        "kb_grep", "kb_read", "kb_list",
                "fetch_url",
        "generate_image", "render_diagram", "review_page",
        # 工作流（runtime/workstreams.py）：一張票／MR／branch 一份共用 context。給 Claude Code 讀與記，
        # 讓它接手時不必重問；server 只綁 127.0.0.1（SECURITY.md）。
        "workstream_get", "workstream_note",
        # A pack's reads, and the MR writes in mcp_server._DIRECT_WRITE_TOOLS, join
        # once the pack is up (mcp_server.add_pack_tools; tested in
        # test_mcp_adapter.py and below) — not here, where no pack runs.
    }

    # 這一層「絕不」直接暴露：Jira 寫入與 Confluence 寫入屬票務流程，繞過 sprint skill 的開單格式與約定會破壞流程本身。
    NEVER_DIRECTLY_EXPOSED = {
        "jira_create_issue", "jira_add_comment",
        "jira_transition_issue", "jira_add_issues_to_sprint", "jira_update_issue",
        "confluence_create_page", "confluence_update_page", "confluence_add_comment",
        "propose_sop_rule",
    }

    def _surface(self) -> set[str]:
        import asyncio

        from meeseek.runtime.mcp_server import build_mcp_server

        return {t.name for t in asyncio.run(build_mcp_server().list_tools())}

    def test_the_exposed_surface_is_exactly_what_is_declared_here(self):
        actual = self._surface()
        added = sorted(actual - self.EXPECTED_MCP_TOOLS)
        removed = sorted(self.EXPECTED_MCP_TOOLS - actual)
        assert not added, (
            f"MCP 多暴露了工具: {added} — 外部任何能連上這個 server 的人都拿得到，"
            "確認是有意的再加進 EXPECTED_MCP_TOOLS"
        )
        assert not removed, f"MCP 少了工具: {removed} — 外部 client 會突然失去能力"

    def test_no_external_write_tool_is_exposed_without_being_listed(self):
        """MCP 上的寫入工具繞過 SKILL.md 與 SOP——任何一個都必須是明確決定。

        上面那份清單就是那個決定。這裡確認沒有第二條路默默把別的寫入工具放上去。
        """
        unlisted = sorted((self._surface() & APPROVED_EXTERNAL_WRITES)
                          - self.EXPECTED_MCP_TOOLS)
        assert unlisted == [], (
            f"外部寫入工具直接暴露在 MCP 上但沒登記: {unlisted} — 這會繞過 code skill "
            "的 SOP 與 SKILL_EXTERNAL_WRITE_GRANTS，任何能連上 8787 的人都能直接呼叫"
        )

    def test_the_tools_we_refused_to_expose_stay_unexposed(self):
        """有些能力就算方便也不放上來：Jira transition／Confluence 寫入繞過
        sprint 的流程約定。"""
        leaked = sorted(self._surface() & self.NEVER_DIRECTLY_EXPOSED)
        assert leaked == [], (
            f"這些工具刻意不直接暴露，卻出現在 MCP 上: {leaked}"
        )

    def test_directly_exposed_writes_are_approved_and_only_those_writes(self):
        """直接暴露的寫入（現在來自 GitLab pack）每一個都登記過；其他 pack 的
        寫入不上 MCP 表面——test_mcp_adapter 用一個真的 pack 驗證這件事。"""
        from meeseek.runtime.mcp_server import _DIRECT_WRITE_TOOLS

        assert set(_DIRECT_WRITE_TOOLS) <= APPROVED_EXTERNAL_WRITES
        assert set(_DIRECT_WRITE_TOOLS).isdisjoint(self.NEVER_DIRECTLY_EXPOSED)

    def test_ask_meeseek_tells_callers_that_writes_go_through_it(self):
        """Jira 寫入刻意不上 MCP，只能經 ask_meeseek。呼叫端的模型看不到對應
        工具就會回報「我沒有權限」——所以寫入走哪裡必須寫在它的描述裡。"""
        import asyncio

        from meeseek.runtime.mcp_server import build_mcp_server

        tools = asyncio.run(build_mcp_server().list_tools())
        desc = next(t.description for t in tools if t.name == "ask_meeseek")
        assert "WRITING" in desc or "writing" in desc
        for capability in ("MR", "Jira"):
            assert capability in desc, f"描述沒提到 {capability} 的寫入能力"
