import asyncio
import time
import json
import logging
from typing import AsyncGenerator, Any
from fastapi import Request

from app.config import config

logger = logging.getLogger("freelance")
from app.core.observability import log_event

# Known model families that can't be identified by size suffix alone
_KNOWN_MODEL_FAMILIES = {
    "huge": ["deepseek-v3", "deepseek-r1:671b"],
    "large": ["deepseek-r1", "qwen2.5:72b", "qwen2.5:32b", "llama3:70b", "llama3.1:70b", "llama3.2:70b"],
    "medium": ["qwen2.5", "llama3:8b", "llama3.1:8b", "llama3.2:8b", "gemma2"],
}

class StreamerService:
    @staticmethod
    def get_model_size_level(model_id: str) -> str:
        """Determine model size level from model ID."""
        model_id_lower = model_id.lower()
        
        # 1. Check known model families first
        for size_level, patterns in _KNOWN_MODEL_FAMILIES.items():
            for pattern in patterns:
                if pattern in model_id_lower:
                    return size_level
        
        # 2. Pattern-based detection by parameter count suffix
        if any(x in model_id_lower for x in ["500b", "600b", "671b", "675b", "700b", "800b", "900b", "1t", "1000b"]):
            return "huge"
        if any(x in model_id_lower for x in ["100b", "128b", "175b", "200b", "300b", "400b", "405b", "5x"]):
            return "xlarge"
        if any(x in model_id_lower for x in ["70b", "65b", "52b", "40b", "34b", "32b", "28b", "20b", "18b", "17b", "16b", "14b", "13b", "12b", "11b", "large", "3x", "4x"]):
            return "large"
        if any(x in model_id_lower for x in ["7b", "8b", "9b", "10b"]):
            return "medium"
        return "small"

    @staticmethod
    def get_probe_timeout(model_id: str) -> int:
        """Determine first-token probe timeout based on model size."""
        size_level = StreamerService.get_model_size_level(model_id)
        if size_level == "huge":
            return config.settings.stream_probe.defunct_model_timeout
        elif size_level == "xlarge":
            return config.settings.stream_probe.xlarge_model_timeout
        elif size_level == "large":
            return config.settings.stream_probe.large_model_timeout
        else:
            return config.settings.stream_probe.small_model_timeout

    @staticmethod
    def get_read_timeout(model_id: str) -> int:
        """Determine read timeout between chunks based on model size."""
        size_level = StreamerService.get_model_size_level(model_id)
        if size_level == "huge":
            return config.settings.stream_probe.huge_model_read_timeout
        else:
            return config.settings.stream_probe.read_timeout

    @staticmethod
    async def stream_with_heartbeat(
        gen: AsyncGenerator,
        p_name: str,
        model_id: str,
        request: Request,
        initial_chunks: int = 0,
        initial_bytes: int = 0,
    ):
        """
        Wraps provider stream with heartbeat functionality and read timeout handling.
        """
        start_time = time.time()
        chunks_sent = initial_chunks
        total_bytes = initial_bytes
        last_activity_time = time.time()
        ping_interval = config.settings.stream_probe.ping_interval
        read_timeout = StreamerService.get_read_timeout(model_id)
        
        message_queue = asyncio.Queue()
        stop_event = asyncio.Event()
        timeout_event = asyncio.Event() 
        
        async def heartbeat_task():
            nonlocal last_activity_time
            try:
                while not stop_event.is_set():
                    if await request.is_disconnected():
                        log_event(logger, "stream_disconnected", provider=p_name, model=model_id)
                        logger.warning(f"[CLIENT_DISCONNECT] Client disconnected for {model_id}")
                        break
                    current_time = time.time()
                    time_since_activity = current_time - last_activity_time
                    if time_since_activity >= read_timeout:
                        log_event(logger, "stream_timeout", logging.WARNING, provider=p_name, model=model_id)
                        logger.warning(f"[TIMEOUT] Read timeout detected for {model_id} (inactivity: {time_since_activity:.1f}s)")
                        timeout_event.set()
                        break
                    if time_since_activity >= ping_interval:
                        await message_queue.put(("heartbeat", ": ping\n\n"))
                        last_activity_time = current_time
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[HEARTBEAT] Error: {e}")
        
        async def business_data_task():
            from app.services.stats_service import stats_service
            nonlocal last_activity_time
            nonlocal chunks_sent
            nonlocal total_bytes
            try:
                async for chunk in gen:
                    if timeout_event.is_set() or await request.is_disconnected():
                        logger.info(f"[STREAM] Stopping business task - timeout={timeout_event.is_set()}, disconnected={await request.is_disconnected()}")
                        break
                    last_activity_time = time.time()
                    chunk_len = len(chunk) if chunk else 0
                    total_bytes += chunk_len
                    if chunks_sent == 0:
                        ttft = (time.time() - start_time) * 1000
                        logger.info(f"[STREAM] First chunk for {model_id} at +{ttft:.2f}ms (len={chunk_len})")
                        # RECORD TTFT and success start
                        stats_service.record_request(p_name, model_id, ttft_ms=ttft)
                    elif chunk and chunk.strip() == "data: [DONE]":
                        logger.info(f"[STREAM] Received end signal for {model_id} after {chunks_sent + 1} chunks")
                    await message_queue.put(("business", chunk))
                    chunks_sent += 1
                logger.info(f"[STREAM] Business task completed, total chunks: {chunks_sent}")
            except Exception as e:
                if not isinstance(e, asyncio.CancelledError):
                    log_event(logger, "stream_failed", logging.ERROR, provider=p_name,
                              model=model_id, error_type=type(e).__name__)
                    logger.error(f"Stream interrupted: {e}")
                    await message_queue.put(("business", f"data: {json.dumps({'error': {'message': str(e)}})}\n\n"))
            finally:
                await message_queue.put(("end", None))
        
        h_task = asyncio.create_task(heartbeat_task())
        b_task = asyncio.create_task(business_data_task())
        
        try:
            while True:
                # Check for exit conditions before waiting
                if timeout_event.is_set():
                    logger.warning(f"[STREAM] Timeout event set, exiting loop")
                    break
                if await request.is_disconnected():
                    log_event(logger, "stream_disconnected", provider=p_name, model=model_id)
                    logger.warning(f"[STREAM] Client disconnected, exiting loop")
                    break
                
                # Use timeout to allow periodic checks
                try:
                    msg_type, data = await asyncio.wait_for(message_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                    
                if msg_type == "end":
                    logger.info(f"[STREAM] Business data stream ended for {model_id}")
                    break
                yield data
                message_queue.task_done()
        finally:
            stop_event.set()
            h_task.cancel()
            b_task.cancel()
            log_event(logger, "stream_relay_finished", provider=p_name, model=model_id,
                      chunks=chunks_sent, characters=total_bytes,
                      elapsed_ms=round((time.time() - start_time) * 1000, 3))
            logger.info(f"--- [STREAM FINISHED] ({chunks_sent} chunks, {total_bytes} bytes relayed) ---")
