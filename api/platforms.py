from fastapi import APIRouter

from core.config_store import config_store
from core.flags import FEATURE_GATED_PLATFORMS, flag_enabled
from core.registry import list_platforms

router = APIRouter(prefix="/platforms", tags=["platforms"])

_ALWAYS_HIDDEN = frozenset({"cursor", "tavily"})


@router.get("")
def get_platforms():
    platforms = list_platforms()
    cfg = config_store.get_all()
    result = []
    for item in platforms:
        name = str(item.get("name") or "").strip().lower()
        if name in _ALWAYS_HIDDEN:
            continue
        flag = FEATURE_GATED_PLATFORMS.get(name)
        if flag and not flag_enabled(flag, cfg):
            continue
        result.append(item)
    return result
