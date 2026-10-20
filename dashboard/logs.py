"""Clear only known application logs, preserving data and open log file handles."""
from pathlib import Path


def log_targets(root, settings):
    root = Path(root).resolve()
    manager, plex = settings.values('manager'), settings.values('plex')
    bases = [root / (manager.get('MANAGER_DATA_DIR') or 'data/manager') / 'manager.log',
             root / (plex.get('PLEX_DATA_DIR') or 'data/plex') / 'plex.log',
             root / 'bot.log', root / (plex.get('BOT_LOG_FILE') or 'bot.log')]
    targets = set()
    for base in bases:
        if base.suffix.lower() != '.log' and base.name != 'logs.txt':
            continue
        for candidate in [base, *(Path(str(base) + '.' + str(i)) for i in range(1, 4))]:
            # Check every component before resolving: a symlink inside the repo is not a log target.
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            if '..' in relative.parts:
                continue
            current = root
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise ValueError('Un chemin de log contient un lien symbolique; suppression refusée')
            if candidate.exists():
                if not candidate.is_file():
                    raise ValueError('Un chemin de log ne désigne pas un fichier')
                targets.add(candidate)
    return sorted(targets)


def clear_logs(root, settings):
    targets = log_targets(root, settings)
    cleared = []
    for path in targets:
        # Truncation works while the services still have append-mode file handles open (Windows too).
        with path.open('wb'):
            pass
        cleared.append(path.relative_to(root).as_posix())
    return {'cleared': cleared, 'count': len(cleared)}
