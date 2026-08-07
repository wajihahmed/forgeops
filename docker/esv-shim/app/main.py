import base64
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
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
CATALINA_PROPERTIES_CM = "am-catalina-properties"
IDM_BOOT_PROPERTIES_CM = "idm-boot-properties"
RESTART_DEPLOYMENTS = ["am", "idm"]

GITEA_CLONE_URL = "http://forgerock:forgerock@gitea.fr-platform.svc.cluster.local:3000/forgerock/customer-config.git"
AM_FBC_ROOT = "/home/forgerock/openam/config/services"
AM_MIRROR_REALM_DIRS = ["realm/root-alpha", "realm/root-bravo"]

# Base catalina.properties content — Tomcat bootstrap loads this as system properties.
# ESV values are appended as esv.foo.bar=value entries by do_restart().
CATALINA_PROPERTIES_BASE = """\
package.access=sun.,org.apache.catalina.,org.apache.coyote.,org.apache.jasper.,org.apache.tomcat.
package.definition=sun.,java.,org.apache.catalina.,org.apache.coyote.,\\
org.apache.jasper.,org.apache.naming.,org.apache.tomcat.

common.loader="${catalina.base}/lib","${catalina.base}/lib/*.jar","${catalina.home}/lib","${catalina.home}/lib/*.jar"
server.loader=
shared.loader=

tomcat.util.scan.StandardJarScanFilter.jarsToSkip=\\
annotations-api.jar,\\
ant-junit*.jar,\\
ant-launcher*.jar,\\
ant*.jar,\\
asm-*.jar,\\
aspectj*.jar,\\
bcel*.jar,\\
biz.aQute.bnd*.jar,\\
bootstrap.jar,\\
catalina-ant.jar,\\
catalina-ha.jar,\\
catalina-ssi.jar,\\
catalina-storeconfig.jar,\\
catalina-tribes.jar,\\
catalina.jar,\\
cglib-*.jar,\\
cobertura-*.jar,\\
commons-beanutils*.jar,\\
commons-codec*.jar,\\
commons-collections*.jar,\\
commons-compress*.jar,\\
commons-daemon.jar,\\
commons-dbcp*.jar,\\
commons-digester*.jar,\\
commons-fileupload*.jar,\\
commons-httpclient*.jar,\\
commons-io*.jar,\\
commons-lang*.jar,\\
commons-logging*.jar,\\
commons-math*.jar,\\
commons-pool*.jar,\\
derby-*.jar,\\
dom4j-*.jar,\\
easymock-*.jar,\\
ecj-*.jar,\\
el-api.jar,\\
geronimo-spec-jaxrpc*.jar,\\
h2*.jar,\\
ha-api-*.jar,\\
hamcrest-*.jar,\\
hibernate*.jar,\\
httpclient*.jar,\\
icu4j-*.jar,\\
jakartaee-migration-*.jar,\\
jasper-el.jar,\\
jasper.jar,\\
jaspic-api.jar,\\
jaxb-*.jar,\\
jaxen-*.jar,\\
jaxws-rt-*.jar,\\
jdom-*.jar,\\
jetty-*.jar,\\
jmx-tools.jar,\\
jmx.jar,\\
jsp-api.jar,\\
jstl.jar,\\
jta*.jar,\\
junit-*.jar,\\
junit.jar,\\
log4j*.jar,\\
mail*.jar,\\
objenesis-*.jar,\\
oraclepki.jar,\\
org.hamcrest.core_*.jar,\\
org.junit_*.jar,\\
oro-*.jar,\\
servlet-api-*.jar,\\
servlet-api.jar,\\
slf4j*.jar,\\
taglibs-standard-spec-*.jar,\\
tagsoup-*.jar,\\
tomcat-api.jar,\\
tomcat-coyote.jar,\\
tomcat-coyote-ffm.jar,\\
tomcat-dbcp.jar,\\
tomcat-i18n-*.jar,\\
tomcat-jdbc.jar,\\
tomcat-jni.jar,\\
tomcat-juli-adapters.jar,\\
tomcat-juli.jar,\\
tomcat-util-scan.jar,\\
tomcat-util.jar,\\
tomcat-websocket.jar,\\
tools.jar,\\
unboundid-ldapsdk-*.jar,\\
websocket-api.jar,\\
websocket-client-api.jar,\\
wsdl4j*.jar,\\
xercesImpl.jar,\\
xml-apis.jar,\\
xmlParserAPIs-*.jar,\\
xmlParserAPIs.jar,\\
xom-*.jar

tomcat.util.scan.StandardJarScanFilter.jarsToScan=\\
log4j-taglib*.jar,\\
log4j-jakarta-web*.jar,\\
log4javascript*.jar,\\
slf4j-taglib*.jar

tomcat.util.buf.StringCache.byte.enabled=true
org.apache.el.GET_CLASSLOADER_USE_PRIVILEGED=false
"""

# Base boot.properties content — IDM loads this at startup; identityServer.getProperty() resolves against it.
# ESV values are appended as esv.foo.bar=value entries by do_restart().
BOOT_PROPERTIES_BASE = """\
openidm.repo.host=ds-idrepo-0.ds-idrepo
openidm.repo.port=1636
openidm.repo.user=uid=admin
openidm.repo.password=password
openidm.repo.databaseName=openidm
openidm.repo.schema=openidm

openidm.anonymous.password=anonymous

openidm.idpconfig.clientsecret=password

userstore.host=ds-idrepo-0.ds-idrepo
userstore.password=password
userstore.port=1636
userstore.basecontext=ou=identities

openidm.port.http=8080
openidm.port.https=8443
openidm.port.mutualauth=8444

openidm.lb.port.http=80
openidm.ln.port.https=443
openidm.auth.clientauthonlyports=8444

openidm.https.keystore.cert.alias=openidm-localhost

openidm.keystore.type=JCEKS
openidm.truststore.type=JKS
openidm.keystore.provider=SunJCE
openidm.truststore.provider=SUN
openidm.keystore.location=/var/run/secrets/idm/keystore.jceks
openidm.truststore.location=/opt/openidm/idmtruststore

openidm.keystore.password=changeit
openidm.truststore.password=changeit

openidm.prometheus.username=prometheus
openidm.prometheus.password=prometheus
openidm.prometheus.role=openidm-prometheus

openidm.servlet.alias=/openidm
openidm.servlet.upload.alias=/upload
openidm.servlet.export.alias=/export

openidm.config.crypto.alias=openidm-sym-default
openidm.script.javascript.debug=transport=socket,suspend=y,address=9888,trace=true

openidm.config.crypto.selfservice.sharedkey.alias=openidm-selfservice-key
openidm.config.crypto.jwtsession.hmackey.alias=openidm-jwtsessionhmac-key
openidm.config.crypto.opendj.localhost.cert=server-cert

openidm.ssl.host.aliases=localhost=

openidm.policy.enforcement.enabled=true

openidm.scheduler.execute.persistent.schedules=true

openidm.bonecp.statistics.enabled=false

javascript.exception.debug.info=false

openidm.external.rest.hostnameVerifier=ALLOW_ALL

openidm.cluster.remove.offline.node.state=true

openidm.apidescriptor.enabled=false

openidm.workflow.enabled=true

felix.fileinstall.enableConfigSave=true

com.iplanet.am.cookie.name=iPlanetDirectoryPro
com.sun.identity.auth.cookieName=AMAuthCookie

rs.client.secret=idm-resource-server
"""

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


def _escape_properties_value(value: str) -> str:
    """Escape a value for Java .properties format (backslash, then handle newlines)."""
    # Escape backslashes first, then newlines as \n (single line value)
    value = value.replace("\\", "\\\\")
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "\\r")
    return value


def _mirror_am_to_gitea():
    """
    Snapshot the live AM FBC realm directories from the AM pod and push them to
    Gitea so that filesystem-init re-populates the correct config on next pod restart.
    Called from do_restart() before AM is restarted — ensures journeys/nodes imported
    live via REST API survive the restart cycle.
    """
    from kubernetes.stream import stream as k8s_stream

    pods = core_v1.list_namespaced_pod(NAMESPACE, label_selector="app=am").items
    if not pods:
        raise RuntimeError("No AM pod found — cannot mirror config to Gitea")
    pod_name = pods[0].metadata.name

    # Stream realm dirs out of the pod as a base64-encoded tar so binary data
    # travels safely over the websocket text channel
    dirs_arg = " ".join(AM_MIRROR_REALM_DIRS)
    resp = k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        pod_name, NAMESPACE,
        command=["sh", "-c", f"tar -C {AM_FBC_ROOT} -cf - {dirs_arg} | base64"],
        stderr=False, stdin=False, stdout=True, tty=False,
    )
    tar_bytes = base64.b64decode(resp)

    clone_dir = tempfile.mkdtemp(prefix="esv-shim-mirror-")
    try:
        subprocess.run(
            ["git", "clone", GITEA_CLONE_URL, clone_dir],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "-C", clone_dir, "config", "user.email", "esv-shim@localhost"], check=True, capture_output=True)
        subprocess.run(["git", "-C", clone_dir, "config", "user.name", "esv-shim"], check=True, capture_output=True)

        am_services_dst = os.path.join(clone_dir, "am", "services")
        os.makedirs(am_services_dst, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
            tf.extractall(path=am_services_dst)

        subprocess.run(["git", "-C", clone_dir, "add", "am/services/realm/"], check=True, capture_output=True)
        r = subprocess.run(
            ["git", "-C", clone_dir, "diff", "--cached", "--quiet"],
            check=False, capture_output=True,
        )
        if r.returncode != 0:
            subprocess.run(
                ["git", "-C", clone_dir, "commit", "-m", "esv-shim: snapshot live AM config before restart"],
                check=True, capture_output=True,
            )
            subprocess.run(["git", "-C", clone_dir, "push"], check=True, capture_output=True)
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def do_restart():
    """
    Real AIC has no separate 'apply' step: a PUT to /environment/{secrets,variables}/{id}
    is durable immediately, and POST /environment/restart is what makes AM pick the new
    values up. AM scripts read ESVs via systemEnv.getProperty() which resolves against
    JVM system properties. Tomcat loads catalina.properties into system properties at
    bootstrap — so we project all ESVs there, translating esv-foo-bar -> esv.foo.bar.
    """
    cms = core_v1.list_namespaced_config_map(NAMESPACE, label_selector=f"{TYPE_LABEL}=variable").items
    secrets = core_v1.list_namespaced_secret(NAMESPACE, label_selector=f"{TYPE_LABEL}=secret").items

    esv_props = {}
    for cm in cms:
        plain = (cm.data or {}).get("value", "")
        dot_key = cm.metadata.name[len(VAR_PREFIX):].replace("-", ".")
        esv_props[dot_key] = plain

    for s in secrets:
        raw_b64 = (s.data or {}).get("value", "")
        try:
            plain = base64.b64decode(raw_b64).decode("utf-8", errors="replace")
        except Exception:
            plain = raw_b64
        dot_key = s.metadata.name[len(SECRET_PREFIX):].replace("-", ".")
        esv_props[dot_key] = plain

    esv_lines = "\n".join(
        f"{k}={_escape_properties_value(v)}"
        for k, v in sorted(esv_props.items())
    )
    esv_comment = "\n# ESV values injected by esv-shim\n"
    catalina_content = CATALINA_PROPERTIES_BASE + esv_comment + esv_lines + "\n"
    boot_content = BOOT_PROPERTIES_BASE + esv_comment + esv_lines + "\n"

    project_configmap(CATALINA_PROPERTIES_CM, {"catalina.properties": catalina_content})
    project_configmap(IDM_BOOT_PROPERTIES_CM, {"boot.properties": boot_content})

    try:
        _mirror_am_to_gitea()
    except Exception as exc:
        print(f"WARNING: AM config mirror to Gitea failed: {exc} — proceeding with restart anyway")

    restarted = [d for d in RESTART_DEPLOYMENTS if restart_deployment(d)]

    return {
        "variableCount": len([k for k in esv_props if k.startswith("esv.")]),
        "secretCount": len(secrets),
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
