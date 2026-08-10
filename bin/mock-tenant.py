#!/usr/bin/env python3
"""
mock-tenant.py — CLI for the ForgeOps mock-tenant dev stack.

Commands:
  bootstrap                 Install cluster-wide prerequisites (once per cluster instance)
  deploy                    Deploy the application stack (AM, IDM, DS, Gitea, ESV shim) — requires bootstrap first
  push-config               Push config to Gitea and restart pod(s)

Usage:
  python3 bin/mock-tenant.py [--context CONTEXT] bootstrap
  python3 bin/mock-tenant.py [--context CONTEXT] deploy [--force]
  python3 bin/mock-tenant.py [--context CONTEXT] push-config [--target idm|am|all] [--saasrepo-path PATH]

--context defaults to "orbstack". Pass --context minikube (or set MOCK_TENANT_K8S_CONTEXT)
for other local Kubernetes runtimes.
"""

import argparse
import json
import os
import secrets
import shutil
import string
import subprocess
import tempfile
import time


PLATFORM_FQDN = "mock.iam.example.com"
NAMESPACE = "fr-platform"
K8S_CONTEXT = os.environ.get("MOCK_TENANT_K8S_CONTEXT", "orbstack")

# Static merged IDM config files committed to this repo — source of truth for Gitea.
IDM_CONF_STATIC_DIR = "kustomize/base/gitea-seed/idm-conf"
IDM_SCRIPT_STATIC_DIR = "kustomize/base/gitea-seed/idm-script"
_AIC_IDM_CONF_FILES = [
    ("managed", "managed.json"),
    ("repo-ds",  "repo.ds.json"),
]
_AIC_IDM_SCRIPT_FILES = [
    "teammember.js",
]

# Static AM config directory — empty for now, populated when AM config is managed via FBC.
AM_CONF_STATIC_DIR = "kustomize/base/gitea-seed/am-conf"

# Subpath within the saas repo root to the IDM override patch files.
_SAAS_IDM_OVERRIDES_SUBPATH = "services/idm/idm-idc-overrides/system"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def run(cmd, capture=False, check=True, timeout=600, **kwargs):
    """Run a shell command, print it, return CompletedProcess."""
    print(f"  $ {cmd}")
    result = subprocess.run(
        cmd, shell=True, text=True,
        capture_output=capture, timeout=timeout, **kwargs,
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"Command failed (exit {result.returncode}):\n"
            f"{result.stdout or ''}{result.stderr or ''}"
        )
    return result


def kubectl(args, capture=False, check=True, timeout=120):
    return run(f"kubectl {args}", capture=capture, check=check, timeout=timeout)


def kube_secret_value(secret, key, namespace=NAMESPACE):
    import base64
    r = kubectl(
        f"get secret {secret} -n {namespace} -o jsonpath='{{.data.{key}}}'",
        capture=True,
    )
    return base64.b64decode(r.stdout.strip().strip("'")).decode()


def curl_json(cmd):
    r = run(cmd, capture=True)
    return json.loads(r.stdout)


def step(n, title):
    print(f"\n{'='*60}")
    print(f"Step {n} — {title}")
    print("="*60)


class _gitea_portforward:
    """Context manager: port-forward Gitea svc to localhost:3000."""
    def __enter__(self):
        self._pf = subprocess.Popen(
            f"kubectl port-forward -n {NAMESPACE} svc/gitea 3000:3000",
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        return self

    def __exit__(self, *_):
        self._pf.terminate()


def _gitea_clone_push(clone_work_fn, commit_message):
    """
    Clone customer-config, call clone_work_fn(clone_dir) to mutate files,
    then commit and push if anything changed. Returns True if pushed.
    Assumes Gitea is already port-forwarded to localhost:3000.
    """
    clone_dir = tempfile.mkdtemp(prefix="customer-config-")
    try:
        run(
            "git clone http://forgerock:forgerock@localhost:3000/forgerock/customer-config"
            f" {clone_dir}",
        )
        run(f"git -C {clone_dir} config user.email deploy@localhost")
        run(f"git -C {clone_dir} config user.name 'mock-tenant'")

        clone_work_fn(clone_dir)

        r = run(f"git -C {clone_dir} diff --cached --quiet", check=False)
        if r.returncode == 0:
            return False  # nothing changed
        run(f"git -C {clone_dir} commit -m '{commit_message}'")
        run(f"git -C {clone_dir} push")
        return True
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# deploy subcommand — application deployment (AM, IDM, DS, Gitea, ESV shim)
# Requires bootstrap to have been run first on the cluster.
# ---------------------------------------------------------------------------

def _step_prerequisites():
    step(0, "Prerequisites")

    r = run("kubectl config current-context", capture=True)
    ctx = r.stdout.strip()
    if ctx != K8S_CONTEXT:
        raise SystemExit(f"Wrong Kubernetes context: {ctx!r}. Run: kubectl config use-context {K8S_CONTEXT}")
    print(f"  Context: {ctx} ✓")

    for f in [
        "kustomize/overlay/mock-tenant/ds-idrepo/sts.yaml",
        "kustomize/overlay/mock-tenant/ds-cts/sts.yaml",
    ]:
        r = run(f"grep storageClassName {f}", capture=True)
        if "local-path" not in r.stdout:
            raise SystemExit(f"{f} does not use local-path storageClassName")
    print("  storageClassName: local-path ✓")

    r = run(
        "grep -E 'FQDN|AM_SERVER_FQDN' kustomize/overlay/mock-tenant/base/platform-config.yaml",
        capture=True,
    )
    if r.stdout.count(PLATFORM_FQDN) < 2:
        raise SystemExit(f"platform-config.yaml is missing FQDN or AM_SERVER_FQDN = {PLATFORM_FQDN}")
    print("  FQDN / AM_SERVER_FQDN ✓")

    r = run(f"grep {PLATFORM_FQDN} /etc/hosts", capture=True)
    if "127.0.0.1" not in r.stdout:
        raise SystemExit(f"/etc/hosts must have: 127.0.0.1 {PLATFORM_FQDN}")
    print("  /etc/hosts ✓")

    for ns in ["cert-manager", "ingress-nginx", "secret-generator"]:
        kubectl(f"get namespace {ns}", capture=True)
        print(f"  {ns} namespace ✓")

    kubectl("get deployment metrics-server -n kube-system", capture=True)
    print("  metrics-server ✓")

    r = kubectl(
        "get configmap coredns-custom -n kube-system -o jsonpath='{.data}'",
        capture=True, check=False,
    )
    if f"rewrite name {PLATFORM_FQDN}" not in r.stdout:
        print("  CoreDNS fix: applying...")
        run(
            f"kubectl create configmap coredns-custom -n kube-system "
            f"--from-literal=local-hosts.override='rewrite name {PLATFORM_FQDN} "
            "ingress-nginx-controller.ingress-nginx.svc.cluster.local' "
            "--dry-run=client -o yaml | kubectl apply -f -"
        )
        kubectl("rollout restart deployment/coredns -n kube-system")
        kubectl("rollout status deployment/coredns -n kube-system --timeout=60s", timeout=70)
    print("  CoreDNS /etc/hosts leak fix ✓")


def _step_build_images():
    step(1, "Build config-loader, esv-shim, and ds images")

    r = run("docker context ls --format '{{.Name}} {{.Current}}'", capture=True)
    active = next((l.split()[0] for l in r.stdout.splitlines() if "true" in l.lower()), "")
    ctx = f"docker --context {K8S_CONTEXT}" if active != K8S_CONTEXT else "docker"

    for tag, path, dockerfile in [
        ("config-loader:local", "docker/config-loader/", None),
        ("esv-shim:local",      "docker/esv-shim/",      None),
        ("ds:local-base",       "docker/ds/",            None),
        ("ds:local",            "docker/ds/",            "Dockerfile.mock-tenant"),
    ]:
        print(f"  Building {tag}...")
        df_flag = f"-f {path}{dockerfile} " if dockerfile else ""
        run(f"{ctx} build {df_flag}-t {tag} {path}", timeout=300)

    run(f"{ctx} images --format 'table {{{{.Repository}}}}\\t{{{{.Tag}}}}' | grep -E 'config-loader|esv-shim|^ds'")


def _step_create_namespace():
    step(2, f"Create namespace {NAMESPACE}")
    r = kubectl(f"get namespace {NAMESPACE}", capture=True, check=False)
    if r.returncode != 0:
        kubectl(f"create namespace {NAMESPACE}")
    kubectl(f"config set-context --current --namespace={NAMESPACE}")
    print(f"  Namespace {NAMESPACE} ready ✓")


def _step_deploy_gitea():
    step(3, "Deploy Gitea")
    kubectl("apply -k kustomize/overlay/mock-tenant/gitea/")
    kubectl(f"rollout status deployment/gitea -n {NAMESPACE} --timeout=120s", timeout=130)


def _step_seed_customer_config():
    step(4, "Seed customer-config repo")
    kubectl("apply -k kustomize/overlay/mock-tenant/gitea-seed/ --server-side")
    kubectl(f"wait --for=condition=complete job/gitea-seed -n {NAMESPACE} --timeout=120s", timeout=130)
    r = kubectl(f"logs job/gitea-seed -n {NAMESPACE} -c seed", capture=True)
    if "Seeding complete" not in r.stdout and "already exists" not in r.stdout:
        raise SystemExit(f"gitea-seed did not complete successfully:\n{r.stdout}")
    print("  Seeding complete ✓")


def _step_deploy_esv_shim():
    step(5, "Deploy ESV shim")
    kubectl("apply -k kustomize/overlay/mock-tenant/esv-shim/")
    kubectl(f"rollout status deployment/esv-shim -n {NAMESPACE} --timeout=60s", timeout=70)


def _step_deploy_ds():
    step(6, "Deploy DS and secrets")
    run(f"bin/forgeops apply -e mock-tenant -n {NAMESPACE} base ds-cts ds-idrepo", timeout=120)
    kubectl(f"rollout status statefulset/ds-cts -n {NAMESPACE} --timeout=300s", timeout=310)
    kubectl(f"rollout status statefulset/ds-idrepo -n {NAMESPACE} --timeout=300s", timeout=310)
    kubectl(f"wait --for=condition=complete job/ds-set-passwords -n {NAMESPACE} --timeout=120s", timeout=130)
    print("  DS and ds-set-passwords ✓")


def _step_deploy_keystore():
    step(7, "Deploy keystore-create Job")
    kubectl("apply -k kustomize/overlay/mock-tenant/keystore-create/")
    kubectl(f"wait --for=condition=complete job/keystore-create -n {NAMESPACE} --timeout=120s", timeout=130)
    kubectl(f"get secret keystore -n {NAMESPACE}", capture=True)
    print("  keystore secret ✓")


def _step_deploy_tls():
    step(8, "Issue TLS certificate")
    kubectl("apply -k kustomize/overlay/mock-tenant/tls/")
    kubectl(f"wait --for=condition=Ready certificate/platform-tls -n {NAMESPACE} --timeout=60s", timeout=70)
    print("  platform-tls ✓")


def _step_deploy_am_idm_uis():
    step(9, "Deploy AM, IDM, admin-ui, login-ui, end-user-ui")
    run(f"bin/forgeops apply -e mock-tenant -n {NAMESPACE} am idm admin-ui login-ui end-user-ui", timeout=120)


def _am_token(admin_pw, retries=20, delay=15):
    cmd = (
        f'curl -sk -X POST "https://{PLATFORM_FQDN}/am/json/realms/root/authenticate" '
        f'-H "X-OpenAM-Username: amadmin" -H "X-OpenAM-Password: {admin_pw}" '
        f'-H "Content-Type: application/json"'
    )
    for attempt in range(1, retries + 1):
        try:
            d = curl_json(cmd)
            return d["tokenId"]
        except (json.JSONDecodeError, KeyError):
            if attempt == retries:
                raise
            print(f"  AM not ready yet (attempt {attempt}/{retries}), retrying in {delay}s...")
            time.sleep(delay)
    raise RuntimeError("AM did not become ready in time")


def _ensure_clean_secret(secret_name, secret_key):
    """Regenerate a k8s secret value in-place if it contains + or / (breaks AM Basic Auth)."""
    import base64
    r = kubectl(
        f"get secret {secret_name} -n {NAMESPACE} -o jsonpath='{{.data.{secret_key}}}'",
        capture=True,
    )
    raw = base64.b64decode(r.stdout.strip().strip("'")).decode()
    if any(c in raw for c in ("+", "/")):
        print(f"  {secret_key} contains +// — regenerating to alphanumeric-only...")
        new_secret = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
        new_b64 = base64.b64encode(new_secret.encode()).decode()
        kubectl(
            f"patch secret {secret_name} -n {NAMESPACE} --type='json' "
            f"-p='[{{\"op\": \"replace\", \"path\": \"/data/{secret_key}\", \"value\": \"{new_b64}\"}}]'"
        )
        print(f"  Regenerated ✓")
    else:
        print(f"  {secret_key} is clean (no +//) ✓")


def _am_put_oauth2_client_secret(token, realm_path, client_id, secret):
    """GET the OAuth2 client config, flatten it, set the new secret, PUT it back.
    If the client doesn't exist in the target realm, copy it from the root realm first.
    Note: If-Match header must be omitted on creation — AM returns 404 with it."""
    r = run(
        f'curl -sk "https://{PLATFORM_FQDN}/am/json/{realm_path}/realm-config/agents/OAuth2Client/{client_id}" '
        f'-H "iPlanetDirectoryPro: {token}" -H "Accept-API-Version: resource=1.0"',
        capture=True,
    )
    client_data = json.loads(r.stdout)
    creating = False
    if "code" in client_data:
        # Client doesn't exist in this realm — copy from root realm
        print(f"    {client_id} not found in {realm_path} — copying from root realm...")
        r = run(
            f'curl -sk "https://{PLATFORM_FQDN}/am/json/realms/root/realm-config/agents/OAuth2Client/{client_id}" '
            f'-H "iPlanetDirectoryPro: {token}" -H "Accept-API-Version: resource=1.0"',
            capture=True,
        )
        client_data = json.loads(r.stdout)
        if "code" in client_data:
            print(f"    {client_id} not found in root realm either — skipping")
            return False
        creating = True
    flat = {}
    for section in [
        "overrideOAuth2ClientConfig", "advancedOAuth2ClientConfig",
        "signEncOAuth2ClientConfig", "coreOAuth2ClientConfig",
        "coreOpenIDClientConfig", "coreUmaClientConfig",
    ]:
        flat.update(client_data.get(section, {}))
    flat["_id"] = client_id
    flat["userpassword"] = secret
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(flat, f)
        tmp_path = f.name
    # If-Match: * causes 404 on creation — only include it for updates
    if_match = "" if creating else '-H "If-Match: *" '
    run(
        f'curl -sk -X PUT "https://{PLATFORM_FQDN}/am/json/{realm_path}/realm-config/agents/OAuth2Client/{client_id}" '
        f'-H "iPlanetDirectoryPro: {token}" -H "Accept-API-Version: resource=1.0" '
        f'-H "Content-Type: application/json" {if_match}'
        f'-d @{tmp_path}',
        capture=True,
    )
    os.unlink(tmp_path)
    return True


def _step_amster_and_fix_secret():
    step(11, "Deploy amster and fix OAuth2 client secrets")

    kubectl(f"rollout status deployment/am -n {NAMESPACE} --timeout=300s", timeout=310)
    run(f"bin/forgeops apply -e mock-tenant -n {NAMESPACE} amster", timeout=60)
    kubectl(f"wait --for=condition=complete job/amster -n {NAMESPACE} --timeout=300s", timeout=310)

    r = kubectl(f"logs job/amster -n {NAMESPACE} -c amster", capture=True)
    if "Import done" not in r.stdout:
        raise SystemExit(f"Amster import did not succeed:\n{r.stdout[-2000:]}")
    print("  Amster import done ✓")

    # Ensure neither secret contains + or / (breaks AM Basic Auth parsing)
    _ensure_clean_secret("amster-env-secrets", "IDM_RS_CLIENT_SECRET")
    _ensure_clean_secret("amster-env-secrets", "IDM_PROVISIONING_CLIENT_SECRET")

    admin_pw = kube_secret_value("am-env-secrets", "AM_PASSWORDS_AMADMIN_CLEAR")
    token = _am_token(admin_pw)

    idm_rs_secret = kube_secret_value("amster-env-secrets", "IDM_RS_CLIENT_SECRET")
    idm_prov_secret = kube_secret_value("amster-env-secrets", "IDM_PROVISIONING_CLIENT_SECRET")

    # Push correct secrets into all realms (amster only sets root, and uses hardcoded defaults).
    # Sub-realm API paths use the short form (/am/json/alpha/...) not realms/root/realms/alpha.
    for realm_path, realm_label in [
        ("realms/root", "root"),
        ("alpha", "alpha"),
        ("bravo", "bravo"),
    ]:
        print(f"  Fixing OAuth2 client secrets in realm {realm_label!r}...")
        ok = _am_put_oauth2_client_secret(token, realm_path, "idm-resource-server", idm_rs_secret)
        if ok:
            print(f"    idm-resource-server ✓")
        ok = _am_put_oauth2_client_secret(token, realm_path, "idm-provisioning", idm_prov_secret)
        if ok:
            print(f"    idm-provisioning ✓")

    # Verify root idm-resource-server auth works
    r = run(
        f'curl -sk -X POST "https://{PLATFORM_FQDN}/am/oauth2/introspect" '
        f'-u "idm-resource-server:{idm_rs_secret}" -d "token=dummy"',
        capture=True,
    )
    d = json.loads(r.stdout)
    if "error" in d:
        raise SystemExit(f"idm-resource-server auth failed after PUT: {d}")
    print(f"  idm-resource-server introspect (root): {d} ✓")
    # IDM restart deferred to push-config step — IDM will restart once there
    # with both the correct secret (envFrom) and the correct Gitea config.


def _step_create_realms():
    step(10, "Create alpha and bravo realms")
    kubectl(f"rollout status deployment/am -n {NAMESPACE} --timeout=300s", timeout=310)
    admin_pw = kube_secret_value("am-env-secrets", "AM_PASSWORDS_AMADMIN_CLEAR")
    token = _am_token(admin_pw)

    for attempt in range(1, 21):
        try:
            r = run(
                f'curl -sk "https://{PLATFORM_FQDN}/am/json/global-config/realms/?_queryFilter=true" '
                f'-H "iPlanetDirectoryPro: {token}" -H "Accept-API-Version: resource=1.0"',
                capture=True,
            )
            existing = {realm["name"] for realm in json.loads(r.stdout)["result"]}
            break
        except (json.JSONDecodeError, KeyError):
            if attempt == 20:
                raise
            print(f"  AM realms endpoint not ready yet (attempt {attempt}/20), retrying in 15s...")
            time.sleep(15)
            token = _am_token(admin_pw)

    for realm in ("alpha", "bravo"):
        if realm in existing:
            print(f"  Realm {realm!r} already exists, skipping")
            continue
        print(f"  Creating realm {realm!r}...")
        run(
            f'curl -sk -X POST "https://{PLATFORM_FQDN}/am/json/global-config/realms/?_action=create" '
            f'-H "iPlanetDirectoryPro: {token}" -H "Accept-API-Version: resource=1.0" '
            f'-H "Content-Type: application/json" '
            f'-d \'{{"name": "{realm}", "parentPath": "/", "active": true, "aliases": []}}\'',
            capture=True,
        )
    print("  Realms ✓")


def _step_create_secret_store():
    step("10b", "Create FileSystemSecretStore/ESV in AM (global + per-realm)")
    admin_pw = kube_secret_value("am-env-secrets", "AM_PASSWORDS_AMADMIN_CLEAR")
    token = _am_token(admin_pw)

    store_payload = '{"_id": "ESV", "format": "PEM", "directory": "/home/forgerock/openam/esv-secrets"}'

    # Global store
    r = run(
        f'curl -sk "https://{PLATFORM_FQDN}/am/json/global-config/secrets/stores/FileSystemSecretStore/ESV" '
        f'-H "iPlanetDirectoryPro: {token}" -H "Accept-API-Version: resource=1.0"',
        capture=True, check=False,
    )
    if r.returncode == 0 and '"_id"' in r.stdout:
        print("  FileSystemSecretStore/ESV (global) already exists, skipping")
    else:
        run(
            f'curl -sk -X POST '
            f'"https://{PLATFORM_FQDN}/am/json/global-config/secrets/stores/FileSystemSecretStore/?_action=create" '
            f'-H "iPlanetDirectoryPro: {token}" -H "Accept-API-Version: resource=1.0" '
            f'-H "Content-Type: application/json" '
            f"-d '{store_payload}'",
            capture=True,
        )
        print("  FileSystemSecretStore/ESV (global) ✓")

    for realm in ("alpha", "bravo"):
        r = run(
            f'curl -sk "https://{PLATFORM_FQDN}/am/json/realms/root/realms/{realm}/realm-config/secrets/stores/FileSystemSecretStore/ESV" '
            f'-H "iPlanetDirectoryPro: {token}" -H "Accept-API-Version: resource=1.0"',
            capture=True, check=False,
        )
        if r.returncode == 0 and '"_id"' in r.stdout:
            print(f"  FileSystemSecretStore/ESV ({realm}) already exists, skipping")
        else:
            run(
                f'curl -sk -X POST '
                f'"https://{PLATFORM_FQDN}/am/json/realms/root/realms/{realm}/realm-config/secrets/stores/FileSystemSecretStore?_action=create" '
                f'-H "iPlanetDirectoryPro: {token}" -H "Accept-API-Version: resource=1.0" '
                f'-H "Content-Type: application/json" '
                f"-d '{store_payload}'",
                capture=True,
            )
            print(f"  FileSystemSecretStore/ESV ({realm}) ✓")


def _step_create_tenant_stubs():
    step("11a", "Create tenant stubs (org-system ConfigMaps)")

    r = kubectl("get namespace org-system", capture=True, check=False)
    if r.returncode != 0:
        kubectl("create namespace org-system")
        print("  namespace org-system created ✓")
    else:
        print("  namespace org-system already exists ✓")

    engine_state = {
        "ENGINE_DISABLED": "false",
        "ENGINE_DISABLED_REASON": "",
        "CURRENT_VERSION": "mock-tenant",
        "CURRENT_RELEASE_VERSION": "0.0.1",
        "LAST_SUCCESSFUL_VERSION": "mock-tenant",
        "LAST_SUCCESSFUL_RELEASE_VERSION": "0.0.1",
        "LAST_SUCCESSFUL_RELEASE_VERSION_TIMESTAMP": "2026-01-01T00:00Z",
        "UPGRADED_FROM_VERSION": "",
        "EMA_EDITORS": "",
    }

    tenant_state = {
        "ENVIRONMENT_IMMUTABLE": "false",
        "INSTALL_COMPLETE": "true",
        "FEATURES": "identity-cloud",
        "FEATURES_ACTIVE": "identity-cloud",
        "FEATURES_ENABLED": "identity-cloud",
        "FEATURES_UNSUPPORTED": "",
        "DUMPSTER_ENABLED": "false",
        "ENGINE_DISABLED": "false",
        "TENANT_TIER": "production",
        "CLOUD_ARMOR_PREVIEW_MODE": "false",
        "DOMAIN": f".{PLATFORM_FQDN}",
        "SUBDOMAIN": "mock",
        "ACTIVE_REGION": "local",
        "CLUSTER_NAME": K8S_CONTEXT,
        "CLUSTER_REGION": "local",
        "ONBOARDING_STARTED": "true",
        "CONTINUE_ON_UNRESOLVED_PLACEHOLDERS": "true",
        "USERS_SUPPORTED_MILLIONS": "1",
        "DESIRED_USERS_SUPPORTED_MILLIONS": "1",
        "IDM_FEATURES_INSTALLED": "aiagent,groups,indexed/strings/6thru20",
    }

    for name, data in [("engine-state", engine_state), ("tenant-state", tenant_state)]:
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": "org-system"},
            "data": data,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(manifest, f)
            tmp = f.name
        kubectl(f"apply -f {tmp}")
        os.unlink(tmp)
        print(f"  {name} ✓")

    # Stub CronJob so load tests can read the image tag — suspended, never runs.
    org_engine_cronjob = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": "org-engine",
            "namespace": "org-system",
            "labels": {"app": "org-engine"},
        },
        "spec": {
            "schedule": "*/1 * * * *",
            "suspend": True,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 1,
            "failedJobsHistoryLimit": 1,
            "jobTemplate": {
                "spec": {
                    "template": {
                        "spec": {
                            "restartPolicy": "OnFailure",
                            "containers": [{
                                "name": "org-engine",
                                "image": "mock-tenant/org-engine:mock",
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits":   {"cpu": "1",    "memory": "512Mi"},
                                },
                            }],
                        }
                    }
                }
            },
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(org_engine_cronjob, f)
        tmp = f.name
    kubectl(f"apply -f {tmp}")
    os.unlink(tmp)
    print("  org-engine cronjob (stub) ✓")

    # am-logging-config — ConfigMap containing a logback.xml that sets AM to WARN level
    # with structured JSON output. ForgeOps AM does not consume this at runtime; it is a 
    # well-known artefact read by the load-testing harness (pyrock/lodestar) running in 
    # the cluster so it knows how to parse and ingest AM log output during a test run.
    am_logging_config = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "am-logging-config", "namespace": NAMESPACE},
        "data": {
            "logback.xml": (
                "<configuration>\n"
                "    <appender name=\"JSON\" class=\"ch.qos.logback.core.ConsoleAppender\">\n"
                "        <encoder class=\"ch.qos.logback.core.encoder.LayoutWrappingEncoder\">\n"
                "            <layout class=\"org.forgerock.openam.logback.JsonLayout\">\n"
                "                <timestampFormat>yyyy-MM-dd'T'HH:mm:ss.SSSX</timestampFormat>\n"
                "                <timestampFormatTimezoneId>Etc/UTC</timestampFormatTimezoneId>\n"
                "                <jsonFormatter class=\"ch.qos.logback.contrib.jackson.JacksonJsonFormatter\"/>\n"
                "                <appendLineSeparator>true</appendLineSeparator>\n"
                "            </layout>\n"
                "        </encoder>\n"
                "        <immediateFlush>true</immediateFlush>\n"
                "    </appender>\n"
                "    <root level=\"WARN\">\n"
                "        <appender-ref ref=\"JSON\" />\n"
                "    </root>\n"
                "    <logger name=\"org.forgerock.openam.uma.rest.UmaPolicyApplicationListener\" level=\"Off\"/>\n"
                "    <logger name=\"org.forgerock.openam.auth.nodes.helpers.ScriptedNodeHelper\" level=\"ERROR\"/>\n"
                "    <logger name=\"com.sun.identity.monitoring.MonitoringServicesImpl\" level=\"ERROR\"/>\n"
                "</configuration>\n"
            )
        },
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(am_logging_config, f)
        tmp = f.name
    kubectl(f"apply -f {tmp}")
    os.unlink(tmp)
    print("  am-logging-config ✓")

    # org-public/haproxy — load tests run `kubectl rollout restart deployment/haproxy -n org-public`.
    # A real Deployment is required (rollout restart patches the spec); a stub with pause container is sufficient.
    for manifest in [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": "org-public"},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "haproxy", "namespace": "org-public", "labels": {"app": "haproxy"}},
            "spec": {
                "replicas": 0,
                "selector": {"matchLabels": {"app": "haproxy"}},
                "template": {
                    "metadata": {"labels": {"app": "haproxy"}},
                    "spec": {
                        "containers": [{
                            "name": "haproxy",
                            "image": "gcr.io/google-containers/pause:3.9",
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "16Mi"},
                                "limits":   {"cpu": "10m", "memory": "16Mi"},
                            },
                        }],
                    },
                },
            },
        },
    ]:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(manifest, f)
            tmp = f.name
        kubectl(f"apply -f {tmp}")
        os.unlink(tmp)
    print("  org-public/haproxy deployment (stub) ✓")


def _step_create_pkce_client():
    step("11b", "Create idmAdminClient PKCE OAuth2 client in AM")

    admin_pw = kube_secret_value("am-env-secrets", "AM_PASSWORDS_AMADMIN_CLEAR")
    token = _am_token(admin_pw)

    client_id = "idmAdminClient"
    url = f"https://{PLATFORM_FQDN}/am/json/realms/root/realm-config/agents/OAuth2Client/{client_id}"

    client = {
        "clientType": "Public",
        "tokenEndpointAuthMethod": "none",
        "grantTypes": ["authorization_code", "implicit"],
        "responseTypes": ["code", "code id_token"],
        "scopes": ["openid", "fr:idm:*"],
        "redirectionUris": [
            f"https://{PLATFORM_FQDN}/platform/appAuthHelperRedirect.html",
            f"https://{PLATFORM_FQDN}/platform/sessionCheck.html",
        ],
        "isConsentImplied": True,
        "status": "Active",
        "accessTokenLifetime": 240,
        "authorizationCodeLifetime": 0,
        "refreshTokenLifetime": 0,
        "idTokenSignedResponseAlg": "RS256",
        "userinfoResponseFormat": "JSON",
        "tokenIntrospectionResponseFormat": "JSON",
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(client, f)
        tmp = f.name

    run(
        f'curl -sk -X PUT "{url}" '
        f'-H "iPlanetDirectoryPro: {token}" -H "Accept-API-Version: resource=1.0" '
        f'-H "Content-Type: application/json" '
        f'-d @{tmp}',
        capture=True,
    )
    os.unlink(tmp)
    print(f"  {client_id} created/updated ✓")


def _step_verify_fbc():
    step(12, "Verify FBC init containers")
    for app in ("am", "idm"):
        r = kubectl(f"logs -n {NAMESPACE} -l app={app} -c custom-vol-init", capture=True)
        if "config-loader done" not in r.stdout:
            raise SystemExit(f"{app} custom-vol-init did not complete:\n{r.stdout}")
        print(f"  {app} custom-vol-init: done ✓")


def _port_forward_health(pod_selector, local_port, pod_port, path):
    """Returns (status_line, body) from a curl -si request via port-forward."""
    r = kubectl(
        f"get pod -n {NAMESPACE} -l {pod_selector} -o jsonpath='{{.items[0].metadata.name}}'",
        capture=True,
    )
    pod = r.stdout.strip().strip("'")
    pf = subprocess.Popen(
        f"kubectl port-forward -n {NAMESPACE} pod/{pod} {local_port}:{pod_port}",
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(4)
    try:
        r = run(f"curl -si --max-time 5 http://localhost:{local_port}{path}", capture=True, check=False)
        lines = r.stdout.splitlines() if r.stdout else []
        status_line = lines[0] if lines else "(no response)"
        body = lines[-1] if len(lines) > 1 else ""
        return status_line, body
    finally:
        pf.terminate()


def _step_health_checks():
    step(13, "Health checks")
    for dep in ("am", "idm", "admin-ui", "login-ui", "end-user-ui"):
        kubectl(f"rollout status deployment/{dep} -n {NAMESPACE} --timeout=300s", timeout=310)

    am_status, _ = _port_forward_health("app=am", 18080, 8080, "/am/json/health/live")
    if "200" not in am_status:
        raise SystemExit(f"AM health check failed: {am_status}")
    print(f"  AM: {am_status} ✓")

    idm_status, idm_body = _port_forward_health("app=idm", 18180, 8080, "/openidm/info/ping")
    if "ACTIVE_READY" not in idm_body:
        raise SystemExit(f"IDM not ready: {idm_status} {idm_body}")
    print(f"  IDM: {idm_body} ✓")

    kubectl(f"get pods -n {NAMESPACE}")


def _step_print_credentials():
    step(15, "Credentials")
    amadmin_pw = kube_secret_value("am-env-secrets", "AM_PASSWORDS_AMADMIN_CLEAR")
    print(f"\n  amadmin password: {amadmin_pw}")
    print("\n  Browser access:")
    print("    Run:  bin/tunnel")
    print(f"    AM:   https://{PLATFORM_FQDN}/am")
    print(f"    UI:   https://{PLATFORM_FQDN}/platform")


def _step_bootstrap():
    step("bootstrap", "Install cluster-wide prerequisites")

    # 1. Verify Kubernetes context
    r = run("kubectl config current-context", capture=True)
    ctx = r.stdout.strip()
    if ctx != K8S_CONTEXT:
        raise SystemExit(f"Wrong Kubernetes context: {ctx!r}. Run: kubectl config use-context {K8S_CONTEXT}")
    print(f"  Context: {ctx} ✓")

    # 2. /etc/hosts entry
    r = run(f"grep {PLATFORM_FQDN} /etc/hosts", capture=True, check=False)
    if "127.0.0.1" not in r.stdout:
        raise SystemExit(
            f"/etc/hosts must have: 127.0.0.1 {PLATFORM_FQDN}\n"
            f"Add it with:\n  sudo sh -c 'echo \"127.0.0.1 {PLATFORM_FQDN}\" >> /etc/hosts'"
        )
    print("  /etc/hosts ✓")

    # 3. cert-manager
    r = kubectl("get namespace cert-manager", capture=True, check=False)
    if r.returncode != 0:
        print("  Installing cert-manager...")
        kubectl(
            "apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml",
            timeout=120,
        )
        kubectl("rollout status deployment/cert-manager -n cert-manager --timeout=120s", timeout=130)
    print("  cert-manager ✓")

    # 4. nginx ingress controller
    r = kubectl("get namespace ingress-nginx", capture=True, check=False)
    if r.returncode != 0:
        print("  Installing nginx ingress controller...")
        run("helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx", check=False)
        run("helm repo update ingress-nginx")
        run(
            "helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx "
            "--namespace ingress-nginx --create-namespace "
            "--set controller.hostNetwork=true "
            "--set controller.kind=DaemonSet "
            "--set controller.service.type=ClusterIP "
            "--wait",
            timeout=180,
        )
    kubectl("rollout status daemonset/ingress-nginx-controller -n ingress-nginx --timeout=120s", timeout=130)
    print("  nginx ingress ✓")

    # 5. mittwald kubernetes-secret-generator
    r = kubectl("get namespace secret-generator", capture=True, check=False)
    if r.returncode != 0:
        print("  Installing mittwald secret-generator...")
        run("helm repo add mittwald https://helm.mittwald.de", check=False)
        run("helm repo update mittwald")
        run(
            "helm upgrade --install secret-generator mittwald/kubernetes-secret-generator "
            "--namespace secret-generator --create-namespace --wait",
            timeout=180,
        )
    print("  secret-generator ✓")

    # 6. metrics-server
    r = kubectl("get deployment metrics-server -n kube-system", capture=True, check=False)
    if r.returncode != 0:
        print("  Installing metrics-server...")
        kubectl(
            "apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml",
            timeout=60,
        )
        kubectl("rollout status deployment/metrics-server -n kube-system --timeout=90s", timeout=100)
    print("  metrics-server ✓")

    # 7. CoreDNS /etc/hosts leak fix (OrbStack-specific)
    r = kubectl(
        "get configmap coredns-custom -n kube-system -o jsonpath='{.data}'",
        capture=True, check=False,
    )
    if f"rewrite name {PLATFORM_FQDN}" not in r.stdout:
        print("  Applying CoreDNS /etc/hosts leak fix...")
        run(
            f"kubectl create configmap coredns-custom -n kube-system "
            f"--from-literal=local-hosts.override='rewrite name {PLATFORM_FQDN} "
            "ingress-nginx-controller.ingress-nginx.svc.cluster.local' "
            "--dry-run=client -o yaml | kubectl apply -f -"
        )
        kubectl("rollout restart deployment/coredns -n kube-system")
        kubectl("rollout status deployment/coredns -n kube-system --timeout=60s", timeout=70)
    print("  CoreDNS /etc/hosts leak fix ✓")

    print("\n  Bootstrap complete.")
    print("\n  Ensure /etc/hosts has these entries (add manually if missing):")
    print(f"    127.0.0.1 {PLATFORM_FQDN}")
    print( "    127.0.0.1 overseer-0.fr-platform.iam.orb.local")
    print("\n  Then run: python3 bin/mock-tenant.py deploy")


def cmd_bootstrap(args):
    os.chdir(os.path.join(os.path.dirname(__file__), ".."))
    _step_bootstrap()


def _step_teardown():
    step("teardown", "Delete fr-platform namespace")
    for ns in (NAMESPACE,):
        r = kubectl(f"get namespace {ns}", capture=True, check=False)
        if r.returncode == 0:
            print(f"  Deleting namespace {ns}...")
            kubectl(f"delete namespace {ns} --timeout=120s", timeout=130)
            print(f"  {ns} deleted ✓")
        else:
            print(f"  {ns} not found, skipping ✓")


def cmd_deploy(args):
    _start = time.monotonic()
    os.chdir(os.path.join(os.path.dirname(__file__), ".."))
    r = kubectl(f"get namespace {NAMESPACE}", capture=True, check=False)
    if r.returncode == 0:
        if not args.force:
            raise SystemExit(
                f"Namespace '{NAMESPACE}' already exists.\n"
                f"Use --force to tear it down and redeploy from scratch."
            )
        _step_teardown()
    _step_prerequisites()
    _step_build_images()
    _step_create_namespace()
    _step_deploy_gitea()
    _step_seed_customer_config()
    _step_deploy_esv_shim()
    _step_deploy_ds()
    _step_deploy_keystore()
    _step_deploy_tls()
    _step_deploy_am_idm_uis()
    _step_create_realms()
    _step_create_secret_store()
    _step_amster_and_fix_secret()
    _step_create_tenant_stubs()
    _step_create_pkce_client()
    _step_verify_fbc()
    _step_health_checks()
    # Step 14 — push IDM and AM config to Gitea and restart both pods.
    # force_restart=True is required for IDM: step 11 patches amster-env-secrets with the correct
    # IDM_RS_CLIENT_SECRET / IDM_PROVISIONING_CLIENT_SECRET values, which IDM reads via envFrom at
    # pod startup only — a running pod never sees the updated values without a restart. The restart
    # is forced even when Gitea config was unchanged so the secret patch is always picked up.
    # AM does not need this restart for correctness — realm creation (step 10) and OAuth2 client
    # secret fixes (step 11) are written directly via AM REST and persisted to FBC immediately.
    # AM is restarted here only because both pods take similar time and the step already waits for
    # both; if deploy time ever needs to be optimised, the AM restart could be removed and IDM
    # could be restarted independently after step 11.
    _push_config(target="all", saas_repo_path=None, force_restart=True)
    _step_print_credentials()
    elapsed = time.monotonic() - _start
    mins, secs = divmod(int(elapsed), 60)
    print(f"\n\nDeploy complete ✓  ({mins}m {secs}s)")


# ---------------------------------------------------------------------------
# push-config subcommand
# ---------------------------------------------------------------------------

def _push_idm(clone_dir):
    """Copy IDM static config and script files into the customer-config clone."""
    for _, conf_file in _AIC_IDM_CONF_FILES:
        src = os.path.join(IDM_CONF_STATIC_DIR, conf_file)
        dst = os.path.join(clone_dir, "idm", "conf", conf_file)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    for script_file in _AIC_IDM_SCRIPT_FILES:
        src = os.path.join(IDM_SCRIPT_STATIC_DIR, script_file)
        dst = os.path.join(clone_dir, "idm", "script", script_file)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    run(f"git -C {clone_dir} add idm/")


def _push_am(clone_dir):
    """Sync AM static config files into the customer-config clone (am/services/ subtree)."""
    src_root = AM_CONF_STATIC_DIR
    dst_root = os.path.join(clone_dir, "am", "services")

    # Collect source files
    src_files = set()
    for dirpath, _, filenames in os.walk(src_root):
        for fname in filenames:
            src_files.add(os.path.relpath(os.path.join(dirpath, fname), src_root))

    # Collect existing destination files
    dst_files = set()
    if os.path.isdir(dst_root):
        for dirpath, _, filenames in os.walk(dst_root):
            for fname in filenames:
                dst_files.add(os.path.relpath(os.path.join(dirpath, fname), dst_root))

    # Copy new/updated files
    for rel in src_files:
        src = os.path.join(src_root, rel)
        dst = os.path.join(dst_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    # Remove files no longer in source
    for rel in dst_files - src_files:
        os.remove(os.path.join(dst_root, rel))

    run(f"git -C {clone_dir} add am/")


def _sync_idm_from_saas(saas_overrides_dir):
    """
    Re-merge IDM config from the saas patch files and update the static files
    in IDM_CONF_STATIC_DIR if different. Does not commit — leaves changes on
    disk for the user to review and commit.
    """
    r = kubectl(
        f"get pod -n {NAMESPACE} -l app=idm -o jsonpath='{{.items[0].metadata.name}}'",
        capture=True,
    )
    idm_pod = r.stdout.strip().strip("'")
    merge_script = os.path.join(os.path.dirname(__file__), "gitea-seed.py")
    any_updated = False

    for subcommand, conf_file in _AIC_IDM_CONF_FILES:
        if subcommand is None:
            continue  # not a saas-merge target; managed directly in the repo
        print(f"  Syncing {conf_file} from saas...")
        r = kubectl(
            f"exec {idm_pod} -n {NAMESPACE} -- cat /opt/openidm/conf/{conf_file}",
            capture=True,
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=f"-base-{conf_file}", delete=False) as f:
            f.write(r.stdout)
            base_tmp = f.name

        r = run(
            f"python3 {merge_script} merge {subcommand} {base_tmp} {saas_overrides_dir}/{conf_file}",
            capture=True,
        )
        merged = r.stdout
        os.unlink(base_tmp)

        static_path = os.path.join(IDM_CONF_STATIC_DIR, conf_file)
        with open(static_path) as f:
            current = f.read()

        if merged.strip() != current.strip():
            with open(static_path, "w") as f:
                f.write(merged)
            print(f"  {conf_file} updated on disk ✓")
            any_updated = True
        else:
            print(f"  {conf_file} unchanged")

    if any_updated:
        print(
            "\n  Static IDM config files updated. Review and commit:\n"
            f"    git diff {IDM_CONF_STATIC_DIR}\n"
            f"    git add {IDM_CONF_STATIC_DIR}\n"
            "    git commit -m 'Update merged IDM config from saas'"
        )
    else:
        print("  All static IDM config files already up to date with saas ✓")


def _push_config(target, saas_repo_path, force_restart=False):
    """Core logic for the push-config command, also called by cmd_deploy."""

    if saas_repo_path:
        merge_script = os.path.join(os.path.dirname(__file__), "gitea-seed.py")
        if target in ("idm", "all"):
            saas_overrides_dir = os.path.join(saas_repo_path, _SAAS_IDM_OVERRIDES_SUBPATH)
            if not os.path.isdir(saas_overrides_dir):
                raise SystemExit(
                    f"saas IDM overrides not found at: {saas_overrides_dir}\n"
                    f"Expected: {_SAAS_IDM_OVERRIDES_SUBPATH} within the saas repo."
                )
            print(f"  --saasrepo-path given: syncing IDM static files from {saas_repo_path}")
            _sync_idm_from_saas(saas_overrides_dir)

    targets = ("idm", "am") if target == "all" else (target,)

    with _gitea_portforward():
        for t in targets:
            print(f"\n  --- {t} ---")
            work_fn = _push_idm if t == "idm" else _push_am
            pushed = _gitea_clone_push(work_fn, commit_message=f"Deploy {t} config")
            if pushed:
                print(f"  {t} config pushed to Gitea ✓")
            else:
                print(f"  {t} config unchanged in Gitea ✓")
            if pushed or force_restart:
                kubectl(f"rollout restart deployment/{t} -n {NAMESPACE}")
                kubectl(f"rollout status deployment/{t} -n {NAMESPACE} --timeout=120s", timeout=130)
                print(f"  {t} restarted ✓")


def cmd_push_config(args):
    os.chdir(os.path.join(os.path.dirname(__file__), ".."))
    step("push-config", f"Push config to Gitea (target: {args.target})")
    _push_config(target=args.target, saas_repo_path=args.saasrepo_path)


# ---------------------------------------------------------------------------
# Argument parsing and dispatch
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="mock-tenant.py",
        description="CLI for the ForgeOps mock-tenant dev stack",
    )
    parser.add_argument(
        "--context",
        metavar="CONTEXT",
        default=None,
        help=(
            "Kubernetes context to use (default: orbstack). "
            "Can also be set via MOCK_TENANT_K8S_CONTEXT env var."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # bootstrap
    sub.add_parser("bootstrap", help="Install cluster-wide prerequisites (once per cluster instance)")

    # deploy
    deploy_p = sub.add_parser("deploy", help="Deploy the application stack (AM, IDM, DS, Gitea, ESV shim) — run bootstrap first")
    deploy_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Tear down existing deployment before deploying (required if namespace already exists)",
    )

    # push-config
    pc = sub.add_parser(
        "push-config",
        help="Push config to Gitea and restart pod(s)",
    )
    pc.add_argument(
        "--target",
        choices=["idm", "am", "all"],
        default="idm",
        help="Which component to push config for (default: idm)",
    )
    pc.add_argument(
        "--saasrepo-path",
        metavar="PATH",
        default=None,
        help=(
            "Path to the saas repo root. Re-merges IDM config from the saas patch "
            f"files and updates {IDM_CONF_STATIC_DIR} if different "
            "(does not auto-commit). No effect when --target=am."
        ),
    )

    args = parser.parse_args()

    # Allow --context to override the module-level default
    if args.context:
        global K8S_CONTEXT
        K8S_CONTEXT = args.context

    if args.command == "bootstrap":
        cmd_bootstrap(args)
    elif args.command == "deploy":
        cmd_deploy(args)
    elif args.command == "push-config":
        cmd_push_config(args)


if __name__ == "__main__":
    main()
