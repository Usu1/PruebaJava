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


def resolve_filter_jql(session: requests.Session, base_url: str, filter_name: str) -> str:
    candidates = []

    # Filtros propios del usuario del token: más fiable que /filter/search para
    # filtros privados o no marcados como favoritos.
    resp = session.get(f"{base_url}/rest/api/2/filter/my", params={"includeFavourites": "true"}, timeout=30)
    if resp.ok:
        candidates.extend(resp.json())

    # Respaldo: filtros compartidos con el usuario por otros, buscados por nombre (paginado).
    start_at = 0
    while True:
        resp = session.get(
            f"{base_url}/rest/api/2/filter/search",
            params={"filterName": filter_name, "startAt": start_at, "maxResults": 50},
            timeout=30,
        )
        ensure_ok(resp, f"Error buscando el filtro '{filter_name}' en {base_url}")
        data = resp.json()
        values = data.get("values", [])
        candidates.extend(values)
        start_at += len(values)
        if not values or start_at >= data.get("total", 0):
            break

    seen_ids = set()
    unique_candidates = []
    for f in candidates:
        if f["id"] not in seen_ids:
            seen_ids.add(f["id"])
            unique_candidates.append(f)

    matches = [f for f in unique_candidates if f["name"].strip().lower() == filter_name.strip().lower()]
    if not matches:
        names = ", ".join(f["name"] for f in unique_candidates) or "(ninguno)"
        raise JiraCloneError(
            f"No se encontró un filtro llamado '{filter_name}' en {base_url} visible para el usuario del token. "
            f"Filtros visibles para ese usuario: {names}."
        )
    if len(matches) > 1:
        raise JiraCloneError(
            f"Hay varios filtros llamados '{filter_name}' en {base_url}; usa --from-jql con el JQL exacto en su lugar."
        )
    return matches[0]["jql"]


def search_issue_keys(session: requests.Session, base_url: str, jql: str) -> list[str]:
    # Jira Cloud retiró GET /rest/api/2|3/search en favor de este endpoint,
    # paginado por cursor (nextPageToken) en vez de startAt/total.
    keys: list[str] = []
    next_page_token = None
    while True:
        body = {"jql": jql, "maxResults": 100, "fields": ["key"]}
        if next_page_token:
            body["nextPageToken"] = next_page_token
        resp = session.post(f"{base_url}/rest/api/3/search/jql", json=body, timeout=30)
        ensure_ok(resp, f"Error ejecutando la consulta JQL en {base_url}")
        data = resp.json()
        issues = data.get("issues", [])
        keys.extend(issue["key"] for issue in issues)
        next_page_token = data.get("nextPageToken")
        if not next_page_token or not issues:
            break
    return keys


def issue_exists_in_target(
    session: requests.Session, base_url: str, project_key: str, key_client_field_id: str, source_key: str
) -> bool:
    field_number = key_client_field_id.removeprefix("customfield_")
    jql = f'project = "{project_key}" AND cf[{field_number}] = "{source_key}"'
    resp = session.get(
        f"{base_url}/rest/api/2/search",
        params={"jql": jql, "maxResults": 1, "fields": "key"},
        timeout=30,
    )
    ensure_ok(resp, f"Error comprobando si '{source_key}' ya existe en {project_key} de {base_url}")
    return resp.json().get("total", 0) > 0


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
    parser.add_argument(
        "source_key", nargs="?", default=None, help="Key de una única incidencia origen, p. ej. ECDMG-54"
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--from-filter",
        default=None,
        help="Nombre de un filtro guardado en el Jira origen; se clona cada resultado que no exista ya en destino.",
    )
    source_group.add_argument(
        "--from-jql",
        default=None,
        help="JQL a ejecutar en el Jira origen; se clona cada resultado que no exista ya en destino.",
    )
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
    args = parser.parse_args()

    sources_given = sum(1 for v in (args.source_key, args.from_filter, args.from_jql) if v)
    if sources_given != 1:
        parser.error("indica exactamente uno de: source_key, --from-filter o --from-jql")

    return args


def clone_one_issue(
    src_session: requests.Session,
    src_url: str,
    dst_session: requests.Session,
    dst_url: str,
    args: argparse.Namespace,
    source_key: str,
    issue_type: str,
    key_client_field_id: str,
    fix_version: str | None,
    affected_version: str | None,
    assignee: str,
) -> str | None:
    source_issue = fetch_source_issue(src_session, src_url, source_key)

    fields = source_issue["fields"]
    summary = fields["summary"]
    prefix = f"[{source_key}]"
    if not summary.startswith(prefix):
        summary = f"{prefix} {summary}"
    description = fields.get("description") or ""
    source_project_key = fields["project"]["key"]
    component = args.component or source_project_key

    payload_fields = {
        "project": {"key": args.target_project},
        "summary": summary,
        "description": description,
        "issuetype": {"name": issue_type},
        "components": [{"name": component}],
        "assignee": {"name": assignee},
        key_client_field_id: source_key,
    }
    if fix_version:
        payload_fields["fixVersions"] = [{"name": fix_version}]
    if affected_version:
        payload_fields["versions"] = [{"name": affected_version}]

    payload = {"fields": payload_fields}

    if args.dry_run:
        import json

        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return None

    created = create_target_issue(dst_session, dst_url, payload)
    new_key = created["key"]
    print(f"Creada {new_key} en {dst_url}/browse/{new_key} (a partir de {source_key})")

    if not args.no_transition:
        transition_issue(dst_session, dst_url, new_key, args.transition_to)
        print(f"Estado de {new_key} cambiado a '{args.transition_to}'")

    return new_key


def main() -> int:
    load_dotenv()
    args = parse_args()

    try:
        src_session, src_url = build_source_session()
        dst_session, dst_url = build_target_session()

        key_client_field_id = discover_key_client_field_id(dst_session, dst_url)
        issue_type = resolve_issue_type(dst_session, dst_url, args.target_project, args.issue_type)
        assignee = get_current_username(dst_session, dst_url)
        fix_version = (
            resolve_version(dst_session, dst_url, args.target_project, args.fix_version, "Fix Version/s")
            if args.fix_version
            else None
        )
        affected_version = (
            resolve_version(dst_session, dst_url, args.target_project, args.affected_version, "Affects Version/s")
            if args.affected_version
            else None
        )

        batch_mode = not args.source_key
        if args.source_key:
            source_keys = [args.source_key]
        else:
            jql = resolve_filter_jql(src_session, src_url, args.from_filter) if args.from_filter else args.from_jql
            source_keys = search_issue_keys(src_session, src_url, jql)
            print(f"La consulta ha devuelto {len(source_keys)} incidencia(s) en el origen.")
            if not source_keys:
                return 0

        created_count = 0
        skipped_count = 0
        failed_count = 0

        for source_key in source_keys:
            try:
                if batch_mode and issue_exists_in_target(
                    dst_session, dst_url, args.target_project, key_client_field_id, source_key
                ):
                    print(f"{source_key}: ya existe en {args.target_project}, se omite.")
                    skipped_count += 1
                    continue

                new_key = clone_one_issue(
                    src_session,
                    src_url,
                    dst_session,
                    dst_url,
                    args,
                    source_key,
                    issue_type,
                    key_client_field_id,
                    fix_version,
                    affected_version,
                    assignee,
                )
                if new_key:
                    created_count += 1
            except JiraCloneError as exc:
                if not batch_mode:
                    raise
                failed_count += 1
                print(f"{source_key}: error - {exc}", file=sys.stderr)

        if batch_mode:
            print(f"Resumen: {created_count} creada(s), {skipped_count} ya existía(n), {failed_count} con error.")

        return 1 if failed_count else 0

    except JiraCloneError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"Error de red hablando con Jira: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
