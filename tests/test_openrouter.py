"""OpenRouter policy and tool-loop tests."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import tests  # noqa: F401  # Import bootstraps legacy test-module aliases.
from agentic_runner import cli as agentic
from agentic_runner import context as agentic_context
from agentic_runner import openrouter as agentic_openrouter
from agentic_runner.context import compact_openrouter_request
from agentic_runner import openrouter_tools as agentic_openrouter_tools


class OpenRouterTests(unittest.TestCase):
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
                    self.value = value
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def read(self):
                    return json.dumps(self.value).encode()

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
            self.assertEqual(names, ['list_files', 'read_file', 'run_command'])
            self.assertIn('src/main.go', agentic_openrouter._openrouter_tool_call(
                root, 'list_files', {'path': '.'}, readonly=True
            ))
            self.assertEqual(
                agentic_openrouter._openrouter_tool_call(root, 'read_file', {'path': 'src/main.go'}, readonly=True),
                'package main\n',
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

    def test_openrouter_command_policy_rejects_shell_network_and_review_mutations(self):
        for argv in ([], ['bash', '-c', 'echo unsafe'], ['curl', 'https://example.test'],
                     ['git', 'push'], ['git', 'checkout', 'main'],
                     ['git', '-C', '../other', 'status'],
                     ['git', '-c', 'core.sshCommand=ssh', 'status'],
                     ['python', '-c', 'print(1)'],
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
                ['git', 'log', '-p', '--', 'deleted-dir'],
                ['git', 'log', '--patch-with-stat'],
                ['git', 'log', '--patch-with-raw', 'HEAD'],
                ['git', 'log', '-pu'],
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
            def read(self):
                return self.body

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
        with patch.object(Path, 'open', return_value=reader):
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
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

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
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

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
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

