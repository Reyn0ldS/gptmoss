"""Generic, configurable project-domain classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


@dataclass(frozen=True)
class DomainDefinition:
    name: str
    markers: tuple[str, ...]


DEFAULT_DOMAINS = (
    DomainDefinition("software-engineering", (
        "application", "logiciel", "programme", "software", "code", "api",
        "site web", "project", "projet",
    )),
    DomainDefinition("data-and-automation", (
        "donnée", "donnee", "data", "pipeline", "import", "export",
        "automatisation", "automation", "workflow", "intégration", "integration",
    )),
    DomainDefinition("intelligent-processing", (
        "ia", "ai", "modèle", "modele", "model", "apprentissage",
        "inférence", "inference", "classification", "reconstruction",
    )),
    DomainDefinition("media-and-spatial-processing", (
        "image", "photo", "audio", "vidéo", "video", "3d", "maillage",
        "mesh", "texture", "rendu", "render",
    )),
    DomainDefinition("user-experience", (
        "interface utilisateur", "user interface", "ui", "desktop", "frontend",
        "utilisateur", "accessibilité", "accessibility",
    )),
    DomainDefinition("security-and-privacy", (
        "sécurité", "securite", "security", "confidentialité", "privacy",
        "personnel", "rgpd", "gdpr", "authentification", "authorization",
    )),
    DomainDefinition("offline-and-operations", (
        "hors-ligne", "hors ligne", "offline", "portable", "autonome",
        "déploiement", "deploiement", "observabilité", "sauvegarde", "recovery",
    )),
    DomainDefinition("document-workflow", (
        "docx", "pptx", "corpus documentaire", "analyse documentaire",
        "document analysis", "rédaction professionnelle", "long-form document",
        "matrice de traçabilité", "evidence matrix",
    )),
)


class ProjectDomainRegistry:
    """Classify projects using generic defaults plus optional project definitions."""

    def __init__(self, definitions: Iterable[DomainDefinition] = DEFAULT_DOMAINS):
        self._definitions = {item.name: item for item in definitions}

    @property
    def definitions(self) -> tuple[DomainDefinition, ...]:
        return tuple(self._definitions.values())

    def register(self, name: str, markers: Iterable[str]) -> None:
        normalized = str(name).strip().lower()
        values = tuple(dict.fromkeys(str(item).strip().casefold() for item in markers if str(item).strip()))
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", normalized):
            raise ValueError("Domain name must contain 2-80 lowercase safe characters.")
        if not values:
            raise ValueError("A project domain requires at least one marker.")
        self._definitions[normalized] = DomainDefinition(normalized, values)

    def load(self, path: str | Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        domains: Mapping[str, object] = payload.get("domains", payload)
        if not isinstance(domains, dict):
            raise ValueError("Project domain configuration must be a JSON object.")
        for name, markers in domains.items():
            if not isinstance(markers, list):
                raise ValueError(f"Domain markers must be an array: {name}")
            self.register(str(name), markers)

    @staticmethod
    def _contains(text: str, marker: str) -> bool:
        return bool(re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", text, re.IGNORECASE))

    def classify(self, task: str) -> list[str]:
        text = str(task or "").casefold()
        return [
            definition.name
            for definition in self._definitions.values()
            if any(self._contains(text, marker.casefold()) for marker in definition.markers)
        ]


DEFAULT_DOMAIN_REGISTRY = ProjectDomainRegistry()
