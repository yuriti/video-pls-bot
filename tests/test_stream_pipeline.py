import asyncio
import tempfile
import unittest
from pathlib import Path

from tools.performance.stream_pipeline import PART, upload_growing


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_overlaps_production_defers_mutable_header_and_final_part(self):
        for tail in (0, 123):
            with self.subTest(tail=tail), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'video.mp4'
                parts, totals, active, peak = {}, {}, 0, 0
                early = asyncio.Event()
                async def produce():
                    with path.open('wb') as stream:
                        stream.write(b'\0' * PART)
                        for index in range(1, 12):
                            stream.write(bytes([index]) * PART)
                            stream.flush()
                            await asyncio.sleep(0.001)
                        stream.write(b't' * tail)
                        stream.flush()
                        await asyncio.wait_for(early.wait(), 1)
                        stream.seek(0)
                        stream.write(b'h' * PART)
                producer = asyncio.create_task(produce())
                async def send(index, total, data):
                    nonlocal active, peak
                    active += 1
                    peak = max(active, peak)
                    if not producer.done():
                        self.assertGreater(index, 0)
                        self.assertEqual(total, -1)
                        early.set()
                    await asyncio.sleep(0.002)
                    self.assertNotIn(index, parts)
                    parts[index], totals[index] = data, total
                    active -= 1
                    return True
                count = await upload_growing(path, producer, send, prefix=PART, window=3)
                self.assertEqual(b''.join(parts[i] for i in range(count)), path.read_bytes())
                self.assertEqual(totals[count-1], count)
                self.assertTrue(all(totals[i] == -1 for i in range(count-1)))
                self.assertLessEqual(peak, 3)

    async def test_failed_producer_never_finalizes_upload_and_cancels_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'video.mp4'
            path.write_bytes(b'x' * PART * 5)
            totals = []
            async def produce():
                await asyncio.sleep(0.01)
                raise OSError('mux_failed')
            async def send(index, total, data):
                totals.append(total)
                await asyncio.sleep(0.02)
                return True
            producer = asyncio.create_task(produce())
            with self.assertRaisesRegex(OSError, 'mux_failed'):
                await upload_growing(path, producer, send, prefix=PART, window=2)
            self.assertTrue(all(total == -1 for total in totals))
