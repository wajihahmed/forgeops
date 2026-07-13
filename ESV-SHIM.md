# ESV Shim — AIC-like Environment Secrets & Variables API for ForgeOps

## Context

AIC's Environment Secrets & Variables (ESV) feature lets operators manage config/secret values through a REST API and have the platform "apply" them without redeploying. ForgeOps (open source) has no equivalent — config today is set at deploy time via Kustomize overlays, Helm values, and hand-edited ConfigMaps/Secrets (`platform-config`, `am-env-secrets`, etc.), with manual `kubectl rollout restart` to pick up changes (as already documented in this repo's CLAUDE.md).

The goal is a small sidecar **control-plane service** — not a change to AM/IDM internals, not a fake AIC backend — that exposes an ESV-shaped HTTP API (create/list/update/delete variable or secret, plus apply/restart) and translates those calls onto Kubernetes Secrets/ConfigMaps that AM/IDM already consume. It runs inside the `fr-platform` namespace like every other piece of this dev stack (Gitea, keystore-create), reusing patterns already proven in this fork rather than inventing new ones.

Decisions already made with the user:
- **Language:** Python + FastAPI (matches repo's existing python3 usage in `bin/`, has a mature k8s client)
- **Storage model:** one Kubernetes object per ESV item (`esv-var-<name>` ConfigMap, `esv-secret-<name>` Secret) — trivial CRUD, per-item metadata via annotations
- **Apply mechanism:** aggregate all ESV items into a projection object consumed via `envFrom`, then roll-restart AM/IDM
- **Auth:** none for now — ClusterIP only, cluster-internal, same access model as Gitea

## Architecture

```
Client (curl / script)
   │  HTTP :8080
   ▼
esv-shim (FastAPI, Deployment+Service in fr-platform)
   │  uses Kubernetes Python client (in-cluster ServiceAccount token)
   ▼
Per-item objects (source of truth, CRUD target):
   ConfigMap  esv-var-<name>      data: {value: "..."}     annotations: description, updatedAt
   Secret     esv-secret-<name>   data: {value: "..."}     annotations: description, updatedAt
   label on both: esv.forgeops/managed=true, esv.forgeops/type=variable|secret

"Apply" step (triggered by POST /environment/apply):
   1. List all esv-var-* / esv-secret-* objects by label
   2. Project their key/value pairs into two aggregate objects that AM/IDM already mount via envFrom:
        ConfigMap  esv-variables   (merged variable data)
        Secret     esv-secrets     (merged secret data)
   3. Patch Deployment am and Deployment idm with a
      `esv.forgeops/restartedAt: <timestamp>` pod-template annotation
      → triggers a rollout restart (same effect as the documented
        `kubectl rollout restart deployment/am -n fr-platform`)
```

Per-item objects stay the CRUD surface (so `GET /environment/variables/foo` is a direct 1:1 read of `esv-var-foo`); the aggregate `esv-variables`/`esv-secrets` objects exist purely so AM/IDM can consume everything through a single stable `envFrom` reference, instead of the shim having to rewrite the Deployment spec every time an item is added.

**Why not merge directly into `platform-config`:** AM's `envFrom` pulls `platform-config`, but IDM's `envFrom` pulls its own `idm` ConfigMap — there's no single object both containers already read from. Introducing new `esv-variables`/`esv-secrets` objects (shipped empty as base resources, referenced by a new `envFrom` entry added to both `am` and `idm` overlay deployment patches) avoids commingling shim-owned runtime state with the chart/kustomize-declared `platform-config` and `idm` ConfigMaps, which are re-applied verbatim on every `bin/forgeops apply`.

## API Surface

All routes prefixed `/environment` to look AIC-ESV-shaped:

| Method | Path | Action |
|---|---|---|
| POST | `/environment/variables` | create variable `{name, value, description?}` |
| GET | `/environment/variables` | list variables |
| GET | `/environment/variables/{name}` | get one |
| PUT | `/environment/variables/{name}` | update value/description |
| DELETE | `/environment/variables/{name}` | delete |
| POST | `/environment/secrets` | create secret `{name, value, description?}` |
| GET | `/environment/secrets` | list secrets (values redacted) |
| GET | `/environment/secrets/{name}?showSecretValue=true` | get one (redacted by default) |
| PUT | `/environment/secrets/{name}` | update value/description |
| DELETE | `/environment/secrets/{name}` | delete |
| POST | `/environment/apply` | run the apply step described above (project + restart am/idm) |

## RBAC

Namespaced `Role` (not `ClusterRole` — shim only ever touches `fr-platform`), modeled on `keystore-create`'s RBAC (`charts/identity-platform/templates/keystore-create-rbac-clusterrole.yaml`) but scoped down:

```yaml
rules:
  - apiGroups: [""]
    resources: ["configmaps", "secrets"]
    verbs: ["get","list","watch","create","update","patch","delete"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get","list","watch","patch"]
```
Bound to a dedicated `esv-shim` ServiceAccount via a namespaced `RoleBinding`.

## Files to Add / Modify

**New Docker image** (mirrors `docker/config-loader/`):
- `docker/esv-shim/Dockerfile` — `python:3.12-slim`, installs `fastapi`, `uvicorn`, `kubernetes` (python client)
- `docker/esv-shim/requirements.txt`
- `docker/esv-shim/app/main.py` — FastAPI app implementing the routes above, using the in-cluster k8s client config (`kubernetes.config.load_incluster_config()`)

**New Kustomize base** (mirrors `kustomize/base/gitea/`):
- `kustomize/base/esv-shim/kustomization.yaml`
- `kustomize/base/esv-shim/esv-shim-deployment.yaml` — single container, `serviceAccountName: esv-shim`
- `kustomize/base/esv-shim/esv-shim-service.yaml` — ClusterIP, port 8080
- `kustomize/base/esv-shim/esv-shim-rbac.yaml` — ServiceAccount + Role + RoleBinding (see above)
- `kustomize/base/esv-shim/esv-projection-configmap.yaml` — empty `esv-variables` ConfigMap (shipped pre-created so AM/IDM don't crash-loop on first boot before the shim writes anything)
- `kustomize/base/esv-shim/esv-projection-secret.yaml` — empty `esv-secrets` Secret, same reasoning

**Overlay wiring** (mirrors `kustomize/overlay/default/gitea/`):
- `kustomize/overlay/default/esv-shim/kustomization.yaml` — references base + `../image-defaulter` component
- Add `- ./esv-shim` to the `resources:` list in `kustomize/overlay/default/kustomization.yaml`, same always-on treatment Gitea got — no `bin/commands/common.sh` changes needed, it deploys with every `bin/forgeops apply`

**Image defaulter:**
- Add to `kustomize/overlay/default/image-defaulter/kustomization.yaml`:
  ```yaml
  - name: esv-shim
    newName: esv-shim
    newTag: local
  ```

**Docker build:**
- `docker/docker-bake.hcl` — add `ESV_SHIM_FROM_IMAGE` variable and an `esv-shim` target (same shape as the existing `config-loader` target)

**AM/IDM envFrom wiring** (small, targeted patches):
- `kustomize/overlay/default/am/deployment.yaml` — add to the `openam` container's `envFrom`:
  ```yaml
  - configMapRef: {name: esv-variables, optional: true}
  - secretRef: {name: esv-secrets, optional: true}
  ```
- `kustomize/overlay/default/idm/deployment.yaml` — same two entries added to the `openidm` container

`optional: true` is defensive; the objects are also pre-shipped as empty in `kustomize/base/esv-shim/`, so ordering with respect to the documented deploy sequence (step 11, AM/IDM last) is safe either way.

## Verification

1. `docker build -t esv-shim:local docker/esv-shim/`
2. `bin/forgeops apply -e default -n fr-platform base` (or full deploy per CLAUDE.md's Deploy Order) — confirm `esv-shim` Deployment/Service/Role come up: `kubectl get pods,svc -n fr-platform -l app=esv-shim`
3. Port-forward: `kubectl port-forward -n fr-platform svc/esv-shim 8090:8080`
4. Exercise the API:
   ```sh
   curl -s -X POST localhost:8090/environment/variables -d '{"name":"esv-test","value":"hello"}' -H 'Content-Type: application/json'
   curl -s localhost:8090/environment/variables
   curl -s -X POST localhost:8090/environment/apply
   ```
5. Confirm projection + restart: `kubectl get cm esv-variables -n fr-platform -o yaml` shows the merged key, and `kubectl rollout status deployment/am -n fr-platform` shows a fresh rollout after the apply call.
6. Confirm AM/IDM pods actually see the value: `kubectl exec <am-pod> -n fr-platform -- env | grep esv-test`.
