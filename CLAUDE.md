# ForgeOps — FBC Dev Stack Project

## What We Are Building

A branch of ForgeOps that adds **File Based Configuration (FBC)** — meaning AM and IDM load their configuration from a local git repository at pod startup rather than from config baked into the Docker image.

The `bin/forgeops` CLI (already in this repo) is the deployment tool. No standalone Python script is needed. The branch modifies the kustomize manifests and adds a `config-loader` component; `forgeops apply` and `forgeops build` continue to work as the user-facing interface.

The target is a `fr-platform` namespace on a **local Kubernetes cluster** (Colima). Config loading is **read-only** at pod startup — no config-saver / round-trip back to git needed.

---

## Background and Context

This work originated from an investigation of the `ForgeCloud/saas` monorepo (at `/Users/wajih.ahmed/source/github.com/ForgeCloud/saas`). That repo is the full production SaaS platform for Ping Identity, deeply coupled to GCP (GKE Workload Identity, Google Secret Manager, Google Cloud Source Repositories, GCP-specific StorageClasses). It is not directly deployable to a non-GKE cluster without significant rework.

**Why ForgeOps instead of the saas repo:**
- ForgeOps is the public open-source base — no GCP coupling, plain Kubernetes Secrets, standard StorageClasses
- Already uses Kustomize, same products (AM, IDM, DS), same image base
- Already has an FBC init-container hook point (see below)
- Full investigation notes live in `/Users/wajih.ahmed/source/github.com/ForgeCloud/saas/CLAUDE.md`

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

### Gitea in-cluster git server

Single Gitea pod serves the `forgerock/customer-config` repo at `http://gitea.fr-platform.svc.cluster.local:3000`.

Key implementation detail: Gitea's s6 init supervisor must start as root. The pod has **no** `runAsUser` set. An `init-dirs` init container (running as root) pre-creates `/data/git/.ssh` and `/data/gitea` with ownership `1000:1000` before Gitea starts.

### Gitea seed job

A one-time Job (`gitea-seed`) uses the Gitea REST API (via `curl`) to create the admin user and `forgerock/customer-config` repo, then `git push` the initial AM and IDM config stubs.

Key implementation detail: uses `curl -u user:pass` not `wget` — Alpine BusyBox wget lacks `--user`/`--password`. Uses `alpine:3.19` image (not `alpine/git` which has git as its ENTRYPOINT and can't run a shell).

### keystore-create fixes

The base `keystore-create` Job uses the AM image, which has no `jq` and a stripped-down non-writeable apt cache. Two overlay patches in `kustomize/overlay/default/keystore-create/`:

- `keystore-type-patch.yaml` — overrides the initContainer command to download a static `jq` binary from GitHub releases before running the script; sets `KEYSTORE_TYPE=jceks` to skip the first `jq` call
- `role-binding.yaml` — patches the RoleBinding `subjects[0].namespace` from the hardcoded `prod` to `fr-platform`

---

## Init Container Flow After FBC Changes

```
load-config-clone (config-loader image)
  → git clone gitea/customer-config → /tmp/customer-config
  → copy /tmp/customer-config/am|idm → /custom/config
  → prints "config-loader done" on success

filesystem-init (am|idm image, unchanged)
  → if /custom/config exists: cp image defaults + overlay /custom/config → /fbc
  → else: cp image defaults → /fbc

truststore-init (am|idm image, unchanged)
  → import PEM certs → /new-truststore

Main container
  → reads config from /fbc (AM) or /fbc/conf,/fbc/ui,/fbc/script (IDM)
```

---

## Prerequisites (Must Be Installed Before First Deploy)

These are cluster-wide installs done once:

### 1. cert-manager
Required by DS for SSL certificate generation (even with `secret-generator` mode):
```sh
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml
kubectl rollout status deployment/cert-manager -n cert-manager --timeout=120s
```

### 2. mittwald kubernetes-secret-generator
Watches Secret annotations and populates random values. **Must be running before DS is deployed** — DS reads the `ds-passwords` Secret during first-init to set its admin password. If this operator isn't running when DS first starts, the admin password will be empty and `ds-set-passwords` will permanently fail.
```sh
helm repo add mittwald https://helm.mittwald.de
helm repo update
helm upgrade --install secret-generator mittwald/kubernetes-secret-generator \
  --namespace secret-generator --create-namespace --wait
```

---

## Overlay Changes for Local Dev (already committed)

The default overlay uses `secret-agent`. For local Colima clusters (no Secret Agent operator), these were switched to `secret-generator`:

- `kustomize/overlay/default/secrets/kustomization.yaml`
- `kustomize/overlay/default/am/kustomization.yaml`
- `kustomize/overlay/default/idm/kustomization.yaml`

---

## Colima-Specific Notes

- StorageClass patched from `fast` → `local-path` in `kustomize/overlay/default/ds-idrepo/sts.yaml` and `ds-cts/sts.yaml`
- FQDN changed from `prod.iam.example.com` → `localhost` in `kustomize/overlay/default/base/platform-config.yaml`
- No `kind load` needed — Colima's docker daemon is shared with Kubernetes
- Full step-by-step manual instructions in `colima.md`

---

## Deploy Order (Critical)

Wrong order causes DS to initialize with an empty admin password, which cannot be recovered without wiping PVCs.

**Correct order:**
1. Install cert-manager and mittwald (cluster-wide, once)
2. `bin/forgeops apply -e default -n fr-platform base ds-cts ds-idrepo`
3. Wait for `ds-set-passwords` Job to complete
4. `kubectl apply -k kustomize/overlay/default/keystore-create/`
5. Wait for `keystore-create` Job to complete (downloads `jq` via curl — needs internet)
6. `bin/forgeops apply -e default -n fr-platform am idm`

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

AM's service port 80 maps to the HTTPS container port (8081), not HTTP. To health-check via HTTP, port-forward directly to the pod's port 8080:

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

---

## Key File Locations

```
docker/config-loader/Dockerfile                              — config-loader image (Alpine + git + jq)
docker/config-loader/clone-and-copy.sh                       — loader script
docker/docker-bake.hcl                                       — added config-loader build target
kustomize/base/am/secret-generator/am-deployment.yaml        — load-config-clone init container
kustomize/base/am/secret-agent/am-deployment.yaml            — load-config-clone init container
kustomize/base/idm/secret-generator/idm-deployment.yaml      — load-config-clone init container
kustomize/base/idm/secret-agent/idm-deployment.yaml          — load-config-clone init container
kustomize/base/gitea/                                        — Gitea Deployment, Service, PVC
kustomize/base/gitea-seed/                                   — seed Job + ConfigMap
kustomize/overlay/default/gitea/                             — overlay for Gitea
kustomize/overlay/default/gitea-seed/                        — overlay for seed job
kustomize/overlay/default/kustomization.yaml                 — includes gitea + gitea-seed
kustomize/overlay/default/image-defaulter/kustomization.yaml — config-loader:local image mapping
kustomize/overlay/default/keystore-create/keystore-type-patch.yaml — jq download + KEYSTORE_TYPE fix
kustomize/overlay/default/keystore-create/role-binding.yaml  — namespace patched to fr-platform
kustomize/overlay/default/am/deployment.yaml                 — overlay patch uses load-config-clone
kustomize/overlay/default/idm/deployment.yaml                — overlay patch uses load-config-clone
kustomize/overlay/default/base/platform-config.yaml          — FQDN set to localhost
kustomize/overlay/default/ds-idrepo/sts.yaml                 — storageClassName: local-path
kustomize/overlay/default/ds-cts/sts.yaml                    — storageClassName: local-path
colima.md                                                    — full manual deploy instructions
.claude/commands/deploy-fbc.md                               — /deploy-fbc slash command
.claude/settings.json                                        — allow rules for kubectl/docker/forgeops
```

---

## Claude Code Slash Command

`.claude/commands/deploy-fbc.md` defines a `/deploy-fbc` slash command that automates the full deploy sequence. Requires a Claude Code restart to appear after creation.

The command covers all 9 steps: prerequisites (cert-manager + mittwald), config-loader build, namespace, Gitea, seed, DS+secrets (with ds-set-passwords wait and recovery instructions), keystore-create, AM+IDM, and health checks for both AM and IDM.
