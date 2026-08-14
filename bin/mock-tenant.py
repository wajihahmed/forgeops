#!/usr/bin/env python3
"""
mock-tenant.py — CLI for the ForgeOps mock-tenant dev stack.

Commands:
  bootstrap                 Install cluster-wide prerequisites (once per cluster instance)
  deploy                    Deploy the application stack (AM, IDM, DS, Gitea, tenant shim) — requires bootstrap first
  push-config               Push static config files to Gitea and restart pod(s)
  sync-saas                 Merge saas repo patches into local static config files (does not commit)
  seed-gitea merge <managed|repo-ds|access> <base> <patch>
                            Merge IDM config patch files into a base JSON file (stdout)
  seed-gitea am-mirror      Mirror root realm tree/node config from a live AM pod into am-conf/

Usage:
  python3 bin/mock-tenant.py [--context CONTEXT] bootstrap
  python3 bin/mock-tenant.py [--context CONTEXT] deploy [--force]
  python3 bin/mock-tenant.py [--context CONTEXT] push-config [--target idm|am|all]
  python3 bin/mock-tenant.py sync-saas --repo-path PATH [--pod POD] [--target idm|am|usr|cts|all]
  python3 bin/mock-tenant.py seed-gitea merge <managed|repo-ds|access> <base> <patch>
  python3 bin/mock-tenant.py seed-gitea am-mirror [--namespace fr-platform] [--am-conf kustomize/base/gitea-seed/am-conf]

--context defaults to "orbstack". Pass --context minikube (or set MOCK_TENANT_K8S_CONTEXT)
for other local Kubernetes runtimes.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote


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
# deploy subcommand — application deployment (AM, IDM, DS, Gitea, tenant shim)
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
    step(1, "Build config-loader, tenant-shim, and ds images")

    r = run("docker context ls --format '{{.Name}} {{.Current}}'", capture=True)
    active = next((l.split()[0] for l in r.stdout.splitlines() if "true" in l.lower()), "")
    ctx = f"docker --context {K8S_CONTEXT}" if active != K8S_CONTEXT else "docker"

    for tag, path, dockerfile in [
        ("config-loader:local",  "docker/config-loader/", None),
        ("tenant-shim:local",    "docker/tenant-shim/",   None),
        ("ds:local-base",        "docker/ds/",            None),
        ("ds:local",             "docker/ds/",            "Dockerfile.mock-tenant"),
    ]:
        print(f"  Building {tag}...")
        df_flag = f"-f {path}{dockerfile} " if dockerfile else ""
        run(f"{ctx} build {df_flag}-t {tag} {path}", timeout=300)

    run(f"{ctx} images --format 'table {{{{.Repository}}}}\\t{{{{.Tag}}}}' | grep -E 'config-loader|tenant-shim|^ds'")


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


def _step_deploy_tenant_shim():
    step(5, "Deploy tenant shim")
    kubectl("apply -k kustomize/overlay/mock-tenant/tenant-shim/")
    kubectl(f"rollout status deployment/tenant-shim -n {NAMESPACE} --timeout=60s", timeout=70)


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
    _step_deploy_tenant_shim()
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
    _push_config(target="all", force_restart=True)
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


def _sync_idm_from_saas(saas_overrides_dir, pod=None):
    """
    Merge IDM config from saas patch files into the static files in IDM_CONF_STATIC_DIR.

    If pod is given, the base JSON is read from that live IDM pod via kubectl exec.
    Otherwise the already-committed static files in IDM_CONF_STATIC_DIR are used as
    the base (no cluster required).

    Does not commit — leaves changes on disk for the user to review and commit.
    """
    _merge_fns = {"managed": merge_managed, "repo-ds": merge_repo_ds, "access": merge_access}
    any_updated = False

    for subcommand, conf_file in _AIC_IDM_CONF_FILES:
        if subcommand is None:
            continue  # not a saas-merge target; managed directly in the repo
        print(f"  Syncing {conf_file} from saas...")

        if pod:
            r = kubectl(
                f"exec {pod} -n {NAMESPACE} -- cat /opt/openidm/conf/{conf_file}",
                capture=True,
            )
            base = json.loads(r.stdout)
            print(f"    base: live pod {pod}")
        else:
            static_path = os.path.join(IDM_CONF_STATIC_DIR, conf_file)
            with open(static_path) as f:
                base = json.load(f)
            print(f"    base: static file {static_path}")

        with open(os.path.join(saas_overrides_dir, conf_file)) as f:
            patches = json.load(f)

        result = _merge_fns[subcommand](base, patches)
        merged = json.dumps(result, indent=4)

        static_path = os.path.join(IDM_CONF_STATIC_DIR, conf_file)
        with open(static_path) as f:
            current = f.read()

        if merged.strip() != current.strip():
            with open(static_path, "w") as f:
                f.write(merged)
            print(f"    {conf_file} updated on disk ✓")
            any_updated = True
        else:
            print(f"    {conf_file} unchanged")

    if any_updated:
        print(
            "\n  Static IDM config files updated. Review and commit:\n"
            f"    git diff {IDM_CONF_STATIC_DIR}\n"
            f"    git add {IDM_CONF_STATIC_DIR}\n"
            "    git commit -m 'Update merged IDM config from saas'"
        )
    else:
        print("  All static IDM config files already up to date with saas ✓")


def _sync_am_from_saas(_saas_repo_path, _pod=None):
    # TODO: implement automated AM config sync from saas repo.
    #
    # Manual steps (currently done by hand):
    #   1. Review services/am/config/aic-overrides/ in the saas repo for any new or changed
    #      service/realm config files that should be reflected in the alpha/bravo realm FBC.
    #   2. Diff saas services/am/config/aic-overrides/services/realm/ against
    #      kustomize/base/gitea-seed/am-conf/realm/root-alpha/ and root-bravo/.
    #   3. Copy changed files into the appropriate FBC realm directory, then run
    #      `python3 bin/mock-tenant.py push-config --target am` to push to Gitea.
    #
    # Complexity: AM FBC uses a realm-per-directory layout. Saas aic-overrides are realm-agnostic
    # templates applied to all realms, so each file must be fanned out to root-alpha/ and root-bravo/.
    # Some saas files reference ESV variables that must be replaced with local equivalents.
    raise NotImplementedError("sync-saas --target am is not yet implemented")


def _sync_usr_from_saas(_saas_repo_path, _pod=None):
    # TODO: implement automated DS userstore (ds-idrepo) sync from saas repo.
    #
    # Manual steps (currently done by hand when saas changes):
    #   1. Schema (trivial — verbatim copy):
    #      cp <saas>/services/userstore/setup-profiles/FRAAS/repo/7.0/schema/99-fraas-schema.ldif \
    #         docker/ds/config/schema-mock-tenant/99-fraas-schema.ldif
    #      Rebuild the ds:local Docker image after copying.
    #
    #   2. Indexes / VLV / plugins (medium effort):
    #      Diff <saas>/services/userstore/configuration/dsconfig-input against the dsconfig
    #      --batch block in docker/ds/runtime-scripts-mock-tenant/ds-idrepo/setup.
    #      Update the setup script with any new/changed create-backend-index, create-vlv-index,
    #      create-plugin, or set-backend-prop commands.
    #      Exclude ACL changes, OpenTelemetry plugin, and any commands that reference ESV variables.
    #      Rebuild the ds:local Docker image and wipe the PVC so setup re-runs on next pod start.
    #
    # Complexity: saas dsconfig-input is a flat 1031-line batch file; mock-tenant splits it across
    # saas-compat-config.sh (build-time, backend-independent settings) and runtime-scripts (first-boot,
    # after setup-profiles create the amIdentityStore/idmRepo/cfgStore backends). Any automated sync
    # must partition new commands into the correct file.
    raise NotImplementedError("sync-saas --target usr is not yet implemented")


def _sync_cts_from_saas(_saas_repo_path, _pod=None):
    # TODO: implement automated DS CTS store (ds-cts) sync from saas repo.
    #
    # Manual steps (currently done by hand when saas changes):
    #   Diff <saas>/services/ctsstore/configuration/dsconfig-input against the dsconfig
    #   --batch block in docker/ds/runtime-scripts-mock-tenant/ds-cts/setup.
    #   Update the setup script with any new/changed set-backend-prop or index commands.
    #   Rebuild the ds:local Docker image and wipe the CTS PVC so setup re-runs.
    #
    # Complexity: low — ctsstore/dsconfig-input is small (db-durability and a handful of settings).
    raise NotImplementedError("sync-saas --target cts is not yet implemented")


def cmd_sync_saas(args):
    os.chdir(os.path.join(os.path.dirname(__file__), ".."))
    step("sync-saas", f"Sync config from saas repo (target: {args.target})")

    saas_overrides_dir = os.path.join(args.repo_path, _SAAS_IDM_OVERRIDES_SUBPATH)
    if not os.path.isdir(saas_overrides_dir):
        raise SystemExit(
            f"saas IDM overrides not found at: {saas_overrides_dir}\n"
            f"Expected: {_SAAS_IDM_OVERRIDES_SUBPATH} within the saas repo."
        )

    if args.target in ("idm", "all"):
        _sync_idm_from_saas(saas_overrides_dir, pod=args.pod)
    if args.target in ("am", "all"):
        _sync_am_from_saas(args.repo_path, args.pod)
    if args.target in ("usr", "all"):
        _sync_usr_from_saas(args.repo_path, args.pod)
    if args.target in ("cts", "all"):
        _sync_cts_from_saas(args.repo_path, args.pod)


def _push_config(target, force_restart=False):
    """Core logic for the push-config command, also called by cmd_deploy."""
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
    _push_config(target=args.target)


# ---------------------------------------------------------------------------
# seed subcommand — IDM config merging and AM config mirroring
# ---------------------------------------------------------------------------

# ── managed.json helpers ──────────────────────────────────────────────────────

def _managed_extract_name(field):
    m = re.search(r'/objects\[/name eq "([^"]+)"\]', field)
    if not m:
        raise ValueError(f"Cannot parse filter field: {field!r}")
    return m.group(1)


def merge_managed(base, patches):
    objects = base.get("objects", [])

    for op in patches:
        operation = op["operation"]
        field = op["field"]

        if operation == "remove":
            name = _managed_extract_name(field)
            before = len(objects)
            objects = [o for o in objects if o.get("name") != name]
            if len(objects) == before:
                print(f"  skip remove: '{name}' not in base (ok)", file=sys.stderr)
            else:
                print(f"  removed: '{name}'", file=sys.stderr)

        elif operation == "add" and field == "/objects/-":
            value = op["value"]
            name = value.get("name", "<unnamed>")
            if any(o.get("name") == name for o in objects):
                print(f"  skip add: '{name}' already present (idempotent)", file=sys.stderr)
            else:
                objects.append(value)
                print(f"  added: '{name}'", file=sys.stderr)

        else:
            print(f"  WARNING: unsupported op '{operation}' on '{field}' — skipped",
                  file=sys.stderr)

    base["objects"] = objects
    return base


# ── repo.ds.json helpers ──────────────────────────────────────────────────────

_REPO_DS_SKIP_PREFIXES = {"/ldapConnectionFactories"}


def _parse_path(field):
    """'/a/b%2Fc/d' -> ['a', 'b/c', 'd']"""
    parts = field.lstrip("/").split("/")
    return [unquote(p) for p in parts]


def _set_path(doc, parts, value):
    node = doc
    for key in parts[:-1]:
        if key not in node or not isinstance(node[key], dict):
            node[key] = {}
        node = node[key]
    node[parts[-1]] = value


def merge_repo_ds(base, patches):
    for op in patches:
        operation = op["operation"]
        field = op["field"]

        top = "/" + field.lstrip("/").split("/")[0]
        if top in _REPO_DS_SKIP_PREFIXES:
            print(f"  skip (connection config): {field}", file=sys.stderr)
            continue

        if operation in ("replace", "add"):
            parts = _parse_path(field)
            _set_path(base, parts, op["value"])
            print(f"  {operation}: {field}", file=sys.stderr)
        else:
            print(f"  WARNING: unsupported op '{operation}' on '{field}' — skipped",
                  file=sys.stderr)

    return base


# ── access.json helpers ───────────────────────────────────────────────────────

def _parse_array_filter(field):
    m = re.match(r'/?(\w+)\[(.+)\]$', field.strip())
    if m:
        return m.group(1), m.group(2)
    key = field.lstrip("/").split("[")[0].rstrip("/-")
    return key, None


def _matches_filter(item, filter_str):
    """Evaluate an IDM array-filter expression (eq, co, and) against a dict."""
    conditions = re.split(r'\s+and\s+', filter_str, flags=re.IGNORECASE)
    for cond in conditions:
        cond = cond.strip()
        m = re.match(r'/(\S+)\s+(eq|co)\s+"([^"]*)"', cond)
        if not m:
            print(f"  WARNING: unparseable filter condition: {cond!r}", file=sys.stderr)
            return False
        field, operator, value = m.group(1), m.group(2), m.group(3)
        item_val = str(item.get(field, ""))
        if operator == "eq" and item_val != value:
            return False
        if operator == "co" and value not in item_val:
            return False
    return True


def merge_access(base, patches):
    configs = base.get("configs", [])

    for op in patches:
        operation = op["operation"]
        field = op.get("field", "")

        if operation == "remove":
            array_key, filter_str = _parse_array_filter(field)
            if array_key != "configs" or not filter_str:
                print(f"  WARNING: unsupported remove target '{field}' — skipped",
                      file=sys.stderr)
                continue
            before = len(configs)
            configs = [c for c in configs if not _matches_filter(c, filter_str)]
            removed = before - len(configs)
            print(f"  remove {removed} entry(s) matching: {filter_str}", file=sys.stderr)

        elif operation == "replace":
            array_key, filter_str = _parse_array_filter(field)
            if array_key != "configs" or not filter_str:
                print(f"  WARNING: unsupported replace target '{field}' — skipped",
                      file=sys.stderr)
                continue
            replaced = False
            for i, item in enumerate(configs):
                if _matches_filter(item, filter_str):
                    configs[i] = op["value"]
                    replaced = True
                    break
            if replaced:
                print(f"  replaced entry matching: {filter_str}", file=sys.stderr)
            else:
                print(f"  skip replace: no entry matched: {filter_str}", file=sys.stderr)

        elif operation == "append":
            elements = op.get("elements", [])
            added = 0
            for elem in elements:
                if elem in configs:
                    print(f"  skip append: entry already present (idempotent): "
                          f"{json.dumps(elem, separators=(',', ':'))[:80]}",
                          file=sys.stderr)
                else:
                    configs.append(elem)
                    added += 1
            if added:
                print(f"  appended {added} new entries ({len(elements)-added} already present)",
                      file=sys.stderr)

        else:
            print(f"  WARNING: unsupported op '{operation}' on '{field}' — skipped",
                  file=sys.stderr)

    base["configs"] = configs
    return base


# ── am-mirror helpers ─────────────────────────────────────────────────────────

_AM_MIRROR_SAFE_DIRS = {
    "authenticationtreesservice",
    "pagenode",
    "validatedusernamenode",
    "validatedpasswordnode",
    "identitystoredecisionnode",
    "datastoredecisionnode",
    "incrementlogincountnode",
    "innertreeevaluatornode",
    "retrylimitdecisionnode",
    "accountlockoutnode",
    "logincountdecisionnode",
    "queryfilterdecisionnode",
    "patchobjectnode",
    "attributecollectornode",
    "sunidentityrepositoryservice",
    "oauth2provider",
    "idmintegrationservice",
    "amrealmbaseurl",
    "selfservicetrees",
    "socialidentityproviders",
    "validationservice",
}

_AM_MIRROR_REALMS = ("alpha", "bravo")
_AM_ROOT_UID_SUFFIX = "ou=am-config"


def _am_mirror_get_pod(namespace):
    r = subprocess.run(
        ["kubectl", "get", "pod", "-n", namespace, "-l", "app=am",
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _am_mirror_copy_root(am_pod, namespace, tmpdir):
    root_path = "/home/forgerock/openam/config/services/realm/root"
    dst = os.path.join(tmpdir, "root")
    subprocess.run(
        ["kubectl", "cp", "-n", namespace, f"{am_pod}:{root_path}", dst],
        check=True,
    )
    return dst


def _am_mirror_patch_idrepo(data, realm):
    base_dn = f"o={realm},o=root,ou=identities"
    for section in data.values():
        if not isinstance(section, dict):
            continue
        if "sun-idrepo-ldapv3-config-organization_name" in section:
            section["sun-idrepo-ldapv3-config-organization_name"] = base_dn
        if "sun-idrepo-ldapv3-config-psearchbase" in section:
            section["sun-idrepo-ldapv3-config-psearchbase"] = base_dn
        if "sun-idrepo-ldapv3-config-people-container-value" in section:
            section["sun-idrepo-ldapv3-config-people-container-value"] = "user"


def _am_mirror_realm(root_dir, realm, am_conf_dir):
    dst_realm = os.path.join(am_conf_dir, "realm", f"root-{realm}")
    realm_suffix = f"o={realm},ou=services,ou=am-config"
    written = 0

    for dirpath, _, filenames in os.walk(root_dir):
        rel_dir = os.path.relpath(dirpath, root_dir)
        parts = rel_dir.split(os.sep)
        top = parts[0]
        if top not in _AM_MIRROR_SAFE_DIRS:
            continue

        for fname in filenames:
            if not fname.endswith(".json"):
                continue
            if fname == "default.json" and parts[-1] == "organizationconfig":
                continue

            src = os.path.join(dirpath, fname)
            dst = os.path.join(dst_realm, rel_dir, fname)
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            with open(src) as f:
                doc = json.load(f)

            raw = json.dumps(doc)
            raw = raw.replace('"realm" : "/"', f'"realm" : "/{realm}"')
            raw = raw.replace('"realm": "/"', f'"realm": "/{realm}"')
            raw = raw.replace(_AM_ROOT_UID_SUFFIX, realm_suffix)
            doc = json.loads(raw)

            if (top == "sunidentityrepositoryservice"
                    and fname == "opendj.json"
                    and isinstance(doc.get("data"), dict)):
                _am_mirror_patch_idrepo(doc["data"], realm)

            _IDM_NODES = {
                "IncrementLoginCountNode", "LoginCountDecisionNode",
                "PatchObjectNode", "QueryFilterDecisionNode",
            }
            if (top == "authenticationtreesservice"
                    and doc.get("data", {}).get("nodes") is not None
                    and "identityResource" not in doc["data"]):
                nodes = doc["data"]["nodes"]
                if any(n.get("nodeType") in _IDM_NODES for n in nodes.values()):
                    doc["data"]["identityResource"] = f"managed/{realm}_user"

            if (top in ("patchobjectnode", "queryfilterdecisionnode",
                        "incrementlogincountnode", "logincountdecisionnode")
                    and isinstance(doc.get("data"), dict)
                    and doc["data"].get("identityResource") == "managed/user"):
                doc["data"]["identityResource"] = f"managed/{realm}_user"

            with open(dst, "w") as f:
                json.dump(doc, f, indent=2)
            written += 1

    return written


def _am_mirror_service_singletons(root_dir, realm, am_conf_dir):
    realm_suffix = f"o={realm},ou=services,ou=am-config"
    written = 0

    for svc in _AM_MIRROR_SAFE_DIRS:
        src = os.path.join(root_dir, svc, "1.0", "organizationconfig", "default.json")
        if not os.path.exists(src):
            continue

        with open(src) as f:
            content = f.read()

        content = content.replace('"realm" : "/"', f'"realm" : "/{realm}"')
        content = content.replace('"realm": "/"', f'"realm": "/{realm}"')
        content = content.replace(_AM_ROOT_UID_SUFFIX, realm_suffix)

        dst_dir = os.path.join(am_conf_dir, "realm", f"root-{realm}",
                               svc, "1.0", "organizationconfig")
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "default.json")

        with open(dst, "w") as f:
            f.write(content)
        written += 1

    return written


def cmd_seed(args):
    os.chdir(os.path.join(os.path.dirname(__file__), ".."))

    if args.seed_command == "am-mirror":
        print(f"Getting AM pod in namespace {args.namespace}...")
        am_pod = _am_mirror_get_pod(args.namespace)
        print(f"AM pod: {am_pod}")

        with tempfile.TemporaryDirectory() as tmpdir:
            print("Copying root realm config from AM pod...")
            root_dir = _am_mirror_copy_root(am_pod, args.namespace, tmpdir)

            for realm in _AM_MIRROR_REALMS:
                print(f"\nMirroring root → {realm}...")
                n = _am_mirror_realm(root_dir, realm, args.am_conf)
                s = _am_mirror_service_singletons(root_dir, realm, args.am_conf)
                print(f"  {n} instance files + {s} service singletons written")

        print("\nDone. Push to Gitea with:")
        print("  python3 bin/mock-tenant.py push-config --target am")

    elif args.seed_command == "merge":
        subcommand = args.merge_type
        with open(args.base) as f:
            base = json.load(f)
        with open(args.patch) as f:
            patches = json.load(f)

        print(f"Subcommand: {subcommand}", file=sys.stderr)
        print(f"Patch operations: {len(patches)}", file=sys.stderr)

        if subcommand == "managed":
            print(f"Base objects: {len(base.get('objects', []))}", file=sys.stderr)
            result = merge_managed(base, patches)
            print(f"Result objects: {len(result.get('objects', []))}", file=sys.stderr)
        elif subcommand == "repo-ds":
            result = merge_repo_ds(base, patches)
        else:
            print(f"Base configs: {len(base.get('configs', []))}", file=sys.stderr)
            result = merge_access(base, patches)
            print(f"Result configs: {len(result.get('configs', []))}", file=sys.stderr)

        print(json.dumps(result, indent=4))


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
    deploy_p = sub.add_parser("deploy", help="Deploy the application stack (AM, IDM, DS, Gitea, tenant shim) — run bootstrap first")
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
    # sync-saas
    ss = sub.add_parser(
        "sync-saas",
        help="Merge saas repo patches into local static config files (does not commit)",
    )
    ss.add_argument(
        "--repo-path",
        metavar="PATH",
        required=True,
        help="Path to the saas repo root",
    )
    ss.add_argument(
        "--pod",
        metavar="POD",
        default=None,
        help=(
            "Name of a live IDM pod to use as the merge base. "
            "If omitted, the committed static files are used as the base (no cluster required)."
        ),
    )
    ss.add_argument(
        "--target",
        choices=["idm", "am", "usr", "cts", "all"],
        default="idm",
        help="Which component to sync (default: idm). usr=userstore, cts=CTS store",
    )

    # seed-gitea
    seed_p = sub.add_parser(
        "seed-gitea",
        help="Utilities for preparing Gitea seed repo content (IDM merge and AM mirror)",
    )
    seed_sub = seed_p.add_subparsers(dest="seed_command", required=True)

    # seed merge
    merge_p = seed_sub.add_parser(
        "merge",
        help="Merge IDM config patch file into a base JSON file (output to stdout)",
    )
    merge_p.add_argument(
        "merge_type",
        choices=["managed", "repo-ds", "access"],
        help="IDM config file type to merge",
    )
    merge_p.add_argument("base", help="Path to base JSON file")
    merge_p.add_argument("patch", help="Path to patch JSON file")

    # seed am-mirror
    mirror_p = seed_sub.add_parser(
        "am-mirror",
        help="Mirror root realm tree/node config from a live AM pod into am-conf/",
    )
    mirror_p.add_argument("--namespace", default="fr-platform")
    mirror_p.add_argument(
        "--am-conf",
        default="kustomize/base/gitea-seed/am-conf",
        metavar="DIR",
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
    elif args.command == "sync-saas":
        cmd_sync_saas(args)
    elif args.command == "seed-gitea":
        cmd_seed(args)


if __name__ == "__main__":
    main()
