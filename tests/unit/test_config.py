"""Config loading: schema, loader, paths.

Key invariant: a default value is written once, in the schema dataclass; the
loader never repeats it as a `.get("x", <literal>)`.
"""

import ast
import dataclasses
import inspect


import meeseek.config as config_pkg
from meeseek.config import loader as loader_mod
from meeseek.config import schema as schema_mod
from meeseek.config.schema import Config, ModelsConfig, ProjectConfig
from meeseek.config.loader import build_config
from meeseek.config.paths import PROJECT_ROOT, Home, default_home, home, using_home


class TestConfigSchema:
    """Config dataclasses have sane defaults."""

    def test_empty_config_has_defaults(self):
        cfg = Config()
        assert cfg.models.fast == "gemini-2.5-flash"
        assert cfg.workflows.meeting_notes is True

    def test_project_config_fields(self):
        pc = ProjectConfig(name="test", team_name="team1")
        assert pc.name == "test"
        assert pc.team_name == "team1"


class TestConfigLoader:
    """build_config() correctly transforms raw YAML dicts."""

    def test_build_from_minimal_yaml(self):
        cfg = build_config({"project": {"name": "TestProject"}})
        assert cfg.project.name == "TestProject"
        assert cfg.mcp_servers == []

    def test_an_old_vendor_section_becomes_its_pack(self):
        """project.yaml written before packs keeps its tools (config/legacy.py)."""
        cfg = build_config({"jira": {"projects": [{"key": "TP"}], "base_url": "https://j"}})
        [server] = cfg.mcp_servers
        assert (server.name, server.pack, server.agents) == ("jira", "jira", ["*"])
        assert server.config == {"projects": [{"key": "TP"}], "base_url": "https://j"}

    def test_a_file_that_names_the_pack_keeps_its_own_entry(self):
        cfg = build_config({"jira": {"base_url": "https://old"},
                            "mcp_servers": {"tickets": {"pack": "jira", "config": {"base_url": "https://new"}}}})
        assert [(s.name, s.config) for s in cfg.mcp_servers] == [("tickets", {"base_url": "https://new"})]

    def test_old_k8s_environments_become_the_gcp_pack(self):
        raw = {
            "kubernetes": {
                "default_namespace": "default",
                "environments": {
                    "prod": {"context": "gke_prod", "namespace": "acme-app"},
                    "uat": {"context": "gke_uat"},
                },
            }
        }
        cfg = build_config({**raw, "google_cloud": {"project": "p-1"}})
        [gcp] = cfg.mcp_servers
        assert (gcp.name, gcp.pack) == ("gcp", "gcp")
        assert gcp.config["environments"]["prod"] == {"context": "gke_prod", "namespace": "acme-app"}
        assert gcp.config["default_namespace"] == "default" and gcp.config["project"] == "p-1"

    def test_old_trigger_switches_become_the_packs_sources(self):
        cfg = build_config({"triggers": {"alerts": True, "gmail": True, "gmail_query": "is:unread",
                                         "chat": False, "calendar": True, "calendar_lead_minutes": 15}})
        assert cfg.triggers.sources == {
            "alert": {"tool": "alerts_poll"},
            "gmail": {"tool": "gmail_poll", "args": {"query": "is:unread"}},
            "calendar": {"tool": "calendar_poll", "every_s": 300, "args": {"lead_minutes": 15}}}
        assert [(s.name, s.pack) for s in cfg.mcp_servers] == [("google", "google")], "the Google pack serves them"

    def test_an_old_drive_section_becomes_the_google_pack(self):
        cfg = build_config({"google_drive": {"folder_ids": ["a", "b"], "exclude_file_ids": ["x"]}})
        [google] = cfg.mcp_servers
        assert google.pack == "google" and google.config == {"folder_ids": ["a", "b"], "exclude_file_ids": ["x"]}

    def test_build_empty_dict(self):
        cfg = build_config({})
        assert cfg.project.name == ""
        assert cfg.mcp_servers == []


class TestConfigPaths:
    """Where things live: code beside the package, your context under a home."""

    def test_project_root_is_repo_root(self):
        assert (PROJECT_ROOT / "pyproject.toml").exists()

    def test_the_default_home_is_the_checkout(self, monkeypatch):
        monkeypatch.delenv("MEESEEK_HOME", raising=False)
        monkeypatch.delenv("MEESEEK_SKILLS", raising=False)
        h = default_home()
        assert h.data == PROJECT_ROOT / ".cache" and h.kb == PROJECT_ROOT / "knowledge_base"
        assert h.skills == PROJECT_ROOT / "skills"

    def test_the_home_can_live_anywhere(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEESEEK_HOME", str(tmp_path / "ctx"))
        monkeypatch.setenv("MEESEEK_SKILLS", str(tmp_path / "skills"))
        h = default_home()
        assert h.kb == tmp_path / "ctx" / "knowledge_base" and h.skills == tmp_path / "skills"

    def test_paths_resolve_when_used_not_when_imported(self, tmp_path):
        from meeseek.runtime import inbox

        before = inbox.items_dir()
        with using_home(Home(root=tmp_path, skills=tmp_path / "skills")):
            assert inbox.items_dir() == tmp_path / ".cache" / "inbox"
        assert inbox.items_dir() == before == home().data / "inbox"

    def test_a_home_is_made_at_startup_not_on_import(self, tmp_path):
        h = Home(root=tmp_path, skills=tmp_path / "skills")
        assert not h.data.exists()
        h.ensure()
        assert h.data.is_dir() and h.notes.is_dir() and h.outputs.is_dir()


class TestDefaultsLiveOnlyInTheSchema:
    """One fact, one place — for every default value in the config."""

    def test_empty_yaml_equals_dataclass_defaults(self):
        """The loader adds nothing to an empty mapping, so a default changed in
        schema.py alone stays green and a literal in the loader goes red."""
        assert build_config({}) == Config()

    def test_loader_has_no_literal_defaults(self):
        """No `.get("key", <literal>)` in the loader — that is the second copy.

        Empty containers and `None` are fine (they mean "absent"), and so is a
        Name/Call (a derivation such as `k8s.get("default_namespace", "")`).
        """
        tree = ast.parse(inspect.getsource(loader_mod))
        offenders = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and len(node.args) == 2):
                continue
            default = node.args[1]
            if isinstance(default, ast.Constant) and default.value not in (None, "", 0, False):
                offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
            elif isinstance(default, (ast.List, ast.Dict, ast.Set)) and getattr(default, "elts", getattr(default, "keys", [])):
                offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
        assert offenders == [], (
            "loader repeats defaults the schema already owns:\n- " + "\n- ".join(offenders))

    def test_unknown_yaml_keys_are_reported_not_swallowed(self, caplog):
        """A stale key (a removed feature, a typo) must be visible, not ignored."""
        import logging
        with caplog.at_level(logging.WARNING):
            build_config({"models": {"fast": "m", "nonsense_key": 1}})
        assert any("nonsense_key" in r.message for r in caplog.records)


class TestDerivedModelDefaults:
    def test_coding_follows_workflow_unless_set(self):
        assert build_config({"models": {"workflow": "m-x"}}).models.coding == "m-x"
        assert build_config({"models": {"workflow": "m-x", "coding": "m-y"}}).models.coding == "m-y"
        assert ModelsConfig(workflow="m-x").coding == "m-x"

    def test_resolve_maps_tier_aliases_and_passes_ids_through(self):
        m = ModelsConfig(router="r", workflow="w", fast="f", coding="c", multimodal="mm", image="i")
        assert m.resolve("router") == "r"
        assert m.resolve("fast") == "f"
        assert m.resolve("coding") == "c"
        assert m.resolve("gemini-9.9-pro") == "gemini-9.9-pro"
        assert m.resolve("") == "w", "empty means the workflow tier"


class TestEnvPolicy:
    """yaml is the source; env is an export target, never an override."""

    def test_env_never_overrides_yaml(self, monkeypatch):
        monkeypatch.setenv("MODEL_FAST", "from-shell")
        monkeypatch.setenv("MAX_TURNS", "999")
        cfg = build_config({"models": {"fast": "from-yaml", "max_turns": 7}})
        assert cfg.models.fast == "from-yaml"
        assert cfg.models.max_turns == 7

    def test_model_and_turn_settings_are_yaml_only(self):
        """Model and turn settings come from project.yaml only; none is read from the environment."""
        src = inspect.getsource(config_pkg) + inspect.getsource(loader_mod)
        for name in ("MODEL_ROUTER", "MODEL_WORKFLOW", "MODEL_FAST", "MODEL_CODING",
                     "MODEL_IMAGE", "MODEL_MULTIMODAL", "MAX_TURNS", "CACHE_TTL",
                     "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
            assert f'environ.get("{name}"' not in src, f"{name} is still read from the environment"

    def test_cfg_is_the_config_of_now(self):
        mine = build_config({"models": {"fast": "m-mine"}})
        before = config_pkg.cfg.models.fast
        with config_pkg.using_config(mine):
            assert config_pkg.cfg.models.fast == "m-mine"
        assert config_pkg.cfg.models.fast == before

    def test_yaml_values_are_exported_to_the_environment(self, monkeypatch):
        """Third parties (the genai SDK, ${VAR} interpolation) read these."""
        for name in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
                     "GOOGLE_GENAI_USE_VERTEXAI", "GCP_PROJECT_ID", "JIRA_URL", "GITLAB_URL"):
            monkeypatch.delenv(name, raising=False)
        build_config({
            "google_cloud": {"project": "p-1", "location": "us-central1",
                             "quota_project": "q-1", "vertex": True},
            "jira": {"base_url": "https://j"}, "gitlab": {"base_url": "https://g"},
        })
        import os
        assert os.environ["GOOGLE_CLOUD_PROJECT"] == "p-1"
        assert os.environ["GOOGLE_CLOUD_LOCATION"] == "us-central1"
        assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "TRUE"
        assert os.environ["GCP_PROJECT_ID"] == "q-1"
        assert os.environ["JIRA_URL"] == "https://j"
        assert os.environ["GITLAB_URL"] == "https://g"

    def test_constructing_a_config_has_no_side_effects(self, monkeypatch):
        """Only build_config exports; `Config()` must stay pure."""
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        Config(google_cloud=schema_mod.GoogleCloudConfig(project="should-not-export"))
        import os
        assert os.environ.get("GOOGLE_CLOUD_PROJECT") != "should-not-export"


class TestSchemaExports:
    def test_every_dataclass_is_exported_and_reachable(self):
        declared = {n for n, o in vars(schema_mod).items()
                    if dataclasses.is_dataclass(o) and not n.startswith("_")}
        assert declared <= set(schema_mod.__all__), "a dataclass is missing from __all__"
        for name in schema_mod.__all__:
            assert hasattr(config_pkg, name), f"{name} is not reachable from meeseek.config"
