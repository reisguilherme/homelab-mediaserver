"""Generate user-facing configuration from the runtime registry."""

from .settings import FIELDS, PRODUCTION_REQUIRED, string_value


def catalog():
    return [
        {
            "key": "HOMESERVER_" + name.upper(),
            "type": field.kind,
            "default": "" if field.secret else field.default,
            "unit": field.unit,
            "secret": field.secret,
            "required_production": name in PRODUCTION_REQUIRED,
            "consumer": field.consumer,
            "application": field.application,
            "choices": field.choices,
        }
        for name, field in FIELDS.items()
        if field.editable
    ]


def example_env():
    import json

    lines = [
        "# HomeServer — preferências e credenciais da instalação pessoal.",
        "# Copie para .env, preencha as credenciais e ajuste suas preferências.",
        "# Caminhos, portas e URLs internas ficam definidos no código/Compose.",
        "# Nunca envie o .env preenchido para o Git.",
    ]
    groups = {}
    for name, field in FIELDS.items():
        if not field.editable:
            continue
        if name in ("prowlarr_indexers", "byparr_enabled"):
            group = "Indexadores e Cloudflare"
        elif field.secret or name in ("admin_username", "qbit_username"):
            group = "Credenciais — Jellyfin/Seerr podem ficar vazios até o primeiro setup"
        elif field.consumer == "qbittorrent" or name == "series_download_window":
            group = "Downloads, conexões e seeding"
        elif field.consumer == "worker":
            group = "Busca e troca de fontes lentas"
        elif field.consumer == "quality":
            group = "Qualidade e áudio — listas em ordem de preferência"
        elif field.consumer in ("subtitles", "bazarr"):
            group = "Legendas — pt-BR antes de en-US"
        elif field.consumer in ("metrics", "logging", "operations") or name.startswith("log_"):
            group = "Monitoramento e logs"
        else:
            group = "Preferências gerais"
        value = (
            ("[]" if field.kind == "json" else "")
            if field.secret
            else (
                json.dumps(field.default) if field.kind == "json" else string_value(field.default)
            )
        )
        groups.setdefault(group, []).append(
            "HOMESERVER_" + name.upper() + "=" + json.dumps(value, ensure_ascii=False)
        )
    for group, entries in groups.items():
        if not group.startswith("Credenciais"):
            lines.extend(["", "# " + group, *entries])
    for group, entries in groups.items():
        if group.startswith("Credenciais"):
            lines.extend(["", "# " + group, *entries])
    return "\n".join(lines) + "\n"
