# ESV Shim — AIC-like Environment Secrets & Variables API for ForgeOps

## Context

AIC's Environment Secrets & Variables (ESV) feature lets operators manage config/secret values through a REST API and have the platform "apply" them without redeploying. ForgeOps (open source) has no equivalent — config today is set at deploy time via Kustomize overlays, Helm values, and hand-edited ConfigMaps/Secrets (`platform-config`, `am-env-secrets`, etc.), with manual `kubectl rollout restart` to pick up changes (as already documented in this repo's CLAUDE.md).

The goal is a small sidecar **control-plane service** — not a change to AM/IDM internals, not a fake AIC backend — that exposes an ESV-shaped HTTP API (create/list/update/delete variable or secret, plus apply/restart) and translates those calls onto Kubernetes Secrets/ConfigMaps that AM/IDM already consume. It runs inside the `fr-platform` namespace like every other piece of this dev stack (Gitea, keystore-create), reusing patterns already proven in this fork rather than inventing new ones.

Decisions already made with the user:
- **Language:** Python + FastAPI (matches repo's existing python3 usage in `bin/`, has a mature k8s client)
- **Storage model:** one Kubernetes object per ESV item (`esv-var-<id>` ConfigMap, `esv-secret-<id>` Secret) — trivial CRUD, per-item metadata via annotations
- **Apply mechanism:** aggregate all ESV items into a projection object consumed via `envFrom`, then roll-restart AM/IDM
- **Auth:** none for now — ClusterIP only, cluster-internal, same access model as Gitea

**Wire-format compatibility validated against real AIC clients.** The initial version of this API used a made-up shape (`POST` to create, plain `value` field, `name`/`showSecretValue` params). That was checked against the actual ESV write path used in production tooling — `/Users/wajih.ahmed/work/qa-lodestar-fork-mock-api/shared/scripts/tenant_util.py` and `shared/lib/tenant/tenant_config_importer.py` (byte-identical ESV logic also lives in `perf-tools/tenant_util.py`) — and it didn't match. The real AIC ESV API:
- Has **no POST-to-create**. Every write is `PUT /environment/{secrets,variables}/{_id}`, which upserts: `201` if the id didn't exist, `200` if it did.
- Sends values as `valueBase64` (always base64-encoded), never a plain `value` field.
- Secrets carry `encoding` (`generic`/`pem`/`base64hmac`/`base64aes`) and `useInPlaceholders`; variables carry `expressionType` (`string`/`number`/`int`/`object`/`array`).
- Identifies items by `_id` in responses, not `name`.
- Never returns a secret's value on GET — `GET /environment/secrets/{_id}` returns metadata only (encoding, description, version). Lodestar retrieves clear-text secret values through a separate IDM script-eval endpoint, not the ESV API itself.
- Has a `POST /environment/restart` endpoint (not `/environment/apply`) that AM must be sent after an ESV import, since ESVs aren't hot-swappable.

The API below reflects the corrected, verified contract. It was validated by running lodestar's actual `tenant_util.py esv import --apply` CLI against a live deployment of this shim, using the real `openam-perf-banc_esv-export.json` export file (11 secrets, 13 variables) — all 24 items imported with `201` on first run and `200` on re-run, and decoded values (including a nested JSON object variable) matched the source file exactly after projection.

## Architecture

```
Client (curl / script)
   │  HTTP :8080
   ▼
esv-shim (FastAPI, Deployment+Service in fr-platform)
   │  uses Kubernetes Python client (in-cluster ServiceAccount token)
   ▼
Per-item objects (source of truth, PUT-upsert target):
   ConfigMap  esv-var-<_id>      data: {value: "<plain>"}       annotations: description, expressionType, updatedAt
   Secret     esv-secret-<_id>   data: {value: "<valueBase64>"} annotations: description, encoding, useInPlaceholders, updatedAt
   label on both: esv.forgeops/managed=true, esv.forgeops/type=variable|secret

   Note: for secrets, the client-supplied valueBase64 is stored verbatim as the
   Secret's data value (Kubernetes Secret.data is itself base64, so no re-encoding
   happens). For variables, valueBase64 is decoded once and the plain value is
   stored in the ConfigMap, then re-encoded back to valueBase64 on GET.

"Restart" step (triggered by POST /environment/restart, POST /environment/apply as a compatibility alias):
   1. List all esv-var-* / esv-secret-* objects by label
   2. Project their key/value pairs into two aggregate objects that AM/IDM already mount via envFrom:
        ConfigMap  esv-variables   (merged variable data, plain values)
        Secret     esv-secrets     (merged secret data, plain values)
   3. Patch Deployment am and Deployment idm with a
      `esv.forgeops/restarted-at: <timestamp>` pod-template annotation
      → triggers a rollout restart (same effect as the documented
        `kubectl rollout restart deployment/am -n fr-platform`)
```

Per-item objects stay the CRUD surface (so `GET /environment/variables/foo` is a direct 1:1 read of `esv-var-foo`); the aggregate `esv-variables`/`esv-secrets` objects exist purely so AM/IDM can consume everything through a single stable `envFrom` reference, instead of the shim having to rewrite the Deployment spec every time an item is added.

**Why a separate restart step exists at all when real AIC's PUT is already durable:** it is — a `PUT` to `/environment/secrets/{_id}` in this shim is immediately persisted to its ConfigMap/Secret, matching AIC. But AM/IDM here read ESV data via `envFrom` referencing the aggregate `esv-variables`/`esv-secrets` objects, not via a live property store, so `/environment/restart` (mirroring what `tenant_config_importer.py`'s `_restart_am_for_esv()` actually calls) both re-projects and restarts, matching the real "ESVs aren't hot-swappable, AM must restart" behavior lodestar already codes around.

**Why not merge directly into `platform-config`:** AM's `envFrom` pulls `platform-config`, but IDM's `envFrom` pulls its own `idm` ConfigMap — there's no single object both containers already read from. Introducing new `esv-variables`/`esv-secrets` objects (shipped empty as base resources, referenced by a new `envFrom` entry added to both `am` and `idm` overlay deployment patches) avoids commingling shim-owned runtime state with the chart/kustomize-declared `platform-config` and `idm` ConfigMaps, which are re-applied verbatim on every `bin/forgeops apply`.

## API Surface

Matches the real AIC ESV wire contract, verified against lodestar's `tenant_util.py`:

| Method | Path | Body / Response |
|---|---|---|
| GET | `/environment/variables` | `{"result": [...], "resultCount": N}`, each item `{_id, valueBase64, description, expressionType, lastChangeDate, lastChangedBy, loaded}` |
| GET | `/environment/variables/{_id}` | single item, same shape |
| PUT | `/environment/variables/{_id}` | request `{valueBase64, description?, expressionType?}` — upsert: `201` created / `200` updated |
| DELETE | `/environment/variables/{_id}` | `204` |
| GET | `/environment/secrets` | `{"result": [...], "resultCount": N}`, each item `{_id, activeVersion, loadedVersion, description, encoding, useInPlaceholders, lastChangeDate, lastChangedBy, loaded}` — **no value field**, matching real AIC |
| GET | `/environment/secrets/{_id}` | single item metadata, same shape (no value) |
| PUT | `/environment/secrets/{_id}` | request `{valueBase64, description?, encoding?, useInPlaceholders?}` — upsert: `201` created / `200` updated |
| DELETE | `/environment/secrets/{_id}` | `204` |
| POST | `/environment/restart` | project all items into `esv-variables`/`esv-secrets` + rolling-restart am/idm; `{variableCount, secretCount, restarted}` |
| POST | `/environment/apply` | alias of `/environment/restart`, kept for convenience — not part of the real AIC API |

No `POST`-to-create exists on `/environment/variables` or `/environment/secrets` collections — this intentionally matches AIC, where creation and update are both done via `PUT .../{_id}`.

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

## Roadmap / TODO

**Goal: run `lodestar.py` (or `tenant_util.py`) against this ForgeOps deployment as if it were a real AIC "mock tenant."**

Current state: `/deploy-mock-tenant` deploys the esv-shim service itself (Step 5) but does not
import any ESV data — `esv-variables`/`esv-secrets` stay empty through the rest of the deploy.
Populating them from a real export file (e.g. `openam-perf-banc_esv-export.json`) is a manual
step today, run by hand against the shim's `esv import` endpoint after the deploy finishes (see
Verification section below).

Not yet done, needed for lodestar to treat this as a drop-in tenant target:
- [ ] Wire an actual `esv import --apply` run into `/deploy-mock-tenant` (or a separate step/script)
  so a fresh deploy ends with ESVs already populated, not just the shim running empty.
- [ ] Same treatment for the *other* domains lodestar's `tenant_util.py` manages against a tenant —
  journeys, OAuth2 clients, scripts, IDM managed objects/endpoints, secret store mappings, SAML2 —
  none of which this shim (or ForgeOps generally) has an import path for yet. ESV was the first
  slice; the rest of `tenant_util.py`'s domains are still real-AIC-only.
- [ ] Decide whether the eventual "mock tenant" import is meant to be idempotent/re-runnable as
  part of every deploy, or a one-time seed step like `gitea-seed`.

## Verification

1. `docker build -t esv-shim:local docker/esv-shim/` (on OrbStack, build against the OrbStack docker context, e.g. `docker --context orbstack build ...`, since its Kubernetes pulls images from that daemon's local image store, not whichever context happens to be active)
2. `bin/forgeops apply -e default -n fr-platform base` (or full deploy per CLAUDE.md's Deploy Order) — confirm `esv-shim` Deployment/Service/Role come up: `kubectl get pods,svc -n fr-platform -l app=esv-shim`
3. Port-forward: `kubectl port-forward -n fr-platform svc/esv-shim 8090:8080`
4. Real-client verification (strongest check — this is what was actually run): use lodestar's own CLI against the shim instead of hand-written curl:
   ```sh
   echo "faketoken" > at.txt   # tenant_util.py just needs a bearer token file to exist
   python3 /Users/wajih.ahmed/work/qa-lodestar-fork-mock-api/shared/scripts/tenant_util.py esv import \
     --target http://localhost:8090 \
     --file /Users/wajih.ahmed/work/qa-lodestar-fork-mock-api/shared/config/tenant-customer-configurations/banc/openam-perf-banc_esv-export.json \
     --apply
   python3 /Users/wajih.ahmed/work/qa-lodestar-fork-mock-api/shared/scripts/tenant_util.py esv list --source http://localhost:8090
   ```
   Expect all secrets/variables to report `OK (HTTP 201)` on first run, `OK (HTTP 200)` on re-run, and `esv list` to show correct `_id`, `encoding`/`expressionType`, and `description` for every item.
5. Confirm projection + restart: `curl -X POST localhost:8090/environment/restart` then `kubectl get cm esv-variables -n fr-platform -o yaml` / `kubectl get secret esv-secrets -n fr-platform -o yaml` show merged, correctly-decoded data, and `kubectl rollout status deployment/am -n fr-platform` shows a fresh rollout.
6. Confirm AM/IDM pods actually see the value: `kubectl exec <am-pod> -n fr-platform -- env | grep <esv-id>`.
