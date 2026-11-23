import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tools.vpb import cli, common, server
from video_pls_bot.extract import download

COOKIE = {'domain': '.youtube.com', 'name': 'SAPISID', 'value': 'sensitive-fixture',
          'path': '/', 'secure': True, 'httpOnly': True, 'expires': -1}


class CookieTests(unittest.TestCase):
    def test_only_selected_platform_cookies_can_cross_the_wire(self):
        self.assertTrue(common.scoped_domain('.youtube.com', 'youtube'))
        self.assertTrue(common.scoped_domain('www.youtube.com', 'youtube'))
        for domain in ('youtube.com.evil.test', 'google.com', '.tiktok.com', None):
            self.assertFalse(common.scoped_domain(domain, 'youtube'))
            with self.assertRaises(common.AuthError):
                common.validate_cookies([{**COOKIE, 'domain': domain}], 'youtube')

    def test_cookie_headers_and_command_injection_are_rejected(self):
        for field, value in (('value', 'cookie\nInjected'), ('name', 'cookie\tname'), ('path', '/\r\n'),
                             ('domain', '..youtube.com'), ('expires', float('inf'))):
            with self.subTest(field=field), self.assertRaises(common.AuthError):
                common.netscape_bytes([{**COOKIE, field: value}], 'youtube')
        with self.assertRaises(common.AuthError):
            common.platform_spec(['youtube'])
        for url in ('http://youtube.com/watch?v=a', 'https://youtube.com.evil.test/a',
                    'https://youtube.com@127.0.0.1/a', 'https://youtube.com:123/a'):
            with self.assertRaises(common.AuthError):
                common.validate_url('youtube', url)

    def test_expired_and_partitioned_login_cookies_cannot_replace_credentials(self):
        for change in ({'expires': 100}, {'partitionKey': {'topLevelSite': 'https://google.com'}}, {'value': ''}):
            with self.assertRaisesRegex(common.AuthError, 'login_required'):
                common.validate_cookies([{**COOKIE, **change}], 'youtube', now=200)

    def test_netscape_format_preserves_session_and_http_only(self):
        output = common.netscape_bytes([COOKIE], 'youtube')
        self.assertIn(b'#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\t', output)
        self.assertNotIn(b'google.com', output)

    def test_output_never_prints_unexpected_error_text_or_cookie_fields(self):
        for json_output in (False, True):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cli.show({'ok': False, 'code': COOKIE['value'], 'cookies': [COOKIE], 'state': COOKIE['value']}, json_output)
            self.assertNotIn(COOKIE['value'], output.getvalue())


class ServerTests(unittest.TestCase):
    def test_failed_probe_leaves_encrypted_production_credential_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            existing = root / 'youtube-cookies.txt.cred'
            existing.write_bytes(b'existing-encrypted-credential')
            with patch.object(server, 'RUNTIME', root), patch.object(server, 'CONFIG', root), \
                 patch.object(server, 'ensure_memory_runtime'), \
                 patch.object(server, 'check_access', return_value={'ok': False, 'code': 'source_login_required'}), \
                 patch.object(server, 'encrypt_and_install') as install, patch.object(server, 'activate') as activate:
                result = server.handle({'version': 1, 'action': 'install', 'platform': 'youtube', 'cookies': [COOKIE]})
            self.assertFalse(result['ok'])
            install.assert_not_called()
            activate.assert_not_called()
            self.assertEqual(existing.read_bytes(), b'existing-encrypted-credential')

    def test_ciphertext_is_verified_and_previous_ciphertext_is_saved(self):
        plain = common.netscape_bytes([COOKIE], 'youtube')
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target = root / 'youtube-cookies.txt.cred'
            target.write_bytes(b'old-ciphertext')
            def run(*args, input=None, **fields):
                self.assertNotIn(COOKIE['value'], str(args))
                if args[1] == 'encrypt':
                    self.assertEqual(input, plain)
                    self.assertIn('--with-key=host', args)
                    Path(args[-1]).write_bytes(b'new-ciphertext')
                    return b''
                self.assertEqual(args[1], 'decrypt')
                return plain
            with patch.object(server, 'CONFIG', root), patch.object(server, 'run', side_effect=run):
                server.encrypt_and_install('youtube', plain)
            self.assertEqual(target.read_bytes(), b'new-ciphertext')
            self.assertEqual((root / 'youtube-cookies.txt.cred.previous').read_bytes(), b'old-ciphertext')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(root.glob('.*candidate*')))
            self.assertFalse(any(plain in p.read_bytes() for p in root.iterdir()))

    def test_failed_ciphertext_verification_does_not_replace_current(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            target = root / 'youtube-cookies.txt.cred'
            target.write_bytes(b'old')
            def run(*args, **fields):
                if args[1] == 'encrypt':
                    Path(args[-1]).write_bytes(b'cipher')
                    return b''
                return b'incorrect'
            with patch.object(server, 'CONFIG', root), patch.object(server, 'run', side_effect=run):
                with self.assertRaisesRegex(common.AuthError, 'encryption_failed'):
                    server.encrypt_and_install('youtube', b'plain')
            self.assertEqual(target.read_bytes(), b'old')

    def test_no_export_or_arbitrary_command_endpoint_exists(self):
        for action in ('export', 'shell', 'exec', 'decrypt', 'restart', '/bin/sh'):
            with self.subTest(action=action), self.assertRaisesRegex(common.AuthError, 'invalid_request'):
                server.handle({'version': 1, 'platform': 'youtube', 'action': action})
        with self.assertRaises(common.AuthError):
            server.handle({'version': 1, 'platform': 'youtube', 'action': 'check', 'cookies': [COOKIE]})

    def test_runtime_must_be_memory_backed(self):
        with patch.object(server, 'run', return_value=b'ext4\n'):
            with self.assertRaisesRegex(common.AuthError, 'runtime_not_memory'):
                server.ensure_memory_runtime()

    def test_extractor_cookie_jar_lives_in_runtime_and_is_always_removed(self):
        with tempfile.TemporaryDirectory() as root, patch('video_pls_bot.extract.YoutubeDL') as cls:
            root = Path(root)
            runtime, job = root / 'runtime', root / 'job'
            runtime.mkdir(mode=0o700)
            job.mkdir(mode=0o700)
            cookie = root / 'source.txt'
            cookie.write_bytes(common.netscape_bytes([COOKIE], 'youtube'))
            worker = cls.return_value.__enter__.return_value
            worker.extract_info.return_value = {'duration': 10}
            captured = []
            def save(*a, **k):
                jar = Path(cls.call_args.args[0]['cookiefile'])
                captured.append(jar)
                self.assertTrue(jar.is_relative_to(runtime))
                self.assertEqual(jar.stat().st_mode & 0o777, 0o600)
                (job / 'video.mp4').write_bytes(b'video')
                return {'title': 'Video'}
            worker.process_ie_result.side_effect = save
            with patch.dict(os.environ, {'COOKIE_TEMP_DIR': str(runtime)}):
                download('https://youtu.be/BaW_jenozKc', job, 'http://127.0.0.1:3128', 1000, 180,
                         '/usr/bin/node', cookies_file=cookie)
            self.assertTrue(captured)
            self.assertFalse(captured[0].exists())
            self.assertEqual(list(runtime.iterdir()), [])
            self.assertFalse((job / 'cookies.txt').exists())


class LocalTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_sends_cookies_in_stdin_not_arguments_and_pins_host_key(self):
        child = MagicMock()
        child.returncode = 0
        child.communicate = AsyncMock(return_value=(b'{"ok":true,"code":"installed"}', b''))
        with patch.object(cli.asyncio, 'create_subprocess_exec', AsyncMock(return_value=child)) as spawn:
            result = await cli.rpc({'ssh_config': '/config'}, 'install', 'youtube', cookies=[COOKIE])
        self.assertTrue(result['ok'])
        self.assertNotIn(COOKIE['value'], str(spawn.call_args))
        self.assertIn('StrictHostKeyChecking=yes', spawn.call_args.args)
        sent = json.loads(child.communicate.call_args.args[0])
        self.assertEqual(sent['cookies'][0]['value'], COOKIE['value'])
        self.assertEqual(spawn.call_args.args[-1], 'vpb-server')

    async def test_ssh_failure_does_not_leak_raw_output(self):
        child = MagicMock()
        child.returncode = 1
        child.communicate = AsyncMock(return_value=(COOKIE['value'].encode(), b''))
        with patch.object(cli.asyncio, 'create_subprocess_exec', AsyncMock(return_value=child)):
            with self.assertRaisesRegex(common.AuthError, '^ssh_failed$'):
                await cli.rpc({'ssh_config': '/config'}, 'check', 'youtube')

    async def test_killed_worker_cookie_jar_is_removed_without_touching_other_jars(self):
        import sys
        from video_pls_bot.process import run_process
        with tempfile.TemporaryDirectory() as root:
            runtime = Path(root)
            other = runtime / 'jar-unrelated'
            other.mkdir()
            (other / 'cookies.txt').write_text('unrelated')
            code = ('import os,pathlib,sys,time; '
                    'p=pathlib.Path(sys.argv[1])/f"jar-{os.getpid()}-test"; '
                    'p.mkdir(); (p/"cookies.txt").write_text("temporary"); time.sleep(20)')
            with patch.dict(os.environ, {'COOKIE_TEMP_DIR': str(runtime)}):
                with self.assertRaises(TimeoutError):
                    await run_process(sys.executable, '-c', code, str(runtime), timeout=0.3)
            self.assertEqual(list(runtime.iterdir()), [other])

    async def test_enter_waiter_is_cancellable_and_reports_browser_exit(self):
        import asyncio
        reader, writer = os.pipe()
        with os.fdopen(reader) as terminal:
            browser = MagicMock()
            proxy = MagicMock()
            browser.wait = AsyncMock(side_effect=lambda: None)
            async def pending():
                await asyncio.Event().wait()
            browser.wait = pending
            proxy.wait = pending
            try:
                with patch.object(cli.sys, 'stdin', terminal), contextlib.redirect_stdout(io.StringIO()):
                    task = asyncio.create_task(cli.wait_for_enter(browser, proxy))
                    await asyncio.sleep(0)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    task = asyncio.create_task(cli.wait_for_enter(browser, proxy))
                    await asyncio.sleep(0)
                    os.write(writer, b'\n')
                    await asyncio.wait_for(task, timeout=1)
                    browser.wait = AsyncMock(return_value=0)
                    with self.assertRaisesRegex(common.AuthError, 'browser_closed'):
                        await cli.wait_for_enter(browser, proxy)
            finally:
                os.close(writer)

    async def test_export_reads_only_the_finished_profile_without_visiting_login_pages(self):
        calls = []
        async def call(method, params=None, **fields):
            calls.append((method, params, fields))
            if method == 'Storage.getCookies':
                self.assertIsNone(params)
                return {'cookies': [COOKIE, {**COOKIE, 'domain': '.google.com'}]}
            if method == 'Browser.close':
                raise common.AuthError('browser_closed')
            raise AssertionError('Export must not navigate or automate a login page')
        cdp = MagicMock()
        cdp.call = call
        cookies = await cli.finish_browser(cdp, 'youtube')
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]['domain'], '.youtube.com')
        self.assertEqual([m for m,_,_ in calls], ['Storage.getCookies', 'Browser.close'])

    async def test_human_login_is_plain_chrome(self):
        browser = MagicMock()
        with patch.object(cli.asyncio, 'create_subprocess_exec', AsyncMock(return_value=browser)) as spawn:
            await cli.launch_login_browser(Path('/private/owned-vpb-profile'), 12345, 'https://www.youtube.com/')
        command = spawn.call_args.args
        self.assertEqual(command[0], str(cli.CHROME))
        self.assertIn('--user-data-dir=/private/owned-vpb-profile', command)
        self.assertIn('--proxy-server=http://127.0.0.1:12345', command)
        for arg in command:
            self.assertFalse(any(flag in arg for flag in ('remote-debugging', 'automation', 'headless', 'incognito')))

    async def test_login_closes_human_browser_before_starting_cookie_reader(self):
        native, exporter, proxy, cdp = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        cdp.call = AsyncMock(return_value={})
        cdp.close = AsyncMock()
        events = []
        profiles = []
        async def native_launch(profile, port, url):
            events.append('human-opened')
            profiles.append(profile)
            return native
        async def close(browser):
            self.assertIs(browser, native)
            events.append('human-closed')
        async def capture(profile, port, **fields):
            self.assertIn('human-closed', events)
            self.assertEqual(profile, profiles[0])
            self.assertTrue(fields['headless'])
            events.append('reader-opened')
            return exporter, cdp
        with tempfile.TemporaryDirectory() as root, \
             patch.object(cli, 'CHROME', Path(__file__)), \
             patch.object(cli, 'DEFAULT_CONFIG', Path(root) / 'config.json'), \
             patch.object(cli.sys, 'stdin', MagicMock()), \
             patch.object(cli, 'rpc', AsyncMock(side_effect=[{'ok': True, 'code': 'status'}, {'ok': True, 'code': 'installed'}])) as rpc, \
             patch.object(cli, 'tunnel', AsyncMock(return_value=(proxy, 12345))), \
             patch.object(cli, 'launch_login_browser', side_effect=native_launch), \
             patch.object(cli, 'wait_for_enter', AsyncMock()) as human_wait, \
             patch.object(cli, 'close_login_browser', side_effect=close), \
             patch.object(cli, 'launch_browser', side_effect=capture), \
             patch.object(cli, 'finish_browser', AsyncMock(return_value=[COOKIE])), \
             patch.object(cli, 'stop', AsyncMock()), contextlib.redirect_stdout(io.StringIO()):
            result = await cli.login({}, 'youtube', 'https://www.youtube.com/watch?v=pySJJbS-1oo')
        self.assertTrue(result['ok'])
        self.assertEqual(events, ['human-opened', 'human-closed', 'reader-opened'])
        human_wait.assert_awaited_once_with(native, proxy, allow_browser_exit=True)
        self.assertEqual(rpc.call_args.kwargs['cookies'], [COOKIE])

    async def test_manual_browser_close_still_requires_enter_before_cookie_transfer(self):
        import asyncio
        reader, writer = os.pipe()
        with os.fdopen(reader) as terminal:
            browser, proxy = MagicMock(), MagicMock()
            browser.wait = AsyncMock(return_value=0)
            async def pending():
                await asyncio.Event().wait()
            proxy.wait = pending
            try:
                with patch.object(cli.sys, 'stdin', terminal), contextlib.redirect_stdout(io.StringIO()):
                    task = asyncio.create_task(cli.wait_for_enter(browser, proxy, allow_browser_exit=True))
                    for _ in range(5):
                        await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    os.write(writer, b'\n')
                    await asyncio.wait_for(task, timeout=1)
            finally:
                os.close(writer)
