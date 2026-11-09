"""Apply the narrow provider change to either local or older deployed code."""
from pathlib import Path
import sys


def patch(root):
    target = Path(root) / "leadbot" / "classifier.py"
    text = target.read_text(encoding="utf-8")
    if 'if LLM_BACKEND == "codex_cli":' in text:
        return
    text = text.replace('def provider_list():\n',
        'def provider_list():\n    if LLM_BACKEND == "codex_cli":\n'
        '        return [("codex_cli", os.getenv("CODEX_MODEL") or "default")]\n', 1)
    text = text.replace('        if provider == "claude_cli":',
                        '        if provider in {"claude_cli", "codex_cli"}:', 1)
    text = text.replace('                card = run_claude_cli(system, content, model)',
        '                if provider == "codex_cli":\n'
        '                    from .codex_backend import classify as run_codex\n'
        '                    card = run_codex(system, content, model)\n'
        '                else:\n'
        '                    card = run_claude_cli(system, content, model)', 1)
    # Local newer version supports batching. Keep that behavior for both CLIs.
    if 'def _cli_call(' in text:
        marker = '    env = {k: v for k, v in os.environ.items()'
        pos = text.index(marker, text.index('def _cli_call('))
        text = text[:pos] + ('    if LLM_BACKEND == "codex_cli":\n'
            '        from .codex_backend import call\n'
            '        return call(system, prompt, model)\n') + text[pos:]
        text = text.replace('if p == "claude_cli"), None)',
                            'if p in {"claude_cli", "codex_cli"}), None)')
    compile(text, str(target), "exec")
    target.write_text(text, encoding="utf-8", newline="\n")
    pending = Path(root) / "leadbot" / "pending.py"
    text = pending.read_text(encoding="utf-8")
    text = text.replace('LLM_BACKEND == "claude_cli" and',
                        'LLM_BACKEND in {"claude_cli", "codex_cli"} and')
    text = text.replace('key = "claude_cli:" + classifier.batch_model()',
                        'key = LLM_BACKEND + ":" + classifier.batch_model()')
    text = text.replace('пауза Claude CLI', 'пауза LLM CLI')
    compile(text, str(pending), "exec")
    pending.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    patch(sys.argv[1] if len(sys.argv) > 1 else ".")
