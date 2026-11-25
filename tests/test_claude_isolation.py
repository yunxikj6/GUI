"""Check the actual tool catalog and lifecycle of an installed local Claude plugin."""
import json
import shlex
import sys

from test_native_claude import NativeClaudeFixture


def hook(marker):
    return {"SessionStart": [{"hooks": [{"type": "command", "command": "touch " + shlex.quote(str(marker))}]}]}


class ClaudeToolIsolation(NativeClaudeFixture):
    def test_new_and_resumed_chat_exclude_hooks_and_keep_user_model_and_auth(self):
        base = self.root.parent
        user_marker, project_marker, plugin_marker = [base / name for name in ("user-hook", "project-hook", "plugin-hook")]
        market = base / "marketplace"
        plugin = market / "fixture"
        (market / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / "hooks").mkdir()
        (market / ".claude-plugin/marketplace.json").write_text(json.dumps({
            "name": "localmarket", "owner": {"name": "fixture"},
            "plugins": [{"name": "fixture", "source": "./fixture"}],
        }))
        (plugin / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "fixture", "version": "1.0.0"}))
        (plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": hook(plugin_marker)}))
        for args in (["plugin", "marketplace", "add", str(market)], ["plugin", "install", "fixture@localmarket"]):
            self.assertEqual(self.cli(args, "").returncode, 0)
        mcp_marker = base / "mcp-started"
        program = f"from pathlib import Path; Path({str(mcp_marker)!r}).touch()"
        self.assertEqual(self.cli(["mcp", "add", "--scope", "user", "localfixture", "--", sys.executable, "-c", program], "").returncode, 0)

        project_config = self.root / ".claude"
        project_config.mkdir()
        (project_config / "settings.json").write_text(json.dumps({"hooks": hook(project_marker)}))
        settings_file = self.config / "settings.json"
        settings = json.loads(settings_file.read_text())
        provider_env = {name: self.env.pop(name) for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")}
        self.model.expected_key = provider_env["ANTHROPIC_API_KEY"]
        settings.update(env=provider_env, model="claude-sonnet-4-6", hooks=hook(user_marker))
        settings_file.write_text(json.dumps(settings))
        self.model.answers.extend(["fixture-control", "fixture-first", "fixture-follow-up"])

        # This control proves all three installed hooks are live in print mode.
        previous = self.cli(["-p", "--verbose", "--output-format", "stream-json", "--no-session-persistence",
                             "--permission-mode", "dontAsk", "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
                             "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'], "Greet without tools.")
        self.assertEqual(previous.returncode, 0)
        for marker in (user_marker, project_marker, plugin_marker):
            self.assertTrue(marker.exists(), marker.name)
            marker.unlink()
        self.assertFalse(mcp_marker.exists())

        parent = {}
        for request in ("first", "next"):
            task = self.rpc("start", provider="claude", requestId=request, prompt="Greet without tools.", **parent)
            self.finished(task)
            parent = {"parentTaskId": task["id"]}
            for marker in (user_marker, project_marker, plugin_marker, mcp_marker):
                self.assertFalse(marker.exists(), marker.name)
            model_request = self.model.requests[-1]
            self.assertEqual(model_request["model"], "claude-sonnet-4-6")
            self.assertEqual({tool["name"] for tool in model_request["tools"]}, {"Read", "Glob", "Grep"})
        self.assertEqual(len(self.model.requests), 3)
