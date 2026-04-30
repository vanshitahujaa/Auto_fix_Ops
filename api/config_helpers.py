"""
Project & System Config Helpers
================================
All engine components use these helpers instead of reading env vars directly.
Caches config with TTL to avoid hitting DB on every task.
"""

import time
import logging
from typing import Optional
from .database import SessionLocal
from .models import ProjectConfig, SystemConfig, SystemMode

logger = logging.getLogger("autofixops")

# ─── TTL Cache ───
_config_cache: dict = {}  # {project_id: (config_dict, timestamp)}
_system_cache: dict = {}  # {"global": (system_config_dict, timestamp)}
CONFIG_TTL_SECONDS = 30


def get_project_config(project_id: str) -> Optional[dict]:
    """
    Fetches project config from DB with TTL cache.
    Returns a dict, never the ORM object (safe across threads/processes).
    """
    now = time.time()
    cached = _config_cache.get(str(project_id))
    if cached and (now - cached[1]) < CONFIG_TTL_SECONDS:
        return cached[0]

    db = SessionLocal()
    try:
        config = db.query(ProjectConfig).filter(ProjectConfig.id == project_id).first()
        if not config:
            return None

        config_dict = {
            "id": str(config.id),
            "name": config.name,
            "github_repo": config.github_repo,
            "github_token": config.get_github_token(),
            "prometheus_url": config.prometheus_url,
            "target_namespace": config.target_namespace,
            "target_manifest_path": config.target_manifest_path,
            "shadow_mode": config.shadow_mode,
            "confidence_threshold": config.confidence_threshold,
            "allowed_chaos_namespaces": config.allowed_chaos_namespaces or [],
            "max_resource_scale_factor": config.max_resource_scale_factor or 2.0,
        }

        _config_cache[str(project_id)] = (config_dict, now)
        return config_dict
    except Exception as e:
        logger.error(f"[CONFIG] Failed to fetch project config: {e}")
        return cached[0] if cached else None
    finally:
        db.close()


def invalidate_config_cache(project_id: str):
    """Call after config POST to force re-fetch."""
    _config_cache.pop(str(project_id), None)


def get_default_project_id() -> Optional[str]:
    """
    Returns the first project config ID (used for single-tenant compatibility).
    Creates a default project if none exists.
    """
    db = SessionLocal()
    try:
        config = db.query(ProjectConfig).first()
        if config:
            return str(config.id)

        # Create default project
        import uuid
        default = ProjectConfig(
            id=uuid.uuid4(),
            name="Default Project",
        )
        db.add(default)
        db.commit()
        db.refresh(default)
        logger.info(f"[CONFIG] Created default project: {default.id}")
        return str(default.id)
    except Exception as e:
        logger.error(f"[CONFIG] Failed to get/create default project: {e}")
        return None
    finally:
        db.close()


# ─── System Mode (Kill Switch) ───

def get_system_mode() -> str:
    """
    Returns the current system mode. Checked at top of every Celery task.
    Returns 'ACTIVE' on any error (fail-open for task execution, not for safety).
    """
    now = time.time()
    cached = _system_cache.get("global")
    if cached and (now - cached[1]) < CONFIG_TTL_SECONDS:
        return cached[0]

    db = SessionLocal()
    try:
        sys_config = db.query(SystemConfig).filter(SystemConfig.id == "global").first()
        if not sys_config:
            # Create default ACTIVE config
            sys_config = SystemConfig(id="global", system_mode=SystemMode.ACTIVE)
            db.add(sys_config)
            db.commit()

        mode = sys_config.system_mode.value
        _system_cache["global"] = (mode, now)
        return mode
    except Exception as e:
        logger.error(f"[SYSTEM] Failed to fetch system mode: {e}")
        return cached[0] if cached else "ACTIVE"
    finally:
        db.close()


def set_system_mode(mode: str, reason: str = None):
    """Updates the global system mode. Invalidates cache."""
    import datetime
    db = SessionLocal()
    try:
        sys_config = db.query(SystemConfig).filter(SystemConfig.id == "global").first()
        if not sys_config:
            sys_config = SystemConfig(id="global")
            db.add(sys_config)

        sys_config.system_mode = SystemMode(mode)
        sys_config.disabled_reason = reason if mode == "DISABLED" else None
        sys_config.disabled_at = datetime.datetime.utcnow() if mode == "DISABLED" else None
        db.commit()

        _system_cache.pop("global", None)
        logger.info(f"[SYSTEM] Mode changed to {mode}" + (f" — reason: {reason}" if reason else ""))
        
        # Emit WebSocket event
        try:
            from .events import emit_sync
            emit_sync("system.mode_changed", {"mode": mode, "reason": reason})
        except Exception as ws_e:
            logger.error(f"[WS] Failed to emit system.mode_changed: {ws_e}")
            
    except Exception as e:
        logger.error(f"[SYSTEM] Failed to set system mode: {e}")
        db.rollback()
    finally:
        db.close()


def is_system_disabled() -> bool:
    """Quick check — returns True if system is DISABLED."""
    return get_system_mode() == "DISABLED"
