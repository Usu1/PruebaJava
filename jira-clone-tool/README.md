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
| `DST_JIRA_CA_BUNDLE` | Solo si el destino usa certificado autofirmado/CA interna (`SSLCertVerificationError`). Ruta a un `.pem` con la CA, o `false` para desactivar la verificación (inseguro). |
| `SRC_JIRA_USER_AGENT`, `DST_JIRA_USER_AGENT` | Solo si un proxy/WAF corporativo devuelve `403` al ver el `User-Agent` por defecto de `requests`. |

## Certificado autofirmado / CA interna en el destino

Si al ejecutar el script ves `SSLCertVerificationError: self-signed certificate in
certificate chain`, es que `jira.indra.es` (u otro destino corporativo) usa un
certificado de una CA interna que Windows ya conoce (por eso el navegador no
se queja) pero que Python no tiene en su propio almacén de confianza
(`certifi`). Dos formas de arreglarlo, de más a menos cómoda:

1. **Reutilizar el almacén de certificados de Windows** (recomendado):
   ```
   .venv\Scripts\python.exe -m pip install pip-system-certs
   ```
   Con este paquete instalado en el venv, `requests` usa automáticamente los
   certificados que Windows ya tiene instalados (incluida la CA interna), sin
   tocar nada más.

2. **Indicar la CA manualmente**: exporta el certificado raíz interno (tu
   equipo de IT lo tiene, o lo sacas del almacén de certificados de Windows) a
   un `.pem` y ponlo en `.env`:
   ```
   DST_JIRA_CA_BUNDLE=C:\certs\indra-ca.pem
   ```

Como último recurso (solo para pruebas puntuales, nunca en uso normal, porque
el token de autenticación viajaría sin verificar el certificado del
servidor): `DST_JIRA_CA_BUNDLE=false`.

## Notas

- La descripción se copia tal cual la devuelve la API v2 del origen (texto/wiki
  markup). Si el destino usa Jira Cloud con formato ADF, habría que adaptar
  `create_target_issue` para convertir el texto a ese formato.
- El campo "Key Client" se localiza por nombre vía `/rest/api/2/field`; si hay
  varios campos con nombres parecidos, fija su id explícitamente con
  `DST_JIRA_KEY_CLIENT_FIELD`.
