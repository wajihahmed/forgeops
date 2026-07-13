import base64
import datetime
import os
import re
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel

NAMESPACE = os.environ.get("NAMESPACE", "fr-platform")

MANAGED_LABEL = "esv.forgeops/managed"
TYPE_LABEL = "esv.forgeops/type"
DESC_ANNOTATION = "esv.forgeops/description"
UPDATED_ANNOTATION = "esv.forgeops/updated-at"
RESTART_ANNOTATION = "esv.forgeops/restarted-at"

VAR_PREFIX = "esv-var-"
SECRET_PREFIX = "esv-secret-"
PROJECTION_CONFIGMAP_NAME = "esv-variables"
PROJECTION_SECRET_NAME = "esv-secrets"
RESTART_DEPLOYMENTS = ["am", "idm"]

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

core_v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()

app = FastAPI(
    title="ESV Shim",
    description="AIC ESV-shaped API backed by Kubernetes ConfigMaps/Secrets",
)


class VariableCreate(BaseModel):
    name: str
    value: str
    description: Optional[str] = None


class VariableUpdate(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None


class SecretCreate(BaseModel):
    name: str
    value: str
    description: Optional[str] = None


class SecretUpdate(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None


def validate_name(name: str) -> str:
    if len(name) > 200 or not NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="name must be a lowercase alphanumeric string, optionally "
            "with internal dashes (max 200 chars)",
        )
    return name


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def get_configmap_or_404(cm_name: str, display_name: str):
    try:
        return core_v1.read_namespaced_config_map(cm_name, NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"variable '{display_name}' not found")
        raise HTTPException(status_code=500, detail=str(e))


def get_secret_or_404(secret_name: str, display_name: str):
    try:
        return core_v1.read_namespaced_secret(secret_name, NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"secret '{display_name}' not found")
        raise HTTPException(status_code=500, detail=str(e))


def configmap_to_item(cm) -> dict:
    annotations = cm.metadata.annotations or {}
    return {
        "name": cm.metadata.name[len(VAR_PREFIX):],
        "value": (cm.data or {}).get("value", ""),
        "description": annotations.get(DESC_ANNOTATION) or None,
        "updatedAt": annotations.get(UPDATED_ANNOTATION),
    }


def secret_to_item(secret, reveal: bool) -> dict:
    annotations = secret.metadata.annotations or {}
    raw = (secret.data or {}).get("value")
    value = base64.b64decode(raw).decode() if raw else ""
    return {
        "name": secret.metadata.name[len(SECRET_PREFIX):],
        "value": value if reveal else "********",
        "description": annotations.get(DESC_ANNOTATION) or None,
        "updatedAt": annotations.get(UPDATED_ANNOTATION),
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/environment/variables", status_code=201)
def create_variable(body: VariableCreate):
    name = validate_name(body.name)
    cm_name = VAR_PREFIX + name
    try:
        core_v1.read_namespaced_config_map(cm_name, NAMESPACE)
        raise HTTPException(status_code=409, detail=f"variable '{name}' already exists")
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))

    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=cm_name,
            labels={MANAGED_LABEL: "true", TYPE_LABEL: "variable"},
            annotations={
                DESC_ANNOTATION: body.description or "",
                UPDATED_ANNOTATION: now(),
            },
        ),
        data={"value": body.value},
    )
    core_v1.create_namespaced_config_map(NAMESPACE, cm)
    return configmap_to_item(core_v1.read_namespaced_config_map(cm_name, NAMESPACE))


@app.get("/environment/variables")
def list_variables():
    cms = core_v1.list_namespaced_config_map(NAMESPACE, label_selector=f"{TYPE_LABEL}=variable")
    return {"result": [configmap_to_item(cm) for cm in cms.items]}


@app.get("/environment/variables/{name}")
def get_variable(name: str):
    name = validate_name(name)
    cm = get_configmap_or_404(VAR_PREFIX + name, name)
    return configmap_to_item(cm)


@app.put("/environment/variables/{name}")
def update_variable(name: str, body: VariableUpdate):
    name = validate_name(name)
    cm_name = VAR_PREFIX + name
    cm = get_configmap_or_404(cm_name, name)

    if body.value is not None:
        cm.data = {**(cm.data or {}), "value": body.value}

    annotations = dict(cm.metadata.annotations or {})
    if body.description is not None:
        annotations[DESC_ANNOTATION] = body.description
    annotations[UPDATED_ANNOTATION] = now()
    cm.metadata.annotations = annotations

    core_v1.replace_namespaced_config_map(cm_name, NAMESPACE, cm)
    return configmap_to_item(core_v1.read_namespaced_config_map(cm_name, NAMESPACE))


@app.delete("/environment/variables/{name}", status_code=204)
def delete_variable(name: str):
    name = validate_name(name)
    cm_name = VAR_PREFIX + name
    get_configmap_or_404(cm_name, name)
    core_v1.delete_namespaced_config_map(cm_name, NAMESPACE)
    return None


@app.post("/environment/secrets", status_code=201)
def create_secret(body: SecretCreate):
    name = validate_name(body.name)
    secret_name = SECRET_PREFIX + name
    try:
        core_v1.read_namespaced_secret(secret_name, NAMESPACE)
        raise HTTPException(status_code=409, detail=f"secret '{name}' already exists")
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))

    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=secret_name,
            labels={MANAGED_LABEL: "true", TYPE_LABEL: "secret"},
            annotations={
                DESC_ANNOTATION: body.description or "",
                UPDATED_ANNOTATION: now(),
            },
        ),
        string_data={"value": body.value},
    )
    core_v1.create_namespaced_secret(NAMESPACE, secret)
    return secret_to_item(core_v1.read_namespaced_secret(secret_name, NAMESPACE), reveal=True)


@app.get("/environment/secrets")
def list_secrets():
    secrets = core_v1.list_namespaced_secret(NAMESPACE, label_selector=f"{TYPE_LABEL}=secret")
    return {"result": [secret_to_item(s, reveal=False) for s in secrets.items]}


@app.get("/environment/secrets/{name}")
def get_secret(name: str, showSecretValue: bool = Query(False)):
    name = validate_name(name)
    secret = get_secret_or_404(SECRET_PREFIX + name, name)
    return secret_to_item(secret, reveal=showSecretValue)


@app.put("/environment/secrets/{name}")
def update_secret(name: str, body: SecretUpdate):
    name = validate_name(name)
    secret_name = SECRET_PREFIX + name
    secret = get_secret_or_404(secret_name, name)

    if body.value is not None:
        secret.string_data = {"value": body.value}

    annotations = dict(secret.metadata.annotations or {})
    if body.description is not None:
        annotations[DESC_ANNOTATION] = body.description
    annotations[UPDATED_ANNOTATION] = now()
    secret.metadata.annotations = annotations

    core_v1.replace_namespaced_secret(secret_name, NAMESPACE, secret)
    return secret_to_item(core_v1.read_namespaced_secret(secret_name, NAMESPACE), reveal=True)


@app.delete("/environment/secrets/{name}", status_code=204)
def delete_secret(name: str):
    name = validate_name(name)
    secret_name = SECRET_PREFIX + name
    get_secret_or_404(secret_name, name)
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
        existing.data = None
        existing.string_data = data
        core_v1.replace_namespaced_secret(name, NAMESPACE, existing)
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))
        secret = client.V1Secret(metadata=client.V1ObjectMeta(name=name), string_data=data)
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


@app.post("/environment/apply")
def apply_environment():
    cms = core_v1.list_namespaced_config_map(NAMESPACE, label_selector=f"{TYPE_LABEL}=variable").items
    secrets = core_v1.list_namespaced_secret(NAMESPACE, label_selector=f"{TYPE_LABEL}=secret").items

    var_data = {cm.metadata.name[len(VAR_PREFIX):]: (cm.data or {}).get("value", "") for cm in cms}

    secret_data = {}
    for s in secrets:
        raw = (s.data or {}).get("value")
        secret_data[s.metadata.name[len(SECRET_PREFIX):]] = base64.b64decode(raw).decode() if raw else ""

    project_configmap(PROJECTION_CONFIGMAP_NAME, var_data)
    project_secret(PROJECTION_SECRET_NAME, secret_data)

    restarted = [d for d in RESTART_DEPLOYMENTS if restart_deployment(d)]

    return {
        "variableCount": len(var_data),
        "secretCount": len(secret_data),
        "restarted": restarted,
    }
