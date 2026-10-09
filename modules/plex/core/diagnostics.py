"""Small, non-sensitive snapshots for conversational download support."""


def snapshot(task, torrent):
    result = task.get("result") or {}
    data = {"id": task["id"], "title": task["payload"].get("title", ""),
            "task_state": task["state"], "torrent_available": bool(torrent)}
    # Never expose links, tokens, local paths, trackers, peers or another user's task.
    for field in ("state", "progress", "dlspeed", "eta", "num_seeds", "num_leechs", "availability"):
        value = (torrent or {}).get(field)
        if value is not None:
            data[field] = value
    if task["state"] == "simulated":
        reason = "Mode test : aucun téléchargement lancé."
    elif task["state"] == "needs_review":
        reason = "Opération incertaine : vérification manuelle nécessaire."
    elif task["state"] in {"completed", "cancelled", "failed", "duplicate", "queued", "adding", "importing"}:
        reason = "État de la demande : " + task["state"] + "."
    elif not torrent:
        reason = "Torrent introuvable dans qBittorrent au moment de la consultation."
    elif torrent.get("state") in {"pausedDL", "stoppedDL"}:
        reason = "Téléchargement en pause/arrêté."
    elif torrent.get("state") in {"error", "missingFiles"}:
        reason = "qBittorrent signale une erreur ou des fichiers manquants ; consulte ses détails."
    elif torrent.get("progress", 0) >= 1:
        reason = "Le téléchargement est terminé ; le module Plex doit encore finaliser son suivi/import."
    elif torrent.get("state") in {"checkingDL", "checkingResumeData", "checkingUP"}:
        reason = "qBittorrent vérifie les fichiers du torrent."
    elif torrent.get("state") == "queuedDL":
        reason = "qBittorrent garde ce téléchargement dans sa file d’attente."
    elif torrent.get("state") == "metaDL":
        reason = "En attente des métadonnées du torrent."
    elif torrent.get("dlspeed", 0) == 0 and torrent.get("num_seeds") == 0:
        reason = "Débit nul et aucun seed connecté ; cela peut expliquer l’attente."
    elif torrent.get("dlspeed", 0) == 0:
        reason = "Débit nul actuellement ; cette seule mesure ne suffit pas à déterminer la cause."
    else:
        reason = "Le téléchargement transfère actuellement des données."
    data["observation"] = reason
    return data
