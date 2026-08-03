import base64
import datetime
import json
import os
import re
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel

NAMESPACE = os.environ.get("NAMESPACE", "fr-platform")

MANAGED_LABEL = "esv.forgeops/managed"
TYPE_LABEL = "esv.forgeops/type"
DESC_ANNOTATION = "esv.forgeops/description"
ENCODING_ANNOTATION = "esv.forgeops/encoding"
USE_IN_PLACEHOLDERS_ANNOTATION = "esv.forgeops/use-in-placeholders"
EXPRESSION_TYPE_ANNOTATION = "esv.forgeops/expression-type"
UPDATED_ANNOTATION = "esv.forgeops/updated-at"
RESTART_ANNOTATION = "esv.forgeops/restarted-at"

VAR_PREFIX = "esv-var-"
SECRET_PREFIX = "esv-secret-"
MAPPING_PREFIX = "esv-mapping-"
PROJECTION_CONFIGMAP_NAME = "esv-variables"
PROJECTION_SECRET_NAME = "esv-secrets"
RESTART_DEPLOYMENTS = ["am", "idm"]

# ESV ids observed in the wild look like "esv-hmac-sha256-key-2" / "esv-error-map".
ID_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# AM secret alias IDs look like "am.services.httpclient.mtls.clientcert.wxaclientcrtmtls.secret"
MAPPING_ID_RE = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9._]*[a-zA-Z0-9]$")

try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

core_v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

app = FastAPI(
    title="ESV Shim",
    description="AIC ESV-compatible API (PUT-upsert, valueBase64 wire format) backed by "
    "Kubernetes ConfigMaps/Secrets",
)


class VariablePut(BaseModel):
    valueBase64: str
    description: Optional[str] = ""
    expressionType: Optional[str] = "string"


class SecretPut(BaseModel):
    valueBase64: str
    description: Optional[str] = ""
    encoding: Optional[str] = "generic"
    useInPlaceholders: Optional[bool] = True


def validate_id(esv_id: str) -> str:
    if len(esv_id) > 200 or not ID_RE.match(esv_id):
        raise HTTPException(
            status_code=400,
            detail="id must be a lowercase alphanumeric string, optionally "
            "with internal dashes (max 200 chars)",
        )
    return esv_id


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def get_configmap_or_404(cm_name: str, display_id: str):
    try:
        return core_v1.read_namespaced_config_map(cm_name, NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"variable '{display_id}' not found")
        raise HTTPException(status_code=500, detail=str(e))


def get_secret_or_404(secret_name: str, display_id: str):
    try:
        return core_v1.read_namespaced_secret(secret_name, NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"secret '{display_id}' not found")
        raise HTTPException(status_code=500, detail=str(e))


def configmap_to_variable(cm) -> dict:
    """Variables are metadata + valueBase64 — AIC returns the value directly on GET."""
    annotations = cm.metadata.annotations or {}
    plain = (cm.data or {}).get("value", "")
    return {
        "_id": cm.metadata.name[len(VAR_PREFIX):],
        "valueBase64": base64.b64encode(plain.encode()).decode(),
        "description": annotations.get(DESC_ANNOTATION, ""),
        "expressionType": annotations.get(EXPRESSION_TYPE_ANNOTATION, "string"),
        "lastChangeDate": annotations.get(UPDATED_ANNOTATION),
        "lastChangedBy": "esv-shim",
        "loaded": True,
    }


def secret_to_metadata(secret) -> dict:
    """Secrets never expose their value via GET — matches the real ESV API."""
    annotations = secret.metadata.annotations or {}
    return {
        "_id": secret.metadata.name[len(SECRET_PREFIX):],
        "activeVersion": "1",
        "loadedVersion": "1",
        "description": annotations.get(DESC_ANNOTATION, ""),
        "encoding": annotations.get(ENCODING_ANNOTATION, "generic"),
        "useInPlaceholders": annotations.get(USE_IN_PLACEHOLDERS_ANNOTATION, "true") == "true",
        "lastChangeDate": annotations.get(UPDATED_ANNOTATION),
        "lastChangedBy": "esv-shim",
        "loaded": True,
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/environment/variables")
def list_variables():
    cms = core_v1.list_namespaced_config_map(NAMESPACE, label_selector=f"{TYPE_LABEL}=variable")
    result = [configmap_to_variable(cm) for cm in cms.items]
    return {"result": result, "resultCount": len(result)}


@app.get("/environment/variables/{esv_id}")
def get_variable(esv_id: str):
    esv_id = validate_id(esv_id)
    cm = get_configmap_or_404(VAR_PREFIX + esv_id, esv_id)
    return configmap_to_variable(cm)


@app.put("/environment/variables/{esv_id}")
def put_variable(esv_id: str, body: VariablePut, response: Response):
    esv_id = validate_id(esv_id)
    cm_name = VAR_PREFIX + esv_id

    try:
        plain = base64.b64decode(body.valueBase64, validate=True).decode()
    except Exception:
        raise HTTPException(status_code=400, detail="valueBase64 is not valid base64")

    annotations = {
        DESC_ANNOTATION: body.description or "",
        EXPRESSION_TYPE_ANNOTATION: body.expressionType or "string",
        UPDATED_ANNOTATION: now(),
    }

    try:
        existing = core_v1.read_namespaced_config_map(cm_name, NAMESPACE)
        existing.data = {"value": plain}
        existing.metadata.annotations = {**(existing.metadata.annotations or {}), **annotations}
        core_v1.replace_namespaced_config_map(cm_name, NAMESPACE, existing)
        response.status_code = 200
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))
        cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=cm_name,
                labels={MANAGED_LABEL: "true", TYPE_LABEL: "variable"},
                annotations=annotations,
            ),
            data={"value": plain},
        )
        core_v1.create_namespaced_config_map(NAMESPACE, cm)
        response.status_code = 201

    return configmap_to_variable(core_v1.read_namespaced_config_map(cm_name, NAMESPACE))


@app.delete("/environment/variables/{esv_id}", status_code=204)
def delete_variable(esv_id: str):
    esv_id = validate_id(esv_id)
    cm_name = VAR_PREFIX + esv_id
    get_configmap_or_404(cm_name, esv_id)
    core_v1.delete_namespaced_config_map(cm_name, NAMESPACE)
    return None


@app.get("/environment/secrets")
def list_secrets():
    secrets = core_v1.list_namespaced_secret(NAMESPACE, label_selector=f"{TYPE_LABEL}=secret")
    result = [secret_to_metadata(s) for s in secrets.items]
    return {"result": result, "resultCount": len(result)}


@app.get("/environment/secrets/{esv_id}")
def get_secret(esv_id: str):
    esv_id = validate_id(esv_id)
    secret = get_secret_or_404(SECRET_PREFIX + esv_id, esv_id)
    return secret_to_metadata(secret)


@app.put("/environment/secrets/{esv_id}")
def put_secret(esv_id: str, body: SecretPut, response: Response):
    esv_id = validate_id(esv_id)
    secret_name = SECRET_PREFIX + esv_id

    try:
        base64.b64decode(body.valueBase64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="valueBase64 is not valid base64")

    annotations = {
        DESC_ANNOTATION: body.description or "",
        ENCODING_ANNOTATION: body.encoding or "generic",
        USE_IN_PLACEHOLDERS_ANNOTATION: "true" if body.useInPlaceholders else "false",
        UPDATED_ANNOTATION: now(),
    }
    # Kubernetes Secret.data values are themselves base64 strings, so the
    # client-supplied valueBase64 can be stored verbatim with no re-encoding.
    data = {"value": body.valueBase64}

    try:
        existing = core_v1.read_namespaced_secret(secret_name, NAMESPACE)
        existing.data = data
        existing.string_data = None
        existing.metadata.annotations = {**(existing.metadata.annotations or {}), **annotations}
        core_v1.replace_namespaced_secret(secret_name, NAMESPACE, existing)
        response.status_code = 200
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                labels={MANAGED_LABEL: "true", TYPE_LABEL: "secret"},
                annotations=annotations,
            ),
            data=data,
        )
        core_v1.create_namespaced_secret(NAMESPACE, secret)
        response.status_code = 201

    return secret_to_metadata(core_v1.read_namespaced_secret(secret_name, NAMESPACE))


@app.delete("/environment/secrets/{esv_id}", status_code=204)
def delete_secret(esv_id: str):
    esv_id = validate_id(esv_id)
    secret_name = SECRET_PREFIX + esv_id
    get_secret_or_404(secret_name, esv_id)
    core_v1.delete_namespaced_secret(secret_name, NAMESPACE)
    return None


def project_configmap(name: str, data: dict):
    try:
        existing = core_v1.read_namespaced_config_map(name, NAMESPACE)
        existing.data = data
        core_v1.replace_namespaced_config_map(name, NAMESPACE, existing)
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))
        cm = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=name), data=data)
        core_v1.create_namespaced_config_map(NAMESPACE, cm)


def project_secret(name: str, data: dict):
    try:
        existing = core_v1.read_namespaced_secret(name, NAMESPACE)
        existing.string_data = None
        existing.data = data
        core_v1.replace_namespaced_secret(name, NAMESPACE, existing)
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))
        secret = client.V1Secret(metadata=client.V1ObjectMeta(name=name), data=data)
        core_v1.create_namespaced_secret(NAMESPACE, secret)


def restart_deployment(deployment_name: str) -> bool:
    patch_body = {
        "spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: now()}}}}
    }
    try:
        apps_v1.patch_namespaced_deployment(deployment_name, NAMESPACE, patch_body)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise HTTPException(status_code=500, detail=str(e))


def do_restart():
    """
    Real AIC has no separate 'apply' step: a PUT to /environment/{secrets,variables}/{id}
    is durable immediately, and POST /environment/restart is what makes AM pick the new
    values up (see tenant_config_importer.py's _restart_am_for_esv). Since AM/IDM here read
    ESVs via envFrom rather than a live AIC-style property store, this endpoint additionally
    re-projects every item into esv-variables/esv-secrets before triggering the restart.
    """
    cms = core_v1.list_namespaced_config_map(NAMESPACE, label_selector=f"{TYPE_LABEL}=variable").items
    secrets = core_v1.list_namespaced_secret(NAMESPACE, label_selector=f"{TYPE_LABEL}=secret").items

    var_data = {}
    for cm in cms:
        plain = (cm.data or {}).get("value", "")
        var_data[cm.metadata.name[len(VAR_PREFIX):]] = plain

    secret_data = {}
    for s in secrets:
        raw = (s.data or {}).get("value", "")
        secret_data[s.metadata.name[len(SECRET_PREFIX):]] = raw

    project_configmap(PROJECTION_CONFIGMAP_NAME, var_data)
    project_secret(PROJECTION_SECRET_NAME, secret_data)

    restarted = [d for d in RESTART_DEPLOYMENTS if restart_deployment(d)]

    return {
        "variableCount": len(var_data),
        "secretCount": len(secret_data),
        "restarted": restarted,
    }


@app.post("/environment/restart")
def restart_environment():
    return do_restart()


@app.post("/environment/apply")
def apply_environment():
    """Deprecated alias of POST /environment/restart, kept for backward compatibility."""
    return do_restart()


# ---------------------------------------------------------------------------
# AM secret-store mapping endpoints
# Intercepts PUT/GET/DELETE for:
#   /am/json/realms/root/realms/{realm}/realm-config/secrets/stores/
#     GoogleSecretManagerSecretStoreProvider/ESV/mappings/{name}
# On real AIC this maps AM secret aliases to GCP Secret Manager via the
# GoogleSecretManagerSecretStoreProvider. Here we accept and persist the
# mapping to a ConfigMap so callers get 200 instead of a 404.
# ---------------------------------------------------------------------------

def _mapping_cm_name(realm: str, name: str) -> str:
    """Return a valid k8s ConfigMap name for a realm+mapping pair."""
    safe = re.sub(r"[^a-z0-9-]", "-", f"{realm}-{name}".lower())
    return f"{MAPPING_PREFIX}{safe[:200]}"


def _validate_mapping_name(name: str) -> str:
    if len(name) > 200 or not MAPPING_ID_RE.match(name):
        raise HTTPException(status_code=400, detail=f"invalid mapping name: {name!r}")
    return name


@app.put("/am/json/realms/root/realms/{realm}/realm-config/secrets/stores/GoogleSecretManagerSecretStoreProvider/ESV/mappings/{name}")
async def put_secret_store_mapping(realm: str, name: str, request: Request, response: Response):
    name = _validate_mapping_name(name)
    body = await request.json()
    body["_id"] = name
    cm_name = _mapping_cm_name(realm, name)
    payload = json.dumps(body)

    try:
        existing = core_v1.read_namespaced_config_map(cm_name, NAMESPACE)
        existing.data = {"mapping": payload}
        existing.metadata.annotations = {**(existing.metadata.annotations or {}), UPDATED_ANNOTATION: now()}
        core_v1.replace_namespaced_config_map(cm_name, NAMESPACE, existing)
        response.status_code = 200
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))
        cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=cm_name,
                labels={MANAGED_LABEL: "true", TYPE_LABEL: "mapping"},
                annotations={UPDATED_ANNOTATION: now()},
            ),
            data={"mapping": payload, "realm": realm, "name": name},
        )
        core_v1.create_namespaced_config_map(NAMESPACE, cm)
        response.status_code = 201

    return body


@app.get("/am/json/realms/root/realms/{realm}/realm-config/secrets/stores/GoogleSecretManagerSecretStoreProvider/ESV/mappings/{name}")
def get_secret_store_mapping(realm: str, name: str):
    name = _validate_mapping_name(name)
    cm_name = _mapping_cm_name(realm, name)
    try:
        cm = core_v1.read_namespaced_config_map(cm_name, NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"mapping '{name}' not found")
        raise HTTPException(status_code=500, detail=str(e))
    return json.loads((cm.data or {}).get("mapping", "{}"))


@app.delete("/am/json/realms/root/realms/{realm}/realm-config/secrets/stores/GoogleSecretManagerSecretStoreProvider/ESV/mappings/{name}", status_code=204)
def delete_secret_store_mapping(realm: str, name: str):
    name = _validate_mapping_name(name)
    cm_name = _mapping_cm_name(realm, name)
    try:
        core_v1.delete_namespaced_config_map(cm_name, NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"mapping '{name}' not found")
        raise HTTPException(status_code=500, detail=str(e))
    return None
