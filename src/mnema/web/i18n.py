from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "tagline": "Your cloud, remembered.",
        "warning": "Automatic deletion is disabled by default.",
        "setup": "Setup",
        "dashboard": "Dashboard",
        "save": "Save safely",
    },
    "es": {
        "tagline": "Tu nube, recordada.",
        "warning": "La eliminación automática está desactivada de forma predeterminada.",
        "setup": "Configuración",
        "dashboard": "Panel",
        "save": "Guardar de forma segura",
    },
}


def strings(language: str) -> dict[str, str]:
    return STRINGS.get(language, STRINGS["en"])
