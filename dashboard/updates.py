"""Git-based updates from one fixed repository, reviewed and backed up."""
import asyncio
import json
import re
import secrets
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath
from dashboard.settings import stamp

REPOSITORY = "https://github.com/lucasdedango/ChamoxisBOT.git"


def safe_path(name):
    path = PurePosixPath(name)
    if not name or "\\" in name or ":" in name or any(ord(c) < 32 for c in name) or path.is_absolute() or any(p in {"", ".", ".."} for p in name.split("/")):
        return False
    if any(p.startswith(".") for p in path.parts):
        if name in {".gitignore", ".env.example"} or path.name == ".env.example" and path.parts[0] in {"manager", "modules"} and all(not p.startswith(".") for p in path.parts[:-1]):
            return True
        return name.startswith(".github/workflows/") and path.suffix in {".yml", ".yaml"} and ".." not in path.parts
    if path.parts[0] in {"manager", "modules", "shared", "chamoxis_common", "dashboard", "scripts", "tests", "docs"}:
        return path.suffix in {".py", ".md", ".html", ".css", ".js", ".ps1", ".bat"} or path.name == "requirements.txt" or path.name == ".env.example"
    return name in {"bot.py", "requirements.txt", "constraints.txt", "README.md", "AGENTS.md", ".gitignore",
                    "dashboard.bat", "start.bat", "dashboard-update-manifest.json", "config/services.example.json"}


class Updater:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.cache = self.root / "data/dashboard/update-cache.git"
        self.backups = self.root / "data/dashboard/update-backups"
        self.plans = {}
        self.remote_heads = {}
        self.lock = asyncio.Lock()

    def git(self, *args, cache=False, check=True):
        cwd = self.cache if cache else self.root
        try:
            result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=90,
                                    env=self.git_env())
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("Git indisponible ou délai réseau dépassé") from error
        if check and result.returncode:
            raise ValueError("Git a refusé l’opération; vérifie le réseau et l’état du dépôt")
        return result

    @staticmethod
    def git_env():
        import os
        return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(Path(__file__).resolve().parent / "no-hooks")}

    def text(self, *args, **kwargs):
        return self.git(*args, **kwargs).stdout.decode("utf-8", "replace").strip()

    def status(self):
        if not (self.root / ".git").exists():
            return {"available": False, "reason": "Installer depuis un clone Git pour utiliser les mises à jour"}
        branch, commit = self.text("branch", "--show-current"), self.text("rev-parse", "HEAD")
        remote = self.remote_heads.get(branch)
        return {"available": True, "branch": branch, "commit": commit,
                "remote_commit": remote, "update_available": remote != commit if remote else None,
                "dirty": bool(self.text("status", "--porcelain", "--untracked-files=no"))}

    def branches(self):
        output = self.text("ls-remote", "--heads", REPOSITORY)
        self.remote_heads = {line.split("\trefs/heads/", 1)[1]: line.split("\t", 1)[0]
                             for line in output.splitlines() if "\trefs/heads/" in line}
        return sorted(self.remote_heads)[:200]

    def tree(self, sha, cache=False):
        raw = self.git("ls-tree", "-r", "-z", sha, cache=cache).stdout
        entries = {}
        for entry in raw.split(b"\x00"):
            if not entry:
                continue
            meta, name = entry.split(b"\t", 1)
            mode, kind, blob = meta.decode().split()
            name = name.decode("utf-8")
            if mode not in {"100644", "100755"} or kind != "blob" or not safe_path(name):
                raise ValueError("La branche contient un chemin ou un type de fichier non autorisé : " + name[:160])
            entries[name] = blob
        return entries

    def prepare(self, branch):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,180}", branch) or self.git("check-ref-format", "refs/heads/" + branch, check=False).returncode:
            raise ValueError("Nom de branche invalide")
        state = self.status()
        if not state["available"] or state["dirty"]:
            raise ValueError("Le dépôt doit être un clone Git sans modifications locales suivies; sauvegarde/commit tes changements d’abord")
        self.cache.mkdir(parents=True, exist_ok=True)
        if not (self.cache / "HEAD").exists():
            self.git("init", "--bare", cache=True)
        self.git("fetch", "--no-tags", REPOSITORY, "refs/heads/" + branch, cache=True)
        target = self.text("rev-parse", "FETCH_HEAD", cache=True)
        entries = self.tree(target, cache=True)
        if not {"dashboard/main.py", "dashboard-update-manifest.json", "manager/main.py", "modules/plex/main.py"} <= entries.keys():
            raise ValueError("Cette branche ne contient pas le dashboard modulaire compatible. Aucun fichier du bot n’a été modifié")
        manifest = json.loads(self.git("show", target + ":dashboard-update-manifest.json", cache=True).stdout)
        names = manifest.get("files", [])
        if manifest.get("version") != 1 or not isinstance(names, list) or any(not isinstance(p, str) or p not in entries for p in names) or set(names) != set(entries):
            raise ValueError("Manifeste incomplet ou invalide")
        current = self.tree(state["commit"])
        changed = [{"path": p, "change": "ajout" if p not in current else "suppression" if p not in entries else "modification"}
                   for p in sorted(set(current) | set(entries)) if current.get(p) != entries.get(p)]
        # Git itself will preserve untracked files; detect collisions before displaying a plan.
        for name in entries.keys() - current.keys():
            if (self.root / name).exists():
                raise ValueError("Un fichier local non suivi occupe un chemin de la mise à jour : " + name)
        plan_id = secrets.token_urlsafe(24)
        self.plans = {k: v for k, v in self.plans.items() if v["expires"] > time.time()}
        plan = {"id": plan_id, "branch": branch, "commit": target, "base": state["commit"],
                "base_branch": state["branch"], "files": changed, "expires": time.time() + 900,
                "dependencies_changed": any(p["path"].endswith("requirements.txt") or p["path"] == "constraints.txt" for p in changed)}
        self.plans[plan_id] = plan
        return plan

    def apply(self, plan_id):
        plan = self.plans.get(plan_id)
        if not plan or plan["expires"] < time.time():
            raise ValueError("Aperçu expiré; préparer la mise à jour à nouveau")
        state = self.status()
        if state.get("dirty") or state.get("commit") != plan["base"] or state.get("branch") != plan["base_branch"]:
            raise ValueError("Le dépôt a changé depuis l’aperçu; préparer à nouveau")
        target = plan["commit"]
        # Import objects, then reject local commits that would otherwise be discarded by -B.
        self.git("fetch", "--no-tags", str(self.cache), target)
        local = self.git("rev-parse", "--verify", "refs/heads/" + plan["branch"], check=False)
        if local.returncode == 0 and self.git("merge-base", "--is-ancestor", local.stdout.decode().strip(), target, check=False).returncode:
            raise ValueError("Cette branche locale possède des commits non présents sur GitHub; ils ne seront pas écrasés")
        current = self.tree(state["commit"])
        self.backups.mkdir(parents=True, exist_ok=True)
        backup = self.backups / (stamp() + ".zip")
        with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("update.json", json.dumps(plan, ensure_ascii=False, indent=2))
            for name in current:
                path = self.root / name
                if path.is_file() and not path.is_symlink():
                    archive.write(path, "code/" + name)
            for name in ("manager/.env", "modules/plex/.env", "config/services.json"):
                path = self.root / name
                if path.is_file() and not path.is_symlink():
                    archive.write(path, "configuration/" + name)
        # No reset --hard, clean, shell command, hooks, dependency install or service restart.
        self.git("checkout", "--no-overwrite-ignore", "-B", plan["branch"], target)
        self.plans.pop(plan_id, None)
        return {"updated": True, "branch": plan["branch"], "commit": target,
                "backup": str(backup.relative_to(self.root)), "dependencies_changed": plan["dependencies_changed"],
                "message": "Redémarrer le dashboard et les deux services. Installer les dépendances si elles ont changé."}
