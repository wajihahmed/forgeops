# Deploy & Test on Colima

These instructions assume you have already started Colima with:
```sh
colima start --profile forgeops --cpu 4 --memory 12 --disk 100 --runtime docker --kubernetes
```

---

## Prerequisites — do these once

```sh
# Confirm colima context is active
kubectl config current-context
# Should show: colima-forgeops

# Confirm the default StorageClass (Colima uses "local-path")
kubectl get storageclass
```

**Fix the DS StorageClass** — the default overlay hardcodes `fast` which doesn't exist on Colima:

```sh
sed -i '' 's/storageClassName: fast/storageClassName: local-path/g' \
  kustomize/overlay/default/ds-idrepo/sts.yaml \
  kustomize/overlay/default/ds-cts/sts.yaml
```

**Fix the FQDN** — `forgeops` needs a real hostname. For local testing use `localhost`:

Edit `kustomize/overlay/default/base/platform-config.yaml`, change:
```yaml
FQDN: "prod.iam.example.com"
```
to:
```yaml
FQDN: "localhost"
```

**Install forgeops Python deps:**

```sh
bin/forgeops configure
```

---

## Step 1 — Build the `config-loader` image

Colima's docker daemon is the same one Kubernetes uses, so you just need to build — no `kind load` needed.

```sh
docker build -t config-loader:local docker/config-loader/
```

Verify it's there:
```sh
docker images config-loader
```

---

## Step 2 — Create the namespace

```sh
kubectl create namespace fr-platform
kubectl config set-context --current --namespace=fr-platform
```

---

## Step 3 — Deploy Gitea

```sh
kubectl apply -k kustomize/overlay/default/gitea/
```

Wait for Gitea to be ready:
```sh
kubectl rollout status deployment/gitea -n fr-platform --timeout=120s
```

---

## Step 4 — Seed the `customer-config` repo

```sh
kubectl apply -k kustomize/overlay/default/gitea-seed/
```

Watch the seed job complete:
```sh
kubectl wait --for=condition=complete job/gitea-seed -n fr-platform --timeout=120s
kubectl logs job/gitea-seed -n fr-platform -c seed
```

Expected output ends with: `Seeding complete`

Verify the repo exists in Gitea by port-forwarding:
```sh
kubectl port-forward svc/gitea 3000:3000 -n fr-platform &
# Open http://localhost:3000 in browser
# Login: forgerock / forgerock
# You should see the forgerock/customer-config repo
kill %1
```

---

## Step 5 — Deploy the platform

Deploy secrets and DS first (DS takes the longest):
```sh
bin/forgeops apply -e default -n fr-platform -c base secrets ds-cts ds-idrepo
```

Wait for DS to be ready (takes 3-5 min):
```sh
kubectl rollout status statefulset/ds-cts -n fr-platform --timeout=300s
kubectl rollout status statefulset/ds-idrepo -n fr-platform --timeout=300s
```

Then deploy AM and IDM:
```sh
bin/forgeops apply -e default -n fr-platform am idm
```

---

## Step 6 — Watch the FBC init containers

This is the key thing to verify. As AM starts, watch the init container logs:

```sh
# Get the AM pod name
kubectl get pods -n fr-platform -l app=am

# Watch load-config-clone init container
kubectl logs -n fr-platform -l app=am -c load-config-clone --follow

# Watch filesystem-init after it
kubectl logs -n fr-platform -l app=am -c filesystem-init
```

Expected from `load-config-clone`:
```
Cloning http://gitea.fr-platform.svc.cluster.local:3000/... branch=master
config-loader done
```

Expected from `filesystem-init`:
```
Copying docker image configuration files to the shared volume
```
(This means the clone worked but no override JSON was found — correct for the initial stub seed.)

Same check for IDM:
```sh
kubectl logs -n fr-platform -l app=idm -c load-config-clone
kubectl logs -n fr-platform -l app=idm -c filesystem-init
```

---

## Step 7 — Confirm AM and IDM came up

```sh
kubectl get pods -n fr-platform
```

All pods should be `Running`. AM can take 3-4 min to pass its startup probe.

```sh
# Quick health check (port-forward AM)
kubectl port-forward svc/am 8080:80 -n fr-platform &
curl -s http://localhost:8080/am/json/health/live | python3 -m json.tool
kill %1
```

Expected: `{"status":"alive"}`

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `load-config-clone` fails with connection refused | Gitea pod not ready, or seed job didn't finish — check `kubectl get pods -n fr-platform` |
| DS PVC stuck in `Pending` | StorageClass `fast` not patched — run the `sed` from Prerequisites again |
| AM stuck in `Init:0/3` | `load-config-clone` init container failed — check its logs |
| `filesystem-init` says "Existing openam config found, skipping" | AM pod restarted and `/fbc` already has data — this is fine |
| Gitea seed job fails | Check `kubectl logs job/gitea-seed -n fr-platform -c seed` for details |
