#!/usr/bin/env python3
"""Clona una incidencia de un Jira origen a un Jira destino.

Lee resumen y descripción de la incidencia origen (por ejemplo, en
madrid-es.atlassian.net) y crea una incidencia nueva en el Jira destino
(por ejemplo, jira.indra.es), copiando ese texto y rellenando el campo
"Key Client" del destino con la key de la incidencia origen.

Uso:
    python clone_issue.py ECDMG-54
    python clone_issue.py ECDMG-54 --target-project IAMNAM --component ECDMG
    python clone_issue.py ECDMG-54 --dry-run
"""
import argparse
import os
import sys

import requests
from dotenv import load_dotenv

KEY_CLIENT_FIELD_NAME = "Key Client"
DEFAULT_ISSUE_TYPE = "Feature Request"
DEFAULT_TRANSITION_TO = "Previous Study"
DEFAULT_VERSION = "Evolutivo_P3"


class JiraCloneError(Exception):
    pass


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise JiraCloneError(f"Falta la variable de entorno {name} (revisa tu .env)")
    return value


def ensure_ok(resp: requests.Response, context: str) -> None:
    if resp.ok:
        return
    body = resp.text.strip()
    if len(body) > 2000:
        body = body[:2000] + "... (truncado)"
    server_header = resp.headers.get("Server", "?")
    raise JiraCloneError(
        f"{context}: HTTP {resp.status_code} {resp.reason} (Server: {server_header})\n{body}"
    )


def build_source_session() -> tuple[requests.Session, str]:
    url = env("SRC_JIRA_URL", required=True).rstrip("/")
    email = env("SRC_JIRA_EMAIL", required=True)
    token = env("SRC_JIRA_TOKEN", required=True)
    session = requests.Session()
    session.auth = (email, token)
    session.headers["Accept"] = "application/json"
    user_agent = env("SRC_JIRA_USER_AGENT")
    if user_agent:
        session.headers["User-Agent"] = user_agent
    return session, url


def build_target_session() -> tuple[requests.Session, str]:
    url = env("DST_JIRA_URL", required=True).rstrip("/")
    token = env("DST_JIRA_TOKEN", required=True)
    auth_type = env("DST_JIRA_AUTH_TYPE", "bearer").lower()
    session = requests.Session()
    if auth_type == "bearer":
        session.headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "basic":
        user = env("DST_JIRA_USER", required=True)
        session.auth = (user, token)
    else:
        raise JiraCloneError(f"DST_JIRA_AUTH_TYPE desconocido: {auth_type!r} (usa 'bearer' o 'basic')")
    session.headers["Accept"] = "application/json"
    session.headers["Content-Type"] = "application/json"
    # Requerido por Jira Server/Data Center para peticiones de escritura (POST/PUT/DELETE)
    # que no vienen de una sesión de navegador; si no, responde 403 "XSRF check failed".
    session.headers["X-Atlassian-Token"] = "no-check"
    # Algunos proxys/WAF delante de Jira validan Origin/Referer en peticiones de
    # escritura y devuelven el mismo "XSRF check failed" si no los ven.
    session.headers["Origin"] = url
    session.headers["Referer"] = f"{url}/"
    user_agent = env("DST_JIRA_USER_AGENT")
    if user_agent:
        session.headers["User-Agent"] = user_agent

    ca_bundle = env("DST_JIRA_CA_BUNDLE")
    if ca_bundle:
        if ca_bundle.strip().lower() in ("false", "0", "no"):
            session.verify = False
            requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
            print(
                "Aviso: verificación TLS desactivada (DST_JIRA_CA_BUNDLE=false). "
                "El token de autenticación viaja sin comprobar el certificado del servidor.",
                file=sys.stderr,
            )
        else:
            session.verify = ca_bundle

    return session, url


def fetch_source_issue(session: requests.Session, base_url: str, key: str) -> dict:
    resp = session.get(
        f"{base_url}/rest/api/2/issue/{key}",
        params={"fields": "summary,description,issuetype,project"},
        timeout=30,
    )
    if resp.status_code == 404:
        raise JiraCloneError(f"No se encontró la incidencia {key} en {base_url}")
    ensure_ok(resp, f"Error leyendo {key} de {base_url}")
    return resp.json()


def get_current_username(session: requests.Session, base_url: str) -> str:
    resp = session.get(f"{base_url}/rest/api/2/myself", timeout=30)
    ensure_ok(resp, f"Error consultando el usuario del token en {base_url}")
    data = resp.json()
    return data.get("name") or data["key"]


def discover_key_client_field_id(session: requests.Session, base_url: str) -> str:
    override = env("DST_JIRA_KEY_CLIENT_FIELD")
    if override:
        return override
    resp = session.get(f"{base_url}/rest/api/2/field", timeout=30)
    ensure_ok(resp, f"Error listando campos de {base_url}")
    for field in resp.json():
        if field.get("name", "").strip().lower() == KEY_CLIENT_FIELD_NAME.lower():
            return field["id"]
    raise JiraCloneError(
        f"No se encontró un campo llamado '{KEY_CLIENT_FIELD_NAME}' en {base_url}. "
        "Indica su id explícitamente con DST_JIRA_KEY_CLIENT_FIELD (p. ej. customfield_10500)."
    )


def resolve_issue_type(session: requests.Session, base_url: str, project_key: str, desired_name: str) -> str:
    resp = session.get(
        f"{base_url}/rest/api/2/issue/createmeta/{project_key}/issuetypes",
        timeout=30,
    )
    if resp.status_code == 404:
        raise JiraCloneError(
            f"El proyecto '{project_key}' no existe en {base_url} o el usuario no tiene permiso para crear incidencias en él."
        )
    ensure_ok(resp, f"Error consultando tipos de incidencia de {project_key} en {base_url}")
    available = [it["name"] for it in resp.json().get("values", [])]
    for name in available:
        if name.strip().lower() == desired_name.strip().lower():
            return name
    raise JiraCloneError(
        f"El tipo de incidencia '{desired_name}' no es válido en el proyecto '{project_key}'. "
        f"Tipos disponibles: {', '.join(available) or '(ninguno)'}. Usa --issue-type para indicar uno."
    )


def resolve_version(session: requests.Session, base_url: str, project_key: str, desired_name: str, field_label: str) -> str:
    resp = session.get(f"{base_url}/rest/api/2/project/{project_key}/versions", timeout=30)
    ensure_ok(resp, f"Error consultando versiones de {project_key} en {base_url}")
    available = [v["name"] for v in resp.json()]
    for name in available:
        if name.strip().lower() == desired_name.strip().lower():
            return name
    raise JiraCloneError(
        f"La versión '{desired_name}' no existe en el proyecto '{project_key}' (campo {field_label}). "
        f"Versiones disponibles: {', '.join(available) or '(ninguna)'}."
    )


def create_target_issue(session: requests.Session, base_url: str, payload: dict) -> dict:
    resp = session.post(f"{base_url}/rest/api/2/issue", json=payload, timeout=30)
    ensure_ok(resp, f"Error creando la incidencia en {base_url}")
    return resp.json()


def transition_issue(session: requests.Session, base_url: str, issue_key: str, target_status: str) -> None:
    resp = session.get(f"{base_url}/rest/api/2/issue/{issue_key}/transitions", timeout=30)
    ensure_ok(resp, f"Error consultando transiciones de {issue_key} en {base_url}")
    transitions = resp.json().get("transitions", [])
    match = next(
        (
            t
            for t in transitions
            if t["name"].strip().lower() == target_status.strip().lower()
            or t["to"]["name"].strip().lower() == target_status.strip().lower()
        ),
        None,
    )
    if match is None:
        available = ", ".join(t["name"] for t in transitions) or "(ninguna)"
        raise JiraCloneError(
            f"No hay una transición a '{target_status}' disponible para {issue_key} desde su estado actual. "
            f"Transiciones disponibles: {available}."
        )
    resp = session.post(
        f"{base_url}/rest/api/2/issue/{issue_key}/transitions",
        json={"transition": {"id": match["id"]}},
        timeout=30,
    )
    ensure_ok(resp, f"Error aplicando la transición '{target_status}' a {issue_key} en {base_url}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source_key", help="Key de la incidencia origen, p. ej. ECDMG-54")
    parser.add_argument(
        "--target-project",
        default=os.environ.get("DST_PROJECT", "IAMNAM"),
        help="Project key del Jira destino donde crear la incidencia (por defecto: %(default)s)",
    )
    parser.add_argument(
        "--component",
        default=None,
        help="Component del destino. Por defecto, el project key de la incidencia origen (p. ej. ECDMG).",
    )
    parser.add_argument(
        "--issue-type",
        default=DEFAULT_ISSUE_TYPE,
        help="Issue type en el destino (por defecto: %(default)r, independientemente del tipo en origen).",
    )
    parser.add_argument(
        "--fix-version",
        default=DEFAULT_VERSION,
        help="Fix Version/s en el destino (por defecto: %(default)r). Vacío ('') para no rellenarlo.",
    )
    parser.add_argument(
        "--affected-version",
        default=DEFAULT_VERSION,
        help="Affects Version/s en el destino (por defecto: %(default)r). Vacío ('') para no rellenarlo.",
    )
    parser.add_argument(
        "--transition-to",
        default=DEFAULT_TRANSITION_TO,
        help="Estado al que pasar la incidencia tras crearla (por defecto: %(default)r).",
    )
    parser.add_argument(
        "--no-transition",
        action="store_true",
        help="No cambiar el estado de la incidencia tras crearla.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra el payload que se enviaría al Jira destino sin crear nada.",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    try:
        src_session, src_url = build_source_session()
        source_issue = fetch_source_issue(src_session, src_url, args.source_key)

        fields = source_issue["fields"]
        summary = fields["summary"]
        prefix = f"[{args.source_key}]"
        if not summary.startswith(prefix):
            summary = f"{prefix} {summary}"
        description = fields.get("description") or ""
        source_project_key = fields["project"]["key"]
        component = args.component or source_project_key

        dst_session, dst_url = build_target_session()
        key_client_field_id = discover_key_client_field_id(dst_session, dst_url)
        issue_type = resolve_issue_type(dst_session, dst_url, args.target_project, args.issue_type)
        assignee = get_current_username(dst_session, dst_url)

        payload_fields = {
            "project": {"key": args.target_project},
            "summary": summary,
            "description": description,
            "issuetype": {"name": issue_type},
            "components": [{"name": component}],
            "assignee": {"name": assignee},
            key_client_field_id: args.source_key,
        }

        if args.fix_version:
            fix_version = resolve_version(dst_session, dst_url, args.target_project, args.fix_version, "Fix Version/s")
            payload_fields["fixVersions"] = [{"name": fix_version}]

        if args.affected_version:
            affected_version = resolve_version(
                dst_session, dst_url, args.target_project, args.affected_version, "Affects Version/s"
            )
            payload_fields["versions"] = [{"name": affected_version}]

        payload = {"fields": payload_fields}

        if args.dry_run:
            import json

            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0

        created = create_target_issue(dst_session, dst_url, payload)
        new_key = created["key"]
        print(f"Creada {new_key} en {dst_url}/browse/{new_key} (a partir de {args.source_key})")

        if not args.no_transition:
            transition_issue(dst_session, dst_url, new_key, args.transition_to)
            print(f"Estado de {new_key} cambiado a '{args.transition_to}'")

        return 0

    except JiraCloneError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"Error de red hablando con Jira: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
