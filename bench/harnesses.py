"""The model harnesses: the coding-agent CLIs that a model works through,
each run with the user's settings left out, its web tools off, and only its
own provider's credentials."""

import json
import os
import re
from dataclasses import dataclass

from .sandbox import BenchmarkError

# For each provider: the hosts its harness may reach, and the environment
# variables that may hold its credential.
PROVIDERS = {
    "openai": (["api.openai.com", "chatgpt.com", "auth.openai.com"], ["OPENAI_API_KEY"]),
    "anthropic": (["api.anthropic.com"], ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"]),
    "google": (["generativelanguage.googleapis.com", "oauth2.googleapis.com"],
               ["GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_API_KEY"]),
    "openrouter": (["openrouter.ai"], ["OPENROUTER_API_KEY"]),
    "xai": (["api.x.ai"], ["XAI_API_KEY"]),
    "deepseek": (["api.deepseek.com"], ["DEEPSEEK_API_KEY"]),
    "mistral": (["api.mistral.ai"], ["MISTRAL_API_KEY"]),
}
HARNESSES = ("claude", "codex", "opencode", "agy")


def claude_settings():
    """Claude Code's settings for a build container. Its own sandbox stays
    on, with no network of its own: the container is the outer wall, and
    this keeps the credential away from the commands the model runs."""
    return {
        "disableAllHooks": True,
        "sandbox": {
            "enabled": True, "failIfUnavailable": True,
            "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": True,
            "enableWeakerNestedSandbox": True, "enableWeakerNetworkIsolation": False,
            "bwrapPath": "/opt/bench/claude-bwrap",
            "filesystem": {"allowWrite": ["/workspace", "/tmp", "/home/bench"]},
            "network": {"allowedDomains": [], "deniedDomains": ["*"], "strictAllowlist": True,
                        "allowUnixSockets": [], "allowAllUnixSockets": False, "allowLocalBinding": False},
        },
    }


def agy_settings():
    """Antigravity's settings for a build container: the Gemini API with a
    key instead of a sign-in, and no web or MCP tools."""
    return {
        "modelProvider": "gemini",
        "permissions": {"allow": [], "ask": [], "deny": ["read_url(*)", "execute_url(*)", "mcp(*)"]},
        "statusLine": {"enabled": False},
        "title": {"enabled": False},
    }


def auth_secrets(path):
    """The tokens in a Codex auth.json, to scrub from logs."""
    if not path or not path.is_file():
        return ()
    found = []

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {"access_token", "refresh_token", "id_token", "api_key", "openai_api_key"} \
                        and isinstance(item, str):
                    found.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(json.loads(path.read_text()))
    return tuple(found)


@dataclass(frozen=True)
class Harness:
    name: str
    model: str
    effort: str = None

    @property
    def provider(self):
        if self.name == "codex":
            return "openai"
        if self.name == "claude":
            return "anthropic"
        if self.name == "agy":
            return "google"
        if "/" not in self.model:
            raise BenchmarkError("OpenCode needs the model as provider/model")
        provider = self.model.split("/", 1)[0]
        if provider not in PROVIDERS:
            raise BenchmarkError(f"no host allowlist for provider '{provider}': add one to bench/harnesses.py")
        return provider

    @property
    def hosts(self):
        return PROVIDERS[self.provider][0]

    def credential_names(self):
        """The environment variables, any one of which is the harness's credential."""
        # Antigravity reads only this one. Its own sign-in is kept by the
        # desktop's credential store, which can't be copied into a container.
        return ["GEMINI_API_KEY"] if self.name == "agy" else PROVIDERS[self.provider][1]

    def credentials(self):
        return {name: os.environ[name] for name in self.credential_names() if os.environ.get(name)}

    def environment(self):
        env = self.credentials()
        if self.name == "agy":
            env.update(AGY_CLI_DISABLE_AUTO_UPDATE="true")
        if self.name == "claude":
            env.update(CLAUDE_CODE_SUBPROCESS_ENV_SCRUB="1", CLAUDE_CODE_MAX_OUTPUT_TOKENS="128000",
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        if self.name == "opencode":
            env.update(
                OPENCODE_CONFIG_CONTENT=json.dumps({
                    "$schema": "https://opencode.ai/config.json", "share": "disabled",
                    "autoupdate": False, "plugin": [], "mcp": {}, "instructions": [],
                    "permission": {"*": "deny", "read": "allow", "edit": "allow",
                                   "bash": "allow", "glob": "allow", "grep": "allow",
                                   "list": "allow", "external_directory": "deny"},
                }),
                OPENCODE_DISABLE_AUTOUPDATE="true", OPENCODE_AUTO_SHARE="false",
                OPENCODE_DISABLE_MODELS_FETCH="true", OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX="65536",
            )
        return env

    def preflight(self, docker, container):
        """Checks what the harness needs from the container before any model call."""
        if self.name != "claude":
            return
        try:
            docker.run(container, "/opt/bench/claude-bwrap", "--unshare-user", "--unshare-pid",
                       "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--", "/bin/true")
        except BenchmarkError as e:
            raise BenchmarkError(f"Claude's subprocess sandbox can't start in this container: {e}") from None

    def authenticate(self, docker, container, auth_file):
        """Codex keeps its login in a file; the others read the environment."""
        if self.name == "agy":
            # Root makes Antigravity's hook file, which names the tool policy,
            # so the model, which is another user, can't change or remove it.
            # The directories are sticky: Antigravity keeps its own files in
            # them, but nobody else can rename or delete root's.
            hooks = {"bitbath": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": "python3 /opt/bench/agy-policy", "timeout": 10}]}]}}
            docker.run(container, "sh", "-c",
                       "umask 022; cd /home/bench && mkdir -p .gemini/config && cat > .gemini/config/hooks.json"
                       " && chmod 1777 .gemini .gemini/config",
                       user="0:0", input_bytes=json.dumps(hooks).encode())
        if self.name != "codex":
            return
        if self.credentials().get("OPENAI_API_KEY"):
            docker.run(container, "codex", "login", "--with-api-key",
                       input_bytes=os.environ["OPENAI_API_KEY"].encode())
        elif auth_file and auth_file.is_file():
            # Only auth.json: never the user's config, rules, or history.
            docker.run(container, "sh", "-c",
                       "umask 077; mkdir -p /home/bench/.codex; cat > /home/bench/.codex/auth.json",
                       input_bytes=auth_file.read_bytes())
        else:
            raise BenchmarkError("Codex needs OPENAI_API_KEY, or --auth-file naming a Codex auth.json")

    def command(self, prompt, *, version=None):
        """The command for one session, and the text for its stdin, if any.

        The flags that skip permission prompts are only ever used inside a
        build container, which is what does the confining.
        """
        if self.name == "codex":
            args = ["codex", "exec", "--json", "--ephemeral", "--ignore-user-config",
                    "--ignore-rules", "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox", "--model", self.model,
                    "--cd", "/workspace", "-c", 'web_search="disabled"',
                    "-c", 'shell_environment_policy.exclude=["*KEY*","*TOKEN*","*SECRET*"]']
            if self.effort:
                args += ["-c", f"model_reasoning_effort={json.dumps(self.effort)}"]
            return args + ["-"], prompt
        if self.name == "claude":
            tools = "Read,Write,Edit,Glob,Grep,Bash"
            args = ["claude", "--safe-mode", "--print", "--verbose", "--output-format", "stream-json",
                    "--no-session-persistence", "--no-chrome", "--strict-mcp-config",
                    "--mcp-config", '{"mcpServers":{}}', "--settings", json.dumps(claude_settings()),
                    "--permission-mode", "dontAsk", "--tools", tools, "--allowedTools", tools,
                    "--disallowedTools", "WebFetch,WebSearch,mcp__*", "--model", self.model]
            if self.effort:
                args += ["--effort", self.effort]
            return args, prompt
        if self.name == "agy":
            # Antigravity's own terminal sandbox needs user namespaces, which
            # the container doesn't give it, and without it every command
            # would wait for an approval nobody can give.
            args = ["agy", "--input-format", "stream-json", "--output-format", "stream-json",
                    "--model", self.model, "--dangerously-skip-permissions", "--disable-slash-commands",
                    "--settings", json.dumps(agy_settings())]
            if self.effort:
                args += ["--effort", self.effort]
            return args, json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        match = re.search(r"(?:^|\s)v?(\d+)\.", version or "")
        v2 = bool(match and int(match.group(1)) >= 2)
        model = self.model
        if v2 and self.effort and "#" not in model:
            model += "#" + self.effort
        args = ["opencode", "run", "--pure", "--format", "json", "--model", model,
                "--agent", "build", "--dir", "/workspace",
                "--auto" if v2 else "--dangerously-skip-permissions"]
        if self.effort and not v2:
            args += ["--variant", self.effort]
        # OpenCode takes its prompt as an argument, not on stdin.
        return args + [prompt], None


PERMANENT_ERRORS = (
    "invalid api key", "incorrect api key", "authentication failed", "unauthorized",
    "model not found", "modelnotfounderror", "model_not_found", "unknown model",
    "does not support this model", "insufficient_quota", "permission denied",
    "cannot be used for codex", "codex_byok_restricted",
    "api key not valid", "api_key_invalid", "authentication required",
)
TRANSIENT_ERRORS = (
    "rate limit", "rate_limit", "too many requests", "429", "502", "503", "504",
    "overloaded", "connection reset", "connection refused", "temporarily unavailable",
    "failed to fetch", "transport error", "connection error", "request timeout",
)


def agy_failed(event):
    """Whether this is Antigravity's closing event for a turn that failed."""
    result = event.get("result")
    return event.get("event") == "result" and isinstance(result, dict) and result.get("status") != "SUCCESS"


def failure_kind(stdout, stderr, returncode):
    """Why a session failed: "configuration", "transient", "harness", or
    None if it didn't. A CLI can exit with 0 after an error event, so the
    events are read too."""
    errors = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if (event.get("type") in {"error", "turn.failed"} or event.get("is_error")
                or (event.get("type") == "result" and str(event.get("subtype", "")).startswith("error"))
                or agy_failed(event)):
            errors.append(json.dumps(event))
    if not returncode and not errors:
        return None
    text = "\n".join(errors + [stderr[-20000:]]).lower()
    if any(word in text for word in ("max_tokens_truncation", "error_max_turns", "error_max_output_tokens")):
        return None  # The session ran out of room, not into a fault. The next one continues.
    if any(word in text for word in PERMANENT_ERRORS):
        return "configuration"
    if any(word in text for word in TRANSIENT_ERRORS):
        return "transient"
    return "harness"


def error_message(stdout):
    """The last error a harness reported in its events, or ""."""
    message = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in {"error", "turn.failed"}:
            found = event.get("message") or event.get("error")
            while isinstance(found, (dict, str)):
                if isinstance(found, str):
                    try:
                        inner = json.loads(found)
                    except ValueError:
                        break
                    if not isinstance(inner, dict):
                        break
                    found = inner
                else:
                    found = found.get("message") or found.get("error")
            if found:
                message = str(found)
        elif isinstance(event, dict) and agy_failed(event):
            message = str(event["result"].get("error") or f"Antigravity ended with status {event['result'].get('status')}")
    return message[:1000]


def activity(stdout):
    """What a session has done so far, from its events: how many commands it
    ran and files it changed, its latest command, and the latest thing the
    model said. Reads the events of Claude Code, Codex, and Antigravity;
    others count nothing."""
    seen = {"commands": 0, "edits": 0, "command": "", "said": ""}
    steps, speech = set(), {}

    def one_line(text):
        return " ".join(str(text).split())

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":  # Claude Code
            content = (event.get("message") or {}).get("content")
            for part in content if isinstance(content, list) else []:
                if part.get("type") == "text" and part.get("text", "").strip():
                    seen["said"] = one_line(part["text"])
                elif part.get("type") == "tool_use":
                    given = part.get("input") or {}
                    if part.get("name") in {"Write", "Edit"}:
                        seen["edits"] += 1
                    elif part.get("name") == "Bash":
                        seen["commands"] += 1
                        seen["command"] = one_line(given.get("command", ""))
        elif event.get("event") == "step_update":  # Antigravity
            step = event.get("step_update") or {}
            index, tool = step.get("step_index"), step.get("tool_name")
            if step.get("step_type") == "agent_response":
                speech[index] = speech.get(index, "") + str(step.get("text_delta") or "")
                if speech[index].strip():
                    seen["said"] = one_line(speech[index])
            elif tool and index not in steps:
                steps.add(index)
                if tool == "run_command":
                    seen["commands"] += 1
                    given = (step.get("tool_info") or {}).get("parameters") or {}
                    seen["command"] = one_line(given.get("CommandLine", ""))
                elif tool in {"write_file", "write_to_file", "edit_file", "replace_file_content", "multi_replace_file_content"}:
                    seen["edits"] += 1
        elif event.get("type") == "item.started" or event.get("type") == "item.completed":  # Codex
            item = event.get("item") or {}
            if item.get("type") == "command_execution" and event["type"] == "item.started":
                seen["commands"] += 1
                seen["command"] = one_line(item.get("command", ""))
            elif item.get("type") == "file_change" and event["type"] == "item.completed":
                seen["edits"] += 1
            elif item.get("type") == "agent_message" and event["type"] == "item.completed":
                seen["said"] = one_line(item.get("text", ""))
    return seen


def usage_records(stdout):
    """Every usage record the harness reported, as it reported them. Some
    harnesses report running totals and some per-turn counts, so these
    aren't summed here."""
    records = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("usage"), dict):
            records.append(event["usage"])
        # Antigravity: a turn's closing usage already counts its steps'.
        result = event.get("result")
        if event.get("event") == "result" and isinstance(result, dict) and isinstance(result.get("usage"), dict):
            records.append(result["usage"])
        message = event.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            records.append(message["usage"])
    return records
