from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _PluginContext:
    def __init__(self) -> None:
        self.tools: list[str] = []
        self.tool_schemas: dict[str, dict] = {}
        self.hooks: list[str] = []
        self.commands: list[str] = []
        self.sections: list[str] = []

    def get_config(self, _key, default=None):
        return default

    def register_tool(self, *, name, **kwargs):
        self.tools.append(name)
        self.tool_schemas[name] = kwargs.get("schema", {})

    def register_hook(self, name, _handler):
        self.hooks.append(name)

    def register_command(self, *, name, **_kwargs):
        self.commands.append(name)

    def register_system_prompt_section(self, *, id, **_kwargs):
        self.sections.append(id)

    def register_platform(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("v0.6 不得註冊 Telegram 或其他 platform")


class PluginContractTests(unittest.TestCase):
    def test_root_registers_only_seven_tools_and_five_hooks(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "tcb_test_plugin",
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
            context = _PluginContext()
            module.register(context)
        finally:
            sys.modules.pop(spec.name, None)
        self.assertEqual(
            set(context.tools),
            {
                "agent_task_start",
                "agent_task_status",
                "agent_task_message",
                "agent_task_cancel",
                "bridge_task_update",
                "bridge_task_inbox",
                "bridge_task_status",
            },
        )
        self.assertEqual(
            context.hooks,
            [
                "pre_tool_call",
                "post_tool_call",
                "pre_llm_call",
                "post_llm_call",
                "on_session_end",
            ],
        )
        self.assertEqual(context.commands, ["agenttask"])
        start_parameters = context.tool_schemas["agent_task_start"]["parameters"]
        self.assertIn("exact_payload", start_parameters["properties"])
        self.assertEqual(
            start_parameters["properties"]["exact_payload"]["properties"]["kind"]["enum"],
            ["exact_text"],
        )

    def test_manifest_declares_general_plugin(self) -> None:
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("version: 0.6.5", manifest)
        self.assertNotIn("kind: platform", manifest)
        self.assertNotIn("register_platform", (ROOT / "__init__.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
