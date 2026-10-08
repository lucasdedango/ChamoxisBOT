"""Historical storage operations extracted without changing their behavior."""
from modules.plex.config import *
MIN_FREE_BYTES = int(MIN_FREE_GB * 1024 * 1024 * 1024)

class NoStorageAvailableError(RuntimeError):
    pass


def estimate_required_bytes(torrent: dict, files: List[Path]) -> int:
    total = int(torrent.get("total_size") or 0)
    if total > 0:
        return total
    size = 0
    for f in files:
        try:
            size += f.stat().st_size
        except OSError:
            continue
    return size


def pick_storage_root(candidates: List[Path], required_bytes: int) -> Path:
    allowed: List[Tuple[Path, int]] = []
    checked: List[str] = []
    for root in candidates:
        try:
            free = shutil.disk_usage(root).free
        except Exception as e:
            checked.append(f"{root}=? ({e})")
            continue
        checked.append(f"{root}={free // (1024**3)}GiB")
        if free >= required_bytes + MIN_FREE_BYTES:
            allowed.append((root, free))
    if not allowed:
        raise NoStorageAvailableError(
            f"Aucun disque éligible (requis={required_bytes // (1024**3)}GiB, marge={MIN_FREE_GB:.1f}GiB). "
            f"Disques: {', '.join(checked)}"
        )
    if STORAGE_PICK_MODE == "random":
        return random.choice([p for p, _ in allowed])
    # weighted_random par défaut
    paths = [p for p, _ in allowed]
    weights = [max(free - required_bytes, 1) for _, free in allowed]
    return random.choices(paths, weights=weights, k=1)[0]
