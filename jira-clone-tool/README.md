# jira-clone-tool

Clona una incidencia de un Jira origen (p. ej. `madrid-es.atlassian.net`) a un
Jira destino (p. ej. `jira.indra.es`), copiando resumen y descripción, y
rellenando el campo **Key Client** del destino con la key de la incidencia
origen.

## Instalación

```bash
cd jira-clone-tool
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edita .env con tus credenciales y URLs
```

## Uso

```bash
python clone_issue.py ECDMG-54
```

Opciones:

- `--target-project IAMNAM` — project key destino (por defecto, `DST_PROJECT` del `.env`, o `IAMNAM`).
- `--component ECDMG` — component a asignar en destino (por defecto, el project key de la incidencia origen).
- `--issue-type "Feature Request"` — issue type en destino (por defecto, el mismo que en origen).
- `--dry-run` — imprime el payload que se enviaría, sin crear nada.

## Configuración (`.env`)

Ver `.env.example`. Resumen:

| Variable | Descripción |
|---|---|
| `SRC_JIRA_URL`, `SRC_JIRA_EMAIL`, `SRC_JIRA_TOKEN` | Jira Cloud origen (auth básica con API token). |
| `DST_JIRA_URL`, `DST_JIRA_AUTH_TYPE`, `DST_JIRA_USER`, `DST_JIRA_TOKEN` | Jira destino. `DST_JIRA_AUTH_TYPE` es `bearer` (PAT) o `basic`. |
| `DST_PROJECT` | Project key destino por defecto. |
| `DST_JIRA_KEY_CLIENT_FIELD` | Id del custom field "Key Client" en destino (p. ej. `customfield_10500`). Si se deja vacío, se busca automáticamente por nombre. |

## Notas

- La descripción se copia tal cual la devuelve la API v2 del origen (texto/wiki
  markup). Si el destino usa Jira Cloud con formato ADF, habría que adaptar
  `create_target_issue` para convertir el texto a ese formato.
- El campo "Key Client" se localiza por nombre vía `/rest/api/2/field`; si hay
  varios campos con nombres parecidos, fija su id explícitamente con
  `DST_JIRA_KEY_CLIENT_FIELD`.
