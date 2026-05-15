# ForgeOps — FBC Dev Stack Project

## What We Are Building

A branch of ForgeOps that adds **File Based Configuration (FBC)** — meaning AM and IDM load their configuration from a local git repository at pod startup rather than from config baked into the Docker image.

The `bin/forgeops` CLI (already in this repo) is the deployment tool. No standalone Python script is needed. The branch modifies the kustomize manifests and adds a `config-loader` component; `forgeops apply` and `forgeops build` continue to work as the user-facing interface.

The target is a `fr-platform` namespace on a **local Kubernetes cluster** (OrbStack). Config loading is **read-only** at pod startup — no config-saver / round-trip back to git needed.

All work lives on branch **`wajih-fbc-local`**.

---

## Background and Context

This work originated from an investigation of the `ForgeCloud/saas` monorepo (at `/Users/wajih.ahmed/source/github.com/ForgeCloud/saas`). That repo is the full production SaaS platform for Ping Identity, deeply coupled to GCP (GKE Workload Identity, Google Secret Manager, Google Cloud Source Repositories, GCP-specific StorageClasses). It is not directly deployable to a non-GKE cluster without significant rework.

**Why ForgeOps instead of the saas repo:**
- ForgeOps is the public open-source base — no GCP coupling, plain Kubernetes Secrets, standard StorageClasses
- Already uses Kustomize, same products (AM, IDM, DS), same image base
- Already has an FBC init-container hook point (see below)
- Full investigation notes live in `/Users/wajih.ahmed/source/github.com/ForgeCloud/saas/CLAUDE.md`

---

## Kubernetes Runtime: OrbStack

**OrbStack** is used instead of Colima. OrbStack provides a stable Kubernetes API on `127.0.0.1:26443` with no SSH tunnel and no socket_vmnet complications. The OrbStack node IP (`192.168.139.2`) is still blocked by CrowdStrike, but the Kubernetes API and `kubectl port-forward` both run over loopback (`127.0.0.1`) which is unaffected.

- Start OrbStack from the macOS menu bar or `open -a OrbStack`
- Kubernetes context name: `orbstack`
- Switch to it: `kubectl config use-context orbstack`

Colima was investigated earlier but abandoned due to SSH tunnel instability and socket_vmnet entitlement issues on a corporate laptop. Do not attempt to use Colima for this project.

---

## FBC: What It Is and Why It Matters

**File Based Configuration (FBC)** means AM and IDM configuration is stored as JSON files in a git repository, not in a database or baked into the Docker image. At pod startup, an init container clones the git repo and copies the relevant config files into a shared volume before the main container starts.

**Benefits:**
- Config changes are version-controlled in git
- Config can be updated without rebuilding Docker images (just commit to git, restart pod)
- Multiple environments can share the same image with different config repos/branches

---

## ForgeOps Current Config Model (What Exists Today)

ForgeOps does NOT use FBC by default — configs are baked into Docker images via a `CONFIG_PROFILE` build arg. However, it already has the FBC infrastructure:

**Existing init container chain (AM and IDM):**
```
1. custom-vol-init   — reads from ConfigMap at /config/config → copies to 'custom' emptyDir
2. filesystem-init   — merges 'custom' + base image files → 'fbc' emptyDir
3. truststore-init   — imports PEM certs into JKS truststore → 'new-truststore' emptyDir
   ↓
Main container mounts 'fbc' as the live config directory
```

**Key volumes already defined:**
- `fbc` (emptyDir) — the live config volume, mounted by main container
- `custom` (emptyDir) — staging area between init containers

**`custom-vol-init` is the injection point** — this is what we replace with `config-loader`.

**DS is different:** Uses `setup-profile` commands at first init (data persists in PVC). Not FBC-managed. No changes needed for phase 1.

**Important:** The default config profiles (`docker/am/config-profiles/default/` and `docker/idm/config-profiles/default/`) are **empty placeholders** — real base config is baked into the upstream images (`us-docker.pkg.dev/forgeops-public/images/am` and `idm`). `filesystem-init` copies this image-baked config into the `fbc` volume, then overlays anything in the `custom` volume on top.

---

## Implementation: What Was Built

### config-loader image (shell + Alpine)

Rather than modifying the Go `config-loader` from the saas repo (which has GCP OAuth2 hard-coded), a clean replacement was written:

- No GCP dependencies
- No Go compilation required
- ~60 lines of shell

**Files:**
```
docker/config-loader/Dockerfile          — Alpine 3.19 + git + jq
docker/config-loader/clone-and-copy.sh   — the loader script
```

**Env vars:**
```
GIT_URL               — full repo URL (required)
GIT_TOKEN             — optional bearer token embedded into HTTPS clone URL
GIT_PATH              — local clone destination inside the container (required)
CONFIG_SRC_PATH       — subdirectory in repo: am / idm (required)
DESTINATION_PATH      — where to copy files (required)
CONFIG_LOAD_STRATEGY  — JSON_MERGE (AM) or JSON_REPLACE (IDM) (required)
BRANCH                — git branch to clone (default: master)
```

**Strategies:**
- `JSON_REPLACE` — plain `cp` of all files from src to dst (IDM)
- `JSON_MERGE` — `jq -s '.[0] * .[1]'` deep merge per JSON file; non-JSON files copied straight (AM)

### AM and IDM manifests

Replaced `custom-vol-init` with `load-config-clone` in all four base deployment files:
- `kustomize/base/am/secret-generator/am-deployment.yaml`
- `kustomize/base/am/secret-agent/am-deployment.yaml`
- `kustomize/base/idm/secret-generator/idm-deployment.yaml`
- `kustomize/base/idm/secret-agent/idm-deployment.yaml`

Overlay deployment patches (`kustomize/overlay/default/am/deployment.yaml` and `idm/deployment.yaml`) reference `load-config-clone` (not the old `custom-vol-init` name).

### AM service fix (critical)

The base AM service had `targetPort: https` (port 8081, AM's HTTPS port). Changed to `targetPort: http` (port 8080, AM's HTTP port) in both:
- `kustomize/base/am/secret-generator/am-service.yaml`
- `kustomize/base/am/secret-agent/am-service.yaml`

Without this fix, nginx routes all traffic to AM's HTTPS port and gets TLS handshake errors.

### AM server URL fix (critical)

AM stores its own server URL (`am.server.fqdn`) in config on first bootstrap. Without correct JVM properties, it defaults to the Kubernetes service name `am` and issues redirects to `https://am/am/XUI/` which the browser cannot reach.

Two changes were made to fix this:

1. **`kustomize/overlay/default/base/platform-config.yaml`** — added `AM_SERVER_FQDN: "prod.iam.example.com"` (was missing from the overlay; base had `identity-platform.domain.local`)

2. **`kustomize/overlay/default/am/deployment.yaml`** — added `CATALINA_USER_OPTS` env var to the `openam` container:
   ```yaml
   env:
   - name: CATALINA_USER_OPTS
     value: "-Dam.server.protocol=https -Dam.server.fqdn=prod.iam.example.com -Dam.server.port=443"
   ```
   This passes the correct values as JVM system properties. The `AM_SERVER_FQDN` env var alone is NOT sufficient — it must also be passed to the JVM via `CATALINA_USER_OPTS`.

### AM and IDM ingress

Both AM and IDM ingresses have:
- `ingressClassName: nginx`
- TLS enabled with `secretName: platform-tls`
- FQDN patched to `prod.iam.example.com` via `ingress-fqdn.yaml` JSON patches

Files:
- `kustomize/base/am/secret-agent/am-ingress.yaml`
- `kustomize/base/am/secret-generator/am-ingress.yaml`
- `kustomize/base/idm/secret-agent/idm-ingress.yaml`
- `kustomize/base/idm/secret-generator/idm-ingress.yaml`
- `kustomize/overlay/default/am/ingress-fqdn.yaml`
- `kustomize/overlay/default/idm/ingress-fqdn.yaml`

### TLS certificate

A cert-manager `ClusterIssuer` (self-signed) and `Certificate` resource issue the `platform-tls` Secret used by both ingresses:
- `kustomize/overlay/default/tls/certificate.yaml`
- `kustomize/overlay/default/tls/kustomization.yaml`

Must be applied before AM/IDM are deployed.

### nginx ingress controller

ForgeOps manifests use `ingressClassName: nginx` but do NOT ship the nginx ingress controller. It must be pre-installed as a cluster prerequisite:
```sh
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.hostNetwork=true \
  --set controller.kind=DaemonSet \
  --set controller.service.type=ClusterIP \
  --wait
```
`hostNetwork=true` is required so nginx binds to the node's IP on ports 80/443 (no cloud load balancer on local k8s).

### Gitea in-cluster git server

Single Gitea pod serves the `forgerock/customer-config` repo at `http://gitea.fr-platform.svc.cluster.local:3000`.

Key implementation detail: Gitea's s6 init supervisor must start as root. The pod has **no** `runAsUser` set. An `init-dirs` init container (running as root) pre-creates `/data/git/.ssh` and `/data/gitea` with ownership `1000:1000` before Gitea starts.

Admin user is created via a `lifecycle.postStart` hook running `gitea admin user create` — the `DEFAULT_ADMIN_*` env vars do NOT work in gitea:1.22.

### Gitea seed job

A one-time Job (`gitea-seed`) uses the Gitea REST API (via `curl`) to create the `forgerock/customer-config` repo, then `git push` the initial AM and IDM config stubs.

Key implementation details:
- Uses `curl -u user:pass` (not `wget` — Alpine BusyBox wget lacks `--user`/`--password`)
- Uses `alpine:3.19` image (not `alpine/git` which has git as its ENTRYPOINT)
- All curl calls use `--max-time 10` to prevent indefinite hangs

### keystore-create fixes

The base `keystore-create` Job uses the AM image, which has no `jq`. Two overlay patches:

- `keystore-type-patch.yaml` — overrides the initContainer command to download a static `jq` binary from GitHub releases before running the script; sets `KEYSTORE_TYPE=jceks` to skip the first `jq` call
- `role-binding.yaml` — patches the RoleBinding `subjects[0].namespace` from the hardcoded `prod` to `fr-platform`

---

## Init Container Flow After FBC Changes

```
load-config-clone (config-loader image)
  AM:  git clone gitea/customer-config/am  → /custom/config/services
  IDM: git clone gitea/customer-config/idm → /custom/config
  → prints "config-loader done" on success

filesystem-init (am|idm image, unchanged)
  → if /custom/config exists: cp image defaults + overlay /custom/config → /fbc
  → else: cp image defaults → /fbc

truststore-init (am|idm image, unchanged)
  → import PEM certs → /new-truststore

Main container
  → reads config from /fbc (AM) or /fbc/conf,/fbc/ui,/fbc/script (IDM)
  → CATALINA_USER_OPTS passes -Dam.server.fqdn=prod.iam.example.com to JVM (AM only)
```

## customer-config Repo Structure and Path Mapping

The `forgerock/customer-config` Gitea repo layout maps to pod paths as follows:

```
Repo path                          →  Pod path (AM)
am/                                →  /home/forgerock/openam/config/services/
am/realm/root/...                  →  /home/forgerock/openam/config/services/realm/root/...

Repo path                          →  Pod path (IDM)
idm/                               →  /home/forgerock/openam/config/  (IDM uses JSON_REPLACE)
```

The translation chain for AM:
```
Gitea: am/         (DESTINATION_PATH = /custom/config/services)
         ↓ load-config-clone copies to
       /custom/config/services/
         ↓ filesystem-init copies /custom/config → /fbc/config
       /fbc/config/services/
         ↓ fbc volume mounted at /home/forgerock/openam
       /home/forgerock/openam/config/services/
```

### Updating config from a running AM pod

To export live config from a pod and push it to Gitea:

```sh
# 1. Start Gitea port-forward (separate terminal, keep running)
kubectl port-forward -n fr-platform svc/gitea 3000:3000

# 2. Copy services config out of the pod
AM_POD=$(kubectl get pod -n fr-platform -l app=am -o jsonpath='{.items[0].metadata.name}')
kubectl cp -n fr-platform ${AM_POD}:/home/forgerock/openam/config/services /tmp/am-services

# 3. Clone the repo, place files at the correct level, push
git clone http://forgerock:forgerock@localhost:3000/forgerock/customer-config /tmp/customer-config-repo
cp -r /tmp/am-services/. /tmp/customer-config-repo/am/
cd /tmp/customer-config-repo
git add .
git commit -m "Export AM config from pod"
git push

# 4. Restart AM to pick up the new config
kubectl rollout restart deployment/am -n fr-platform
```

Note: `am/` in the repo maps directly to `config/services/` in the pod — do **not** add a `config/` subdirectory in the repo.

---

## Prerequisites (Must Be Installed Before First Deploy)

These are cluster-wide installs done once on OrbStack:

### 1. cert-manager
Required by DS for SSL certificate generation and by the TLS overlay:
```sh
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml
kubectl rollout status deployment/cert-manager -n cert-manager --timeout=120s
```

### 2. nginx ingress controller
ForgeOps uses `ingressClassName: nginx` but does not install the controller:
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

### 3. mittwald kubernetes-secret-generator
Watches Secret annotations and populates random values. **Must be running before DS is deployed** — DS reads the `ds-passwords` Secret during first-init to set its admin password. If this operator isn't running when DS first starts, the admin password will be empty and `ds-set-passwords` will permanently fail.
```sh
helm repo add mittwald https://helm.mittwald.de
helm repo update
helm upgrade --install secret-generator mittwald/kubernetes-secret-generator \
  --namespace secret-generator --create-namespace --wait
```

---

## Overlay Changes for Local Dev (already committed)

- `kustomize/overlay/default/secrets/kustomization.yaml` — switched to `secret-generator` mode
- `kustomize/overlay/default/am/kustomization.yaml` — switched to `secret-generator` mode
- `kustomize/overlay/default/idm/kustomization.yaml` — switched to `secret-generator` mode
- `kustomize/overlay/default/ds-idrepo/sts.yaml` — `storageClassName: local-path` (was `fast`)
- `kustomize/overlay/default/ds-cts/sts.yaml` — `storageClassName: local-path` (was `fast`)
- `kustomize/overlay/default/base/platform-config.yaml` — FQDN `prod.iam.example.com`, `AM_SERVER_FQDN: prod.iam.example.com`
- `/etc/hosts` must have `127.0.0.1 prod.iam.example.com`

---

## Deploy Order (Critical)

Wrong order causes DS to initialize with an empty admin password, which cannot be recovered without wiping PVCs.

**Correct order:**
1. Install cert-manager, nginx ingress, and mittwald (cluster-wide, once)
2. Build config-loader image: `docker build -t config-loader:local docker/config-loader/`
3. Create namespace: `kubectl create namespace fr-platform`
4. Deploy Gitea: `kubectl apply -k kustomize/overlay/default/gitea/`
5. Seed customer-config repo: `kubectl apply -k kustomize/overlay/default/gitea-seed/`
6. Deploy DS and secrets: `bin/forgeops apply -e default -n fr-platform base ds-cts ds-idrepo`
7. Wait for `ds-set-passwords` Job to complete
8. Deploy keystore-create: `kubectl apply -k kustomize/overlay/default/keystore-create/`
9. Wait for `keystore-create` Job to complete (downloads `jq` via curl — needs internet)
10. Issue TLS cert: `kubectl apply -k kustomize/overlay/default/tls/`
11. Deploy AM and IDM: `bin/forgeops apply -e default -n fr-platform am idm`

**Recovery if DS initialized with empty secret:**
```sh
kubectl delete statefulset ds-idrepo ds-cts -n fr-platform
kubectl delete pvc -n fr-platform -l app=ds-idrepo
kubectl delete pvc -n fr-platform -l app=ds-cts
kubectl delete job ds-set-passwords -n fr-platform
bin/forgeops apply -e default -n fr-platform base ds-cts ds-idrepo
```

---

## Health Check

AM is accessed via HTTPS through nginx. Direct HTTP health check via pod port-forward:

```sh
AM_POD=$(kubectl get pod -n fr-platform -l app=am -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n fr-platform pod/$AM_POD 18080:8080 > /tmp/pf-am.log 2>&1 &
sleep 4
curl -si http://localhost:18080/am/json/health/live
kill %1 2>/dev/null
```

A successful deploy returns `HTTP/1.1 200`. The body may be empty — that is normal for this AM version.

IDM health check:
```sh
IDM_POD=$(kubectl get pod -n fr-platform -l app=idm -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n fr-platform pod/$IDM_POD 18180:8080 > /tmp/pf-idm.log 2>&1 &
sleep 4
curl -s http://localhost:18180/openidm/info/ping
kill %1 2>/dev/null
```

Must return `{"state":"ACTIVE_READY"}`.

Via nginx (full HTTPS flow):
```sh
NGINX_POD=$(kubectl get pod -n ingress-nginx -l app.kubernetes.io/component=controller -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n ingress-nginx pod/$NGINX_POD 18443:443 > /tmp/pf-nginx.log 2>&1 &
sleep 3
curl -sk -D - "https://localhost:18443/am/XUI/" -H "Host: prod.iam.example.com" --max-redirs 0 | grep "HTTP/"
kill %1 2>/dev/null
# Expected: HTTP/2 200
```

---

## Accessing the Gitea Git Server

Gitea is an in-cluster service only (no ingress). Access it via port-forward:

```sh
kubectl port-forward -n fr-platform svc/gitea 3000:3000
```

Keep that command running, then open **http://localhost:3000** in a browser.

- **Username:** `forgerock`
- **Password:** `forgerock`
- **Repo:** `http://localhost:3000/forgerock/customer-config`

The port-forward must stay in the foreground — Ctrl+C closes the connection.

---

## Browser Access

`bin/tunnel` port-forwards the nginx ingress controller's port 443 to `sudo` localhost:443:
```sh
bin/tunnel        # start
bin/tunnel stop   # stop
```

Requires `/etc/hosts` to have `127.0.0.1 prod.iam.example.com`.
Requires `sudo` (port 443 is privileged).

URLs:
- AM:  `https://prod.iam.example.com/am`
- IDM: `https://prod.iam.example.com/openidm`

The cert is self-signed — accept the browser warning.

### Why port-forwarding is still needed on OrbStack

Although nginx runs with `hostNetwork=true` and binds to ports 80/443 on the OrbStack node IP (`192.168.139.2`), that IP is **not directly reachable** from the Mac. CrowdStrike and other corporate security tooling block direct access to the OrbStack VM network — the same reason Colima's bridged networking was abandoned.

The port-forward works because it tunnels over OrbStack's stable loopback (`127.0.0.1`) rather than routing through the VM network, which is unaffected by the security tooling. The key advantage over Colima is that OrbStack's port-forward never flaps — Colima went over an SSH tunnel that dropped intermittently.

---

## Key File Locations

```
docker/config-loader/Dockerfile                                    — config-loader image (Alpine + git + jq)
docker/config-loader/clone-and-copy.sh                             — loader script
docker/docker-bake.hcl                                             — added config-loader build target
kustomize/base/am/secret-generator/am-deployment.yaml             — load-config-clone init container
kustomize/base/am/secret-agent/am-deployment.yaml                 — load-config-clone init container
kustomize/base/am/secret-generator/am-service.yaml                — targetPort: http (was https)
kustomize/base/am/secret-agent/am-service.yaml                    — targetPort: http (was https)
kustomize/base/am/secret-generator/am-ingress.yaml                — nginx, TLS
kustomize/base/am/secret-agent/am-ingress.yaml                    — nginx, TLS
kustomize/base/idm/secret-generator/idm-deployment.yaml           — load-config-clone init container
kustomize/base/idm/secret-agent/idm-deployment.yaml               — load-config-clone init container
kustomize/base/idm/secret-generator/idm-ingress.yaml              — nginx, TLS
kustomize/base/idm/secret-agent/idm-ingress.yaml                  — nginx, TLS
kustomize/base/gitea/                                              — Gitea Deployment, Service, PVC
kustomize/base/gitea-seed/                                         — seed Job + ConfigMap
kustomize/overlay/default/gitea/                                   — overlay for Gitea
kustomize/overlay/default/gitea-seed/                              — overlay for seed job
kustomize/overlay/default/tls/certificate.yaml                     — ClusterIssuer + Certificate (platform-tls)
kustomize/overlay/default/tls/kustomization.yaml                   — TLS overlay kustomization
kustomize/overlay/default/kustomization.yaml                       — includes gitea + gitea-seed + tls
kustomize/overlay/default/image-defaulter/kustomization.yaml       — config-loader:local image mapping
kustomize/overlay/default/keystore-create/keystore-type-patch.yaml — jq download + KEYSTORE_TYPE fix
kustomize/overlay/default/keystore-create/role-binding.yaml        — namespace patched to fr-platform
kustomize/overlay/default/am/deployment.yaml                       — CATALINA_USER_OPTS for am.server.fqdn
kustomize/overlay/default/am/ingress-fqdn.yaml                     — host/TLS patched to prod.iam.example.com
kustomize/overlay/default/idm/deployment.yaml                      — overlay patch uses load-config-clone
kustomize/overlay/default/idm/ingress-fqdn.yaml                    — host/TLS patched to prod.iam.example.com
kustomize/overlay/default/base/platform-config.yaml                — FQDN + AM_SERVER_FQDN = prod.iam.example.com
kustomize/overlay/default/ds-idrepo/sts.yaml                       — storageClassName: local-path
kustomize/overlay/default/ds-cts/sts.yaml                          — storageClassName: local-path
bin/tunnel                                                          — port-forwards nginx 443 for browser access
colima.md                                                           — Colima notes (superseded by OrbStack)
.claude/commands/deploy-fbc.md                                      — /deploy-fbc slash command
.claude/settings.json                                               — allow rules for kubectl/docker/forgeops
```

---

## Claude Code Slash Command

`.claude/commands/deploy-fbc.md` defines a `/deploy-fbc` slash command that automates the full deploy sequence. Requires a Claude Code restart to appear.

The command covers all 11 steps: prerequisites (cert-manager + nginx + mittwald), config-loader build, namespace, Gitea, seed, DS+secrets (with ds-set-passwords wait and recovery instructions), keystore-create, TLS cert, AM+IDM, and health checks.

---

## Known Issues / Gotchas

- **`AM_SERVER_FQDN` alone is not enough** — must also be in `CATALINA_USER_OPTS` as `-Dam.server.fqdn=...` for AM to use it as a JVM property. The env var is consumed by the shell entrypoint but the JVM only reads system properties.
- **`proxy-redirect-from`/`proxy-redirect-to` annotations are not used** — these were an earlier attempt at a workaround and have been removed. The AM redirect issue is fully solved by the `CATALINA_USER_OPTS` JVM properties fix.
- **DS admin password is permanent** — if DS starts before the mittwald operator populates `ds-passwords`, the password will be blank and cannot be changed. Must wipe PVCs and redeploy.
- **Gitea `DEFAULT_ADMIN_*` env vars don't work** in gitea:1.22 without running the install wizard. Admin user is created via `lifecycle.postStart` hook instead.
- **keystore-create needs internet** — downloads a static `jq` binary from GitHub releases at runtime.
- **`bin/tunnel` requires sudo** — port 443 is privileged on macOS.
