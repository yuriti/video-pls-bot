import asyncio
import os
import signal
import shutil
from pathlib import Path


class ProcessError(Exception):
    pass


async def watch_workspace(workspace: Path | None, max_disk_bytes: int):
    while True:
        if workspace:
            size = 0
            for path in workspace.iterdir():
                try:
                    if path.is_file():
                        size += path.stat().st_size
                except FileNotFoundError:
                    pass
            if size > max_disk_bytes:
                raise ProcessError("size_limit")
        await asyncio.sleep(0.1)


async def stop_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()
    # A killed extractor cannot run TemporaryDirectory.__exit__. Its parent
    # removes only jars belonging to that worker after the whole group exits.
    cookie_root = os.environ.get("COOKIE_TEMP_DIR")
    if cookie_root:
        for directory in Path(cookie_root).glob(f"jar-{process.pid}-*"):
            if directory.is_dir() and not directory.is_symlink():
                shutil.rmtree(directory, ignore_errors=True)


async def run_process(*args: str, timeout: float = 120, workspace: Path | None = None,
                      max_disk_bytes: int = 240_000_000) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    output = bytearray()

    async def consume():
        while chunk := await process.stdout.read(16384):
            output.extend(chunk)
            if len(output) > 1_000_000:
                raise ProcessError("output_limit")
        await process.wait()

    reader = asyncio.create_task(consume())
    watcher = asyncio.create_task(watch_workspace(workspace, max_disk_bytes))
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait((reader, watcher), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if process.returncode:
                raise ProcessError("process_failed")
            return bytes(output)
    finally:
        # Kill the whole process group, including ffmpeg spawned by yt-dlp, and
        # await exit before the queue releases capacity or removes the workspace.
        await stop_process_group(process)
        reader.cancel()
        watcher.cancel()
        await asyncio.gather(reader, watcher, return_exceptions=True)
