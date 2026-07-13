# /deploy-mock-tenant

Deploy the FBC (File Based Configuration) dev stack on OrbStack, step by step.

Execute these steps in order. After each step, verify it succeeded before continuing.
If a step fails, report the error and stop — do not continue to the next step.

## Step 0 — Prerequisites

**Kubernetes runtime: OrbStack** — OrbStack provides stable Kubernetes on `127.0.0.1:26443`
with no SSH tunnel and no socket_vmnet issues. The OrbStack node IP is still blocked by
CrowdStrike but the API and port-forwards run over loopback and are unaffected.
Start OrbStack from the macOS menu bar or `open -a OrbStack`.

1. Verify the OrbStack context is active:
   ```
   kubectl config current-context
   ```
   It must show `orbstack`. If not, run `kubectl config use-context orbstack` and confirm.

2. Verify StorageClass `fast` has been patched to `local-path`:
   ```
   grep storageClassName kustomize/overlay/default/ds-idrepo/sts.yaml kustomize/overlay/default/ds-cts/sts.yaml
   ```
   If it still says `fast`, patch it:
   ```
   sed -i '' 's/storageClassName: fast/storageClassName: local-path/g' \
     kustomize/overlay/default/ds-idrepo/sts.yaml \
     kustomize/overlay/default/ds-cts/sts.yaml
   ```

3. Verify FQDN and AM_SERVER_FQDN are set to `prod.iam.example.com` in `kustomize/overlay/default/base/platform-config.yaml`:
   ```
   grep -E "FQDN|AM_SERVER_FQDN" kustomize/overlay/default/base/platform-config.yaml
   ```
   Must show both `FQDN: "prod.iam.example.com"` and `AM_SERVER_FQDN: "prod.iam.example.com"`.
   If either is missing or wrong, update the file. Both are required — `AM_SERVER_FQDN` alone is
   not enough; it must also be passed as a JVM property via `CATALINA_USER_OPTS` in
   `kustomize/overlay/default/am/deployment.yaml`.

4. Verify `/etc/hosts` has `127.0.0.1` mapped to `prod.iam.example.com`:
   ```
   grep prod.iam.example.com /etc/hosts
   ```
   It must show `127.0.0.1 prod.iam.example.com`. If wrong:
   ```
   sudo sed -i '' 's/.*prod.iam.example.com.*/127.0.0.1 prod.iam.example.com/' /etc/hosts
   ```

5. Install cert-manager if not already installed:
   ```
   kubectl get namespace cert-manager 2>/dev/null || kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml
   kubectl rollout status deployment/cert-manager -n cert-manager --timeout=120s
   ```

6. Install the nginx ingress controller if not already installed (ForgeOps uses `ingressClassName: nginx`
   but does NOT ship the controller itself):
   ```
   kubectl get namespace ingress-nginx 2>/dev/null || \
     helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx && \
     helm repo update ingress-nginx && \
     helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
       --namespace ingress-nginx --create-namespace \
       --set controller.hostNetwork=true \
       --set controller.kind=DaemonSet \
       --set controller.service.type=ClusterIP \
       --wait
   kubectl rollout status daemonset/ingress-nginx-controller -n ingress-nginx --timeout=120s
   ```

7. Install the mittwald kubernetes-secret-generator if not already installed:
   ```
   helm repo add mittwald https://helm.mittwald.de 2>/dev/null || true
   helm repo update mittwald
   helm upgrade --install secret-generator mittwald/kubernetes-secret-generator \
     --namespace secret-generator --create-namespace --wait
   ```

## Step 1 — Build the config-loader and esv-shim images

```
docker build -t config-loader:local docker/config-loader/
docker build -t esv-shim:local docker/esv-shim/
```

Verify with `docker images config-loader esv-shim` — must show `config-loader:local` and `esv-shim:local`.

**OrbStack docker-context gotcha:** OrbStack's Kubernetes pulls images from OrbStack's own Docker
daemon, not necessarily whatever `docker` context is currently active on your Mac (e.g. if Colima
is also installed, `docker build` may silently build into Colima's image store instead). Check the
active context with `docker context ls`; if it's not `orbstack`, build with:
```
docker --context orbstack build -t config-loader:local docker/config-loader/
docker --context orbstack build -t esv-shim:local docker/esv-shim/
```
Symptom if this is wrong: pods for `config-loader`/`esv-shim`-based images show `ImagePullBackOff`
with `pull access denied for <image>, repository does not exist` even though `docker images` shows
the tag locally — that's because it exists in the wrong daemon's store.

## Step 2 — Create the namespace

Check if namespace exists:
```
kubectl get namespace fr-platform
```

If it doesn't exist:
```
kubectl create namespace fr-platform
kubectl config set-context --current --namespace=fr-platform
```

## Step 3 — Deploy Gitea

```
kubectl apply -k kustomize/overlay/default/gitea/
kubectl rollout status deployment/gitea -n fr-platform --timeout=120s
```

Confirm the pod is Running before continuing.

## Step 4 — Seed the customer-config repo

```
kubectl apply -k kustomize/overlay/default/gitea-seed/
kubectl wait --for=condition=complete job/gitea-seed -n fr-platform --timeout=120s
```

Check seed logs:
```
kubectl logs job/gitea-seed -n fr-platform -c seed
```

The last line must be `Seeding complete`. If not, report the logs and stop.

## Step 5 — Deploy the ESV shim

The ESV shim is a small FastAPI service that exposes an AIC ESV-compatible API
(`PUT`-upsert `/environment/variables/{_id}` and `/environment/secrets/{_id}`, `POST
/environment/restart`) backed by per-item Kubernetes ConfigMaps/Secrets, so ESV exports from a real
AIC tenant can be imported into this dev stack. It is not wired into `bin/forgeops apply`'s
component list (same as Gitea) — deploy it directly via kustomize:
```
kubectl apply -k kustomize/overlay/default/esv-shim/
kubectl rollout status deployment/esv-shim -n fr-platform --timeout=60s
```

This also creates the empty `esv-variables` ConfigMap and `esv-secrets` Secret that AM/IDM's
`envFrom` references — safe to create now since AM/IDM aren't deployed yet.

Verify:
```
kubectl get pods,svc -n fr-platform -l app=esv-shim
```

**This step only deploys the shim — it does not import any ESV data.** `esv-variables`/
`esv-secrets` stay empty through the rest of this deploy. Populating them from a real export file
(e.g. `openam-perf-banc_esv-export.json`) via `tenant_util.py esv import --apply` is a manual
follow-up today; see `ESV-SHIM.md`'s Roadmap/TODO and Verification sections for the exact command.
The end goal is for `lodestar.py`/`tenant_util.py` to be able to run against this deployment as a
mock AIC tenant — that's not wired into this skill yet.

## Step 6 — Deploy DS and secrets

**IMPORTANT:** DS reads the `ds-passwords` Secret during its very first startup to set the admin
password. If the secret is empty when DS first starts, `ds-set-passwords` will permanently fail
and AM/IDM will not be able to connect. The mittwald operator (installed in Step 0) must be
running before this step.

```
bin/forgeops apply -e default -n fr-platform base ds-cts ds-idrepo
```

Wait for DS StatefulSets to be ready:
```
kubectl rollout status statefulset/ds-cts -n fr-platform --timeout=300s
kubectl rollout status statefulset/ds-idrepo -n fr-platform --timeout=300s
```

Wait for ds-set-passwords to complete — this MUST succeed before deploying AM/IDM:
```
kubectl wait --for=condition=complete job/ds-set-passwords -n fr-platform --timeout=120s
```

If `ds-set-passwords` fails with "Invalid Credentials", DS was initialized with an empty secret. Recovery:
```
kubectl delete statefulset ds-idrepo ds-cts -n fr-platform
kubectl delete pvc -n fr-platform -l app=ds-idrepo
kubectl delete pvc -n fr-platform -l app=ds-cts
kubectl delete job ds-set-passwords -n fr-platform
bin/forgeops apply -e default -n fr-platform base ds-cts ds-idrepo
kubectl rollout status statefulset/ds-cts -n fr-platform --timeout=300s
kubectl rollout status statefulset/ds-idrepo -n fr-platform --timeout=300s
kubectl wait --for=condition=complete job/ds-set-passwords -n fr-platform --timeout=120s
```

## Step 7 — Deploy keystore-create Job

AM requires a `keystore` Secret before it can start. Deploy the keystore-create Job and wait for it:
```
kubectl apply -k kustomize/overlay/default/keystore-create/
kubectl wait --for=condition=complete job/keystore-create -n fr-platform --timeout=120s
```

Verify the secret was created:
```
kubectl get secret keystore -n fr-platform
```

Must show `keystore   Opaque   1`. If the job fails, check logs:
```
kubectl logs -n fr-platform -l job-name=keystore-create -c keystore-create --tail=20
```

The patch downloads a static `jq` binary from GitHub releases — internet access from within the
cluster is required.

## Step 8 — Issue the TLS certificate

Before deploying AM/IDM, issue the self-signed TLS cert for `prod.iam.example.com`:
```
kubectl apply -k kustomize/overlay/default/tls/
kubectl wait --for=condition=Ready certificate/platform-tls -n fr-platform --timeout=60s
```

Verify:
```
kubectl get secret platform-tls -n fr-platform
```
Must show `platform-tls   kubernetes.io/tls   2`. If cert-manager is not ready yet, wait 30s and retry.

## Step 9 — Deploy AM, IDM, and admin-ui

```
bin/forgeops apply -e default -n fr-platform am idm admin-ui
```

## Step 10 — Deploy amster and fix idm-resource-server secret

Amster bootstraps AM with OAuth2 clients (idm-admin-ui, idm-resource-server, idm-provisioning, etc.).
Without it the admin-ui OAuth2 flow has nothing to authenticate against.

```
bin/forgeops apply -e default -n fr-platform amster
kubectl wait --for=condition=complete job/amster -n fr-platform --timeout=300s
kubectl logs job/amster -n fr-platform -c amster | tail -5
```

The last line must be `Import done`. If it says `Amster import errors`, stop and investigate.

**Fix idm-resource-server secret (required — amster does not set this correctly):**

Amster imports the `idm-resource-server` OAuth2 client but the `&{idm.rs.client.secret|...}`
variable substitution does not reliably set the client secret in AM. IDM uses this client to
introspect Bearer tokens; if the secret is wrong, IDM hangs for 30 seconds on every authenticated
request and the admin-ui shows a blank page.

1. Open the AM console at `https://prod.iam.example.com/am/ui-admin/`
2. Go to **Applications → OAuth 2.0 → Clients → idm-resource-server → Core**
3. Set **Client secret** to the value of `IDM_RS_CLIENT_SECRET` from the `amster-env-secrets`
   Kubernetes secret:
   ```
   kubectl get secret amster-env-secrets -n fr-platform \
     -o jsonpath='{.data.IDM_RS_CLIENT_SECRET}' | base64 -d
   ```
4. Save the client in the AM console.

Then patch the Kubernetes secret so IDM's env var matches whatever value is now in AM:
```
kubectl patch secret amster-env-secrets -n fr-platform \
  --type='json' \
  -p='[{"op":"replace","path":"/data/IDM_RS_CLIENT_SECRET","value":"'"$(kubectl get secret amster-env-secrets -n fr-platform -o jsonpath='{.data.IDM_RS_CLIENT_SECRET}')"'"}]'
kubectl rollout restart deployment/idm -n fr-platform
kubectl rollout status deployment/idm -n fr-platform --timeout=120s
```

Verify IDM can now introspect tokens (should return in under 1 second):
```
PROV_SECRET=$(kubectl get secret amster-env-secrets -n fr-platform \
  -o jsonpath='{.data.IDM_PROVISIONING_CLIENT_SECRET}' | base64 -d)
ACCESS_TOKEN=$(curl -sk -X POST "https://prod.iam.example.com/am/oauth2/access_token" \
  -d "grant_type=client_credentials&scope=fr:idm:*" \
  -u "idm-provisioning:$PROV_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
IDM_POD=$(kubectl get pod -n fr-platform -l app=idm -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n fr-platform pod/$IDM_POD 18180:8080 >/tmp/pf-idm-check.log 2>&1 &
sleep 3
curl -si --max-time 5 http://localhost:18180/openidm/info/uiconfig \
  -H "Authorization: Bearer $ACCESS_TOKEN" | head -1
kill %1 2>/dev/null
```
Must return `HTTP/1.1 200`.

## Step 11 — Verify FBC init containers

Check AM init container logs:
```
kubectl logs -n fr-platform -l app=am -c load-config-clone
kubectl logs -n fr-platform -l app=am -c filesystem-init
```

Check IDM init container logs:
```
kubectl logs -n fr-platform -l app=idm -c load-config-clone
kubectl logs -n fr-platform -l app=idm -c filesystem-init
```

`load-config-clone` must contain `config-loader done`.
`filesystem-init` must not contain any errors.

## Step 12 — Health check

Wait for AM, IDM, and admin-ui to roll out (up to 5 min each):
```
kubectl rollout status deployment/am -n fr-platform --timeout=300s
kubectl rollout status deployment/idm -n fr-platform --timeout=300s
kubectl rollout status deployment/admin-ui -n fr-platform --timeout=300s
```

Health-check AM via pod port-forward:
```
AM_POD=$(kubectl get pod -n fr-platform -l app=am -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n fr-platform pod/$AM_POD 18080:8080 > /tmp/pf-am.log 2>&1 &
sleep 4
curl -si http://localhost:18080/am/json/health/live
kill %1 2>/dev/null
```

A successful deploy returns `HTTP/1.1 200` (body may be empty — that is normal for this AM version).

Check IDM:
```
IDM_POD=$(kubectl get pod -n fr-platform -l app=idm -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n fr-platform pod/$IDM_POD 18180:8080 > /tmp/pf-idm.log 2>&1 &
sleep 4
curl -s http://localhost:18180/openidm/info/ping
kill %1 2>/dev/null
```

IDM must return `{"state":"ACTIVE_READY"}`.

Check admin-ui:
```
ADMIN_UI_POD=$(kubectl get pod -n fr-platform -l app=admin-ui -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward -n fr-platform pod/$ADMIN_UI_POD 18280:8080 > /tmp/pf-admin-ui.log 2>&1 &
sleep 4
curl -si http://localhost:18280/ | head -1
kill %1 2>/dev/null
```

Must return `HTTP/1.1 200`.

Show the final pod status:
```
kubectl get pods -n fr-platform
```

Expected: am `1/1 Running`, idm `1/1 Running`, admin-ui `1/1 Running`, ds-cts `1/1 Running`,
ds-idrepo `1/1 Running`, gitea `1/1 Running`, esv-shim `1/1 Running`, keystore-create `Completed`,
ds-set-passwords `Completed`.

## Step 13 — Browser access

AM and IDM are exposed via nginx at `https://prod.iam.example.com`.
`/etc/hosts` must have `127.0.0.1 prod.iam.example.com`.

To access from a browser, port-forward nginx port 443 to localhost:
```
bin/tunnel
```

This forwards the nginx ingress controller port 443 → `sudo` localhost:443 (requires sudo for privileged port).
Note: even with `hostNetwork=true`, the OrbStack node IP is blocked by CrowdStrike — the port-forward
over loopback is required and works reliably.

URLs:
- AM:       `https://prod.iam.example.com/am`        (self-signed cert — accept browser warning)
- IDM:      `https://prod.iam.example.com/openidm`
- Admin UI: `https://prod.iam.example.com/platform`

To stop: `bin/tunnel stop`
