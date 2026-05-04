"""
WebSocket Event Bus for AutoFixOps
====================================
Manages WebSocket connections and broadcasts real-time events.
Uses Redis PubSub to bridge Celery background tasks to the FastAPI WebSocket clients.

Event types:
  - incident.created
  - incident.status_changed
  - incident.diagnosed
  - remediation.pr_created
  - remediation.verified
  - system.mode_changed
  - circuit_breaker.state_changed
"""

import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, Set
import redis

from fastapi import WebSocket

logger = logging.getLogger("autofixops")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CHANNEL_NAME = "autofixops_events"

class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)
        logger.info(f"[WS] Client connected. Total: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            self.active_connections.discard(websocket)
        logger.info(f"[WS] Client disconnected. Total: {len(self.active_connections)}")

    async def broadcast(self, message: str):
        """Sends raw message string to all connected clients."""
        if not self.active_connections:
            return

        dead: Set[WebSocket] = set()
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                dead.add(connection)

        if dead:
            async with self._lock:
                self.active_connections -= dead
            logger.info(f"[WS] Cleaned {len(dead)} dead connections.")

ws_manager = ConnectionManager()

# ─── Redis PubSub Loop ───

async def redis_listener(manager: ConnectionManager):
    """Background task to listen to Redis and broadcast to WebSockets."""
    import redis.asyncio as aioredis
    
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(CHANNEL_NAME)
    logger.info(f"[WS] Subscribed to Redis channel '{CHANNEL_NAME}'")

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = message["data"]
                await manager.broadcast(data)
    except asyncio.CancelledError:
        logger.info("[WS] Redis listener cancelled")
    except Exception as e:
        logger.error(f"[WS] Redis listener error: {e}")
    finally:
        await pubsub.unsubscribe(CHANNEL_NAME)
        await redis_client.close()

# ─── Sync Publisher (for Celery tasks) ───

_sync_redis_client = None

def get_sync_redis():
    global _sync_redis_client
    if _sync_redis_client is None:
        _sync_redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=0.5,
            socket_connect_timeout=0.5
        )
    return _sync_redis_client

def build_event(event_type: str, data: Dict[str, Any], incident_id: str = None) -> Dict[str, Any]:
    return {
        "type": event_type,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "incident_id": incident_id,
        "data": data,
    }

def emit_sync(event_type: str, data: Dict[str, Any], incident_id: str = None):
    """Publish an event to Redis (called from Celery tasks or API handlers)."""
    event = build_event(event_type, data, incident_id)
    payload = json.dumps(event, default=str)
    try:
        get_sync_redis().publish(CHANNEL_NAME, payload)
    except Exception as e:
        logger.error(f"[WS] Failed to publish sync event: {e}")
