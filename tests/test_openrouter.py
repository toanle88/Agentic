"""OpenRouter policy and tool-loop tests."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from threading import Lock
from unittest.mock import patch

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from agentic_runner import context as agentic_context
from agentic_runner import openrouter as agentic_openrouter
from agentic_runner import file_boundaries
from agentic_runner.context import compact_openrouter_request
from agentic_runner import openrouter_tools as agentic_openrouter_tools
from agentic_runner.git_policy import GIT_LOG_METADATA_FORMAT


class OpenRouterTests(unittest.TestCase):
    def test_openrouter_facade_serializes_compatibility_mutation(self):
        observed = []
        state_lock = Lock()
        active = 0
        max_active = 0

        def fake_run(*args, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            observed.append((
                agentic_openrouter._loop._openrouter_request,
                agentic_openrouter._loop._openrouter_tool_call,
                agentic_openrouter._loop.OPENROUTER_MAX_TURNS,
            ))
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return args, kwargs

        original_request = agentic_openrouter._loop._openrouter_request
        original_tool_call = agentic_openrouter._loop._openrouter_tool_call
        original_turns = agentic_openrouter._loop.OPENROUTER_MAX_TURNS
        with patch.object(agentic_openrouter._loop, "run_openrouter", side_effect=fake_run):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(agentic_openrouter.run_openrouter, ("one", "two")))

        self.assertEqual(len(results), 2)
        self.assertEqual(max_active, 1)
        self.assertEqual(
            observed,
            [
                (
                    agentic_openrouter._openrouter_request,
                    agentic_openrouter._openrouter_tool_call,
                    agentic_openrouter.OPENROUTER_MAX_TURNS,
                ),
            ] * 2,
        )
        self.assertIs(agentic_openrouter._loop._openrouter_request, original_request)
        self.assertIs(agentic_openrouter._loop._openrouter_tool_call, original_tool_call)
        self.assertEqual(agentic_openrouter._loop.OPENROUTER_MAX_TURNS, original_turns)

        with patch.object(
            agentic_openrouter._loop,
            "run_openrouter",
            side_effect=RuntimeError("OpenRouter failed"),
        ), self.assertRaises(RuntimeError):
            agentic_openrouter.run_openrouter("failed")
        self.assertIs(agentic_openrouter._loop._openrouter_request, original_request)
        self.assertIs(agentic_openrouter._loop._openrouter_tool_call, original_tool_call)
        self.assertEqual(agentic_openrouter._loop.OPENROUTER_MAX_TURNS, original_turns)

    def test_file_tools_stay_on_open_parent_when_directory_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "worktree"
            root.mkdir()
            (root / "nested").mkdir()
            (root / "nested" / "note.txt").write_text("safe\n", encoding="utf-8")
            outside = base / "outside"
            outside.mkdir()
            (outside / "note.txt").write_text("outside\n", encoding="utf-8")
            original = file_boundaries._open_directory_at

            def replace_parent(descriptor, name):
                opened = original(descriptor, name)
                if name == "nested":
                    (root / "nested").rename(root / "nested-original")
                    try:
                        (root / "nested").symlink_to(outside, target_is_directory=True)
                    except OSError as exc:
                        self.skipTest(f"directory symlinks unavailable: {exc}")
                return opened

            with patch.object(file_boundaries, "_open_directory_at", side_effect=replace_parent):
                result = agentic_openrouter._openrouter_tool_call(
                    root, "read_file", {"path": "nested/note.txt"}, readonly=True,
                )
        self.assertEqual(result, "safe\n")

    def test_file_listing_stays_on_open_directory_when_target_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "worktree"
            root.mkdir()
            (root / "tree").mkdir()
            (root / "tree" / "safe.txt").write_text("safe", encoding="utf-8")
            outside = base / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            original = file_boundaries._open_directory_at

            def replace_target(descriptor, name):
                opened = original(descriptor, name)
                if name == "tree":
                    (root / "tree").rename(root / "tree-original")
                    try:
                        (root / "tree").symlink_to(outside, target_is_directory=True)
                    except OSError as exc:
                        self.skipTest(f"directory symlinks unavailable: {exc}")
                return opened

            with patch.object(file_boundaries, "_open_directory_at", side_effect=replace_target):
                result = agentic_openrouter._openrouter_tool_call(
                    root, "list_files", {"path": "tree"}, readonly=True,
                )
        self.assertIn("tree/safe.txt", result)
        self.assertNotIn("secret.txt", result)

    def test_file_write_stays_on_open_parent_when_directory_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "worktree"
            root.mkdir()
            (root / "nested").mkdir()
            (root / "nested" / "note.txt").write_text("safe\n", encoding="utf-8")
            outside = base / "outside"
            outside.mkdir()
            (outside / "note.txt").write_text("outside\n", encoding="utf-8")
            original = file_boundaries._open_directory_at

            def replace_parent(descriptor, name):
                opened = original(descriptor, name)
                if name == "nested":
                    (root / "nested").rename(root / "nested-original")
                    try:
                        (root / "nested").symlink_to(outside, target_is_directory=True)
                    except OSError as exc:
                        self.skipTest(f"directory symlinks unavailable: {exc}")
                return opened

            with patch.object(file_boundaries, "_open_directory_at", side_effect=replace_parent):
                result = agentic_openrouter._openrouter_tool_call(
                    root, "write_file", {
                        "path": "nested/note.txt", "content": "updated\n",
                    }, readonly=False,
                )
            self.assertEqual((outside / "note.txt").read_text(encoding="utf-8"), "outside\n")
            self.assertEqual(
                (root / "nested-original" / "note.txt").read_text(encoding="utf-8"),
                "updated\n",
            )
        self.assertEqual(result, "Wrote nested/note.txt")

    def assert_process_stopped(self, pid):
        if os.name == 'nt':
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}'],
                capture_output=True, text=True, check=False,
            )
            self.assertNotIn(str(pid), result.stdout)
            return
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_request_budget_trims_large_system_and_task_without_breaking_pairing(self):
        messages = [
            {"role": "system", "content": "system-" + ("s" * 20_000)},
            {"role": "user", "content": "task-" + ("u" * 50_000)},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "x" * 20_000})},
            }]},
            {"role": "tool", "tool_call_id": "call-1", "content": "tool-" + ("t" * 50_000)},
        ]
        stats = compact_openrouter_request(messages, [{"type": "function", "function": {"name": "read_file"}}])
        self.assertLessEqual(stats["request_chars"], stats["budget"])
        self.assertEqual(
            stats["request_chars"],
            len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
            + len(json.dumps([{"type": "function", "function": {"name": "read_file"}}], ensure_ascii=False, separators=(",", ":"))),
        )
        self.assertEqual(messages[2]["tool_calls"][0]["id"], messages[3]["tool_call_id"])
        self.assertEqual(messages[2]["tool_calls"][0]["function"]["arguments"], "{}")

    def test_request_budget_compacts_oversized_tool_schemas(self):
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        tools = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "d" * 20_000,
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }]

        stats = compact_openrouter_request(messages, tools, budget=1_000)

        self.assertLessEqual(stats["request_chars"], stats["budget"])
        self.assertEqual(tools[0]["function"]["name"], "read_file")
        self.assertLessEqual(stats["tool_schema_chars"], 1_000)

    def test_request_budget_compacts_oversized_tool_call_metadata(self):
        messages = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "provider_extension": "x" * 20_000,
                "function": {
                    "name": "read_file",
                    "arguments": {"path": "x" * 20_000},
                },
            }],
        }, {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "result",
        }]

        stats = compact_openrouter_request(messages, [], budget=1_000)

        self.assertLessEqual(stats["request_chars"], stats["budget"])
        self.assertEqual(messages[0]["tool_calls"][0]["id"], messages[1]["tool_call_id"])
        self.assertEqual(messages[0]["tool_calls"][0]["function"]["arguments"], "{}")

    def test_openrouter_permission_policy_network_overrides_legacy_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            responses = iter([
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'call-1', 'function': {
                        'name': 'run_command',
                        'arguments': json.dumps({'argv': ['curl', 'https://example.test']}),
                    }}],
                }}]},
                {'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]},
            ])
            policy = {
                'read_files': True, 'search_files': True, 'edit_files': True,
                'create_files': True, 'git_read': True, 'run_checks': True,
                'network': True, 'allow_dangerous': False,
            }
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', side_effect=lambda *args: next(responses)),
                  patch('agentic_openrouter._openrouter_tool_call', return_value='exit 0') as tool_call):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY', allow_network=False,
                    permission_policy=policy,
                )
            self.assertTrue(ok)
            self.assertEqual(message, 'done')
            self.assertTrue(tool_call.call_args.kwargs['allow_network'])
            log = (root / 'developer.log').read_text(encoding='utf-8')
            self.assertNotIn('Using tool: run_command', log)
            raw = (root / 'developer.jsonl').read_text(encoding='utf-8')
            self.assertIn('"event": "tool_call"', raw)
            self.assertIn('"event": "tool_result"', raw)

    def test_openrouter_requires_model_and_never_reads_key_from_config(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'agent_config.json'
            path.write_text(json.dumps({
                'test_commands': [['go', 'test', './...']],
                'developer': {'provider': 'openrouter'},
            }))
            with self.assertRaisesRegex(ValueError, 'developer.model'):
                agentic.load_config(path)

    def test_openrouter_tool_loop_can_write_only_inside_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'developer-001.txt'
            responses = iter([
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {
                        'name': 'write_file',
                        'arguments': json.dumps({'path': 'hello.txt', 'content': 'hello\n'}),
                    }}],
                }}]},
                {'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]},
            ])

            class Response:
                def __init__(self, value):
                    self.body = json.dumps(value).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def read(self, size=-1):
                    if size < 0:
                        body, self.body = self.body, b''
                        return body
                    body, self.body = self.body[:size], self.body[size:]
                    return body

            def fake_urlopen(request, *args, **kwargs):
                return Response(next(responses))

            with (patch.dict(agentic.os.environ, {'MY_OPENROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter.urllib.request.urlopen', side_effect=fake_urlopen)):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=output, model='openai/gpt-5.4',
                    api_key_env='MY_OPENROUTER_KEY',
                )
            self.assertTrue(ok)
            self.assertEqual(message, 'done')
            self.assertEqual((root / 'hello.txt').read_text(), 'hello\n')
            self.assertNotIn('secret', output.with_suffix('.log').read_text())
            metrics = json.loads((root / 'run.json').read_text())['metrics']
            self.assertEqual(metrics['operations'][0]['role'], 'developer')
            self.assertEqual(metrics['operations'][0]['status'], 'completed')
            with self.assertRaisesRegex(ValueError, 'worktree'):
                agentic_openrouter._safe_worktree_path(root, '../outside')

    def test_openrouter_self_hosted_exception_allows_ci_but_not_policy_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = agentic_openrouter._openrouter_tool_call(
                root, 'write_file', {
                    'path': '.github/workflows/ci.yml',
                    'content': 'name: CI\n',
                }, readonly=False, allow_runner_source=True,
            )
            self.assertEqual(result, 'Wrote .github/workflows/ci.yml')
            self.assertEqual(
                (root / '.github/workflows/ci.yml').read_text(), 'name: CI\n',
            )
            blocked = agentic_openrouter._openrouter_tool_call(
                root, 'write_file', {
                    'path': 'AGENTS.md',
                    'content': 'do not weaken policy\n',
                }, readonly=False, allow_runner_source=True,
            )
            self.assertIn('protected file', blocked)

    def test_openrouter_colors_console_prefixes_but_keeps_log_plain(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            response = {'choices': [{'message': {
                'role': 'assistant', 'content': 'PASSED /tmp/result.txt',
            }}]}
            console = TTYBuffer()
            with (patch.dict(agentic.os.environ, {
                      'ROUTER_KEY': 'secret', 'NO_COLOR': '', 'AGENTIC_PLAIN': '',
                  }, clear=False),
                  patch('sys.stdout', console),
                  patch('agentic_openrouter._openrouter_request', return_value=response)):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY',
                )

            self.assertTrue(ok)
            self.assertEqual(message, 'PASSED /tmp/result.txt')
            self.assertIn('\033[90m[', console.getvalue())
            self.assertIn('\033[36m[DEVELOPER]\033[0m', console.getvalue())
            self.assertNotIn('\033[32mPASSED\033[0m', console.getvalue())
            log = (root / 'developer.log').read_text(encoding='utf-8')
            self.assertNotIn('\033[', log)
            self.assertIn('[DEVELOPER] Finished in', log)

    def test_openrouter_verbose_keeps_transient_tool_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            responses = iter([
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'call-1', 'function': {
                        'name': 'list_files', 'arguments': '{}',
                    }}],
                }}]},
                {'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]},
            ])
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request',
                        side_effect=lambda *args: next(responses)),
                  patch('agentic_openrouter._openrouter_tool_call', return_value='ok')):
                ok, _ = agentic.run_openrouter(
                    root, 'prompt', output=root / 'verbose.txt', model='openai/test',
                    api_key_env='ROUTER_KEY', verbose=True,
                )
            self.assertTrue(ok)
            self.assertIn(
                'Using tool: list_files',
                (root / 'verbose.log').read_text(encoding='utf-8'),
            )

    def test_openrouter_colors_generated_error_without_coloring_provider_message(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            console = TTYBuffer()
            with (patch.dict(agentic.os.environ, {
                      'ROUTER_KEY': 'secret', 'NO_COLOR': '', 'AGENTIC_PLAIN': '',
                  }, clear=False),
                  patch('sys.stdout', console),
                  patch('agentic_openrouter._openrouter_request',
                        side_effect=RuntimeError('provider says PASSED'))):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY',
                )

            self.assertFalse(ok)
            self.assertEqual(message, 'provider says PASSED')
            rendered = console.getvalue()
            self.assertIn('\033[31mERROR\033[0m: provider says PASSED', rendered)
            self.assertNotIn('\033[32mPASSED\033[0m', rendered)
            log = (root / 'developer.log').read_text(encoding='utf-8')
            self.assertIn('ERROR: provider says PASSED', log)
            self.assertNotIn('\033[', log)

    def test_openrouter_tools_cover_readonly_boundaries_and_file_operations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'src').mkdir()
            (root / 'src' / 'main.go').write_text('package main\n')
            (root / '.git').mkdir()

            names = [item['function']['name'] for item in agentic_openrouter._openrouter_tools(True)]
            self.assertEqual(names, ['list_files', 'read_file', 'search_files', 'run_command'])
            developer_names = [
                item['function']['name']
                for item in agentic_openrouter._openrouter_tools(False)
            ]
            self.assertIn('write_file', developer_names)
            self.assertIn('edit_file', developer_names)
            self.assertIn('src/main.go', agentic_openrouter._openrouter_tool_call(
                root, 'list_files', {'path': '.'}, readonly=True
            ))
            self.assertEqual(
                agentic_openrouter._openrouter_tool_call(root, 'read_file', {'path': 'src/main.go'}, readonly=True),
                'package main\n',
            )
            (root / 'src' / 'version.py').write_text('__version__ = "1.1.115"\n', encoding='utf-8')
            search_result = agentic_openrouter._openrouter_tool_call(
                root, 'search_files', {'query': '__version__', 'path': 'src'}, readonly=True,
            )
            self.assertIn('src/version.py:1: __version__ = "1.1.115"', search_result)
            self.assertEqual(
                agentic_openrouter._openrouter_tool_call(
                    root, 'search_files', {'query': 'MISSING'}, readonly=True,
                ),
                '(no matches)',
            )
            self.assertIn('ERROR: write_file is unavailable', agentic_openrouter._openrouter_tool_call(
                root, 'write_file', {'path': 'new.txt', 'content': 'nope'}, readonly=True
            ))
            self.assertFalse((root / 'new.txt').exists())
            self.assertIn('exit 0', agentic_openrouter._openrouter_tool_call(
                root, 'run_command', {'argv': ['pwd']}, readonly=False
            ))
            self.assertIn('ERROR:', agentic_openrouter._openrouter_tool_call(
                root, 'list_files', {'path': 'src/main.go'}, readonly=True
            ))
            self.assertIn('ERROR: protected file', agentic_openrouter._openrouter_tool_call(
                root, 'write_file', {'path': '.env', 'content': 'secret'}, readonly=False
            ))
            (root / '.env').write_text('PRIVATE=do-not-send\n')
            self.assertIn('ERROR: protected file', agentic_openrouter._openrouter_tool_call(
                root, 'read_file', {'path': '.env'}, readonly=False
            ))
            self.assertNotIn('.env', agentic_openrouter._openrouter_tool_call(
                root, 'list_files', {'path': '.'}, readonly=False
            ))
            self.assertIn('ERROR: unknown tool', agentic_openrouter._openrouter_tool_call(
                root, 'unknown', {}, readonly=False
            ))

    def test_openrouter_edit_file_replaces_one_exact_block(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'module.py'
            original = 'header\nVERSION = "1.1.115"\nfooter\n'
            target.write_text(original, encoding='utf-8')

            result = agentic_openrouter._openrouter_tool_call(
                root, 'edit_file', {
                    'path': 'module.py',
                    'old_text': 'VERSION = "1.1.115"',
                    'new_text': 'VERSION = "1.1.116"',
                }, readonly=False,
            )
            self.assertEqual(result, 'Edited module.py')
            self.assertEqual(
                target.read_text(encoding='utf-8'),
                'header\nVERSION = "1.1.116"\nfooter\n',
            )

            for old_text in ('MISSING', 'header\n'):
                before = target.read_text(encoding='utf-8')
                error = agentic_openrouter._openrouter_tool_call(
                    root, 'edit_file', {
                        'path': 'module.py', 'old_text': old_text,
                        'new_text': 'replacement',
                    }, readonly=False,
                )
                if old_text == 'MISSING':
                    self.assertIn('was not found', error)
                else:
                    # This block occurs once, so verify a valid edit is not
                    # accidentally rejected merely because it is at the start.
                    self.assertEqual(error, 'Edited module.py')
                    target.write_text(before, encoding='utf-8')
                self.assertEqual(target.read_text(encoding='utf-8'), before)

    def test_openrouter_blocks_full_write_after_truncated_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'large.py'
            original = ('keep this file intact\n' + ('x' * 13_000) + '\n')
            target.write_text(original, encoding='utf-8')
            responses = iter([
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'read', 'function': {
                        'name': 'read_file',
                        'arguments': json.dumps({'path': 'large.py'}),
                    }}],
                }}]},
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'write', 'function': {
                        'name': 'write_file',
                        'arguments': json.dumps({
                            'path': 'large.py', 'content': 'damaged\n',
                        }),
                    }}],
                }}]},
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'finish', 'function': {
                        'name': 'finish', 'arguments': '{"summary":"done"}',
                    }}],
                }}]},
            ])
            bodies = []

            def request(_url, _key, body, _timeout):
                bodies.append(body)
                return next(responses)

            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', side_effect=request)):
                ok, message = agentic.run_openrouter(
                    root, 'preserve the file', output=root / 'developer.txt',
                    model='openai/test', api_key_env='ROUTER_KEY', max_tool_turns=10,
                )

            self.assertTrue(ok)
            self.assertEqual(message, 'done')
            self.assertEqual(target.read_text(encoding='utf-8'), original)
            self.assertTrue(any(
                'truncated content for this path' in message.get('content', '')
                for message in bodies[2]['messages']
                if message.get('role') == 'tool'
            ))

    def test_openrouter_command_policy_rejects_shell_network_and_review_mutations(self):
        for argv in ([], ['bash', '-c', 'echo unsafe'], ['curl', 'https://example.test'],
                     ['git', 'push'], ['git', 'checkout', 'main'],
                     ['git', '-C', '../other', 'status'],
                     ['git', '-c', 'core.sshCommand=ssh', 'status'],
                     ['python', '-c', 'print(1)'],
                     ['python', '-cprint(1)'],
                     ['node', '-econsole.log(1)'],
                     ['node', '-p', "require('child_process').execSync('id')"],
                     ['node', '-pprocess.env'],
                     ['node', '--print', 'process.env'],
                     ['php', '-r', 'system("id");'],
                     ['php', '-rsystem("id");'],
                     ['php', '--run', 'system("id");'],
                     ['python', '-m', 'pip'],
                     ['python3.11', '-c', 'print(1)'],
                     ['python3.11', '-m', 'pip'],
                     ['python3.11.exe', '-c', 'print(1)'],
                     ['python.exe', '-m', 'pip'],
                     ['node20', '-econsole.log(1)'],
                     ['node20', '-m', 'pip'],
                     ['node20.exe', '-econsole.log(1)'],
                     ['php8.2', '-rsystem("id");'],
                     ['php8.2', '-m', 'unittest'],
                     ['php8.2.exe', '-rsystem("id");'],
                     ['perl', '-m', 'unittest'],
                     ['perl', '-munittest'],
                     ['python', 'malicious.py', '-m', 'unittest'],
                     ['python', 'malicious.py', '-munittest'],
                     ['python', '-m'],
                     ['node', '--eval', 'process.exit(1)']):
            allowed, reason = agentic_openrouter._openrouter_command_allowed(argv, readonly=False)
            self.assertFalse(allowed, argv)
            self.assertTrue(reason)

        allowed, reason = agentic_openrouter._openrouter_command_allowed(
            ['curl', 'https://example.test'], readonly=False, allow_network=True
        )
        self.assertTrue(allowed, reason)
        for argv in (
            ['git', 'status'], ['git', 'diff', '--', 'tracked.txt'],
            ['git', 'grep', 'needle', '--', 'tracked.txt'], ['rg', 'needle', 'README.md'],
        ):
            allowed, reason = agentic_openrouter._openrouter_command_allowed(argv, readonly=True)
            self.assertTrue(allowed, reason)
        for argv in (['python', '-c', 'print(1)'], ['git', 'fetch']):
            allowed, reason = agentic_openrouter._openrouter_command_allowed(argv, readonly=True)
            self.assertFalse(allowed, argv)
            self.assertTrue(reason)

        allowed, reason = agentic_openrouter._openrouter_command_allowed(
            ['python', '-m', 'unittest'], readonly=False
        )
        self.assertTrue(allowed, reason)

    def test_openrouter_rejects_path_qualified_readonly_lookalikes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lookalike = root / 'ls'
            lookalike.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
            lookalike.chmod(0o700)

            blocked = (
                ['../ls'],
                ['./ls'],
                ['./git', 'status'],
                [str(lookalike)],
                [str(root / 'git'), 'status'],
            )
            with patch(
                'agentic_openrouter_tools._bounded_command_capture',
                side_effect=AssertionError('path-qualified command must not run'),
            ) as capture:
                for argv in blocked:
                    allowed, reason = agentic_openrouter._openrouter_command_allowed(
                        argv, readonly=True, worktree=root,
                    )
                    self.assertFalse(allowed, (argv, reason))
                    self.assertIn('path-qualified', reason)
                    result = agentic_openrouter._openrouter_tool_call(
                        root, 'run_command', {'argv': argv}, readonly=True,
                    )
                    self.assertIn('ERROR:', result, argv)
            capture.assert_not_called()

            with patch(
                'agentic_openrouter_tools._bounded_command_capture',
                return_value=(0, 'public\n', False, False),
            ) as capture:
                result = agentic_openrouter._openrouter_tool_call(
                    root, 'run_command', {'argv': ['ls', 'public.txt']}, readonly=True,
                )
            self.assertIn('exit 0', result)
            self.assertEqual(capture.call_args.args[0], ['ls', 'public.txt'])

    def test_openrouter_developer_inspection_stays_inside_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root.parent / 'outside-secret.txt'
            outside.write_text('outside\n', encoding='utf-8')
            outside_link = root / 'outside-link'
            try:
                outside_link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f'symlinks unavailable: {exc}')
            for argv in (
                ['rg', 'needle', '/etc'],
                ['ls', '../outside'],
                ['file', str(root.parent / 'outside')],
                ['rg', 'needle', 'outside-link'],
            ):
                allowed, reason = agentic_openrouter._openrouter_command_allowed(
                    argv, readonly=False, worktree=root,
                )
                self.assertFalse(allowed, (argv, reason))
                self.assertIn('worktree', reason)
            allowed, reason = agentic_openrouter._openrouter_command_allowed(
                ['rg', 'needle', 'README.md'], readonly=False, worktree=root,
            )
            self.assertTrue(allowed, reason)

    def test_openrouter_readonly_commands_resolve_and_protect_path_operands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'worktree'
            outside = Path(temp) / 'outside-secret.txt'
            root.mkdir()
            outside.write_text('outside\n', encoding='utf-8')
            (root / '.env').write_text('TOKEN=private\n', encoding='utf-8')
            (root / 'private.pem').write_text('PRIVATE=do-not-scan\n', encoding='utf-8')
            (root / 'public.txt').write_text('public\n', encoding='utf-8')
            outside_link = root / 'outside-link'
            env_link = root / 'env-link'
            dash_env_link = root / '-env-link'
            try:
                outside_link.symlink_to(outside)
                env_link.symlink_to(root / '.env')
                dash_env_link.symlink_to(root / '.env')
            except OSError as exc:
                self.skipTest(f'symlinks unavailable: {exc}')

            blocked = (
                ['rg', 'needle', 'outside-link'],
                ['ls', 'env-link'],
                ['rg', '--hidden', 'TOKEN', '.'],
                ['rg', '--follow', 'needle', '.'],
                ['rg', '-uu', 'TOKEN', '.'],
                ['rg', '-f', '.env', 'public.txt'],
                ['rg', '--file=private.pem', 'public.txt'],
                ['rg', '--ignore-file=.env', 'needle', 'public.txt'],
                ['rg', '--files-from', 'public.txt', 'needle'],
                ['rg', 'needle', '.'],
                ['rg', '--type', 'py', 'needle'],
                ['rg', 'TOKEN'],
                ['grep', '-r', 'TOKEN', '.'],
                ['grep', '-R', 'TOKEN', '.'],
                ['grep', '--file=.env', 'public.txt'],
                ['grep', '-f.env', 'public.txt'],
                ['grep', '--exclude-from=.env', 'public.txt'],
                ['grep', 'TOKEN', '--', 'env-link'],
                ['grep', 'TOKEN', '--', '-env-link'],
                ['ls', '-R', '.'],
                ['git', 'diff'],
                ['git', 'grep', 'TOKEN'],
                ['git', 'grep', 'TOKEN', '--', '.'],
                ['git', 'grep', 'TOKEN', '--', 'outside-link'],
                ['git', 'grep', 'TOKEN', '--', 'env-link'],
                ['git', 'status', '--', '-env-link'],
                ['git', 'show', 'HEAD'],
                ['git', 'log', '-p'],
                ['git', 'log', '-p', 'HEAD'],
                ['git', 'log', '-p', '--', '.'],
                ['git', 'log', '-p', '--', 'public.txt'],
                ['git', 'log', '-p', '--', 'deleted-dir'],
                ['git', 'log', '--patch-with-stat'],
                ['git', 'log', '--patch-with-raw', 'HEAD'],
                ['git', 'log', '-pu'],
                ['git', 'log', '-pu', '--', 'public.txt'],
                ['git', 'log', '--follow', '--', 'public.txt'],
                ['git', 'log', '-L', '1,1:public.txt'],
                ['git', 'log', '-L1,1:public.txt'],
                ['git', 'log', '--word-diff'],
                ['git', 'log', '--word-diff=porcelain'],
                ['git', 'log', '--word-diff-regex=.'],
                ['git', 'log', '--color-words'],
                ['git', 'log', '--stat'],
                ['git', 'log', '--raw'],
                ['git', 'log', '--name-only'],
                ['git', 'log', '--diff-merges=first-parent'],
                ['git', 'log', '--unknown-future-diff-option'],
                ['git', 'diff', '--', 'outside-link'],
                ['git', 'show', 'HEAD:.env'],
                ['git', 'show', 'HEAD:.env', '--'],
                ['git', 'show', 'HEAD:deleted-dir'],
                ['git', 'show', 'HEAD:deleted-dir', '--'],
                ['git', 'diff', '--', ':(top).env'],
                ['git', 'diff', '--', '*.env'],
                ['git', 'diff', '--', '.'],
            )
            with patch(
                'agentic_openrouter_tools._bounded_command_capture',
                side_effect=AssertionError('blocked command must not be captured'),
            ) as capture:
                for argv in blocked:
                    result = agentic_openrouter._openrouter_tool_call(
                        root, 'run_command', {'argv': argv}, readonly=True,
                    )
                    self.assertIn('ERROR:', result, argv)
            capture.assert_not_called()

            with patch(
                'agentic_openrouter_tools._bounded_command_capture',
                return_value=(0, 'public\n', False, False),
            ) as capture:
                result = agentic_openrouter._openrouter_tool_call(
                    root, 'run_command', {'argv': ['rg', 'needle', 'public.txt']}, readonly=True,
                )
            self.assertIn('exit 0', result)
            capture.assert_called_once()

            with patch(
                'agentic_openrouter_tools._bounded_command_capture',
                return_value=(0, 'public\n', False, False),
            ) as capture:
                result = agentic_openrouter._openrouter_tool_call(
                    root, 'run_command',
                    {'argv': ['git', 'grep', 'needle', '--', 'public.txt']},
                    readonly=True,
                )
            self.assertIn('exit 0', result)
            capture.assert_called_once()

    def test_openrouter_git_log_rejects_history_content_after_protected_rename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def run(*argv):
                return subprocess.run(
                    ['git', *argv], cwd=root, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )

            run('init', '-q')
            (root / '.env').write_text('historical-secret\n', encoding='utf-8')
            run('add', '.env')
            run('-c', 'user.email=test@example.com', '-c', 'user.name=Test',
                'commit', '-qm', 'protected')
            (root / '.env').rename(root / 'public.txt')
            run('add', '-A')
            run('-c', 'user.email=test@example.com', '-c', 'user.name=Test',
                'commit', '-qm', 'rename')

            for argv in (
                ['git', 'log', '-p', '--', 'public.txt'],
                ['git', 'log', '-pu', '--', 'public.txt'],
                ['git', 'log', '--follow', '--', 'public.txt'],
                ['git', 'log', '-L1,1:public.txt'],
            ):
                with self.subTest(argv=argv), patch(
                    'agentic_openrouter_tools._bounded_command_capture',
                    side_effect=AssertionError('blocked command must not be captured'),
                ) as capture:
                    result = agentic_openrouter._openrouter_tool_call(
                        root, 'run_command', {'argv': argv}, readonly=True,
                    )
                self.assertIn('ERROR:', result)
                self.assertNotIn('historical-secret', result)
                capture.assert_not_called()

            for argv in (
                ['git', 'log'],
                ['git', 'log', '--oneline', '--', 'public.txt'],
                ['git', 'log', '--pretty=%h', '--max-count=1', '--', 'public.txt'],
                ['git', 'log', '--format=%B', '--', 'public.txt'],
                ['git', 'log', '--pretty=format:%N', '--', 'public.txt'],
                ['git', 'log', '--decorate=short', '--', 'public.txt'],
            ):
                allowed, reason = agentic_openrouter._openrouter_command_allowed(
                    argv, readonly=True, worktree=root,
                )
                self.assertFalse(allowed, (argv, reason))

            for argv in (
                ['git', 'log', f'--format={GIT_LOG_METADATA_FORMAT}', '--', 'public.txt'],
                ['git', 'log', f'--pretty=format:{GIT_LOG_METADATA_FORMAT}', '--max-count=1'],
            ):
                allowed, reason = agentic_openrouter._openrouter_command_allowed(
                    argv, readonly=True, worktree=root,
                )
                self.assertTrue(allowed, (argv, reason))

    def test_openrouter_read_policy_matches_provider_protected_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.github' / 'workflows').mkdir(parents=True)
            for relative in ('agentic.py', 'agent_config.json', '.github/workflows/ci.yml'):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('protected', encoding='utf-8')

            for relative in ('agentic.py', 'agent_config.json', '.github/workflows/ci.yml'):
                with self.subTest(relative=relative):
                    result = agentic_openrouter._openrouter_tool_call(
                        root, 'read_file', {'path': relative}, readonly=True,
                    )
                    self.assertIn('protected file', result)
                    allowed, reason = agentic_openrouter._openrouter_command_allowed(
                        ['git', 'show', f'HEAD:{relative}'],
                        readonly=True, worktree=root,
                    )
                    self.assertFalse(allowed, (relative, reason))

    def test_openrouter_git_diff_disables_malicious_external_diff_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / 'external-diff-ran'
            helper = root / 'external-diff'
            helper.write_text(
                f'#!/bin/sh\ntouch {marker}\n', encoding='utf-8',
            )
            helper.chmod(0o700)
            def run(*argv):
                return subprocess.run(
                    ['git', *argv], cwd=root, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
            run('init', '-q')
            run('config', 'diff.external', str(helper))
            (root / 'tracked.txt').write_text('before\n', encoding='utf-8')
            run('add', 'tracked.txt')
            (root / 'tracked.txt').write_text('after\n', encoding='utf-8')
            result = agentic_openrouter._openrouter_tool_call(
                root, 'run_command', {'argv': ['git', 'diff', '--', 'tracked.txt']}, readonly=True,
            )
            self.assertNotIn('ERROR:', result)
            self.assertFalse(marker.exists(), result)

    def test_openrouter_git_show_rejects_historical_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def run(*argv):
                return subprocess.run(
                    ['git', *argv], cwd=root, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )

            run('init', '-q')
            run('-c', 'user.email=test@example.com', '-c', 'user.name=Test',
                'config', 'core.autocrlf', 'false')
            old_directory = root / 'history-transition'
            old_directory.mkdir()
            (old_directory / 'private.pem').write_text('historical\n', encoding='utf-8')
            run('add', '.')
            run('-c', 'user.email=test@example.com', '-c', 'user.name=Test',
                'commit', '-qm', 'directory')
            (old_directory / 'private.pem').unlink()
            old_directory.rmdir()
            old_directory.write_text('current regular file\n', encoding='utf-8')
            run('add', '.')
            run('-c', 'user.email=test@example.com', '-c', 'user.name=Test',
                'commit', '-qm', 'file')

            for argv in (['git', 'show', 'HEAD~1:history-transition'],):
                allowed, reason = agentic_openrouter._openrouter_command_allowed(
                    argv, readonly=True, worktree=root,
                )
                self.assertFalse(allowed, (argv, reason))
                self.assertIn('historical director', reason)

    def test_openrouter_commands_use_configured_sandbox(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = {
                'enabled': True, 'engine': 'docker', 'image': 'test-image',
                'allow_network': False, 'pass_env': ['OPENAI_API_KEY'],
            }
            with (patch.dict(agentic.os.environ, {'OPENAI_API_KEY': 'secret'}, clear=True),
                  patch('agentic_openrouter_tools._bounded_command_capture', return_value=(
                      0, 'sandboxed\n', False, False,
                  )) as run):
                result = agentic_openrouter._openrouter_tool_call(
                    root, 'run_command', {'argv': ['python', '-m', 'unittest']},
                    readonly=False, sandbox=settings,
                )
            self.assertIn('sandboxed', result)
            process_argv = run.call_args.args[0]
            self.assertEqual(process_argv[:2], ['docker', 'run'])
            self.assertIn('--network', process_argv)
            self.assertEqual(process_argv[process_argv.index('--network') + 1], 'none')
            self.assertNotIn('OPENAI_API_KEY', process_argv)
            self.assertNotIn('secret', process_argv)

    def test_openrouter_validation_runner_does_not_inherit_worktree_git_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = {
                'enabled': True, 'engine': 'docker', 'image': 'test-image',
                'allow_network': False,
            }
            with (
                patch('agentic_openrouter_tools.sandbox_command', return_value=['python', '-m', 'unittest']) as sandbox,
                patch('agentic_openrouter_tools._bounded_command_capture', return_value=(
                    0, 'ok\n', False, False,
                )),
            ):
                result = agentic_openrouter._openrouter_tool_call(
                    root, 'run_command', {'argv': ['python', '-m', 'unittest']},
                    readonly=False, sandbox=settings,
                )

        self.assertIn('exit 0', result)
        self.assertFalse(sandbox.call_args.kwargs['preserve_git_metadata'])

    def test_openrouter_commands_receive_a_restricted_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (
                patch.dict(agentic.os.environ, {
                    'OPENROUTER_API_KEY': 'router-secret',
                    'OTHER_PROVIDER_TOKEN': 'provider-secret',
                    'PATH': agentic.os.environ.get('PATH', ''),
                }, clear=True),
                patch('agentic_openrouter_tools._bounded_command_capture', return_value=(
                    0, 'ok\n', False, False,
                )) as capture,
            ):
                result = agentic_openrouter._openrouter_tool_call(
                    root, 'run_command', {'argv': ['python', '-m', 'unittest']},
                    readonly=False,
                )

            self.assertIn('exit 0', result)
            environment = capture.call_args.kwargs['env']
            self.assertNotIn('OPENROUTER_API_KEY', environment)
            self.assertNotIn('OTHER_PROVIDER_TOKEN', environment)
            self.assertIn('PATH', environment)

    def test_openrouter_network_denial_overrides_sandbox_network_setting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = {
                'enabled': True, 'engine': 'docker', 'image': 'test-image',
                'allow_network': True,
            }
            with patch('agentic_openrouter_tools._bounded_command_capture', return_value=(
                0, 'sandboxed\n', False, False,
            )) as run:
                result = agentic_openrouter._openrouter_tool_call(
                    root, 'run_command', {'argv': ['python', '-m', 'unittest']},
                    readonly=False, sandbox=settings, allow_network=False,
                )
            self.assertIn('sandboxed', result)
            process_argv = run.call_args.args[0]
            self.assertEqual(process_argv[process_argv.index('--network') + 1], 'none')


    def test_openrouter_request_translates_http_network_and_payload_errors(self):
        from io import BytesIO

        class Response:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size=-1):
                if size < 0:
                    return self.body
                body, self.body = self.body[:size], self.body[size:]
                return body

        cases = [
            (agentic_openrouter.urllib.error.HTTPError(
                'https://router.test', 401, 'unauthorized', {}, BytesIO(b'bad key')
            ), '401'),
            (agentic_openrouter.urllib.error.URLError('offline'), 'offline'),
            (json.JSONDecodeError('bad json', '!', 0), 'bad json'),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected), patch(
                'agentic_openrouter.urllib.request.urlopen', side_effect=error
            ):
                with self.assertRaisesRegex(RuntimeError, 'OpenRouter request failed') as raised:
                    agentic_openrouter._openrouter_request(
                        'https://router.test/chat/completions', 'secret', {}, timeout=2,
                        allow_custom_endpoint=True,
                    )
                self.assertIn(expected, str(raised.exception))

        for body, message in [(b'[]', 'non-object'), (b'{"error":"quota"}', 'quota')]:
            with self.subTest(message=message), patch(
                'agentic_openrouter.urllib.request.urlopen', return_value=Response(body)
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    agentic_openrouter._openrouter_request(
                        'https://router.test/chat/completions', 'secret', {}, timeout=None,
                        allow_custom_endpoint=True,
                    )

        for error in (
            OSError('response read failed'),
            socket.timeout('response timed out'),
            http.client.IncompleteRead(b'partial'),
            http.client.HTTPException('response protocol failed'),
        ):
            with self.subTest(response_error=type(error).__name__):
                class FailingResponse:
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        return False
                    def read(self, size=-1):
                        raise error

                with patch(
                    'agentic_openrouter.urllib.request.urlopen',
                    return_value=FailingResponse(),
                ):
                    with self.assertRaisesRegex(RuntimeError, 'OpenRouter request failed'):
                        agentic_openrouter._openrouter_request(
                            'https://router.test/chat/completions', 'secret', {}, timeout=2,
                            allow_custom_endpoint=True,
                        )

    def test_openrouter_response_read_failure_persists_failed_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            class FailingResponse:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def read(self, size=-1):
                    raise http.client.IncompleteRead(b'partial')

            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter.urllib.request.urlopen', return_value=FailingResponse())):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY',
                )

            self.assertFalse(ok)
            self.assertIn('OpenRouter request failed', message)
            self.assertIn('OpenRouter request failed', (root / 'developer.txt').read_text())
            record = json.loads((root / 'run.json').read_text(encoding='utf-8'))
            self.assertEqual(record['metrics']['operations'][0]['status'], 'failed')
            self.assertEqual(record['metrics']['operations'][0]['exit_code'], 1)

    def test_openrouter_request_rejects_oversized_http_body(self):
        class Response:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, size=-1):
                body, self.body = self.body[:size], self.body[size:]
                return body

        body = b'{"choices":[]}' + b"x" * agentic_openrouter_tools.OPENROUTER_HTTP_RESPONSE_BYTES
        with patch('agentic_openrouter.urllib.request.urlopen', return_value=Response(body)):
            with self.assertRaisesRegex(RuntimeError, 'exceeded'):
                agentic_openrouter._openrouter_request(
                    'https://router.test/chat/completions', 'secret', {}, timeout=None,
                    allow_custom_endpoint=True,
                )

    def test_openrouter_raw_events_and_final_message_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            oversized = {
                'choices': [{'message': {'role': 'assistant', 'content': 'done'}}],
                'provider_trace': 'x' * (agentic_openrouter._loop.OPENROUTER_RAW_EVENT_BYTES + 1),
            }
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', return_value=oversized)):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY',
                )
            self.assertTrue(ok)
            self.assertEqual(message, 'done')
            raw_path = root / 'developer.jsonl'
            self.assertLessEqual(raw_path.stat().st_size, agentic_openrouter._loop.OPENROUTER_RAW_LOG_BYTES)
            self.assertIn('agentic_stream_truncated', raw_path.read_text(encoding='utf-8'))

            huge_message = 'z' * (agentic_openrouter._loop.OPENROUTER_FINAL_MESSAGE_CHARS + 100)
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', return_value={
                      'choices': [{'message': {'role': 'assistant', 'content': huge_message}}],
                  })):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer-2.txt', model='openai/test',
                    api_key_env='ROUTER_KEY',
                )
            self.assertTrue(ok)
            self.assertIn('agentic_stream_truncated', message)
            self.assertLessEqual(
                len((root / 'developer-2.txt').read_text(encoding='utf-8')),
                agentic_openrouter._loop.OPENROUTER_FINAL_MESSAGE_CHARS,
            )

    def test_openrouter_tool_turn_limit_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'developer.txt'
            response = {'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'call', 'function': {
                    'name': 'unknown', 'arguments': '{not-json',
                }}],
            }}]}
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch.object(agentic_openrouter, 'OPENROUTER_MAX_TURNS', 2),
                  patch('agentic_openrouter._openrouter_request', return_value=response)):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=output, model='openai/test',
                    api_key_env='ROUTER_KEY', timeout=None,
                )
            self.assertFalse(ok)
            self.assertIn('exceeded 2 tool turns', message)
            self.assertIn('exceeded 2 tool turns', output.read_text())

    def test_openrouter_reports_repeated_tool_calls_and_preserves_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'notes.txt').write_text('notes\n', encoding='utf-8')
            response = {'choices': [{'message': {
                'role': 'assistant', 'content': 'I am still inspecting the file.',
                'tool_calls': [{'id': 'call', 'function': {
                    'name': 'read_file', 'arguments': '{"path":"notes.txt"}',
                }}],
            }}]}
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', return_value=response)):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY', max_tool_turns=10,
                )

        self.assertFalse(ok)
        self.assertIn('repeated the same tool call 4 times', message)
        self.assertIn('Last assistant message: I am still inspecting the file.', message)

    def test_openrouter_structured_readonly_run_forces_final_response_after_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'notes.txt').write_text('notes\n', encoding='utf-8')
            schema = root / 'schema.json'
            schema.write_text('{}', encoding='utf-8')
            repeated = {'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'call', 'function': {
                    'name': 'read_file', 'arguments': '{"path":"notes.txt"}',
                }}],
            }}]}
            final = {'choices': [{'message': {
                'role': 'assistant', 'content': '{"summary":"inspected","findings":[]}',
            }}]}
            responses = iter([repeated] * 5 + [final])
            bodies = []

            def request(_url, _key, body, _timeout):
                bodies.append(body)
                return next(responses)

            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', side_effect=request)):
                ok, message = agentic.run_openrouter(
                    root, 'inspect the repository', output=root / 'review.txt',
                    model='openai/test', api_key_env='ROUTER_KEY', readonly=True,
                    schema=schema, max_tool_turns=10,
                )

            self.assertTrue(ok)
            self.assertEqual(message, '{"summary":"inspected","findings":[]}')
            self.assertEqual(bodies[-1]['tools'], [])
            self.assertEqual(bodies[-1]['tool_choice'], 'none')

    def test_openrouter_duplicate_tool_warning_can_recover(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'notes.txt').write_text('notes\n', encoding='utf-8')
            repeated = {'choices': [{'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [{'id': 'call', 'function': {
                    'name': 'read_file', 'arguments': '{"path":"notes.txt"}',
                }}],
            }}]}
            done = {'choices': [{'message': {
                'role': 'assistant', 'content': 'done',
            }}]}
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request',
                        side_effect=[repeated] * 5 + [done])):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY', max_tool_turns=10,
                )

        self.assertTrue(ok)
        self.assertEqual(message, 'done')

    def test_openrouter_finish_tool_ends_developer_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            responses = [
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'finish', 'function': {
                        'name': 'finish',
                        'arguments': '{"summary":"implemented and checked"}',
                    }}],
                }}]},
            ]
            bodies = []

            def request(_url, _key, body, _timeout):
                bodies.append(body)
                return responses.pop(0)

            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', side_effect=request)):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY', max_tool_turns=10,
                )
        self.assertTrue(ok)
        self.assertEqual(message, 'implemented and checked')
        self.assertEqual(len(bodies), 1)
        self.assertIn(
            'finish',
            [item['function']['name'] for item in bodies[0]['tools']],
        )

    def test_openrouter_validate_phase_blocks_exploration_until_check_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            responses = [
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'write', 'function': {
                        'name': 'write_file',
                        'arguments': '{"path":"version.txt","content":"2\\n"}',
                    }}],
                }}]},
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'read', 'function': {
                        'name': 'read_file',
                        'arguments': '{"path":"version.txt"}',
                    }}],
                }}]},
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'finish', 'function': {
                        'name': 'finish',
                        'arguments': '{"summary":"implemented"}',
                    }}],
                }}]},
            ]
            results = []

            def request(_url, _key, body, _timeout):
                results.append(json.loads(json.dumps(body)))
                return responses.pop(0)

            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', side_effect=request)):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY', max_tool_turns=10,
                    check_commands=[['python', '-m', 'unittest']],
                )

        self.assertTrue(ok)
        self.assertEqual(message, 'implemented')
        self.assertEqual(len(results), 3)
        self.assertIn(
            'validate phase',
            results[2]['messages'][-1]['content'],
        )

    def test_openrouter_failed_edit_reopens_repair_inspection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            responses = [
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'write', 'function': {
                        'name': 'write_file',
                        'arguments': '{"path":"version.txt","content":"1\\n1\\n"}',
                    }}],
                }}]},
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'edit', 'function': {
                        'name': 'edit_file', 'arguments': json.dumps({
                            'path': 'version.txt', 'old_text': '1\n', 'new_text': '2\n',
                        }),
                    }}],
                }}]},
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'read', 'function': {
                        'name': 'read_file', 'arguments': '{"path":"version.txt"}',
                    }}],
                }}]},
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'finish', 'function': {
                        'name': 'finish', 'arguments': '{"summary":"needs a narrower edit"}',
                    }}],
                }}]},
            ]
            bodies = []

            def request(_url, _key, body, _timeout):
                bodies.append(json.loads(json.dumps(body)))
                return responses.pop(0)

            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', side_effect=request)):
                ok, message = agentic.run_openrouter(
                    root, 'update the version', output=root / 'developer.txt',
                    model='openai/test', api_key_env='ROUTER_KEY', max_tool_turns=10,
                )

            self.assertTrue(ok)
            self.assertEqual(message, 'needs a narrower edit')
            self.assertTrue(any(
                message.get('role') == 'tool' and message.get('content') == '1\n1\n'
                for message in bodies[3]['messages']
            ))

    def test_openrouter_reports_the_underlying_error_after_assistant_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'notes.txt').write_text('notes\n', encoding='utf-8')
            response = {'choices': [{'message': {
                'role': 'assistant', 'content': 'Let me inspect the file.',
                'tool_calls': [{'id': 'call', 'function': {
                    'name': 'read_file', 'arguments': '{"path":"notes.txt"}',
                }}],
            }}]}
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', side_effect=[
                      response, RuntimeError('OpenRouter request failed: connection reset'),
                  ])):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY',
                )

        self.assertFalse(ok)
        self.assertIn('OpenRouter request failed: connection reset', message)
        self.assertIn('Last assistant message: Let me inspect the file.', message)

    def test_openrouter_drops_unknown_assistant_metadata_between_tool_turns(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bodies = []
            responses = iter([
                {'choices': [{'message': {
                    'role': 'assistant', 'content': None,
                    'unknown_provider_metadata': 'x' * 200_000,
                    'tool_calls': [{'id': 'call-1', 'type': 'function',
                                    'unknown_call_metadata': 'y' * 100_000,
                                    'function': {'name': 'read_file', 'arguments': '{"path":"notes.txt"}'}}],
                }}]},
                {'choices': [{'message': {
                    'role': 'assistant', 'content': 'done',
                    'unknown_provider_metadata': 'z' * 200_000,
                }}]},
            ])

            def fake_request(url, api_key, body, timeout):
                bodies.append(json.loads(json.dumps(body)))
                return next(responses)

            (root / 'notes.txt').write_text('notes\n', encoding='utf-8')
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', side_effect=fake_request)):
                ok, message = agentic.run_openrouter(
                    root, 'prompt', output=root / 'developer.txt', model='openai/test',
                    api_key_env='ROUTER_KEY', max_tool_turns=2,
                )
        self.assertTrue(ok)
        self.assertEqual(message, 'done')
        assistant = bodies[1]['messages'][2]
        self.assertEqual(set(assistant), {'role', 'content', 'tool_calls'})
        self.assertEqual(set(assistant['tool_calls'][0]), {'id', 'type', 'function'})
        self.assertNotIn('unknown_provider_metadata', json.dumps(bodies[1]))
        self.assertNotIn('unknown_call_metadata', json.dumps(bodies[1]))

    def test_openrouter_json_mode_is_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bodies = []

            def fake_request(url, api_key, body, timeout):
                bodies.append(body)
                return {'choices': [{'message': {'role': 'assistant', 'content': '{}'}}]}

            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', side_effect=fake_request)):
                agentic.run_openrouter(
                    root, 'prompt', output=root / 'default.txt', model='openai/test',
                    api_key_env='ROUTER_KEY', schema=root / 'schema.json',
                )
                agentic.run_openrouter(
                    root, 'prompt', output=root / 'json.txt', model='openai/test',
                    api_key_env='ROUTER_KEY', schema=root / 'schema.json', json_mode=True,
                )
        self.assertNotIn('response_format', bodies[0])
        self.assertEqual(bodies[1]['response_format'], {'type': 'json_object'})

    def test_openrouter_preserves_task_when_guidance_is_large(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'AGENTS.md').write_text('guidance-' * 1_500, encoding='utf-8')
            bodies = []

            def fake_request(url, api_key, body, timeout):
                bodies.append(body)
                return {'choices': [{'message': {'role': 'assistant', 'content': 'done'}}]}

            task = 'TASK_MARKER: implement the requested version bump'
            with (patch.dict(agentic.os.environ, {'ROUTER_KEY': 'secret'}, clear=False),
                  patch('agentic_openrouter._openrouter_request', side_effect=fake_request)):
                ok, message = agentic.run_openrouter(
                    root, task, output=root / 'result.txt', model='openai/test',
                    api_key_env='ROUTER_KEY',
                )

        self.assertTrue(ok)
        self.assertEqual(message, 'done')
        self.assertEqual(bodies[0]['messages'][1]['content'], task)
        self.assertIn('REPOSITORY GUIDANCE', bodies[0]['messages'][2]['content'])

    def test_openrouter_history_compacts_old_tool_results(self):
        messages = [
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': 'prompt'},
            {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'write', 'type': 'function', 'function': {
                    'name': 'write_file', 'arguments': 'x' * 20_000,
                },
            }]},
            {'role': 'tool', 'tool_call_id': 'one', 'content': 'a' * 20_000},
            {'role': 'assistant', 'content': None, 'tool_calls': []},
            {'role': 'tool', 'tool_call_id': 'two', 'content': 'b' * 20_000},
        ]
        agentic_context._compact_openrouter_history(messages)
        self.assertLessEqual(
            sum(agentic_context._message_context_size(message) for message in messages),
            agentic_context.OPENROUTER_HISTORY_CHARS,
        )
        self.assertEqual(messages[2]['tool_calls'][0]['function']['arguments'], '{}')
        self.assertIn('omitted to control context size', messages[3]['content'])

    def test_openrouter_read_file_supports_ranges_and_bounds_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'notes.txt').write_text('one\ntwo\nthree\n')
            ranged = agentic_openrouter._openrouter_tool_call(
                root, 'read_file', {'path': 'notes.txt', 'start_line': 2, 'end_line': 2},
                readonly=True,
            )
            self.assertEqual(ranged, 'two\n')

            (root / 'large.txt').write_text(
                'x' * (agentic_openrouter.OPENROUTER_READ_FILE_CHARS + 100)
            )
            bounded = agentic_openrouter._openrouter_tool_call(
                root, 'read_file', {'path': 'large.txt'}, readonly=True,
            )
            self.assertLessEqual(len(bounded), agentic_openrouter.OPENROUTER_READ_FILE_CHARS)
            self.assertIn('use start_line/end_line', bounded)

    def test_openrouter_read_file_rejects_symlinks_and_special_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'regular.txt').write_text('safe\n', encoding='utf-8')
            try:
                (root / 'link.txt').symlink_to(root / 'regular.txt')
            except OSError as exc:
                self.skipTest(f'symlinks unavailable: {exc}')
            self.assertIn('symbolic links', agentic_openrouter._openrouter_tool_call(
                root, 'read_file', {'path': 'link.txt'}, readonly=True,
            ))
            if not hasattr(os, 'mkfifo'):
                self.skipTest('FIFO creation unavailable')
            fifo = root / 'pipe'
            try:
                os.mkfifo(fifo)
            except OSError as exc:
                self.skipTest(f'FIFO unavailable: {exc}')
            result = agentic_openrouter._openrouter_tool_call(
                root, 'read_file', {'path': 'pipe'}, readonly=True,
            )
        self.assertIn('regular files', result)

    @unittest.skipUnless(
        hasattr(os, 'mkfifo') and hasattr(os, 'O_NONBLOCK'),
        'FIFO and nonblocking open are unavailable',
    )
    def test_openrouter_write_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            os.mkfifo(root / 'pipe')
            source_root = Path(__file__).resolve().parents[1] / 'src'
            environment = os.environ.copy()
            environment['PYTHONPATH'] = os.pathsep.join(
                item for item in (str(source_root), environment.get('PYTHONPATH', ''))
                if item
            )
            probe = subprocess.run(
                [
                    sys.executable,
                    '-c',
                    (
                        'from pathlib import Path; '
                        'from agentic_runner.openrouter import _openrouter_tool_call; '
                        'import sys; '
                        "print(_openrouter_tool_call(Path(sys.argv[1]), 'write_file', "
                        "{'path': 'pipe', 'content': 'blocked'}, readonly=False))"
                    ),
                    str(root),
                ],
                capture_output=True,
                text=True,
                env=environment,
                timeout=2,
                check=False,
            )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn('could not open', probe.stdout)

    def test_openrouter_file_tools_bound_large_tree_and_file_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(350):
                path = root / "tree" / f"file-{index:03d}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"file {index}\n")
            listed = agentic_openrouter._openrouter_tool_call(
                root, 'list_files', {'path': 'tree'}, readonly=True,
            ).splitlines()
            self.assertEqual(len(listed), agentic_openrouter_tools.OPENROUTER_LIST_FILES_PAGE_SIZE + 1)
            self.assertEqual(listed[0], 'tree/file-000.txt')
            self.assertEqual(listed[agentic_openrouter_tools.OPENROUTER_LIST_FILES_PAGE_SIZE - 1], 'tree/file-299.txt')
            self.assertIn('call list_files with page=2', listed[-1])
            second_page = agentic_openrouter._openrouter_tool_call(
                root, 'list_files', {'path': 'tree', 'page': 2}, readonly=True,
            ).splitlines()
            self.assertEqual(second_page, [f'tree/file-{index:03d}.txt' for index in range(300, 350)])
            self.assertIn('ERROR: page must be a positive integer', agentic_openrouter._openrouter_tool_call(
                root, 'list_files', {'path': 'tree', 'page': 0}, readonly=True,
            ))
            self.assertIn(
                f"ERROR: page must be at most {agentic_openrouter_tools.OPENROUTER_LIST_FILES_MAX_PAGE}",
                agentic_openrouter._openrouter_tool_call(
                    root, 'list_files', {
                        'path': 'tree',
                        'page': agentic_openrouter_tools.OPENROUTER_LIST_FILES_MAX_PAGE + 1,
                    }, readonly=True,
                ),
            )

            large = root / 'large-no-newline.txt'
            large.write_text('z' * (20 * 1024 * 1024))
            bounded = agentic_openrouter._openrouter_tool_call(
                root, 'read_file', {'path': large.name}, readonly=True,
            )
            self.assertLessEqual(len(bounded), agentic_openrouter.OPENROUTER_READ_FILE_CHARS)
            self.assertIn('use start_line/end_line', bounded)
            ranged_bounded = agentic_openrouter._openrouter_tool_call(
                root, 'read_file', {
                    'path': large.name, 'start_line': 1, 'end_line': 1,
                }, readonly=True,
            )
            self.assertLessEqual(
                len(ranged_bounded), agentic_openrouter.OPENROUTER_READ_FILE_CHARS,
            )
            self.assertIn('use start_line/end_line', ranged_bounded)

    def test_openrouter_file_list_pages_reuse_bounded_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(350):
                path = root / "tree" / f"file-{index:03d}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("file\n")
            with patch.object(
                agentic_openrouter_tools,
                '_build_file_list_index',
                wraps=agentic_openrouter_tools._build_file_list_index,
            ) as build:
                first = agentic_openrouter_tools._list_files_page(root / 'tree', root, 1)
                second = agentic_openrouter_tools._list_files_page(root / 'tree', root, 2)
            self.assertEqual(len(first[0]), 300)
            self.assertEqual(len(second[0]), 50)
            build.assert_called_once()

    def test_openrouter_file_list_rejects_oversized_page_before_indexing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'tree').mkdir()
            with patch.object(
                agentic_openrouter_tools, '_list_files_page',
                side_effect=AssertionError('oversized page must not traverse'),
            ) as list_page:
                result = agentic_openrouter._openrouter_tool_call(
                    root, 'list_files', {
                        'path': 'tree',
                        'page': agentic_openrouter_tools.OPENROUTER_LIST_FILES_MAX_PAGE + 1,
                    }, readonly=True,
                )
            self.assertEqual(
                result,
                f"ERROR: page must be at most {agentic_openrouter_tools.OPENROUTER_LIST_FILES_MAX_PAGE}",
            )
            list_page.assert_not_called()

            schema = next(
                item for item in agentic_openrouter._openrouter_tools(True)
                if item['function']['name'] == 'list_files'
            )
            self.assertEqual(
                schema['function']['parameters']['properties']['page']['maximum'],
                agentic_openrouter_tools.OPENROUTER_LIST_FILES_MAX_PAGE,
            )

    def test_openrouter_ranged_reader_uses_bounded_chunks(self):
        class HugeLineReader:
            def __init__(self):
                self.remaining = 20 * 1024 * 1024
                self.read_sizes = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def __iter__(self):
                raise AssertionError('ranged reads must not iterate over whole lines')

            def read(self, size=-1):
                self.read_sizes.append(size)
                if self.remaining <= 0:
                    return ''
                count = min(size, 4096, self.remaining)
                self.remaining -= count
                return 'z' * count

        reader = HugeLineReader()
        with patch.object(agentic_openrouter_tools, '_open_regular_text_stream', return_value=reader):
            bounded = agentic_openrouter._openrouter_tool_call(
                Path('synthetic-worktree'), 'read_file', {
                    'path': 'synthetic-large-file', 'start_line': 1, 'end_line': 1,
                }, readonly=True,
            )
        self.assertLessEqual(len(bounded), agentic_openrouter.OPENROUTER_READ_FILE_CHARS)
        self.assertIn('use start_line/end_line', bounded)
        self.assertTrue(reader.read_sizes)
        self.assertLessEqual(
            max(reader.read_sizes), agentic_openrouter_tools.OPENROUTER_READ_CHUNK_CHARS,
        )

    def test_openrouter_distant_ranged_reader_bounds_scan_work(self):
        class DistantLineReader:
            def __init__(self):
                self.remaining = 2_000_000
                self.read_sizes = []
                self.total_read = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size=-1):
                self.read_sizes.append(size)
                if self.remaining <= 0:
                    return ""
                count = min(size, self.remaining)
                self.remaining -= count
                self.total_read += count
                return ("distant-line\n" * ((count + 12) // 13))[:count]

        reader = DistantLineReader()
        stats = {}
        with patch.object(agentic_openrouter_tools, "_open_regular_text_stream", return_value=reader):
            bounded = agentic_openrouter_tools._bounded_file_text(
                Path("synthetic-worktree"), "synthetic-large-file",
                start_line=100_000, end_line=100_001, scan_stats=stats,
            )
        self.assertIn("scan limit reached", bounded)
        self.assertTrue(stats["scan_limited"])
        self.assertLessEqual(
            stats["scanned_chars"], agentic_openrouter_tools.OPENROUTER_READ_SCAN_CHARS,
        )
        self.assertLessEqual(
            stats["scanned_lines"], agentic_openrouter_tools.OPENROUTER_READ_SCAN_LINES,
        )
        self.assertEqual(reader.total_read, stats["scanned_chars"])

    def test_openrouter_rejects_symlink_escape_and_invalid_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'worktree'
            outside = Path(temp) / 'outside.txt'
            root.mkdir()
            outside.write_text('private')
            link = root / 'outside-link'
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f'symlinks unavailable: {exc}')
            with self.assertRaisesRegex(ValueError, 'escapes'):
                agentic_openrouter._safe_worktree_path(root, 'outside-link')
            with self.assertRaisesRegex(ValueError, 'absolute'):
                agentic_openrouter._safe_worktree_path(root, str(outside))
            with self.assertRaisesRegex(ValueError, 'non-empty'):
                agentic_openrouter._safe_worktree_path(root, '')
            with self.assertRaisesRegex(ValueError, '\\.git'):
                agentic_openrouter._safe_worktree_path(root, '.git/config', write=True)

    def test_openrouter_write_rejects_symlinked_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'target.txt'
            target.write_text('original\n', encoding='utf-8')
            link = root / 'link.txt'
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f'symlinks unavailable: {exc}')
            result = agentic_openrouter._openrouter_tool_call(
                root, 'write_file', {'path': 'link.txt', 'content': 'changed\n'},
                readonly=False,
            )
            self.assertIn('symbolic links', result)
            self.assertEqual(target.read_text(encoding='utf-8'), 'original\n')

    def test_openrouter_rejects_hardlinked_read_and_write_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'worktree'
            root.mkdir()
            protected = Path(temp) / '.env'
            protected.write_text('PRIVATE=do-not-send\n', encoding='utf-8')
            writable = Path(temp) / 'external.txt'
            writable.write_text('external-original\n', encoding='utf-8')
            try:
                os.link(protected, root / 'visible.txt')
                os.link(writable, root / 'writable.txt')
            except OSError as exc:
                self.skipTest(f'hardlinks unavailable: {exc}')

            read_result = agentic_openrouter._openrouter_tool_call(
                root, 'read_file', {'path': 'visible.txt'}, readonly=True,
            )
            self.assertIn('hard-linked', read_result)
            self.assertNotIn('PRIVATE=do-not-send', read_result)

            write_result = agentic_openrouter._openrouter_tool_call(
                root, 'write_file', {
                    'path': 'writable.txt', 'content': 'must-not-truncate\n',
                }, readonly=False,
            )
            self.assertIn('hard-linked', write_result)
            self.assertEqual(writable.read_text(encoding='utf-8'), 'external-original\n')

    def test_openrouter_command_output_is_bounded_and_process_is_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / 'pid'
            script = root / 'emit-output.py'
            script.write_text(
                'import os\n'
                'import time\n'
                f'open({str(pid_file)!r}, "w").write(str(os.getpid()))\n'
                'os.write(1, b"A" * 7000 + b"TAIL")\n'
                'time.sleep(30)\n',
                encoding='utf-8',
            )
            result = agentic_openrouter._openrouter_tool_call(
                root, 'run_command', {'argv': [sys.executable, str(script)]},
                readonly=False, command_timeout=5,
            )
            for _ in range(100):
                if pid_file.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            pid = int(pid_file.read_text(encoding='utf-8'))
            self.assertIn('command output truncated; showing the tail', result)
            self.assertTrue(result.endswith('TAIL'))
            self.assertLessEqual(
                len(result.split('\n', 1)[1]),
                agentic_openrouter.OPENROUTER_COMMAND_OUTPUT_CHARS,
            )
            self.assert_process_stopped(pid)

    def test_openrouter_configured_check_completes_despite_noisy_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'emit-output.py'
            script.write_text(
                'import os\n'
                'os.write(1, b"A" * 7000 + b"TAIL")\n',
                encoding='utf-8',
            )
            argv = [sys.executable, str(script)]
            result = agentic_openrouter._openrouter_tool_call(
                root, 'run_command', {'argv': argv}, readonly=False,
                command_timeout=5, check_commands=[argv],
            )
            self.assertIn('exit 0', result)
            self.assertIn('command output truncated; showing the tail', result)
            self.assertTrue(result.endswith('TAIL'))

    def test_openrouter_tool_command_timeout_is_reported_and_process_is_stopped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / 'pid'
            script = root / 'sleep-command.py'
            script.write_text(
                'import os\n'
                'import time\n'
                f'open({str(pid_file)!r}, "w").write(str(os.getpid()))\n'
                'time.sleep(30)\n',
                encoding='utf-8',
            )
            result = agentic_openrouter._openrouter_tool_call(
                root, 'run_command', {'argv': [sys.executable, str(script)]},
                readonly=False, command_timeout=1,
            )
            pid = int(pid_file.read_text(encoding='utf-8'))
            self.assertIn('timed out after 1s', result)
            self.assert_process_stopped(pid)

    def test_openrouter_command_capture_failure_stops_and_reaps_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pid_file = root / 'pid'
            script = root / 'sleep-command.py'
            script.write_text(
                'import os\n'
                'import time\n'
                f'open({str(pid_file)!r}, "w").write(str(os.getpid()))\n'
                'time.sleep(30)\n',
                encoding='utf-8',
            )
            def fail_after_command_starts(*args, **kwargs):
                for _ in range(100):
                    if pid_file.exists():
                        break
                    time.sleep(0.01)
                raise RuntimeError('capture failed')

            with patch(
                'agentic_openrouter_tools._stop_command_process',
                wraps=agentic_openrouter_tools._stop_command_process,
            ) as stop:
                with patch(
                    'agentic_openrouter_tools.queue.Queue.get',
                    side_effect=fail_after_command_starts,
                ):
                    with self.assertRaisesRegex(RuntimeError, 'capture failed'):
                        agentic_openrouter._openrouter_tool_call(
                            root, 'run_command', {'argv': [sys.executable, str(script)]},
                            readonly=False, command_timeout=5,
                        )
            for _ in range(100):
                if pid_file.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            pid = int(pid_file.read_text(encoding='utf-8'))
            stop.assert_called_once()
            self.assert_process_stopped(pid)

