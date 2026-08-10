# Mock Tenant — User, Developer & Design Guide

This document is the single authoritative reference for the **ForgeOps FBC (File Based Configuration) Dev Stack** — a local Kubernetes deployment of AM, IDM, and DS on OrbStack that closely mirrors a real AIC (Ping Identity Cloud) tenant. It covers architecture, prerequisites, deployment, all ForgeOps modifications, operational procedures, and known issues.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Kubernetes Runtime — OrbStack](#kubernetes-runtime--orbstack)
4. [Prerequisites](#prerequisites)
5. [Deploy Guide](#deploy-guide)
6. [Push Config & AM Mirror](#push-config--am-mirror)
7. [Implementation Section](#implementation-section)
8. [AM Tree Config for Alpha/Bravo Realms](#am-tree-config-for-alphabravo-realms)
9. [SaaS Sync — Planned Work](#saas-sync--planned-work)
10. [Tenant Shim](#tenant-shim)
11. [Operational Runbook](#operational-runbook)
12. [Known Issues & Gotchas](#known-issues--gotchas)
13. [TODO](#todo)

---

## Overview

This is a fork of [ForgeOps](https://github.com/ForgeRock/forgeops) — the open-source Ping Identity deployment toolkit — extended with:

- **File Based Configuration (FBC)**: AM and IDM load their configuration from a Gitea git repository at pod startup, not from config baked into the Docker image.
- **Alpha/bravo realms**: Pre-configured via FBC with Login trees, identity stores, and OAuth2 clients that match AIC's multi-realm model.
- **Tenant shim**: A FastAPI service that emulates AIC's Environment Secrets & Variables REST API and other AIC-specific endpoints, enabling unmodified lodestar tooling to configure the local tenant.
- **SaaS-compatible DS**: Custom schema, indexes, and security settings sourced from the production saas repo.
- **`mock-tenant.py`**: A single automation script (`bin/mock-tenant.py`) with three subcommands: `bootstrap` installs cluster-wide prerequisites (once per OrbStack instance); `deploy` deploys the application stack (AM, IDM, DS, Gitea, tenant shim); `push-config` pushes updated config to Gitea and restarts the relevant pod.
- **`gitea-seed.py`**: A utility script (`bin/gitea-seed.py`) with two roles: (1) `am-mirror` — mirrors the live AM root realm's tree/node config into Gitea-ready FBC files for the alpha/bravo realms; (2) `merge <managed|repo-ds|access>` subcommands — merges IDM config files from the saas repo into the ForgeOps-compatible static files committed under `kustomize/base/gitea-seed/idm-conf/`.
- **`tunnel`**: A helper script (`bin/tunnel`) that port-forwards the nginx ingress controller's port 443 to localhost:443 (requires `sudo`), enabling browser access to `https://mock.iam.example.com` from your laptop/desktop.

**Why ForgeOps instead of the saas repo:**
The production `ForgeCloud/saas` monorepo (`/Users/wajih.ahmed/source/github.com/ForgeCloud/saas`) is deeply coupled to GCP (GKE Workload Identity, Google Secret Manager, Google Cloud Source Repositories, GCP-specific StorageClasses). It cannot be deployed to a non-GKE cluster without significant rework. ForgeOps is the public open-source base — no GCP coupling, plain Kubernetes Secrets, standard StorageClasses — and already uses Kustomize with the same products (AM, IDM, DS) and the same image base.

**All changes are isolated in `kustomize/overlay/mock-tenant/` and new supporting files.** No upstream ForgeOps files are modified — the base and `overlay/default` directories are identical to master. The branch can be rebased onto a newer ForgeOps master without conflicts.

**Branch:** `wajih-mock-tenant`

---

## Architecture

### FBC: What It Is and Why It Matters

**File Based Configuration (FBC)** means AM and IDM configuration is stored as JSON files in a git repository, not in a database or baked into the Docker image. At pod startup, an init container clones the git repo and copies the relevant config files into a shared volume before the main container starts.

Benefits:
- Config changes are version-controlled in git
- Config can be updated without rebuilding Docker images (commit to git, restart pod)
- Multiple environments can share the same image with different config repos/branches

### Init Container Chain

ForgeOps has an existing init container chain for AM and IDM. The `mock-tenant` overlay patches the `custom-vol-init` container (already present in the base) to use the `config-loader:local` image with the `clone-and-copy` entrypoint:

```
custom-vol-init  (config-loader:local image — patched by mock-tenant overlay)
  AM:  git clone gitea/customer-config → /custom/config/services
  IDM: git clone gitea/customer-config → /custom/config
  → prints "config-loader done" on success

filesystem-init  (am|idm image — UNCHANGED)
  → if /custom/config exists: cp image defaults + overlay /custom/config → /fbc
  → else: cp image defaults → /fbc

truststore-init  (am|idm image — UNCHANGED)
  → import PEM certs → /new-truststore

Main container
  → reads config from /fbc  (AM: /home/forgerock/openam)  (IDM: /fbc/conf, /fbc/ui, /fbc/script)
  → CATALINA_USER_OPTS passes -Dam.server.fqdn=mock.iam.example.com to JVM (AM only)
```

**The base image split:** ForgeOps bakes image-default config in two separate filesystem locations:
- `/home/forgerock/base/config/services` — image-baked root realm config (including `iPlanetAMAuthService`)
- `/home/forgerock/openam/config/services` — FBC overlay path populated by `filesystem-init` from Gitea

The FBC importer must scan both paths. The override is in `kustomize/overlay/mock-tenant/am/deployment.yaml`. See [FBC_BASE_PATHS Two-Path Requirement](#fbc_base_paths-two-path-requirement).

### Gitea In-Cluster Git Server

A single Gitea pod serves `http://gitea.fr-platform.svc.cluster.local:3000`. The repo `forgerock/customer-config` holds AM and IDM config files, seeded by the `gitea-seed` Job.

### REST API vs Gitea FBC — Two Complementary Mechanisms

For any AM service configuration (secret stores, realms, httpclient instances, etc.) there are two ways to get config into a running AM pod:

1. **REST API at deploy time** — `mock-tenant.py deploy` creates the configuration by calling AM's REST API (e.g. step 10 creates realms, step 10b creates `FileSystemSecretStore/ESV`). This is the right tool for the initial creation of config that doesn't exist in the base image.

2. **Gitea FBC** — the corresponding JSON file is committed under `kustomize/base/gitea-seed/am-conf/` and pushed to Gitea. `custom-vol-init` re-applies the entire Gitea tree on every pod start, so the config comes back automatically after any AM restart.

**Neither mechanism alone is sufficient:**

- REST only → config exists after `deploy`, but disappears on any bare AM pod restart (crash, node eviction, `kubectl rollout restart`) because `filesystem-init` repopulates `/fbc` from Gitea, which doesn't have the change.
- Gitea FBC only → config is present after a restart, but not on a fresh deploy (AM must already be running for REST to work, and the Gitea seed Job runs before AM is ready).

**The correct pattern for any persistent AM service config is both:** create via REST in the deploy step *and* commit the equivalent FBC JSON to `kustomize/base/gitea-seed/am-conf/` so it survives restarts. The same principle applies to IDM config committed under `kustomize/base/gitea-seed/idm-conf/`.

### Customer-Config Repo Structure

```
Repo path                     →  Pod path (AM)
am/                           →  /home/forgerock/openam/config/services/
am/realm/root-alpha/...       →  /home/forgerock/openam/config/services/realm/root-alpha/...

Repo path                     →  Pod path (IDM)
idm/                          →  /home/forgerock/openam/config/  (JSON_REPLACE strategy)
```

The translation chain for AM config:
```
Gitea: am/  (DESTINATION_PATH = /custom/config/services)
         ↓ custom-vol-init copies to /custom/config/services/
         ↓ filesystem-init copies /custom/config → /fbc/config
       /fbc/config/services/
         ↓ fbc volume mounted at /home/forgerock/openam
       /home/forgerock/openam/config/services/
```

**Important:** `am/` in the repo maps directly to `config/services/` in the pod — do NOT add a `config/` subdirectory in the repo.

### Component Map

```
OrbStack (local k8s)
└── fr-platform namespace
    ├── am              — ForgeOps AM with FBC init container
    ├── idm             — ForgeOps IDM with FBC init container
    ├── ds-idrepo       — DS (identity + IDM repo store), customised schema/indexes
    ├── ds-cts          — DS (CTS session store)
    ├── gitea           — In-cluster git server (customer-config repo)
    ├── tenant-shim     — AIC ESV API emulator + stub endpoints (FastAPI)
    ├── admin-ui        — ForgeRock Admin UI
    ├── login-ui        — Platform Login UI
    ├── end-user-ui     — End User UI
    └── amster          — One-shot Job that bootstraps AM OAuth2 clients
```

---

## Kubernetes Runtime — OrbStack

**OrbStack** is used as the local Kubernetes runtime. It provides a stable Kubernetes API on `127.0.0.1:26443` with no SSH tunnel complications.

- Start: macOS menu bar or `open -a OrbStack`
- Kubernetes context: `orbstack`
- Switch to it: `kubectl config use-context orbstack`

**CrowdStrike note:** The OrbStack node IP (`192.168.139.2`) is blocked by CrowdStrike on corporate laptops. The Kubernetes API and `kubectl port-forward` both run over loopback (`127.0.0.1`), which is unaffected.

**Do not use Colima** — abandoned due to SSH tunnel instability and socket_vmnet entitlement issues on a corporate laptop.

### Browser access

`bin/tunnel` port-forwards nginx ingress port 443 to localhost:443 (requires `sudo`):
```sh
bin/tunnel        # start
bin/tunnel stop   # stop
```

Requires `/etc/hosts` to have `127.0.0.1 mock.iam.example.com`.

URLs:
- AM: `https://mock.iam.example.com/am`
- IDM: `https://mock.iam.example.com/openidm`
- Admin UI: `https://mock.iam.example.com/platform`

The cert is self-signed — accept the browser warning.

The IDM Admin UI at `/admin` is not available — it was deprecated and removed from ForgeOps. Use `/platform` instead.

---

## Prerequisites

These are cluster-wide installs done once per OrbStack instance. Run the `bootstrap` subcommand on a fresh OrbStack cluster before the first `deploy`:

```sh
python3 bin/mock-tenant.py bootstrap
```

`bootstrap` is idempotent — it skips any component already present. `deploy`'s step 0 checks that these are present and aborts if any are missing (except the CoreDNS fix, which both `bootstrap` and `deploy` apply automatically).

The steps below document what `bootstrap` does and serve as a manual recovery reference.

### 1. cert-manager
```sh
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml
kubectl rollout status deployment/cert-manager -n cert-manager --timeout=120s
```

### 2. nginx ingress controller
`hostNetwork=true` is required — no cloud load balancer on a local cluster.
```sh
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update ingress-nginx
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.hostNetwork=true \
  --set controller.kind=DaemonSet \
  --set controller.service.type=ClusterIP \
  --wait
```

After installing, enable `configuration-snippet` annotations so the AM/IDM Ingresses can inject the `x-forgerock-transactionid` header:
```sh
kubectl patch configmap ingress-nginx-controller -n ingress-nginx \
  --type merge \
  -p '{"data":{"allow-snippet-annotations":"true","annotations-risk-level":"Critical"}}'
kubectl rollout restart daemonset/ingress-nginx-controller -n ingress-nginx
kubectl rollout status daemonset/ingress-nginx-controller -n ingress-nginx --timeout=90s
```
`annotations-risk-level=Critical` is required in ingress-nginx v1.12+ — without it the admission webhook rejects `configuration-snippet` even when `allow-snippet-annotations` is true.

### 3. mittwald kubernetes-secret-generator
**Must be running before DS is deployed.** DS reads the `ds-passwords` Secret during first-init to set its admin password. If this operator is absent, the password is empty and cannot be changed without wiping PVCs.
```sh
helm repo add mittwald https://helm.mittwald.de
helm repo update
helm upgrade --install secret-generator mittwald/kubernetes-secret-generator \
  --namespace secret-generator --create-namespace --wait
```

### 4. metrics-server
Needed for `kubectl top nodes`/`kubectl top pods` (useful for diagnosing OOMKill under load tests). Works out of the box on OrbStack — no `--kubelet-insecure-tls` patch needed.
```sh
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl rollout status deployment/metrics-server -n kube-system --timeout=90s
```

### 5. CoreDNS /etc/hosts leak fix (OrbStack-specific)
OrbStack syncs the Mac's `/etc/hosts` into CoreDNS. This makes `127.0.0.1 mock.iam.example.com` (required for laptop browser access) also resolve inside pods — breaking pod-to-pod and pod-to-self calls to the FQDN (AM's own configured server URL). A second `hosts` block cannot be added (CoreDNS limitation); use `rewrite` via the `coredns-custom` ConfigMap:
```sh
kubectl create configmap coredns-custom -n kube-system \
  --from-literal=local-hosts.override='rewrite name mock.iam.example.com ingress-nginx-controller.ingress-nginx.svc.cluster.local' \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/coredns -n kube-system
```
This fix is OrbStack-specific. minikube/kind/Colima don't sync the host's `/etc/hosts` into CoreDNS by default.

### 6. /etc/hosts entries (laptop side)
Add to `/etc/hosts`:
```
127.0.0.1 mock.iam.example.com
127.0.0.1 overseer-0.fr-platform.iam.orb.local
```

- `mock.iam.example.com` — required for browser and `bin/tunnel` access to AM/IDM/UIs.
- `overseer-0.fr-platform.iam.orb.local` — required for the lodestar overseer pod (deployed into `fr-platform`) to be reachable from the Mac during load test runs.

---

## Deploy Guide

`deploy` deploys the **application stack** — AM, IDM, DS, Gitea, tenant shim, and supporting resources. It assumes `bootstrap` has already been run on the cluster.

**Always use `mock-tenant.py` to deploy. Never use manual kubectl/forgeops steps.**

```sh
python3 bin/mock-tenant.py deploy
```

This runs all steps in the correct order. Individual steps can be run in isolation if needed (e.g. `python3 bin/mock-tenant.py push-config`).

### What `deploy` does (step by step)

Steps are numbered 0–15 internally. Key ordering constraints:

1. **Build images** — `config-loader:local`, `tenant-shim:local`, `ds:local` built against the OrbStack docker context. `ds:local` is required because `image-defaulter` maps `ds` to a local tag for the security-settings customisation.

   ```sh
   # Manual equivalent (if needed):
   docker --context orbstack build -t config-loader:local docker/config-loader/
   docker --context orbstack build -t tenant-shim:local docker/tenant-shim/
   docker --context orbstack build -t ds:local docker/ds/
   ```

2. **Create namespace** — `kubectl create namespace fr-platform` (idempotent)

3. **Deploy Gitea** — `kubectl apply -k kustomize/overlay/mock-tenant/gitea/`

4. **Seed customer-config repo** — `kubectl apply -k kustomize/overlay/mock-tenant/gitea-seed/ --server-side`
   Uses `--server-side` apply because `managed.json` (323KB) exceeds the 262KB annotation limit.

5. **Deploy tenant shim** — `kubectl apply -k kustomize/overlay/mock-tenant/tenant-shim/`

6. **Deploy DS and secrets** — `bin/forgeops apply -e mock-tenant -n fr-platform base ds-cts ds-idrepo`
   Then waits for `ds-set-passwords` Job to complete.

7. **Deploy keystore-create** — `kubectl apply -k kustomize/overlay/mock-tenant/keystore-create/`
   Downloads a static `jq` binary from GitHub at runtime — requires internet access.

8. **Issue TLS cert** — `kubectl apply -k kustomize/overlay/mock-tenant/tls/`

9. **Deploy AM, IDM, UIs** — `bin/forgeops apply -e mock-tenant -n fr-platform am idm admin-ui login-ui end-user-ui`

10. **Create alpha and bravo realms** — via `POST /json/global-config/realms/?_action=create`.
    Idempotent: checks existing realms with `_queryFilter=true` and filters client-side (the endpoint returns 501 for filtered queries).

10b. **Create FileSystemSecretStore/ESV in AM** — via `POST /am/json/global-config/secrets/stores/FileSystemSecretStore/?_action=create`.
    Creates a `FileSystemSecretStore` instance named `ESV` pointing at `/home/forgerock/openam/config/services/esv-secrets` (on the FBC PVC).
    This is the local equivalent of the `GoogleSecretManagerSecretStoreProvider/ESV` store that AIC pre-wires to Google Secret Manager.
    AM's httpclient mTLS cert resolution (`mtlsClientCertSecretPurpose`) reads from this store.
    Idempotent: skips if the store already exists.
    **Note:** the `esv-secrets/` directory itself does NOT exist after this step — it is created lazily by `_write_pem_secrets_to_am()` in the tenant-shim on the first `POST /environment/restart` that has PEM-encoded secrets to write (i.e. after `apply-customer-configuration` runs). The store registration and the directory creation are intentionally separate steps.

11. **Deploy amster and fix OAuth2 client secrets** — `bin/forgeops apply -e mock-tenant -n fr-platform amster`

    Amster creates `idm-resource-server` and `idm-provisioning` clients in the root realm only. Step 11 then:
    - Regenerates `IDM_RS_CLIENT_SECRET` and `IDM_PROVISIONING_CLIENT_SECRET` to alphanumeric-only if they contain `+` or `/` (which breaks AM Basic Auth — see [Known Issues](#known-issues--gotchas))
    - Pushes the correct secrets into AM's root, alpha, and bravo realms via the REST API

11a. **Create tenant stubs** — creates `org-system` namespace with `engine-state`/`tenant-state` ConfigMaps, `am-logging-config` ConfigMap in `fr-platform`, and stub `org-public/haproxy` Deployment. These exist so load test tooling can find them without error.

11b. **Create idmAdminClient** — creates a public PKCE OAuth2 client in the root realm, used by the Admin UI.

12. **Verify FBC init containers** — checks `custom-vol-init` logs for `config-loader done` in both AM and IDM pods.

13. **Health checks** — verifies AM (`/am/json/health/live`) and IDM (`/openidm/info/ping`) are up.

14. **Push AM and IDM config** — `push-config --target all`: clones the customer-config Gitea repo, copies `kustomize/base/gitea-seed/am-conf/` into `am/services/` and IDM static files into `idm/conf/`, commits, and pushes. Restarts AM and IDM if anything changed.

    > **Why both the seed job and push-config are needed:**
    >
    > The `gitea-seed` Job (step 4) does two things `push-config` cannot:
    > 1. **Creates the Gitea repo** via the Gitea API and performs the initial `git push`. `push-config` clones an existing repo — it would fail if the repo doesn't exist yet.
    > 2. **Seeds files not tracked in the local repo** — `webserver.listener-http.json`, `cluster.json`, and `teammember.js` are baked into the seed scripts ConfigMap and only reach Gitea via the seed job.
    >
    > However the seed job only carries a small subset of config: `managed.json`, `repo.ds.json`, `access.json`, the two `opendj.json` DS identity store files, and the three files above. The full AM tree/node config (~150 files in `am-conf/`) is **not** in the ConfigMap and only reaches Gitea via `push-config`. For IDM, `push-config` additionally acts as a safety net — if the local files differ from what the seed job wrote, Gitea is updated and IDM is restarted; if identical, no push or restart occurs.

### Recovery if DS initialized with empty secret

This happens if mittwald was not running when DS first started (step 6).

```sh
kubectl delete statefulset ds-idrepo ds-cts -n fr-platform
kubectl delete pvc -n fr-platform -l app=ds-idrepo
kubectl delete pvc -n fr-platform -l app=ds-cts
kubectl delete job ds-set-passwords -n fr-platform
bin/forgeops apply -e mock-tenant -n fr-platform base ds-cts ds-idrepo
```

Secrets are reused so passwords don't change after recovery.

---

## Push Config & AM Mirror

### push-config

Pushes local config files into Gitea and restarts the relevant pod:

```sh
# Push both AM and IDM
python3 bin/mock-tenant.py push-config

# Push only AM config (faster, and restarts only AM)
python3 bin/mock-tenant.py push-config --target am

# Push only IDM config
python3 bin/mock-tenant.py push-config --target idm

# Re-sync IDM static files from the saas repo before pushing
python3 bin/mock-tenant.py push-config --saasrepo-path /path/to/saas
```

The `--saasrepo-path` option re-runs `bin/merge_idm_gitea-seed.py` against the saas patch files, diffs the result against the static files in `kustomize/base/gitea-seed/idm-conf/`, updates them on disk if different, and prints a reminder to review and commit. It does **not** auto-commit.

### am-mirror

`bin/gitea-seed.py am-mirror` mirrors the live AM root realm's tree and node config into `kustomize/base/gitea-seed/am-conf/` for alpha and bravo realms.

**Why it exists:** Files exported directly from AIC are in AIC's export format (no `metadata` block) and cause NPE in `ConfigEntityConverter` on ForgeOps AM. The ForgeOps root realm ships files with `metadata.uid` in the correct format. `am-mirror` copies root realm files and adapts the realm reference and uid, producing valid ForgeOps-format files.

**What it generates:**
- Instance files: `<service>/1.0/organizationconfig/default/<uuid>.json` — one per node instance, with `metadata.uid` pointing to `o=alpha|bravo,ou=services,ou=am-config`
- Service singletons: `<service>/1.0/organizationconfig/default.json` — one per whitelisted service directory

**Service singletons are critical.** Without `pagenode/1.0/organizationconfig/default.json`, the DS subtree that holds instance UUIDs doesn't exist. AM then throws `NodeProcessException: Node did not exist <uuid>` when executing the tree.

**`identityResource` auto-injection:** `am-mirror` automatically injects `"identityResource": "managed/{realm}_user"` into any tree that contains IDM-calling nodes (`IncrementLoginCountNode`, `LoginCountDecisionNode`, `PatchObjectNode`, `QueryFilterDecisionNode`) but does not already have it set. This is required for those nodes to find the correct IDM managed object. Inner trees (called via `InnerTreeEvaluatorNode`) need `identityResource` independently — they do NOT inherit it from the outer tree.

**When to re-run:** when the Login tree structure changes (new nodes added).

```sh
python3 bin/gitea-seed.py am-mirror --namespace fr-platform
python3 bin/mock-tenant.py push-config --target am
```

### SAFE_DIRS whitelist

Only whitelisted directories are included by `am-mirror`. The whitelist covers tree-node instance directories and a subset of realm service directories that produce ForgeOps-compatible files (with `metadata.uid`) when mirrored from the root realm. Service directories that are NOT whitelisted (e.g. `scriptingservice`, `iplanetamauthservice`) are excluded because their AIC export format lacks the `metadata` block and causes NPE in `ConfigEntityConverter` on ForgeOps AM.

Current whitelist (`_AM_MIRROR_SAFE_DIRS` in `bin/gitea-seed.py`):
```
authenticationtreesservice    datastoredecisionnode         incrementlogincountnode
pagenode                      innertreeevaluatornode        logincountdecisionnode
validatedusernamenode         retrylimitdecisionnode        queryfilterdecisionnode
validatedpasswordnode         accountlockoutnode            patchobjectnode
attributecollectornode        sunidentityrepositoryservice
oauth2provider                idmintegrationservice         amrealmbaseurl
selfservicetrees              socialidentityproviders       validationservice
```

`sunidentityrepositoryservice` (opendj.json) configures the identity store pointing at `ou=user,o={realm},o=root,ou=identities`.

`oauth2provider` and `idmintegrationservice` are included so the `idm-provisioning` OAuth2 client and IDM integration endpoint are present in alpha/bravo without manual REST creation.

---

## Implementation Section

### What Was Changed in ForgeOps

This section documents every file added or modified from the base ForgeOps repo.

#### config-loader image

A new Alpine-based init container image that clones a git repo and copies config files into the shared volume. The `mock-tenant` overlay patches the existing `custom-vol-init` container in AM and IDM Deployments to use this image with the `clone-and-copy` entrypoint.

Env vars consumed:
```
GIT_URL               — full repo URL (required)
GIT_TOKEN             — optional bearer token embedded into HTTPS clone URL
GIT_PATH              — local clone destination (required)
CONFIG_SRC_PATH       — subdirectory in repo: am / idm (required)
DESTINATION_PATH      — where to copy files (required)
CONFIG_LOAD_STRATEGY  — JSON_MERGE (AM) or JSON_REPLACE (IDM) (required)
BRANCH                — git branch to clone (default: master)
```

Strategies:
- `JSON_REPLACE` — plain `cp` (IDM)
- `JSON_MERGE` — `jq -s '.[0] * .[1]'` deep-merge per JSON file; non-JSON files copied straight (AM)

#### x-forgerock-transactionid header

Real AIC tenants (via HAProxy or a front-end gateway) inject an `x-forgerock-transactionid` header on every request. This is used for request correlation in AM/IDM audit logs and by lodestar test assertions.

In the mock-tenant, the nginx Ingress controller injects it using a `configuration-snippet` annotation on the AM and IDM Ingress objects:

```nginx
more_set_headers "x-forgerock-transactionid: $request_id";
```

`$request_id` is a 32-char hex string generated per request by nginx — unique and suitable for correlation, though not UUID-formatted. The annotation requires two one-time ConfigMap settings on the nginx controller (done as part of `bootstrap`):

```sh
kubectl patch configmap ingress-nginx-controller -n ingress-nginx \
  --type merge \
  -p '{"data":{"allow-snippet-annotations":"true","annotations-risk-level":"Critical"}}'
```

`annotations-risk-level=Critical` is required from ingress-nginx v1.12+ — without it the admission webhook rejects `configuration-snippet` even when `allow-snippet-annotations` is true.

#### AM service targetPort fix

The base AM Service had `targetPort: https` (port 8081, AM's HTTPS port). nginx was routing to AM's HTTPS port and getting TLS handshake errors. Changed to `targetPort: http` (port 8080).

#### AM server URL fix

AM stores its server URL in config at first bootstrap. Without correct JVM properties it defaults to the k8s service name `am` and issues redirects to `https://am/am/XUI/`, which browsers cannot reach. Two changes:

1. `platform-config.yaml` — `AM_SERVER_FQDN: mock.iam.example.com`
2. `CATALINA_USER_OPTS` in the AM Deployment overlay — `-Dam.server.protocol=https -Dam.server.fqdn=mock.iam.example.com -Dam.server.port=443`

`AM_SERVER_FQDN` alone is not sufficient — the JVM only reads system properties set via `CATALINA_USER_OPTS`.

#### FBC_BASE_PATHS override

The ForgeOps AM image bakes in:
```
FBC_BASE_PATHS=-Dcom.forgerock.am.fileconfig.basepaths=/home/forgerock/base/config/services
```

This only covers the root realm. Alpha/bravo config from Gitea lands in `/home/forgerock/openam/config/services/` — outside the importer's scan path. The overlay adds:
```yaml
- name: FBC_BASE_PATHS
  value: "-Dcom.forgerock.am.fileconfig.basepaths=/home/forgerock/base/config/services,/home/forgerock/openam/config/services"
```

**Do NOT simplify to a single path.** Removing `base/` causes AM to crash at startup with `AuthD init() — java.util.NoSuchElementException` because root realm `iPlanetAMAuthService` config only lives in `base/`, not in the FBC overlay path. See [FBC_BASE_PATHS Two-Path Requirement](#fbc_base_paths-two-path-requirement).

#### DS customisations

DS security settings are relaxed for local dev at Docker build time (not runtime), via `docker/ds/mock-tenant-config.sh` run after `ds-setup.sh` inside the Dockerfile. Settings: `require-secure-authentication`, `require-secure-password-changes`, `unauthenticated-requests-policy`, and `password-validator` are all relaxed. This is baked into `data.tar.gz` so it survives PVC wipes.

This is deliberately in a separate file from `ds-setup.sh` — upstream ForgeOps previously removed these settings from `ds-setup.sh` (FORGEOPS-4828, "move DS to secure by default"), so keeping them in a separate `RUN` step avoids merge conflicts.

#### keystore-create fixes

The base `keystore-create` Job uses the AM image, which has no `jq`. Two overlay patches:
- `keystore-type-patch.yaml` — downloads a static `jq` binary from GitHub releases; sets `KEYSTORE_TYPE=jceks`
- `role-binding.yaml` — patches the RoleBinding namespace from hardcoded `prod` to `fr-platform`
- `image-pull-policy.yaml` (ds-set-passwords) — overrides `imagePullPolicy: Always` to `IfNotPresent` so the locally-built `ds:local` image is used without a registry pull attempt

#### Gitea in-cluster git server

Single Gitea pod. Key implementation details:
- No `runAsUser` set — Gitea's s6 init supervisor must start as root
- `init-dirs` init container pre-creates `/data/git/.ssh` and `/data/gitea` with ownership `1000:1000`
- Admin user created via `lifecycle.postStart` hook as `su git -c 'gitea admin user create ...'` — `DEFAULT_ADMIN_*` env vars do NOT work in gitea:1.22
- Seed Job uses `curl -u user:pass` (not `wget` — Alpine BusyBox wget lacks `--user`/`--password`) and `alpine:3.19` image (not `alpine/git` which has git as its ENTRYPOINT)

#### Tenant shim

FastAPI service that emulates AIC's Environment Secrets & Variables REST API and other AIC-specific endpoints. See [Tenant Shim](#tenant-shim) for architecture, API surface, RBAC, and verification steps.

### Files Table

#### New Files Added

**Docker**

| File | Purpose |
|---|---|
| `docker/config-loader/Dockerfile` | config-loader image (Alpine + git + jq) |
| `docker/config-loader/clone-and-copy.sh` | FBC init container script |
| `docker/tenant-shim/Dockerfile` | Tenant shim image (python:3.12-slim + FastAPI + git) |
| `docker/tenant-shim/requirements.txt` | Tenant shim Python dependencies |
| `docker/tenant-shim/app/main.py` | ESV-compatible API (PUT-upsert, valueBase64); mirrors live AM and IDM config to Gitea before restart |
| `docker/ds/mock-tenant-config.sh` | Relaxes DS security settings for local dev |
| `docker/ds/saas-compat-config.sh` | Applies saas-compatible DS settings at build time |
| `docker/ds/Dockerfile.mock-tenant` | Two-stage DS build: inherits `ds:local-base`; copies mock-tenant runtime-scripts and runs mock-tenant/saas-compat config |
| `docker/ds/runtime-scripts-mock-tenant/ds-cts/setup` | CTS setup: copies `custom-schema/*.ldif` to `db/schema/`, sets `db-durability:low` |
| `docker/ds/runtime-scripts-mock-tenant/ds-idrepo/setup` | idrepo setup: saas-aligned indexes, virtual attributes, unique-attribute plugins |
| `docker/ds/runtime-scripts-mock-tenant/ds-idrepo/post-init` | Grants `unindexed-search` privilege to `am-identity-bind-account` |
| `docker/ds/ldif-ext/identities/mock-tenant-orgs.ldif` | Extra LDAP entries: `ou=svcaccts`, `ou=user`, `ou=organization`, `ou=application` for alpha/bravo |
| `docker/ds/config/schema-mock-tenant/99-fraas-schema.ldif` | FRaaS custom LDAP schema (copied from saas `FRAAS/repo` setup-profile; staged separately so it loads after `idm-repo` defines `fr-idm-uuid` — see SaaS Sync Part 1) |
| `docker/mock-tenant-bake.hcl` | Bake overlay: adds `config-loader` and `tenant-shim` targets; compose with `docker-bake.hcl` |

**Kustomize — base**

| File | Purpose |
|---|---|
| `kustomize/base/gitea/` | Gitea Deployment, Service, PVC |
| `kustomize/base/gitea-seed/am-conf/` | AM FBC config files for alpha/bravo realms (~150 JSON files) |
| `kustomize/base/gitea-seed/idm-conf/managed.json` | Merged IDM managed objects (saas-compatible) |
| `kustomize/base/gitea-seed/idm-conf/repo.ds.json` | Merged IDM DS repo mappings |
| `kustomize/base/gitea-seed/idm-conf/access.json` | IDM access policy |
| `kustomize/base/gitea-seed/idm-script/teammember.js` | IDM teammember script |
| `kustomize/base/tenant-shim/kustomization.yaml` | Tenant shim base kustomization |
| `kustomize/base/tenant-shim/tenant-shim-deployment.yaml` | Tenant shim Deployment |
| `kustomize/base/tenant-shim/tenant-shim-ingress.yaml` | Tenant shim Ingress |
| `kustomize/base/tenant-shim/tenant-shim-service.yaml` | Tenant shim Service (ClusterIP, port 8080) |
| `kustomize/base/tenant-shim/tenant-shim-rbac.yaml` | Tenant shim ServiceAccount + Role + RoleBinding (pods + pods/exec added for AM config mirror) |
| `kustomize/base/tenant-shim/esv-projection-configmap.yaml` | Empty `esv-variables` ConfigMap (pre-created) |
| `kustomize/base/tenant-shim/esv-projection-secret.yaml` | Empty `esv-secrets` Secret (pre-created) |

**Kustomize — overlay/mock-tenant**

| File | Purpose |
|---|---|
| `kustomize/overlay/mock-tenant/kustomization.yaml` | Top-level overlay kustomization |
| `kustomize/overlay/mock-tenant/base/platform-config.yaml` | `FQDN` + `AM_SERVER_FQDN: mock.iam.example.com` |
| `kustomize/overlay/mock-tenant/image-defaulter/kustomization.yaml` | Local image tag mappings (`ds:local`, `config-loader:local`, `tenant-shim:local`) |
| `kustomize/overlay/mock-tenant/am/deployment.yaml` | `custom-vol-init` patch, `CATALINA_USER_OPTS`, `FBC_BASE_PATHS`, envFrom, `postStart` realm hook, `catalina-properties` subPath volume mount |
| `kustomize/overlay/mock-tenant/am/catalina-properties-cm.yaml` | `am-catalina-properties` ConfigMap — base Tomcat `catalina.properties` + ESV values appended by shim on restart |
| `kustomize/overlay/mock-tenant/am/ingress-fqdn.yaml` | AM host/TLS → `mock.iam.example.com`; injects `x-forgerock-transactionid: $request_id` response header |
| `kustomize/overlay/mock-tenant/am/service.yaml` | `targetPort: http` (was `https`) |
| `kustomize/overlay/mock-tenant/amster/amster-job.yaml` | Amster job: SSH key path fix (`id_rsa`) |
| `kustomize/overlay/mock-tenant/idm/deployment.yaml` | `custom-vol-init` patch, `esv-variables`/`esv-secrets` envFrom |
| `kustomize/overlay/mock-tenant/idm/ingress-fqdn.yaml` | IDM host/TLS → `mock.iam.example.com`; injects `x-forgerock-transactionid: $request_id` response header |
| `kustomize/overlay/mock-tenant/ig/deployment.yaml` | IG imagePullPolicy patch |
| `kustomize/overlay/mock-tenant/ig/ingress-fqdn.yaml` | IG host/TLS → `mock.iam.example.com` |
| `kustomize/overlay/mock-tenant/admin-ui/ingress-fqdn.yaml` | Admin UI host/TLS → `mock.iam.example.com` |
| `kustomize/overlay/mock-tenant/end-user-ui/ingress-fqdn.yaml` | End-user UI host/TLS → `mock.iam.example.com` |
| `kustomize/overlay/mock-tenant/login-ui/ingress-fqdn.yaml` | Login UI host/TLS → `mock.iam.example.com` |
| `kustomize/overlay/mock-tenant/tls/certificate.yaml` | cert-manager ClusterIssuer + Certificate (`platform-tls`) |
| `kustomize/overlay/mock-tenant/keystore-create/keystore-type-patch.yaml` | jq download + `KEYSTORE_TYPE=jceks` fix |
| `kustomize/overlay/mock-tenant/keystore-create/role-binding.yaml` | Namespace patch: `prod` → `fr-platform` |
| `kustomize/overlay/mock-tenant/ds-set-passwords/image-pull-policy.yaml` | `imagePullPolicy: IfNotPresent` for `ds:local` |
| `kustomize/overlay/mock-tenant/ds-idrepo/sts.yaml` | `storageClassName: local-path`; memory 2Gi; `imagePullPolicy: IfNotPresent` |
| `kustomize/overlay/mock-tenant/ds-idrepo/snapshot-schedule.yaml` | DS idrepo snapshot schedule |
| `kustomize/overlay/mock-tenant/ds-cts/sts.yaml` | `storageClassName: local-path`; `imagePullPolicy: IfNotPresent` |
| `kustomize/overlay/mock-tenant/ds-cts/snapshot-schedule.yaml` | DS CTS snapshot schedule |
| `kustomize/overlay/mock-tenant/ds-snapshot/` | DS snapshot RBAC + ConfigMap overlay |
| `kustomize/overlay/mock-tenant/tenant-shim/ingress-fqdn.yaml` | Tenant shim host/TLS → `mock.iam.example.com` |
| `kustomize/overlay/mock-tenant/gitea/` | Gitea overlay (namespace) |
| `kustomize/overlay/mock-tenant/gitea-seed/` | Gitea seed Job overlay |

**Scripts / docs**

| File | Purpose |
|---|---|
| `bin/mock-tenant.py` | Full deploy/push-config/bootstrap automation |
| `bin/gitea-seed.py` | IDM merge + AM mirror tool (`merge managed`, `merge repo-ds`, `merge access`, `am-mirror` subcommands) |
| `bin/merge_idm_gitea-seed.py` | IDM config merge tool (called by `gitea-seed.py`) |
| `bin/get_admin_tok.sh` | Fetches AM admin token via curl |
| `bin/tunnel` | Port-forwards nginx 443 for browser access |
| `mock-tenant.md` | This document |
| `.gitignore` | Added `fbc/`, `env.log`, and `.claude/settings*.json` ignores |
| `kustomize/base/gitea-seed/kustomization.yaml` | Seed Job kustomization with `configMapGenerator` for IDM conf files |
| `kustomize/base/gitea-seed/gitea-seed-job.yaml` | Seed Job: mounts IDM conf ConfigMap at `/idm-conf/` |
| `kustomize/base/gitea-seed/gitea-seed-configmap.yaml` | `seed.sh`: copies from `/idm-conf/`, seeds am-conf tree |
| `kustomize/overlay/mock-tenant/am/kustomization.yaml` | AM overlay kustomization (`secret-generator` mode) |
| `kustomize/overlay/mock-tenant/idm/kustomization.yaml` | IDM overlay kustomization (`secret-generator` mode) |
| `kustomize/overlay/mock-tenant/secrets/kustomization.yaml` | Secrets overlay kustomization (`secret-generator` mode) |

---

## AM Tree Config for Alpha/Bravo Realms

### How AM FBC Imports Config into DS

AM has two distinct config layers when using FBC:

1. **In-memory / file layer**: AM reads FBC files at startup and caches service definitions in memory. This makes trees _visible_ in the Admin API.
2. **DS layer**: AM writes node instance configs into DS via a FBC importer at startup. This makes trees _executable_ — `AuthNodeFactory.getConfigForNode` reads node instances from DS at auth time.

Both layers are required for a tree to execute. Both are driven by the `FBC_BASE_PATHS` JVM property.

### FBC_BASE_PATHS Two-Path Requirement

**AIC context:** AIC production JVM args have NO `basepaths` property — the importer uses `-Dcom.sun.identity.configuration.directory=/home/forgerock/openam` as the implicit root. This works in AIC because `openam/` contains a complete config tree including all root realm auth services.

**ForgeOps context:** ForgeOps splits config across two paths:
- `/home/forgerock/base/config/services` — image-baked, root realm only (`iPlanetAMAuthService`)
- `/home/forgerock/openam/config/services` — FBC overlay, populated at pod startup from Gitea

The FBC importer must scan both. The override in `kustomize/overlay/mock-tenant/am/deployment.yaml`:
```yaml
- name: FBC_BASE_PATHS
  value: "-Dcom.forgerock.am.fileconfig.basepaths=/home/forgerock/base/config/services,/home/forgerock/openam/config/services"
```

**Do NOT simplify to a single path.** Removing `base/` causes AM to crash at startup:
```
AuthD init() — java.util.NoSuchElementException
    at AuthD.updateAuthServiceGlobals()
```
Root realm `iPlanetAMAuthService` config is only in `base/` — there is no FBC file for it in `openam/`.

### Why IdentityStoreDecisionNode Cannot Be Used

AIC's Login tree uses `IdentityStoreDecisionNode`, which sets the IDM managed object identity into shared state for downstream nodes (`IncrementLoginCountNode` etc.). **This node does not exist in ForgeOps AM.** Attempting to use it causes:
```
java.lang.IllegalArgumentException: Unsupported node type IdentityStoreDecisionNode
    at AuthNodeFactory.getOutcomeProvider(AuthNodeFactory.java:231)
```
This makes the Login tree return 500 and fail to render in the UI entirely.

**The workaround:** The `identityResource` field on the tree definition (not on any node) tells AM which managed object to associate with the session. Setting `"identityResource": "managed/alpha_user"` in `login.json` at the tree level achieves the same shared-state population. The Login trees in `kustomize/base/gitea-seed/am-conf/realm/root-alpha/` and `root-bravo/` use `DataStoreDecisionNode` with `identityResource` set on the tree.

**Do NOT add `identitystoredecisionnode` to `_AM_MIRROR_SAFE_DIRS`** — it only exists in AIC, not in ForgeOps AM.

### identityResource on Inner Trees

**Inner trees called via `InnerTreeEvaluatorNode` do NOT inherit `identityResource` from the outer tree.** Trees like `ProgressiveProfile`, `Registration`, `ResetPassword`, and `UpdatePassword` each need `identityResource` set independently if they contain `IncrementLoginCountNode`, `LoginCountDecisionNode`, or `PatchObjectNode`.

Without it, those nodes fail:
- `IncrementLoginCountNode`: "No object to increment"
- `LoginCountDecisionNode` / `PatchObjectNode`: "Failed to retrieve existing object"

`am-mirror` auto-injects `identityResource` on any tree containing IDM nodes that doesn't already have it.

### AIC JVM Flag: `-Dcom.forgerock.am.enable_cloud_only_features=true`

Present in AIC production JVM args (confirmed from `/Users/minikube/FRaaS/prf-e2e-ame-34937-2/fbc/am/jvm.txt`). Enables:
- `/environment` REST API (ESV endpoints) — without this flag those endpoints return 404 on ForgeOps AM. Our tenant shim handles this separately, so this flag is NOT currently needed.
- Tenant management APIs — cloud-tier admin endpoints not in the open-source build.
- Cloud-specific OAuth2/OIDC behaviors.

**Not currently set in this mock tenant.** If a load test failure traces back to a missing cloud API, add it to `CATALINA_USER_OPTS` in `kustomize/overlay/mock-tenant/am/deployment.yaml` alongside the existing `-Dam.server.*` properties — safe to add.

### Load Test Status (as of 2026-07-28)

The Lodestar/Pyrock `idc.login` load test authenticates against the alpha realm using `authIndexType=service&authIndexValue=Login`.

| Issue | Status | Fix |
|---|---|---|
| `500 "No authentication trees service found"` | FIXED | uid suffix `ou=` → `o=` in `am-mirror` |
| `500 NodeProcessException: Node did not exist` | FIXED | `FBC_BASE_PATHS` two-path + service singletons |
| `IllegalArgumentException: Unsupported node type IdentityStoreDecisionNode` | FIXED | Reverted to `DataStoreDecisionNode` + `identityResource` on tree |
| `IncrementLoginCountNode: No object to increment` | FIXED | `identityResource: managed/alpha_user` on Login tree |
| `LoginCountDecisionNode: Failed to retrieve existing object` | FIXED | `identityResource` on inner trees (ProgressiveProfile etc.) |
| `invalid_client` on OAuth2 endpoints | FIXED | `IDM_PROVISIONING_CLIENT_SECRET` regenerated alphanumeric-only |
| `Insufficient Access Rights: unindexed search` | FIXED | `unindexed-search` DS privilege on `am-identity-bind-account` persisted in `docker/ds/runtime-scripts/ds-idrepo/post-init` |
| `PatchObjectNode: identity resource mismatch (managed/user vs managed/alpha_user)` | FIXED | Node instance files rewritten to `managed/{realm}_user`; `am-mirror` now does this automatically |
| **`idc.login` end-to-end** | **PASSING** | |
| `httpClient` NPE in `LIBRARY_SCRIPT` (banc load test) | FIXED | `SCRIPTED_DECISION_NODE` `propertyNamePrefix: "esv."` + ESVs injected via `catalina.properties`; see [Known Issues](#am) |

---

## SaaS Sync — Planned Work

The mock tenant needs to stay compatible with the production saas tenant (`/Users/wajih.ahmed/source/github.com/ForgeCloud/saas`) so load tests run against a realistic data model. The following parts are planned.

### Part 1 — DS Schema

Add `docker/ds/config/schema-mock-tenant/99-fraas-schema.ldif` — verbatim copy of the saas repo's `services/userstore/setup-profiles/FRAAS/repo/7.0/schema/99-fraas-schema.ldif`. Defines FRaaS attribute types and object classes with the same OIDs as production (indexed: `fr-attr-istr1–20` etc.; unindexed: `fr-attr-str1–5` etc.; object classes: `fr-ext-attrs`, `fr-id-cloud-svc-acct`, `fraas-admin`).

**Important — schema dependency and load ordering:** `fr-id-cloud-svc-acct` in `99-fraas-schema.ldif` requires `fr-idm-uuid` as a MUST attribute. `fr-idm-uuid` is **not** defined in the saas repo or in `99-fraas-schema.ldif` itself — it is defined by PingDS's own `idm-repo` setup-profile (`60-repo-schema.ldif`). In AIC, DS is a managed service where `idm-repo` has already been applied before the FRAAS schema is loaded, so the dependency is satisfied transparently. In ForgeOps, `idm-repo` runs at pod init time via the `setup` script. This means `99-fraas-schema.ldif` **must not** be placed in `config/schema/` (where the base Dockerfile puts it into `custom-schema/` and the setup script copies it into `db/schema/` before setup-profiles run — before `fr-idm-uuid` exists). Instead it is staged at `mock-tenant-schema/` by `Dockerfile.mock-tenant` and explicitly copied into `db/schema/` at the end of `runtime-scripts-mock-tenant/ds-idrepo/setup` — after `idm-repo` has run and `60-repo-schema.ldif` is already in `db/schema/`. DS loads `db/schema/` alphabetically (`60-repo-schema.ldif` before `99-fraas-schema.ldif`), so `fr-idm-uuid` is defined before the objectclass references it.

### Part 2 — DS Global Settings

New `docker/ds/saas-compat-config.sh` (same pattern as `mock-tenant-config.sh`): enables all password storage schemes (Argon2, Bcrypt, PBKDF2-*, SCRAM-SHA-*), sets `allow-pre-encoded-passwords:true`, `cfgStore index-entry-limit:20000`, `max-request-size:15mb`, `strict-format-postal-addresses:false`.

### Part 3 — DS Indexes

Append to `docker/ds/runtime-scripts/ds-idrepo/setup`: new equality/big-equality/extensible indexes for all FRaaS attributes on `amIdentityStore`; new indexes on `idmRepo` and `cfgStore`; virtual `memberURL` attributes; `uid` uniqueness plugins for alpha/bravo.

### Part 4 — DS CTS

Set `db-durability:low` on `amCts` backend.

### Part 5 — IDM Config Files

Bring `managed.json` (adds `teammember`, `svcacct` types), `repo.ds.json` (LDAP mappings for new OUs), `access.json`, `webserver.listener-http.json`, `cluster.json`, and `script/teammember.js` from the saas repo into Gitea via the seed job.

**Why seed IDM config via Gitea, not baked into the IDM image:** IDM config files are plain JSON loaded at pod startup. Seeding via Gitea keeps the FBC model intact — change the file, re-run `push-config`, restart IDM, no image rebuild.

**Static files:** The merged outputs for `managed.json`, `repo.ds.json`, and `access.json` are committed as static files in `kustomize/base/gitea-seed/idm-conf/`. To re-sync when saas patch files change:
```sh
python3 bin/mock-tenant.py push-config --saasrepo-path /path/to/saas
```

**Build notes:** Parts 1–4 require a `ds:local` rebuild and PVC wipe. Part 5 requires a DS rebuild (new OUs in `orgs.ldif`).

### Verification

```sh
DS_POD=$(kubectl get pod -n fr-platform -l app=ds-idrepo -o jsonpath='{.items[0].metadata.name}')

# Schema present
kubectl exec -n fr-platform $DS_POD -- \
    ldapsearch -h localhost -p 1389 -D uid=admin -w password \
    -b cn=schema "(objectClass=subschema)" attributeTypes | grep fr-attr-istr1

# Indexes present
kubectl exec -n fr-platform $DS_POD -- \
    dsconfig --offline list-backend-indexes --backend-name amIdentityStore | grep fr-attr-istr1

# Password schemes enabled
kubectl exec -n fr-platform $DS_POD -- \
    dsconfig --offline list-password-storage-schemes | grep -E "Argon2|Bcrypt|SCRAM"

# IDM managed objects (empty result, not 404, means schema registered)
curl -sk "https://mock.iam.example.com/openidm/managed/teammember?_queryFilter=true&_pageSize=1" \
    -H "Cookie: iPlanetDirectoryPro=$TOKEN"
```

---

## Tenant Shim

A FastAPI service that emulates AIC's Environment Secrets & Variables (ESV) REST API and other AIC-specific endpoints, enabling unmodified lodestar tooling (`tenant_util.py esv import --apply`) to configure the local tenant.

### Architecture

```
Client (curl / lodestar tenant_util.py)
   │  HTTP :8080
   ▼
tenant-shim (FastAPI, Deployment+Service in fr-platform)
   │  uses Kubernetes Python client (in-cluster ServiceAccount token)
   ▼
Per-item objects (source of truth, PUT-upsert target):
   ConfigMap  esv-var-<_id>      data: {value: "<plain>"}        annotations: description, expressionType, updatedAt
   Secret     esv-secret-<_id>   data: {value: "<valueBase64>"}  annotations: description, encoding, useInPlaceholders, updatedAt
   label on both: esv.forgeops/managed=true, esv.forgeops/type=variable|secret

   Note: secret valueBase64 is stored verbatim (k8s Secret.data is itself base64 — no re-encoding).
   Variable valueBase64 is decoded once to plain; re-encoded on GET.

ESV secrets fall into two categories that are consumed differently by AM/IDM:

   1. Scalar (single static value) ESVs — strings, JSON blobs, numbers, key material stored as plain text.
      Consumed via systemEnv.getProperty("esv.foo.bar") in AM scripts, or
      identityServer.getProperty("esv.foo.bar") in IDM scripts.
      Injected by do_restart() into catalina.properties (AM) and boot.properties (IDM).
      All ESV variables and most ESV secrets fall into this category.

   2. PEM/cert ESVs — secrets with esv.forgeops/encoding=pem annotation, containing
      certificate and/or private key material.
      AM cannot consume these as a string property — it loads them as Java crypto objects
      through its secret store machinery (FileSystemSecretStore/ESV).
      do_restart() writes these as files to /home/forgerock/openam/config/services/esv-secrets/
      on the AM FBC PVC. Secret-store mappings (PUT by lodestar) tell AM which file
      maps to which secret purpose (e.g. mtlsClientCertSecretPurpose).

      IMPORTANT — write order: PEM files must be written AFTER the AM pod has fully restarted,
      not before. The reason: custom-vol-init (the init container) runs on every pod start and
      clones /home/forgerock/openam/config/services/ fresh from Gitea, completely replacing its
      contents. Since esv-secrets/ is not tracked in Gitea, any files written there before the
      restart are wiped when the new pod starts. do_restart() therefore: (1) triggers the rolling
      restart, (2) waits for the new AM pod to be fully ready via _wait_for_rollout("am"),
      (3) only then writes the PEM files — at which point custom-vol-init has already finished
      and the directory is stable for the lifetime of the pod.

POST /environment/restart — execution order:
   1. List all esv-var-* / esv-secret-* objects by label
   2. Build catalina.properties (AM) and boot.properties (IDM) with all ESV scalar values.
      Patch ConfigMaps am-catalina-properties and idm-boot-properties.
      (These are ConfigMap volume mounts — picked up at next pod start, no exec needed.)
   3. Mirror live AM FBC (realm/root-alpha, realm/root-bravo) to Gitea — so journeys/nodes
      imported live survive the restart. Written by: tenant-shim via kubectl exec tar|base64.
   4. Mirror live IDM conf/ and script/ to Gitea — so managed objects imported live survive
      the restart. Written by: tenant-shim via kubectl exec tar|base64.
   5. Patch Deployment am and Deployment idm with esv.forgeops/restarted-at annotation
      → triggers rolling restart. New AM pod starts: custom-vol-init clones Gitea (steps 3+4
      content now in Gitea) → filesystem-init overlays → AM starts with updated FBC and ESV
      scalar values from catalina.properties.
   6. Wait for AM rollout to complete (_wait_for_rollout polls apps_v1 until all replicas ready).
   7. Write PEM files to AM pod via kubectl exec — AFTER custom-vol-init has finished and the
      directory is stable. Written by: tenant-shim to /home/forgerock/openam/config/services/esv-secrets/
      on the am-fbc PVC. These files persist for the lifetime of the pod and are read on demand
      by AM's FileSystemSecretStore/ESV when an httpclient mTLS cert is needed.

PUT /am/json/realms/root/realms/{realm}/realm-config/secrets/stores/
    GoogleSecretManagerSecretStoreProvider/ESV/mappings/{name}:
   1. Persist mapping in a ConfigMap (esv-mapping-<realm-name>) — same as before
   2. Forward the mapping to AM's local FileSystemSecretStore/ESV via PUT
      (translates the store type transparently — AM stores the mapping against the local store)
```

**Key design decisions:**
- **Storage:** one Kubernetes object per ESV item (`esv-var-<id>` ConfigMap, `esv-secret-<id>` Secret) — trivial CRUD, per-item metadata via annotations
- **Apply mechanism:** projects all ESV values into `am-catalina-properties` ConfigMap as Java system properties (`esv.foo.bar=value`), then roll-restarts AM/IDM. AM scripts call `systemEnv.getProperty("esv.foo.bar")` which resolves against JVM system properties — Tomcat's `catalina.properties` is the correct injection point, replicating what AIC's `secrets-loader` does.
- **Why not env vars:** `systemEnv.getProperty()` uses `System.getProperty()` (JVM system properties), not `System.getenv()`. Env var names cannot contain dots on Linux, so `esv-foo-bar` (dashes) cannot be looked up as `esv.foo.bar` (dots). The `catalina.properties` approach handles this correctly and also supports large values (JSON blobs, key pairs) without command-line length limits.
- **AM config mirror before restart:** `apply-customer-configuration` imports journeys and ESVs into live AM via REST, then calls `POST /environment/restart`. Because AM's `filesystem-init` init container repopulates `/home/forgerock/openam/config/services` from Gitea on every pod boot, any live config not committed to Gitea is lost on restart. To prevent this, `do_restart()` snapshots the live FBC realm directories (`realm/root-alpha`, `realm/root-bravo`) from the AM pod via `kubectl exec tar | base64`, clones `customer-config` from Gitea, extracts the snapshot into `am/services/realm/`, and commits+pushes if anything changed — before triggering the restart. Mirror failure is warning-only so ESV values always take effect even if Gitea is unreachable. This required adding `git` to the tenant-shim image and `pods`/`pods/exec` RBAC.
- **mTLS cert loading via FileSystemSecretStore:** On real AIC, AM resolves httpclient `mtlsClientCertSecretPurpose` through a `GoogleSecretManagerSecretStoreProvider/ESV` store instance pre-wired by the platform to Google Secret Manager. Locally this store instance doesn't exist, so AM presents no client cert and the mTLS handshake fails with 403. The fix is three-part: (1) `mock-tenant.py` step 10b creates a `FileSystemSecretStore/ESV` instance in AM at deploy time, pointing at `/home/forgerock/openam/config/services/esv-secrets/` on the FBC PVC — the directory does not yet exist at this point; (2) `do_restart()` creates the directory via `kubectl exec mkdir -p` and writes every PEM-encoded ESV secret (those with `esv.forgeops/encoding: pem` annotation) as a file into it — the filename is the k8s Secret name minus the `esv-secret-` prefix — this happens on the first `POST /environment/restart` after `apply-customer-configuration` has imported the PEM secrets; (3) when lodestar PUTs a secret-store mapping targeting `GoogleSecretManagerSecretStoreProvider/ESV`, the shim intercepts it, stores it in a ConfigMap as before, and also forwards it to AM's `FileSystemSecretStore/ESV` via the AM internal REST API — so AM knows which file to load for which purpose. No new PVC or volume mounts are needed: the files go onto the existing `am-fbc` PVC which already backs `/home/forgerock/openam/config/services/`.
- **IDM config mirror before restart:** The same root cause applies to IDM — `apply-customer-configuration` PATCHes custom managed objects (`Captcha`, `config_data`, `key_manager`, `service_token_storage`) into live IDM via `PATCH /openidm/config/managed`. Because IDM's `fbc` volume is an emptyDir repopulated from Gitea by `custom-vol-init` on every pod boot, any live IDM config not committed to Gitea is lost on restart. `do_restart()` now also calls `_mirror_idm_to_gitea()` which snapshots `conf/` and `script/` from the IDM pod via `kubectl exec tar | base64`, clones `customer-config` from Gitea, extracts the snapshot into `idm/`, and commits+pushes if anything changed — before triggering the restart. Mirror failure is warning-only (same as AM mirror). This required adding `git` to the tenant-shim image.
- **Auth:** none — ClusterIP only, cluster-internal
- **PUT is an upsert** — `201` if id didn't exist, `200` if it did; no POST-to-create — matches real AIC
- **`GET /environment/secrets/{_id}` never returns the secret value** — metadata only, matching real AIC (lodestar retrieves clear-text secrets via a separate IDM script-eval endpoint)

### API Surface

Wire-format compatible with real AIC, verified by running lodestar's `tenant_util.py esv import --apply` against a live deployment of this shim with a real `openam-perf-banc_esv-export.json` export (11 secrets, 13 variables — all 24 items imported with `201` on first run, `200` on re-run).

| Method | Path | Body / Response |
|---|---|---|
| GET | `/environment/variables` | `{"result": [...], "resultCount": N}`, each item: `{_id, valueBase64, description, expressionType, lastChangeDate, lastChangedBy, loaded}` |
| GET | `/environment/variables/{_id}` | single item, same shape |
| PUT | `/environment/variables/{_id}` | body: `{valueBase64, description?, expressionType?}` — upsert: `201` created / `200` updated |
| DELETE | `/environment/variables/{_id}` | `204` |
| GET | `/environment/secrets` | `{"result": [...], "resultCount": N}`, each item: `{_id, activeVersion, loadedVersion, description, encoding, useInPlaceholders, lastChangeDate, lastChangedBy, loaded}` — **no value field** |
| GET | `/environment/secrets/{_id}` | single item metadata, same shape (no value) |
| PUT | `/environment/secrets/{_id}` | body: `{valueBase64, description?, encoding?, useInPlaceholders?}` — upsert: `201` / `200` |
| DELETE | `/environment/secrets/{_id}` | `204` |
| POST | `/environment/restart` | write PEM secrets to AM pod → project all items → `am-catalina-properties` (JVM system properties) + roll-restart am/idm; returns `{variableCount, secretCount, restarted}` |
| POST | `/environment/apply` | alias of `/environment/restart` (convenience — not part of real AIC API) |
| PUT | `/am/json/realms/root/realms/{realm}/realm-config/secrets/stores/GoogleSecretManagerSecretStoreProvider/ESV/mappings/{name}` | persists mapping in ConfigMap AND forwards to AM's `FileSystemSecretStore/ESV` — `201` / `200` |
| GET | `/am/json/realms/root/realms/{realm}/realm-config/secrets/stores/GoogleSecretManagerSecretStoreProvider/ESV/mappings/{name}` | returns stored mapping from ConfigMap |
| DELETE | `/am/json/realms/root/realms/{realm}/realm-config/secrets/stores/GoogleSecretManagerSecretStoreProvider/ESV/mappings/{name}` | deletes ConfigMap; `204` |

### RBAC

Namespaced `Role` (not `ClusterRole` — shim only touches `fr-platform`), bound to a dedicated `tenant-shim` ServiceAccount:

```yaml
rules:
  - apiGroups: [""]
    resources: ["configmaps", "secrets"]
    verbs: ["get","list","watch","create","update","patch","delete"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get","list"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get","list","watch","patch"]
```

### Verification

```sh
# Port-forward the shim
kubectl port-forward -n fr-platform svc/tenant-shim 8090:8080

# Import a real ESV export using lodestar's CLI (strongest check):
echo "faketoken" > /tmp/at.txt
python3 /path/to/lodestar/shared/scripts/tenant_util.py esv import \
  --target http://localhost:8090 \
  --file /path/to/banc/openam-perf-banc_esv-export.json \
  --apply
python3 /path/to/lodestar/shared/scripts/tenant_util.py esv list \
  --source http://localhost:8090
# Expect: HTTP 201 on first run, 200 on re-run; esv list shows correct _id/encoding/description

# Confirm projection + restart:
curl -X POST http://localhost:8090/environment/restart
kubectl rollout status deployment/am -n fr-platform

# Confirm ESV values are in catalina.properties (JVM system properties):
kubectl exec -n fr-platform deploy/am -- grep "esv\." /usr/local/tomcat/conf/catalina.properties | head -10
```

**Important gotchas:**
- Tenant shim changes require `POST /environment/restart` to take effect — writes to per-item objects are immediate and durable, but AM reads ESVs from `catalina.properties` which is only updated and loaded at pod startup; `/environment/restart` re-projects into `am-catalina-properties` and triggers the rolling restart
- `docker build` for `tenant-shim:local` must target the OrbStack docker context — see [Known Issues](#known-issues--gotchas)

---

## Operational Runbook

### Health Checks

**AM (direct pod, HTTP):**
```sh
AM_POD=$(kubectl get pod -n fr-platform -l app=am -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n fr-platform pod/$AM_POD 18080:8080 > /tmp/pf-am.log 2>&1 &
sleep 4
curl -si http://localhost:18080/am/json/health/live
kill %1 2>/dev/null
# Expected: HTTP/1.1 200 (body may be empty — normal)
```

**IDM (direct pod, HTTP):**
```sh
IDM_POD=$(kubectl get pod -n fr-platform -l app=idm -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n fr-platform pod/$IDM_POD 18180:8080 > /tmp/pf-idm.log 2>&1 &
sleep 4
curl -s http://localhost:18180/openidm/info/ping
kill %1 2>/dev/null
# Expected: {"state":"ACTIVE_READY"}
```

**Via nginx (full HTTPS flow):**
```sh
NGINX_POD=$(kubectl get pod -n ingress-nginx -l app.kubernetes.io/component=controller -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n ingress-nginx pod/$NGINX_POD 18443:443 > /tmp/pf-nginx.log 2>&1 &
sleep 3
curl -sk -D - "https://localhost:18443/am/XUI/" -H "Host: mock.iam.example.com" --max-redirs 0 | grep "HTTP/"
kill %1 2>/dev/null
# Expected: HTTP/2 200
```

### Accessing Gitea

Gitea is in-cluster only (no ingress). Access via port-forward:
```sh
kubectl port-forward -n fr-platform svc/gitea 3000:3000
```
Open `http://localhost:3000` — username `forgerock`, password `forgerock`, repo `forgerock/customer-config`.

To clone the repo locally (while the port-forward is running):
```sh
git clone http://forgerock:forgerock@localhost:3000/forgerock/customer-config.git
```

### Exporting Live AM Config to Gitea

Use this when you've made changes in the AM Admin UI and want to persist them:
```sh
kubectl port-forward -n fr-platform svc/gitea 3000:3000 &

AM_POD=$(kubectl get pod -n fr-platform -l app=am -o jsonpath='{.items[0].metadata.name}')
kubectl cp -n fr-platform ${AM_POD}:/home/forgerock/openam/config/services /tmp/am-services

git clone http://forgerock:forgerock@localhost:3000/forgerock/customer-config /tmp/customer-config-repo
cp -r /tmp/am-services/. /tmp/customer-config-repo/am/
cd /tmp/customer-config-repo && git add . && git commit -m "Export AM config from pod" && git push

kubectl rollout restart deployment/am -n fr-platform
```

**Warning:** any live AM/IDM config change (realm creation, admin UI changes) does NOT survive an AM restart unless pushed to Gitea. FBC is read-only/one-way by design. See [Known Issues](#known-issues--gotchas).

### Re-creating Alpha/Bravo Realms After AM Restart

Alpha/bravo realm REST configuration is lost on AM restart unless it was pushed to Gitea. `mock-tenant.py deploy` step 10 re-creates the realms, and the AM `postStart` lifecycle hook re-creates them on every subsequent pod start. To run a full re-deploy:
```sh
python3 bin/mock-tenant.py deploy  # runs all steps; idempotent
```
The `mock-tenant.py deploy` command is idempotent — re-running it will re-create the realms.

### DS Memory Limit for Load Testing

The `ds-idrepo` memory limit is set to 2Gi in `kustomize/overlay/mock-tenant/ds-idrepo/sts.yaml` (both requests and limits). The default PingDS limit (1366Mi) causes OOMKill under write-heavy load tests.

---

## Known Issues & Gotchas

### Deployment

- **Built-in probe delays inflate deploy time** — several components have conservative probe settings inherited from the upstream ForgeOps manifests that add unnecessary wall-clock time on a local dev cluster. Not tuned yet; revisit if further deploy time reduction is needed:
  - **AM `startupProbe`**: `failureThreshold: 40 × periodSeconds: 10` = up to 400s before Kubernetes kills the pod. In practice AM starts in ~60–90s but the probe window means `rollout status` won't clear until startup probe passes regardless.
  - **AM `readinessProbe`**: `initialDelaySeconds: 20, periodSeconds: 10` — fine as-is.
  - **IDM `startupProbe`**: same 400s window as AM.
  - **IDM `readinessProbe`**: `initialDelaySeconds: 30, periodSeconds: 30` — up to **60s dead time** after IDM is actually healthy before Kubernetes marks it ready. If anything in the deploy sequence gates on IDM readiness this is the largest tunable delay.
  - **IDM `livenessProbe`**: `initialDelaySeconds: 120` — not on the critical deploy path.
  - **Amster `AMSTER_DURATION: 10`** — the pause container in the amster Job sleeps 10s after the import init container finishes. The deploy script waits on job completion, so this adds a trivial but avoidable 10s.

- **`AM_SERVER_FQDN` alone is not enough** — must also be in `CATALINA_USER_OPTS` as `-Dam.server.fqdn=...`. The env var is consumed by the shell entrypoint but the JVM only reads system properties.

- **DS admin password is permanent** — if DS starts before mittwald populates `ds-passwords`, the password is blank and cannot be changed. Wipe PVCs and redeploy (see recovery procedure in Deploy Guide).

- **DS `:latest` tag can cause binary/config version mismatch** — if the image tag moves between bootstrap and a later pod restart, DS logs `The PingDS binary version 'X' does not match the installed configuration version 'Y'` and crash-loops. Wipe PVCs and redeploy.

- **`ds-set-passwords-job.yaml` hardcodes `imagePullPolicy: Always`** — harmless against the upstream registry but breaks when `ds` is mapped to a local image. Fixed via `kustomize/overlay/mock-tenant/ds-set-passwords/image-pull-policy.yaml`.

- **DS security settings not set at first boot if `ds:local` is not rebuilt** — `mock-tenant-config.sh` is baked into the DS image. If the script changes, rebuild `ds:local` before redeploying.

- **`docker build` may target the wrong daemon on OrbStack** — if another Docker context (e.g. Colima) is active, a plain `docker build` builds into that daemon's image store. OrbStack's Kubernetes only pulls from OrbStack's own daemon. Use `docker --context orbstack build ...` when unsure.

- **`bin/tunnel` requires sudo** — port 443 is privileged on macOS.

- **keystore-create needs internet** — downloads a static `jq` binary from GitHub releases at runtime.

### Gitea

- **Gitea `DEFAULT_ADMIN_*` env vars don't work in gitea:1.22** — admin user is created via `lifecycle.postStart` hook as `su git -c 'gitea admin user create ...'`. If the hook runs as root (not `git`), user creation silently fails and the seed job crash-loops with every API call returning `401 Unauthorized`.

### AM

- **Alpha/bravo realms and all other live AM config changes do NOT survive an AM restart** — `filesystem-init` repopulates `/fbc` from Gitea on every pod boot, discarding any in-pod changes. This is by design (FBC is read-only). Push config to Gitea before restarting AM.

- **amster only creates OAuth2 clients in the root realm** — `idm-resource-server` and `idm-provisioning` are created only in root by amster. Alpha/bravo inherit `idm-provisioning` via `am-mirror` FBC import, but the secret baked into those FBC files at mirror time will be stale after step 11 regenerates it. Step 11 in `mock-tenant.py` explicitly pushes the correct secret into all three realms.

- **amster cannot configure realm-level OAuth2 provider settings (e.g. statelessTokensEnabled)** — amster's config profile targets `realms/root` only; there is no amster equivalent for alpha/bravo realm-level service configuration. OAuth2 provider settings for alpha/bravo (such as `statelessTokensEnabled`) must be set via the gitea-seed FBC files at `kustomize/base/gitea-seed/am-conf/realm/root-alpha|root-bravo/oauth2provider/1.0/organizationconfig/default.json` and take effect on the next redeploy from scratch.

- **amster never sets the idm-resource-server OAuth2 client secret** — it always resolves to the hardcoded literal `password`. The config profile uses `&{idm.rs.client.secret|password}` and nothing sets that Java property anywhere in kustomize/charts. Step 10 in `mock-tenant.py` fixes this via AM's REST API.

- **AM's OAuth2 endpoints reject client secrets containing `+` or `/`** — `secret-generator` occasionally produces base64-alphabet secrets with these characters, which break AM Basic Auth with `invalid_client` even when the secret is otherwise correct. `mock-tenant.py` step 11 regenerates both `IDM_RS_CLIENT_SECRET` and `IDM_PROVISIONING_CLIENT_SECRET` to alphanumeric-only. Diagnostic: `{"active":false}` on `/oauth2/introspect` means client auth passed; `{"error":"invalid_client"}` means it didn't.

- **AM `PUT` for OAuth2 clients requires flat attribute object** — the grouped shape from `GET` (`coreOAuth2ClientConfig`, `advancedOAuth2ClientConfig`, etc.) is rejected with `Invalid attribute specified.` Must flatten all groups into a single object before PUT.

- **`IdentityStoreDecisionNode` is AIC-only** — see [Why IdentityStoreDecisionNode Cannot Be Used](#why-identitystoredecisionnode-cannot-be-used).

- **`identityResource` must be set on every tree containing IDM nodes** — including inner trees. Not inherited from outer trees. See [identityResource on Inner Trees](#identityresource-on-inner-trees).

- **`PatchObjectNode` (and other IDM node) instance files carry `managed/user` from the root realm** — AM validates that the node-level `identityResource` matches the tree-level one and throws `NodeProcessException: Configured identity resource for the node (managed/user) does not match the configured identity resource for the tree (managed/alpha_user)` if they differ. Node instance files mirrored from the root realm must have `identityResource` rewritten to `managed/{realm}_user`. `gitea-seed.py am-mirror` now does this automatically for `patchobjectnode`, `queryfilterdecisionnode`, `incrementlogincountnode`, and `logincountdecisionnode` instance files.

- **`/admin` URL (IDM Admin UI) is not available** — the IDM Admin UI at `/admin` was deprecated and removed from ForgeOps. Use `/platform` instead.

- **HTTP 500 on `POST /authenticate` — `UnsupportedOperationException: Output callback's value cannot be trusted from input`** *(OPEN — 2026-08-04)*

  **Symptom:** Simulation fails with HTTP 500 on `POST /am/json/realms/root/realms/alpha/authenticate`. Response body is empty (`Content-Length: 0`). AM logs show:

  ```
  java.lang.UnsupportedOperationException: Output callback's value cannot be trusted from input
    at RestAuthMetadataCallbackHandler$1.getOutputValue(RestAuthMetadataCallbackHandler.java:66)
    at RestAuthMetadataCallbackHandler.convertToJson(RestAuthMetadataCallbackHandler.java:41)
    at IntermediateTreeResult.constructCallbacks(IntermediateTreeResult.java:93)
  ```

  **Context:** The failing tree is `gd-auth_user_pin_temp` in the alpha realm. Entry node `2ea82d8c` (`gd-auth_user_pin-prep`) → `cacc0ece` (`journey_information`) → `515a45ad` (`DeviceProfileCollectorNode`). The `DeviceProfileCollectorNode` issues a `MetadataCallback` to collect device fingerprint data. AM throws when it tries to serialize the callback for the response.

  **Theory (not yet verified):** Either:
  1. `/home/forgerock/base/config/services` is being ignored — possibly the base image config is not on the classpath or FBC path, so the `DeviceProfileCollectorNode` type or v1 node config is not being found correctly; or
  2. The PVC at `/home/forgerock/openam/config/services` is missing the v1 node config for `DeviceProfileCollectorNode` — this was present from Gitea-seed but may not have been imported correctly after recent changes.

  **Recommended next step:** Redeploy from scratch (`python3 bin/mock-tenant.py deploy`) to rule out stale state accumulated across multiple incremental changes. Much has changed since the last clean deploy.

- **`LIBRARY_SCRIPT` NPE when calling `httpClient` — caused by wrong `propertyNamePrefix` and ESV injection mechanism** *(FIXED)*

  **Symptom:** `Script '...' with evaluatorVersion 2.0 terminated with exception ... Wrapped java.lang.NullPointerException (library_get_key_pinblock#34)`. Previously also preceded by `WARN: propertyName must start with [script]`.

  **Root cause — two problems in sequence:**

  1. **`systemEnv.getProperty()` returns `null` due to wrong prefix.** `PrefixedScriptPropertyResolver` validates the requested property name against a configured prefix. The default prefix in the ForgeOps AM image is `"script"`, but banc scripts call `systemEnv.getProperty("esv.service.keys.pinblock.url")` — the `"esv."` prefix check fails, the resolver logs `propertyName must start with [script]`, and returns `null`.

     The resolver prefix comes from the **calling script's context**, not `LIBRARY`. Since the caller is a v2 evaluator node, the relevant context is `SCRIPTED_DECISION_NODE`. Fix: committed `kustomize/base/gitea-seed/am-conf/realm/root/scriptingservice/1.0/globalconfig/default/scripted_decision_node/engineconfiguration.json` with `"propertyNamePrefix": "esv."` (full baseline content, not a stub — required so it wins over the base image file on the PVC path). `FBC_BASE_PATHS` lists the PVC path first so this file takes precedence.

  2. **`systemEnv.getProperty()` still returned `null` even after prefix fix** — because ESV values were injected as env vars (`esv-service-keys-pinblock-url` with dashes) via `envFrom`, but `systemEnv.getProperty("esv.service.keys.pinblock.url")` resolves against **JVM system properties** (not env vars). Dots and dashes are not interchangeable — `System.getenv("esv.service.keys.pinblock.url")` returns null on Linux because dots are not valid in env var names. (In AIC, `secrets-loader` materialises ESVs into a properties file that is loaded as system properties at AM startup.)

     Fix: the tenant shim's `do_restart()` now writes all ESV values into the `am-catalina-properties` ConfigMap as `esv.foo.bar=value` entries (translating `esv-foo-bar` key names, escaping values for Java `.properties` format to handle large values like JSON blobs and key pairs). The AM Deployment mounts this ConfigMap at `/usr/local/tomcat/conf/catalina.properties` via `subPath`. Tomcat loads `catalina.properties` into JVM system properties at bootstrap — making all ESV values available to `systemEnv.getProperty()` under the correct dot-notation keys.

  **Files changed:**
  - `kustomize/base/gitea-seed/am-conf/realm/root/scriptingservice/1.0/globalconfig/default/scripted_decision_node/engineconfiguration.json` — new, `propertyNamePrefix: "esv."`
  - `kustomize/overlay/mock-tenant/am/catalina-properties-cm.yaml` — new ConfigMap with base `catalina.properties` content; tenant shim appends ESV entries on every restart
  - `kustomize/overlay/mock-tenant/am/deployment.yaml` — mounts `am-catalina-properties` at `/usr/local/tomcat/conf/catalina.properties` via `subPath`; removed `esv-variables`/`esv-secrets` `envFrom` entries (no longer needed)
  - `docker/tenant-shim/app/main.py` — `do_restart()` projects ESVs into `am-catalina-properties` instead of `esv-variables`/`esv-secrets`

### DS

- **`am-identity-bind-account` lacks `unindexed-search` DS privilege by default** — without this privilege, AM's browse searches on `o=alpha,o=root,ou=identities` are rejected with `Insufficient Access Rights: unindexed search`. Fixed in `docker/ds/runtime-scripts/ds-idrepo/post-init` via `ldapmodify` (adds `ds-privilege-name: unindexed-search` to `uid=am-identity-bind-account,ou=admins,ou=identities`; idempotent). AIC production doesn't need this because it has all saas indexes.

- **`ds-idrepo` memory limit** — set to 2Gi in `kustomize/overlay/mock-tenant/ds-idrepo/sts.yaml`; see [DS Memory Limit for Load Testing](#ds-memory-limit-for-load-testing).

### IDM

- **`managed.json` gitea-seed uses `--server-side` apply** — the file is 323KB, exceeding the 262KB `kubectl.kubernetes.io/last-applied-configuration` annotation limit. Standard `kubectl apply` will fail; `mock-tenant.py` uses `--server-side`.

- **AM and IDM `fbc` PVCs use `ReadWriteOnce` — do not scale past 1 replica** — both `am-fbc` and `idm-fbc` PVCs are provisioned with `accessModes: ReadWriteOnce`, which only allows a single node to mount the volume at a time. Scaling `Deployment/am` or `Deployment/idm` to more than one replica would cause multiple processes writing to the same filesystem directory simultaneously, risking config file corruption and undefined startup behaviour. This stack is single-replica by design; do not raise the replica count without first replacing the PVCs with a `ReadWriteMany`-capable storage class or switching to a different persistence strategy.

### Tenant Shim

- **Tenant shim changes require `POST /environment/restart` to take effect** — writes are immediately durable to per-item objects, but AM/IDM read via `envFrom` from the aggregate projection objects. `/environment/restart` re-projects and roll-restarts.

- **`GET /environment/secrets/{_id}` never returns the secret value** — metadata only, matching real AIC.

### `lodestar-mock-api` overlay — SAC CRD not installed

**Symptom:** Lodestar's `forgeops apply` against `kustomize/overlay/lodestar-mock-api/` fails with:

```
error: resource mapping not found for name: "forgerock-sac" namespace: "lodestar-mock-api"
no matches for kind "SecretAgentConfiguration" in version "secret-agent.secrets.forgerock.io/v1alpha1"
ensure CRDs are installed first
```

**Why this did not affect `mock-tenant.py deploy`:** `mock-tenant.py` never applies the top-level overlay as a whole. It calls `bin/forgeops apply -e mock-tenant -n fr-platform <component>` per component (and a handful of direct `kubectl apply -k` for sub-overlays like `tls/`, `keystore-create/`). ForgeOps' own CLI resolves each component to its individual sub-directory (e.g. `kustomize/overlay/mock-tenant/am/`) — it does not walk through `kustomize/overlay/mock-tenant/kustomization.yaml` and therefore never reaches `secrets/kustomization.yaml → secrets/secret-agent/ → base/secrets/secret-agent/secret-agent-config.yaml` where the `SecretAgentConfiguration` CRD object lives.

Lodestar, on the other hand, applies `kustomize/overlay/lodestar-mock-api/` as a single top-level overlay, which causes kustomize to resolve the full resource tree including the SAC CRD reference.

**Fix:** `kustomize/overlay/lodestar-mock-api/` now points to `secret-generator` instead of `secret-agent` in all four kustomizations (`secrets/`, `am/`, `idm/`, `amster/`). `secret-generator` uses plain Kubernetes Secrets generated by the mittwald `kubernetes-secret-generator` operator (already installed by `bootstrap`), with no CRD dependency.

---

## TODO

### 12. Replace AM FBC PVC with emptyDir

The AM FBC PVC (`am-fbc-pvc`, `kustomize/overlay/mock-tenant/am/am-fbc-pvc.yaml` + `fbc-pvc-patch.yaml`) was introduced to persist `/home/forgerock/openam/config/services` across AM pod restarts so that live config changes (journeys, nodes) weren't lost. Now that `do_restart()` mirrors the live AM FBC back to Gitea before every restart, `filesystem-init` always repopulates from an up-to-date source — the PVC's persistence role is redundant.

AM originally used `emptyDir` for this volume. Reverting to `emptyDir` simplifies the stack: no storage class dependency, no PVC provisioning delay during deploy, and no risk of stale volume data from a previous deployment bleeding through.

**Caveat — crash resilience:** The Gitea mirror only runs inside `do_restart()`, which is triggered by `POST /environment/restart`. If AM crashes (OOM, node eviction, etc.) and Kubernetes restarts the pod directly, `filesystem-init` repopulates from Gitea — which only reflects the state at the last mirror. Any live config changes made after the last `POST /environment/restart` (e.g. journeys imported without a subsequent ESV restart) would be lost. With the PVC those changes survive because the volume persists independently of the pod lifecycle. This item should only be actioned if a separate mechanism is added to trigger a mirror on every live AM config change, not just on ESV restart.

**Possible mechanism — FBC watcher sidecar:** A sidecar in the AM pod could watch the FBC directory (`/home/forgerock/openam/config/services`) using `inotifywait` (kernel-level inotify, not polling) in recursive mode with `-e close_write`, and on detecting any write debounce via a timer — reset the timer on each event, and only when a quiet period expires (e.g. 10 seconds of no new writes) make a single call to a dedicated `POST /config/mirror` endpoint on the tenant shim. That endpoint would do only the Gitea push step, without the `catalina.properties` rebuild or the AM restart. AM can write many files during a journey import; debouncing means only one mirror call goes out at the end of the burst. No restart, no ESV projection, just the Gitea push.

**Files to change:**
- Remove `kustomize/overlay/mock-tenant/am/am-fbc-pvc.yaml`
- Remove `kustomize/overlay/mock-tenant/am/fbc-pvc-patch.yaml`
- Remove both from `kustomize/overlay/mock-tenant/am/kustomization.yaml`
- The base `kustomize/base/am/` volume definition already uses `emptyDir` — no base change needed

### 9. Reduce Deploy Time Below 5 Minutes

Current deploy time is ~6m20s (down from 7m30s). Target: under 5 minutes. Ideas ranked by expected saving:

**a. Parallelise DS + keystore + TLS (~60–90s)**
`_step_deploy_ds()`, `_step_deploy_keystore()`, and `_step_deploy_tls()` are sequential but fully independent. DS dominates (~2–3 min). Running keystore and TLS concurrently with DS using Python threads or backgrounded subprocesses would recover their wall-clock time.

**b. ~~Apply AM/IDM/UI manifests earlier~~ — N/A**
AM and IDM both connect to DS at startup and will crash-loop until DS is ready. On a local OrbStack cluster with images already cached, there is no meaningful overlap to exploit — the real wait is always the `rollout status deployment/am` gate in step 11, which is gated on DS being healthy. Not worth implementing.

**c. Seed `access.json` and `teammember.js` via gitea-seed Job, eliminating the force-restart (~90s)**
Step 14 (`push-config --force-restart`) unconditionally restarts IDM (~90s) to pick up `access.json` and `teammember.js`, which the gitea-seed Job doesn't currently seed. If both files were added to the seed Job/ConfigMap, IDM would have them on first boot and step 14 would find nothing new — the restart could be skipped when nothing changed.
- `access.json` adds `health/*` open access (needed for the step 13 health check) and removes a `selfservice/user/*` patch rule from the image default.
- `teammember.js` doesn't exist in the base image at all — it's a hard dependency for the `teammember` managed object, which calls `require('teammember')` in its `onCreate`, `onUpdate`, and `postUpdate` hooks. Any create/update on a `teammember` object will fail with a script-not-found error if the script isn't in Gitea when IDM boots.
- The seed Job would need to handle `idm/script/` as a second destination path alongside `idm/conf/`.

**Status (2026-08-03):** Fix implemented — both files are now seeded via the gitea-seed Job (`kustomize/base/gitea-seed/`). Note: this may be reverted in the future. The original ConfigMap pattern was intentional for files that are useful to modify at runtime (updating the ConfigMap + restarting the pod is a clean operator workflow). `access.json` and `teammember.js` don't currently need runtime modification, but if that changes they should be moved back to ConfigMaps rather than kept in the gitea-seed path.

**d. Remove unconditional sleep in `_step_verify_fbc()` (~4s, trivial)**
Line 692 has `time.sleep(4)` that could be replaced with a poll loop.

### 1. FBC Write-Back

Currently FBC is one-way: Gitea → pod. Any config change made via the AM/IDM Admin UI or REST API lands in the running pod's `/fbc` filesystem but is never written back to Gitea. On the next pod restart, `filesystem-init` re-clones from Gitea and the change is lost.

This affects everything — realm creation, tree edits, OAuth2 client changes, IDM config changes — not just the alpha/bravo realm workaround documented in the runbook.

**Design intent:** Gitea is a runtime persistence layer for the lifetime of a development use case — it keeps config alive across pod restarts within a single deployment. It is **not** a mechanism to write config back to the local repo on disk. Writing back to disk would pollute the baseline with use-case-specific config that is not relevant to other use cases. The local repo (`kustomize/base/gitea-seed/`) holds curated, use-case-agnostic baseline config; Gitea holds the live runtime state for the current deployment.

**Approaches to investigate:**

1. **`sync-config` subcommand in `mock-tenant.py`** — `kubectl cp`s the relevant config directories out of the running AM/IDM pod and pushes them directly to Gitea via git, so the change survives the next pod restart. Operator runs it on demand after making changes via the UI or REST. Simple, consistent with the existing `push-config` pattern, no new infrastructure.

2. **On-demand push endpoint (extend tenant shim)** — add an endpoint (e.g. `POST /config/save`) that exports the pod's current `/fbc` config tree and pushes it to Gitea. Same trigger model as `/environment/restart` — no CLI needed, callable via curl.

3. **Automated sidecar (config-saver)** — a container that watches the AM/IDM `/fbc` directory for changes and continuously syncs to Gitea. Covers every change automatically including unscripted UI edits. More complex: requires conflict resolution for partial/transient writes, and adds a standing process that this project's phase 1 deliberately scoped out.

4. **PVC for `/home/forgerock/openam`** — mount a PersistentVolumeClaim at `/home/forgerock/openam` (AM's working directory where FBC config, keystore, and runtime state live). Any write made via the UI or REST API would land on the PVC and survive pod restarts with no operator action. Trade-offs to investigate: whether `filesystem-init` (which clones from Gitea into `/fbc`) would conflict with a pre-populated PVC on first boot; whether the PVC contents can diverge from Gitea in ways that are hard to reason about; and whether this approach extends naturally to IDM (whose working directory is different).

Option 1 is likely the right starting point — simple, on-demand, no new infrastructure. Option 4 (PVC) would eliminate the write-back problem entirely at the cost of Gitea no longer being the authoritative runtime state.

**Relationship to deploy-step REST config and the dual-maintenance pattern:**

Several `mock-tenant.py deploy` steps create AM config via REST (step 10 — realms, step 10b — `FileSystemSecretStore/ESV`, step 11b — PKCE client). For those changes to survive a bare AM restart, equivalent FBC JSON files must also exist in `kustomize/base/gitea-seed/am-conf/` — so any REST-created config currently requires maintenance in two places. This is the dual-maintenance burden described in [REST API vs Gitea FBC](#rest-api-vs-gitea-fbc--two-complementary-mechanisms).

Write-back to Gitea would eliminate this: REST steps in deploy would create config once, write-back would push it to Gitea's runtime state, and the static FBC files could be removed. Note that this still requires REST steps to run on every fresh `deploy --force` since the Gitea seed is reset from the static baseline each time — write-back only helps within the lifetime of a deployment, not across redeployments.

The current `_mirror_am_to_gitea()` in the tenant shim already does partial write-back on every `POST /environment/restart`, but only for `realm/root-alpha` and `realm/root-bravo`. Expanding its scope to cover the full config tree would close the gap without new infrastructure.

### 2. ~~Persist `unindexed-search` DS Privilege~~ — DONE

Fixed in `docker/ds/runtime-scripts/ds-idrepo/post-init`: `ldapmodify` adds `ds-privilege-name: unindexed-search` to `uid=am-identity-bind-account,ou=admins,ou=identities` after every fresh PVC init. The `|| true` makes it idempotent on pod restarts.

### 3. ~~Persist `ds-idrepo` Memory Limit for Load Testing~~ — DONE

`kustomize/overlay/mock-tenant/ds-idrepo/sts.yaml` sets `requests/limits: memory: 2Gi` on the `ds` container.

### 4. Make `PLATFORM_FQDN` a Single Source of Truth

`PLATFORM_FQDN` is defined in `bin/mock-tenant.py` but the kustomize files all have `mock.iam.example.com` hardcoded independently. Changing the FQDN currently requires editing ~20 files across `kustomize/overlay/mock-tenant/`, plus `bin/get_admin_tok.sh`, `bin/tunnel`, `docker/gatling/*/util.scala`, and `/etc/hosts`.

**Likely path:** simple find-and-replace rename to `mock.iam.example.com` — one `sed` across the repo, one `/etc/hosts` edit, one line in lodestar config. See file list below.

**Longer-term path (kustomize-native):** `platform-config.yaml` already has `FQDN` and `AM_SERVER_FQDN`. Most of the hardcoding can be eliminated without a rename:
- `-Dam.server.fqdn=` in `CATALINA_USER_OPTS` is redundant — AM reads `AM_SERVER_FQDN` from `envFrom: platform-config` directly; remove it from `CATALINA_USER_OPTS`
- The `postStart` hook already has `$PLATFORM_FQDN` in scope via `envFrom` — replace the hardcoded hostname there
- Ingress patches and TLS cert can be driven by kustomize `replacements` sourced from `platform-config.yaml`

After that, only `platform-config.yaml` (one file) needs editing to change the FQDN.

Files that need updating when FQDN changes (current state):
- `kustomize/overlay/mock-tenant/base/platform-config.yaml` — `FQDN` and `AM_SERVER_FQDN`
- `kustomize/overlay/mock-tenant/am/deployment.yaml` — `-Dam.server.fqdn=` in `CATALINA_USER_OPTS` (redundant once above fix applied) and `postStart` hook
- All `ingress-fqdn.yaml` files under `kustomize/overlay/mock-tenant/` (am, idm, tenant-shim, login-ui, admin-ui, end-user-ui)
- `kustomize/overlay/mock-tenant/tls/certificate.yaml` — TLS SAN `dnsName`
- `bin/mock-tenant.py` — `PLATFORM_FQDN` constant
- `bin/get_admin_tok.sh` — `TENANT` variable
- `bin/tunnel` — echo labels
- `docker/gatling/*/util.scala` — `TARGET_HOST` default
- `/etc/hosts` on the laptop
- `/Users/wajih.ahmed/work/qa-lodestar-fork-dev/config/config.yaml` — `fqdnOverride`

### 6. Retire ConfigMap-based Gitea Seed in Favour of push-config

Currently Gitea seeding is split between the `gitea-seed` Kubernetes Job (which creates the repo and writes a subset of files via ConfigMaps) and `push-config` (which writes the rest). The long-term goal is to have a single, simpler path:

- Enhance `push-config` (or a new `init-config` subcommand) to create the Gitea repo via the API if it does not exist, then push all config files in one step.
- Keep `managed.json` and `repo.ds.json` in their ConfigMaps — the ConfigMap pattern is correct for these since updating the ConfigMap and restarting the pod ensures all replicas pick up the same config consistently.
- Move `webserver.listener-http.json`, `cluster.json`, and `teammember.js` out of the seed scripts ConfigMap and into the local repo (e.g. `kustomize/base/gitea-seed/idm-conf/`), so they can be updated independently of `seed.sh` and pushed via `push-config`.
- Move `access.json` out of its ConfigMap and into the repo as well — it does not benefit from the ConfigMap pattern and is better tracked in git.
- Clean up the `alpha-opendj.json` / `bravo-opendj.json` wiring — they are already in `am-conf/` in the repo but are redundantly also mounted via ConfigMap.
- Retire the `gitea-seed` Job and its associated ConfigMaps once the above is in place.

This simplifies the deploy flow, removes the ConfigMap size constraint on config files, and makes all config visible and editable in the local repo.

### 7. ~~Refactor Branch as a True Kustomize Overlay~~ — DONE

Committed in `7e486a062`. All forgeops-owned base and `overlay/default` files reverted to master state; all customisations migrated into `kustomize/overlay/mock-tenant/` as Kustomize patches. Net effect on the deployed cluster is zero — `kubectl kustomize kustomize/overlay/mock-tenant/` produces identical manifests.

**What was done:**

- **13 base files reverted to master**: am/idm deployments (init container `custom-vol-init` back to image default), am/idm ingresses (`secretName` back to `tls-identity-platform.domain.local`), am services (port back to `https`), admin-ui ingress (`/admin` path removed), idm ingresses (`/admin` path restored), amster jobs (secret key back to `id_rsa`).
- **overlay/default fully reverted to master**: 14 modified files reverted, 10 branch-added files deleted.
- **mock-tenant patches added/updated**:
  - `am/deployment.yaml`: `custom-vol-init` strategic merge patch — image `config-loader:local`, command `clone-and-copy`, Gitea env vars (`JSON_MERGE am/services`); `CATALINA_USER_OPTS`; `FBC_BASE_PATHS` (PVC path first so gitea-seed overrides base image); `postStart` lifecycle hook that re-creates alpha/bravo realms on every pod start; `catalina-properties` volume mount at `/usr/local/tomcat/conf/catalina.properties` (subPath) for ESV injection.
  - `idm/deployment.yaml`: `custom-vol-init` strategic merge patch — image `config-loader:local`, command `clone-and-copy`, Gitea env vars (`JSON_REPLACE idm`); `esv-variables`/`esv-secrets` envFrom.
  - `am/ingress-fqdn.yaml` / `idm/ingress-fqdn.yaml`: `op: replace secretName: platform-tls`.
  - `admin-ui/ingress-fqdn.yaml`: `op: replace secretName: platform-tls` + `op: add /admin path`.
  - `idm/ingress-fqdn.yaml`: `op: remove /admin path`.
  - `am/service.yaml`: `op: replace` port `targetPort/name: http`.
  - `amster/amster-job.yaml`: strategic merge patch for `ssh-privatekey` secret key.
  - `ds-cts/sts.yaml` / `ds-idrepo/sts.yaml`: `storageClassName: local-path`; `imagePullPolicy: IfNotPresent`; `ds-idrepo` memory 2Gi.
  - `ds-set-passwords/image-pull-policy.yaml`: `imagePullPolicy: IfNotPresent` for `ds:local`.

**Intentional divergences kept (docker/ds — cannot be Kustomize overlaid):**
- `docker/ds/Dockerfile` — mock-tenant RUN steps
- `docker/ds/ldif-ext/identities/orgs.ldif` — alpha/bravo OU entries
- `docker/ds/runtime-scripts/ds-cts/setup` and `ds-idrepo/setup` — saas-aligned indexes/setup
- `docker/docker-bake.hcl` — additive: config-loader/tenant-shim build targets

### 5. ~~Restore `/admin` URL (IDM Admin UI)~~ — N/A

The IDM Admin UI at `/admin` was deprecated and removed from ForgeOps. Not fixable — use `/platform`.

### 10. Permanent Fix for `gcr.io/engineeringpit` Image Pull Auth

Pods that reference `gcr.io/engineeringpit/lodestar-images/...` images (e.g. IG in the `lodestar-mock-api` overlay) fail with an unauthenticated pull error because the OrbStack cluster has no GCR credentials. The current workaround is `imagePullPolicy: IfNotPresent` + a manual `docker pull` before each deploy.

**Approaches to investigate:**

1. **Automate the pre-pull in `mock-tenant.py`** — add a deploy step that runs `docker pull <image>` for each `gcr.io/engineeringpit` image referenced by the active overlay before applying it. Reads the image tags from the overlay's `image-defaulter/kustomization.yaml` so it stays in sync automatically.

2. **`imagePullSecret` refreshed from gcloud token** — create a docker-registry secret from `gcloud auth print-access-token` and patch the `default` ServiceAccount in `lodestar-mock-api` to use it. Token expires every hour so this would need to be re-run before each deploy; could be wired into the deploy script.

3. **Workload Identity / long-lived credential** — provision a GCP service account with `artifactregistry.reader` on the `engineeringpit` project, create a long-lived JSON key, and store it as a persistent `imagePullSecret` in the namespace. Would not require refreshing but involves managing a GCP credential.

Option 1 is the lowest-friction path — no credential management, consistent with the existing `IfNotPresent` pattern, and can be added as a pre-deploy step to `mock-tenant.py`.

### 11. ~~Rename `esv-shim` to `tenant-shim`~~ ✓ DONE

Completed 2026-08-10. All directories, image tags, YAML filenames, Kubernetes resource names, and string references renamed from `esv-shim` / `ESV Shim` to `tenant-shim` / `Tenant Shim`.

### 8. Research: Get `IdentityStoreDecisionNode` into ForgeOps AM

`IdentityStoreDecisionNode` is AIC-only and does not exist in the ForgeOps AM image. Using it causes `IllegalArgumentException: Unsupported node type IdentityStoreDecisionNode` — see the [Known Issues section](#why-identitystoredecisionnode-cannot-be-used) for the current workaround (`DataStoreDecisionNode` + `identityResource` on the tree).

Research whether the node can be introduced into ForgeOps AM (e.g. via a custom auth node jar, or by identifying which AIC AM jar contains it and adding it to the image). If feasible, this would allow closer parity with AIC Login trees without the `identityResource` workaround.

---

## mTLS Client Certificate — How It Works (and What Broke It)

This section documents the investigation that produced `ssl_verify=SUCCESS` in the mock-api nginx logs on 2026-08-10. The fix required understanding four distinct layers of AM internals.

### The Goal

AM makes outbound HTTPS calls to mock-api (e.g. `POST /mocks-ciam/ciam/pin/api/v1/verify`). mock-api nginx requires a valid client certificate (`ssl_verify_client optional; if ($ssl_client_verify != SUCCESS) { return 403; }`). AM must present the cert via mTLS. The symptom of failure was `ssl_verify=NONE ssl_dn="-"` in nginx access logs, and a 403 response.

### How AM Resolves the mTLS Client Certificate

The journey script calls `httpClient.send(url, { clientName: "wxa-client-mtls-cert" })`. AM looks up the `httpclient` service instance `wxa-client-mtls-cert`, which has `mtlsClientCertSecretPurpose: "wxaclientcrtmtls"`. The resolution chain from there:

1. **`@SecretPurpose` annotation** — the `mtlsClientCertSecretPurpose` field on `HttpClientInstance` carries `@SecretPurpose("am.services.httpclient.mtls.clientcert.%s.secret")`. AM substitutes the configured value (`wxaclientcrtmtls`) to form the full **secretId**: `am.services.httpclient.mtls.clientcert.wxaclientcrtmtls.secret`.

2. **`FileSystemSecretStore` filename** — AM looks for a file named exactly `am.services.httpclient.mtls.clientcert.wxaclientcrtmtls.secret` in the store's configured directory (`/home/forgerock/openam/esv-secrets/`). The `OrderedStableIdResolver` matches filenames against `<secretId>(<versionSuffix>\d+)?$` — so the filename must be the full secretId, not the short purpose label. Files named `wxaclientcrtmtls` or `esv-mtls-client-cert-wajih-dev4` are ignored.

3. **`PEM` format, not `PLAIN`** — the store must be configured with `"format": "PEM"`. With `PLAIN`, AM reads the bytes as a generic secret and cannot parse the certificate/key structure needed to build an `X509ExtendedKeyManager`. The file content must be a PEM bundle containing both the private key (`-----BEGIN RSA PRIVATE KEY-----`) and the certificate (`-----BEGIN CERTIFICATE-----`), concatenated in a single file.

4. **`RefreshedX509ExtendedKeyManager` is initialized at startup** — `HttpClientService.createClient()` is called lazily (Guava `LoadingCache`) on the first request to a named instance. It calls `secretsSupplier.get().getKeyManager(purpose)` which constructs a `RefreshedX509ExtendedKeyManager`. That constructor calls `refreshKeyManager.get()` immediately and caches the result. If the cert file does not exist at that moment, the `KeyManager` holds an empty/null state. `setNeedsReload()` is only triggered by `SecretLabelListener.secretStoreMappingHasChanged()` — which fires when a secret store **mapping** changes. Because `FileSystemSecretStore` has no mappings (the `/mappings` REST sub-resource returns 404 for this store type), a store config change (e.g. format update) never triggers a reload. **An AM restart is required** to pick up cert files that were written after the service first loaded.

### What Was Wrong and What Fixed It

| # | Problem | Fix |
|---|---------|-----|
| 1 | Format was `PLAIN` — AM could not parse the PEM bundle as a key+cert | Changed `"format"` to `"PEM"` in both `esv.json` (global store, Gitea) and the new alpha realm store FBC file |
| 2 | Files were named after the short alias (`wxaclientcrtmtls`, `esv-mtls-client-cert-wajih-dev4`) — AM looked for the full secretId | Wrote files named `am.services.httpclient.mtls.clientcert.wxaclientcrtmtls.secret` and `am.services.httpclient.mtls.clientcert.clientcrtmtls.secret` into the PVC |
| 3 | `esv-secrets` was an `emptyDir` — files were lost on every AM restart, so the `KeyManager` always initialized with an empty store | Replaced with a dedicated 10Mi PVC (`am-esv-secrets`) mounted at `/home/forgerock/openam/esv-secrets/`; files now survive indefinitely |
| 4 | `RefreshedX509ExtendedKeyManager` cached an empty `KeyManager` on first load (before files existed); no mapping-change event ever triggered a reload | AM restart after the correct files were in place caused `createClient()` to re-initialize with the cert present |

### What Is Still Manual

PEM file writes are now automated: `_write_pem_secret_to_am()` in `docker/tenant-shim/app/main.py` reverse-looks up the full secretId from mapping ConfigMaps and writes the file with the correct name at `do_restart()` time. Files survive on the PVC across AM restarts.

### FBC Files

The `FileSystemSecretStore/ESV` store is now seeded via two FBC files checked into `kustomize/base/gitea-seed/am-conf/`:

- `realm/root/filesystemsecretstore/1.0/globalconfig/default/esv.json` — global scope, `format: PEM`, `directory: /home/forgerock/openam/esv-secrets`
- `realm/root-alpha/filesystemsecretstore/1.0/organizationconfig/default/esv.json` — alpha realm scope, same format and directory

The alpha realm store is required because the `httpclient` service is realm-scoped and AM resolves secrets in realm context first. Both stores point at the same PVC directory.

Step 10b in `mock-tenant.py` creates these stores via REST on a fresh deploy (before Gitea config is pushed); the Gitea FBC files ensure they persist through subsequent restarts.
