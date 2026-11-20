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
HARNESSES = ("claude", "codex", "opencode")


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
        if "/" not in self.model:
            raise BenchmarkError("OpenCode needs the model as provider/model")
        provider = self.model.split("/", 1)[0]
        if provider not in PROVIDERS:
            raise BenchmarkError(f"no host allowlist for provider '{provider}': add one to bench/harnesses.py")
        return provider

    @property
    def hosts(self):
        return PROVIDERS[self.provider][0]

    def credentials(self):
        return {name: os.environ[name] for name in PROVIDERS[self.provider][1] if os.environ.get(name)}

    def environment(self):
        env = self.credentials()
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
)
TRANSIENT_ERRORS = (
    "rate limit", "rate_limit", "too many requests", "429", "502", "503", "504",
    "overloaded", "connection reset", "connection refused", "temporarily unavailable",
    "failed to fetch", "transport error", "connection error", "request timeout",
)


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
                or (event.get("type") == "result" and str(event.get("subtype", "")).startswith("error"))):
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
    return message[:1000]


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
        message = event.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            records.append(message["usage"])
    return records
